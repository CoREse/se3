"""Tests for _build_step_inputs forwarding spec_changes and design_doc.

Verifies:
1. PLAN outputs spec_changes → verify_spec and update_spec receive it
2. PLAN outputs design_doc (from plan.design) → update_spec receives it
3. When PLAN has no spec_changes, downstream steps are unaffected
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.state_machine import StateMachine


def _make_flow(tmp_path: Path, task_description: str = "Test task") -> FlowInstance:
    """Create a flow with PLAN completed in step history."""
    flow = FlowInstance(
        flow_id="test-flow-sm-sc",
        task_description=task_description,
        task_type="feature",
        status=FlowStatus.RUNNING,
        change_path=tmp_path / "changes" / "test",
    )
    return flow


def _add_completed_step(
    flow: FlowInstance,
    step_type: StepType,
    outputs: dict,
) -> Step:
    """Add a completed step with given outputs to the flow."""
    step = Step(step_type=step_type, status=StepStatus.COMPLETED, outputs=outputs)
    flow.state.add_step(step)
    return step


class TestSpecChangesForwarding:
    """Test _build_step_inputs forwards spec_changes from PLAN to downstream steps."""

    @pytest.fixture
    def sm(self, tmp_path):
        return StateMachine(tmp_path)

    @pytest.fixture
    def flow_with_plan(self, tmp_path):
        """Flow with a completed PLAN step that has spec_changes."""
        flow = _make_flow(tmp_path)
        _add_completed_step(flow, StepType.PLAN, {
            "plan": {
                "proposal": {"summary": "s"},
                "design": {
                    "overview": "High-level design",
                    "architecture_decisions": [
                        {"decision": "D1", "rationale": "R1"}
                    ],
                    "components": [
                        {"name": "C1", "responsibilities": "Do things"}
                    ],
                },
            },
            "task_groups": [{"group_id": "G1", "name": "g", "tasks": []}],
            "spec_changes": [
                {
                    "spec_name": "flow-engine",
                    "change_type": "add_requirement",
                    "target": "Requirement: New Feature",
                    "description": "Add new feature",
                    "rationale": "Needed for X",
                }
            ],
        })
        return flow

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_verify_spec_receives_spec_changes(self, _cfg, sm, flow_with_plan):
        inputs = sm._build_step_inputs(flow_with_plan, StepType.VERIFY_SPEC)
        assert "spec_changes" in inputs
        assert len(inputs["spec_changes"]) == 1
        assert inputs["spec_changes"][0]["spec_name"] == "flow-engine"
        assert inputs["spec_changes"][0]["change_type"] == "add_requirement"

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_update_spec_receives_spec_changes(self, _cfg, sm, flow_with_plan):
        inputs = sm._build_step_inputs(flow_with_plan, StepType.UPDATE_SPEC)
        assert "spec_changes" in inputs
        assert len(inputs["spec_changes"]) == 1
        assert inputs["spec_changes"][0]["target"] == "Requirement: New Feature"

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_update_spec_receives_design_doc(self, _cfg, sm, flow_with_plan):
        inputs = sm._build_step_inputs(flow_with_plan, StepType.UPDATE_SPEC)
        assert "design_doc" in inputs
        assert inputs["design_doc"]["overview"] == "High-level design"
        assert len(inputs["design_doc"]["architecture_decisions"]) == 1
        assert len(inputs["design_doc"]["components"]) == 1


class TestNoSpecChanges:
    """Test that missing/empty spec_changes doesn't break existing behavior."""

    @pytest.fixture
    def sm(self, tmp_path):
        return StateMachine(tmp_path)

    @pytest.fixture
    def flow_without_spec_changes(self, tmp_path):
        """Flow with PLAN that has no spec_changes key in outputs."""
        flow = _make_flow(tmp_path)
        _add_completed_step(flow, StepType.PLAN, {
            "plan": {
                "proposal": {"summary": "bugfix"},
                "design": {"overview": "Fix approach"},
            },
            "task_groups": [{"group_id": "G1", "name": "fix", "tasks": []}],
            # No spec_changes key at all
        })
        return flow

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_verify_spec_gets_empty_list_when_no_spec_changes(self, _cfg, sm, flow_without_spec_changes):
        inputs = sm._build_step_inputs(flow_without_spec_changes, StepType.VERIFY_SPEC)
        assert inputs["spec_changes"] == []

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_update_spec_gets_empty_list_when_no_spec_changes(self, _cfg, sm, flow_without_spec_changes):
        inputs = sm._build_step_inputs(flow_without_spec_changes, StepType.UPDATE_SPEC)
        assert inputs["spec_changes"] == []

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_design_doc_still_forwarded_without_spec_changes(self, _cfg, sm, flow_without_spec_changes):
        inputs = sm._build_step_inputs(flow_without_spec_changes, StepType.UPDATE_SPEC)
        assert inputs["design_doc"]["overview"] == "Fix approach"

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_other_plan_outputs_unaffected(self, _cfg, sm, flow_without_spec_changes):
        """task_groups and proposal still forwarded normally."""
        inputs = sm._build_step_inputs(flow_without_spec_changes, StepType.IMPLEMENT)
        assert inputs["task_groups"] == [{"group_id": "G1", "name": "fix", "tasks": []}]
        assert inputs["design_doc"]["overview"] == "Fix approach"


class TestEmptyDesignDoc:
    """Test that empty/missing design in plan doesn't break update_spec."""

    @pytest.fixture
    def sm(self, tmp_path):
        return StateMachine(tmp_path)

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_empty_design_forwarded_as_empty_dict(self, _cfg, sm, tmp_path):
        flow = _make_flow(tmp_path)
        _add_completed_step(flow, StepType.PLAN, {
            "plan": {
                "proposal": {"summary": "quick fix"},
                # No "design" key
            },
            "task_groups": [],
        })
        inputs = sm._build_step_inputs(flow, StepType.UPDATE_SPEC)
        assert inputs["design_doc"] == {}
