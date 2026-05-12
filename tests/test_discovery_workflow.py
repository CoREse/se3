"""Tests for discovery workflow functionality."""

import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
    get_default_step_sequence,
)
from se3.engine.state_machine import StateMachine
from se3.engine.steps.discovery import (
    discovery_handler,
    PROGRAMMATIC_CONFIRM_SENTINEL,
    _format_conversation_history,
    _generate_summary,
    _extract_narrative_from_raw,
    _display_discovery_message,
    INITIAL_DISCOVERY_PROMPT,
    CONTINUE_DISCOVERY_PROMPT,
)


class TestDiscoveryStepType:
    """Test that DISCOVERY step type exists."""

    def test_discovery_step_type_exists(self):
        """DISCOVERY should be a valid StepType."""
        assert StepType.DISCOVERY.value == "discovery"

    def test_discovery_in_step_pool(self):
        """DISCOVERY should be in the step pool."""
        from se3.engine.models import STEP_POOL

        assert StepType.DISCOVERY in STEP_POOL
        info = STEP_POOL[StepType.DISCOVERY]
        assert info["name"] == "discovery"
        assert info["uses_llm"] is True


class TestDiscoveryStepSequence:
    """Test discovery task type step sequence."""

    def test_discovery_sequence_starts_with_discovery(self):
        """Discovery task type should start with DISCOVERY step."""
        sequence = get_default_step_sequence("discovery")
        assert sequence[0] == StepType.DISCOVERY
        assert sequence[1] == StepType.ANALYZE

    def test_discovery_sequence_length(self):
        """Discovery sequence should include all standard steps (summarize now optional)."""
        sequence = get_default_step_sequence("discovery")
        # discovery + analyze + plan + implement + test + verify_spec + update_spec + version_analyze + commit
        assert len(sequence) >= 9


class TestDiscoveryHandler:
    """Test discovery step handler."""

    def test_discovery_handler_requires_task_description(self):
        """Handler should fail without task description."""
        step = Step(step_type=StepType.DISCOVERY, inputs={})
        flow = FlowInstance(task_description="")

        result = discovery_handler(step, flow)

        assert result == StepStatus.FAILED
        assert "No initial description" in step.error_message

    @patch("se3.engine.steps.discovery.LLMCaller")
    def test_discovery_initial_round_asks_questions(self, mock_caller_class):
        """Initial discovery round should ask questions."""
        # Setup mock
        mock_caller = Mock()
        mock_caller_class.return_value = mock_caller
        mock_caller.call.return_value = json.dumps({
            "mode": "question",
            "content": "What problem are you trying to solve?",
            "questions": ["Who is the target user?", "What are the key features?"],
            "thinking": "Need to understand the scope",
        })

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={"task_description": "I want to build a feature"},
        )
        flow = FlowInstance(task_description="I want to build a feature")

        result = discovery_handler(step, flow)

        assert result == StepStatus.PAUSED
        # Internal state stored in discovery_state
        assert step.inputs["discovery_state"]["mode"] == "question"
        assert step.inputs["discovery_state"]["round"] == 1
        # User-facing outputs
        assert "message" in step.outputs
        assert "questions" in step.outputs
        assert step.outputs["questions"] == ["Who is the target user?", "What are the key features?"]

    @patch("se3.engine.steps.discovery.LLMCaller")
    def test_discovery_synthesis_needs_confirmation(self, mock_caller_class):
        """Synthesis mode should pause for user confirmation."""
        mock_caller = Mock()
        mock_caller_class.return_value = mock_caller
        mock_caller.call.return_value = json.dumps({
            "mode": "synthesis",
            "content": "Here's what I understand...",
            "refined_description": "Build a user authentication system with login/logout",
            "thinking": "Have enough information",
        })

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={"task_description": "I want auth"},
        )
        flow = FlowInstance(task_description="I want auth")

        result = discovery_handler(step, flow)

        assert result == StepStatus.PAUSED
        # Synthesis with refined description and no questions routes through
        # the programmatic confirmation gate (same as confirmation mode).
        assert "message" in step.outputs
        assert "refined_description" in step.outputs
        assert step.outputs["refined_description"] == "Build a user authentication system with login/logout"
        assert step.outputs.get("awaiting_programmatic_confirm") is True
        # Internal state stored in discovery_state
        assert step.inputs["discovery_state"]["mode"] == "synthesis"

    @patch("se3.engine.steps.discovery.LLMCaller")
    def test_discovery_confirmation_pauses_for_programmatic_confirm(self, mock_caller_class):
        """Confirmation with ready_to_proceed should PAUSE for programmatic confirmation."""
        mock_caller = Mock()
        mock_caller_class.return_value = mock_caller
        mock_caller.call.return_value = json.dumps({
            "mode": "confirmation",
            "content": "Confirmed! Let's proceed.",
            "refined_description": "Build a user authentication system",
            "ready_to_proceed": True,
            "thinking": "User confirmed",
        })

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={
                "task_description": "I want auth",
                "discovery_state": {"round": 2, "history": [
                    {"role": "assistant", "content": "What do you need?", "round": 0},
                    {"role": "user", "content": "yes, proceed", "round": 1},
                ]},
            },
        )
        flow = FlowInstance(task_description="I want auth")

        result = discovery_handler(step, flow)

        assert result == StepStatus.PAUSED
        assert step.outputs["awaiting_programmatic_confirm"] is True
        assert step.outputs["refined_description"] == "Build a user authentication system"

    @patch("se3.engine.steps.discovery.LLMCaller")
    def test_discovery_high_round_with_user_confirmation(self, mock_caller_class):
        """Should pause for programmatic confirm at any round number."""
        mock_caller = Mock()
        mock_caller_class.return_value = mock_caller
        mock_caller.call.return_value = json.dumps({
            "mode": "confirmation",
            "content": "Confirmed at final round!",
            "refined_description": "Final round refined description",
            "ready_to_proceed": True,
            "thinking": "User confirmed",
        })

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={
                "task_description": "Initial idea",
                "discovery_state": {"round": 10, "history": [
                    {"role": "assistant", "content": "Please confirm"},
                ]},
                "resumed": True,
                "user_response": "yes",  # User confirming at max rounds
            },
        )
        flow = FlowInstance(task_description="Initial idea")

        result = discovery_handler(step, flow)

        # Should pause for programmatic confirmation, not directly complete
        assert result == StepStatus.PAUSED
        assert step.outputs["awaiting_programmatic_confirm"] is True
        assert step.outputs["refined_description"] == "Final round refined description"


