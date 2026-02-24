"""Integration tests for flow engine step handlers.

Tests step implementations with mocked LLM calls.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from .models import FlowInstance, Step, StepStatus, StepType
from .steps import (
    STEP_HANDLERS,
    analyze_handler,
    test_handler as run_test_step,
    commit_handler,
)


class TestAnalyzeStep:
    """Tests for the analyze step."""

    @patch("se3_tools.engine.steps.analyze.LLMCaller")
    def test_analyze_success(self, MockLLMCaller):
        """Test successful analysis."""
        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({
            "task_type": "feature",
            "scope": "backend",
            "complexity": "medium",
            "required_steps": ["analyze", "design", "implement", "test"],
        })
        MockLLMCaller.return_value = mock_caller

        flow = FlowInstance(task_description="Add user login")
        step = Step(step_type=StepType.ANALYZE)
        step.inputs["task_description"] = "Add user login"

        result = analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert "task_type" in step.outputs
        assert step.outputs["task_type"] == "feature"

    @patch("se3_tools.engine.steps.analyze.LLMCaller")
    def test_analyze_invalid_json(self, MockLLMCaller):
        """Test handling of invalid JSON from LLM."""
        mock_caller = MagicMock()
        mock_caller.call.return_value = "not valid json"
        MockLLMCaller.return_value = mock_caller

        flow = FlowInstance(task_description="Test task")
        step = Step(step_type=StepType.ANALYZE)
        step.inputs["task_description"] = "Test task"

        result = analyze_handler(step, flow)

        assert result == StepStatus.FAILED
        assert step.error_message is not None

    @patch("se3_tools.engine.steps.analyze.LLMCaller")
    def test_analyze_llm_error(self, MockLLMCaller):
        """Test handling of LLM call failure."""
        from .llm_caller import LLMCallError

        mock_caller = MagicMock()
        mock_caller.call.side_effect = LLMCallError("API error")
        MockLLMCaller.return_value = mock_caller

        flow = FlowInstance(task_description="Test task")
        step = Step(step_type=StepType.ANALYZE)
        step.inputs["task_description"] = "Test task"

        result = analyze_handler(step, flow)

        assert result == StepStatus.FAILED


class TestTestStep:
    """Tests for the test step."""

    @patch("subprocess.run")
    def test_test_success(self, mock_run):
        """Test successful test execution."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="5 passed, 0 failed",
            stderr="",
        )

        flow = FlowInstance(task_description="Test feature")
        step = Step(step_type=StepType.TEST)

        result = run_test_step(step, flow)

        assert result == StepStatus.COMPLETED
        assert "test_results" in step.outputs

    @patch("subprocess.run")
    def test_test_failure(self, mock_run):
        """Test handling of test failures."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="3 passed, 2 failed",
            stderr="FAILED test_foo",
        )

        flow = FlowInstance(task_description="Test feature")
        step = Step(step_type=StepType.TEST)

        result = run_test_step(step, flow)

        # test_handler returns COMPLETED even on test failure
        # (to allow verify_spec to decide what to do)
        assert result == StepStatus.COMPLETED
        assert step.outputs["tests_passed"] is False


class TestCommitStep:
    """Tests for the commit step."""

    @patch("subprocess.run")
    def test_commit_success(self, mock_run):
        """Test successful commit."""
        # Mock git status (has changes) then git add, git commit, git rev-parse
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="M file.py", stderr=""),  # git status
            MagicMock(returncode=0, stdout="", stderr=""),  # git add
            MagicMock(returncode=0, stdout="[main abc123] message", stderr=""),  # git commit
            MagicMock(returncode=0, stdout="abc123def456", stderr=""),  # git rev-parse
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            flow = FlowInstance(task_description="Test commit")
            flow.change_path = Path(tmpdir) / "dummy"
            step = Step(step_type=StepType.COMMIT)

            result = commit_handler(step, flow)

            assert result == StepStatus.COMPLETED

    @patch("subprocess.run")
    def test_commit_no_changes(self, mock_run):
        """Test commit when there are no changes."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",  # No changes
            stderr="",
        )

        flow = FlowInstance(task_description="Test commit")
        step = Step(step_type=StepType.COMMIT)

        result = commit_handler(step, flow)

        # Should succeed even with no changes
        assert result == StepStatus.COMPLETED


