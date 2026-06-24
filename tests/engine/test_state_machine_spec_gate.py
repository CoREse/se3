"""State-machine fix-loop accounting tests (mechanism B baseline budget).

The mechanism-A spec_gate integration (``_snapshot_specs_before_update``,
SPEC_GATE ``gate_route`` dispatch, ``_transition_to_update_spec_redo`` and the
SPEC_GATE input injection) was retired by the charter refactor (group G6) along
with the whole spec governance machinery; only the per-flow baseline-budget
accounting in ``_transition_to_fix`` survives and is covered here.
"""

from __future__ import annotations

import pytest

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.state_machine import StateMachine



class TestBaselineAttemptCounting:
    @pytest.fixture
    def state_machine(self, tmp_path):
        return StateMachine(project_root=tmp_path)

    def _flow_with_implement(self):
        flow = FlowInstance(
            flow_id="f",
            task_description="t",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERIFY_SPEC,
        ]
        implement = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": []},
        )
        flow.state.add_step(implement)
        return flow, implement

    def test_baseline_targeted_fix_increments_counter(self, state_machine):
        flow, implement = self._flow_with_implement()
        trigger = Step(
            step_type=StepType.TEST,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "fix baseline",
                "fix_context": {
                    "reason": "baseline_failure",
                    "baseline_failures_targeted": ["tests/test_x.py::test_a"],
                },
            },
        )
        flow.state.add_step(trigger)

        state_machine._transition_to_fix(flow, trigger)

        assert flow.state.context["baseline_fix_attempts"] == 1

        # A second baseline-targeted fix increments again.
        trigger.status = StepStatus.REVISION_NEEDED
        state_machine._transition_to_fix(flow, trigger)
        assert flow.state.context["baseline_fix_attempts"] == 2

    def test_introduced_only_fix_does_not_increment(self, state_machine):
        flow, implement = self._flow_with_implement()
        trigger = Step(
            step_type=StepType.TEST,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "fix regression",
                "fix_context": {"reason": "test_failure"},  # no baseline target
            },
        )
        flow.state.add_step(trigger)

        state_machine._transition_to_fix(flow, trigger)

        assert "baseline_fix_attempts" not in flow.state.context
