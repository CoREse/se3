"""Read-path tests for the engine.json hot/cold split (issue #244 一期, group G6).

These cover the *reader* half of Part B — the persistence layer detecting and
loading the new header+cold-files format alongside the legacy inline format:

* (d) new-format save→load round-trip is value-equivalent (engine.json and the
      per-flow resumable snapshot);
* (e) legacy inline engine.json / resumable snapshots still load unchanged
      (the reader is backward compatible, no store migration);
* (h) a missing or corrupt cold step file / _context.json degrades that step's
      inputs/outputs (or the shared context) to empty with a warning and never
      crashes the whole flow load;
* the split header stays KB-level (bounded) even with multi-MB step bodies, and
      the daemon hot-path fields (flow_id/status/is_worktree_mode) remain
      directly readable from the header with no cold-file access.

The new-format *writer* is Part B's write group; here we exercise the reader by
materialising fixtures through the authoritative split contract
(``PersistenceManager._split_to_new_format``).
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
from se3.engine.persistence import (
    COLD_CONTEXT_FILENAME,
    ENGINE_FORMAT_KEY,
    PersistenceManager,
)


# --------------------------------------------------------------------------
# Fixtures & helpers
# --------------------------------------------------------------------------

@pytest.fixture
def pm(tmp_path: Path) -> PersistenceManager:
    return PersistenceManager(tmp_path)


def _make_flow(
    flow_id: str = "flow-hc",
    *,
    status: FlowStatus = FlowStatus.RUNNING,
    is_worktree_mode: bool = False,
    big: int = 0,
) -> FlowInstance:
    """Build a realistic multi-step flow with non-trivial inputs/outputs/context.

    ``big`` (bytes) optionally inflates a step body so the header-bounding
    assertion has something substantial to externalise.
    """
    state = State()

    analyze = Step(step_type=StepType.ANALYZE, status=StepStatus.COMPLETED)
    analyze.inputs = {"task_description": "do a thing", "project_context": "x" * 200}
    analyze.outputs = {"task_type": "feature", "scope": "backend"}

    implement = Step(step_type=StepType.IMPLEMENT, status=StepStatus.RUNNING)
    payload = "y" * big if big else "small"
    implement.inputs = {"task_groups": [{"id": 1}], "blob": payload}
    implement.outputs = {"files_changed": ["a.py", "b.py"]}
    implement.artifacts = [Path("src/a.py")]

    for step in (analyze, implement):
        state.add_step(step)
    state.current_step_id = implement.step_id
    state.selected_steps = [StepType.ANALYZE, StepType.IMPLEMENT]
    state.current_step_index = 1
    state.context = {
        "resolved_type": "feature",
        "shared_blob": "z" * (big or 10),
        "fix_iterations": 0,
    }

    return FlowInstance(
        flow_id=flow_id,
        status=status,
        task_description="do a thing",
        task_type="feature",
        state=state,
        is_worktree_mode=is_worktree_mode,
        worktree_path="/tmp/wt" if is_worktree_mode else None,
    )


def _write_new_format(pm: PersistenceManager, flow: FlowInstance, header_path: Path) -> None:
    """Materialise ``flow`` in the new header+cold layout at ``header_path``.

    Uses the production split contract so the fixtures are byte-faithful to what
    the write group will emit; the reader under test reassembles the inverse.
    """
    header, cold_steps, context = pm._split_to_new_format(flow.to_dict())
    header_path.parent.mkdir(parents=True, exist_ok=True)
    header_path.write_text(json.dumps(header, indent=2, default=str), encoding="utf-8")

    steps_root = pm._steps_root_for(flow.flow_id)
    steps_root.mkdir(parents=True, exist_ok=True)
    for step_id, cold in cold_steps.items():
        (steps_root / f"{step_id}.json").write_text(
            json.dumps(cold, indent=2, default=str), encoding="utf-8"
        )
    (steps_root / COLD_CONTEXT_FILENAME).write_text(
        json.dumps(context, indent=2, default=str), encoding="utf-8"
    )


def _assert_equivalent(loaded: FlowInstance, original: FlowInstance) -> None:
    assert loaded is not None
    assert loaded.to_dict() == original.to_dict()


# --------------------------------------------------------------------------
# (d) new-format save→load round-trip (engine.json + resumable snapshot)
# --------------------------------------------------------------------------

def test_new_format_engine_round_trip(pm: PersistenceManager) -> None:
    flow = _make_flow("flow-new", is_worktree_mode=True)
    pm.ensure_directories()
    _write_new_format(pm, flow, pm.state_file)

    loaded = pm.load_flow()
    _assert_equivalent(loaded, flow)
    # cold bodies actually made it back
    impl = loaded.state.steps[flow.state.current_step_id]
    assert impl.outputs["files_changed"] == ["a.py", "b.py"]
    assert loaded.state.context["resolved_type"] == "feature"


def test_new_format_resumable_snapshot_round_trip(pm: PersistenceManager) -> None:
    flow = _make_flow("flow-resume", status=FlowStatus.PAUSED)
    pm.resumable_dir.mkdir(parents=True, exist_ok=True)
    _write_new_format(pm, flow, pm.resumable_dir / f"{flow.flow_id}.json")

    _assert_equivalent(pm.load_resumable_snapshot("flow-resume"), flow)
    listed = pm.list_resumable_snapshots()
    assert [f.flow_id for f in listed] == ["flow-resume"]
    _assert_equivalent(listed[0], flow)


def test_new_format_load_flow_by_id_falls_back_to_snapshot(pm: PersistenceManager) -> None:
    # engine.json holds an unrelated flow; the target lives only as a new-format
    # resumable snapshot and must still be recovered by id.
    other = _make_flow("other", status=FlowStatus.RUNNING)
    pm.ensure_directories()
    _write_new_format(pm, other, pm.state_file)

    target = _make_flow("target", status=FlowStatus.PAUSED)
    pm.resumable_dir.mkdir(parents=True, exist_ok=True)
    _write_new_format(pm, target, pm.resumable_dir / "target.json")

    _assert_equivalent(pm.load_flow_by_id("target"), target)


def test_new_format_header_is_bounded_and_hot_fields_inline(pm: PersistenceManager) -> None:
    # ~2 MiB of step body must live in cold files, not the header.
    flow = _make_flow("flow-big", is_worktree_mode=True, big=2 * 1024 * 1024)
    pm.ensure_directories()
    _write_new_format(pm, flow, pm.state_file)

    header_bytes = pm.state_file.stat().st_size
    assert header_bytes < 100 * 1024, f"header {header_bytes} bytes exceeds 100KB budget"

    # Daemon hot path: flow identity readable straight from the header, no cold I/O.
    header = json.loads(pm.state_file.read_text(encoding="utf-8"))
    assert PersistenceManager._is_new_format(header)
    assert header["flow_id"] == "flow-big"
    assert header["status"] == FlowStatus.RUNNING.value
    assert header["is_worktree_mode"] is True
    # No step body leaked into the header.
    for step in header["state"]["steps"].values():
        assert "inputs" not in step and "outputs" not in step
    assert "context" not in header["state"]

    # Full load still reconstructs the multi-MB bodies from cold files.
    loaded = pm.load_flow()
    assert len(loaded.state.steps[flow.state.current_step_id].inputs["blob"]) == 2 * 1024 * 1024


# --------------------------------------------------------------------------
# (e) legacy inline format still loads unchanged (backward-compatible reader)
# --------------------------------------------------------------------------

def test_legacy_inline_engine_round_trip(pm: PersistenceManager) -> None:
    flow = _make_flow("flow-legacy", status=FlowStatus.RUNNING)
    # save_flow still writes the legacy inline format (write group owns the flip).
    pm.save_flow(flow)

    raw = json.loads(pm.state_file.read_text(encoding="utf-8"))
    assert ENGINE_FORMAT_KEY not in raw  # genuinely old format
    assert "inputs" in next(iter(raw["state"]["steps"].values()))

    loaded = pm.load_flow()
    assert loaded is not None
    assert loaded.flow_id == "flow-legacy"
    assert loaded.state.steps[flow.state.current_step_id].outputs["files_changed"] == [
        "a.py",
        "b.py",
    ]
    assert loaded.state.context["resolved_type"] == "feature"


def test_legacy_inline_resumable_snapshot_loads(pm: PersistenceManager) -> None:
    flow = _make_flow("flow-legacy-snap", status=FlowStatus.PAUSED)
    pm.save_resumable_snapshot(flow)

    raw = json.loads((pm.resumable_dir / "flow-legacy-snap.json").read_text(encoding="utf-8"))
    assert ENGINE_FORMAT_KEY not in raw

    restored = pm.load_resumable_snapshot("flow-legacy-snap")
    assert restored is not None
    assert restored.flow_id == "flow-legacy-snap"
    assert restored.state.get_current_step().inputs["task_groups"] == [{"id": 1}]


def test_mixed_old_and_new_snapshots_list_together(pm: PersistenceManager) -> None:
    old = _make_flow("old-snap", status=FlowStatus.RUNNING)
    pm.save_resumable_snapshot(old)  # legacy inline
    new = _make_flow("new-snap", status=FlowStatus.PAUSED)
    pm.resumable_dir.mkdir(parents=True, exist_ok=True)
    _write_new_format(pm, new, pm.resumable_dir / "new-snap.json")  # new format

    ids = {f.flow_id for f in pm.list_resumable_snapshots()}
    assert ids == {"old-snap", "new-snap"}


# --------------------------------------------------------------------------
# (h) cold step file / _context.json missing or corrupt -> degrade, never crash
# --------------------------------------------------------------------------

def test_missing_cold_step_file_degrades(pm: PersistenceManager) -> None:
    flow = _make_flow("flow-miss")
    pm.ensure_directories()
    _write_new_format(pm, flow, pm.state_file)

    impl_id = flow.state.current_step_id
    (pm._steps_root_for("flow-miss") / f"{impl_id}.json").unlink()

    loaded = pm.load_flow()  # must not raise
    assert loaded is not None
    # the degraded step is emptied ...
    assert loaded.state.steps[impl_id].inputs == {}
    assert loaded.state.steps[impl_id].outputs == {}
    # ... while an intact sibling step keeps its bodies
    analyze_id = flow.state.step_history[0]
    assert loaded.state.steps[analyze_id].outputs["task_type"] == "feature"


def test_corrupt_cold_step_file_degrades(pm: PersistenceManager) -> None:
    flow = _make_flow("flow-corrupt")
    pm.ensure_directories()
    _write_new_format(pm, flow, pm.state_file)

    impl_id = flow.state.current_step_id
    (pm._steps_root_for("flow-corrupt") / f"{impl_id}.json").write_text(
        "{not valid json", encoding="utf-8"
    )

    loaded = pm.load_flow()
    assert loaded is not None
    assert loaded.state.steps[impl_id].inputs == {}
    assert loaded.state.steps[impl_id].outputs == {}


def test_corrupt_cold_context_degrades(pm: PersistenceManager) -> None:
    flow = _make_flow("flow-ctx")
    pm.ensure_directories()
    _write_new_format(pm, flow, pm.state_file)

    (pm._steps_root_for("flow-ctx") / COLD_CONTEXT_FILENAME).write_text(
        "@@garbage@@", encoding="utf-8"
    )

    loaded = pm.load_flow()
    assert loaded is not None
    assert loaded.state.context == {}
    # step bodies unaffected by a bad context file
    assert loaded.state.steps[flow.state.step_history[0]].outputs["task_type"] == "feature"


def test_tolerant_load_reports_cold_warnings(pm: PersistenceManager) -> None:
    flow = _make_flow("flow-tol")
    pm.ensure_directories()
    _write_new_format(pm, flow, pm.state_file)

    impl_id = flow.state.current_step_id
    (pm._steps_root_for("flow-tol") / f"{impl_id}.json").unlink()

    loaded, warnings = pm.load_flow_tolerant()
    assert loaded is not None
    assert loaded.state.steps[impl_id].inputs == {}
    assert any("missing" in w.lower() and impl_id in w for w in warnings)


def test_tolerant_repair_applies_to_header_only(pm: PersistenceManager) -> None:
    # A truncated header must be repaired while intact cold files load normally.
    flow = _make_flow("flow-trunc")
    pm.ensure_directories()
    _write_new_format(pm, flow, pm.state_file)

    content = pm.state_file.read_text(encoding="utf-8")
    # Drop the trailing top-level scalar fields (created_at onward), leaving a
    # dangling comma: invalid JSON that _try_repair_json closes by balancing
    # braces, exercising header-only repair while the cold files stay intact.
    pm.state_file.write_text(content[: content.index('"created_at"')], encoding="utf-8")

    loaded, warnings = pm.load_flow_tolerant()
    assert loaded is not None
    assert loaded.flow_id == "flow-trunc"
    # header repaired -> the intact cold step body is still reassembled
    assert loaded.state.steps[flow.state.step_history[0]].outputs["task_type"] == "feature"
    assert any("truncat" in w.lower() or "repair" in w.lower() or "JSON" in w for w in warnings)
