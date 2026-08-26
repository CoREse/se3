"""Tests for the self_check step handler."""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType, FlowStatus
from tianluo.engine.steps.self_check import (
    self_check_handler,
    _format_changes,
    _format_review_scope,
    _format_test_results,
    _format_fix_context,
    _issue_signature,
    _validate_and_filter_issues,
    _fold_still_present_into_current,
    _FOLD_MARKER,
    SELF_CHECK_PROMPT,
)


def _valid_issue(
    severity: str = "high",
    quote: str = "Implement feature X",
    path: str = "src/feature.py",
    line: int = 42,
    actual: str = "returns None when input is empty",
    expected: str = "returns an empty list",
    divergence: str = "callers iterating the result get TypeError",
) -> dict:
    """Build a fully-populated issue dict matching the new self_check
    schema. Used by tests that exercise the post-validation revision
    path (issues that should survive ``_validate_and_filter_issues``).

    Defaults align with the standard ``flow`` / ``step`` fixtures:
    ``task_description`` contains "Implement feature X" so the verbatim
    quote substring-matches; ``changes_made.files_changed`` contains
    ``src/feature.py`` so evidence_lines validate.
    """
    return {
        "severity": severity,
        "actual_behavior": actual,
        "expected_behavior": expected,
        "divergence": divergence,
        "expectation_source": {
            "type": "task_description",
            "verbatim_quote": quote,
        },
        "evidence_lines": [f"{path}:{line}"],
        "missing_in": [],
    }


class TestSelfCheckHandler:
    """Test cases for self_check_handler."""

    @pytest.fixture
    def flow(self, tmp_path):
        flow = FlowInstance(
            flow_id="test-flow-sc",
            task_description="Implement feature X",
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "test-change",
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
            StepType.VERIFY_SPEC,
        ]
        return flow

    @pytest.fixture
    def step(self):
        return Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Implement feature X",
                "changes_made": {
                    "files_changed": [
                        {"path": "src/feature.py", "action": "create", "explanation": "New feature module"},
                    ]
                },
                "test_results": {"passed": True, "returncode": 0, "stdout": "All tests passed"},
                "spec_content": {"base": "Base spec content"},
            },
        )

    def test_returns_completed_when_no_issues(self, flow, step):
        response = json.dumps({
            "issues": [],
            "summary": "Implementation looks solid.",
        })

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["actionable_count"] == 0
        assert step.outputs["issues"] == []

    def test_returns_revision_needed_when_medium_low_issues(self, flow, step):
        step.inputs["fix_iteration"] = 0
        response = json.dumps({
            "issues": [
                _valid_issue(severity="medium", line=42,
                             actual="lacks defensive check",
                             expected="validates the input length",
                             divergence="empty input crashes downstream"),
                _valid_issue(severity="low", line=10,
                             actual="silent failure path",
                             expected="logs at WARNING level",
                             divergence="bugs are invisible in production"),
            ],
            "summary": "Minor suggestions only.",
        })

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["actionable_count"] == 2
        assert len(step.outputs["issues"]) == 2
        assert step.outputs["fix_needed"] is True

    def test_returns_revision_needed_with_critical_issues(self, flow, step):
        step.inputs["fix_iteration"] = 0
        response = json.dumps({
            "issues": [
                _valid_issue(severity="critical", line=30,
                             actual="dereferences None on missing key",
                             expected="returns default sentinel",
                             divergence="AttributeError crashes the request handler"),
            ],
            "summary": "Critical issue found.",
        })

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["actionable_count"] == 1
        assert step.outputs["fix_needed"] is True
        assert step.outputs["fix_context"]["reason"] == "self_check"
        assert len(step.outputs["fix_context"]["issues"]) == 1
        assert step.outputs["fix_context"]["iteration"] == 1

    def test_returns_revision_needed_with_high_issues(self, flow, step):
        step.inputs["fix_iteration"] = 0
        response = json.dumps({
            "issues": [
                _valid_issue(severity="high", line=55,
                             actual="unhandled error path on disk full",
                             expected="catches OSError and retries",
                             divergence="long-running uploads crash partway"),
                _valid_issue(severity="medium", line=10,
                             actual="suggestion-level concern",
                             expected="documented invariant",
                             divergence="future drift risk"),
            ],
            "summary": "Issues found.",
        })

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["actionable_count"] == 2
        assert step.outputs["fix_instructions"]
        assert "unhandled error path on disk full" in step.outputs["fix_instructions"]

    def test_returns_revision_needed_at_max_iterations(self, flow, step):
        """self_check returns REVISION_NEEDED even at max iterations.

        Exhaustion is handled centrally by state_machine.transition_to_next.
        """
        step.inputs["fix_iteration"] = 3
        response = json.dumps({
            "issues": [
                _valid_issue(severity="critical", line=30,
                             actual="bug from prev iteration still present",
                             expected="bug fixed",
                             divergence="same crash continues"),
            ],
            "summary": "Issue persists.",
        })

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["actionable_count"] == 1

    def test_returns_failed_on_llm_error(self, flow, step):
        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.side_effect = RuntimeError("LLM timeout")
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.FAILED
        assert "LLM timeout" in step.error_message

    def test_returns_failed_on_unparseable_response(self, flow, step):
        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = "not valid json at all"
            mock_cls.return_value = mock_caller

            with patch("tianluo.engine.steps.self_check.parse_json_response", return_value=None):
                result = self_check_handler(step, flow)

        assert result == StepStatus.FAILED
        assert step.error_message

    def test_fix_context_contains_all_issues(self, flow, step):
        step.inputs["fix_iteration"] = 0
        # Multiple files in changes_made so each issue's evidence_lines
        # path validates.
        step.inputs["changes_made"] = {
            "files_changed": [
                {"path": "src/feature.py", "action": "create"},
                {"path": "a.py", "action": "modify"},
                {"path": "b.py", "action": "modify"},
                {"path": "c.py", "action": "modify"},
                {"path": "d.py", "action": "modify"},
            ]
        }
        response = json.dumps({
            "issues": [
                _valid_issue(severity="critical", path="a.py", line=1,
                             actual="critical bug", expected="works",
                             divergence="crash"),
                _valid_issue(severity="medium", path="b.py", line=1,
                             actual="medium concern", expected="addressed",
                             divergence="edge case"),
                _valid_issue(severity="high", path="c.py", line=1,
                             actual="missing handler", expected="handled",
                             divergence="error swallowed"),
                _valid_issue(severity="low", path="d.py", line=1,
                             actual="nit", expected="cleaned",
                             divergence="readability"),
            ],
            "summary": "Mixed.",
        })

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["actionable_count"] == 4
        fix_issues = step.outputs["fix_context"]["issues"]
        assert len(fix_issues) == 4
        severities = {i["severity"] for i in fix_issues}
        assert severities == {"critical", "high", "medium", "low"}

    def test_uses_two_phase_json_mode(self, flow, step):
        response = json.dumps({"issues": [], "summary": "OK"})

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            self_check_handler(step, flow)

            call_kwargs = mock_caller.call.call_args[1]
            assert call_kwargs["json_mode"] == "two_phase"
            assert "json_schema_hint" in call_kwargs

    def test_prompt_excludes_plan_as_requirement_authority(self):
        assert "Never treat them as requirement sources" in SELF_CHECK_PROMPT
        assert "plan_task" in SELF_CHECK_PROMPT

    def test_prompt_excludes_version_decisions(self):
        # Version bump decisions belong to the downstream version_analyze step;
        # the fix-loop checker must not report them as issues (separation of duties).
        assert "version_analyze" in SELF_CHECK_PROMPT
        assert "pyproject.toml" in SELF_CHECK_PROMPT
        assert "version" in SELF_CHECK_PROMPT.lower()

    def test_prompt_includes_review_dimensions(self):
        assert "Requirement Completeness" in SELF_CHECK_PROMPT
        assert "Behavioral Correctness" in SELF_CHECK_PROMPT
        assert "Cross-Module Integration" in SELF_CHECK_PROMPT
        assert "Regression Safety" in SELF_CHECK_PROMPT
        assert "Robustness" in SELF_CHECK_PROMPT
        assert "Test Coverage" in SELF_CHECK_PROMPT

    def test_prompt_uses_severity_not_priority(self):
        assert '"severity":' in SELF_CHECK_PROMPT
        assert "critical|high|medium|low" in SELF_CHECK_PROMPT

    def test_fix_iteration_passed_to_prompt(self, flow, step):
        step.inputs["fix_iteration"] = 2
        response = json.dumps({"issues": [], "summary": "OK"})

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            self_check_handler(step, flow)

            prompt = mock_caller.call.call_args[1]["prompt"]
            assert "Fix iteration: 2" in prompt

    def test_zero_sentinel_in_inputs_is_honored(self, flow, step):
        """An explicit max_fix_iterations=0 in inputs must survive to outputs and prompt.

        Regression: the previous `or` short-circuit silently fell back to config
        whenever inputs supplied 0 (the unlimited sentinel), letting an
        upstream/config skew go undetected.
        """
        step.inputs["fix_iteration"] = 5
        step.inputs["max_fix_iterations"] = 0
        response = json.dumps({
            "issues": [
                _valid_issue(severity="high",
                             actual="returns wrong type", expected="returns dict",
                             divergence="callers crash"),
            ],
            "summary": "issue",
        })

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            # The handler must honor an explicit ``max_fix_iterations=0`` from
            # inputs rather than silently falling back to config. The fallback
            # path (_fallback_max_fix_iterations) would return DEFAULT
            # (currently 100), so a regression would surface as "of 100"
            # in the prompt and a non-zero value in outputs.
            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["max_fix_iterations"] == 0
        prompt = mock_caller.call.call_args[1]["prompt"]
        assert "unlimited" in prompt.lower()
        assert "of 100" not in prompt

    def test_stores_self_check_result(self, flow, step):
        llm_result = {
            "issues": [{"severity": "low", "description": "Minor", "location": "x.py"}],
            "summary": "Mostly fine.",
        }
        response = json.dumps(llm_result)

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            self_check_handler(step, flow)

        assert step.outputs["self_check_result"]["summary"] == "Mostly fine."
        assert len(step.outputs["self_check_result"]["issues"]) == 1


class TestFormatChanges:
    def test_empty(self):
        assert _format_changes({}) == "No changes recorded."

    def test_dict_entries(self):
        changes = {
            "files_changed": [
                {"path": "a.py", "action": "modify", "explanation": "Fix bug"},
                {"path": "b.py", "action": "create"},
            ]
        }
        result = _format_changes(changes)
        assert "modify: a.py" in result
        assert "(Fix bug)" in result
        assert "create: b.py" in result

    def test_string_entries(self):
        changes = {"files_changed": ["file1.py", "file2.py"]}
        result = _format_changes(changes)
        assert "modified: file1.py" in result
        assert "modified: file2.py" in result

    def test_empty_files_changed(self):
        assert _format_changes({"files_changed": []}) == "Changes made but details unavailable."


class TestFormatTestResults:
    def test_empty(self):
        assert _format_test_results({}) == "No test results available."

    def test_flat_format(self):
        result = _format_test_results({"passed": True, "returncode": 0, "stdout": "OK", "stderr": ""})
        assert "Tests passed: True" in result

    def test_structured_format(self):
        results = {
            "overall_passed": True,
            "phases": [{"name": "default", "passed": True, "returncode": 0}],
            "new_tests": {"count": 0, "passed": [], "failed": []},
            "regression": {"passed": [], "failed": []},
        }
        result = _format_test_results(results)
        assert "Overall passed: True" in result


class TestFormatFixContext:
    def test_initial(self):
        result = _format_fix_context(0, 3)
        assert "initial self-check" in result.lower()

    def test_iteration(self):
        result = _format_fix_context(2, 3)
        assert "Fix iteration: 2 of 3" in result

    def test_max_reached(self):
        result = _format_fix_context(3, 3)
        assert "WARNING" in result
        assert "final fix-loop iteration" in result

    def test_unlimited_sentinel_zero(self):
        """max_iterations=0 sentinel: render 'unlimited', skip final-attempt warning."""
        result = _format_fix_context(7, 0)
        assert "unlimited" in result
        assert "WARNING" not in result
        assert "final fix-loop iteration" not in result

    def test_negative_treated_as_unlimited(self):
        """Negatives are rejected at config load, but if one slips through
        (e.g. via tests that mock max_iterations directly), the format
        helper treats ``<= 0`` as unlimited so rendering matches the
        state_machine's ``> 0`` exhaustion guard exactly.
        """
        result = _format_fix_context(99, -5)
        assert "unlimited" in result
        assert "WARNING" not in result
        assert "of -5" not in result

    def test_unlimited_sentinel_zero_at_initial(self):
        """max_iterations=0 at fix_iteration=0 must not show the final-attempt warning."""
        result = _format_fix_context(0, 0)
        assert "WARNING" not in result
        assert "final fix-loop iteration" not in result

    def test_warning_suppressed_before_boundary(self):
        """Warning must NOT fire while iteration is below the cap."""
        result = _format_fix_context(4, 5)
        assert "WARNING" not in result
        assert "final fix-loop iteration" not in result

    def test_final_attempt_warning_at_boundary(self):
        """When fix_iteration == max_iterations, the final-iteration warning
        text must appear verbatim (handler-level prompt-rendering branch).

        Wording note: the warning intentionally says "final fix-loop iteration"
        rather than "final fix attempt" because by the time self_check sees
        the boundary the IMPLEMENT step already ran for this iteration; what
        remains is the self-check decision (clean vs. issues), not another fix.
        """
        result = _format_fix_context(5, 5)
        assert "WARNING: This is the final fix-loop iteration before exhaustion." in result
        assert "the flow will be marked as FAILED" in result
        # The misleading "fix attempt" wording must NOT reappear.
        assert "final fix attempt" not in result
        # past_final warning must NOT appear at the on-boundary case
        assert "Iteration cap exceeded" not in result


class TestIssueSignature:
    def test_empty_list(self):
        assert _issue_signature([]) == set()

    def test_single_issue(self):
        sig = _issue_signature([
            {"severity": "low", "location": "a.py:1", "description": "x"},
        ])
        assert sig == {("a.py:1", "x")}

    def test_ignores_severity(self):
        """Severity is not part of the mechanical duplicate identity."""
        a = _issue_signature([{"severity": "low", "location": "a", "description": "x"}])
        b = _issue_signature([{"severity": "high", "location": "a", "description": "x"}])
        assert a == b

    def test_skips_non_dict(self):
        sig = _issue_signature(["not-a-dict", {"location": "a", "description": "x"}])
        assert sig == {("a", "x")}

    def test_skips_empty_signature(self):
        sig = _issue_signature([{"location": "", "description": ""}])
        assert sig == set()


