"""Tests for per-flow resumable snapshots and load-by-flow-id recovery.

Covers the engine-side half of the "any flow that *should* be resumable
actually can be resumed" guarantee (design problem 2, group G2):

* ``save_flow`` mirrors every non-COMPLETED save into
  ``se3/state/resumable/<flow_id>.json`` and removes it on COMPLETED.
* ``load_flow_by_id`` falls back to that snapshot after engine.json is
  overwritten by a later run, covering paused / interrupted (RUNNING) /
  recoverable-FAILED flows.
* the interactive resume picker (``find_resumable_snapshot_flows``) surfaces a
  snapshot-only flow and never surfaces a COMPLETED one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.persistence import PersistenceManager


def _make_flow(
    flow_id: str,
    status: FlowStatus,
    *,
    step_status: StepStatus = StepStatus.RUNNING,
    task: str = "do the thing",
) -> FlowInstance:
    """Build a minimal but realistic FlowInstance with one current step."""
    step = Step(step_type=StepType.DISCOVERY, status=step_status)
    state = State()
    state.add_step(step)
    state.current_step_id = step.step_id
    state.selected_steps = [StepType.DISCOVERY]
    return FlowInstance(
        flow_id=flow_id,
        status=status,
        task_description=task,
        state=state,
    )


@pytest.fixture
def pm(tmp_path: Path) -> PersistenceManager:
    return PersistenceManager(tmp_path)


# --------------------------------------------------------------------------
# Task 1: save_flow snapshot write/clear
# --------------------------------------------------------------------------

def test_non_completed_save_writes_snapshot(pm: PersistenceManager) -> None:
    flow = _make_flow("flowA", FlowStatus.RUNNING)
    pm.save_flow(flow)

    snapshot = pm.resumable_dir / "flowA.json"
    assert snapshot.exists()
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    assert data["flow_id"] == "flowA"
    assert data["status"] == FlowStatus.RUNNING.value


@pytest.mark.parametrize(
    "status",
    [FlowStatus.INIT, FlowStatus.RUNNING, FlowStatus.PAUSED, FlowStatus.FAILED],
)
def test_each_non_completed_status_snapshotted(
    pm: PersistenceManager, status: FlowStatus
) -> None:
    flow = _make_flow("flowS", status)
    pm.save_flow(flow)
    assert (pm.resumable_dir / "flowS.json").exists()


def test_snapshot_reflects_latest_state(pm: PersistenceManager) -> None:
    flow = _make_flow("flowB", FlowStatus.RUNNING, task="first")
    pm.save_flow(flow)

    flow.task_description = "second"
    flow.status = FlowStatus.PAUSED
    pm.save_flow(flow)

    data = json.loads((pm.resumable_dir / "flowB.json").read_text(encoding="utf-8"))
    assert data["task_description"] == "second"
    assert data["status"] == FlowStatus.PAUSED.value


def test_completed_save_clears_snapshot(pm: PersistenceManager) -> None:
    flow = _make_flow("flowC", FlowStatus.RUNNING)
    pm.save_flow(flow)
    assert (pm.resumable_dir / "flowC.json").exists()

    flow.status = FlowStatus.COMPLETED
    pm.save_flow(flow)
    assert not (pm.resumable_dir / "flowC.json").exists()


def test_snapshot_restorable_via_from_dict(pm: PersistenceManager) -> None:
    flow = _make_flow("flowD", FlowStatus.FAILED, step_status=StepStatus.FAILED)
    pm.save_resumable_snapshot(flow)

    restored = pm.load_resumable_snapshot("flowD")
    assert restored is not None
    assert restored.flow_id == "flowD"
    assert restored.status == FlowStatus.FAILED
    assert restored.state.get_current_step().status == StepStatus.FAILED


def test_load_snapshot_missing_returns_none(pm: PersistenceManager) -> None:
    assert pm.load_resumable_snapshot("nope") is None


def test_load_snapshot_corrupt_returns_none(pm: PersistenceManager) -> None:
    pm.resumable_dir.mkdir(parents=True, exist_ok=True)
    (pm.resumable_dir / "bad.json").write_text("{not json", encoding="utf-8")
    assert pm.load_resumable_snapshot("bad") is None


def test_clear_snapshot_idempotent(pm: PersistenceManager) -> None:
    # clearing a non-existent snapshot must not raise
    pm.clear_resumable_snapshot("ghost")
    flow = _make_flow("flowE", FlowStatus.RUNNING)
    pm.save_resumable_snapshot(flow)
    pm.clear_resumable_snapshot("flowE")
    pm.clear_resumable_snapshot("flowE")
    assert not (pm.resumable_dir / "flowE.json").exists()


def test_list_resumable_snapshots(pm: PersistenceManager) -> None:
    pm.save_resumable_snapshot(_make_flow("f1", FlowStatus.RUNNING))
    pm.save_resumable_snapshot(_make_flow("f2", FlowStatus.PAUSED))
    # a corrupt file is skipped, not fatal
    (pm.resumable_dir / "f3.json").write_text("garbage", encoding="utf-8")

    ids = {f.flow_id for f in pm.list_resumable_snapshots()}
    assert ids == {"f1", "f2"}


def test_list_resumable_snapshots_empty_when_no_dir(pm: PersistenceManager) -> None:
    assert pm.list_resumable_snapshots() == []


# --------------------------------------------------------------------------
# Task 2: load_flow_by_id with engine.json-overwrite fallback
# --------------------------------------------------------------------------

def test_load_flow_by_id_prefers_active_engine(pm: PersistenceManager) -> None:
    flow = _make_flow("active1", FlowStatus.RUNNING)
    pm.save_flow(flow)
    loaded = pm.load_flow_by_id("active1")
    assert loaded is not None
    assert loaded.flow_id == "active1"


def test_load_flow_by_id_falls_back_to_snapshot_after_overwrite(
    pm: PersistenceManager,
) -> None:
    # Flow A is paused mid-run; its snapshot is written by save_flow.
    flow_a = _make_flow("A", FlowStatus.PAUSED)
    pm.save_flow(flow_a)

    # A later run overwrites the single-slot engine.json with flow B.
    flow_b = _make_flow("B", FlowStatus.RUNNING)
    pm.save_flow(flow_b)

    # engine.json now holds B, but A is still resumable from its snapshot.
    active = pm.load_flow()
    assert active.flow_id == "B"

    recovered = pm.load_flow_by_id("A")
    assert recovered is not None
    assert recovered.flow_id == "A"
    assert recovered.status == FlowStatus.PAUSED


@pytest.mark.parametrize(
    "flow_status,step_status",
    [
        (FlowStatus.PAUSED, StepStatus.PAUSED),
        (FlowStatus.RUNNING, StepStatus.RUNNING),
        (FlowStatus.FAILED, StepStatus.FAILED),
    ],
)
def test_three_resumable_categories_recover_from_snapshot(
    pm: PersistenceManager, flow_status: FlowStatus, step_status: StepStatus
) -> None:
    flow = _make_flow("victim", flow_status, step_status=step_status)
    pm.save_flow(flow)
    # overwrite engine.json with another flow
    pm.save_flow(_make_flow("other", FlowStatus.RUNNING))

    recovered = pm.load_flow_by_id("victim")
    assert recovered is not None
    assert recovered.status == flow_status
    assert recovered.state.get_current_step().status == step_status


def test_load_flow_by_id_unknown_returns_none(pm: PersistenceManager) -> None:
    pm.save_flow(_make_flow("known", FlowStatus.RUNNING))
    assert pm.load_flow_by_id("does-not-exist") is None


def test_completed_flow_not_resumable_via_snapshot(pm: PersistenceManager) -> None:
    flow = _make_flow("done", FlowStatus.RUNNING)
    pm.save_flow(flow)
    flow.status = FlowStatus.COMPLETED
    pm.save_flow(flow)
    # engine.json is overwritten by a later run
    pm.save_flow(_make_flow("next", FlowStatus.RUNNING))

    # The completed flow's snapshot was cleared, so it cannot be resurrected.
    assert pm.load_flow_by_id("done") is None


# --------------------------------------------------------------------------
# resume write-back + breakpoint bookkeeping (Task 2 integration with run.py)
# --------------------------------------------------------------------------

def test_resume_writes_snapshot_back_to_engine_json(pm: PersistenceManager) -> None:
    """Mirror run.py's resume branch: a snapshot-only flow is reactivated."""
    flow_a = _make_flow("resumeme", FlowStatus.FAILED, step_status=StepStatus.FAILED)
    pm.save_flow(flow_a)
    pm.save_flow(_make_flow("intervening", FlowStatus.RUNNING))

    # engine.json holds the intervening flow.
    assert pm.load_flow().flow_id == "intervening"

    # Resume path: locate by id, then write back as the active engine.json
    # when engine.json no longer holds it.
    flow = pm.load_flow_by_id("resumeme")
    assert flow is not None
    active = pm.load_flow()
    if active is None or active.flow_id != flow.flow_id:
        pm.save_flow(flow)

    assert pm.load_flow().flow_id == "resumeme"

    # Breakpoint bookkeeping (failed step reset) survives a save.
    step = flow.state.get_current_step()
    step.status = StepStatus.PENDING
    flow.status = FlowStatus.RUNNING
    pm.save_flow(flow)
    reloaded = pm.load_flow()
    assert reloaded.status == FlowStatus.RUNNING
    assert reloaded.state.get_current_step().status == StepStatus.PENDING


