"""Tests for _build_step_inputs after the PLAN spec_changes channel retired.

Verifies:
1. PLAN no longer forwards a ``spec_changes`` input to any downstream step,
   even when a legacy persisted flow still carries the key in its outputs
2. PLAN still forwards design_doc (from plan.design) to update_spec
3. The rest of PLAN's outputs are unaffected by the channel removal
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch

from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.state_machine import StateMachine


def _make_flow(tmp_path: Path, task_description: str = "Test task") -> FlowInstance:
    """Create a flow with PLAN completed in step history."""
    return FlowInstance(
        flow_id="test-flow-sm-sc",
        task_description=task_description,
        task_type="feature",
        status=FlowStatus.RUNNING,
        change_path=tmp_path / "changes" / "test",
    )


def _add_completed_step(
    flow: FlowInstance,
    step_type: StepType,
    outputs: dict,
) -> Step:
    """Add a completed step with given outputs to the flow."""
    step = Step(step_type=step_type, status=StepStatus.COMPLETED, outputs=outputs)
    flow.state.add_step(step)
    return step


class TestSpecChangesNotForwarded:
    """PLAN's retired spec_changes channel reaches no downstream step."""

    @pytest.fixture
    def sm(self, tmp_path):
        return StateMachine(tmp_path)

    @pytest.fixture
    def flow_with_legacy_spec_changes(self, tmp_path):
        """A persisted flow whose PLAN step still carries spec_changes.

        Old flows on disk keep the key; resuming one must not revive the
        channel — the key is simply ignored.
        """
        flow = _make_flow(tmp_path)
        _add_completed_step(flow, StepType.ANALYZE, {
            "task_type": "feature",
            "scope": "src/",
        })
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

    @pytest.mark.parametrize(
        "step_type",
        [
            StepType.VERIFY_SPEC,
            StepType.UPDATE_SPEC,
            StepType.IMPLEMENT,
            StepType.VERSION_ANALYZE,
        ],
    )
    @patch("tianluo.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_no_step_receives_spec_changes(
        self, _cfg, sm, flow_with_legacy_spec_changes, step_type,
    ):
        inputs = sm._build_step_inputs(flow_with_legacy_spec_changes, step_type)
        assert "spec_changes" not in inputs

    @patch("tianluo.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_update_spec_receives_design_doc(
        self, _cfg, sm, flow_with_legacy_spec_changes,
    ):
        inputs = sm._build_step_inputs(
            flow_with_legacy_spec_changes, StepType.UPDATE_SPEC,
        )
        assert "design_doc" in inputs
        assert inputs["design_doc"]["overview"] == "High-level design"
        assert len(inputs["design_doc"]["architecture_decisions"]) == 1
        assert len(inputs["design_doc"]["components"]) == 1


class TestPlanForwardingUnaffected:
    """Removing the channel leaves PLAN's remaining forwarding intact."""

    @pytest.fixture
    def sm(self, tmp_path):
        return StateMachine(tmp_path)

    @pytest.fixture
    def flow_without_spec_changes(self, tmp_path):
        """Flow with PLAN that has no spec_changes key in outputs."""
        flow = _make_flow(tmp_path)
        _add_completed_step(flow, StepType.ANALYZE, {
            "task_type": "bugfix",
            "scope": "src/",
        })
        _add_completed_step(flow, StepType.PLAN, {
            "plan": {
                "proposal": {"summary": "bugfix"},
                "design": {"overview": "Fix approach"},
            },
            "task_groups": [{"group_id": "G1", "name": "fix", "tasks": []}],
        })
        return flow

    @patch("tianluo.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_design_doc_still_forwarded(self, _cfg, sm, flow_without_spec_changes):
        inputs = sm._build_step_inputs(flow_without_spec_changes, StepType.UPDATE_SPEC)
        assert inputs["design_doc"]["overview"] == "Fix approach"

    @patch("tianluo.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_other_plan_outputs_unaffected(self, _cfg, sm, flow_without_spec_changes):
        """task_groups and design_doc still forwarded normally."""
        inputs = sm._build_step_inputs(flow_without_spec_changes, StepType.IMPLEMENT)
        assert inputs["task_groups"] == [{"group_id": "G1", "name": "fix", "tasks": []}]
        assert inputs["design_doc"]["overview"] == "Fix approach"