class TestDiffScopedEvidence:
    def _inputs(self):
        return {
            "task_description": "Preserve consumer behavior",
            "task_description_base": "Preserve consumer behavior",
            "changes_made": {"files_changed": ["src/cause.py"]},
            "scope_changed_paths": ["src/cause.py"],
            "scope_causal_anchors": {"src/cause.py": [[10, 12]]},
        }

    def test_causal_anchor_keeps_unchanged_impact_evidence(self):
        issue = _valid_issue(
            quote="Preserve consumer behavior", path="src/cause.py", line=11,
        )
        issue["evidence_lines"].append("src/consumer.py:50")

        kept, stats = _validate_and_filter_issues([issue], self._inputs())

        assert kept == [issue]
        assert kept[0]["evidence_lines"][-1] == "src/consumer.py:50"
        assert stats["kept_count"] == 1

    def test_changed_path_outside_causal_hunk_is_rejected(self):
        issue = _valid_issue(
            quote="Preserve consumer behavior", path="src/cause.py", line=9,
        )
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_empty_causal_anchors_degrade_to_changed_path_grounding(self):
        # Binary-only / rename-only / chmod-only diffs produce no hunk lines,
        # so scope_causal_anchors is {} — the checker cannot satisfy the
        # anchor requirement there. The path-in-changed rule must still
        # accept the evidence instead of dropping the finding.
        inputs = self._inputs()
        inputs["scope_causal_anchors"] = {}
        issue = _valid_issue(
            quote="Preserve consumer behavior", path="src/cause.py", line=1,
        )
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["kept_count"] == 1
        assert stats["bad_evidence_count"] == 0

    def test_empty_causal_anchors_regression_grounds_at_path_level(self):
        # An anchor-less changed path (no current-side line exists by
        # construction) grounds a regression at path level: the accompanying
        # line number is ignored, and the finding must reach the fix loop.
        inputs = self._inputs()
        inputs["scope_causal_anchors"] = {}
        issue = _valid_issue(path="src/cause.py", line=1)
        issue["expectation_source"] = {
            "type": "regression",
            "verbatim_quote": "pre-existing behavior",
        }
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0

        # A path outside the current scope grounds nothing, anchors or not.
        issue["evidence_lines"] = ["src/consumer.py:50"]
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_regression_on_bare_gitlink_grounds_at_path_level(self):
        # A fix that only moves a submodule's HEAD yields anchors keyed by the
        # inner file (``vendor/inner.py``) while the bare gitlink path
        # (``vendor``) has no line space of its own. It is anchor-less by
        # construction, so citing it grounds the finding and the meaningless
        # line number is ignored rather than used to reject it.
        inputs = self._inputs()
        inputs["scope_changed_paths"] = ["vendor", "vendor/inner.py"]
        inputs["scope_causal_anchors"] = {"vendor/inner.py": [[1, 2]]}
        issue = _valid_issue(path="vendor", line=99999)
        issue["expectation_source"] = {
            "type": "regression",
            "verbatim_quote": "pre-existing behavior",
        }
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0

        # The anchored inner path grounds it too, with or without a bare-path
        # entry alongside.
        issue["evidence_lines"] = ["vendor", "vendor/inner.py:2"]
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0

        # The inner path DOES have anchors, so a line outside them is still
        # rejected — anchor-bearing paths keep the exact-line requirement.
        issue["evidence_lines"] = ["vendor/inner.py:9"]
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_anchor_less_path_accepts_bare_and_unusable_line_citations(self):
        # Path-level grounding must not depend on the citation's shape: a bare
        # path, a zero line and a non-numeric suffix all name the same
        # anchor-less changed path.
        inputs = self._inputs()
        inputs["scope_changed_paths"] = ["assets/icon.png"]
        inputs["scope_causal_anchors"] = {}
        for citation in ("assets/icon.png", "assets/icon.png:0", "assets/icon.png:n/a"):
            issue = _valid_issue(quote="Preserve consumer behavior")
            issue["evidence_lines"] = [citation]
            kept, stats = _validate_and_filter_issues([issue], inputs)
            assert kept == [issue], citation
            assert stats["bad_evidence_count"] == 0, citation

    def test_path_without_anchor_ranges_in_mixed_diff_degrades(self):
        # One hunk-bearing text file plus one binary asset: evidence on the
        # binary path has no anchor ranges but is a changed path.
        inputs = self._inputs()
        inputs["scope_changed_paths"] = ["src/cause.py", "assets/icon.png"]
        inputs["scope_causal_anchors"] = {"src/cause.py": [[10, 12]]}
        issue = _valid_issue(
            quote="Preserve consumer behavior", path="assets/icon.png", line=1,
        )
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0

    def test_regression_requires_current_scope_causal_anchor(self):
        issue = _valid_issue(path="src/cause.py", line=11)
        issue["expectation_source"] = {
            "type": "regression",
            "verbatim_quote": "pre-existing behavior",
        }
        kept, _ = _validate_and_filter_issues([issue], self._inputs())
        assert kept == [issue]

        issue["evidence_lines"] = ["src/consumer.py:50"]
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_old_side_deletion_range_does_not_anchor_current_line(self):
        # A fix deletes old lines 5-15 from the top of src/foo.py: current
        # line 10 is unchanged code (it was old line 20). The deleted lines'
        # old-side numbers live in the separate deletion space and must never
        # validate a ``path:N`` citation, which names current-file lines.
        inputs = self._inputs()
        inputs["scope_changed_paths"] = ["src/foo.py"]
        inputs["scope_causal_anchors"] = {"src/foo.py": [[3, 3]]}
        inputs["scope_deletion_anchors"] = {"src/foo.py": [[5, 15]]}
        issue = _valid_issue(path="src/foo.py", line=10)
        issue["expectation_source"] = {
            "type": "regression",
            "verbatim_quote": "pre-existing behavior",
        }
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

        # A citation on a real current-file anchor still grounds the finding.
        issue["evidence_lines"] = ["src/foo.py:3"]
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0

    def test_regression_on_fully_deleted_file_grounds_at_path_level(self):
        # A fix deletes src/deleted.py entirely: the path is anchor-less by
        # construction, so it grounds at path level and the old-side line
        # number is ignored. Dropping the finding here would let an
        # evidence-backed defect skip the fix loop entirely.
        inputs = self._inputs()
        inputs["scope_changed_paths"] = ["src/deleted.py"]
        inputs["scope_causal_anchors"] = {}
        inputs["scope_deletion_anchors"] = {"src/deleted.py": [[1, 7]]}
        issue = _valid_issue(path="src/deleted.py", line=7)
        issue["expectation_source"] = {
            "type": "regression",
            "verbatim_quote": "pre-existing behavior",
        }
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0

    def test_non_regression_on_deleted_path_degrades_to_path_grounding(self):
        # Same rule without the regression source type: a deletion-only path
        # carries no current-side line, so the citation only has to name it.
        # ``missing_in`` remains an equally valid channel (see
        # test_requirement_omission_continues_to_use_missing_in).
        inputs = self._inputs()
        inputs["scope_changed_paths"] = ["src/deleted.py"]
        inputs["scope_causal_anchors"] = {}
        inputs["scope_deletion_anchors"] = {"src/deleted.py": [[1, 7]]}
        issue = _valid_issue(
            quote="Preserve consumer behavior", path="src/deleted.py", line=7,
        )
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0

    def test_unusable_anchor_ranges_degrade_to_path_grounding(self):
        # Ranges that cannot be compared against a line number give the path no
        # hittable line space, so demanding an anchor there would drop the
        # finding with no way for the checker to satisfy the requirement.
        inputs = self._inputs()
        inputs["scope_causal_anchors"] = {"src/cause.py": [["x", "y"], [3]]}
        issue = _valid_issue(
            quote="Preserve consumer behavior", path="src/cause.py", line=11,
        )
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0

    def test_requirement_omission_continues_to_use_missing_in(self):
        issue = _valid_issue(quote="Preserve consumer behavior")
        issue["evidence_lines"] = []
        issue["missing_in"] = ["src/missing_adapter.py"]
        kept, _ = _validate_and_filter_issues([issue], self._inputs())
        assert kept == [issue]

    def test_incremental_prompt_and_history_carry_scope_audit(self, tmp_path):
        flow = FlowInstance(
            flow_id="scope-history",
            task_description="Preserve consumer behavior",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.context["project_root"] = str(tmp_path)
        step = Step(
            step_type=StepType.SELF_CHECK,
            inputs={
                **self._inputs(),
                "scope_mode": "incremental",
                "requested_scope_mode": "incremental",
                "baseline_id": "fix-1-abcdef123456",
                "scope_diff": (
                    "diff --git a/src/cause.py b/src/cause.py\n"
                    "@@ -10,1 +10,1 @@\n-old\n+new\n"
                ),
                "scope_diff_artifact": "tianluo/state/review.diff",
                "self_check_round_id": "scr-abcdef123456",
                "self_check_pass_index": 2,
                "self_check_passes_required": 3,
                "fix_iteration": 4,
            },
        )
        response = json.dumps({"issues": [], "summary": "clean"})
        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            caller = Mock()
            caller.call.return_value = response
            mock_cls.return_value = caller
            assert self_check_handler(step, flow) == StepStatus.COMPLETED

        prompt = caller.call.call_args.kwargs["prompt"]
        assert "Incremental round" in prompt
        assert "controls attention, never tool permissions" in prompt
        assert "shared state, protocols, data formats" in prompt
        assert "diff --git a/src/cause.py" in prompt
        assert step.outputs["scope_mode"] == "incremental"
        assert step.outputs["self_check_round_id"] == "scr-abcdef123456"
        assert step.outputs["fix_iteration"] == 4

        history_path = (
            tmp_path / "tianluo" / "history" / flow.flow_id
            / f"{step.step_id}.jsonl"
        )
        records = [json.loads(line) for line in history_path.read_text().splitlines()]
        scope_record = next(r for r in records if r.get("type") == "self_check_scope")
        assert scope_record["scope_mode"] == "incremental"
        assert scope_record["baseline_id"] == "fix-1-abcdef123456"
        assert scope_record["scope_changed_paths"] == ["src/cause.py"]
        assert scope_record["fix_iteration"] == 4
        assert scope_record["round_id"] == "scr-abcdef123456"
        assert scope_record["pass_index"] == 2
        assert "files_read" not in scope_record


class TestScopeDiffDelivery:
    """Over-budget diffs are withheld whole; the manifest always stays."""

    def _oversized_inputs(self):
        from tianluo.engine.truncation import SELF_CHECK_SCOPE_DIFF_MAX_CHARS

        head = "HEAD-SENTINEL-LINE\n" + "x" * SELF_CHECK_SCOPE_DIFF_MAX_CHARS
        return {
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": ["big.py", "other.py"],
            "scope_causal_anchors": {"big.py": [[10, 12], [40, 40]]},
            "scope_deletion_anchors": {"other.py": [[7, 9]]},
            "scope_diff": head + "\nTAIL-SENTINEL-LINE\n",
            "scope_diff_artifact": "tianluo/state/review/diffs/abc.diff",
            "scope_undecidable": False,
        }

    def test_large_diff_is_not_inlined_even_partially(self):
        scope = _format_review_scope(self._oversized_inputs())
        # Neither end of the diff may leak: a half diff reads as a whole one.
        assert "HEAD-SENTINEL-LINE" not in scope
        assert "TAIL-SENTINEL-LINE" not in scope
        assert "```diff" not in scope
        assert "NOT INLINED" in scope

    def test_large_diff_keeps_manifest_with_hunk_ranges(self):
        scope = _format_review_scope(self._oversized_inputs())
        assert "scope_manifest" in scope
        assert "big.py: +4 added (whole task) -0 deleted (implementation baseline)" in scope
        assert "added lines (current file) 10-12, 40" in scope
        assert "other.py: +0 added (whole task) -3 deleted (implementation baseline)" in scope
        assert "deleted lines (baseline file) 7-9" in scope

    def test_large_diff_points_at_the_pull_command_and_artifact(self):
        scope = _format_review_scope(self._oversized_inputs())
        assert "luo review-scope diff --baseline implementation" in scope
        assert "tianluo/state/review/diffs/abc.diff" in scope

    def test_small_diff_is_still_inlined_whole(self):
        scope = _format_review_scope({
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": ["small.py"],
            "scope_causal_anchors": {"small.py": [[1, 1]]},
            "scope_diff": "diff --git a/small.py b/small.py\n+new",
            "scope_diff_artifact": "tianluo/state/review/diffs/abc.diff",
            "scope_undecidable": False,
        })
        assert "NOT INLINED" not in scope
        assert "+new" in scope
        assert "small.py: +1 added (whole task) -0 deleted (implementation baseline)" in scope


class TestScopeManifestAndAccess:
    def _incremental(self):
        return {
            "scope_mode": "incremental",
            "baseline_id": "fix-1-abcdef123456",
            "scope_changed_paths": ["src/fix.py"],
            "scope_causal_anchors": {"src/fix.py": [[40, 48]]},
            "scope_deletion_anchors": {},
            "scope_task_available": True,
            "scope_task_changed_paths": ["src/earlier.py", "src/fix.py"],
            "scope_task_causal_anchors": {
                "src/earlier.py": [[3, 5]],
                "src/fix.py": [[10, 15], [40, 48]],
            },
            "scope_task_deletion_anchors": {"src/earlier.py": [[30, 31]]},
            "scope_diff": "diff --git a/src/fix.py b/src/fix.py\n+fixed\n",
            "scope_diff_artifact": "tianluo/state/review/diffs/abc.diff",
        }

    def _full_closure(self):
        return {
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": ["app.py", "other.py"],
            "scope_causal_anchors": {
                "app.py": [[1, 1], [20, 24]],
                "other.py": [[1, 3]],
            },
            "scope_deletion_anchors": {},
            "scope_fix_delta_available": True,
            "scope_fix_delta_baseline_id": "fix-1-abcdef123456",
            "scope_fix_delta_changed_paths": ["app.py", "other.py"],
            "scope_fix_delta_causal_anchors": {
                "app.py": [[20, 24]],
                "other.py": [[1, 3]],
            },
            "scope_fix_delta_deletion_anchors": {},
            "scope_diff": "diff --git a/app.py b/app.py\n+x\n",
        }

    def test_incremental_manifest_separates_fix_delta_from_earlier_work(self):
        rendered = _format_review_scope(self._incremental())
        assert "src/fix.py: +15 added (whole task + this fix, combined) -0 deleted (this fix's baseline)" in rendered
        assert "added lines (current file) 10-15, 40-48" in rendered
        assert "- this fix: added 40-48" in rendered
        assert "- earlier work in this task: added 10-15" in rendered
        # A path only the earlier work touched is listed, and labelled as such.
        # Its deletions are numbered in the IMPLEMENTATION baseline, not in
        # this round's fix baseline, so they get their own labelled line and
        # never enter the head line's ``-N``.
        assert "src/earlier.py: +3 added (whole task + this fix, combined) -0 deleted (this fix's baseline)" in rendered
        assert "- earlier work in this task: added 3-5" in rendered
        assert (
            "deleted across the whole task (old-side numbers of the "
            "implementation baseline, NOT of this round's baseline): "
            "2 deleted lines at 30-31" in rendered
        )

    def test_full_round_manifest_marks_changes_since_last_full_round(self):
        rendered = _format_review_scope(self._full_closure())
        assert "app.py: +6 added (whole task) -0 deleted (implementation baseline)" in rendered
        assert (
            "- changed by fixes since the last full round: added 20-24"
            in rendered
        )
        assert "- already present at the last full round: added 1" in rendered
        assert "- changed by fixes since the last full round: added 1-3" in rendered

    def test_manifest_carries_no_fix_iteration_or_closed_findings(self):
        inputs = self._full_closure()
        inputs["fix_iteration"] = 3
        inputs["self_check_round_reason"] = "full_closure"
        inputs["fix_history"] = [{"iteration": 1, "issues": ["closed one"]}]
        rendered = _format_review_scope(inputs)
        assert "fix_iteration" not in rendered
        assert "closed one" not in rendered
        assert "full_closure" not in rendered

    def test_initial_full_round_has_no_delta_annotation(self):
        inputs = self._full_closure()
        inputs["scope_fix_delta_available"] = False
        inputs["scope_fix_delta_changed_paths"] = []
        inputs["scope_fix_delta_causal_anchors"] = {}
        rendered = _format_review_scope(inputs)
        assert "app.py: +6 added (whole task) -0 deleted (implementation baseline)" in rendered
        assert "since the last full round" not in rendered

    def test_access_block_bans_git_diff_and_names_the_command(self):
        rendered = _format_review_scope(self._incremental(), flow_id="flow-9")
        assert (
            "luo review-scope diff --baseline implementation --flow flow-9"
            in rendered
        )
        assert (
            "luo review-scope diff --baseline implementation --flow flow-9 --stat"
            in rendered
        )
        assert "--path <path>" in rendered
        assert "luo review-scope diff --baseline fix --flow flow-9" in rendered
        assert "do NOT rebuild the review range yourself with `git diff`" in rendered
        assert "NOT a commit" in rendered
        assert "HEAD advances inside a flow" in rendered

    def test_full_round_does_not_advertise_the_fix_baseline_view(self):
        rendered = _format_review_scope(self._full_closure())
        assert "--baseline fix" not in rendered

    def test_undecidable_scope_marks_the_manifest_as_unproven(self):
        inputs = self._incremental()
        inputs["scope_undecidable"] = True
        inputs["scope_diagnostic"] = "baseline unreadable"
        rendered = _format_review_scope(inputs)
        assert "UNPROVEN" in rendered

    def test_manifest_range_list_is_never_truncated(self):
        """Every citable hunk range is written out, however many there are.

        The manifest's contract is that the anchor space it shows IS the space
        evidence validation grounds in. A hidden range is a hunk the checker
        can neither see nor cite, while a citation on it would still validate.
        """
        ranges = [[n * 10, n * 10 + 1] for n in range(1, 60)]
        rendered = _format_review_scope({
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": ["wide.py"],
            "scope_causal_anchors": {"wide.py": ranges},
            "scope_diff": "diff --git a/wide.py b/wide.py\n+x\n",
        })
        assert "more)" not in rendered
        for start, end in ranges:
            assert f"{start}-{end}" in rendered
        assert f"wide.py: +{2 * len(ranges)} added (whole task) -0 deleted (implementation baseline)" in rendered


class TestManifestMarksAnchorLessPaths:
    """A changed path with no added range still says which domain it is from.

    Binary, rename-only, mode-only and deletion-only paths carry no citable
    line, so range-granularity labels alone render them identically — and the
    checker, whose attention the round splits by domain, could not tell a
    binary the current fix added from one an earlier IMPLEMENT added.
    """

    def test_incremental_marks_anchor_less_paths_by_domain(self):
        rendered = _format_review_scope({
            "scope_mode": "incremental",
            "baseline_id": "fix-1-abcdef123456",
            "scope_changed_paths": ["assets/new.png", "src/fix.py"],
            "scope_causal_anchors": {"src/fix.py": [[40, 48]]},
            "scope_deletion_anchors": {},
            "scope_task_available": True,
            "scope_task_changed_paths": [
                "assets/new.png", "assets/old.png", "src/fix.py",
            ],
            "scope_task_causal_anchors": {"src/fix.py": [[10, 15], [40, 48]]},
            "scope_task_deletion_anchors": {},
            "scope_diff": "diff --git a/src/fix.py b/src/fix.py\n+x\n",
        })
        assert (
            "assets/new.png: +0 added (whole task + this fix, combined) -0 deleted "
            "(this fix's baseline) | domain: this fix" in rendered
        )
        assert (
            "assets/old.png: +0 added (whole task + this fix, combined) -0 deleted "
            "(this fix's baseline) | domain: earlier work in this task"
            in rendered
        )
        # A path with ranges keeps its per-range split and gains the same mark.
        assert (
            "domain: this fix + earlier work in this task" in rendered
        )
        assert "- this fix: added 40-48" in rendered

    def test_full_round_marks_anchor_less_paths_by_domain(self):
        rendered = _format_review_scope({
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": ["assets/new.png", "assets/old.png"],
            "scope_causal_anchors": {},
            "scope_deletion_anchors": {},
            "scope_fix_delta_available": True,
            "scope_fix_delta_baseline_id": "fix-1-abcdef123456",
            "scope_fix_delta_changed_paths": ["assets/new.png"],
            "scope_fix_delta_causal_anchors": {},
            "scope_fix_delta_deletion_anchors": {},
            "scope_diff": "diff --git a/assets/new.png b/assets/new.png\n",
        })
        assert (
            "assets/new.png: +0 added (whole task) -0 deleted "
            "(implementation baseline) | domain: changed by fixes since the "
            "last full round" in rendered
        )
        assert (
            "assets/old.png: +0 added (whole task) -0 deleted "
            "(implementation baseline) | domain: already present at the last "
            "full round" in rendered
        )

    def test_anchor_less_path_in_both_domains_shows_both(self):
        """A binary IMPLEMENT changed and this fix changed again says so.

        Path membership alone cannot say it (every delta path is a whole-domain
        path too) and the added remainder is empty by construction, so the mark
        comes from the persisted baseline-snapshot comparison.
        """
        rendered = _format_review_scope({
            "scope_mode": "incremental",
            "baseline_id": "fix-1-abcdef123456",
            "scope_changed_paths": ["assets/shared.png", "assets/new.png"],
            "scope_causal_anchors": {},
            "scope_deletion_anchors": {},
            "scope_task_available": True,
            "scope_task_changed_paths": ["assets/shared.png", "assets/new.png"],
            "scope_task_causal_anchors": {},
            "scope_task_deletion_anchors": {},
            "scope_prior_work_paths": ["assets/shared.png"],
            "scope_diff": "diff --git a/assets/shared.png b/assets/shared.png\n",
        })
        assert (
            "assets/shared.png: +0 added (whole task + this fix, combined) -0 deleted "
            "(this fix's baseline) | domain: this fix + earlier work in this "
            "task" in rendered
        )
        # The path the fix alone produced keeps the single mark: the earlier
        # mark is a proven fact, never a default.
        assert (
            "assets/new.png: +0 added (whole task + this fix, combined) -0 deleted "
            "(this fix's baseline) | domain: this fix\n" in rendered
        )

    def test_full_round_anchor_less_path_in_both_domains_shows_both(self):
        rendered = _format_review_scope({
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": ["assets/shared.png", "assets/new.png"],
            "scope_causal_anchors": {},
            "scope_deletion_anchors": {},
            "scope_fix_delta_available": True,
            "scope_fix_delta_baseline_id": "fix-1-abcdef123456",
            "scope_fix_delta_changed_paths": [
                "assets/shared.png", "assets/new.png",
            ],
            "scope_fix_delta_causal_anchors": {},
            "scope_fix_delta_deletion_anchors": {},
            "scope_prior_work_paths": ["assets/shared.png"],
            "scope_diff": "diff --git a/assets/shared.png b/assets/shared.png\n",
        })
        assert (
            "assets/shared.png: +0 added (whole task) -0 deleted "
            "(implementation baseline) | domain: changed by fixes since the "
            "last full round + already present at the last full round"
            in rendered
        )
        assert (
            "assets/new.png: +0 added (whole task) -0 deleted "
            "(implementation baseline) | domain: changed by fixes since the "
            "last full round\n" in rendered
        )

    def test_single_domain_round_gets_no_domain_mark(self):
        # Nothing to distinguish: the head line already names the one domain.
        rendered = _format_review_scope({
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": ["assets/new.png"],
            "scope_causal_anchors": {},
            "scope_deletion_anchors": {},
            "scope_diff": "diff --git a/assets/new.png b/assets/new.png\n",
        })
        assert "domain:" not in rendered


class TestManifestKeepsDeletionBaselinesApart:
    """Old-side line numbers of two baselines are two numbering spaces.

    They name lines of two different file versions, so unioning, subtracting
    or intersecting them fabricates both a size and line numbers that point at
    nothing. Each set is rendered alone, under a label naming its baseline.
    """

    def test_incremental_never_merges_the_two_deletion_spaces(self):
        rendered = _format_review_scope({
            "scope_mode": "incremental",
            "baseline_id": "fix-1-abcdef123456",
            "scope_changed_paths": ["a.py"],
            "scope_causal_anchors": {},
            "scope_deletion_anchors": {"a.py": [[20, 25]]},
            "scope_task_available": True,
            "scope_task_changed_paths": ["a.py"],
            "scope_task_causal_anchors": {},
            "scope_task_deletion_anchors": {"a.py": [[1, 10]]},
            "scope_diff": "diff --git a/a.py b/a.py\n-x\n",
        })
        # 6 + 10 is not a deletion count of anything: the head reports this
        # round's own (fix) baseline alone.
        assert "a.py: +0 added (whole task + this fix, combined) -6 deleted (this fix's baseline)" in rendered
        assert "-16" not in rendered
        assert "deleted lines (this fix's baseline file) 20-25" in rendered
        assert "1-10, 20-25" not in rendered
        # The whole-task deletions survive, on their own named baseline.
        assert (
            "deleted across the whole task (old-side numbers of the "
            "implementation baseline, NOT of this round's baseline): "
            "10 deleted lines at 1-10" in rendered
        )

    def test_every_manifest_size_names_the_domain_it_counts(self):
        # The head line's two sizes are counted over two different domains, so
        # an unlabelled pair would read as one file's total size: `+3 -0` on a
        # path the task deleted 2 lines from would say "nothing was deleted".
        rendered = _format_review_scope({
            "scope_mode": "incremental",
            "baseline_id": "fix-1-abcdef123456",
            "scope_changed_paths": ["src/fix.py"],
            "scope_causal_anchors": {"src/fix.py": [[40, 48]]},
            "scope_deletion_anchors": {},
            "scope_task_available": True,
            "scope_task_changed_paths": ["src/earlier.py", "src/fix.py"],
            "scope_task_causal_anchors": {
                "src/earlier.py": [[3, 5]],
                "src/fix.py": [[40, 48]],
            },
            "scope_task_deletion_anchors": {"src/earlier.py": [[30, 31]]},
            "scope_diff": "diff --git a/src/fix.py b/src/fix.py\n+x\n",
        })
        assert (
            "src/earlier.py: +3 added (whole task + this fix, combined) -0 deleted "
            "(this fix's baseline)" in rendered
        )
        # The whole-task deletion size exists too, on its own baseline's line —
        # never folded into the head line's ``-N``.
        assert "2 deleted lines at 30-31" in rendered
        assert "-2 deleted (this fix's baseline)" not in rendered

    def test_full_round_does_not_attribute_by_numeric_coincidence(self):
        rendered = _format_review_scope({
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": ["a.py"],
            "scope_causal_anchors": {},
            "scope_deletion_anchors": {"a.py": [[1, 10]]},
            "scope_fix_delta_available": True,
            "scope_fix_delta_baseline_id": "fix-1-abcdef123456",
            "scope_fix_delta_changed_paths": ["a.py"],
            "scope_fix_delta_causal_anchors": {},
            "scope_fix_delta_deletion_anchors": {"a.py": [[3, 4]]},
            "scope_diff": "diff --git a/a.py b/a.py\n-x\n",
        })
        # Implementation-baseline lines 1-10 were deleted by IMPLEMENT; the fix
        # baseline's 3-4 numbers a different file version, so it may neither
        # claim a slice of them nor leave a "already present" remainder.
        assert "a.py: +0 added (whole task) -10 deleted (implementation baseline) | deleted lines (baseline file) 1-10" in rendered
        assert "changed by fixes since the last full round: deleted" not in rendered
        assert "already present at the last full round: deleted" not in rendered
        assert (
            "deleted by fixes since the last full round (old-side numbers of "
            "that fix baseline, NOT of this round's baseline): "
            "2 deleted lines at 3-4" in rendered
        )

    def test_single_baseline_round_gets_no_cross_baseline_caveat(self):
        rendered = _format_review_scope({
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": ["a.py"],
            "scope_causal_anchors": {"a.py": [[1, 3]]},
            "scope_deletion_anchors": {"a.py": [[7, 9]]},
            "scope_diff": "diff --git a/a.py b/a.py\n+x\n",
        })
        assert "a.py: +3 added (whole task) -3 deleted (implementation baseline) | added lines (current file) 1-3" in rendered
        assert "deleted lines (baseline file) 7-9" in rendered
        assert "never compare or add them up" not in rendered


class TestManifestAddedCountNamesItsOwnDomain:
    """``+N`` states the domain it is actually counted over.

    On an incremental round carrying both domains that is their UNION, not the
    whole-task diff alone: a fix that restores an implementation-changed line
    to its baseline value drops that line out of the whole-task diff while the
    fix delta still anchors it, so the union genuinely exceeds the whole task.
    """

    def _restored_line_round(self):
        return {
            "scope_mode": "incremental",
            "baseline_id": "fix-1-abcdef123456",
            # The fix delta anchors the restored line; the whole-task diff no
            # longer sees it, but still sees the other implementation change.
            "scope_changed_paths": ["app.py"],
            "scope_causal_anchors": {"app.py": [[10, 10]]},
            "scope_deletion_anchors": {},
            "scope_task_available": True,
            "scope_task_changed_paths": ["app.py"],
            "scope_task_causal_anchors": {"app.py": [[20, 20]]},
            "scope_task_deletion_anchors": {},
            "scope_diff": "diff --git a/app.py b/app.py\n+x\n",
        }

    def test_union_count_is_not_labelled_whole_task(self):
        rendered = _format_review_scope(self._restored_line_round())

        assert "app.py: +2 added (whole task + this fix, combined)" in rendered
        assert "app.py: +2 added (whole task)" not in rendered
        # Both constituent ranges stay citable and separately attributed.
        assert "- this fix: added 10" in rendered
        assert "- earlier work in this task: added 20" in rendered

    def test_single_domain_incremental_still_says_this_fix(self):
        inputs = self._restored_line_round()
        inputs["scope_task_available"] = False
        rendered = _format_review_scope(inputs)

        assert "app.py: +1 added (this fix)" in rendered

    def test_full_round_still_says_whole_task(self):
        rendered = _format_review_scope({
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": ["app.py"],
            "scope_causal_anchors": {"app.py": [[1, 3]]},
            "scope_diff": "diff --git a/app.py b/app.py\n+x\n",
        })

        assert "app.py: +3 added (whole task)" in rendered


class TestManifestMatchesGroundingDomain:
    """What the manifest advertises as citable must survive evidence validation.

    A full round grounds on its own baseline alone: the pinned fix-delta
    reconstruction is an annotation of that domain, not a second domain. Only
    an incremental round, whose two domains are OR-ed in grounding, may present
    their union.
    """

    def _cite(self, inputs: dict, location: str) -> tuple:
        path, line = location.rsplit(":", 1)
        issue = _valid_issue(
            quote="Preserve consumer behavior", path=path, line=int(line),
        )
        return _validate_and_filter_issues([issue], dict(inputs, **{
            "task_description": "Preserve consumer behavior",
            "task_description_base": "Preserve consumer behavior",
        }))

    def _reverting_fix(self):
        """IMPLEMENT touched app.py; the fix reverted foo.py:42 and deleted
        gone.py, so neither survives in the implementation-baseline diff."""
        return {
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": ["app.py"],
            "scope_causal_anchors": {"app.py": [[1, 3]]},
            "scope_deletion_anchors": {},
            "scope_fix_delta_available": True,
            "scope_fix_delta_baseline_id": "fix-1-abcdef123456",
            "scope_fix_delta_changed_paths": ["foo.py", "gone.py"],
            "scope_fix_delta_causal_anchors": {"foo.py": [[42, 42]]},
            "scope_fix_delta_deletion_anchors": {"gone.py": [[1, 8]]},
            "scope_diff": "diff --git a/app.py b/app.py\n+x\n",
        }

    def test_full_round_manifest_omits_net_reverted_fix_paths(self):
        rendered = _format_review_scope(self._reverting_fix())
        assert "app.py: +3 added (whole task) -0 deleted (implementation baseline)" in rendered
        # Reverted / deleted by the fix: absent from this round's baseline diff,
        # so the manifest must not offer them as citable anchors.
        assert "foo.py" not in rendered
        assert "gone.py" not in rendered

    def test_full_round_clips_delta_ranges_to_its_own_domain(self):
        inputs = self._reverting_fix()
        # The fix also re-touched a line app.py's own diff does not carry.
        inputs["scope_fix_delta_changed_paths"].append("app.py")
        inputs["scope_fix_delta_causal_anchors"]["app.py"] = [[2, 2], [90, 95]]
        rendered = _format_review_scope(inputs)
        assert "app.py: +3 added (whole task) -0 deleted (implementation baseline)" in rendered
        assert "90-95" not in rendered
        assert "- changed by fixes since the last full round: added 2" in rendered
        assert "- already present at the last full round: added 1, 3" in rendered

    def test_every_manifest_anchor_grounds_on_a_full_round(self):
        inputs = self._reverting_fix()
        assert self._cite(inputs, "app.py:2")[1]["kept_count"] == 1
        # Anchors the manifest no longer advertises are still (correctly)
        # rejected — the manifest and the validator now agree.
        assert self._cite(inputs, "foo.py:42")[1]["bad_evidence_count"] == 1

    def test_incremental_round_still_unions_both_grounding_domains(self):
        inputs = {
            "scope_mode": "incremental",
            "baseline_id": "fix-1-abcdef123456",
            "scope_changed_paths": ["src/fix.py"],
            "scope_causal_anchors": {"src/fix.py": [[40, 48]]},
            "scope_task_available": True,
            "scope_task_changed_paths": ["src/earlier.py"],
            "scope_task_causal_anchors": {"src/earlier.py": [[3, 5]]},
            "scope_task_deletion_anchors": {},
            "scope_diff": "diff --git a/src/fix.py b/src/fix.py\n+x\n",
        }
        rendered = _format_review_scope(inputs)
        assert "src/earlier.py: +3 added (whole task + this fix, combined) -0 deleted (this fix's baseline)" in rendered
        assert "src/fix.py: +9 added (whole task + this fix, combined) -0 deleted (this fix's baseline)" in rendered
        # Both domains ground, so both may be presented.
        assert self._cite(inputs, "src/earlier.py:4")[1]["kept_count"] == 1
        assert self._cite(inputs, "src/fix.py:41")[1]["kept_count"] == 1

    def test_incremental_manifest_drops_a_task_domain_validation_refuses(self):
        inputs = {
            "scope_mode": "incremental",
            "baseline_id": "fix-1-abcdef123456",
            "scope_changed_paths": ["src/fix.py"],
            "scope_causal_anchors": {"src/fix.py": [[40, 48]]},
            "scope_task_available": True,
            "scope_task_changed_paths": ["src/earlier.py"],
            # Malformed: _task_scope_domain refuses a non-dict anchor map, so
            # the whole-task view is not a grounding domain this round.
            "scope_task_causal_anchors": "corrupt",
            "scope_diff": "diff --git a/src/fix.py b/src/fix.py\n+x\n",
        }
        rendered = _format_review_scope(inputs)
        assert "src/earlier.py" not in rendered
        assert "(every range below is this round's fix delta)" in rendered


class TestManifestNamesUnrenderablePathsAsTheDiffDoes:
    """A path the diff can only spell quoted stays citable end to end.

    The diff headers render such a name as a C-quoted token so the header
    survives as one line; the manifest rows and the changed-path list are
    single-line records with the same constraint, and the quoted token is then
    the only spelling a checker can read back off the prompt. So the manifest
    must show that same token, and evidence validation must accept it.
    """

    _NAME = "line\nbreak.py"
    _TOKEN = '"line\\nbreak.py"'

    def _inputs(self):
        return {
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": [self._NAME],
            "scope_causal_anchors": {self._NAME: [[1, 1]]},
            "scope_diff": (
                f"diff --git a/{self._TOKEN} b/{self._TOKEN}\n"
                "@@ -0,0 +1 @@\n+x\n"
            ),
        }

    def test_manifest_row_is_one_line_and_shows_the_quoted_token(self):
        from tianluo.engine.steps.self_check import _format_scope_manifest

        lines = _format_scope_manifest(self._inputs())
        # Every rendered element must be a single physical line: a torn row
        # would put the path on one line and its anchor ranges on another.
        for line in lines:
            assert len(line.splitlines()) <= 1, line
        row = next(line for line in lines if line.startswith(f"  - {self._TOKEN}:"))
        assert "+1 added (whole task)" in row
        assert "added lines (current file) 1" in row

    def test_changed_paths_line_shows_the_same_spelling(self):
        rendered = _format_review_scope(self._inputs())
        assert f"- changed_paths: {self._TOKEN}" in rendered
        # The raw name would tear the list line in two.
        assert "- changed_paths: line\nbreak.py" not in rendered

    def test_citation_in_the_spelling_the_prompt_shows_grounds(self):
        """The quoted token is what the prompt presents, so it must ground.

        The anchor keys are raw pathnames; without reading the token back, a
        citation copied straight off the prompt would be counted as bad
        evidence and a real finding silently dropped.
        """
        inputs = dict(
            self._inputs(),
            task_description="Preserve consumer behavior",
            task_description_base="Preserve consumer behavior",
        )
        for citation in (f"{self._TOKEN}:1", f"{self._NAME}:1"):
            issue = _valid_issue(quote="Preserve consumer behavior")
            issue["evidence_lines"] = [citation]
            issue["missing_in"] = []
            kept, stats = _validate_and_filter_issues([issue], inputs)
            assert len(kept) == 1, citation
            assert stats["bad_evidence_count"] == 0, citation


class TestManifestNamesEdgeWhitespacePathsRecoverably:
    """A name whose edge whitespace the citation read-back eats is quoted.

    ``_evidence_path_candidates`` strips a citation before parsing it, so a
    raw `` leading.py`` shown by the manifest would be a spelling that can
    never ground — the silent bad-evidence drop the manifest exists to
    prevent. It is presented as a quoted token instead, which survives the
    strip and decodes back to the exact name.
    """

    _NAME = " leading.py"
    _TOKEN = '" leading.py"'

    def _inputs(self):
        return {
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": [self._NAME],
            "scope_causal_anchors": {self._NAME: [[1, 1]]},
            "scope_diff": (
                f"diff --git a/{self._TOKEN} b/{self._TOKEN}\n"
                "@@ -0,0 +1 @@\n+x\n"
            ),
        }

    def test_manifest_shows_the_quoted_token(self):
        from tianluo.engine.steps.self_check import _format_scope_manifest

        lines = _format_scope_manifest(self._inputs())
        row = next(line for line in lines if line.startswith(f"  - {self._TOKEN}:"))
        assert "+1 added (whole task)" in row
        # The raw spelling must not be what the prompt presents.
        assert f"  - {self._NAME}:" not in row

    def test_citation_in_the_spelling_the_prompt_shows_grounds(self):
        inputs = dict(
            self._inputs(),
            task_description="Preserve consumer behavior",
            task_description_base="Preserve consumer behavior",
        )
        issue = _valid_issue(quote="Preserve consumer behavior")
        issue["evidence_lines"] = [f"{self._TOKEN}:1"]
        issue["missing_in"] = []
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert len(kept) == 1
        assert stats["bad_evidence_count"] == 0

    def test_malformed_quoted_citation_does_not_alias_a_real_path(self):
        """A token nothing presented must not decode onto another real path.

        A repairing decoder collapses the unknown escape in ``"src\\q.py"``
        to ``srcq.py``; grounding on that would admit a finding whose cited
        spelling this round never showed.
        """
        inputs = {
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": ["srcq.py"],
            "scope_causal_anchors": {"srcq.py": [[1, 1]]},
            "scope_diff": "diff --git a/srcq.py b/srcq.py\n@@ -0,0 +1 @@\n+x\n",
            "task_description": "Preserve consumer behavior",
            "task_description_base": "Preserve consumer behavior",
        }
        issue = _valid_issue(quote="Preserve consumer behavior")
        issue["evidence_lines"] = ['"src\\q.py":1']
        issue["missing_in"] = []
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

        # The real path, cited as itself, still grounds.
        good = _valid_issue(quote="Preserve consumer behavior")
        good["evidence_lines"] = ["srcq.py:1"]
        good["missing_in"] = []
        kept, stats = _validate_and_filter_issues([good], inputs)
        assert len(kept) == 1


class TestRepeatedSelfCheckFindings:
    """Repeated validated findings always remain in the fix loop."""

    @pytest.fixture
    def flow(self, tmp_path):
        flow = FlowInstance(
            flow_id="test-flow-conv",
            task_description="Fix bug",
            task_type="bugfix",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "test-change",
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
        ]
        return flow

    def _make_inputs(self, fix_iteration=2, **extra):
        """Build a step.inputs dict whose source pool ("Fix bug") and
        changes_made paths support the new validation schema."""
        inp = {
            "task_description": "Fix bug in production handler",
            "changes_made": {
                "files_changed": [
                    {"path": "a.py", "action": "modify"},
                    {"path": "b.py", "action": "modify"},
                    {"path": "new.py", "action": "create"},
                ],
            },
            "test_results": {"passed": True, "returncode": 0},
            "spec_content": {},
            "fix_iteration": fix_iteration,
            "max_fix_iterations": 10,
        }
        inp.update(extra)
        return inp

    def test_repeated_issues_still_enter_fix(self, flow):
        # Repetition is never a finding-discard channel.
        repeated = [
            _valid_issue(severity="low", quote="Fix bug",
                         path="a.py", line=1,
                         actual="returns None on missing key",
                         expected="raises KeyError",
                         divergence="silent failure"),
            _valid_issue(severity="medium", quote="Fix bug",
                         path="b.py", line=2,
                         actual="leaks file handle on error",
                         expected="closes via finally",
                         divergence="ResourceWarning under load"),
        ]
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._make_inputs(
                prev_self_check_issues=repeated,
                self_check_convergence_enabled=True,
            ),
        )
        response = json.dumps({"issues": repeated, "summary": "same"})

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs.get("converged") is not True
        assert step.outputs["issues"] == repeated
        assert step.outputs["fix_needed"] is True

    def test_does_not_converge_on_first_iteration(self, flow):
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._make_inputs(fix_iteration=0),
        )
        response = json.dumps({
            "issues": [
                _valid_issue(severity="low", quote="Fix bug",
                             path="a.py", line=1,
                             actual="initial finding",
                             expected="resolved",
                             divergence="initial path"),
            ],
            "summary": "first",
        })

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert not step.outputs.get("converged")

    def test_still_present_verdict_cannot_end_a_round_clean(self, flow):
        # The reviewer says the previous issue survives but fails to re-ground
        # the re-report in the (narrower) current scope. Returning COMPLETED
        # would be a pass-with-finding contradicting the round's own
        # resolutions record.
        prev_issues = [
            _valid_issue(severity="medium", quote="Fix bug",
                         path="untouched.py", line=99,
                         actual="leaks fd", expected="closed",
                         divergence="OSError under load"),
        ]
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._make_inputs(prev_self_check_issues=prev_issues),
        )
        response = json.dumps({
            "issues": [],
            "previous_issue_resolutions": [
                {"prev_issue_summary": "leaks fd", "status": "still_present"},
            ],
            "summary": "nothing new",
        })

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["issues"] == prev_issues
        assert step.outputs["fix_needed"] is True

    def test_still_present_verdict_survives_cardinality_drift(self, flow):
        # Fewer resolutions than previous issues means positional pairing is
        # untrustworthy — but a 'still_present' verdict must STILL reach the
        # fix loop. Here the summary unambiguously names issue A, so A (only)
        # is re-admitted.
        issue_a = _valid_issue(severity="medium", quote="Fix bug",
                               path="alpha.py", line=99,
                               actual="leaks a file descriptor",
                               expected="descriptor closed",
                               divergence="OSError under sustained load")
        issue_b = _valid_issue(severity="low", quote="Fix bug",
                               path="beta.py", line=7,
                               actual="renders timestamps in local time",
                               expected="renders timestamps in UTC",
                               divergence="log lines shift by the offset")
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._make_inputs(prev_self_check_issues=[issue_a, issue_b]),
        )
        response = json.dumps({
            "issues": [],
            "previous_issue_resolutions": [
                {
                    "prev_issue_summary": (
                        "alpha.py still leaks a file descriptor, OSError "
                        "under sustained load"
                    ),
                    "status": "still_present",
                },
            ],
            "summary": "nothing new",
        })

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["issues"] == [issue_a]
        assert step.outputs["fix_needed"] is True

    def test_unmatchable_still_present_verdict_fails_closed(self, flow):
        # A zero-signal summary cannot identify WHICH previous issue survives,
        # so the round fails closed: every unaccounted previous issue returns
        # to the fix loop rather than the round closing clean.
        issue_a = _valid_issue(severity="medium", quote="Fix bug",
                               path="alpha.py", line=99,
                               actual="leaks a file descriptor",
                               expected="descriptor closed",
                               divergence="OSError under sustained load")
        issue_b = _valid_issue(severity="low", quote="Fix bug",
                               path="beta.py", line=7,
                               actual="renders timestamps in local time",
                               expected="renders timestamps in UTC",
                               divergence="log lines shift by the offset")
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._make_inputs(prev_self_check_issues=[issue_a, issue_b]),
        )
        response = json.dumps({
            "issues": [],
            "previous_issue_resolutions": [
                {"prev_issue_summary": "!!!", "status": "still_present"},
            ],
            "summary": "nothing new",
        })

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["issues"] == [issue_a, issue_b]
        assert step.outputs["fix_needed"] is True

    def test_fixed_verdict_with_no_issues_still_passes(self, flow):
        prev_issues = [
            _valid_issue(severity="medium", quote="Fix bug",
                         path="a.py", line=1,
                         actual="leaks fd", expected="closed",
                         divergence="OSError under load"),
        ]
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._make_inputs(prev_self_check_issues=prev_issues),
        )
        response = json.dumps({
            "issues": [],
            "previous_issue_resolutions": [
                {"prev_issue_summary": "leaks fd", "status": "fixed"},
            ],
            "summary": "clean",
        })

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["issues"] == []

    def test_does_not_converge_when_new_issue_appears(self, flow):
        prev_issues = [
            _valid_issue(severity="low", quote="Fix bug",
                         path="a.py", line=1,
                         actual="leaks fd", expected="closed",
                         divergence="OSError"),
        ]
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._make_inputs(prev_self_check_issues=prev_issues),
        )
        response = json.dumps({
            "issues": [
                _valid_issue(severity="low", quote="Fix bug",
                             path="a.py", line=1,
                             actual="leaks fd", expected="closed",
                             divergence="OSError"),
                _valid_issue(severity="medium", quote="Fix bug",
                             path="new.py", line=1,
                             actual="new issue site",
                             expected="resolved",
                             divergence="new failure mode"),
            ],
            "summary": "new issue found",
        })

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert not step.outputs.get("converged")