class TestProgrammaticConfirmation:
    """Test programmatic confirmation gate in discovery handler."""

    def test_discovery_programmatic_confirm_completes(self):
        """When programmatic_confirmed=True, handler should complete without LLM."""
        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={
                "task_description": "I want auth",
                "programmatic_confirmed": True,
                "discovery_state": {"round": 3, "history": [
                    {"role": "assistant", "content": "What do you need?", "round": 0},
                    {"role": "user", "content": "Auth system", "round": 1},
                    {"role": "assistant", "content": "Confirmed", "round": 2},
                ]},
            },
        )
        # Pre-populate outputs as they would be after the PAUSED state
        step.outputs["refined_description"] = "Build a user authentication system"
        step.outputs["awaiting_programmatic_confirm"] = True

        flow = FlowInstance(task_description="I want auth")

        # No LLMCaller mock needed — should not call LLM at all
        result = discovery_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["refined_description"] == "Build a user authentication system"
        assert step.outputs["requirements_clarified"] is True
        assert "discovery_summary" in step.outputs
        # awaiting flag should be cleared
        assert "awaiting_programmatic_confirm" not in step.outputs

    @patch("se3.engine.steps.discovery.LLMCaller")
    def test_discovery_programmatic_confirm_continue(self, mock_caller_class):
        """When awaiting flag cleared and new user_response given, should continue discovery."""
        mock_caller = Mock()
        mock_caller_class.return_value = mock_caller
        mock_caller.call.return_value = json.dumps({
            "mode": "question",
            "content": "What specific aspects need clarification?",
            "questions": ["What auth method?"],
            "thinking": "User wants to continue",
        })

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={
                "task_description": "I want auth",
                "discovery_state": {"round": 3, "history": [
                    {"role": "assistant", "content": "What do you need?", "round": 0},
                    {"role": "user", "content": "Auth system", "round": 1},
                ]},
                "resumed": True,
                "user_response": "Actually, I also need OAuth support",
            },
        )
        # Outputs should NOT have awaiting flag (it was cleared by run loop)
        step.outputs["message"] = "Previous message"

        flow = FlowInstance(task_description="I want auth")

        result = discovery_handler(step, flow)

        assert result == StepStatus.PAUSED
        # LLM should have been called
        mock_caller.call.assert_called_once()
        # Should not have awaiting flag
        assert "awaiting_programmatic_confirm" not in step.outputs