# --------------------------------------------------------------------------
# Task 3: interactive resume picker surfaces snapshot-only flows
# --------------------------------------------------------------------------

def test_picker_lists_snapshot_only_flow(tmp_path: Path) -> None:
    from se3.commands.run import find_resumable_snapshot_flows

    pm = PersistenceManager(tmp_path)
    pm.save_flow(_make_flow("paused1", FlowStatus.PAUSED))
    # engine.json overwritten by another run
    pm.save_flow(_make_flow("running1", FlowStatus.RUNNING))

    ids = {f["id"] for f in find_resumable_snapshot_flows(tmp_path)}
    assert "paused1" in ids


def test_picker_excludes_completed_flow(tmp_path: Path) -> None:
    from se3.commands.run import find_resumable_snapshot_flows

    pm = PersistenceManager(tmp_path)
    flow = _make_flow("c1", FlowStatus.RUNNING)
    pm.save_flow(flow)
    flow.status = FlowStatus.COMPLETED
    pm.save_flow(flow)

    ids = {f["id"] for f in find_resumable_snapshot_flows(tmp_path)}
    assert "c1" not in ids


def test_picker_entry_shape(tmp_path: Path) -> None:
    from se3.commands.run import find_resumable_snapshot_flows

    pm = PersistenceManager(tmp_path)
    pm.save_flow(_make_flow("shape1", FlowStatus.FAILED, task="describe me"))

    entries = find_resumable_snapshot_flows(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["id"] == "shape1"
    assert entry["status"] == FlowStatus.FAILED.value
    assert entry["description"] == "describe me"
    assert entry["current_step"] is not None
