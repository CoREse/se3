"""Tests for the update_spec step handler — spec_changes and design_doc integration."""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from se3.engine.models import FlowInstance, Step, StepStatus, StepType, FlowStatus
from se3.engine.steps.update_spec import (
    _format_spec_changes,
    _format_design_doc,
    _format_redo_guidance,
    update_spec_handler,
    UPDATE_SPEC_PROMPT,
)


class TestFormatSpecChanges:
    """Tests for _format_spec_changes helper."""

    def test_empty_list_returns_default(self):
        assert _format_spec_changes([]) == "No specific spec changes planned."

    def test_none_like_empty(self):
        """Falsy input returns default message."""
        assert _format_spec_changes(None) == "No specific spec changes planned."

    def test_single_change(self):
        changes = [
            {
                "spec_name": "flow-engine",
                "change_type": "add_requirement",
                "target": "Requirement: Plan spec_changes Output",
                "description": "New output field",
                "rationale": "Guidance for downstream steps",
            }
        ]
        result = _format_spec_changes(changes)
        assert "[add_requirement] flow-engine: Requirement: Plan spec_changes Output" in result
        assert "Description: New output field" in result
        assert "Rationale: Guidance for downstream steps" in result

    def test_multiple_changes(self):
        changes = [
            {"spec_name": "spec-a", "change_type": "modify_requirement", "target": "Req A"},
            {"spec_name": "spec-b", "change_type": "add_scenario", "target": "Scenario B"},
        ]
        result = _format_spec_changes(changes)
        assert "[modify_requirement] spec-a" in result
        assert "[add_scenario] spec-b" in result

    def test_missing_optional_fields(self):
        """description and rationale are optional."""
        changes = [
            {"spec_name": "s", "change_type": "deprecate_requirement", "target": "Old Req"}
        ]
        result = _format_spec_changes(changes)
        assert "[deprecate_requirement] s: Old Req" in result
        assert "Description:" not in result
        assert "Rationale:" not in result

    def test_missing_required_fields_uses_defaults(self):
        """Missing spec_name/change_type/target use 'unknown'/empty."""
        changes = [{}]
        result = _format_spec_changes(changes)
        assert "[unknown] unknown:" in result


class TestFormatDesignDoc:
    """Tests for _format_design_doc helper."""

    def test_empty_dict_returns_default(self):
        assert _format_design_doc({}) == "No design document available."

    def test_none_returns_default(self):
        assert _format_design_doc(None) == "No design document available."

    def test_overview_only(self):
        doc = {"overview": "High-level summary of the design."}
        result = _format_design_doc(doc)
        assert "### Overview" in result
        assert "High-level summary of the design." in result

    def test_components(self):
        doc = {
            "components": [
                {"component": "plan.py", "responsibilities": "Generate spec_changes"},
                {"component": "state_machine.py", "responsibilities": "Forward inputs"},
            ]
        }
        result = _format_design_doc(doc)
        assert "### Components" in result
        assert "**plan.py**" in result
        assert "**state_machine.py**" in result

    def test_components_with_name_key(self):
        """Components may use 'name' instead of 'component'."""
        doc = {"components": [{"name": "MyComponent", "description": "Does things"}]}
        result = _format_design_doc(doc)
        assert "**MyComponent**: Does things" in result

    def test_architecture_decisions(self):
        doc = {
            "architecture_decisions": [
                {
                    "decision": "Use intent declarations, not diffs",
                    "rationale": "Plan stage is too early for precise diffs",
                }
            ]
        }
        result = _format_design_doc(doc)
        assert "### Architecture Decisions" in result
        assert "**Use intent declarations, not diffs**" in result
        assert "Rationale: Plan stage is too early for precise diffs" in result

    def test_full_design_doc(self):
        doc = {
            "overview": "Refactor data flow.",
            "components": [{"component": "A", "responsibilities": "Do A"}],
            "architecture_decisions": [{"decision": "D1", "rationale": "R1"}],
        }
        result = _format_design_doc(doc)
        assert "### Overview" in result
        assert "### Components" in result
        assert "### Architecture Decisions" in result

    def test_empty_subsections_returns_default(self):
        """Dict with only empty/missing subsections returns default."""
        doc = {"overview": "", "components": [], "architecture_decisions": []}
        assert _format_design_doc(doc) == "No design document available."