# ---------------------------------------------------------------------------
# Schema validation helpers
# ---------------------------------------------------------------------------

class TestNormalizeForQuoteMatch:
    """Symmetric normalization for verbatim_quote ↔ source pool comparison."""

    def test_basic_passthrough(self):
        from tianluo.engine.steps.self_check import _normalize_for_quote_match
        assert _normalize_for_quote_match("hello world") == "hello world"

    def test_literal_backslash_n_becomes_real_newline_then_space(self):
        from tianluo.engine.steps.self_check import _normalize_for_quote_match
        assert _normalize_for_quote_match("a\\nb") == "a b"

    def test_smart_quotes_replaced_with_ascii(self):
        from tianluo.engine.steps.self_check import _normalize_for_quote_match
        assert _normalize_for_quote_match("“hello” ‘world’") == '"hello" \'world\''

    def test_whitespace_collapsed(self):
        from tianluo.engine.steps.self_check import _normalize_for_quote_match
        assert _normalize_for_quote_match("a  \n\t  b\n\nc") == "a b c"

    def test_nfkc_normalizes_compatibility_codepoints(self):
        from tianluo.engine.steps.self_check import _normalize_for_quote_match
        # Fullwidth digit "１" becomes "1" under NFKC.
        assert _normalize_for_quote_match("１") == "1"

    def test_non_string_input_returns_empty(self):
        from tianluo.engine.steps.self_check import _normalize_for_quote_match
        assert _normalize_for_quote_match(None) == ""
        assert _normalize_for_quote_match(123) == ""
        assert _normalize_for_quote_match(["list"]) == ""


