"""Tests for the self_check step handler."""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from se3.engine.models import FlowInstance, Step, StepStatus, StepType, FlowStatus
from se3.engine.steps.self_check import (
    self_check_handler,
    _format_changes,
    _format_test_results,
    _format_spec_content,
    _format_fix_context,
    _issue_signature,
    _issues_converged,
    SELF_CHECK_PROMPT,
)


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

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
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
                {"severity": "medium", "description": "Could add defensive check", "location": "src/feature.py:42"},
                {"severity": "low", "description": "Consider logging here", "location": "src/feature.py:10"},
            ],
            "summary": "Minor suggestions only.",
        })

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
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
                {"severity": "critical", "description": "Missing null check causes crash", "location": "src/feature.py:30"},
            ],
            "summary": "Critical issue found.",
        })

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
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
                {"severity": "high", "description": "Unhandled error path", "location": "src/feature.py:55"},
                {"severity": "medium", "description": "Suggestion", "location": "src/feature.py:10"},
            ],
            "summary": "Issues found.",
        })

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["actionable_count"] == 2
        assert step.outputs["fix_instructions"]
        assert "Unhandled error path" in step.outputs["fix_instructions"]

    def test_returns_revision_needed_at_max_iterations(self, flow, step):
        """self_check returns REVISION_NEEDED even at max iterations.

        Exhaustion is handled centrally by state_machine.transition_to_next.
        """
        step.inputs["fix_iteration"] = 3
        response = json.dumps({
            "issues": [
                {"severity": "critical", "description": "Still broken", "location": "src/feature.py:30"},
            ],
            "summary": "Issue persists.",
        })

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["actionable_count"] == 1

    def test_returns_failed_on_llm_error(self, flow, step):
        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.side_effect = RuntimeError("LLM timeout")
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.FAILED
        assert "LLM timeout" in step.error_message

    def test_returns_failed_on_unparseable_response(self, flow, step):
        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = "not valid json at all"
            mock_cls.return_value = mock_caller

            with patch("se3.engine.steps.self_check.parse_json_response", return_value=None):
                result = self_check_handler(step, flow)

        assert result == StepStatus.FAILED
        assert step.error_message

    def test_fix_context_contains_all_issues(self, flow, step):
        step.inputs["fix_iteration"] = 0
        response = json.dumps({
            "issues": [
                {"severity": "critical", "description": "Critical bug", "location": "a.py"},
                {"severity": "medium", "description": "Suggestion", "location": "b.py"},
                {"severity": "high", "description": "Missing handler", "location": "c.py"},
                {"severity": "low", "description": "Nit", "location": "d.py"},
            ],
            "summary": "Mixed.",
        })

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
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

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            self_check_handler(step, flow)

            call_kwargs = mock_caller.call.call_args[1]
            assert call_kwargs["json_mode"] == "two_phase"
            assert "json_schema_hint" in call_kwargs

    def test_prompt_excludes_spec_compliance(self):
        assert "do NOT" in SELF_CHECK_PROMPT.lower() or "Do NOT" in SELF_CHECK_PROMPT
        assert "spec compliance" in SELF_CHECK_PROMPT.lower()

    def test_prompt_includes_review_dimensions(self):
        assert "Logic Completeness" in SELF_CHECK_PROMPT
        assert "Code Robustness" in SELF_CHECK_PROMPT
        assert "Functional Gaps" in SELF_CHECK_PROMPT
        assert "Test Coverage Gaps" in SELF_CHECK_PROMPT

    def test_prompt_uses_severity_not_priority(self):
        assert '"severity":' in SELF_CHECK_PROMPT
        assert "critical|high|medium|low" in SELF_CHECK_PROMPT

    def test_fix_iteration_passed_to_prompt(self, flow, step):
        step.inputs["fix_iteration"] = 2
        response = json.dumps({"issues": [], "summary": "OK"})

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
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
                {"severity": "high", "description": "needs fix", "location": "x.py"},
            ],
            "summary": "issue",
        })

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
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

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
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


class TestFormatSpecContent:
    def test_empty(self):
        assert _format_spec_content({}) == "No specifications provided."

    def test_single_spec(self):
        result = _format_spec_content({"base": "Content here"})
        assert "### base" in result
        assert "Content here" in result


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
        """Severity is not part of the signature — re-reporting the same issue
        with a different severity still counts as convergence."""
        a = _issue_signature([{"severity": "low", "location": "a", "description": "x"}])
        b = _issue_signature([{"severity": "high", "location": "a", "description": "x"}])
        assert a == b

    def test_skips_non_dict(self):
        sig = _issue_signature(["not-a-dict", {"location": "a", "description": "x"}])
        assert sig == {("a", "x")}

    def test_skips_empty_signature(self):
        sig = _issue_signature([{"location": "", "description": ""}])
        assert sig == set()