class TestFormatRedoGuidance:
    """Tests for the SPEC_GATE redo-guidance helper (mechanism A)."""

    def test_not_a_redo_returns_empty(self):
        assert _format_redo_guidance(False, "anything", {"spec_errors": ["x"]}) == ""

    def test_redo_includes_fix_instructions(self):
        result = _format_redo_guidance(
            True,
            "The spec edit deleted a requirement and must be redone.",
            {},
        )
        assert "SPEC REDO" in result
        assert "REJECTED" in result
        assert "deleted a requirement and must be redone" in result

    def test_redo_includes_spec_errors_and_names(self):
        result = _format_redo_guidance(
            True,
            "rejected",
            {
                "spec_errors": [
                    "spec 'flow-engine' removed requirement(s): Pre-implement Test Baseline",
                ],
                "edited_specs": ["flow-engine"],
                "new_specs": ["brand-new"],
            },
        )
        assert "removed requirement(s): Pre-implement Test Baseline" in result
        assert "flow-engine" in result
        assert "brand-new" in result
        assert "RESTORE" in result

    def test_redo_tolerates_non_dict_context(self):
        # A malformed fix_context must not raise — still emit the header.
        result = _format_redo_guidance(True, "rejected", None)  # type: ignore[arg-type]
        assert "SPEC REDO" in result



    """Verify prompt contains the expected placeholders."""

    def test_spec_changes_placeholder(self):
        assert "{spec_changes}" in UPDATE_SPEC_PROMPT

    def test_design_doc_placeholder(self):
        assert "{design_doc}" in UPDATE_SPEC_PROMPT

    def test_guided_mode_instructions(self):
        assert "Spec Change Guidance" in UPDATE_SPEC_PROMPT
        assert "checklist" in UPDATE_SPEC_PROMPT

    def test_inference_fallback_instructions(self):
        assert "inference mode" in UPDATE_SPEC_PROMPT

    def test_existing_placeholders_preserved(self):
        assert "{task_description}" in UPDATE_SPEC_PROMPT
        assert "{changes_made}" in UPDATE_SPEC_PROMPT
        assert "{verification_result}" in UPDATE_SPEC_PROMPT
        assert "{specs_dir}" in UPDATE_SPEC_PROMPT

    def test_redo_guidance_placeholder(self):
        assert "{redo_guidance}" in UPDATE_SPEC_PROMPT

    def test_prompt_renders_with_empty_inputs(self):
        """Prompt renders without error when spec_changes and design_doc are empty defaults."""
        rendered = UPDATE_SPEC_PROMPT.format(
            task_description="Test task",
            changes_made="No changes",
            verification_result="No verification",
            spec_changes="No specific spec changes planned.",
            design_doc="No design document available.",
            specs_dir="/tmp/specs",
            redo_guidance="",
        )
        assert "No specific spec changes planned." in rendered
        assert "No design document available." in rendered

    def test_prompt_renders_with_real_inputs(self):
        """Prompt renders with actual spec_changes and design_doc content."""
        rendered = UPDATE_SPEC_PROMPT.format(
            task_description="Add feature X",
            changes_made="- modified: src/foo.py",
            verification_result="Verified: true",
            spec_changes="- [add_requirement] spec-a: New Req",
            design_doc="### Overview\nRefactor data flow.",
            specs_dir="/project/se3/specs",
            redo_guidance="",
        )
        assert "[add_requirement] spec-a" in rendered
        assert "### Overview" in rendered