class TestBuildSourcePool:
    """Source pool collection for verbatim_quote validation."""

    def test_includes_task_description(self):
        from tianluo.engine.steps.self_check import _build_source_pool
        pool = _build_source_pool({"task_description": "user wants X"})
        assert "user wants X" in pool

    def test_excludes_superseded_original_task_description(self):
        from tianluo.engine.steps.self_check import _build_source_pool
        pool = _build_source_pool({
            "task_description": "refined: implement X",
            "original_task_description": "raw user input",
        })
        assert "refined: implement X" in pool
        assert "raw user input" not in pool

    def test_excludes_legacy_spec_content(self):
        """Legacy spec payloads are history, not requirement authority."""
        from tianluo.engine.steps.self_check import _build_source_pool
        pool = _build_source_pool({
            "task_description": "task X",
            "spec_content": {
                "base": "Code must be PEP 8 compliant",
                "feature_x": "feature X spec details",
            },
        })
        assert "task X" in pool
        assert "feature X spec details" not in pool
        assert "Code must be PEP 8 compliant" not in pool

    def test_empty_inputs_returns_empty_pool(self):
        from tianluo.engine.steps.self_check import _build_source_pool
        assert _build_source_pool({}) == []

    def test_prefers_clean_base_over_composed(self):
        """When ``task_description_base`` is provided (the un-decorated
        version state_machine populates for SELF_CHECK steps), it takes
        priority over the composed ``task_description``. This excludes
        our ``## Additional Instructions`` boilerplate header from the
        pool, blocking the attack where an LLM uses the header text
        itself as a verbatim_quote."""
        from tianluo.engine.steps.self_check import _build_source_pool
        pool = _build_source_pool({
            "task_description_base": "task X",
            "task_description": (
                "task X\n\n## Additional Instructions (added during run)\n"
                "\n- [analyze@t1] use Postgres"
            ),
        })
        assert "task X" in pool
        # Composed value with section is NOT added.
        assert not any(
            "## Additional Instructions (added during run)" in p
            for p in pool
        )

    def test_includes_each_interjection_text_separately(self):
        """Each user_interjection's ``text`` is added as its own pool
        entry, so a quote citing only the interjection content (without
        the bullet/timestamp prefix or section header) substring-
        matches."""
        from tianluo.engine.steps.self_check import _build_source_pool
        pool = _build_source_pool({
            "task_description_base": "task X",
            "user_interjections": [
                {"text": "use Postgres not SQLite",
                 "step_type": "analyze", "timestamp": "t1"},
                {"text": "skip the cache layer",
                 "step_type": "implement", "timestamp": "t2"},
            ],
        })
        assert "use Postgres not SQLite" in pool
        assert "skip the cache layer" in pool

    def test_falls_back_to_task_description_when_base_missing(self):
        """Older inputs without ``task_description_base`` still work via
        the composed ``task_description``. (Forward-compat for unit
        tests / tests of pre-upgrade state.)"""
        from tianluo.engine.steps.self_check import _build_source_pool
        pool = _build_source_pool({"task_description": "legacy task"})
        assert "legacy task" in pool

    def test_user_interjections_non_dict_entries_skipped(self):
        """Defensive: malformed interjection entries don't crash the
        pool builder."""
        from tianluo.engine.steps.self_check import _build_source_pool
        pool = _build_source_pool({
            "task_description_base": "task",
            "user_interjections": [
                None, "not a dict", 42,
                {"text": "real one", "step_type": "x", "timestamp": "y"},
                {"text": "", "step_type": "x", "timestamp": "y"},  # empty text
                {"step_type": "x"},  # missing text
            ],
        })
        assert "real one" in pool
        # Empty / missing text not added — would have produced `""` or KeyError.
        assert "" not in pool
        assert _build_source_pool({"task_description": ""}) == []