class TestStepHandlers:
    """Tests for the STEP_HANDLERS registry."""

    def test_all_step_types_have_handlers(self):
        """Verify all step types have registered handlers."""
        for step_type in StepType:
            assert step_type in STEP_HANDLERS, f"Missing handler for {step_type}"

    def test_handler_consistency(self):
        """Test that handlers have required interface."""
        for step_type, handler_func in STEP_HANDLERS.items():
            assert callable(handler_func), f"Handler for {step_type} must be callable"


class TestLLMCallerIntegration:
    """Tests for LLM caller integration with retry logic."""

    @patch("se3_tools.engine.llm_caller.ClaudeRunner")
    def test_llm_retry_success(self, MockRunner):
        """Test that retries eventually succeed."""
        from .llm_caller import LLMCaller

        # Set up mock runner to fail twice, then succeed
        mock_runner = MagicMock()
        mock_result_fail = MagicMock(success=False, cmd_used="claude", returncode=1)
        mock_result_ok = MagicMock(success=True, output="success")
        mock_runner.run_with_monitor.side_effect = [
            mock_result_fail,
            mock_result_fail,
            mock_result_ok,
        ]
        MockRunner.return_value = mock_runner

        caller = LLMCaller(max_retries=3, retry_delay=0.01)
        result = caller.call(prompt="test prompt")

        assert result == "success"
        assert mock_runner.run_with_monitor.call_count == 3

    @patch("se3_tools.engine.llm_caller.ClaudeRunner")
    def test_llm_retry_exhausted(self, MockRunner):
        """Test that retry exhaustion raises LLMCallError."""
        from .llm_caller import LLMCaller, LLMCallError

        mock_runner = MagicMock()
        mock_result_fail = MagicMock(success=False, cmd_used="claude", returncode=1)
        mock_runner.run_with_monitor.return_value = mock_result_fail
        MockRunner.return_value = mock_runner

        caller = LLMCaller(max_retries=2, retry_delay=0.01)

        with pytest.raises(LLMCallError):
            caller.call(prompt="test prompt")

        assert mock_runner.run_with_monitor.call_count == 2


class TestContextBuilder:
    """Tests for context builder."""

    def test_build_step_context(self):
        """Test context building for a step."""
        from .context_builder import ContextBuilder

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a mock spec file
            specs_dir = Path(tmpdir) / "openspec" / "specs" / "test-spec"
            specs_dir.mkdir(parents=True)
            spec_file = specs_dir / "spec.md"
            spec_file.write_text("# Test Spec\n\n## Purpose\nTest purpose")

            builder = ContextBuilder(Path(tmpdir))
            context = builder.build_step_context(
                step_type="analyze",
                task_description="Test task",
            )

            assert "Test task" in context
            assert "analyze" in context.lower()

    def test_context_includes_relevant_specs(self):
        """Test that context includes relevant specs."""
        from .context_builder import ContextBuilder

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a spec
            specs_dir = Path(tmpdir) / "openspec" / "specs" / "flow-engine"
            specs_dir.mkdir(parents=True)
            (specs_dir / "spec.md").write_text("# Flow Engine Spec\nDetails here")

            builder = ContextBuilder(Path(tmpdir))

            context = builder.build_step_context(
                step_type="implement",
                task_description="Update flow engine",
                previous_outputs={"analysis": {"task_type": "feature"}},
                relevant_specs=["flow-engine"],
            )

            assert "Flow Engine Spec" in context
            assert "feature" in context


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
