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


class TestScopeDiffTruncation:
    def test_large_diff_is_truncated_with_artifact_pointer(self):
        from tianluo.engine.truncation import SELF_CHECK_SCOPE_DIFF_MAX_CHARS

        diff = "x" * (SELF_CHECK_SCOPE_DIFF_MAX_CHARS + 5000) + "\nTAIL-SENTINEL-LINE\n"
        scope = _format_review_scope({
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": ["big.py"],
            "scope_diff": diff,
            "scope_diff_artifact": "tianluo/state/review/diffs/abc.diff",
            "scope_undecidable": False,
        })
        assert "TRUNCATED" in scope
        assert "tianluo/state/review/diffs/abc.diff" in scope
        assert "TAIL-SENTINEL-LINE" not in scope
        assert len(scope) < len(diff) + 500

    def test_small_diff_is_not_truncated(self):
        scope = _format_review_scope({
            "scope_mode": "full",
            "baseline_id": "impl-abcdef123456",
            "scope_changed_paths": ["small.py"],
            "scope_diff": "diff --git a/small.py b/small.py\n+new",
            "scope_diff_artifact": "tianluo/state/review/diffs/abc.diff",
            "scope_undecidable": False,
        })
        assert "TRUNCATED" not in scope
        assert "+new" in scope


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