class TestValidateAndFilterIssues:
    """The structural validation pipeline that drops ungrounded issues."""

    def _inputs(self, **overrides):
        base = {
            "task_description": "Implement reliable async retry policy",
            "changes_made": {
                "files_changed": [
                    {"path": "src/retry.py", "action": "create"},
                    {"path": "src/policy.py", "action": "modify"},
                ],
            },
            "spec_content": {"base": "PEP 8 etc"},
        }
        base.update(overrides)
        return base

    def _good_issue(self, **overrides):
        issue = {
            "severity": "high",
            "actual_behavior": "raises on backoff = 0",
            "expected_behavior": "treats 0 as immediate retry",
            "divergence": "valid config crashes init",
            "expectation_source": {
                "type": "task_description",
                "verbatim_quote": "reliable async retry policy",
            },
            "evidence_lines": ["src/retry.py:42"],
            "missing_in": [],
        }
        issue.update(overrides)
        return issue

    def test_kept_when_all_checks_pass(self):
        from tianluo.engine.steps.self_check import _validate_and_filter_issues
        kept, stats = _validate_and_filter_issues(
            [self._good_issue()], self._inputs()
        )
        assert len(kept) == 1
        assert stats["kept_count"] == 1
        assert stats["input_count"] == 1

    def test_out_of_scope_no_longer_exempts(self):
        """There is no exemption channel: a self-marked out_of_scope item
        is validated on evidence like any other and kept when it passes."""
        from tianluo.engine.steps.self_check import _validate_and_filter_issues
        kept, stats = _validate_and_filter_issues(
            [self._good_issue(out_of_scope=True)], self._inputs()
        )
        assert len(kept) == 1
        assert stats["kept_count"] == 1
        assert "out_of_scope_count" not in stats

    def test_out_of_scope_item_still_subject_to_evidence_checks(self):
        """Removing the release valve does not weaken evidence grounding:
        an out_of_scope item with an ungrounded quote is still dropped —
        for failing validation, not for being self-marked non-actionable."""
        from tianluo.engine.steps.self_check import _validate_and_filter_issues

        issue = self._good_issue(out_of_scope=True)
        issue["expectation_source"]["verbatim_quote"] = "this phrase is not in the task"
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == []
        assert stats["quote_not_in_source_count"] == 1
        assert "out_of_scope_count" not in stats

    def test_empty_quote_dropped(self):
        from tianluo.engine.steps.self_check import _validate_and_filter_issues
        bad = self._good_issue()
        bad["expectation_source"]["verbatim_quote"] = "  "
        kept, stats = _validate_and_filter_issues([bad], self._inputs())
        assert kept == []
        assert stats["empty_quote_count"] == 1

    def test_quote_not_in_source_dropped(self):
        from tianluo.engine.steps.self_check import _validate_and_filter_issues
        bad = self._good_issue()
        bad["expectation_source"]["verbatim_quote"] = "this phrase is not in the task"
        kept, stats = _validate_and_filter_issues([bad], self._inputs())
        assert kept == []
        assert stats["quote_not_in_source_count"] == 1

    def test_evidence_path_not_in_changes_dropped(self):
        from tianluo.engine.steps.self_check import _validate_and_filter_issues
        bad = self._good_issue(evidence_lines=["unrelated/file.py:1"], missing_in=[])
        kept, stats = _validate_and_filter_issues([bad], self._inputs())
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_missing_in_substitutes_for_evidence_lines(self):
        from tianluo.engine.steps.self_check import _validate_and_filter_issues
        good = self._good_issue(
            evidence_lines=[],
            missing_in=["src/auth.py"],
        )
        kept, stats = _validate_and_filter_issues([good], self._inputs())
        assert len(kept) == 1
        assert stats["bad_evidence_count"] == 0

    def test_empty_required_field_dropped(self):
        from tianluo.engine.steps.self_check import _validate_and_filter_issues
        bad = self._good_issue(actual_behavior="")
        kept, stats = _validate_and_filter_issues([bad], self._inputs())
        assert kept == []
        assert stats["empty_field_count"] == 1

    def test_non_dict_entry_skipped(self):
        from tianluo.engine.steps.self_check import _validate_and_filter_issues
        kept, stats = _validate_and_filter_issues(
            [None, "not a dict", 42, self._good_issue()],
            self._inputs(),
        )
        assert len(kept) == 1
        assert stats["non_dict_count"] == 3

    def test_smart_quote_drift_in_quote_still_matches(self):
        """LLM paraphrasing with curly quotes still substring-matches
        against an ASCII-quoted source via the symmetric normalize."""
        from tianluo.engine.steps.self_check import _validate_and_filter_issues
        inputs = self._inputs(task_description='Implement "reliable" retry policy')
        good = self._good_issue()
        good["expectation_source"]["verbatim_quote"] = "“reliable” retry policy"
        kept, stats = _validate_and_filter_issues([good], inputs)
        assert len(kept) == 1

    def test_section_header_boilerplate_quote_rejected(self):
        """Regression: an LLM cannot use the ``## Additional Instructions``
        section header (which we inject when interjections exist) as a
        verbatim_quote to slip an ungrounded issue past validation. The
        source pool uses ``task_description_base`` (the un-decorated
        base), not the composed ``task_description``, so the header
        text is not in the pool."""
        from tianluo.engine.steps.self_check import _validate_and_filter_issues
        inputs = self._inputs()
        inputs["task_description_base"] = inputs["task_description"]
        # Composed task_description (what the LLM sees in its prompt)
        # contains the boilerplate. The base in the pool does not.
        inputs["task_description"] = (
            inputs["task_description"]
            + "\n\n## Additional Instructions (added during run)\n"
            + "\n- [analyze@t1] use Postgres"
        )
        bad = self._good_issue()
        bad["expectation_source"]["verbatim_quote"] = (
            "## Additional Instructions (added during run)"
        )
        kept, stats = _validate_and_filter_issues([bad], inputs)
        assert kept == []
        assert stats["quote_not_in_source_count"] == 1

    def test_legitimate_interjection_quote_passes(self):
        """The companion contract: an LLM legitimately citing an
        interjection's content (without the boilerplate header) MUST
        still pass validation."""
        from tianluo.engine.steps.self_check import _validate_and_filter_issues
        inputs = self._inputs()
        inputs["task_description_base"] = inputs["task_description"]
        inputs["user_interjections"] = [
            {"text": "use Postgres not SQLite",
             "step_type": "analyze", "timestamp": "t1"},
        ]
        good = self._good_issue()
        good["expectation_source"] = {
            "type": "user_interjection",
            "verbatim_quote": "use Postgres not SQLite",
        }
        kept, stats = _validate_and_filter_issues([good], inputs)
        assert len(kept) == 1


