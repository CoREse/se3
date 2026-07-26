"""Regression tests for the iteration-23 self-check fixes (issue #244 一期 B3-i).

Locks in the two per-step cold-body data-loss guards that mirror the context
guard added in iteration 22:

* **A re-executed step's freshly produced body must be persisted, not lost
  (models.py / state_machine.py / run.py).** When a lazily-loaded step's cold
  file is missing/corrupt, ``apply_cold(None)`` degrades its in-memory body to
  empty and leaves ``cold_loaded`` False. If that step is then re-executed on
  resume, the execution/assignment path flips ``cold_loaded`` True, so (1) a
  later keyed access via the lazy step dict no longer re-fires the hydrator and
  wipes the freshly produced body back to ``{}``, and (2) ``_split_flow`` detects
  the changed payload and rewrites the step's cold file. A pure read-failure
  degradation (never re-executed) still keeps ``cold_loaded`` False and re-emits
  the recorded ``cold_ref`` untouched.

* **The eager reconstruct path must not clobber an intact-but-unreadable cold
  step file (persistence.py / models.py).** ``_reconstruct_full_dict``
  (load_flow / load_flow_tolerant / load_archived_flow_by_id) now emits per-step
  ``_cold_loaded`` / ``_cold_ref`` provenance markers when a step's cold read
  fails, so ``Step.from_dict`` marks the step not-loaded and preserves its
  reference — a subsequent ``save_flow`` re-emits the ``cold_ref`` instead of
  atomically overwriting the real on-disk cold file with ``{}``. The per-step
  analogue of iteration 22's ``_cold_context_loaded`` guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tianluo.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType
from tianluo.engine.persistence import PersistenceManager


def _make_flow(n_steps: int = 2) -> FlowInstance:
    flow = FlowInstance(task_description="iter23 flow", status=FlowStatus.PAUSED)
    flow.task_type = "feature"
    for i in range(n_steps):
        step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED)
        step.inputs = {"real_input": f"in-{i}", "idx": i}
        step.outputs = {"real_output": f"out-{i}"}
        flow.state.add_step(step)
    flow.state.selected_steps = [StepType.IMPLEMENT]
    flow.state.current_step_id = flow.state.step_history[0]
    return flow


def _cold_step_path(tmp_path: Path, flow_id: str, step_id: str) -> Path:
    return tmp_path / "se3" / "state" / "steps" / flow_id / f"{step_id}.json"


# -- Issue 1: a re-executed step's re-produced body survives and persists -----


def test_reexecuted_step_body_is_persisted_after_corrupt_cold_read(tmp_path):
    """A lazily-loaded step whose cold file is corrupt, then re-executed,
    must persist its freshly produced body and not be wiped by keyed access.

    Simulates the resume-mid-step scenario: load header-only, corrupt the cold
    file, mark the step loaded (as the RUNNING transition / resume assignment
    does), write a fresh body, then prove (a) a later ``steps.get`` does NOT wipe
    it and (b) ``save_flow`` rewrites the cold file with the new payload.
    """
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=2)
    victim = flow.state.step_history[0]
    pm.save_flow(flow)

    cold_file = _cold_step_path(tmp_path, flow.flow_id, victim)
    assert cold_file.exists()
    # Corrupt the current step's cold file (crash mid-write / transient blip).
    cold_file.write_text("{ not json", encoding="utf-8")

    fresh = PersistenceManager(tmp_path)
    loaded = fresh.load_flow_by_id(flow.flow_id)  # lazy, header-only
    assert loaded is not None

    # First keyed access hydrates -> apply_cold(None) degrades body to empty and
    # leaves cold_loaded False (the B3 tolerant read).
    step = loaded.state.steps.get(victim)
    assert step.inputs == {}
    assert step.outputs == {}
    assert step.cold_loaded is False

    # The step re-executes: the execution/assignment path flips cold_loaded True
    # and produces a fresh body.
    step.cold_loaded = True
    step.inputs = {"real_input": "reproduced", "idx": 0}
    step.outputs = {"real_output": "reproduced-out"}

    # A later keyed access must NOT re-fire the hydrator and wipe the new body.
    again = loaded.state.steps.get(victim)
    assert again.outputs == {"real_output": "reproduced-out"}
    assert again.inputs["real_input"] == "reproduced"

    # Persisting rewrites the cold file with the re-produced body.
    fresh.save_flow(loaded)
    written = json.loads(cold_file.read_bytes())
    assert written["outputs"] == {"real_output": "reproduced-out"}
    assert written["inputs"]["real_input"] == "reproduced"

    # And it reloads intact.
    reloaded = PersistenceManager(tmp_path).load_flow_by_id(flow.flow_id)
    assert reloaded.state.steps.get(victim).outputs == {"real_output": "reproduced-out"}


def test_pure_read_failure_does_not_clobber_lazy_cold_file(tmp_path):
    """A step that is NEVER re-executed keeps cold_loaded False and re-emits its
    cold_ref, so a transient read failure does not overwrite the intact file."""
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=2)
    victim = flow.state.step_history[0]
    pm.save_flow(flow)

    cold_file = _cold_step_path(tmp_path, flow.flow_id, victim)
    good_bytes = cold_file.read_bytes()
    good_payload = json.loads(good_bytes)

    # Transiently unreadable at load time.
    cold_file.write_text("{ not json", encoding="utf-8")

    fresh = PersistenceManager(tmp_path)
    loaded = fresh.load_flow_by_id(flow.flow_id)
    step = loaded.state.steps.get(victim)
    assert step.outputs == {}
    assert step.cold_loaded is False
    assert isinstance(step.cold_ref, dict) and step.cold_ref.get("file")

    # Blip clears: restore the real bytes.
    cold_file.write_bytes(good_bytes)

    # Re-persisting must NOT overwrite the (now intact) cold file with {}.
    fresh.save_flow(loaded)
    after = json.loads(cold_file.read_bytes())
    assert after == good_payload
    assert after["outputs"] == {"real_output": "out-0"}


# -- Issue 2: the eager loader must not clobber an unreadable cold step file --


@pytest.mark.parametrize("loader", ["load_flow", "load_flow_tolerant"])
def test_eager_load_failed_step_read_is_not_persisted_over_intact_file(tmp_path, loader):
    """The eager reconstruct path must mark a failed-read step not-loaded and
    preserve its cold_ref, so a subsequent save re-emits the reference rather
    than persisting {} over the intact on-disk cold file (B3-i, eager path)."""
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=2)
    victim = flow.state.step_history[0]
    pm.save_flow(flow)  # engine.json is the live single-slot file

    cold_file = _cold_step_path(tmp_path, flow.flow_id, victim)
    good_bytes = cold_file.read_bytes()
    good_payload = json.loads(good_bytes)

    # Corrupt the victim's cold file so the eager read degrades to empty.
    cold_file.write_text("{ not json", encoding="utf-8")

    fresh = PersistenceManager(tmp_path)
    if loader == "load_flow":
        loaded = fresh.load_flow()
    else:
        loaded, _warnings = fresh.load_flow_tolerant()
    assert loaded is not None

    # Eager path inlines empty IO for the unreadable step but marks it not-loaded
    # and keeps the reference (the fix): a legacy/genuinely-empty step would stay
    # loaded=True.
    step = loaded.state.steps.get(victim)
    assert step.inputs == {}
    assert step.outputs == {}
    assert step.cold_loaded is False
    assert isinstance(step.cold_ref, dict) and step.cold_ref.get("file")

    # A sibling step whose cold read SUCCEEDED must stay loaded and keep its body.
    other = flow.state.step_history[1]
    other_step = loaded.state.steps.get(other)
    assert other_step.cold_loaded is True
    assert other_step.outputs == {"real_output": "out-1"}

    # Transient failure clears: restore the intact cold file.
    cold_file.write_bytes(good_bytes)

    # Saving must NOT overwrite the intact cold file with the empty body.
    fresh.save_flow(loaded)
    after = json.loads(cold_file.read_bytes())
    assert after == good_payload
    assert after["outputs"] == {"real_output": "out-0"}