class TestProgrammaticConfirmGate:
    """Test _handle_discovery_programmatic_confirm in run.py.

    These tests drive every branch of the programmatic confirmation gate
    by mocking _read_multiline_input.
    """

    @patch("se3.commands.run._read_multiline_input")
    def test_input_1_confirms(self, mock_read):
        """Strict '1' confirms and returns sentinel."""
        from se3.commands.run import _handle_discovery_programmatic_confirm

        step = Step(step_type=StepType.DISCOVERY, inputs={})
        step.outputs["message"] = "msg"
        step.outputs["refined_description"] = "desc"
        flow = FlowInstance(task_description="test")
        persistence = Mock()

        mock_read.return_value = "1"

        result = _handle_discovery_programmatic_confirm(flow, step, persistence)

        assert result == PROGRAMMATIC_CONFIRM_SENTINEL
        assert step.inputs["programmatic_confirmed"] is True

    @patch("se3.commands.run._read_multiline_input")
    def test_input_1_with_newline_confirms(self, mock_read):
        """'1\\n' confirms (trailing newline artifact from multiline UI)."""
        from se3.commands.run import _handle_discovery_programmatic_confirm

        step = Step(step_type=StepType.DISCOVERY, inputs={})
        step.outputs["message"] = "msg"
        step.outputs["refined_description"] = "desc"
        flow = FlowInstance(task_description="test")
        persistence = Mock()

        mock_read.return_value = "1\n"

        result = _handle_discovery_programmatic_confirm(flow, step, persistence)

        assert result == PROGRAMMATIC_CONFIRM_SENTINEL
        assert step.inputs["programmatic_confirmed"] is True

    @patch("se3.commands.run._read_multiline_input")
    def test_input_1_with_crlf_confirms(self, mock_read):
        """'1\\r\\n' confirms (trailing CRLF artifact from multiline UI)."""
        from se3.commands.run import _handle_discovery_programmatic_confirm

        step = Step(step_type=StepType.DISCOVERY, inputs={})
        step.outputs["message"] = "msg"
        step.outputs["refined_description"] = "desc"
        flow = FlowInstance(task_description="test")
        persistence = Mock()

        mock_read.return_value = "1\r\n"

        result = _handle_discovery_programmatic_confirm(flow, step, persistence)

        assert result == PROGRAMMATIC_CONFIRM_SENTINEL
        assert step.inputs["programmatic_confirmed"] is True

    @patch("se3.commands.run._read_multiline_input")
    def test_input_1_dot_rejected(self, mock_read):
        """'1.' is not strict '1' — clears flag and returns input for continued discovery."""
        from se3.commands.run import _handle_discovery_programmatic_confirm

        step = Step(step_type=StepType.DISCOVERY, inputs={})
        step.outputs["message"] = "msg"
        step.outputs["refined_description"] = "desc"
        step.outputs["awaiting_programmatic_confirm"] = True
        flow = FlowInstance(task_description="test")
        persistence = Mock()

        mock_read.return_value = "1."

        result = _handle_discovery_programmatic_confirm(flow, step, persistence)

        assert result == "1."
        assert "awaiting_programmatic_confirm" not in step.outputs
        assert "programmatic_confirmed" not in step.inputs

    @patch("se3.commands.run._read_multiline_input")
    def test_input_whitespace_1_rejected(self, mock_read):
        """' 1 ' is not strict '1' — clears flag and returns input."""
        from se3.commands.run import _handle_discovery_programmatic_confirm

        step = Step(step_type=StepType.DISCOVERY, inputs={})
        step.outputs["message"] = "msg"
        step.outputs["refined_description"] = "desc"
        step.outputs["awaiting_programmatic_confirm"] = True
        flow = FlowInstance(task_description="test")
        persistence = Mock()

        mock_read.return_value = " 1 "

        result = _handle_discovery_programmatic_confirm(flow, step, persistence)

        assert result == " 1 "
        assert "awaiting_programmatic_confirm" not in step.outputs

    @patch("se3.commands.run._read_multiline_input")
    def test_input_yes_rejected(self, mock_read):
        """'yes' is not strict '1' — clears flag and returns input."""
        from se3.commands.run import _handle_discovery_programmatic_confirm

        step = Step(step_type=StepType.DISCOVERY, inputs={})
        step.outputs["message"] = "msg"
        step.outputs["refined_description"] = "desc"
        step.outputs["awaiting_programmatic_confirm"] = True
        flow = FlowInstance(task_description="test")
        persistence = Mock()

        mock_read.return_value = "yes"

        result = _handle_discovery_programmatic_confirm(flow, step, persistence)

        assert result == "yes"
        assert "awaiting_programmatic_confirm" not in step.outputs

    @patch("se3.commands.run._read_multiline_input")
    def test_input_foo_rejected(self, mock_read):
        """'foo' clears flag and returns input for continued discovery."""
        from se3.commands.run import _handle_discovery_programmatic_confirm

        step = Step(step_type=StepType.DISCOVERY, inputs={})
        step.outputs["message"] = "msg"
        step.outputs["refined_description"] = "desc"
        step.outputs["awaiting_programmatic_confirm"] = True
        flow = FlowInstance(task_description="test")
        persistence = Mock()

        mock_read.return_value = "foo"

        result = _handle_discovery_programmatic_confirm(flow, step, persistence)

        assert result == "foo"
        assert "awaiting_programmatic_confirm" not in step.outputs

    @patch("se3.engine.steps.discovery._display_discovery_message")
    @patch("se3.commands.run._read_multiline_input")
    def test_empty_input_redisplays(self, mock_read, mock_display):
        """Empty input loops with re-display, then '1' breaks the loop."""
        from se3.commands.run import _handle_discovery_programmatic_confirm

        step = Step(step_type=StepType.DISCOVERY, inputs={})
        step.outputs["message"] = "msg"
        step.outputs["refined_description"] = "desc"
        flow = FlowInstance(task_description="test")
        persistence = Mock()

        mock_read.side_effect = ["", "1"]

        result = _handle_discovery_programmatic_confirm(flow, step, persistence)

        assert result == PROGRAMMATIC_CONFIRM_SENTINEL
        assert mock_display.call_count == 1  # re-displayed after empty input
        assert mock_read.call_count == 2

    @patch("se3.engine.steps.discovery._display_discovery_message")
    @patch("se3.commands.run._read_multiline_input")
    def test_whitespace_only_redisplays(self, mock_read, mock_display):
        """Whitespace-only input loops with re-display, then '1' breaks."""
        from se3.commands.run import _handle_discovery_programmatic_confirm

        step = Step(step_type=StepType.DISCOVERY, inputs={})
        step.outputs["message"] = "msg"
        step.outputs["refined_description"] = "desc"
        flow = FlowInstance(task_description="test")
        persistence = Mock()

        mock_read.side_effect = ["   ", "1"]

        result = _handle_discovery_programmatic_confirm(flow, step, persistence)

        assert result == PROGRAMMATIC_CONFIRM_SENTINEL
        assert mock_display.call_count == 1
        assert mock_read.call_count == 2

    @patch("se3.commands.run._read_multiline_input")
    def test_none_cancels_and_saves(self, mock_read):
        """None (Ctrl+C / EOF) returns None after saving flow."""
        from se3.commands.run import _handle_discovery_programmatic_confirm

        step = Step(step_type=StepType.DISCOVERY, inputs={})
        step.outputs["message"] = "msg"
        step.outputs["refined_description"] = "desc"
        flow = FlowInstance(task_description="test")
        persistence = Mock()

        mock_read.return_value = None

        result = _handle_discovery_programmatic_confirm(flow, step, persistence)

        assert result is None
        persistence.save_flow.assert_called_once_with(flow)


class TestSentinelAssertion:
    """Test that the sentinel never leaks into the LLM path."""

    def test_sentinel_without_programmatic_confirmed_fails(self):
        """If user_response contains the sentinel without programmatic_confirmed,
        the handler must fail rather than feed it to the LLM."""
        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={
                "task_description": "Test task",
                "user_response": PROGRAMMATIC_CONFIRM_SENTINEL,
                "discovery_state": {"round": 1, "history": []},
            },
        )
        flow = FlowInstance(task_description="Test task")

        result = discovery_handler(step, flow)

        assert result == StepStatus.FAILED
        assert "sentinel" in step.error_message.lower()
        assert PROGRAMMATIC_CONFIRM_SENTINEL in step.error_message


class TestStateMachineIntegration:
    """Test discovery integration with state machine."""

    def test_create_flow_with_discovery_type(self, tmp_path):
        """State machine should create flow with discovery steps."""
        from se3.engine.persistence import PersistenceManager

        persistence = PersistenceManager(tmp_path)
        state_machine = StateMachine(tmp_path, persistence)

        flow = state_machine.create_flow(
            task_description="Test discovery",
            task_type="discovery",
        )

        assert flow.task_type == "discovery"
        assert flow.state.selected_steps[0] == StepType.DISCOVERY

        first_step = flow.state.get_current_step()
        assert first_step.inputs.get("discovery_mode") is True

    def test_discovery_outputs_passed_to_analyze(self, tmp_path):
        """Discovery outputs should be available to analyze step."""
        from se3.engine.persistence import PersistenceManager

        persistence = PersistenceManager(tmp_path)
        state_machine = StateMachine(tmp_path, persistence)

        # Create flow with discovery
        flow = state_machine.create_flow(
            task_description="Test discovery",
            task_type="discovery",
        )

        # Simulate completed discovery step
        discovery_step = Step(
            step_type=StepType.DISCOVERY,
            status=StepStatus.COMPLETED,
            outputs={
                "refined_description": "Refined task",
                "discovery_summary": "Summary",
            },
        )
        flow.state.add_step(discovery_step)

        # Build inputs for next step (analyze)
        inputs = state_machine._build_step_inputs(flow, StepType.ANALYZE)

        assert inputs.get("refined_description") == "Refined task"
        assert inputs.get("discovery_summary") == "Summary"