class TestPreviousIssueResolutions:
    """The ``previous_issue_resolutions`` array is captured into outputs."""

    @pytest.fixture
    def flow(self, tmp_path):
        return FlowInstance(
            flow_id="prev-issue-resolutions-flow",
            task_description="Test prev issue resolutions tracking",
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "c",
        )

    def _make_step(self):
        return Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Test prev issue resolutions tracking",
                "changes_made": {"files_changed": [{"path": "a.py", "action": "modify"}]},
                "test_results": {"passed": True, "returncode": 0},
                "spec_content": {},
                "fix_iteration": 1,
                "max_fix_iterations": 10,
            },
        )

    def test_resolutions_written_to_outputs(self, flow):
        step = self._make_step()
        response = json.dumps({
            "issues": [],
            "previous_issue_resolutions": [
                {"prev_issue_summary": "missing null check", "status": "fixed"},
                {"prev_issue_summary": "leak on timeout", "status": "still_present"},
            ],
            "summary": "one fixed, one not",
        })
        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller
            self_check_handler(step, flow)

        resolutions = step.outputs["previous_issue_resolutions"]
        assert len(resolutions) == 2
        assert resolutions[0]["status"] == "fixed"
        assert resolutions[1]["status"] == "still_present"

    def test_missing_resolutions_field_defaults_to_empty_list(self, flow):
        step = self._make_step()
        response = json.dumps({"issues": [], "summary": "ok"})
        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller
            self_check_handler(step, flow)
        assert step.outputs["previous_issue_resolutions"] == []

    def test_validation_stats_in_outputs(self, flow):
        """``validation_stats`` per-rejection-reason counts surface in
        outputs for post-hoc inspection of LLM behavior."""
        step = self._make_step()
        valid = {
            "severity": "high",
            "actual_behavior": "x", "expected_behavior": "y",
            "divergence": "z",
            "expectation_source": {
                "type": "task_description",
                "verbatim_quote": "Test prev issue resolutions tracking",
            },
            "evidence_lines": ["a.py:1"],
            "missing_in": [],
        }
        empty_quote = dict(valid)
        empty_quote["expectation_source"] = {
            "type": "task_description", "verbatim_quote": "",
        }
        # Self-marked out_of_scope carries no exemption: this one is kept
        # because its evidence stands up, exactly like ``valid``.
        out_of_scope = dict(valid)
        out_of_scope["out_of_scope"] = True

        response = json.dumps({
            "issues": [valid, empty_quote, out_of_scope],
            "summary": "mixed",
        })
        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller
            self_check_handler(step, flow)

        stats = step.outputs["validation_stats"]
        assert stats["input_count"] == 3
        assert stats["kept_count"] == 2
        assert stats["empty_quote_count"] == 1
        assert "out_of_scope_count" not in stats
        assert len(step.outputs["raw_issues"]) == 3
        assert len(step.outputs["issues"]) == 2


class TestNoOutOfScopeExemption:
    """Regression: a check-class step's findings have exactly one destination —
    the fix loop, now. There is no exemption channel.

    Origin (2026-07-28): a self_check pass whose findings were ALL self-marked
    ``out_of_scope`` on the FIRST pass — no deferred stash, no carried issues
    from an earlier pass to mask the discard — returned COMPLETED while the raw
    LLM report rendered red. The WebUI showed a red "✗ 2 issues" next to a green
    "✓ passed" at the same time. Before that, every discard happened to be
    accompanied by other actionable issues, so it never released a flow visibly.
    Keep this test: it is the only coverage of the unmasked case.
    """

    @pytest.fixture
    def flow(self, tmp_path):
        return FlowInstance(
            flow_id="no-oos-exemption-flow",
            task_description="Implement feature X",
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "c",
        )

    def _step(self):
        return Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Implement feature X",
                "changes_made": {
                    "files_changed": [
                        {"path": "src/feature.py", "action": "modify"},
                    ]
                },
                "test_results": {"passed": True, "returncode": 0},
                "spec_content": {},
                # First round, first pass: nothing carried, nothing deferred.
                "fix_iteration": 0,
                "max_fix_iterations": 10,
            },
        )

    def test_all_findings_out_of_scope_first_pass_still_routes_to_fix(self, flow):
        step = self._step()
        oos_a = _valid_issue(severity="low", line=42)
        oos_a["out_of_scope"] = True
        oos_b = _valid_issue(severity="medium", line=77,
                             actual="skips the trailing entry",
                             expected="processes every entry",
                             divergence="the last record is silently dropped")
        oos_b["out_of_scope"] = True
        response = json.dumps({
            "issues": [oos_a, oos_b],
            "summary": "observations only",
        })

        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller
            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert len(step.outputs["issues"]) == 2
        assert step.outputs["actionable_count"] == len(step.outputs["issues"])
        assert step.outputs["fix_needed"] is True
        assert "out_of_scope_count" not in step.outputs["validation_stats"]

    def test_prompt_and_schema_hint_no_longer_offer_the_field(self, flow):
        """The LLM contract must not keep advertising a field that no longer
        has any effect — a prompt promising a discard the handler does not
        perform is worse than no field at all."""
        step = self._step()
        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = json.dumps({"issues": [], "summary": "ok"})
            mock_cls.return_value = mock_caller
            self_check_handler(step, flow)

        prompt = mock_caller.call.call_args.kwargs["prompt"]
        hint = mock_caller.call.call_args.kwargs["json_schema_hint"]
        assert "out_of_scope" not in prompt
        assert "out_of_scope" not in hint


class TestWholeTaskEvidenceDomain:
    """An incremental round grounds evidence on the union of two diff domains.

    Its attention is the latest fix delta, but the change under review is the
    whole flow's work: a finding anchored on a line an earlier IMPLEMENT/FIX
    really wrote is grounded in git fact and must not be dropped as fabricated.
    """

    def _inputs(self):
        return {
            "task_description": "Implement feature X",
            "task_description_base": "Implement feature X",
            "changes_made": {"files_changed": ["src/fix.py"]},
            # Round domain: the latest fix delta only.
            "scope_changed_paths": ["src/fix.py"],
            "scope_causal_anchors": {"src/fix.py": [[10, 12]]},
            # Whole-task domain: everything this flow changed.
            "scope_task_available": True,
            "scope_task_changed_paths": ["src/earlier.py", "src/fix.py"],
            "scope_task_causal_anchors": {
                "src/earlier.py": [[5, 8]],
                "src/fix.py": [[3, 12]],
            },
        }

    def test_anchor_in_earlier_flow_work_grounds_the_finding(self):
        issue = _valid_issue(path="src/earlier.py", line=6)
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == [issue]
        assert stats["kept_count"] == 1
        assert stats["bad_evidence_count"] == 0

    def test_earlier_line_of_a_fix_touched_file_grounds_the_finding(self):
        # Line 4 of src/fix.py is outside the fix delta but inside the flow's
        # whole-task change to that same file.
        issue = _valid_issue(path="src/fix.py", line=4)
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0

    def test_line_outside_both_domains_is_still_rejected(self):
        # The union widens the domain; it does not abolish it. An unchanged
        # line is still ungrounded evidence.
        issue = _valid_issue(path="src/earlier.py", line=20)
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == []
        assert stats["bad_evidence_count"] == 1

        issue = _valid_issue(path="src/untouched.py", line=6)
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_without_the_whole_task_domain_the_same_finding_is_dropped(self):
        # Pins what the widening actually buys: the identical finding on the
        # fix-delta domain alone is exactly the loss this change removes.
        inputs = self._inputs()
        inputs["scope_task_available"] = False
        issue = _valid_issue(path="src/earlier.py", line=6)
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_regression_grounds_on_the_whole_task_domain(self):
        # ``require_changed_line`` narrows WHICH citations count, not which
        # baseline they may come from: a regression caused by earlier flow work
        # still points into this flow's change.
        issue = _valid_issue(path="src/earlier.py", line=7)
        issue["expectation_source"] = {
            "type": "regression",
            "verbatim_quote": "pre-existing behavior",
        }
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0

        issue["evidence_lines"] = ["src/earlier.py:20"]
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_anchor_bearing_in_either_domain_is_anchor_bearing_in_the_union(self):
        # src/bin.dat is rename-only/binary in the fix delta (anchor-LESS
        # there) yet carries current-side changed lines across the whole task.
        # The two views are UNIONED before grounding is decided, so the path is
        # anchor-BEARING: a bare-path citation no longer grounds, and the line
        # the union does hold does.
        inputs = self._inputs()
        inputs["scope_changed_paths"] = ["src/bin.dat"]
        inputs["scope_causal_anchors"] = {}
        inputs["scope_task_changed_paths"] = ["src/bin.dat"]
        inputs["scope_task_causal_anchors"] = {"src/bin.dat": [[1, 2]]}

        issue = _valid_issue()
        issue["evidence_lines"] = ["src/bin.dat"]
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

        issue = _valid_issue(path="src/bin.dat", line=2)
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0

    def test_regression_bare_path_is_refused_when_the_union_has_a_line(self):
        # The divergence the union closes: a regression may cite a bare path
        # only where the path is anchor-less. Deciding that per domain would
        # let the fix delta's rename-only view keep granting path-level
        # grounding for a file IMPLEMENT edited by line.
        inputs = self._inputs()
        inputs["scope_changed_paths"] = ["src/bin.dat"]
        inputs["scope_causal_anchors"] = {}
        inputs["scope_task_changed_paths"] = ["src/bin.dat"]
        inputs["scope_task_causal_anchors"] = {"src/bin.dat": [[1, 2]]}
        issue = _valid_issue()
        issue["evidence_lines"] = []
        issue["missing_in"] = ["src/bin.dat"]
        issue["expectation_source"] = {
            "type": "regression",
            "verbatim_quote": "pre-existing behavior",
        }
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_anchor_less_in_both_domains_keeps_path_level_grounding(self):
        # A path with no current-side line in EITHER view is anchor-less in the
        # union too, so the bare-path citation the prompt prescribes there is
        # exactly what grounds it.
        inputs = self._inputs()
        inputs["scope_changed_paths"] = ["src/bin.dat"]
        inputs["scope_causal_anchors"] = {}
        inputs["scope_task_changed_paths"] = ["src/bin.dat"]
        inputs["scope_task_causal_anchors"] = {}
        issue = _valid_issue()
        issue["evidence_lines"] = ["src/bin.dat"]
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0

    def test_regression_missing_in_channel_widens_across_domains(self):
        inputs = self._inputs()
        inputs["scope_changed_paths"] = ["src/fix.py"]
        inputs["scope_causal_anchors"] = {"src/fix.py": [[10, 12]]}
        inputs["scope_task_changed_paths"] = ["src/gone.py", "src/fix.py"]
        # src/gone.py is deletion-only across the whole task: anchor-less, so it
        # grounds a regression at path level under ``missing_in``.
        inputs["scope_task_causal_anchors"] = {"src/fix.py": [[3, 12]]}
        issue = _valid_issue()
        issue["evidence_lines"] = []
        issue["missing_in"] = ["src/gone.py"]
        issue["expectation_source"] = {
            "type": "regression",
            "verbatim_quote": "pre-existing behavior",
        }
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0

    def test_regression_missing_in_grounds_on_exact_and_quoted_spellings(self):
        # ``src/gone.py `` (trailing space) is anchor-less across the whole
        # task, and every surface that shows it to the checker — manifest and
        # diff headers alike — spells it as the C-quoted token. Both that token
        # and the raw name must ground the regression, exactly as they do under
        # ``evidence_lines``: the citation form may not decide the outcome.
        from tianluo.engine.review_scope import quote_diff_path

        anchor_less = "src/gone.py "
        inputs = self._inputs()
        inputs["scope_changed_paths"] = ["src/fix.py"]
        inputs["scope_causal_anchors"] = {"src/fix.py": [[10, 12]]}
        inputs["scope_task_changed_paths"] = [anchor_less, "src/fix.py"]
        inputs["scope_task_causal_anchors"] = {"src/fix.py": [[3, 12]]}

        quoted = quote_diff_path(anchor_less)
        assert quoted != anchor_less
        for cited in (anchor_less, quoted):
            issue = _valid_issue()
            issue["evidence_lines"] = []
            issue["missing_in"] = [cited]
            issue["expectation_source"] = {
                "type": "regression",
                "verbatim_quote": "pre-existing behavior",
            }
            kept, stats = _validate_and_filter_issues([issue], inputs)
            assert kept == [issue], cited
            assert stats["bad_evidence_count"] == 0, cited

        # The widening offers spellings of a changed path; it does not admit a
        # path no domain carries. ``src/gone.py`` (trimmed) is a different file.
        issue = _valid_issue()
        issue["evidence_lines"] = []
        issue["missing_in"] = [anchor_less.strip()]
        issue["expectation_source"] = {
            "type": "regression",
            "verbatim_quote": "pre-existing behavior",
        }
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_full_round_without_task_domain_is_unchanged(self):
        # A full round already diffs from the implementation baseline and never
        # carries the extra keys; its grounding must behave exactly as before.
        inputs = {
            "task_description": "Implement feature X",
            "task_description_base": "Implement feature X",
            "changes_made": {"files_changed": ["src/fix.py"]},
            "scope_changed_paths": ["src/fix.py"],
            "scope_causal_anchors": {"src/fix.py": [[10, 12]]},
        }
        kept, _ = _validate_and_filter_issues(
            [_valid_issue(path="src/fix.py", line=11)], inputs
        )
        assert len(kept) == 1
        kept, stats = _validate_and_filter_issues(
            [_valid_issue(path="src/fix.py", line=4)], inputs
        )
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_undecidable_scope_relaxation_is_unchanged(self):
        # A degraded round grounds on naming any path at all, so the second
        # domain adds nothing and must not disturb the relaxation tally.
        inputs = self._inputs()
        inputs["scope_undecidable"] = True
        issue = _valid_issue(path="src/nowhere.py", line=3)
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["undecidable_scope_kept_count"] == 1
        assert stats["bad_evidence_count"] == 0

    def test_malformed_task_domain_is_ignored(self):
        inputs = self._inputs()
        inputs["scope_task_causal_anchors"] = "not-a-dict"
        issue = _valid_issue(path="src/earlier.py", line=6)
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

        inputs = self._inputs()
        inputs["scope_task_changed_paths"] = {"src/earlier.py": 1}
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1


