"""Regression tests for the iteration-18 self-check fixes (issue #243 / #244 一期).

Locks in the fixes for:

* **apply_cold must not persist a failed read as truth (models.py).** A transient
  cold-file read error during lazy hydration degrades the in-memory step to empty
  but leaves ``cold_loaded`` False, so the next ``save_flow`` re-emits the recorded
  ``cold_ref`` verbatim instead of atomically overwriting an intact cold file with
  ``{}`` — a momentary EACCES/EIO/NFS blip no longer permanently destroys data.
* **Eager/lazy loaders agree on a damaged cold context (persistence.py).** A
  parseable-but-null ``_context.json`` degrades to empty on BOTH ``load_flow`` and
  ``load_flow_by_id`` rather than crashing the eager path with an uncaught
  ``TypeError``.
* **Structurally corrupt headers degrade, never raise (persistence.py).** A valid
  JSON header whose ``state``/``steps``/step-entry is a non-dict does not raise
  ``AttributeError`` out of ``load_flow_tolerant`` (never-raises contract) nor out
  of ``save_flow``'s ``_prior_cold_hashes`` (which would stop the flow persisting).
* **The engine-side size-guarded header scanner (persistence._read_snapshot_header)**
  recovers head keys + a tail ``is_worktree_mode`` from an oversized legacy file
  without a full parse, and returns None when ``flow_id`` is absent.
* **No seam-spanning garbage (persistence + disk_json_cache).** A >128 KiB
  ``task_description`` in an oversized file is a clean miss, never captured as a
  boundary-spanning garbage fragment.
* **clear_state stamps a colliding archive's cold_partition atomically with
  publication (persistence.py).** The published archived header already points at
  its own suffixed cold partition, not an older sibling's.
* **Point (i) integration:** a resumed new-format flow driven through the REAL
  PersistenceManager + a real step handler receives the previous step's
  cold-file-resident outputs and runs to COMPLETED.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tianluo.daemon.disk_json_cache as cache_mod
from tianluo.daemon.disk_json_cache import read_engine_header
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.persistence import (
    PersistenceManager,
    _read_snapshot_header,
    LIST_MAX_PARSE_BYTES,
)
from tianluo.engine.state_machine import StateMachine


@pytest.fixture(autouse=True)
def _clean_cache():
    cache_mod.clear_cache()
    yield
    cache_mod.clear_cache()


# --------------------------------------------------------------------------- #
# apply_cold: a failed read never becomes new on-disk truth
# --------------------------------------------------------------------------- #

def test_apply_cold_failed_read_leaves_step_unloaded():
    """None (read failure) degrades in-memory IO but does NOT mark loaded."""
    step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED)
    step.cold_ref = {"file": f"{step.step_id}.json", "hash": "abc"}
    step.cold_loaded = False

    step.apply_cold(None)

    assert step.inputs == {}
    assert step.outputs == {}
    assert step.artifacts == []
    # The step is intentionally still "not loaded": a later persist must re-emit
    # the cold_ref verbatim, not hash the empty body as a change.
    assert step.cold_loaded is False


def test_apply_cold_successful_read_marks_loaded():
    step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED)
    step.cold_loaded = False
    step.apply_cold({"inputs": {"a": 1}, "outputs": {"b": 2}, "artifacts": []})
    assert step.inputs == {"a": 1}
    assert step.outputs == {"b": 2}
    assert step.cold_loaded is True


def test_transient_cold_read_does_not_destroy_intact_file(tmp_path, monkeypatch):
    """End-to-end: a transient read failure while hydrating a completed step must
    not let the next save overwrite the intact cold file with empties."""
    pm = PersistenceManager(tmp_path)
    pm.ensure_directories()

    flow = FlowInstance(task_description="t", task_type="feature",
                        status=FlowStatus.RUNNING)
    flow.state.selected_steps = [StepType.IMPLEMENT, StepType.COMMIT]
    done = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED,
                step_id="impl-1", outputs={"result_blob": "REAL" * 100})
    flow.state.add_step(done)
    live = Step(step_type=StepType.COMMIT, status=StepStatus.RUNNING,
                step_id="commit-1")
    flow.state.add_step(live)
    flow.state.current_step_id = "commit-1"
    pm.save_flow(flow)

    cold_file = pm.steps_dir / flow.flow_id / "impl-1.json"
    assert cold_file.exists()
    original = cold_file.read_text()

    # Resume header-first (lazy), then simulate a transient read failure for the
    # completed step's cold file at the moment it is hydrated.
    resumed = pm.load_flow_by_id(flow.flow_id)
    assert resumed is not None
    monkeypatch.setattr(
        PersistenceManager, "_read_cold_json",
        staticmethod(lambda path, label, warnings=None: None),
    )
    hydrated = resumed.state.steps.get("impl-1")
    assert hydrated.outputs == {}  # degraded in memory
    assert hydrated.cold_loaded is False

    # The very next persist must NOT rewrite the intact cold file with empties.
    pm.save_flow(resumed)
    assert cold_file.read_text() == original

    # Once the transient error clears, the real outputs are still on disk.
    monkeypatch.undo()
    reloaded = pm.load_flow_by_id(flow.flow_id)
    assert reloaded.state.steps.get("impl-1").outputs == {"result_blob": "REAL" * 100}


# --------------------------------------------------------------------------- #
# eager/lazy loaders agree on a damaged cold context (null-guarded)
# --------------------------------------------------------------------------- #

def test_null_cold_context_degrades_on_both_loaders(tmp_path):
    pm = PersistenceManager(tmp_path)
    pm.ensure_directories()
    flow = FlowInstance(task_description="t", task_type="feature",
                        status=FlowStatus.RUNNING)
    flow.state.selected_steps = [StepType.IMPLEMENT]
    step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED,
                step_id="impl-1", outputs={"x": 1})
    flow.state.add_step(step)
    flow.state.current_step_id = "impl-1"
    flow.state.context = {"k": "v"}
    pm.save_flow(flow)

    # Damage the cold context so it parses but holds explicit nulls.
    ctx = pm.steps_dir / flow.flow_id / PersistenceManager.CONTEXT_COLD_FILENAME
    ctx.write_text(json.dumps({"context": None, "fix_history": None}))

    eager = pm.load_flow()                       # was crashing with TypeError
    lazy = pm.load_flow_by_id(flow.flow_id)
    assert eager is not None and lazy is not None
    assert eager.state.context == {}
    assert eager.state.fix_history == []
    assert lazy.state.context == {}
    assert lazy.state.fix_history == []


# --------------------------------------------------------------------------- #
# structurally corrupt headers degrade, never raise
# --------------------------------------------------------------------------- #

def test_load_flow_tolerant_never_raises_on_nondict_steps(tmp_path):
    pm = PersistenceManager(tmp_path)
    pm.ensure_directories()
    # Valid JSON, but 'steps' is a string and the header is marked hot/cold.
    pm.state_file.write_text(json.dumps({
        "engine_format": "hotcold/1",
        "flow_id": "x",
        "status": "running",
        "state": {"steps": "corrupt", "selected_steps": [], "step_history": []},
    }))
    flow, warnings = pm.load_flow_tolerant()  # must NOT raise AttributeError
    # It degrades (returns None-or-flow) with warnings, never crashes.
    assert isinstance(warnings, list)


def test_load_flow_tolerant_never_raises_on_nondict_step_entry(tmp_path):
    pm = PersistenceManager(tmp_path)
    pm.ensure_directories()
    pm.state_file.write_text(json.dumps({
        "engine_format": "hotcold/1",
        "flow_id": "x",
        "status": "running",
        "state": {"steps": {"s0": "not-a-dict"}, "selected_steps": [],
                  "step_history": []},
    }))
    flow, warnings = pm.load_flow_tolerant()  # must NOT raise AttributeError
    assert isinstance(warnings, list)


def test_save_flow_survives_corrupt_prior_header(tmp_path):
    """A non-dict 'state' in the prior header must not stop the next persist."""
    pm = PersistenceManager(tmp_path)
    pm.ensure_directories()
    flow = FlowInstance(task_description="t", task_type="feature",
                        status=FlowStatus.RUNNING)
    flow.state.selected_steps = [StepType.IMPLEMENT]
    step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.RUNNING,
                step_id="impl-1", outputs={"x": 1})
    flow.state.add_step(step)
    flow.state.current_step_id = "impl-1"
    # Corrupt the on-disk header the same flow_id was last written to.
    pm.state_file.write_text(json.dumps({
        "engine_format": "hotcold/1",
        "flow_id": flow.flow_id,
        "state": "not-a-dict",
    }))
    # _prior_cold_hashes must degrade to ({}, None), not raise AttributeError.
    pm.save_flow(flow)
    reloaded = pm.load_flow()
    assert reloaded is not None
    assert reloaded.state.steps.get("impl-1").outputs == {"x": 1}


# --------------------------------------------------------------------------- #
# engine-side _read_snapshot_header: size-guarded head+tail scan
# --------------------------------------------------------------------------- #

def _write_oversized(
    path: Path,
    *,
    flow_id: str = "flow-A",
    include_flow_id: bool = True,
    worktree: bool = True,
    huge_task_description: bool = False,
) -> None:
    """Write a >guard indent=2 JSON with a multi-MiB nested ``state`` block.

    Top-level identity keys sit in the file head; ``is_worktree_mode`` sits at the
    very tail after the giant ``state``. A nested (6-space) ``is_worktree_mode``
    copy is planted inside ``state`` to prove it is never misread as top-level.
    """
    lines = ["{"]
    if include_flow_id:
        lines.append(f'  "flow_id": "{flow_id}",')
    lines.append('  "status": "running",')
    if huge_task_description:
        # >128 KiB single-line string: its closing quote lands past the head
        # window and in the unread middle gap, so a seam-spanning match would
        # capture garbage without the \n-exclusion fix.
        lines.append('  "task_description": "' + ("D" * (200 * 1024)) + '",')
    else:
        lines.append('  "task_description": "small",')
    lines.append('  "state": {')
    lines.append('    "steps": {')
    lines.append('      "s0": {')
    # A nested is_worktree_mode with the OPPOSITE value: must not be picked up.
    lines.append('        "is_worktree_mode": false,')
    # One giant single-line string blob to blow past the 5 MiB guard.
    lines.append('        "blob": "' + ("Q" * (6 * 1024 * 1024)) + '"')
    lines.append('      }')
    lines.append('    }')
    lines.append('  },')
    lines.append(f'  "is_worktree_mode": {"true" if worktree else "false"}')
    lines.append("}")
    path.write_text("\n".join(lines), encoding="utf-8")


def test_read_snapshot_header_degraded_head_and_tail(tmp_path):
    path = tmp_path / "engine_big.json"
    _write_oversized(path, flow_id="flow-A", worktree=True)
    assert path.stat().st_size > LIST_MAX_PARSE_BYTES

    hdr = _read_snapshot_header(path)
    assert hdr is not None
    # Head keys.
    assert hdr["flow_id"] == "flow-A"
    assert hdr["status"] == "running"
    # Tail key recovered without a full parse (nested false copy ignored).
    assert hdr["is_worktree_mode"] is True
    # Degraded (never fully parsed): the giant 'state' is absent from the result.
    assert "state" not in hdr


def test_read_snapshot_header_missing_flow_id_returns_none(tmp_path):
    path = tmp_path / "engine_noid.json"
    _write_oversized(path, include_flow_id=False)
    assert path.stat().st_size > LIST_MAX_PARSE_BYTES
    assert _read_snapshot_header(path) is None


def test_read_snapshot_header_under_guard_parses_whole(tmp_path):
    path = tmp_path / "engine_small.json"
    payload = {"flow_id": "y", "status": "completed", "state": {"steps": {}}}
    path.write_text(json.dumps(payload, indent=2))
    hdr = _read_snapshot_header(path)
    assert hdr == payload  # small files parse whole, 'state' preserved


# --------------------------------------------------------------------------- #
# no seam-spanning garbage for an oversized task_description (both twins)
# --------------------------------------------------------------------------- #

def test_engine_scanner_no_seam_spanning_garbage(tmp_path):
    path = tmp_path / "engine_huge_desc.json"
    _write_oversized(path, flow_id="flow-B", huge_task_description=True)
    hdr = _read_snapshot_header(path)
    assert hdr is not None
    # Identity still recovered; the truncated description is a clean miss, never
    # a multi-KiB garbage fragment spanning the head/tail seam.
    assert hdr["flow_id"] == "flow-B"
    assert hdr["status"] == "running"
    desc = hdr.get("task_description")
    assert desc is None or "\n" not in desc


def test_daemon_scanner_no_seam_spanning_garbage(tmp_path):
    path = tmp_path / "engine_huge_desc.json"
    _write_oversized(path, flow_id="flow-C", huge_task_description=True)
    hdr = read_engine_header(path)
    assert hdr is not None
    assert hdr["flow_id"] == "flow-C"
    assert hdr["status"] == "running"
    assert hdr.get("is_worktree_mode") is True
    desc = hdr.get("task_description")
    assert desc is None or "\n" not in desc


# --------------------------------------------------------------------------- #
# clear_state: a colliding archive's cold_partition is stamped atomically
# --------------------------------------------------------------------------- #

def test_clear_state_collision_stamps_partition_before_publish(tmp_path):
    pm = PersistenceManager(tmp_path)
    pm.ensure_directories()
    sm = StateMachine(project_root=tmp_path)
    flow = sm.create_flow("collide", task_type="feature")
    sid = next(iter(flow.state.steps))
    flow.state.steps[sid].outputs = {"blob": "NEWDATA"}
    flow.status = FlowStatus.COMPLETED
    pm.save_flow(flow)

    # Pre-seed a prior archive's cold partition at archive/steps/<flow_id> holding
    # DIFFERENT data, forcing a collision so this archive must route to a suffixed
    # partition and stamp it into the published header.
    archive_steps = pm.state_dir / "archive" / "steps"
    (archive_steps / flow.flow_id).mkdir(parents=True, exist_ok=True)
    (archive_steps / flow.flow_id / f"{sid}.json").write_text(
        json.dumps({"inputs": {}, "outputs": {"blob": "OLDDATA"}, "artifacts": []})
    )
    (archive_steps / flow.flow_id / PersistenceManager.CONTEXT_COLD_FILENAME).write_text(
        json.dumps({"context": {}, "fix_history": []})
    )

    pm.clear_state()

    # The newly published archive header must carry its own suffixed partition,
    # so its cold_refs resolve to NEWDATA (not the pre-seeded OLDDATA sibling).
    archived = pm.load_archived_flow_by_id(flow.flow_id)
    assert archived is not None
    assert archived.state.steps.get(sid).outputs == {"blob": "NEWDATA"}

    # And the on-disk header actually records cold_partition (stamped, not left
    # to a post-publish second write that a crash could skip).
    headers = list((pm.state_dir / "archive").glob("engine_*.json"))
    stamped = [
        h for h in headers
        if isinstance((json.loads(h.read_text()).get("state") or {}), dict)
        and (json.loads(h.read_text()).get("state") or {}).get("cold_partition")
    ]
    assert stamped, "collision archive header must record its cold_partition"


# --------------------------------------------------------------------------- #
# point (i): resume through the REAL manager feeds prior cold outputs to done
# --------------------------------------------------------------------------- #

def test_resume_new_format_feeds_prior_cold_outputs_to_completion(tmp_path):
    """End-to-end resume across the REAL PersistenceManager + a real step handler
    that consumes a prior step's cold-file-resident outputs, run to COMPLETED.

    Guards the keyed-access hydration contract: a refactor iterating steps.values()
    (which the lazy dict does not hydrate) would feed the resumed step empty inputs
    while the persistence-level tests stayed green.
    """
    sm = StateMachine(project_root=tmp_path)
    pm = sm.persistence
    pm.ensure_directories()

    blob = "Z" * 40_000
    flow = FlowInstance(task_description="resume integ", task_type="feature",
                        status=FlowStatus.RUNNING)
    flow.state.selected_steps = [StepType.ANALYZE, StepType.PLAN]
    analyze = Step(step_type=StepType.ANALYZE, status=StepStatus.COMPLETED,
                   step_id="analyze-1",
                   outputs={"task_type": "feature", "big": blob})
    flow.state.add_step(analyze)
    plan = Step(step_type=StepType.PLAN, status=StepStatus.RUNNING,
                step_id="plan-1")
    flow.state.add_step(plan)
    flow.state.current_step_id = "plan-1"
    flow.state.current_step_index = 1
    pm.save_flow(flow)

    # The predecessor's outputs genuinely left the KB header for a cold file.
    assert (pm.steps_dir / flow.flow_id / "analyze-1.json").exists()
    header = json.loads(pm.state_file.read_text())
    assert "big" not in json.dumps(header["state"]["steps"]["analyze-1"])

    # Resume header-first via the REAL manager (lazy, keyed-access hydration).
    resumed = pm.load_flow_by_id(flow.flow_id)
    assert resumed is not None

    seen = {}

    def plan_handler(step, fl):
        prior = fl.state.steps.get("analyze-1")   # keyed access hydrates cold
        seen["outputs"] = dict(prior.outputs)
        step.outputs["planned"] = True
        return StepStatus.COMPLETED

    sm.register_handler(StepType.PLAN, plan_handler)

    current = resumed.state.get_current_step()
    current.status = StepStatus.PENDING  # mirror run_flow's resume flip
    status = sm.run_step(resumed, current)
    assert status == StepStatus.COMPLETED
    # The handler received the predecessor's cold-resident outputs, not empties.
    assert seen["outputs"].get("big") == blob
    assert seen["outputs"].get("task_type") == "feature"

    nxt = sm.transition_to_next(resumed)
    assert nxt is None
    assert resumed.status == FlowStatus.COMPLETED
