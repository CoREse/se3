"""Tests for _build_step_inputs after the PLAN spec_changes channel retired.

Verifies:
1. PLAN no longer forwards a ``spec_changes`` input to any downstream step,
   even when a legacy persisted flow still carries the key in its outputs
2. PLAN no longer forwards ``proposal`` / ``design_doc`` either — a legacy
   flow whose PLAN outputs still carry them is read without reviving them
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

    @pytest.mark.parametrize(
        "step_type",
        [StepType.UPDATE_SPEC, StepType.IMPLEMENT, StepType.COMMIT],
    )
    @patch("tianluo.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_legacy_plan_proposal_and_design_are_not_revived(
        self, _cfg, sm, flow_with_legacy_spec_changes, step_type,
    ):
        """A persisted plan.proposal / plan.design is read, never forwarded.

        The old flow still renders in `luo history show`; what stopped is the
        step-to-step channel, so no downstream prompt can pick them back up.
        """
        inputs = sm._build_step_inputs(flow_with_legacy_spec_changes, step_type)
        assert "design_doc" not in inputs
        assert "proposal" not in inputs


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
    def test_other_plan_outputs_unaffected(self, _cfg, sm, flow_without_spec_changes):
        """task_groups is still forwarded normally."""
        inputs = sm._build_step_inputs(flow_without_spec_changes, StepType.IMPLEMENT)
        assert inputs["task_groups"] == [{"group_id": "G1", "name": "fix", "tasks": []}]

    @patch("tianluo.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_deprecated_design_step_still_forwards_its_output(
        self, _cfg, sm, flow_without_spec_changes,
    ):
        """Legacy DESIGN/PROPOSE flows resume unchanged — only PLAN stopped."""
        _add_completed_step(
            flow_without_spec_changes,
            StepType.DESIGN,
            {"design_doc": {"overview": "legacy design"}},
        )
        _add_completed_step(
            flow_without_spec_changes,
            StepType.PROPOSE,
            {"proposal": {"summary": "legacy proposal"}},
        )
        inputs = sm._build_step_inputs(
            flow_without_spec_changes, StepType.UPDATE_SPEC,
        )
        assert inputs["design_doc"]["overview"] == "legacy design"
        assert inputs["proposal"]["summary"] == "legacy proposal"