class TestConversationHistory:
    """Test conversation history formatting."""

    def test_format_empty_history(self):
        """Empty history should have placeholder."""
        result = _format_conversation_history([])
        assert result == "(No conversation yet)"

    def test_format_history_with_entries(self):
        """Should format conversation entries."""
        history = [
            {"role": "user", "content": "I want a feature"},
            {"role": "assistant", "content": "What kind?"},
        ]
        result = _format_conversation_history(history)
        assert "USER: I want a feature" in result
        assert "ASSISTANT: What kind?" in result

    def test_generate_summary(self):
        """Summary should count rounds and inputs."""
        history = [
            {"role": "user", "content": "Idea 1"},
            {"role": "assistant", "content": "Q1"},
            {"role": "user", "content": "Answer 1"},
            {"role": "assistant", "content": "Q2"},
        ]
        result = _generate_summary(history)
        assert "2 rounds" in result
        assert "2 user inputs" in result


class TestDiscoveryPromptTemplates:
    """Test that discovery prompt templates enforce responsibility boundaries."""

    def test_initial_prompt_no_full_tool_access(self):
        """INITIAL_DISCOVERY_PROMPT must not contain 'full tool access'."""
        assert "full tool access" not in INITIAL_DISCOVERY_PROMPT

    def test_continue_prompt_no_full_tool_access(self):
        """CONTINUE_DISCOVERY_PROMPT must not contain 'full tool access'."""
        assert "full tool access" not in CONTINUE_DISCOVERY_PROMPT

    def test_initial_prompt_declares_sole_responsibility(self):
        """INITIAL_DISCOVERY_PROMPT must declare the sole output is Proposed Task Description."""
        assert "Proposed Task Description" in INITIAL_DISCOVERY_PROMPT
        assert "ONLY job" in INITIAL_DISCOVERY_PROMPT or "ONLY" in INITIAL_DISCOVERY_PROMPT

    def test_continue_prompt_declares_sole_responsibility(self):
        """CONTINUE_DISCOVERY_PROMPT must declare the sole output is Proposed Task Description."""
        assert "Proposed Task Description" in CONTINUE_DISCOVERY_PROMPT
        assert "ONLY job" in CONTINUE_DISCOVERY_PROMPT or "ONLY" in CONTINUE_DISCOVERY_PROMPT

    def test_initial_prompt_forbids_overreach(self):
        """INITIAL_DISCOVERY_PROMPT must forbid implementation plans, code, and file modifications."""
        assert "MUST NOT" in INITIAL_DISCOVERY_PROMPT
        assert "implementation plan" in INITIAL_DISCOVERY_PROMPT.lower() or "implementation" in INITIAL_DISCOVERY_PROMPT.lower()
        assert "code" in INITIAL_DISCOVERY_PROMPT.lower()
        assert "Modify any files" in INITIAL_DISCOVERY_PROMPT or "modify" in INITIAL_DISCOVERY_PROMPT.lower()

    def test_continue_prompt_forbids_overreach(self):
        """CONTINUE_DISCOVERY_PROMPT must forbid implementation plans, code, and file modifications."""
        assert "MUST NOT" in CONTINUE_DISCOVERY_PROMPT
        assert "implementation plan" in CONTINUE_DISCOVERY_PROMPT.lower() or "implementation" in CONTINUE_DISCOVERY_PROMPT.lower()
        assert "code" in CONTINUE_DISCOVERY_PROMPT.lower()
        assert "Modify any files" in CONTINUE_DISCOVERY_PROMPT or "modify" in CONTINUE_DISCOVERY_PROMPT.lower()

    def test_initial_prompt_allows_reading(self):
        """INITIAL_DISCOVERY_PROMPT must allow reading spec files and source code."""
        assert "read" in INITIAL_DISCOVERY_PROMPT.lower()
        assert "se3/specs/" in INITIAL_DISCOVERY_PROMPT

    def test_continue_prompt_allows_reading(self):
        """CONTINUE_DISCOVERY_PROMPT must allow reading spec files and source code."""
        assert "read" in CONTINUE_DISCOVERY_PROMPT.lower()
        assert "se3/specs/" in CONTINUE_DISCOVERY_PROMPT


class TestDiscoveryLLMCallErrorHandling:
    """Test that LLMCallError is caught with friendly messages."""

    @patch("se3.engine.output.render_full")
    @patch("se3.engine.steps.discovery.LLMCaller")
    def test_discovery_llm_json_extraction_failure(self, mock_caller_class, mock_render):
        """JSON extraction failure should return FAILED with friendly error message and panel."""
        from se3.engine.llm_caller import LLMCallError

        mock_caller = Mock()
        mock_caller_class.return_value = mock_caller
        mock_caller.call.side_effect = LLMCallError(
            "Two-phase JSON extraction failed: no valid JSON found in response"
        )

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={"task_description": "I want to build something"},
        )
        flow = FlowInstance(task_description="I want to build something")

        result = discovery_handler(step, flow)

        assert result == StepStatus.FAILED
        assert "JSON" in step.error_message
        assert "Two-phase" not in step.error_message
        assert "traceback" not in step.error_message.lower()
        # Verify render_full was called to show friendly panel
        mock_render.assert_called_once()
        rendered_text = mock_render.call_args[0][0]
        assert "JSON" in rendered_text

    @patch("se3.engine.output.render_full")
    @patch("se3.engine.steps.discovery.LLMCaller")
    def test_discovery_other_llm_error(self, mock_caller_class, mock_render):
        """Other LLMCallError should return FAILED with concise error description and panel."""
        from se3.engine.llm_caller import LLMCallError

        mock_caller = Mock()
        mock_caller_class.return_value = mock_caller
        mock_caller.call.side_effect = LLMCallError("API timeout after 60s")

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={"task_description": "I want to build something"},
        )
        flow = FlowInstance(task_description="I want to build something")

        result = discovery_handler(step, flow)

        assert result == StepStatus.FAILED
        assert "API timeout" in step.error_message
        assert step.error_message.startswith("LLM 调用失败")
        # Verify render_full was called to show friendly panel
        mock_render.assert_called_once()

    @patch("se3.engine.steps.discovery.LLMCaller")
    def test_discovery_non_llm_error(self, mock_caller_class):
        """Non-LLMCallError exceptions should still go through generic except path."""
        mock_caller = Mock()
        mock_caller_class.return_value = mock_caller
        mock_caller.call.side_effect = RuntimeError("unexpected internal error")

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={"task_description": "I want to build something"},
        )
        flow = FlowInstance(task_description="I want to build something")

        result = discovery_handler(step, flow)

        assert result == StepStatus.FAILED
        assert step.error_message.startswith("Discovery failed:")
        assert "unexpected internal error" in step.error_message