class TestUpdateSpecHandlerIntegration:
    """Test that update_spec_handler reads spec_changes/design_doc from inputs and injects into prompt."""

    @pytest.fixture
    def flow(self, tmp_path):
        flow = FlowInstance(
            flow_id="test-flow-us",
            task_description="Add feature",
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "test",
        )
        flow.state.selected_steps = [StepType.UPDATE_SPEC]
        return flow

    def _make_step(self, spec_changes=None, design_doc=None):
        inputs = {
            "task_description": "Add feature",
            "changes_made": {"files_changed": ["src/foo.py"]},
            "verification_result": {"verified": True, "summary": "OK"},
        }
        if spec_changes is not None:
            inputs["spec_changes"] = spec_changes
        if design_doc is not None:
            inputs["design_doc"] = design_doc
        return Step(
            step_type=StepType.UPDATE_SPEC,
            status=StepStatus.PENDING,
            inputs=inputs,
        )

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.context_builder.get_step_language_instruction", return_value="")
    def test_handler_injects_spec_changes_into_prompt(self, _lang, _inj, flow):
        step = self._make_step(
            spec_changes=[
                {
                    "spec_name": "flow-engine",
                    "change_type": "add_requirement",
                    "target": "Requirement: New Output",
                    "description": "Add spec_changes output",
                    "rationale": "For downstream guidance",
                }
            ],
        )

        with patch("se3.engine.steps.update_spec.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = '{"specs_updated": [], "new_capabilities": []}'
            mock_cls.return_value = mock_caller

            result = update_spec_handler(step, flow)

            prompt = mock_caller.call.call_args[1]["prompt"]
            assert "[add_requirement] flow-engine: Requirement: New Output" in prompt
            assert "Description: Add spec_changes output" in prompt
            assert "Rationale: For downstream guidance" in prompt
            assert result == StepStatus.COMPLETED

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.context_builder.get_step_language_instruction", return_value="")
    def test_handler_injects_design_doc_into_prompt(self, _lang, _inj, flow):
        step = self._make_step(
            design_doc={
                "overview": "Refactor the data flow pipeline",
                "components": [{"component": "plan.py", "responsibilities": "Generate changes"}],
                "architecture_decisions": [{"decision": "Use intent, not diff", "rationale": "Too early for diffs"}],
            },
        )

        with patch("se3.engine.steps.update_spec.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = '{"specs_updated": [], "new_capabilities": []}'
            mock_cls.return_value = mock_caller

            update_spec_handler(step, flow)

            prompt = mock_caller.call.call_args[1]["prompt"]
            assert "Refactor the data flow pipeline" in prompt
            assert "**plan.py**" in prompt
            assert "**Use intent, not diff**" in prompt

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.context_builder.get_step_language_instruction", return_value="")
    def test_handler_injects_redo_guidance_on_spec_redo(self, _lang, _inj, flow):
        """A SPEC_GATE redo surfaces the gate's diagnosis into the prompt so the
        redo repairs the rejected artifact rather than re-issuing an identical call."""
        step = self._make_step()
        step.inputs["is_spec_redo"] = True
        step.inputs["fix_instructions"] = (
            "The spec edit deleted requirement 'Foo' and must be redone."
        )
        step.inputs["fix_context"] = {
            "reason": "spec_gate_artifact_invalid",
            "spec_errors": ["spec 'flow-engine' removed requirement(s): Foo"],
            "edited_specs": ["flow-engine"],
            "new_specs": [],
        }

        with patch("se3.engine.steps.update_spec.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = '{"specs_updated": [], "new_capabilities": []}'
            mock_cls.return_value = mock_caller

            result = update_spec_handler(step, flow)

            prompt = mock_caller.call.call_args[1]["prompt"]
            assert "SPEC REDO" in prompt
            assert "deleted requirement 'Foo' and must be redone" in prompt
            assert "removed requirement(s): Foo" in prompt
            assert "flow-engine" in prompt
            assert result == StepStatus.COMPLETED

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.context_builder.get_step_language_instruction", return_value="")
    def test_handler_no_redo_guidance_on_normal_run(self, _lang, _inj, flow):
        """A normal (first-pass) update_spec run carries no redo block."""
        step = self._make_step()

        with patch("se3.engine.steps.update_spec.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = '{"specs_updated": [], "new_capabilities": []}'
            mock_cls.return_value = mock_caller

            update_spec_handler(step, flow)

            prompt = mock_caller.call.call_args[1]["prompt"]
            assert "SPEC REDO" not in prompt

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.context_builder.get_step_language_instruction", return_value="")
    def test_handler_defaults_when_no_spec_changes_or_design_doc(self, _lang, _inj, flow):
        """When inputs omit spec_changes and design_doc, prompt uses default messages."""
        step = self._make_step()  # No spec_changes or design_doc

        with patch("se3.engine.steps.update_spec.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = '{"specs_updated": [], "new_capabilities": []}'
            mock_cls.return_value = mock_caller

            result = update_spec_handler(step, flow)

            prompt = mock_caller.call.call_args[1]["prompt"]
            assert "No specific spec changes planned." in prompt
            assert "No design document available." in prompt
            assert result == StepStatus.COMPLETED
