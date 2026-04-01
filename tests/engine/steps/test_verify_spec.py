"""Tests for the verify_spec step handler."""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from se3.engine.models import FlowInstance, Step, StepStatus, StepType, FlowStatus, State
from se3.engine.steps.verify_spec import (
    verify_spec_handler,
    _format_spec_content,
    _format_changes,
    _format_test_results,
    _format_fix_context,
    _get_max_fix_iterations,
    VERIFY_PROMPT,
)


class TestVerifySpecHandler:
    """Test cases for verify_spec_handler."""

    @pytest.fixture
    def flow(self, tmp_path):
        """Create a test flow instance."""
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "test-change",
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERIFY_SPEC,
        ]
        return flow

    @pytest.fixture
    def step(self):
        """Create a test step."""
        return Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Test task",
                "spec_content": {"spec.md": "Test spec content"},
                "changes_made": {"files_changed": [{"path": "test.py", "action": "modify"}]},
                "test_results": {"passed": True, "returncode": 0, "stdout": "All tests passed"},
            },
        )

    @pytest.fixture
    def mock_llm_response(self):
        """Create a mock LLM response."""
        return """{
            "verified": true,
            "issues": [],
            "summary": "All good",
            "recommendations": [],
            "test_analysis": {"tests_passed": true, "failure_summary": "", "root_cause": ""},
            "fix_instructions": ""
        }"""

    def test_verify_prompt_includes_test_failure_analysis(self):
        """Test that VERIFY_PROMPT includes test failure analysis instructions."""
        assert "Test Failure Analysis" in VERIFY_PROMPT
        assert "test_analysis" in VERIFY_PROMPT
        assert "fix_instructions" in VERIFY_PROMPT
        assert "fix_context" in VERIFY_PROMPT

    def test_handler_returns_completed_when_tests_pass(self, flow, step, mock_llm_response):
        """Test that handler returns COMPLETED when tests pass."""
        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_llm_response
            mock_caller_class.return_value = mock_caller

            result = verify_spec_handler(step, flow)

            assert result == StepStatus.COMPLETED
            assert step.outputs["verified"] is True

    def test_handler_returns_revision_needed_when_tests_fail_and_under_max_iterations(self, flow, step):
        """Test that handler returns REVISION_NEEDED when tests fail and under max iterations."""
        step.inputs["test_results"] = {"passed": False, "returncode": 1, "stdout": "Test failed", "stderr": "AssertionError"}
        step.inputs["fix_iteration"] = 0

        mock_response = """{
            "verified": false,
            "issues": [{"severity": "error", "message": "Tests failed"}],
            "summary": "Tests failed - fix needed",
            "recommendations": ["Fix the test"],
            "test_analysis": {"tests_passed": false, "failure_summary": "Assertion error", "root_cause": "Bug in code"},
            "fix_instructions": "Fix the assertion in test.py line 10"
        }"""

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            result = verify_spec_handler(step, flow)

            assert result == StepStatus.REVISION_NEEDED
            assert step.outputs["fix_needed"] is True
            assert step.outputs["fix_instructions"] == "Fix the assertion in test.py line 10"
            assert step.outputs["fix_context"]["iteration"] == 1

    def test_handler_returns_completed_when_max_iterations_reached(self, flow, step):
        """Test that handler returns COMPLETED with warning when max iterations reached."""
        step.inputs["test_results"] = {"passed": False, "returncode": 1, "stdout": "Test failed", "stderr": "AssertionError"}
        step.inputs["fix_iteration"] = 3  # At max iterations

        mock_response = """{
            "verified": false,
            "issues": [{"severity": "error", "message": "Tests failed"}],
            "summary": "Tests still failing",
            "recommendations": [],
            "test_analysis": {"tests_passed": false, "failure_summary": "Still failing", "root_cause": "Unknown"},
            "fix_instructions": "Keep trying"
        }"""

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            with patch("se3.engine.steps.verify_spec._get_max_fix_iterations", return_value=3):
                result = verify_spec_handler(step, flow)

            assert result == StepStatus.COMPLETED
            assert step.outputs.get("max_iterations_reached") is True
            assert "3" in step.outputs.get("warning", "")

    def test_handler_reads_fix_iteration_from_inputs(self, flow, step):
        """Test that handler reads fix_iteration from step inputs."""
        step.inputs["fix_iteration"] = 2
        step.inputs["test_results"] = {"passed": False, "returncode": 1, "stdout": "Failed", "stderr": ""}

        mock_response = """{
            "verified": false,
            "issues": [],
            "summary": "",
            "recommendations": [],
            "test_analysis": {"tests_passed": false, "failure_summary": "", "root_cause": ""},
            "fix_instructions": "Fix it"
        }"""

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            verify_spec_handler(step, flow)

            # Should include iteration 2 in prompt
            call_args = mock_caller.call.call_args
            prompt = call_args[1]["prompt"]
            assert "Fix iteration: 2" in prompt or "fix iteration: 2" in prompt.lower()

    def test_handler_stores_fix_context_in_outputs(self, flow, step):
        """Test that handler stores fix_context in step outputs when tests fail."""
        test_results = {"passed": False, "returncode": 1, "stdout": "Test error", "stderr": ""}
        step.inputs["test_results"] = test_results
        step.inputs["fix_iteration"] = 1

        mock_response = """{
            "verified": false,
            "issues": [],
            "summary": "",
            "recommendations": [],
            "test_analysis": {"tests_passed": false, "failure_summary": "Summary", "root_cause": "Root cause"},
            "fix_instructions": "Instructions here"
        }"""

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            verify_spec_handler(step, flow)

            fix_context = step.outputs.get("fix_context")
            assert fix_context is not None
            assert fix_context["test_results"] == test_results
            assert fix_context["test_analysis"]["tests_passed"] is False
            assert fix_context["fix_instructions"] == "Instructions here"
            assert fix_context["iteration"] == 2  # Incremented


