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


class TestDisplayDiscoveryMessageWithNarrative:
    """Test _display_discovery_message renders narrative from raw_result_text."""

    @patch("se3.engine.display.get_console")
    def test_narrative_rendered_first_when_present(self, mock_console):
        """When raw_result_text contains narrative, it should be first renderable."""
        from rich.console import Group
        from rich.markdown import Markdown

        raw = "Additional narrative text.\n\n```json\n{\"mode\": \"question\"}\n```"
        _display_discovery_message("content", None, questions=None, raw_result_text=raw)

        mock_console.return_value.print.assert_called_once()
        panel = mock_console.return_value.print.call_args[0][0]
        group = panel.renderable
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

        mock_console.return_value.print.assert_called_once()
        panel = mock_console.return_value.print.call_args[0][0]
        group = panel.renderable
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

        mock_console.return_value.print.assert_called_once()
        panel = mock_console.return_value.print.call_args[0][0]
        group = panel.renderable
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
