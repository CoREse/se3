"""Regression tests for issue #244 一期 — engine.json hot/cold split.

Covers the acceptance surface for the Part B / G7 (B4/B5) work:

* (d) new-format ``save`` → ``load`` round-trips equal, incl. resumable snapshot;
* (e) legacy inline engine.json / resumable snapshots still load;
* (f) the engine.json header stays bounded (<100 KB) even for a flow with many
  steps carrying large inputs/outputs;
* (g) per-step persistence touches only the header + that step's cold file
  (write volume proportional to the step, not the flow);
* (h) a missing / corrupt cold step file degrades that step to empty IO without
  crashing the whole load;
* (i) resume reloads a partial new-format flow at full fidelity, and single-step
  cold data can be pulled on demand (B4);
* B5: ``clear_state`` archives header + cold files together (full fidelity) and
  ``list_all_flows`` / export keep working for both formats.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from se3.engine.models import (
    ENGINE_FORMAT_SPLIT,
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.persistence import PersistenceManager


def _make_flow(
    n_steps: int = 6,
    payload_size: int = 50_000,
    status: FlowStatus = FlowStatus.RUNNING,
    worktree: bool = False,
) -> FlowInstance:
    """Build a flow whose steps carry large inputs/outputs (like a real flow)."""
    flow = FlowInstance(task_description="split-format flow", status=status)
    flow.task_type = "feature"
    if worktree:
        flow.is_worktree_mode = True
        flow.worktree_branch = "impl/x/G1"
        flow.worktree_path = "/repo/se3/worktrees/g1"
    blob = "Q" * payload_size
    for i in range(n_steps):
        step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED)
        step.inputs = {"test_results": blob, "idx": i}
        step.outputs = {"artifact_blob": blob, "ok": True}
        flow.state.add_step(step)
    flow.state.selected_steps = [StepType.IMPLEMENT]
    flow.state.current_step_id = flow.state.step_history[-1]
    flow.state.context = {"spec_content": blob, "resolved_type": "feature"}
    flow.state.increment_fix_iteration({"reason": "big fix context", "blob": blob})
    return flow


# -- (d) new-format round-trip ---------------------------------------------


def test_new_format_save_load_roundtrip_equal(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow()
    pm.save_flow(flow)

    header = json.loads((tmp_path / "se3" / "state" / "engine.json").read_text())
    assert header["engine_format"] == ENGINE_FORMAT_SPLIT
    # The header must NOT carry per-step inputs/outputs nor the shared context.
    for step in header["state"]["steps"].values():
        assert "inputs" not in step
        assert "outputs" not in step
    assert header["state"]["context"] == {}

    loaded = pm.load_flow()
    assert loaded == flow


def test_resumable_snapshot_roundtrip_equal(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(status=FlowStatus.PAUSED)
    pm.save_flow(flow)  # non-completed → resumable snapshot written too

    snap = pm.load_resumable_snapshot(flow.flow_id)
    assert snap == flow
    # And it is discoverable via the by-id path (resumable, not active).
    assert pm.load_flow_by_id(flow.flow_id) == flow


# -- (e) legacy inline compatibility ---------------------------------------


def test_legacy_inline_engine_json_loads(tmp_path):
    pm = PersistenceManager(tmp_path)
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True)
    flow = _make_flow(n_steps=4)
    # Legacy format: full inline dict, no engine_format marker, no cold files.
    (state_dir / "engine.json").write_text(
        json.dumps(flow.to_dict(), indent=2, ensure_ascii=False, default=str)
    )
    assert not (state_dir / "steps").exists()

    loaded = pm.load_flow()
    assert loaded == flow  # inline inputs/outputs/context used verbatim


def test_legacy_inline_resumable_snapshot_loads(tmp_path):
    pm = PersistenceManager(tmp_path)
    resumable = tmp_path / "se3" / "state" / "resumable"
    resumable.mkdir(parents=True)
    flow = _make_flow(n_steps=3, status=FlowStatus.FAILED)
    (resumable / f"{flow.flow_id}.json").write_text(
        json.dumps(flow.to_dict(), indent=2, ensure_ascii=False, default=str)
    )
    assert pm.load_resumable_snapshot(flow.flow_id) == flow


# -- (f) bounded header -----------------------------------------------------


def test_header_bounded_under_100kb(tmp_path):
    pm = PersistenceManager(tmp_path)
    # 31 steps × ~700KB inputs is the real 50MB flow shape; the header must stay
    # KB-scale regardless.
    flow = _make_flow(n_steps=31, payload_size=700_000)
    pm.save_flow(flow)
    header_path = tmp_path / "se3" / "state" / "engine.json"
    assert header_path.stat().st_size < 100 * 1024


# -- (g) incremental per-step writes ---------------------------------------


def test_per_step_persistence_touches_only_changed_cold_file(tmp_path, monkeypatch):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=8, status=FlowStatus.COMPLETED)
    pm.save_flow(flow)  # first save writes all cold files

    written: list = []
    real = PersistenceManager._atomic_write_text

    def _spy(path, content):
        written.append(Path(path).name)
        real(path, content)

    monkeypatch.setattr(pm, "_atomic_write_text", _spy)

    # Mutate exactly one step's outputs, leave everything else untouched.
    target = flow.state.step_history[3]
    flow.state.steps[target].outputs["artifact_blob"] = "changed"
    pm.save_flow(flow)

    # Only the changed step's cold file is (re)written; the other 7 steps and
    # the unchanged _context.json are skipped by the sha1 guard.
    assert f"{target}.json" in written
    other_cold = [
        f"{sid}.json"
        for sid in flow.state.step_history
        if sid != target
    ]
    assert not (set(other_cold) & set(written))
    assert "_context.json" not in written


# -- (h) cold-file corruption tolerance ------------------------------------


def test_missing_cold_step_file_degrades_gracefully(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=4)
    pm.save_flow(flow)

    cold_dir = tmp_path / "se3" / "state" / "steps" / flow.flow_id
    victim = flow.state.step_history[1]
    (cold_dir / f"{victim}.json").unlink()  # simulate loss
    # Corrupt another step's cold file.
    other = flow.state.step_history[2]
    (cold_dir / f"{other}.json").write_text("{ not json")

    loaded = pm.load_flow()
    assert loaded is not None
    # The damaged steps degrade to empty IO; the rest survive intact.
    assert loaded.state.steps[victim].inputs == {}
    assert loaded.state.steps[victim].outputs == {}
    assert loaded.state.steps[other].inputs == {}
    survivor = flow.state.step_history[0]
    assert loaded.state.steps[survivor].inputs["idx"] == 0


def test_missing_cold_context_degrades_gracefully(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=2)
    pm.save_flow(flow)
    cold_dir = tmp_path / "se3" / "state" / "steps" / flow.flow_id
    (cold_dir / "_context.json").unlink()

    loaded = pm.load_flow()
    assert loaded is not None
    assert loaded.state.context == {}  # empty, not a crash


# -- (i) resume fidelity + on-demand cold load (B4) ------------------------


def test_resume_partial_flow_reloads_full_fidelity(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=5, status=FlowStatus.PAUSED)
    # Mark the flow mid-way, as a real interrupted resume would be.
    flow.state.current_step_index = 2
    flow.state.current_step_id = flow.state.step_history[2]
    pm.save_flow(flow)

    # A fresh manager (as a resuming `se3 run --resume` process would use).
    resumed = PersistenceManager(tmp_path).load_flow_by_id(flow.flow_id)
    assert resumed is not None
    assert resumed.state.current_step_id == flow.state.step_history[2]
    assert resumed.state.current_step_index == 2
    assert resumed == flow
    # Re-saving continues to write the new split format (never re-inlines).
    PersistenceManager(tmp_path).save_flow(resumed)
    header = json.loads((tmp_path / "se3" / "state" / "engine.json").read_text())
    assert header["engine_format"] == ENGINE_FORMAT_SPLIT


def test_hydrate_step_loads_only_requested_cold_file(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=5)
    pm.save_flow(flow)

    # Load header only (no cold hydration) then pull one step on demand.
    data = json.loads((tmp_path / "se3" / "state" / "engine.json").read_text())
    header_flow = FlowInstance.from_dict(data)
    assert all(s.inputs == {} for s in header_flow.state.steps.values())

    wanted = flow.state.step_history[2]
    step = pm.hydrate_step(header_flow, wanted)
    assert step is not None
    assert step.inputs["idx"] == 2
    # Only the requested step was materialized; the others stay empty.
    for sid, s in header_flow.state.steps.items():
        if sid != wanted:
            assert s.inputs == {}


# -- B5: archive equivalence + listing/export -----------------------------


def test_clear_state_archives_header_and_cold_files(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=4, status=FlowStatus.COMPLETED, worktree=True)
    pm.save_flow(flow)
    cold_dir = tmp_path / "se3" / "state" / "steps" / flow.flow_id
    assert cold_dir.is_dir()

    pm.clear_state()

    archive = tmp_path / "se3" / "state" / "archive"
    headers = list(archive.glob("engine_*.json"))
    assert len(headers) == 1
    # Header preserves format + identity; cold files followed it, full fidelity.
    archived_header = json.loads(headers[0].read_text())
    assert archived_header["flow_id"] == flow.flow_id
    assert archived_header["engine_format"] == ENGINE_FORMAT_SPLIT
    archived_cold = archive / "steps" / flow.flow_id
    assert archived_cold.is_dir()
    assert (archived_cold / "_context.json").is_file()
    assert len(list(archived_cold.glob("*.json"))) == 4 + 1  # steps + context
    # Live cold dir moved out (not left behind).
    assert not cold_dir.exists()


def test_list_all_flows_mixed_formats(tmp_path):
    pm = PersistenceManager(tmp_path)
    # Archive one new-format flow.
    new_flow = _make_flow(n_steps=3, status=FlowStatus.COMPLETED)
    pm.save_flow(new_flow)
    pm.clear_state()
    # Drop a legacy inline archive snapshot beside it.
    archive = tmp_path / "se3" / "state" / "archive"
    legacy = _make_flow(n_steps=2, status=FlowStatus.COMPLETED)
    (archive / "engine_20200101_000000.json").write_text(
        json.dumps(legacy.to_dict(), indent=2, ensure_ascii=False, default=str)
    )

    flows = pm.list_all_flows()
    ids = {f["flow_id"] for f in flows}
    assert new_flow.flow_id in ids
    assert legacy.flow_id in ids


def test_export_context_and_progress_on_new_format(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=3)
    pm.save_flow(flow)
    loaded = pm.load_flow()

    # Context export walks step inputs/outputs — hydrated cold data must be there.
    ctx_path = pm.export_context_from_flow(loaded)
    assert ctx_path.is_file()
    exported = json.loads(ctx_path.read_text())
    assert exported  # non-empty structured context

    md = pm.export_progress_markdown(loaded)
    assert flow.flow_id in md
    assert "## Steps" in md