class TestDiscoveryEmptyResponseRejection:
    """LLM responses with no user-visible fields must be rejected, not rendered as a blank panel."""

    @patch("se3.engine.output.render_full")
    @patch("se3.engine.steps.discovery.LLMCaller")
    def test_all_empty_fields_raise_llm_error(self, mock_caller_class, mock_render):
        """content=='' AND refined_description=='' AND questions==[] → LLMCallError → FAILED with JSON-style friendly message."""
        mock_caller = Mock()
        mock_caller_class.return_value = mock_caller
        mock_caller.call.return_value = json.dumps({
            "mode": "question",
            "content": "",
            "refined_description": "",
            "questions": [],
        })

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={"task_description": "I want something"},
        )
        flow = FlowInstance(task_description="I want something")

        result = discovery_handler(step, flow)

        assert result == StepStatus.FAILED
        # Friendly JSON-extraction-style message (our new LLMCallError has the
        # word "empty" in it, which flows through the LLMCallError branch).
        assert step.error_message  # non-empty
        mock_render.assert_called_once()

    @patch("se3.engine.steps.discovery.LLMCaller")
    def test_content_only_is_accepted(self, mock_caller_class):
        """content non-empty with empty refined/questions is still a valid response."""
        mock_caller = Mock()
        mock_caller_class.return_value = mock_caller
        mock_caller.call.return_value = json.dumps({
            "mode": "question",
            "content": "Let me clarify something first.",
            "refined_description": "",
            "questions": [],
        })

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={"task_description": "I want something"},
        )
        flow = FlowInstance(task_description="I want something")

        with patch("se3.engine.steps.discovery._display_discovery_message"):
            result = discovery_handler(step, flow)

        assert result == StepStatus.PAUSED
        assert step.outputs.get("message") == "Let me clarify something first."

    @patch("se3.engine.steps.discovery.LLMCaller")
    def test_questions_only_is_accepted(self, mock_caller_class):
        """Empty content but with questions is a valid response."""
        mock_caller = Mock()
        mock_caller_class.return_value = mock_caller
        mock_caller.call.return_value = json.dumps({
            "mode": "question",
            "content": "",
            "refined_description": "",
            "questions": ["What platform?", "Which language?"],
        })

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={"task_description": "I want something"},
        )
        flow = FlowInstance(task_description="I want something")

        with patch("se3.engine.steps.discovery._display_discovery_message"):
            result = discovery_handler(step, flow)

        assert result == StepStatus.PAUSED
        assert step.outputs.get("questions") == ["What platform?", "Which language?"]


class TestExtractionPromptSynthesis:
    """EXTRACTION_PROMPT must frame Phase 2 as a re-structuring task over raw
    content (not conditional "extract if JSON else synthesize" logic), since
    by the time Phase 2 runs, Phase 1's JSON has already been judged unusable."""

    def test_prompt_is_structuring_task_not_extraction(self):
        """Prompt must NOT instruct the LLM to copy/prefer embedded JSON when
        present — that's exactly the behavior that fails on thin-but-valid
        Phase 1 JSON."""
        from se3.engine.json_extractor import EXTRACTION_PROMPT

        rendered = EXTRACTION_PROMPT.format(content="x", schema_hint="y").lower()
        # Anti-pattern: prompt must not say "use that JSON" / "preferred source".
        # These were the phrases that caused thin JSON to be copied verbatim.
        assert "use that json" not in rendered
        assert "preferred source" not in rendered

    def test_prompt_requires_schema_complete_output(self):
        """Prompt must explicitly ask for schema-complete output, not just "some JSON"."""
        from se3.engine.json_extractor import EXTRACTION_PROMPT

        rendered = EXTRACTION_PROMPT.format(content="x", schema_hint="y").lower()
        assert "schema-complete" in rendered or "matches the expected schema" in rendered

    def test_prompt_forbids_empty_object(self):
        from se3.engine.json_extractor import EXTRACTION_PROMPT

        rendered = EXTRACTION_PROMPT.format(content="x", schema_hint="y").lower()
        assert "empty object" in rendered or "{}" in rendered

    def test_prompt_renders_without_format_errors(self):
        """Sanity: prompt template must .format() with both placeholders without KeyError."""
        from se3.engine.json_extractor import EXTRACTION_PROMPT

        # Should not raise KeyError or IndexError.
        out = EXTRACTION_PROMPT.format(content="x", schema_hint="y")
        assert "x" in out
        assert "y" in out


