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
    parse_user_response,
    _format_conversation_history,
    _generate_summary,
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
        """Discovery sequence should include all standard steps."""
        sequence = get_default_step_sequence("discovery")
        # Should have discovery + all feature steps (project_summary and read_spec merged into analyze)
        assert len(sequence) >= 10  # discovery + analyze + plan + ... + summarize


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
        # User-facing outputs for synthesis mode
        assert "message" in step.outputs
        assert "proposed_description" in step.outputs
        assert step.outputs["proposed_description"] == "Build a user authentication system with login/logout"
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


class TestUserResponseParsing:
    """Test user response parsing."""

    def test_confirm_keywords(self):
        """Should detect confirmation keywords."""
        assert parse_user_response("yes")["confirmed"] is True
        assert parse_user_response("ok")["confirmed"] is True
        assert parse_user_response("proceed")["confirmed"] is True
        assert parse_user_response("没问题")["confirmed"] is True
        assert parse_user_response("好的")["confirmed"] is True

    def test_reject_keywords(self):
        """Should detect rejection keywords."""
        assert parse_user_response("no")["confirmed"] is False
        assert parse_user_response("not quite")["confirmed"] is False
        assert parse_user_response("需要修改")["confirmed"] is False

    def test_ambiguous_short_response(self):
        """Short ambiguous responses treated as confirm."""
        assert parse_user_response("ok thanks")["confirmed"] is True

    def test_ambiguous_long_response(self):
        """Long ambiguous responses treated as feedback."""
        result = parse_user_response("Actually I think we need to change this part because it doesn't match what I want")
        assert result["confirmed"] is False


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
