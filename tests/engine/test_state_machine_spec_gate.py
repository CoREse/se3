"""Tests for mechanism A's state-machine integration (group G4).

Covers the four G4 tasks wired into ``state_machine.py``:

1. ``_snapshot_specs_before_update`` — captures the stable pre-update_spec spec
   snapshot once, before UPDATE_SPEC first edits a spec; never overwritten.
2. ``transition_to_next`` — SPEC_GATE joins the fix-trigger set and shares the
   ``max_fix_iterations`` exhaustion bound; REVISION_NEEDED dispatches by
   ``gate_route`` (``implement`` → fix loop, ``update_spec`` → redo).
3. ``_transition_to_update_spec_redo`` — resets UPDATE_SPEC to PENDING, counts a
   global fix iteration, lands back on SPEC_GATE after the redo completes.
4. ``_build_step_inputs`` — SPEC_GATE receives the same baseline as TEST plus the
   requirement snapshot; ``_transition_to_fix`` charges the per-flow baseline
   budget only when the fix targets baseline failures.
"""

from __future__ import annotations

from unittest.mock import patch

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


VALID_SPEC = """<!-- spec-format: v1 -->
# my-feature Specification

## Purpose

Defines the my-feature behaviour for the project.

## Requirements

### Requirement: Alpha
- The system SHALL do alpha.

### Requirement: Beta
- The system SHALL do beta.
"""


def _write_spec(project_root, name: str, content: str) -> None:
    spec_dir = project_root / "se3" / "specs" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Task 1: _snapshot_specs_before_update
# ---------------------------------------------------------------------------