class TestRestoreDiscoveryDisplay:
    """Test _restore_discovery_display re-renders discovery content correctly on resume."""

    @patch("se3.engine.steps.discovery._display_discovery_message")
    def test_restore_confirmation_mode_passes_is_confirmation(self, mock_display):
        """On resume from confirmation phase, is_confirmation=True must be passed."""
        from se3.commands.run import _restore_discovery_display

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={
                "task_description": "I want auth",
                "discovery_state": {
                    "round": 3,
                    "history": [
                        {"role": "assistant", "content": '{"mode":"confirmation"}', "round": 2},
                    ],
                },
            },
        )
        step.outputs["message"] = "Confirmed! Let's proceed."
        step.outputs["refined_description"] = "Build a user authentication system"
        step.outputs["awaiting_programmatic_confirm"] = True

        _restore_discovery_display(step)

        mock_display.assert_called_once()
        args, kwargs = mock_display.call_args
        assert kwargs.get("is_confirmation") is True
        assert args[1] == "Build a user authentication system"  # refined_description
        assert args[2] is None  # questions

    @patch("se3.engine.steps.discovery._display_discovery_message")
    def test_restore_synthesis_mode_shows_proposed_description(self, mock_display):
        """On resume from synthesis phase, proposed_description must be displayed."""
        from se3.commands.run import _restore_discovery_display

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={
                "task_description": "I want auth",
                "discovery_state": {
                    "round": 2,
                    "history": [
                        {"role": "assistant", "content": '{"mode":"synthesis"}', "round": 1},
                    ],
                },
            },
        )
        step.outputs["message"] = "Here's what I understand..."
        step.outputs["proposed_description"] = "Build a user authentication system"

        _restore_discovery_display(step)

        mock_display.assert_called_once()
        args, kwargs = mock_display.call_args
        assert kwargs.get("is_confirmation") is False
        assert args[1] == "Build a user authentication system"

    @patch("se3.engine.steps.discovery._display_discovery_message")
    def test_restore_question_mode_shows_questions(self, mock_display):
        """On resume from question phase, questions must be displayed."""
        from se3.commands.run import _restore_discovery_display

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={
                "task_description": "I want auth",
                "discovery_state": {
                    "round": 1,
                    "history": [
                        {"role": "assistant", "content": '{"mode":"question"}', "round": 0},
                    ],
                },
            },
        )
        step.outputs["message"] = "What problem are you trying to solve?"
        step.outputs["questions"] = ["Who is the target user?", "What are the key features?"]

        _restore_discovery_display(step)

        mock_display.assert_called_once()
        args, kwargs = mock_display.call_args
        assert args[2] == ["Who is the target user?", "What are the key features?"]
        assert kwargs.get("is_confirmation") is False

    @patch("se3.engine.steps.discovery._display_discovery_message")
    def test_restore_falls_back_to_history_content_when_no_message(self, mock_display):
        """When message is not in outputs, fall back to last assistant history content."""
        from se3.commands.run import _restore_discovery_display

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={
                "task_description": "I want auth",
                "discovery_state": {
                    "round": 1,
                    "history": [
                        {"role": "assistant", "content": "Fallback content", "round": 0},
                    ],
                },
            },
        )
        step.outputs["proposed_description"] = "Proposed desc"

        _restore_discovery_display(step)

        mock_display.assert_called_once()
        args, kwargs = mock_display.call_args
        assert args[0] == "Fallback content"

    @patch("se3.commands.run.get_console")
    def test_restore_no_history_shows_generic_notice(self, mock_console):
        """When no assistant history exists, show generic resume notice."""
        from se3.commands.run import _restore_discovery_display

        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={
                "task_description": "I want auth",
                "discovery_state": {
                    "round": 0,
                    "history": [],
                },
            },
        )

        _restore_discovery_display(step)

        mock_console.return_value.print.assert_called_once()
        assert "Resuming discovery" in mock_console.return_value.print.call_args[0][0]


