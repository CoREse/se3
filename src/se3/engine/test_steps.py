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

    @patch("se3.engine.steps.analyze.LLMCaller")
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

    @patch("se3.engine.steps.analyze.LLMCaller")
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

    @patch("se3.engine.steps.analyze.LLMCaller")
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

    @patch("subprocess.Popen")
    def test_test_success(self, mock_popen):
        """Test successful test execution."""
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = ("5 passed, 0 failed", "")
        mock_popen.return_value = mock_process

        flow = FlowInstance(task_description="Test feature")
        step = Step(step_type=StepType.TEST)

        result = run_test_step(step, flow)

        assert result == StepStatus.COMPLETED
        assert "test_results" in step.outputs

    @patch("subprocess.Popen")
    def test_test_failure(self, mock_popen):
        """Test handling of test failures."""
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.communicate.return_value = ("3 passed, 2 failed", "FAILED test_foo")
        mock_popen.return_value = mock_process

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

    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
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

    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
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
    """Tests for context builder after dead code removal."""

    def test_dead_methods_removed(self):
        """Verify dead methods are no longer available."""
        from .context_builder import ContextBuilder

        with tempfile.TemporaryDirectory() as tmpdir:
            builder = ContextBuilder(Path(tmpdir))
            assert not hasattr(builder, "build_step_context")
            assert not hasattr(builder, "_build_header")
            assert not hasattr(builder, "get_step_prompt_template")

    def test_retained_methods_exist(self):
        """Verify retained methods still work."""
        from .context_builder import ContextBuilder

        with tempfile.TemporaryDirectory() as tmpdir:
            builder = ContextBuilder(Path(tmpdir))
            assert hasattr(builder, "specs_dir")
            assert hasattr(builder, "_load_spec_content")


class TestIssueDiscoveryInjection:
    """Tests for get_issue_discovery_injection() function."""

    def test_whitelisted_step_returns_prompt(self, tmp_path):
        """Whitelisted step (summarize) returns non-empty prompt."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("summarize", tmp_path)
        assert result != ""
        assert "discovered_issues" in result

    def test_whitelisted_step_verify_spec(self, tmp_path):
        """Whitelisted step (verify_spec) returns non-empty prompt."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("verify_spec", tmp_path)
        assert result != ""
        assert "discovered_issues" in result

    def test_non_whitelisted_step_returns_empty(self, tmp_path):
        """Non-whitelisted step (propose) returns empty string."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("propose", tmp_path)
        assert result == ""

    def test_forbidden_step_implement_returns_empty(self, tmp_path):
        """Forbidden step (implement) returns empty string."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("implement", tmp_path)
        assert result == ""

    def test_forbidden_step_test_returns_empty(self, tmp_path):
        """Forbidden step (test) returns empty string."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("test", tmp_path)
        assert result == ""

    def test_custom_whitelist_from_config(self, tmp_path):
        """Custom se3.yaml whitelist is respected."""
        from .context_builder import get_issue_discovery_injection

        # Create se3.yaml with custom whitelist including 'design'
        config_path = tmp_path / "se3.yaml"
        config_path.write_text(
            "issue_discovery:\n  steps:\n    - design\n    - summarize\n"
        )

        # 'design' should now return injection
        result = get_issue_discovery_injection("design", tmp_path)
        assert result != ""
        assert "discovered_issues" in result

        # 'verify_spec' is no longer in custom whitelist
        result = get_issue_discovery_injection("verify_spec", tmp_path)
        assert result == ""

    def test_forbidden_step_overrides_config(self, tmp_path):
        """Forbidden step returns empty even if config includes it."""
        from .context_builder import get_issue_discovery_injection

        # Create se3.yaml that tries to whitelist forbidden 'implement'
        config_path = tmp_path / "se3.yaml"
        config_path.write_text(
            "issue_discovery:\n  steps:\n    - implement\n    - summarize\n"
        )

        result = get_issue_discovery_injection("implement", tmp_path)
        assert result == ""

    def test_missing_config_uses_defaults(self, tmp_path):
        """Missing se3.yaml uses default whitelist."""
        from .context_builder import get_issue_discovery_injection

        # No se3.yaml exists in tmp_path
        result = get_issue_discovery_injection("summarize", tmp_path)
        assert result != ""
        assert "discovered_issues" in result


class TestSummarizeHandlerIssueDiscoveryIntegration:
    """Integration test: verify summarize handler prompt includes issue discovery."""

    @patch("se3.engine.steps.summarize.LLMCaller")
    def test_summarize_prompt_contains_issue_discovery(self, MockLLMCaller):
        """The actual prompt sent to LLM by summarize handler contains issue discovery text."""
        mock_caller = MagicMock()
        # Return valid NDJSON with summary text
        mock_caller.call.return_value = '{"type": "assistant", "message": {"content": [{"type": "text", "text": "Summary here"}]}}'
        MockLLMCaller.return_value = mock_caller

        flow = FlowInstance(task_description="Test task")
        # Set change_path to a temp dir so project_root resolves
        with tempfile.TemporaryDirectory() as tmpdir:
            flow.change_path = Path(tmpdir) / "dummy"
            step = Step(step_type=StepType.SUMMARIZE)
            step.inputs["task_description"] = "Test task"

            from .steps.summarize import summarize_handler
            summarize_handler(step, flow)

            # Verify the prompt sent to LLM contains issue discovery text
            assert mock_caller.call.called
            call_kwargs = mock_caller.call.call_args
            prompt = call_kwargs.kwargs.get("prompt") or call_kwargs[1].get("prompt") or call_kwargs[0][0] if call_kwargs[0] else ""
            # The prompt keyword argument
            if not prompt and call_kwargs.kwargs:
                prompt = call_kwargs.kwargs.get("prompt", "")
            assert "discovered_issues" in prompt, (
                "Issue discovery injection was NOT found in the summarize handler prompt. "
                "This means the injection is not reaching the LLM."
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