class TestFormatSpecContent:
    """Test cases for _format_spec_content."""

    def test_empty_content(self):
        assert _format_spec_content({}) == "No specifications provided."

    def test_single_spec(self):
        content = {"spec.md": "This is the spec content"}
        result = _format_spec_content(content)
        assert "### spec.md" in result
        assert "This is the spec content" in result

    def test_multiple_specs(self):
        content = {"spec1.md": "Content 1", "spec2.md": "Content 2"}
        result = _format_spec_content(content)
        assert "### spec1.md" in result
        assert "### spec2.md" in result
        assert "Content 1" in result
        assert "Content 2" in result

    def test_long_content_not_truncated(self):
        long_content = "x" * 4000
        content = {"long.md": long_content}
        result = _format_spec_content(content)
        assert long_content in result


class TestFormatChanges:
    """Test cases for _format_changes."""

    def test_empty_changes(self):
        assert _format_changes({}) == "No changes recorded."

    def test_files_changed(self):
        changes = {
            "files_changed": [
                {"path": "file1.py", "action": "modify", "explanation": "Fixed bug"},
                {"path": "file2.py", "action": "create"},
            ]
        }
        result = _format_changes(changes)
        assert "modify: file1.py" in result
        assert "(Fixed bug)" in result
        assert "create: file2.py" in result

    def test_no_files_changed(self):
        assert _format_changes({"files_changed": []}) == "Changes made but details unavailable."


class TestFormatTestResults:
    """Test cases for _format_test_results."""

    def test_no_results(self):
        assert _format_test_results({}) == "No test results available."

    def test_passed_tests(self):
        results = {"passed": True, "returncode": 0, "stdout": "All good", "stderr": ""}
        result = _format_test_results(results)
        assert "Tests passed: True" in result
        assert "All good" in result

    def test_failed_tests(self):
        results = {"passed": False, "returncode": 1, "stdout": "Output", "stderr": "Error"}
        result = _format_test_results(results)
        assert "Tests passed: False" in result
        assert "Output" in result
        assert "Error" in result

    def test_stdout_truncation(self):
        long_stdout = "x" * 1500
        results = {"passed": True, "returncode": 0, "stdout": long_stdout, "stderr": ""}
        result = _format_test_results(results)
        # Should include last 1000 chars
        assert len(result) < len(long_stdout) + 200


class TestFormatFixContext:
    """Test cases for _format_fix_context."""

    def test_initial_iteration(self):
        result = _format_fix_context(0, 3)
        assert "initial verification" in result.lower()
        assert "no previous fix attempts" in result.lower()

    def test_fix_iteration(self):
        result = _format_fix_context(2, 3)
        assert "Fix iteration: 2 of 3" in result
        assert "Previous fix attempts: 2" in result

    def test_max_iterations_warning(self):
        result = _format_fix_context(3, 3)
        assert "WARNING" in result
        assert "final fix attempt" in result


class TestGetMaxFixIterations:
    """Test cases for _get_max_fix_iterations."""

    def test_from_flow_context(self, tmp_path):
        flow = Mock()
        flow.state.context = {"max_fix_iterations": 5}
        flow.change_path = tmp_path

        result = _get_max_fix_iterations(flow)
        assert result == 5

    def test_default_value(self, tmp_path):
        flow = Mock()
        flow.state.context = {}
        flow.change_path = tmp_path / "nonexistent"

        result = _get_max_fix_iterations(flow)
        assert result == 3  # Default

    def test_from_config_file(self, tmp_path):
        # Create project root and change path
        project_root = tmp_path
        change_path = project_root / "openspec" / "changes" / "test-change"
        change_path.mkdir(parents=True)

        flow = Mock()
        flow.state.context = {"project_root": str(project_root)}
        flow.change_path = change_path

        # Create se3.yaml with custom max_fix_iterations in project root
        config = """
workflow:
  max_fix_iterations: 7
"""
        (project_root / "se3.yaml").write_text(config)

        result = _get_max_fix_iterations(flow)
        assert result == 7


class TestIntegration:
    """Integration tests for verify_spec step."""

    def test_prompt_includes_all_sections(self, tmp_path):
        """Test that the prompt includes all required sections."""
        # Create test flow and step locally
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "test-change",
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERIFY_SPEC,
        ]

        step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Test task",
                "spec_content": {"spec.md": "Test spec content"},
                "changes_made": {"files_changed": [{"path": "test.py", "action": "modify"}]},
                "test_results": {"passed": True, "returncode": 0, "stdout": "All tests passed"},
            },
        )

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = '{"verified": true}'
            mock_caller_class.return_value = mock_caller

            verify_spec_handler(step, flow)

            call_args = mock_caller.call.call_args
            prompt = call_args[1]["prompt"]

            # Check all sections are present
            assert "## Task Description" in prompt
            assert "## Relevant Specifications" in prompt
            assert "## Changes Made" in prompt
            assert "## Test Results" in prompt
            assert "## Fix Context" in prompt
            assert "### Test Failure Analysis" in prompt