class TestExtractNarrativeFromRaw:
    """Test narrative extraction from raw LLM responses."""

    def test_fenced_json_block_removed(self):
        """Text outside ```json block should be extracted."""
        raw = "Here's some analysis.\n\n```json\n{\"mode\": \"question\", \"content\": \"hi\"}\n```\n\nMore text."
        result = _extract_narrative_from_raw(raw)
        assert "Here's some analysis." in result
        assert "More text." in result
        assert "```json" not in result
        assert "mode" not in result

    def test_bare_fenced_json_removed(self):
        """JSON in ``` block (without json tag) should also be removed."""
        raw = "Before.\n```\n{\"a\": 1}\n```\nAfter."
        result = _extract_narrative_from_raw(raw)
        assert "Before." in result
        assert "After." in result
        assert "```" not in result
        assert "a" not in result

    def test_pure_json_returns_empty(self):
        """Pure JSON with no narrative returns empty string."""
        raw = '{"mode": "question", "content": "hello"}'
        result = _extract_narrative_from_raw(raw)
        assert result == ""

    def test_pure_text_with_no_json_returned_as_is(self):
        """Text with no JSON blocks is returned as-is."""
        raw = "Just some narrative text without any code blocks."
        result = _extract_narrative_from_raw(raw)
        assert result == raw

    def test_multiple_json_blocks(self):
        """Multiple JSON code blocks should all be stripped."""
        raw = "Intro.\n```json\n{\"a\": 1}\n```\nMiddle.\n```json\n{\"b\": 2}\n```\nOutro."
        result = _extract_narrative_from_raw(raw)
        assert "Intro." in result
        assert "Middle." in result
        assert "Outro." in result
        assert "```json" not in result
        assert "a" not in result
        assert "b" not in result

    def test_non_json_code_block_preserved(self):
        """Fenced code blocks that are NOT valid JSON should be preserved."""
        raw = "Here's some code:\n```python\nprint('hello')\n```\nEnd."
        result = _extract_narrative_from_raw(raw)
        assert "Here's some code:" in result
        assert "```python" in result
        assert "print('hello')" in result
        assert "End." in result

    def test_none_returns_empty(self):
        """None input returns empty string."""
        assert _extract_narrative_from_raw(None) == ""

    def test_empty_string_returns_empty(self):
        """Empty string returns empty string."""
        assert _extract_narrative_from_raw("") == ""

    def test_fenced_json_with_unescaped_ascii_quotes_stripped(self):
        """Bug repro: fenced JSON with unescaped ASCII quotes inside values.

        Strict json.loads fails on unescaped interior quotes, but the
        lenient repair chain used by parse_json_response can recover it.
        The narrative extractor must use the same lenient detection so
        the block is stripped and does NOT appear alongside formatted content.
        """
        raw = 'Here is my analysis.\n\n```json\n{"content": "是否重写"discovery"步骤"}\n```'
        result = _extract_narrative_from_raw(raw)
        # Narrative should only contain the real non-JSON text
        assert "Here is my analysis." in result
        # The fenced JSON body must NOT appear in narrative
        assert "```json" not in result
        assert '"discovery"' not in result
        assert "是否重写" not in result
        assert result.strip() == "Here is my analysis."

    def test_multiple_fenced_blocks_one_with_unescaped_quotes(self):
        """Multiple fenced blocks: one strict-valid, one lenient-only.

        Both must be recognized as JSON and stripped from narrative.
        """
        raw = (
            "Intro text.\n\n"
            "```json\n"
            '{"mode": "question", "content": "hello"}\n'
            "```\n\n"
            "Middle narrative.\n\n"
            "```json\n"
            '{"content": "是否重写"discovery"步骤"}\n'
            "```\n\n"
            "Outro text."
        )
        result = _extract_narrative_from_raw(raw)
        assert "Intro text." in result
        assert "Middle narrative." in result
        assert "Outro text." in result
        assert "```json" not in result
        assert '"discovery"' not in result
        assert "mode" not in result

    def test_trailing_bare_json_with_unescaped_quotes_stripped(self):
        """Trailing bare JSON with unescaped quotes must be stripped.

        After a fenced block is removed, the remaining text may be bare JSON
        (not in fences). The lenient helper must recognize it so it is not
        added to narrative.
        """
        raw = (
            "Intro text.\n\n"
            "```json\n"
            '{"a": 1}\n'
            "```\n\n"
            '{"content": "是否重写"discovery"步骤"}'
        )
        result = _extract_narrative_from_raw(raw)
        assert result == "Intro text."
        assert '"discovery"' not in result
        assert "是否重写" not in result

    def test_chinese_fullwidth_punctuation_in_json_value(self):
        """Chinese full-width quotes inside JSON string values should not break
        the lenient detection; the block must be stripped."""
        raw = 'Analysis here.\n\n```json\n{"key": "「中文内容」"}\n```'
        result = _extract_narrative_from_raw(raw)
        assert "Analysis here." in result
        assert "```json" not in result
        assert "「中文内容」" not in result

    def test_cross_assertion_with_parse_json_response(self):
        """The unescaped-quote sample that parse_json_response recovers must
        also be recognized by the narrative extractor (semantic symmetry)."""
        from se3.engine.utils.json_parser import parse_json_response

        raw = '```json\n{"content": "是否重写"discovery"步骤"}\n```'
        # Verify parse_json_response can recover it
        parsed = parse_json_response(raw)
        assert parsed is not None
        assert "content" in parsed
        # Now verify narrative extractor strips the same block
        result = _extract_narrative_from_raw(raw)
        assert result == ""

    def test_narrative_with_trailing_bare_json_no_fence(self):
        """No-fence text with trailing bare JSON: only narrative is kept.

        When raw text contains NO fenced blocks and looks like
        "narrative ... {JSON}", the trailing JSON must be stripped so the
        narrative path does NOT return the whole string including the JSON
        body (which would duplicate what parse_json_response already shows
        as formatted content).
        """
        raw = 'Here is my analysis.\n\n{"mode": "confirmation", "content": "done"}'
        result = _extract_narrative_from_raw(raw)
        assert "Here is my analysis." in result
        assert "mode" not in result
        assert "confirmation" not in result
        assert result.strip() == "Here is my analysis."

    def test_narrative_with_trailing_bare_json_no_fence_unescaped_quotes(self):
        """No-fence text with trailing bare JSON containing unescaped quotes."""
        raw = 'Analysis here.\n\n{"content": "是否重写"discovery"步骤"}'
        result = _extract_narrative_from_raw(raw)
        assert "Analysis here." in result
        assert "是否重写" not in result
        assert '"discovery"' not in result
        assert result.strip() == "Analysis here."

    def test_narrative_before_and_after_bare_json_no_fence(self):
        """Narrative both before and after bare JSON should both be kept."""
        raw = 'Before narrative.\n\n{"mode": "confirmation", "content": "done"}\n\nAfter narrative.'
        result = _extract_narrative_from_raw(raw)
        assert "Before narrative." in result
        assert "After narrative." in result
        assert "mode" not in result
        assert "confirmation" not in result

    def test_bare_json_at_start_with_trailing_narrative(self):
        """Bare JSON at the start followed by narrative: narrative is kept."""
        raw = '{"mode": "question", "content": "hi"}\n\nTrailing narrative here.'
        result = _extract_narrative_from_raw(raw)
        assert "Trailing narrative here." in result
        assert "mode" not in result
        assert "question" not in result
        assert result.strip() == "Trailing narrative here."

    def test_fenced_block_containing_scalar_json_is_stripped(self):
        """Fenced blocks containing scalar JSON should be stripped."""
        raw = 'Some narrative.\n\n```json\n"just a string"\n```'
        result = _extract_narrative_from_raw(raw)
        assert result.strip() == "Some narrative."
        assert "just a string" not in result
        assert "```json" not in result

    def test_multiple_bare_json_objects_without_fences(self):
        """Two bare JSON objects in trailing text without fences.

        Both JSON objects should be stripped entirely rather than leaving
        the first one in narrative (which would duplicate formatted content).
        """
        raw = '{"a": 1}\n{"b": 2}'
        result = _extract_narrative_from_raw(raw)
        assert "a" not in result
        assert "b" not in result
        assert result == ""

    def test_narrative_with_literal_braces_and_json(self):
        """Narrative containing literal {/} characters outside JSON.

        The literal braces (e.g. in '{{placeholder}}' or code samples) should
        NOT confuse extraction — the actual JSON object must still be stripped.
        """
        raw = 'Use the {{x}} placeholder for {"json": 1}'
        result = _extract_narrative_from_raw(raw)
        assert "Use the {{x}} placeholder for" in result
        assert '"json"' not in result
        assert "1" not in result
        # The literal {{x}} should remain; the JSON should be gone
        assert "{{x}}" in result

    def test_narrative_with_code_sample_braces(self):
        """Narrative with code-like braces followed by actual JSON."""
        raw = 'See `dict = {key: value}` for examples.\n\n{"mode": "confirmation"}'
        result = _extract_narrative_from_raw(raw)
        assert "See `dict = {key: value}` for examples." in result
        assert "mode" not in result
        assert "confirmation" not in result

    def test_narrative_with_literal_braces_and_unescaped_quotes(self):
        """Literal braces in narrative + trailing bare JSON with unescaped quotes.

        Combines two edge cases: literal {placeholder} characters in narrative
        should NOT confuse extraction, and the trailing JSON with unescaped
        ASCII double quotes inside string values must still be recognized and
        stripped (via lenient _try_parse_with_repairs in the backward walk).
        """
        raw = 'See {placeholder} for {"content": "是否重写"X"步骤"}'
        result = _extract_narrative_from_raw(raw)
        assert "See {placeholder} for" in result
        assert "是否重写" not in result
        assert '"X"' not in result
        assert result.strip() == "See {placeholder} for"

    def test_multiple_bare_jsons_with_narrative_prefix(self):
        """Multiple bare JSONs preceded by narrative: ALL JSONs stripped.

        With the recursive helper, both {"a": 1} and {"b": 2} are extracted
        and stripped, leaving only the narrative prefix.
        """
        raw = 'Analysis here.\n\n{"a": 1}\n{"b": 2}'
        result = _extract_narrative_from_raw(raw)
        # Both JSON objects must be stripped (check for JSON syntax, not letters)
        assert '"a"' not in result
        assert '"b"' not in result
        assert result.strip() == "Analysis here."

    def test_multiple_bare_jsons_with_trailing_narrative(self):
        """Multi-JSON followed by trailing narrative: all stripped.

        Ensures that when bare JSON objects are not at the end (they have
        trailing prose after them), the recursive rightmost extraction still
        finds and strips every JSON object.
        """
        raw = 'foo\n{"a":1}\nmid\n{"b":2}\nbaz'
        result = _extract_narrative_from_raw(raw)
        assert "foo" in result
        assert "mid" in result
        assert "baz" in result
        assert '"a"' not in result
        assert '"b"' not in result

    def test_inline_fenced_json_block_removed(self):
        """Single-line fenced blocks like ```json {\"a\":1}``` must be stripped.

        The regex previously required a newline after the opening fence and
        before the closing fence, so inline fences survived into narrative.
        """
        raw = 'Before inline. ```json {"a": 1}``` After inline.'
        result = _extract_narrative_from_raw(raw)
        assert "Before inline." in result
        assert "After inline." in result
        assert "```json" not in result
        assert '"a"' not in result

    def test_inline_fenced_json_without_tag_removed(self):
        """Inline bare fence ```{\"a\":1}``` must also be stripped."""
        raw = 'Start ```{"b": 2}``` End'
        result = _extract_narrative_from_raw(raw)
        assert "Start" in result
        assert "End" in result
        assert "```" not in result
        assert '"b"' not in result

    def test_many_bare_json_objects_no_recursion_error(self):
        """Many bare JSON objects should not hit recursion limit.

        The previous recursive implementation could hit Python's default
        recursion limit (~1000) for pathologically large inputs.
        """
        jsons = '\n'.join(f'{{"n": {i}}}' for i in range(1500))
        raw = f'Narrative prefix.\n\n{jsons}'
        result = _extract_narrative_from_raw(raw)
        # Only the narrative prefix should remain
        assert "Narrative prefix." in result
        # All JSON objects stripped — check a representative key
        assert '"n"' not in result
        assert result.strip() == "Narrative prefix."

    def test_many_identical_bare_jsons_all_stripped(self):
        """Multiple identical bare JSON objects must all be stripped.

        If _strip_all_trailing_jsons used rfind on the extracted string,
        identical objects could cause index drift. The span-tracking fix
        returns the exact start index from _extract_trailing_json_string,
        avoiding any ambiguity when the same JSON literal appears multiple
        times in the text.
        """
        raw = 'Prefix.\n\n{"x": 1}\n\n{"x": 1}\n\n{"x": 1}'
        result = _extract_narrative_from_raw(raw)
        assert "Prefix." in result
        assert '"x"' not in result
        assert result.strip() == "Prefix."


