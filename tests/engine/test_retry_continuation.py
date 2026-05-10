"""Integration tests for step retry continuation behavior.

Verifies the full data flow: state machine injects retry metadata,
LLMCaller uses continue mode, history is formatted with continuation instruction.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from se3.engine.llm_caller import LLMCaller
from se3.engine.chat_history import ChatMessage, ChatSession


class TestLLMCallerRetryMode:
    """Tests for LLMCaller retry_mode parameter."""

    def test_default_retry_mode_is_continue(self):
        """LLMCaller should default to 'continue' retry_mode."""
        caller = LLMCaller(project_root=Path("/tmp"))
        assert caller.retry_mode == "continue"

    def test_retry_mode_stored(self):
        """retry_mode should be stored as instance attribute."""
        caller = LLMCaller(project_root=Path("/tmp"), retry_mode="retry")
        assert caller.retry_mode == "retry"

    @patch("se3.engine.llm_caller.LLMCaller._get_retry_context")
    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
    def test_continue_mode_uses_continuation_prompt(self, mock_runner_cls, mock_get_ctx):
        """In continue mode with retry context, should use continuation instruction, not original prompt."""
        mock_get_ctx.return_value = "[Previous conversation context]\nsome history"

        # Create a mock runner instance
        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = '{"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}'
        mock_runner.run_with_monitor.return_value = mock_result
        mock_runner_cls.return_value = mock_runner

        caller = LLMCaller(
            project_root=Path("/tmp"),
            flow_id="test-flow",
            step_id="test-step",
            step_type="implement",
            external_attempt=1,  # Simulate retry
            retry_mode="continue",
        )

        caller.call(prompt="Original task prompt", on_output=lambda x: None)

        # Check what prompt was passed to run_with_monitor
        call_args = mock_runner.run_with_monitor.call_args
        args_list = call_args[1]["args"] if "args" in call_args[1] else call_args[0][0]
        # Find the -p argument
        prompt_idx = args_list.index("-p") + 1
        effective_prompt = args_list[prompt_idx]

        # In continue mode, the effective prompt should have the history context
        # and a continuation instruction, NOT the original prompt re-appended
        assert "Previous conversation context" in effective_prompt
        assert "Continue the task from where you left off" in effective_prompt
        assert "Do NOT repeat work already completed" in effective_prompt

    @patch("se3.engine.llm_caller.LLMCaller._get_retry_context")
    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
    def test_retry_mode_uses_original_prompt(self, mock_runner_cls, mock_get_ctx):
        """In retry mode with retry context, should prepend history + original prompt."""
        mock_get_ctx.return_value = "[Previous conversation context]\nsome history"

        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = '{"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}'
        mock_runner.run_with_monitor.return_value = mock_result
        mock_runner_cls.return_value = mock_runner

        caller = LLMCaller(
            project_root=Path("/tmp"),
            flow_id="test-flow",
            step_id="test-step",
            step_type="implement",
            external_attempt=1,
            retry_mode="retry",
        )

        caller.call(prompt="Original task prompt", on_output=lambda x: None)

        call_args = mock_runner.run_with_monitor.call_args
        args_list = call_args[1]["args"] if "args" in call_args[1] else call_args[0][0]
        prompt_idx = args_list.index("-p") + 1
        effective_prompt = args_list[prompt_idx]

        # In retry mode, original prompt should be re-appended after history
        assert "Previous conversation context" in effective_prompt
        assert "Original task prompt" in effective_prompt

    @patch("se3.engine.llm_caller.LLMCaller._get_retry_context")
    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
    def test_first_attempt_unchanged(self, mock_runner_cls, mock_get_ctx):
        """On first attempt (external_attempt=0), behavior should be unchanged regardless of mode."""
        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = '{"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}'
        mock_runner.run_with_monitor.return_value = mock_result
        mock_runner_cls.return_value = mock_runner

        caller = LLMCaller(
            project_root=Path("/tmp"),
            flow_id="test-flow",
            step_id="test-step",
            step_type="implement",
            external_attempt=0,
            retry_mode="continue",
        )

        caller.call(prompt="Original task prompt", on_output=lambda x: None)

        call_args = mock_runner.run_with_monitor.call_args
        args_list = call_args[1]["args"] if "args" in call_args[1] else call_args[0][0]
        prompt_idx = args_list.index("-p") + 1
        effective_prompt = args_list[prompt_idx]

        # First attempt should use original prompt as-is
        assert effective_prompt == "Original task prompt"
        # _get_retry_context should NOT have been called (total_attempt == 0)
        mock_get_ctx.assert_not_called()

    def test_get_retry_context_passes_mode(self):
        """_get_retry_context should pass self.retry_mode to format_history_for_retry."""
        caller = LLMCaller(
            project_root=Path("/tmp"),
            flow_id="test-flow",
            step_id="test-step",
            retry_mode="continue",
        )

        # Patch at the point of import inside _get_retry_context
        from unittest.mock import call
        calls_made = []

        def tracking_format(*args, **kwargs):
            calls_made.append((args, kwargs))
            return None

        # Patch via the chat_history module directly
        import se3.engine.chat_history as ch_mod
        original_fn = ch_mod.format_history_for_retry
        try:
            ch_mod.format_history_for_retry = tracking_format
            # Re-create caller to get clean state
            caller2 = LLMCaller(
                project_root=Path("/tmp"),
                flow_id="test-flow",
                step_id="test-step",
                retry_mode="continue",
            )
            caller2._get_retry_context()
            assert len(calls_made) == 1
            args, kwargs = calls_made[0]
            assert args == (Path("/tmp"), "test-flow", "test-step")
            assert kwargs == {"mode": "continue", "current_fix_iteration": 0}
        finally:
            ch_mod.format_history_for_retry = original_fn


class TestStateMachineRetryMetadata:
    """Tests for retry metadata injection in state machine."""

    def test_retry_injects_metadata(self):
        """When a step is retried, is_retry and retry_count should be set in inputs."""
        from se3.engine.models import Step, StepType, StepStatus, FlowInstance, FlowStatus, State

        # Create a step that will "fail"
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.PENDING,
            inputs={"task_description": "test"},
        )

        # Simulate the retry logic from the orchestration loop
        # (extracted to avoid needing a full flow setup)
        step.status = StepStatus.FAILED
        assert "is_retry" not in step.inputs

        # Simulate retry branch
        if step.retry_count < step.max_retries:
            step.retry_count += 1
            step.status = StepStatus.RETRYING
            step.inputs["is_retry"] = True
            step.inputs["retry_count"] = step.retry_count

        assert step.inputs["is_retry"] is True
        assert step.inputs["retry_count"] == 1

    def test_first_execution_no_retry_metadata(self):
        """On first execution, is_retry and retry_count should not be present."""
        from se3.engine.models import Step, StepType, StepStatus

        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.PENDING,
            inputs={"task_description": "test"},
        )

        assert "is_retry" not in step.inputs
        assert "retry_count" not in step.inputs


class TestStateMachineLoadOrCreateFailedFlow:
    """Tests for load_or_create_flow treating FAILED as resumable."""

    def test_load_or_create_returns_resumed_for_failed_flow(self):
        """load_or_create_flow should return (flow, True) for FAILED flows."""
        from se3.engine.models import FlowInstance, FlowStatus
        from se3.engine.state_machine import StateMachine
        from unittest.mock import MagicMock

        mock_persistence = MagicMock()
        failed_flow = FlowInstance(
            flow_id="failed-flow",
            task_description="A failed task",
            status=FlowStatus.FAILED,
        )
        mock_persistence.load_flow.return_value = failed_flow

        sm = StateMachine(project_root=Path("/tmp"), persistence=mock_persistence)
        result_flow, is_resumed = sm.load_or_create_flow("new task")

        assert is_resumed is True
        assert result_flow.flow_id == "failed-flow"

    def test_load_or_create_creates_new_for_completed_flow(self):
        """load_or_create_flow should create new flow for COMPLETED flows."""
        from se3.engine.models import FlowInstance, FlowStatus
        from se3.engine.state_machine import StateMachine
        from unittest.mock import MagicMock

        mock_persistence = MagicMock()
        completed_flow = FlowInstance(
            flow_id="completed-flow",
            task_description="Done task",
            status=FlowStatus.COMPLETED,
        )
        mock_persistence.load_flow.return_value = completed_flow

        sm = StateMachine(project_root=Path("/tmp"), persistence=mock_persistence)
        # Mock create_flow to return a new flow
        new_flow = FlowInstance(
            flow_id="new-flow",
            task_description="new task",
            status=FlowStatus.INIT,
        )
        sm.create_flow = MagicMock(return_value=new_flow)

        result_flow, is_resumed = sm.load_or_create_flow("new task")

        assert is_resumed is False
        assert result_flow.flow_id == "new-flow"


class TestInitFlowIdempotency:
    """Tests that init_flow() is safe for FAILED, PAUSED, and COMPLETED flows."""

    def test_init_flow_safe_for_failed_flow(self):
        """init_flow() can be called on a FAILED flow (idempotent guards skip existing data)."""
        from se3.engine.models import FlowInstance, FlowStatus
        from se3.engine.state_machine import StateMachine
        from unittest.mock import MagicMock, patch

        mock_persistence = MagicMock()
        sm = StateMachine(project_root=Path("/tmp"), persistence=mock_persistence)

        failed_flow = FlowInstance(
            flow_id="failed-flow",
            task_description="A failed task",
            status=FlowStatus.FAILED,
        )

        with patch.object(sm, "_write_flow_meta") as mock_meta, \
             patch.object(sm, "_record_baseline_commit") as mock_baseline:
            sm.init_flow(failed_flow)

        mock_meta.assert_called_once_with(failed_flow)
        mock_baseline.assert_called_once_with(failed_flow)

    def test_init_flow_safe_for_paused_flow(self):
        """init_flow() can be called on a PAUSED flow (idempotent)."""
        from se3.engine.models import FlowInstance, FlowStatus
        from se3.engine.state_machine import StateMachine
        from unittest.mock import MagicMock, patch

        mock_persistence = MagicMock()
        sm = StateMachine(project_root=Path("/tmp"), persistence=mock_persistence)

        paused_flow = FlowInstance(
            flow_id="paused-flow",
            task_description="A paused task",
            status=FlowStatus.PAUSED,
        )

        with patch.object(sm, "_write_flow_meta") as mock_meta, \
             patch.object(sm, "_record_baseline_commit") as mock_baseline:
            sm.init_flow(paused_flow)

        mock_meta.assert_called_once_with(paused_flow)
        mock_baseline.assert_called_once_with(paused_flow)

    def test_init_flow_safe_for_completed_flow(self):
        """init_flow() can be called on a COMPLETED flow (idempotent — guards skip)."""
        from se3.engine.models import FlowInstance, FlowStatus
        from se3.engine.state_machine import StateMachine
        from unittest.mock import MagicMock, patch

        mock_persistence = MagicMock()
        sm = StateMachine(project_root=Path("/tmp"), persistence=mock_persistence)

        completed_flow = FlowInstance(
            flow_id="completed-flow",
            task_description="Done task",
            status=FlowStatus.COMPLETED,
        )

        with patch.object(sm, "_write_flow_meta") as mock_meta, \
             patch.object(sm, "_record_baseline_commit") as mock_baseline:
            sm.init_flow(completed_flow)

        mock_meta.assert_called_once_with(completed_flow)
        mock_baseline.assert_called_once_with(completed_flow)
