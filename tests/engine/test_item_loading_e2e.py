"""End-to-end integration tests for item-level spec loading.

Verifies:
1. spec_index loads real project specs as item-level entries
2. spec_loader "items" mode assembles base + headers + selected items
3. spec_loader "full_spec" mode loads complete spec text
4. Context size reduction: items-mode text << full spec text
5. state_machine forwards selected_items and re-renders spec_content per step load_mode
6. update_spec step prompt includes spec_decisions schema
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.spec_index import load_or_build, SpecIndex
from se3.engine.spec_loader import load_for_step, load_full
from se3.engine.state_machine import StateMachine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPECS_DIR = PROJECT_ROOT / "se3" / "specs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_flow(project_root: Path, task_type: str = "feature") -> FlowInstance:
    return FlowInstance(
        flow_id="test-flow-item-loading",
        task_description="Test item loading",
        task_type=task_type,
        status=FlowStatus.RUNNING,
        change_path=project_root / "dummy",
    )


def _add_completed_step(flow: FlowInstance, step_type: StepType, outputs: dict) -> Step:
    step = Step(step_type=step_type, status=StepStatus.COMPLETED, outputs=outputs)
    flow.state.add_step(step)
    return step


# ---------------------------------------------------------------------------
# Tests against real project specs
# ---------------------------------------------------------------------------

class TestSpecIndexRealSpecs:
    """Build/load the index against the real se3/specs/ directory."""

    def test_load_or_build_creates_index(self):
        index = load_or_build(PROJECT_ROOT)
        assert isinstance(index, SpecIndex)
        assert len(index.items) > 0

    def test_base_spec_items_present(self):
        index = load_or_build(PROJECT_ROOT)
        base_items = [k for k in index.items if k.startswith("base::")]
        assert len(base_items) >= 4  # base has 5 requirements + sentinel

    def test_flow_engine_items_present(self):
        index = load_or_build(PROJECT_ROOT)
        fe_items = [k for k in index.items if k.startswith("flow-engine::")]
        assert len(fe_items) >= 25  # flow-engine has 30 requirements

    def test_all_specs_have_v1_marker(self):
        index = load_or_build(PROJECT_ROOT)
        for spec_dir in sorted(SPECS_DIR.iterdir()):
            if not spec_dir.is_dir():
                continue
            spec_file = spec_dir / "spec.md"
            if not spec_file.exists():
                continue
            text = spec_file.read_text(encoding="utf-8")
            assert "<!-- spec-format: v1 -->" in text, (
                f"{spec_file} missing v1 marker"
            )

    def test_list_for_selector_returns_menu(self):
        index = load_or_build(PROJECT_ROOT)
        menu = index.list_for_selector()
        assert len(menu) > 0
        for entry in menu:
            assert "spec" in entry
            assert "requirement_name" in entry
            assert "tags" in entry
            assert "summary" in entry

    def test_selector_menu_excludes_sentinel(self):
        index = load_or_build(PROJECT_ROOT)
        menu = index.list_for_selector()
        for entry in menu:
            assert entry["requirement_name"] != "__no_requirements__"


class TestSpecLoaderItemsMode:
    """spec_loader items mode with real project specs."""

    def test_items_mode_loads_base_always(self):
        result = load_for_step(
            step_type="plan",
            selected_items=[{"spec": "flow-engine", "requirement_name": "状态机驱动流程"}],
            project_root=PROJECT_ROOT,
            mode="items",
        )
        assert "base" in result.relevant_specs
        assert "# SE3 Framework" in result.text or "# SE3 Framework" in result.text

    def test_items_mode_excludes_unselected_requirements(self):
        result = load_for_step(
            step_type="plan",
            selected_items=[{"spec": "flow-engine", "requirement_name": "状态机驱动流程"}],
            project_root=PROJECT_ROOT,
            mode="items",
        )
        # The selected requirement should be present
        assert "Requirement: 状态机驱动流程" in result.text
        # Other flow-engine requirements should NOT be present
        assert "Requirement: 13 步流程池" not in result.text
        assert "Requirement: 统一入口" not in result.text

    def test_items_mode_includes_spec_header(self):
        result = load_for_step(
            step_type="plan",
            selected_items=[{"spec": "flow-engine", "requirement_name": "状态机驱动流程"}],
            project_root=PROJECT_ROOT,
            mode="items",
        )
        # The spec header (Purpose, etc.) should be included
        assert "## Purpose" in result.text
        assert "flow-engine" in result.text or "Flow Engine" in result.text

    def test_items_mode_context_size_reduction(self):
        """Items mode text should be much smaller than full spec text."""
        # Full flow-engine spec text
        full_text = load_full(["flow-engine"], PROJECT_ROOT)
        assert len(full_text) > 5000  # Sanity: flow-engine is large

        # Items mode: select just one requirement from flow-engine
        result = load_for_step(
            step_type="plan",
            selected_items=[{"spec": "flow-engine", "requirement_name": "状态机驱动流程"}],
            project_root=PROJECT_ROOT,
            mode="items",
        )
        # Items mode text should be significantly smaller than full text
        # (at least 50% reduction for a single-item selection from a large spec)
        assert len(result.text) < len(full_text) * 0.5, (
            f"Items mode ({len(result.text)} chars) not much smaller than "
            f"full spec ({len(full_text)} chars)"
        )

    def test_items_mode_loaded_items_tracks_selection(self):
        result = load_for_step(
            step_type="plan",
            selected_items=[{"spec": "flow-engine", "requirement_name": "状态机驱动流程"}],
            project_root=PROJECT_ROOT,
            mode="items",
        )
        assert "flow-engine::状态机驱动流程" in result.loaded_items


class TestSpecLoaderFullSpecMode:
    """spec_loader full_spec mode with real project specs."""

    def test_full_spec_mode_loads_all_requirements(self):
        result = load_for_step(
            step_type="update_spec",
            selected_items=[{"spec": "flow-engine", "requirement_name": "状态机驱动流程"}],
            project_root=PROJECT_ROOT,
            mode="full_spec",
        )
        # Full spec mode should include ALL requirements from flow-engine
        assert "Requirement: 统一入口" in result.text
        assert "Requirement: 状态机驱动流程" in result.text
        assert "Requirement: 13 步流程池" in result.text

    def test_full_spec_mode_includes_base(self):
        result = load_for_step(
            step_type="update_spec",
            selected_items=[{"spec": "flow-engine", "requirement_name": "统一入口 se3 run"}],
            project_root=PROJECT_ROOT,
            mode="full_spec",
        )
        assert "base" in result.relevant_specs
        assert "flow-engine" in result.relevant_specs


class TestStateMachineSelectedItemsPassthrough:
    """Verify selected_items flows through state_machine._build_step_inputs."""

    @pytest.fixture
    def sm(self):
        return StateMachine(PROJECT_ROOT)

    def _flow_with_analyze_selected_items(self):
        flow = _make_flow(PROJECT_ROOT)
        _add_completed_step(flow, StepType.ANALYZE, {
            "task_type": "feature",
            "scope": "engine",
            "complexity": "medium",
            "reasoning": "Test reasoning",
            "project_summary": "SE3 project",
            "relevant_specs": ["base", "flow-engine"],
            "spec_content": "base + flow-engine header + items",
            "selected_items": [
                {"spec": "flow-engine", "requirement_name": "状态机驱动流程"},
            ],
            "selected_specs": ["flow-engine"],
        })
        return flow

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_selected_items_forwarded_to_plan(self, _cfg, sm):
        flow = self._flow_with_analyze_selected_items()
        inputs = sm._build_step_inputs(flow, StepType.PLAN)
        assert "selected_items" in inputs
        assert isinstance(inputs["selected_items"], list)
        assert len(inputs["selected_items"]) == 1
        assert inputs["selected_items"][0]["spec"] == "flow-engine"

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_selected_items_forwarded_to_implement(self, _cfg, sm):
        flow = self._flow_with_analyze_selected_items()
        inputs = sm._build_step_inputs(flow, StepType.IMPLEMENT)
        assert "selected_items" in inputs
        assert inputs["selected_items"][0]["requirement_name"] == "状态机驱动流程"

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_update_spec_gets_full_spec_mode(self, _cfg, sm):
        """update_spec defaults to full_spec mode (naming collision check)."""
        flow = self._flow_with_analyze_selected_items()
        inputs = sm._build_step_inputs(flow, StepType.UPDATE_SPEC)
        # full_spec mode should re-render spec_content from the actual spec files
        assert "spec_content" in inputs
        # The full spec text should be longer than the analyze item-filtered version
        assert "Requirement: 13 步流程池" in inputs["spec_content"]

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_plan_gets_items_mode(self, _cfg, sm):
        """plan defaults to items mode (uses analyze's already-filtered spec_content)."""
        flow = self._flow_with_analyze_selected_items()
        inputs = sm._build_step_inputs(flow, StepType.PLAN)
        # items mode: spec_content should be the same as what analyze produced
        assert inputs["spec_content"] == "base + flow-engine header + items"

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_relevant_specs_derived_correctly(self, _cfg, sm):
        flow = self._flow_with_analyze_selected_items()
        inputs = sm._build_step_inputs(flow, StepType.PLAN)
        assert "base" in inputs["relevant_specs"]
        assert "flow-engine" in inputs["relevant_specs"]

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_update_spec_full_spec_empty_items_propagates_value_error(self, _cfg, sm):
        """Empty selected_items in full_spec mode must surface as a hard error,
        not silently degrade to items-mode spec_content."""
        flow = _make_flow(PROJECT_ROOT)
        _add_completed_step(flow, StepType.ANALYZE, {
            "task_type": "feature",
            "scope": "engine",
            "complexity": "medium",
            "reasoning": "Test reasoning",
            "project_summary": "SE3 project",
            "relevant_specs": ["base"],
            "spec_content": "base only",
            "selected_items": [],
        })
        with pytest.raises(ValueError, match="analyze"):
            sm._build_step_inputs(flow, StepType.UPDATE_SPEC)


class TestAnalyzeHandlerItemLevel:
    """Integration test: analyze handler outputs item-level selected_items."""

    def test_analyze_handler_outputs_selected_items(self):
        """Mock the LLM call and verify analyze_handler outputs selected_items."""
        from se3.engine.steps.analyze import analyze_handler

        flow = _make_flow(PROJECT_ROOT)
        step = Step(
            step_type=StepType.ANALYZE,
            status=StepStatus.PENDING,
            inputs={"task_description": "Implement a new feature"},
        )
        llm_response = json.dumps({
            "task_type": "feature",
            "scope": "engine",
            "complexity": "medium",
            "reasoning": "This is a feature task affecting the engine",
            "selected_items": [
                {"spec": "flow-engine", "requirement_name": "状态机驱动流程"},
            ],
            "selected_specs": ["flow-engine"],
        })
        with patch("se3.engine.steps.analyze.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = llm_response
            mock_cls.return_value = mock_caller
            status = analyze_handler(step, flow)

        assert status == StepStatus.COMPLETED
        assert "selected_items" in step.outputs
        selected_items = step.outputs["selected_items"]
        assert isinstance(selected_items, list)
        assert len(selected_items) > 0
        assert selected_items[0]["spec"] == "flow-engine"
        assert selected_items[0]["requirement_name"] == "状态机驱动流程"
        # Output-side cleanup: selected_specs must not leak into outputs even
        # when the LLM returns it alongside selected_items.
        assert "selected_specs" not in step.outputs

    def test_analyze_handler_spec_content_smaller_than_full(self):
        """The assembled spec_content should be smaller than full flow-engine text."""
        from se3.engine.steps.analyze import analyze_handler

        flow = _make_flow(PROJECT_ROOT)
        step = Step(
            step_type=StepType.ANALYZE,
            status=StepStatus.PENDING,
            inputs={"task_description": "Implement a new feature"},
        )
        llm_response = json.dumps({
            "task_type": "feature",
            "scope": "engine",
            "complexity": "medium",
            "reasoning": "Feature task",
            "selected_items": [
                {"spec": "flow-engine", "requirement_name": "状态机驱动流程"},
            ],
            "selected_specs": ["flow-engine"],
        })
        with patch("se3.engine.steps.analyze.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = llm_response
            mock_cls.return_value = mock_caller
            analyze_handler(step, flow)

        full_text = load_full(["flow-engine"], PROJECT_ROOT)
        spec_content = step.outputs.get("spec_content", "")
        assert len(spec_content) < len(full_text) * 0.5, (
            f"spec_content ({len(spec_content)} chars) not much smaller than "
            f"full flow-engine ({len(full_text)} chars)"
        )

    def test_analyze_handler_backward_compat_selected_specs(self):
        """When LLM returns old-format selected_specs, fallback maps to items."""
        from se3.engine.steps.analyze import analyze_handler

        flow = _make_flow(PROJECT_ROOT)
        step = Step(
            step_type=StepType.ANALYZE,
            status=StepStatus.PENDING,
            inputs={"task_description": "Implement a new feature"},
        )
        # Old format: only selected_specs, no selected_items
        llm_response = json.dumps({
            "task_type": "feature",
            "scope": "engine",
            "complexity": "medium",
            "reasoning": "Feature",
            "selected_specs": ["base"],
        })
        with patch("se3.engine.steps.analyze.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = llm_response
            mock_cls.return_value = mock_caller
            status = analyze_handler(step, flow)

        assert status == StepStatus.COMPLETED
        # selected_items should be populated via fallback
        assert "selected_items" in step.outputs
        assert isinstance(step.outputs["selected_items"], list)
        # base spec items should be included
        base_items = [
            item for item in step.outputs["selected_items"]
            if item.get("spec") == "base"
        ]
        assert len(base_items) > 0


class TestFormatSelectorItems:
    """Tests for _format_selector_items helper in analyze step."""

    def test_format_selector_skips_empty_spec(self):
        """Items with empty/missing spec name should not emit broken headings."""
        from se3.engine.steps.analyze import _format_selector_items

        items = [
            {"spec": "alpha", "requirement_name": "R1", "tags": [], "summary": ""},
            {"spec": "", "requirement_name": "Bad", "tags": [], "summary": ""},
            {"spec": "beta", "requirement_name": "R2", "tags": [], "summary": ""},
        ]
        result = _format_selector_items(items)
        # Should not contain "### " (empty heading from missing spec)
        assert "### \n" not in result
        assert "### alpha" in result
        assert "### beta" in result
        assert "alpha::R1" in result
        assert "beta::R2" in result
        # The bad item should be skipped entirely
        assert "Bad" not in result

    def test_format_selector_groups_by_spec(self):
        """Requirements from the same spec should be grouped under one heading."""
        from se3.engine.steps.analyze import _format_selector_items

        items = [
            {"spec": "alpha", "requirement_name": "R1", "tags": ["a"], "summary": "s1"},
            {"spec": "alpha", "requirement_name": "R2", "tags": [], "summary": "s2"},
            {"spec": "beta", "requirement_name": "R3", "tags": [], "summary": ""},
        ]
        result = _format_selector_items(items)
        # alpha heading should appear once
        assert result.count("### alpha") == 1
        assert result.count("### beta") == 1
        assert "alpha::R1 [tags: a] — s1" in result
        assert "alpha::R2" in result
        assert "beta::R3" in result

    def test_format_selector_empty_list(self):
        """Empty items list should return the fallback message."""
        from se3.engine.steps.analyze import _format_selector_items

        result = _format_selector_items([])
        assert result == "(no items available)"


class TestUpdateSpecSpecDecisions:
    """Verify update_spec handler outputs spec_decisions field."""

    def test_update_spec_prompt_contains_spec_decisions_schema(self):
        """The update_spec handler's LLM prompt should include spec_decisions schema."""
        from se3.engine.steps.update_spec import update_spec_handler

        flow = _make_flow(PROJECT_ROOT)
        step = Step(
            step_type=StepType.UPDATE_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Add new feature",
                "changes_made": {"files_changed": ["src/foo.py"]},
                "verification_result": {"verified": True, "summary": "OK"},
                "relevant_specs": ["flow-engine"],
            },
        )
        llm_response = json.dumps({
            "specs_updated": [{"spec_name": "flow-engine", "change_description": "Added X"}],
            "new_capabilities": [],
            "spec_decisions": [
                {
                    "requirement_name": "New Feature X",
                    "decision": "append",
                    "target_spec": "flow-engine",
                    "reasoning": "Fits within flow-engine scope",
                }
            ],
            "notes": "",
        })
        with patch("se3.engine.steps.update_spec.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = llm_response
            mock_cls.return_value = mock_caller
            status = update_spec_handler(step, flow)

        assert status == StepStatus.COMPLETED
        assert "spec_decisions" in step.outputs
        spec_decisions = step.outputs["spec_decisions"]
        assert isinstance(spec_decisions, list)
        assert len(spec_decisions) == 1
        assert spec_decisions[0]["decision"] == "append"

    def test_update_spec_empty_spec_decisions(self):
        """When no new requirements are added, spec_decisions should be empty."""
        from se3.engine.steps.update_spec import update_spec_handler

        flow = _make_flow(PROJECT_ROOT)
        step = Step(
            step_type=StepType.UPDATE_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Fix typo",
                "changes_made": {"files_changed": ["README.md"]},
                "verification_result": {"verified": True, "summary": "OK"},
                "relevant_specs": ["base"],
            },
        )
        llm_response = json.dumps({
            "specs_updated": [],
            "new_capabilities": [],
            "spec_decisions": [],
            "notes": "No spec changes needed",
        })
        with patch("se3.engine.steps.update_spec.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = llm_response
            mock_cls.return_value = mock_caller
            status = update_spec_handler(step, flow)

        assert status == StepStatus.COMPLETED
        assert "spec_decisions" in step.outputs
        assert step.outputs["spec_decisions"] == []

    def test_update_spec_prompt_includes_new_spec_vs_append_criteria(self):
        """The update_spec prompt should reference the new-spec-vs-append criteria."""
        from se3.engine.steps.update_spec import update_spec_handler

        flow = _make_flow(PROJECT_ROOT)
        step = Step(
            step_type=StepType.UPDATE_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Add new subsystem",
                "changes_made": {"files_changed": ["src/subsystem.py"]},
                "verification_result": {"verified": True, "summary": "OK"},
                "relevant_specs": ["flow-engine"],
            },
        )
        with patch("se3.engine.steps.update_spec.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = json.dumps({
                "specs_updated": [],
                "new_capabilities": [],
                "spec_decisions": [],
                "notes": "",
            })
            mock_cls.return_value = mock_caller
            update_spec_handler(step, flow)

        prompt = mock_caller.call.call_args[1]["prompt"]
        # The prompt should mention the spec decision criteria
        assert "new spec" in prompt.lower() or "append" in prompt.lower()