class TestDisplayDiscoveryMessageWithNarrative:
    """Test _display_discovery_message renders narrative from raw_result_text."""

    @patch("se3.engine.display.get_console")
    def test_narrative_rendered_first_when_present(self, mock_console):
        """When raw_result_text contains narrative, it should be first renderable."""
        from rich.console import Group
        from rich.markdown import Markdown

        raw = "Additional narrative text.\n\n```json\n{\"mode\": \"question\"}\n```"
        _display_discovery_message("content", None, questions=None, raw_result_text=raw)

        # Heading + blank + Group + blank = 4 prints
        assert mock_console.return_value.print.call_count == 4
        heading = mock_console.return_value.print.call_args_list[0][0][0]
        assert "## Discovery" in heading
        group = mock_console.return_value.print.call_args_list[2][0][0]
        assert isinstance(group, Group)
        # First renderable should be the narrative Markdown
        assert isinstance(group.renderables[0], Markdown)
        assert "Additional narrative text." in str(group.renderables[0].markup)

    @patch("se3.engine.display.get_console")
    def test_no_narrative_when_raw_is_none(self, mock_console):
        """When raw_result_text is None, Group should match current behavior."""
        from rich.console import Group
        from rich.markdown import Markdown

        _display_discovery_message("Hello", None, questions=None, raw_result_text=None)

        assert mock_console.return_value.print.call_count == 4
        group = mock_console.return_value.print.call_args_list[2][0][0]
        assert isinstance(group, Group)
        # First renderable should be Markdown("Hello"), no extra narrative
        assert isinstance(group.renderables[0], Markdown)
        assert "Hello" in str(group.renderables[0].markup)

    @patch("se3.engine.display.get_console")
    def test_no_narrative_when_raw_is_pure_json(self, mock_console):
        """When raw_result_text is pure JSON, no extra narrative renderable."""
        from rich.console import Group
        from rich.markdown import Markdown

        raw = '{"mode": "question", "content": "Hello"}'
        _display_discovery_message("Hello", None, questions=None, raw_result_text=raw)

        assert mock_console.return_value.print.call_count == 4
        group = mock_console.return_value.print.call_args_list[2][0][0]
        assert isinstance(group, Group)
        # First renderable should be Markdown("Hello"), no narrative prefix
        assert isinstance(group.renderables[0], Markdown)
        assert "Hello" in str(group.renderables[0].markup)


class TestRestoreDiscoveryDisplayWithRawText:
    """Test _restore_discovery_display passes raw_result_text from history."""

    @patch("se3.engine.steps.discovery._display_discovery_message")
    def test_restore_passes_raw_result_text_from_history(self, mock_display):
        """Resume should pass last assistant's content as raw_result_text."""
        from se3.commands.run import _restore_discovery_display

        raw_text = "Narrative here.\n```json\n{\"mode\":\"confirmation\"}\n```"
        step = Step(
            step_type=StepType.DISCOVERY,
            inputs={
                "task_description": "I want auth",
                "discovery_state": {
                    "round": 3,
                    "history": [
                        {"role": "assistant", "content": raw_text, "round": 2},
                    ],
                },
            },
        )
        step.outputs["message"] = "Confirmed!"
        step.outputs["refined_description"] = "Build auth"
        step.outputs["awaiting_programmatic_confirm"] = True

        _restore_discovery_display(step)

        mock_display.assert_called_once()
        kwargs = mock_display.call_args.kwargs
        assert kwargs.get("raw_result_text") == raw_text
        assert kwargs.get("is_confirmation") is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