class TestWholeTaskScopeRendering:
    def _inputs(self):
        return {
            "scope_mode": "incremental",
            "baseline_id": "fix-1",
            "scope_changed_paths": ["src/fix.py"],
            "scope_diff": "@@ -1 +1 @@\n+fixed\n",
            "scope_task_available": True,
            "scope_task_changed_paths": ["src/earlier.py", "src/fix.py"],
            # The rule is only stated when the whole-task domain really grounds
            # evidence, so the fixture carries the anchors a reconstructed
            # task scope always persists alongside its paths.
            "scope_task_causal_anchors": {"src/earlier.py": [[3, 5]]},
            "scope_task_deletion_anchors": {},
        }

    def test_incremental_states_the_widened_evidence_rule(self):
        rendered = _format_review_scope(self._inputs())
        assert "task_changed_paths" in rendered
        assert "src/earlier.py" in rendered
        assert "not only inside the fix delta" in rendered
        # Attention is unchanged: the fix delta is still the primary object.
        assert "Focus first on the exact delta that baseline produces" in rendered

    def test_no_fix_iteration_or_closed_finding_metadata_leaks(self):
        inputs = self._inputs()
        inputs["fix_iteration"] = 3
        inputs["fix_history"] = [{"iteration": 1, "issues": ["closed one"]}]
        rendered = _format_review_scope(inputs)
        assert "fix_iteration" not in rendered
        assert "closed one" not in rendered

    def test_full_round_renders_no_second_domain(self):
        inputs = self._inputs()
        inputs["scope_mode"] = "full"
        inputs["scope_task_available"] = False
        inputs["scope_task_changed_paths"] = []
        rendered = _format_review_scope(inputs)
        assert "task_changed_paths" not in rendered
        assert "not only inside the fix delta" not in rendered

    def test_unavailable_task_domain_renders_no_rule(self):
        inputs = self._inputs()
        inputs["scope_task_available"] = False
        rendered = _format_review_scope(inputs)
        assert "task_changed_paths" not in rendered


class TestManifestWithoutASecondDomain:
    def test_incremental_without_task_domain_labels_the_manifest(self):
        # G1's whole-task attachment can fail without degrading the round; the
        # manifest must then say the ranges are the fix delta rather than let
        # them read as the whole task.
        rendered = _format_review_scope({
            "scope_mode": "incremental",
            "baseline_id": "fix-1-abcdef123456",
            "scope_changed_paths": ["src/fix.py"],
            "scope_causal_anchors": {"src/fix.py": [[40, 48]]},
            "scope_task_available": False,
            "scope_task_changed_paths": [],
            "scope_diff": "diff --git a/src/fix.py b/src/fix.py\n+fixed\n",
        })
        assert "every range below is this round's fix delta" in rendered
        assert "src/fix.py: +9 added (this fix) -0 deleted (this fix's baseline)" in rendered

    def test_empty_scope_still_renders_a_manifest_line(self):
        rendered = _format_review_scope({
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": [],
            "scope_diff": "",
        })
        assert "scope_manifest" in rendered
        assert "no changed path in this scope" in rendered


# ---------------------------------------------------------------------------
# still_present fold-in (one defect → one fix-loop finding)
# ---------------------------------------------------------------------------


