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

    def test_returns_completed_when_max_iterations_reached(self, flow, step):
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

            with patch("se3.engine.steps.self_check._get_max_fix_iterations", return_value=3):
                result = self_check_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["max_iterations_reached"] is True
        assert "3" in step.outputs["warning"]
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
        assert "final fix attempt" in result
