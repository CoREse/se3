"""INVESTIGATE must never overlap the background pre-implement baseline suite.

The baseline capture is launched at flow start to hide a multi-minute pytest
run under the ``analyze → plan → confirm`` window; that is only sound while
every step in the window is read-only. INVESTIGATE is not — it may write probe
patches into the very tree the suite is executing. These tests pin the
serialization that keeps the two apart, in both directions:

* a flow that still has a TEST step awaits the clean-tree measurement, so the
  baseline it later subtracts as "inherited" cannot contain probe fallout;
* a survey flow (no TEST at all) discards the run instead of blocking on it,
  and — crucially — never lets a half-finished measurement reach the cache.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.state_machine import StateMachine


def _make_state_machine(tmp_path) -> StateMachine:
    with patch("tianluo.engine.state_machine.PersistenceManager"):
        return StateMachine(project_root=tmp_path)


def _make_flow(selected_steps) -> FlowInstance:
    flow = FlowInstance(
        flow_id="baseline-serialization-flow",
        task_description="intermittent empty result from X",
        task_type="bugfix",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = selected_steps
    return flow


BUGFIX_STEPS = [
    StepType.ANALYZE,
    StepType.INVESTIGATE,
    StepType.PLAN,
    StepType.IMPLEMENT,
    StepType.TEST,
    StepType.COMMIT,
]

SURVEY_STEPS = [StepType.ANALYZE, StepType.INVESTIGATE, StepType.SUMMARIZE]


class TestSettleBaselineBeforeInvestigation:
    def test_awaits_capture_when_a_test_step_still_consumes_the_baseline(
        self, tmp_path
    ):
        sm = _make_state_machine(tmp_path)
        capture = MagicMock()
        sm._baseline_capture = capture
        flow = _make_flow(BUGFIX_STEPS)

        with patch.object(sm, "_ensure_baseline_ready") as ready:
            sm._settle_baseline_before_investigation(flow)

        ready.assert_called_once_with(flow)
        # Awaiting keeps the measurement — it must not be thrown away.
        capture.kill.assert_not_called()

    def test_discards_capture_for_a_survey_flow_without_measuring(self, tmp_path):
        sm = _make_state_machine(tmp_path)
        capture = MagicMock()
        sm._baseline_capture = capture
        flow = _make_flow(SURVEY_STEPS)

        with patch.object(sm, "_ensure_baseline_ready") as ready:
            sm._settle_baseline_before_investigation(flow)

        ready.assert_not_called()
        capture.kill.assert_called_once()
        assert sm._baseline_capture is None
        # Nothing was measured, so nothing (poisoned or otherwise) is persisted.
        assert flow.state.baseline_failures is None

    def test_no_capture_in_flight_never_triggers_a_synchronous_suite(self, tmp_path):
        """A cache hit / resumed flow must not pay for a suite run here."""
        sm = _make_state_machine(tmp_path)
        sm._baseline_capture = None
        flow = _make_flow(BUGFIX_STEPS)

        with patch.object(sm, "_ensure_baseline_ready") as ready:
            sm._settle_baseline_before_investigation(flow)

        ready.assert_not_called()


class TestRunStepSerializesBeforeTheHandler:
    def test_capture_is_settled_before_the_investigate_handler_runs(self, tmp_path):
        """Ordering is the whole point: settling after the handler is useless."""
        sm = _make_state_machine(tmp_path)
        sm.persistence = MagicMock()
        capture = MagicMock()
        sm._baseline_capture = capture
        flow = _make_flow(SURVEY_STEPS)

        seen_at_handler_time = {}

        def handler(step, _flow):
            seen_at_handler_time["capture"] = sm._baseline_capture
            return StepStatus.COMPLETED

        sm._handlers[StepType.INVESTIGATE] = handler
        step = Step(step_type=StepType.INVESTIGATE, status=StepStatus.PENDING)
        flow.state.add_step(step)

        sm.run_step(flow, step)

        assert seen_at_handler_time["capture"] is None
        capture.kill.assert_called_once()

    def test_non_investigate_steps_leave_the_capture_running(self, tmp_path):
        """The overlap is the feature for read-only steps; don't kill it."""
        sm = _make_state_machine(tmp_path)
        sm.persistence = MagicMock()
        capture = MagicMock()
        sm._baseline_capture = capture
        flow = _make_flow(SURVEY_STEPS)

        sm._handlers[StepType.ANALYZE] = lambda step, _flow: StepStatus.COMPLETED
        step = Step(step_type=StepType.ANALYZE, status=StepStatus.PENDING)
        flow.state.add_step(step)

        sm.run_step(flow, step)

        assert sm._baseline_capture is capture
        capture.kill.assert_not_called()