class TestStillPresentFoldIn:
    """A ``still_present`` previous finding whose primary evidence position the
    reviewer re-reported this round is folded into that finding.

    Data shapes are modelled on the creqt flow ``20260826-090952_6c1bcc5e``
    rounds 22 / 24 / 26, where the review prompt's "re-list still_present
    issues" rule and the verbatim re-admission each contributed a copy of the
    same defect, so one deployment defect reached the fix loop three times.
    """

    _TASK = (
        "Harden the deployment scripts: run remote commands with sudo, "
        "document the rollback path, and validate the closure setup inputs"
    )

    @pytest.fixture
    def flow(self, tmp_path):
        flow = FlowInstance(
            flow_id="test-flow-fold",
            task_description=self._TASK,
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "fold-change",
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
        ]
        return flow

    def _inputs(self, **extra):
        inp = {
            "task_description": self._TASK,
            "task_description_base": self._TASK,
            "changes_made": {
                "files_changed": [
                    {"path": "deployment/push-update.sh", "action": "modify"},
                    {"path": "deployment/README.md", "action": "modify"},
                    {"path": "deployment/closure/setup.sh", "action": "modify"},
                ],
            },
            "test_results": {"passed": True, "returncode": 0},
            "spec_content": {},
            "fix_iteration": 2,
            "max_fix_iterations": 10,
        }
        inp.update(extra)
        return inp

    @staticmethod
    def _issue(*, path, line, actual, expected, divergence, quote,
               severity="high"):
        return {
            "severity": severity,
            "actual_behavior": actual,
            "expected_behavior": expected,
            "divergence": divergence,
            "expectation_source": {
                "type": "task_description",
                "verbatim_quote": quote,
            },
            "evidence_lines": [f"{path}:{line}"],
            "missing_in": [],
        }

    @staticmethod
    def _run(step, flow, payload):
        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = json.dumps(payload)
            mock_cls.return_value = mock_caller
            return self_check_handler(step, flow)

    def test_same_position_folds_into_one_finding_losslessly(self, flow):
        # Round-24 shape: the reviewer re-reports the very positions it
        # verdicts ``still_present``, in its own new wording. One defect must
        # reach the fix loop once — carrying BOTH statements and BOTH
        # expectation-source quotes.
        prev = [
            self._issue(
                path="deployment/push-update.sh", line=104,
                actual="ssh command runs without sudo",
                expected="ssh command runs under sudo",
                divergence="update fails with permission denied",
                quote="run remote commands with sudo",
                severity="high",
            ),
            self._issue(
                path="deployment/README.md", line=506,
                actual="documented command omits the sudo prefix",
                expected="documented command carries the sudo prefix",
                divergence="operator copies a command that cannot run",
                quote="document the rollback path",
                severity="medium",
            ),
        ]
        current = [
            self._issue(
                path="deployment/push-update.sh", line=104,
                actual="the remote invocation is still unprivileged",
                expected="the remote invocation is privileged",
                divergence="deploy aborts on the target host",
                quote="Harden the deployment scripts",
            ),
            self._issue(
                path="deployment/README.md", line=506,
                actual="the README example lacks elevation",
                expected="the README example is elevated",
                divergence="the pasted command is rejected",
                quote="validate the closure setup inputs",
            ),
        ]
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._inputs(prev_self_check_issues=prev),
        )
        result = self._run(step, flow, {
            "issues": current,
            "previous_issue_resolutions": [
                {"prev_issue_summary": "ssh without sudo",
                 "status": "still_present"},
                {"prev_issue_summary": "README command without sudo",
                 "status": "still_present"},
            ],
            "summary": "unresolved",
        })

        assert result == StepStatus.REVISION_NEEDED
        issues = step.outputs["issues"]
        # Two defects, two findings — not four.
        assert len(issues) == 2
        assert step.outputs["actionable_count"] == 2

        stats = step.outputs["validation_stats"]
        assert stats["folded_still_present_count"] == 2
        assert "readmitted_still_present_count" not in stats
        assert stats["kept_count"] == 2

        # Each finding keeps its own fields untouched and carries the previous
        # round's statement in full, expectation source included.
        for cur, old, reported in zip(issues, prev, current):
            assert cur["actual_behavior"] == reported["actual_behavior"]
            assert cur["actual_behavior"] != old["actual_behavior"]
            carried = cur["divergence"]
            assert carried.startswith(reported["divergence"])
            assert carried.count(_FOLD_MARKER) == 1
            for value in (
                old["actual_behavior"],
                old["expected_behavior"],
                old["divergence"],
                old["severity"],
                old["evidence_lines"][0],
                old["expectation_source"]["verbatim_quote"],
                old["expectation_source"]["type"],
            ):
                assert value in carried

        instructions = step.outputs["fix_instructions"]
        for issue in current + prev:
            assert issue["actual_behavior"] in instructions
            assert issue["expected_behavior"] in instructions
            assert issue["divergence"] in instructions
            assert (
                issue["expectation_source"]["verbatim_quote"] in instructions
            )
        # And the fix_context the downstream step reads carries the same dicts.
        assert step.outputs["fix_context"]["issues"] == issues

    def test_a_finding_carrying_an_older_statement_folds_losslessly(self, flow):
        # Round-26 SHAPE only: the previous finding is one a round-22 fold
        # already produced, so it arrives carrying an older generation of the
        # same defect. What is asserted is entirely THIS round's behaviour —
        # one previous finding, one same-position re-report, one fold — and
        # that the statement it was already carrying is not lost by that fold.
        # Nothing here requires a later round to fold, re-identify or dedup
        # anything again.
        gen22 = self._issue(
            path="deployment/README.md", line=506,
            actual="doc command missing sudo (first wording)",
            expected="doc command with sudo",
            divergence="operator hits permission denied",
            quote="run remote commands with sudo",
        )
        gen24 = self._issue(
            path="deployment/README.md", line=506,
            actual="doc command still unprivileged (second wording)",
            expected="doc command elevated",
            divergence="copy-paste of the doc fails",
            quote="document the rollback path",
        )
        # The previous finding as a round-22 fold actually produced it.
        gen24 = _fold_still_present_into_current([gen24], [gen22], [True])[0][0]
        gen26 = self._issue(
            path="deployment/README.md", line=506,
            actual="README still shows the bare command (third wording)",
            expected="README shows the elevated command",
            divergence="the documented step cannot be executed",
            quote="validate the closure setup inputs",
        )
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._inputs(prev_self_check_issues=[gen24]),
        )
        # One previous finding, one resolution: the verdict pairs positionally,
        # so the fold's eligibility does not rest on any wording comparison.
        result = self._run(step, flow, {
            "issues": [gen26],
            "previous_issue_resolutions": [
                {"prev_issue_summary": "README command without sudo",
                 "status": "still_present"},
            ],
            "summary": "unresolved",
        })

        assert result == StepStatus.REVISION_NEEDED
        issues = step.outputs["issues"]
        assert len(issues) == 1
        assert step.outputs["actionable_count"] == 1
        assert step.outputs["validation_stats"]["folded_still_present_count"] == 1

        # The chain is flattened, newest-first, and nothing nests deeper: one
        # marker per carried generation, in chronological order.
        carried = issues[0]["divergence"]
        assert carried.count(_FOLD_MARKER) == 2
        assert issues[0]["actual_behavior"] == gen26["actual_behavior"]
        assert carried.startswith(gen26["divergence"])
        order = [
            carried.index(generation["actual_behavior"])
            for generation in (gen24, gen22)
        ]
        assert order == sorted(order)
        for generation in (gen24, gen22):
            assert generation["expected_behavior"] in carried

        instructions = step.outputs["fix_instructions"]
        for generation in (gen22, gen24, gen26):
            assert generation["actual_behavior"] in instructions
            assert (
                generation["expectation_source"]["verbatim_quote"]
                in instructions
            )
        # One bullet, not three.
        assert instructions.count("- [") == 1

    def test_a_refreshed_expectation_source_is_carried_too(self, flow):
        # A previous finding may already carry an expectation-source region
        # from an earlier fold while the reviewer re-reports it against a
        # DIFFERENT quote. Inheriting the old region would leave this round's
        # structured quote — a field no fix-loop renderer prints — unreadable.
        older = self._issue(
            path="deployment/README.md", line=506,
            actual="doc command missing sudo (first wording)",
            expected="doc command with sudo",
            divergence="operator hits permission denied",
            quote="run remote commands with sudo",
        )
        carrying = self._issue(
            path="deployment/README.md", line=506,
            actual="doc command still unprivileged (second wording)",
            expected="doc command elevated",
            divergence="copy-paste of the doc fails",
            quote="document the rollback path",
        )
        carrying = _fold_still_present_into_current(
            [carrying], [older], [True],
        )[0][0]
        # Same finding re-reported this round, now grounded on another quote.
        current = dict(
            carrying,
            expectation_source={
                "type": "task_description",
                "verbatim_quote": "validate the closure setup inputs",
            },
        )
        prev = self._issue(
            path="deployment/README.md", line=506,
            actual="README command not elevated (previous round)",
            expected="README command elevated",
            divergence="the documented step cannot be executed",
            quote="Harden the deployment scripts",
        )
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._inputs(prev_self_check_issues=[prev]),
        )
        result = self._run(step, flow, {
            "issues": [current],
            "previous_issue_resolutions": [
                {"prev_issue_summary": "README command not elevated",
                 "status": "still_present"},
            ],
            "summary": "unresolved",
        })

        assert result == StepStatus.REVISION_NEEDED
        issues = step.outputs["issues"]
        assert len(issues) == 1
        instructions = step.outputs["fix_instructions"]
        # Both the inherited quote and this round's own quote are readable,
        # alongside every carried statement.
        for quote in (
            "document the rollback path",
            "validate the closure setup inputs",
            "run remote commands with sudo",
            "Harden the deployment scripts",
        ):
            assert quote in instructions
        for statement in (older, prev):
            assert statement["actual_behavior"] in instructions

    def test_a_deduped_previous_finding_leaves_its_statement_behind(self, flow):
        # A previous finding the fail-closed sweep re-admits keeps the
        # pre-existing signature dedup: with the same position and the same
        # own wording as a current finding it is dropped as a duplicate, and
        # WHICH entry survives is unchanged. Only its CARRIED statement is
        # rescued — that statement has no other route into the fix loop, and
        # without the fold it would have been re-admitted on its own.
        oldest = self._issue(
            path="deployment/README.md", line=506,
            actual="doc command missing sudo (first wording)",
            expected="doc command with sudo",
            divergence="operator hits permission denied",
            quote="run remote commands with sudo",
            severity="critical",
        )
        carrying_prev = _fold_still_present_into_current(
            [self._issue(
                path="deployment/README.md", line=506,
                actual="the README example lacks elevation",
                expected="the README example is elevated",
                divergence="the pasted command is rejected",
                quote="document the rollback path",
                severity="medium",
            )],
            [oldest],
            [True],
        )[0][0]
        other_prev = self._issue(
            path="deployment/push-update.sh", line=104,
            actual="ssh command runs without sudo",
            expected="ssh command runs under sudo",
            divergence="update fails with permission denied",
            quote="run remote commands with sudo",
        )
        # The reviewer re-listed the carrying finding in ITS OWN earlier
        # wording, so the two collide on the signature key.
        current = self._issue(
            path="deployment/README.md", line=506,
            actual="the README example lacks elevation",
            expected="the README example is elevated",
            divergence="the pasted command is rejected",
            quote="Harden the deployment scripts",
            severity="medium",
        )
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._inputs(
                prev_self_check_issues=[carrying_prev, other_prev],
            ),
        )
        # One indecisive verdict against two previous findings → the
        # fail-closed sweep re-admits both, neither individually identified,
        # so neither is folded and both take the re-admission path.
        result = self._run(step, flow, {
            "issues": [current],
            "previous_issue_resolutions": [
                {"prev_issue_summary": "", "status": "still_present"},
            ],
            "summary": "unresolved",
        })

        assert result == StepStatus.REVISION_NEEDED
        issues = step.outputs["issues"]
        # The dedup verdict itself is untouched: the duplicate is still
        # dropped, and the current finding is still the survivor.
        assert len(issues) == 2
        assert "folded_still_present_count" not in step.outputs["validation_stats"]
        assert issues[0]["actual_behavior"] == current["actual_behavior"]
        assert issues[1] == other_prev
        # The statement the duplicate was carrying is readable on the
        # survivor, whose severity took the higher of the two.
        assert oldest["actual_behavior"] in issues[0]["divergence"]
        assert issues[0]["severity"] == "critical"
        instructions = step.outputs["fix_instructions"]
        assert oldest["actual_behavior"] in instructions
        assert oldest["expectation_source"]["verbatim_quote"] in instructions

    def test_line_drift_does_not_fold_and_still_readmits(self, flow):
        # The fold criterion is structural: a drifted line number is a
        # DIFFERENT position, so the previous finding keeps its own trip
        # through the fail-closed re-admission path rather than being joined
        # by a similarity guess.
        prev = self._issue(
            path="deployment/push-update.sh", line=104,
            actual="ssh command runs without sudo",
            expected="ssh command runs under sudo",
            divergence="update fails with permission denied",
            quote="run remote commands with sudo",
        )
        current = self._issue(
            path="deployment/push-update.sh", line=117,
            actual="the remote invocation is still unprivileged",
            expected="the remote invocation is privileged",
            divergence="deploy aborts on the target host",
            quote="Harden the deployment scripts",
        )
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._inputs(prev_self_check_issues=[prev]),
        )
        result = self._run(step, flow, {
            "issues": [current],
            "previous_issue_resolutions": [
                {"prev_issue_summary": "ssh without sudo",
                 "status": "still_present"},
            ],
            "summary": "unresolved",
        })

        assert result == StepStatus.REVISION_NEEDED
        issues = step.outputs["issues"]
        assert len(issues) == 2
        assert step.outputs["actionable_count"] == 2
        assert issues[1] == prev
        assert all(_FOLD_MARKER not in i["divergence"] for i in issues)

        stats = step.outputs["validation_stats"]
        assert stats["readmitted_still_present_count"] == 1
        assert "folded_still_present_count" not in stats
        assert prev["actual_behavior"] in step.outputs["fix_instructions"]

    def test_missing_position_counterpart_still_readmits(self, flow):
        # No finding at that position at all this round → the pre-existing
        # verbatim re-admission is untouched.
        prev = self._issue(
            path="deployment/closure/setup.sh", line=306,
            actual="setup does not validate its inputs",
            expected="setup validates its inputs",
            divergence="a malformed input reaches the installer",
            quote="validate the closure setup inputs",
        )
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._inputs(prev_self_check_issues=[prev]),
        )
        result = self._run(step, flow, {
            "issues": [],
            "previous_issue_resolutions": [
                {"prev_issue_summary": "setup input validation",
                 "status": "still_present"},
            ],
            "summary": "nothing new",
        })

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["issues"] == [prev]
        assert step.outputs["actionable_count"] == 1
        stats = step.outputs["validation_stats"]
        assert stats["readmitted_still_present_count"] == 1
        assert "folded_still_present_count" not in stats

    def test_fail_closed_sweep_entries_are_readmitted_not_folded(self, flow):
        # An indecisive ``still_present`` verdict cannot say WHICH previous
        # finding survives, so the fail-closed sweep re-admits every unclaimed
        # one. Those carry no verdict of their own, so sharing an evidence
        # position with a current finding is not evidence of the same defect —
        # they keep the separate re-admission path.
        prev = [
            self._issue(
                path="deployment/push-update.sh", line=104,
                actual="ssh command runs without sudo",
                expected="ssh command runs under sudo",
                divergence="update fails with permission denied",
                quote="run remote commands with sudo",
            ),
            self._issue(
                path="deployment/README.md", line=506,
                actual="documented command omits the sudo prefix",
                expected="documented command carries the sudo prefix",
                divergence="operator copies a command that cannot run",
                quote="document the rollback path",
            ),
        ]
        current = [
            self._issue(
                path="deployment/push-update.sh", line=104,
                actual="the remote invocation is still unprivileged",
                expected="the remote invocation is privileged",
                divergence="deploy aborts on the target host",
                quote="Harden the deployment scripts",
            ),
            self._issue(
                path="deployment/README.md", line=506,
                actual="the README example lacks elevation",
                expected="the README example is elevated",
                divergence="the pasted command is rejected",
                quote="validate the closure setup inputs",
            ),
        ]
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._inputs(prev_self_check_issues=prev),
        )
        # One verdict, two previous findings, and a summary with no
        # discriminating tokens → neither pairing nor content match decides.
        result = self._run(step, flow, {
            "issues": current,
            "previous_issue_resolutions": [
                {"prev_issue_summary": "", "status": "still_present"},
            ],
            "summary": "unresolved",
        })

        assert result == StepStatus.REVISION_NEEDED
        issues = step.outputs["issues"]
        assert len(issues) == 4
        assert all(_FOLD_MARKER not in i["divergence"] for i in issues)
        stats = step.outputs["validation_stats"]
        assert stats["readmitted_still_present_count"] == 2
        assert "folded_still_present_count" not in stats

    def test_omission_findings_sharing_missing_in_do_not_fold(self, flow):
        # Two distinct omissions can name the same integration point. With no
        # evidence line to compare, there is no position match and both reach
        # the fix loop.
        prev = {
            "severity": "high",
            "actual_behavior": "the rollback hook is never registered",
            "expected_behavior": "the rollback hook is registered",
            "divergence": "a failed push cannot be rolled back",
            "expectation_source": {
                "type": "task_description",
                "verbatim_quote": "document the rollback path",
            },
            "evidence_lines": [],
            "missing_in": ["deployment/push-update.sh"],
        }
        current = {
            "severity": "high",
            "actual_behavior": "the input validation step is absent",
            "expected_behavior": "the input validation step runs",
            "divergence": "a malformed input reaches the installer",
            "expectation_source": {
                "type": "task_description",
                "verbatim_quote": "validate the closure setup inputs",
            },
            "evidence_lines": [],
            "missing_in": ["deployment/push-update.sh"],
        }
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._inputs(prev_self_check_issues=[prev]),
        )
        result = self._run(step, flow, {
            "issues": [current],
            "previous_issue_resolutions": [
                {"prev_issue_summary": "rollback hook never registered",
                 "status": "still_present"},
            ],
            "summary": "unresolved",
        })

        assert result == StepStatus.REVISION_NEEDED
        issues = step.outputs["issues"]
        assert len(issues) == 2
        assert all(_FOLD_MARKER not in i["divergence"] for i in issues)
        stats = step.outputs["validation_stats"]
        assert stats["readmitted_still_present_count"] == 1
        assert "folded_still_present_count" not in stats

    def test_counters_and_dropped_summary_stay_consistent(self, flow, caplog):
        # Mixed round: one fold, one re-admission, and two structurally
        # rejected raw issues. The drop tally reports STRUCTURAL VALIDATION
        # only, so it agrees with the rejection-reason counters printed beside
        # it however many findings the fold and the re-admission add back; the
        # fold counter must never surface as a drop reason.
        import logging

        folding_prev = self._issue(
            path="deployment/README.md", line=506,
            actual="documented command omits the sudo prefix",
            expected="documented command carries the sudo prefix",
            divergence="operator copies a command that cannot run",
            quote="document the rollback path",
        )
        readmitted_prev = self._issue(
            path="deployment/closure/setup.sh", line=306,
            actual="setup does not validate its inputs",
            expected="setup validates its inputs",
            divergence="a malformed input reaches the installer",
            quote="validate the closure setup inputs",
        )
        current = self._issue(
            path="deployment/README.md", line=506,
            actual="the README example lacks elevation",
            expected="the README example is elevated",
            divergence="the pasted command is rejected",
            quote="Harden the deployment scripts",
        )
        rejected = [
            self._issue(
                path="deployment/README.md", line=12,
                actual="a", expected="b", divergence="c", quote="",
            ),
            self._issue(
                path="deployment/README.md", line=13,
                actual="d", expected="e", divergence="f", quote="",
            ),
        ]
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._inputs(
                prev_self_check_issues=[folding_prev, readmitted_prev],
            ),
        )
        with caplog.at_level(
            logging.INFO, logger="tianluo.engine.steps.self_check"
        ):
            result = self._run(step, flow, {
                "issues": [current] + rejected,
                "previous_issue_resolutions": [
                    {"prev_issue_summary": "README command without sudo",
                     "status": "still_present"},
                    {"prev_issue_summary": "setup input validation",
                     "status": "still_present"},
                ],
                "summary": "unresolved",
            })

        assert result == StepStatus.REVISION_NEEDED
        stats = step.outputs["validation_stats"]
        assert stats["input_count"] == 3
        assert stats["empty_quote_count"] == 2
        assert stats["folded_still_present_count"] == 1
        assert stats["readmitted_still_present_count"] == 1
        # 1 validated + 1 re-admitted; the fold adds no entry.
        assert stats["kept_count"] == 2
        assert len(step.outputs["issues"]) == 2
        assert step.outputs["actionable_count"] == len(step.outputs["issues"])

        summary = [
            r.getMessage() for r in caplog.records
            if "Self-check validation:" in r.getMessage()
        ]
        assert len(summary) == 1
        # 3 raw − 1 validated survivor = 2 dropped, exactly the two rejections
        # the reason counters report. Folding and re-admission move
        # ``kept_count`` afterwards and must not distort this line.
        assert "3 raw → 1 kept (dropped 2:" in summary[0]
        assert "empty_quote_count=2" in summary[0]
        assert "folded_still_present_count" not in summary[0]
        assert "readmitted_still_present_count" not in summary[0]

    def test_dropped_summary_survives_more_readmissions_than_rejections(
        self, flow, caplog,
    ):
        # Deriving the drop tally from the FINAL kept count would go
        # non-positive here and silence the log entirely, hiding a real
        # structural rejection.
        import logging

        prev = [
            self._issue(
                path="deployment/push-update.sh", line=104,
                actual="ssh command runs without sudo",
                expected="ssh command runs under sudo",
                divergence="update fails with permission denied",
                quote="run remote commands with sudo",
            ),
            self._issue(
                path="deployment/closure/setup.sh", line=306,
                actual="setup does not validate its inputs",
                expected="setup validates its inputs",
                divergence="a malformed input reaches the installer",
                quote="validate the closure setup inputs",
            ),
        ]
        rejected = self._issue(
            path="deployment/README.md", line=12,
            actual="a", expected="b", divergence="c", quote="",
        )
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=self._inputs(prev_self_check_issues=prev),
        )
        with caplog.at_level(
            logging.INFO, logger="tianluo.engine.steps.self_check"
        ):
            result = self._run(step, flow, {
                "issues": [rejected],
                "previous_issue_resolutions": [
                    {"prev_issue_summary": "ssh without sudo",
                     "status": "still_present"},
                    {"prev_issue_summary": "setup input validation",
                     "status": "still_present"},
                ],
                "summary": "unresolved",
            })

        assert result == StepStatus.REVISION_NEEDED
        stats = step.outputs["validation_stats"]
        assert stats["empty_quote_count"] == 1
        assert stats["readmitted_still_present_count"] == 2
        assert step.outputs["actionable_count"] == 2
        summary = [
            r.getMessage() for r in caplog.records
            if "Self-check validation:" in r.getMessage()
        ]
        assert len(summary) == 1
        assert "1 raw → 0 kept (dropped 1:" in summary[0]
        assert "empty_quote_count=1" in summary[0]