class TestIssuesConverged:
    def test_empty_current_returns_false(self):
        assert _issues_converged([], [{"location": "a", "description": "x"}]) is False

    def test_empty_prev_returns_false(self):
        assert _issues_converged([{"location": "a", "description": "x"}], []) is False

    def test_none_prev_returns_false(self):
        assert _issues_converged([{"location": "a", "description": "x"}], None) is False

    def test_identical_issues_converges(self):
        issues = [{"severity": "low", "location": "a.py:1", "description": "x"}]
        assert _issues_converged(issues, issues) is True

    def test_subset_converges(self):
        """If current is a subset of prev (LLM only re-reports old issues), converged."""
        prev = [
            {"location": "a", "description": "x"},
            {"location": "b", "description": "y"},
        ]
        current = [{"location": "a", "description": "x"}]
        assert _issues_converged(current, prev) is True

    def test_new_issue_not_converged(self):
        """A new issue not in prev means progress — not converged."""
        prev = [{"location": "a", "description": "x"}]
        current = [
            {"location": "a", "description": "x"},
            {"location": "new.py:42", "description": "new issue"},
        ]
        assert _issues_converged(current, prev) is False

    def test_paraphrased_description_same_location_converges(self):
        """LLMs routinely paraphrase the same logical issue differently across
        iterations. When the location is identical and descriptions share
        the same tokens after normalization, convergence must still fire."""
        prev = [
            {"location": "src/foo.py:42",
             "description": "Missing null check on user input"},
        ]
        current = [
            {"location": "src/foo.py:42",
             "description": "missing null check, on user input!"},
        ]
        assert _issues_converged(current, prev) is True

    def test_location_only_convergence_when_descriptions_fully_differ(self):
        """Location-only fallback catches heavier paraphrases: if every current
        issue lives at a previously-flagged location, treat as converged even
        when the free-text descriptions use entirely different wording."""
        prev = [
            {"location": "src/foo.py:42",
             "description": "Missing null check on user input"},
        ]
        current = [
            {"location": "src/foo.py:42",
             "description": "User input not validated for None"},
        ]
        assert _issues_converged(current, prev) is True

    def test_new_location_defeats_location_only_convergence(self):
        """If current introduces a location never seen in prev, even the
        lenient location-only layer must report not-converged — a genuinely
        new issue has been discovered."""
        prev = [
            {"location": "src/foo.py:42",
             "description": "Missing null check"},
        ]
        current = [
            {"location": "src/foo.py:42", "description": "Null check missing"},
            {"location": "src/bar.py:10", "description": "Leaked handle"},
        ]
        assert _issues_converged(current, prev) is False


class TestSelfCheckConvergence:
    """Tests that self_check short-circuits when the LLM re-reports the same issues."""

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

    def test_converges_when_issues_repeat(self, flow):
        prev_issues = [
            {"severity": "low", "location": "a.py:1", "description": "x"},
            {"severity": "medium", "location": "b.py:2", "description": "y"},
        ]
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Fix bug",
                "changes_made": {},
                "test_results": {"passed": True, "returncode": 0},
                "spec_content": {},
                "fix_iteration": 2,
                "max_fix_iterations": 10,
                "prev_self_check_issues": prev_issues,
                "self_check_convergence_enabled": True,
            },
        )
        response = json.dumps({"issues": prev_issues, "summary": "same"})

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs.get("converged") is True
        assert "convergence_reason" in step.outputs
        # Unresolved issues must still be surfaced to downstream steps
        # even when the loop short-circuits.
        assert step.outputs.get("unresolved_issues") == prev_issues

    def test_does_not_converge_on_first_iteration(self, flow):
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Fix bug",
                "changes_made": {},
                "test_results": {"passed": True, "returncode": 0},
                "spec_content": {},
                "fix_iteration": 0,
                "max_fix_iterations": 10,
            },
        )
        response = json.dumps({
            "issues": [{"severity": "low", "location": "a", "description": "x"}],
            "summary": "first",
        })

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert not step.outputs.get("converged")

    def test_does_not_converge_when_new_issue_appears(self, flow):
        prev_issues = [{"severity": "low", "location": "a", "description": "x"}]
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Fix bug",
                "changes_made": {},
                "test_results": {"passed": True, "returncode": 0},
                "spec_content": {},
                "fix_iteration": 2,
                "max_fix_iterations": 10,
                "prev_self_check_issues": prev_issues,
            },
        )
        response = json.dumps({
            "issues": [
                {"severity": "low", "location": "a", "description": "x"},
                {"severity": "medium", "location": "new.py", "description": "new"},
            ],
            "summary": "new issue found",
        })

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert not step.outputs.get("converged")