class TestSnapshotSpecsBeforeUpdate:
    @pytest.fixture
    def state_machine(self, tmp_path):
        return StateMachine(project_root=tmp_path)

    def _flow(self):
        return FlowInstance(
            flow_id="snap-flow",
            task_description="task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )

    def test_captures_content_and_requirement_names(self, state_machine, tmp_path):
        _write_spec(tmp_path, "my-feature", VALID_SPEC)
        flow = self._flow()

        state_machine._snapshot_specs_before_update(flow)

        snap = flow.state.context["spec_requirement_baseline"]
        assert "my-feature" in snap
        assert snap["my-feature"]["requirements"] == ["Alpha", "Beta"]
        assert "SHALL do alpha" in snap["my-feature"]["content"]

    def test_only_captured_once_not_overwritten(self, state_machine, tmp_path):
        _write_spec(tmp_path, "my-feature", VALID_SPEC)
        flow = self._flow()

        state_machine._snapshot_specs_before_update(flow)
        first = flow.state.context["spec_requirement_baseline"]

        # Simulate a (corrupting) edit landing on disk, then a redo re-entering
        # update_spec: the snapshot must NOT be re-taken from the changed disk.
        reduced = VALID_SPEC.replace(
            "### Requirement: Beta\n- The system SHALL do beta.\n", ""
        )
        _write_spec(tmp_path, "my-feature", reduced)

        state_machine._snapshot_specs_before_update(flow)
        second = flow.state.context["spec_requirement_baseline"]

        assert second is first
        assert second["my-feature"]["requirements"] == ["Alpha", "Beta"]

    def test_tolerates_missing_specs_dir(self, state_machine, tmp_path):
        flow = self._flow()

        state_machine._snapshot_specs_before_update(flow)

        # Snapshot still recorded (empty) so it is not retried, and no raise.
        assert flow.state.context["spec_requirement_baseline"] == {}

    def test_run_step_invokes_snapshot_for_update_spec(self, state_machine, tmp_path):
        _write_spec(tmp_path, "my-feature", VALID_SPEC)
        flow = self._flow()
        step = Step(step_type=StepType.UPDATE_SPEC, status=StepStatus.PENDING)
        flow.state.add_step(step)
        flow.state.current_step_id = step.step_id

        # Register a no-op handler so run_step completes without an LLM call.
        state_machine.register_handler(
            StepType.UPDATE_SPEC, lambda s, f: StepStatus.COMPLETED
        )

        state_machine.run_step(flow, step)

        assert "spec_requirement_baseline" in flow.state.context
        assert flow.state.context["spec_requirement_baseline"]["my-feature"][
            "requirements"
        ] == ["Alpha", "Beta"]


# ---------------------------------------------------------------------------
# Shared fixtures for transition tests
# ---------------------------------------------------------------------------


def _feature_flow_at_gate(gate_outputs):
    """Build a flow whose current step is a REVISION_NEEDED SPEC_GATE."""
    flow = FlowInstance(
        flow_id="gate-flow",
        task_description="task",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = [
        StepType.IMPLEMENT,
        StepType.TEST,
        StepType.VERIFY_SPEC,
        StepType.UPDATE_SPEC,
        StepType.SPEC_GATE,
        StepType.VERSION_ANALYZE,
        StepType.COMMIT,
    ]

    implement = Step(
        step_type=StepType.IMPLEMENT,
        status=StepStatus.COMPLETED,
        outputs={"files_changed": [], "tests_added": []},
    )
    flow.state.add_step(implement)

    update = Step(
        step_type=StepType.UPDATE_SPEC,
        status=StepStatus.COMPLETED,
        outputs={"updated_specs": ["my-feature"]},
    )
    flow.state.add_step(update)

    gate = Step(
        step_type=StepType.SPEC_GATE,
        status=StepStatus.REVISION_NEEDED,
        outputs=gate_outputs,
    )
    flow.state.add_step(gate)
    flow.state.current_step_id = gate.step_id
    flow.state.current_step_index = flow.state.selected_steps.index(StepType.SPEC_GATE)
    return flow, implement, update, gate


# ---------------------------------------------------------------------------
# Task 2 + 3: transition_to_next dispatch by gate_route
# ---------------------------------------------------------------------------


class TestSpecGateTransitionRouting:
    @pytest.fixture
    def state_machine(self, tmp_path):
        return StateMachine(project_root=tmp_path)

    def test_gate_route_update_spec_redoes_update_spec(self, state_machine):
        flow, implement, update, gate = _feature_flow_at_gate(
            {
                "gate_passed": False,
                "gate_route": "update_spec",
                "fix_needed": True,
                "fix_instructions": "spec lost a requirement",
                "fix_context": {"reason": "spec_gate_artifact_invalid"},
            }
        )

        next_step = state_machine.transition_to_next(flow)

        assert next_step is update
        assert update.status == StepStatus.PENDING
        assert update.inputs["is_spec_redo"] is True
        assert update.inputs["fix_instructions"] == "spec lost a requirement"
        # Global fix counter charged; flow points back at update_spec.
        assert flow.state.get_fix_iteration() == 1
        assert flow.state.current_step_id == update.step_id
        # implement untouched by the redo path
        assert implement.status == StepStatus.COMPLETED

    def test_gate_route_implement_enters_fix_loop(self, state_machine):
        flow, implement, update, gate = _feature_flow_at_gate(
            {
                "gate_passed": False,
                "gate_route": "implement",
                "fix_needed": True,
                "fix_instructions": "a spec-content test went red",
                "fix_context": {"reason": "spec_gate_test_failure"},
            }
        )

        next_step = state_machine.transition_to_next(flow)

        assert next_step is implement
        assert implement.status == StepStatus.PENDING
        assert implement.inputs["is_fix_iteration"] is True
        assert flow.state.get_fix_iteration() == 1
        assert flow.state.current_step_id == implement.step_id

    def test_redo_then_progression_returns_to_spec_gate(self, state_machine):
        """After the update_spec redo completes, normal progression lands back
        on a fresh SPEC_GATE so the redone artifact is re-checked."""
        flow, implement, update, gate = _feature_flow_at_gate(
            {
                "gate_passed": False,
                "gate_route": "update_spec",
                "fix_needed": True,
                "fix_instructions": "fix it",
                "fix_context": {},
            }
        )

        state_machine.transition_to_next(flow)  # → update_spec redo
        # Simulate the redo completing cleanly.
        update.status = StepStatus.COMPLETED

        next_step = state_machine.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.SPEC_GATE
        assert next_step.step_id != gate.step_id  # a fresh gate instance

    def test_exhaustion_shared_with_global_budget(self, state_machine):
        flow, implement, update, gate = _feature_flow_at_gate(
            {
                "gate_passed": False,
                "gate_route": "update_spec",
                "fix_needed": True,
                "fix_instructions": "fix it",
                "fix_context": {},
            }
        )
        # Drive the shared global counter to the cap.
        flow.state.fix_iterations = 100

        with patch.object(state_machine, "_get_max_fix_iterations", return_value=100):
            next_step = state_machine.transition_to_next(flow)

        assert next_step is None
        assert flow.status == FlowStatus.FAILED

    def test_exhaustion_unlimited_sentinel_keeps_redoing(self, state_machine):
        flow, implement, update, gate = _feature_flow_at_gate(
            {
                "gate_passed": False,
                "gate_route": "update_spec",
                "fix_needed": True,
                "fix_instructions": "fix it",
                "fix_context": {},
            }
        )
        flow.state.fix_iterations = 500

        with patch.object(state_machine, "_get_max_fix_iterations", return_value=0):
            next_step = state_machine.transition_to_next(flow)

        assert next_step is update
        assert flow.status == FlowStatus.RUNNING


class TestTransitionToUpdateSpecRedo:
    @pytest.fixture
    def state_machine(self, tmp_path):
        return StateMachine(project_root=tmp_path)

    def test_returns_none_when_fix_not_needed(self, state_machine):
        flow, implement, update, gate = _feature_flow_at_gate(
            {"gate_route": "update_spec", "fix_needed": False}
        )
        assert state_machine._transition_to_update_spec_redo(flow, gate) is None

    def test_returns_none_when_no_update_spec_step(self, state_machine):
        flow = FlowInstance(
            flow_id="f", task_description="t", status=FlowStatus.RUNNING
        )
        gate = Step(
            step_type=StepType.SPEC_GATE,
            status=StepStatus.REVISION_NEEDED,
            outputs={"fix_needed": True, "gate_route": "update_spec"},
        )
        assert state_machine._transition_to_update_spec_redo(flow, gate) is None

    def test_records_fix_history_reason(self, state_machine):
        flow, implement, update, gate = _feature_flow_at_gate(
            {
                "gate_route": "update_spec",
                "fix_needed": True,
                "fix_instructions": "x",
                "fix_context": {},
            }
        )
        state_machine._transition_to_update_spec_redo(flow, gate)
        assert flow.state.fix_history[-1]["reason"] == "spec_artifact"
        assert flow.state.fix_history[-1]["trigger_step_type"] == "spec_gate"


# ---------------------------------------------------------------------------
# Task 4: _build_step_inputs for SPEC_GATE + baseline counting
# ---------------------------------------------------------------------------


class TestSpecGateInputs:
    @pytest.fixture
    def state_machine(self, tmp_path):
        return StateMachine(project_root=tmp_path)

    def test_spec_gate_gets_baseline_and_snapshot(self, state_machine):
        flow = FlowInstance(
            flow_id="f",
            task_description="t",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.baseline_failures = ["tests/test_x.py::test_a"]
        snap = {"my-feature": {"content": "...", "requirements": ["Alpha"]}}
        flow.state.context["spec_requirement_baseline"] = snap

        implement = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={
                "files_changed": [],
                "tests_added": ["tests/test_new.py"],
                "estimated_test_duration": 99,
            },
        )
        flow.state.add_step(implement)

        inputs = state_machine._build_step_inputs(flow, StepType.SPEC_GATE)

        assert inputs["baseline_failures"] == ["tests/test_x.py::test_a"]
        assert inputs["spec_requirement_baseline"] == snap
        # Forwarded from implement contract (same as TEST's口径).
        assert inputs["tests_added"] == ["tests/test_new.py"]
        assert inputs["estimated_test_duration"] == 99

    def test_spec_gate_baseline_none_injects_empty_list(self, state_machine):
        flow = FlowInstance(
            flow_id="f",
            task_description="t",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        # baseline_failures left as None (not yet captured)
        inputs = state_machine._build_step_inputs(flow, StepType.SPEC_GATE)
        assert inputs["baseline_failures"] == []
        assert inputs["spec_requirement_baseline"] == {}


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
