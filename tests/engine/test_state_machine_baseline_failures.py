"""Tests for state_machine wiring of the pre-implement test baseline (G3).

Covers:
- init_flow / _start_baseline_capture: cache hit reuses, cache miss launches
  a background capture, and a resumed flow with an already-measured baseline
  does not re-launch.
- run_step / _ensure_baseline_ready: baseline is non-None before IMPLEMENT,
  background failure falls back to a synchronous re-measurement, both-fail
  degrades to an empty baseline, and fix-loop re-entry is idempotent.
- _build_step_inputs: TEST and VERIFY_SPEC inputs carry baseline_failures
  (injected as [] when not yet captured).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.state_machine import StateMachine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_flow() -> FlowInstance:
    flow = FlowInstance(task_description="do a thing", task_type="feature")
    return flow


def _fake_capture(result):
    """A stand-in BaselineCapture whose wait() returns *result*."""
    cap = MagicMock()
    cap.launch.return_value = cap
    cap.wait.return_value = result
    return cap


# ---------------------------------------------------------------------------
# _start_baseline_capture (Task 1)
# ---------------------------------------------------------------------------

class TestStartBaselineCapture:
    def test_cache_hit_skips_background_launch(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()

        with patch("se3.engine.test_baseline.compute_baseline_key", return_value="k1"), \
             patch("se3.engine.test_baseline.load_cached", return_value={"tests/test_a.py::test_x"}), \
             patch("se3.engine.test_baseline.BaselineCapture") as MockCapture:
            sm._start_baseline_capture(flow)

        # Cache hit: baseline written from cache, no background capture launched.
        assert flow.state.baseline_failures == ["tests/test_a.py::test_x"]
        MockCapture.assert_not_called()
        assert sm._baseline_capture is None

    def test_cache_miss_launches_background_capture(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()
        cap = _fake_capture({"tests/test_b.py::test_y"})

        with patch("se3.engine.test_baseline.compute_baseline_key", return_value="k2"), \
             patch("se3.engine.test_baseline.load_cached", return_value=None), \
             patch("se3.engine.test_baseline.BaselineCapture", return_value=cap):
            sm._start_baseline_capture(flow)

        # Cache miss: background capture launched, state still un-measured (None)
        # because the result is only resolved at _ensure_baseline_ready time.
        cap.launch.assert_called_once()
        assert sm._baseline_capture is cap
        assert sm._baseline_key == "k2"
        assert flow.state.baseline_failures is None

    def test_resume_with_existing_baseline_does_not_relaunch(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()
        flow.state.baseline_failures = []  # measured, zero failures

        with patch("se3.engine.test_baseline.compute_baseline_key") as mock_key, \
             patch("se3.engine.test_baseline.BaselineCapture") as MockCapture:
            sm._start_baseline_capture(flow)

        mock_key.assert_not_called()
        MockCapture.assert_not_called()
        assert sm._baseline_capture is None

    def test_launch_failure_does_not_raise(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()

        with patch("se3.engine.test_baseline.compute_baseline_key", side_effect=RuntimeError("boom")):
            sm._start_baseline_capture(flow)  # must not raise

        assert flow.state.baseline_failures is None


# ---------------------------------------------------------------------------
# _ensure_baseline_ready (Task 2)
# ---------------------------------------------------------------------------

class TestEnsureBaselineReady:
    def test_idempotent_when_already_measured(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()
        flow.state.baseline_failures = ["tests/test_c.py::test_z"]
        sm._baseline_capture = _fake_capture({"other"})

        sm._ensure_baseline_ready(flow)

        # Already measured → no wait, value unchanged.
        sm._baseline_capture.wait.assert_not_called()
        assert flow.state.baseline_failures == ["tests/test_c.py::test_z"]

    def test_background_success_sets_sorted_baseline(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()
        sm._baseline_capture = _fake_capture({"b::2", "a::1"})
        sm._baseline_key = "k"

        with patch("se3.engine.test_baseline.save_cache") as mock_save:
            sm._ensure_baseline_ready(flow)

        assert flow.state.baseline_failures == ["a::1", "b::2"]
        mock_save.assert_called_once()
        # handle cleared so re-entry hits the idempotent guard
        assert sm._baseline_capture is None

    def test_background_none_triggers_sync_fallback(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()
        # Background capture returns the None failure sentinel.
        sm._baseline_capture = _fake_capture(None)
        sm._baseline_key = "k"

        sync_cap = _fake_capture({"tests/test_d.py::test_real"})
        with patch("se3.engine.test_baseline.BaselineCapture", return_value=sync_cap), \
             patch("se3.engine.test_baseline.save_cache"):
            sm._ensure_baseline_ready(flow)

        # Synchronous fallback ran and produced the authoritative baseline.
        sync_cap.launch.assert_called_once()
        assert flow.state.baseline_failures == ["tests/test_d.py::test_real"]

    def test_no_handle_runs_sync_fallback(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()
        sm._baseline_capture = None  # e.g. launch failed earlier
        sm._baseline_key = "k"

        sync_cap = _fake_capture({"tests/test_e.py::test_f"})
        with patch("se3.engine.test_baseline.BaselineCapture", return_value=sync_cap), \
             patch("se3.engine.test_baseline.save_cache"):
            sm._ensure_baseline_ready(flow)

        sync_cap.launch.assert_called_once()
        assert flow.state.baseline_failures == ["tests/test_e.py::test_f"]

    def test_both_paths_fail_uses_empty_baseline(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()
        sm._baseline_capture = _fake_capture(None)  # background sentinel
        sm._baseline_key = "k"

        sync_cap = _fake_capture(None)  # sync fallback also fails
        with patch("se3.engine.test_baseline.BaselineCapture", return_value=sync_cap), \
             patch("se3.engine.test_baseline.save_cache"):
            sm._ensure_baseline_ready(flow)

        # Last resort: empty baseline (never None — so introduced detection runs).
        assert flow.state.baseline_failures == []

    def test_resolved_baseline_written_to_cache(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()
        sm._baseline_capture = _fake_capture({"x::1"})
        sm._baseline_key = "mykey"

        with patch("se3.engine.test_baseline.save_cache") as mock_save:
            sm._ensure_baseline_ready(flow)

        mock_save.assert_called_once()
        args, _ = mock_save.call_args
        # (project_root, key, failures_set)
        assert args[1] == "mykey"
        assert args[2] == {"x::1"}


# ---------------------------------------------------------------------------
# run_step wiring (Task 2)
# ---------------------------------------------------------------------------

class TestRunStepBaselineWiring:
    def test_implement_triggers_ensure_baseline(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()

        def handler(step, flow):
            return StepStatus.COMPLETED

        sm.register_handler(StepType.IMPLEMENT, handler)
        step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.PENDING)
        flow.state.add_step(step)
        flow.state.current_step_id = step.step_id

        with patch.object(sm, "_ensure_baseline_ready") as mock_ensure:
            sm.run_step(flow, step)

        mock_ensure.assert_called_once_with(flow)

    def test_non_implement_step_does_not_ensure_baseline(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()

        sm.register_handler(StepType.TEST, lambda step, flow: StepStatus.COMPLETED)
        step = Step(step_type=StepType.TEST, status=StepStatus.PENDING)
        flow.state.add_step(step)
        flow.state.current_step_id = step.step_id

        with patch.object(sm, "_ensure_baseline_ready") as mock_ensure:
            sm.run_step(flow, step)

        mock_ensure.assert_not_called()

    def test_fix_loop_reentry_does_not_remeasure(self, tmp_path):
        """A fix-loop re-entry into implement must not re-run the baseline."""
        sm = StateMachine(tmp_path)
        flow = _make_flow()
        flow.state.baseline_failures = ["already::measured"]
        sm.register_handler(StepType.IMPLEMENT, lambda step, flow: StepStatus.COMPLETED)

        step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.PENDING)
        flow.state.add_step(step)
        flow.state.current_step_id = step.step_id

        with patch("se3.engine.test_baseline.BaselineCapture") as MockCapture:
            sm.run_step(flow, step)  # _ensure_baseline_ready runs but hits guard

        MockCapture.assert_not_called()
        assert flow.state.baseline_failures == ["already::measured"]


# ---------------------------------------------------------------------------
# _build_step_inputs injection (Task 3)
# ---------------------------------------------------------------------------

class TestBuildStepInputsBaselineInjection:
    def test_test_step_receives_baseline_failures(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()
        flow.state.baseline_failures = ["tests/test_g.py::test_h"]

        inputs = sm._build_step_inputs(flow, StepType.TEST)

        assert inputs["baseline_failures"] == ["tests/test_g.py::test_h"]

    def test_verify_spec_step_receives_baseline_failures(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()
        flow.state.baseline_failures = ["tests/test_i.py::test_j"]

        inputs = sm._build_step_inputs(flow, StepType.VERIFY_SPEC)

        assert inputs["baseline_failures"] == ["tests/test_i.py::test_j"]

    def test_uncaptured_baseline_injected_as_empty_list(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = _make_flow()
        assert flow.state.baseline_failures is None  # not captured

        test_inputs = sm._build_step_inputs(flow, StepType.TEST)
        verify_inputs = sm._build_step_inputs(flow, StepType.VERIFY_SPEC)

        assert test_inputs["baseline_failures"] == []
        assert verify_inputs["baseline_failures"] == []

    def test_injected_baseline_is_a_copy(self, tmp_path):
        """Mutating the injected list must not corrupt flow state."""
        sm = StateMachine(tmp_path)
        flow = _make_flow()
        flow.state.baseline_failures = ["a::1"]

        inputs = sm._build_step_inputs(flow, StepType.TEST)
        inputs["baseline_failures"].append("b::2")

        assert flow.state.baseline_failures == ["a::1"]
