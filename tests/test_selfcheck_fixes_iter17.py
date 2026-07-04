"""Regression tests for the four self-check fixes (issue #243 / #244 一期).

Locks in:

* **Cache is bounded (disk_json_cache).** The module-level parse cache evicts
  the least-recently-used entry past a cap and drops an entry whose backing
  path no longer stats, so a long-lived daemon's RSS does not grow monotonically
  with every archive/worktree it has ever observed.
* **No orphaned cold partitions (persistence).** ``clear_resumable_snapshot``
  reclaims a ``steps/<flow_id>/`` partition once nothing references it, and a new
  run overwriting a prior COMPLETED flow's engine.json prunes that prior flow's
  partition — while a live/snapshot-referenced partition is preserved.
* **Schema covers the whole new-format header (schema.py).** Every top-level and
  ``state`` key the serializer emits is described by ``ENGINE_JSON_SCHEMA``.
* **Newest archive wins by mtime (persistence).** ``load_archived_flow_by_id``
  picks the most recent archive across the two coexisting naming schemes
  (timestamped ``engine_<ts>.json`` vs slug ``engine_<flow>.json``).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

import se3.daemon.disk_json_cache as cache_mod
from se3.daemon.disk_json_cache import read_json_cached
from se3.engine.models import FlowStatus
from se3.engine.persistence import PersistenceManager
from se3.engine.schema import ENGINE_JSON_SCHEMA
from se3.engine.state_machine import StateMachine


@pytest.fixture(autouse=True)
def _clean_cache():
    cache_mod.clear_cache()
    yield
    cache_mod.clear_cache()


# --------------------------------------------------------------------------- #
# disk_json_cache: bounded cache + deleted-path eviction
# --------------------------------------------------------------------------- #

def test_cache_evicts_deleted_path_entry(tmp_path):
    p = tmp_path / "a.json"
    p.write_text('{"flow_id": "x"}')
    assert read_json_cached(p) == {"flow_id": "x"}
    assert str(p) in cache_mod._CACHE
    p.unlink()
    # The failed stat must bypass AND drop the stale entry, not just skip it.
    assert read_json_cached(p) is None
    assert str(p) not in cache_mod._CACHE


def test_cache_is_lru_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "_MAX_CACHE_ENTRIES", 8)
    for i in range(20):
        f = tmp_path / f"{i}.json"
        f.write_text("{}")
        read_json_cached(f)
    assert len(cache_mod._CACHE) <= 8


def test_cache_lru_keeps_recently_used(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "_MAX_CACHE_ENTRIES", 4)
    files = []
    for i in range(4):
        f = tmp_path / f"{i}.json"
        f.write_text("{}")
        read_json_cached(f)
        files.append(f)
    # Touch file 0 so it becomes most-recently-used.
    read_json_cached(files[0])
    # Inserting a 5th evicts the LRU (file 1), never the freshly-touched file 0.
    extra = tmp_path / "extra.json"
    extra.write_text("{}")
    read_json_cached(extra)
    assert str(files[0]) in cache_mod._CACHE
    assert str(files[1]) not in cache_mod._CACHE


# --------------------------------------------------------------------------- #
# persistence: orphaned cold partition reclamation
# --------------------------------------------------------------------------- #

def _paused_flow_with_cold(pm: PersistenceManager, sm: StateMachine, desc: str):
    flow = sm.create_flow(desc, task_type="feature")
    sid = next(iter(flow.state.steps))
    flow.state.steps[sid].outputs = {"blob": "y" * 2048}
    flow.status = FlowStatus.PAUSED
    pm.save_flow(flow)
    return flow


def test_end_session_prunes_orphaned_partition(tmp_path):
    pm = PersistenceManager(tmp_path)
    pm.ensure_directories()
    sm = StateMachine(project_root=tmp_path)
    flow = _paused_flow_with_cold(pm, sm, "task")
    partition = pm.steps_dir / flow.flow_id
    assert partition.is_dir()

    # end-session/salvage sequence: archive (snapshot alive -> partition kept),
    # then drop the snapshot -> partition is now unreferenced and reclaimed.
    pm.clear_state()
    assert partition.is_dir(), "kept while snapshot still references it"
    pm.clear_resumable_snapshot(flow.flow_id)
    assert not partition.is_dir(), "reclaimed once snapshot gone"

    # Archive copy retains full fidelity (data was moved, not lost).
    archive_steps = pm.state_dir / "archive" / "steps"
    assert archive_steps.is_dir() and any(archive_steps.iterdir())


def test_new_run_prunes_prior_completed_partition(tmp_path):
    pm = PersistenceManager(tmp_path)
    pm.ensure_directories()
    sm = StateMachine(project_root=tmp_path)

    f1 = sm.create_flow("one", task_type="feature")
    sid = next(iter(f1.state.steps))
    f1.state.steps[sid].outputs = {"blob": "y" * 2048}
    f1.status = FlowStatus.COMPLETED
    pm.save_flow(f1)
    p1 = pm.steps_dir / f1.flow_id
    assert p1.is_dir()
    assert not (pm.resumable_dir / f"{f1.flow_id}.json").exists()

    # A fresh run overwrites engine.json; the prior completed flow's partition
    # loses its last reference and must be reclaimed.
    f2 = sm.create_flow("two", task_type="feature")
    sid2 = next(iter(f2.state.steps))
    f2.state.steps[sid2].outputs = {"blob": "z" * 2048}
    pm.save_flow(f2)
    assert not p1.is_dir(), "prior completed flow partition reclaimed"
    assert (pm.steps_dir / f2.flow_id).is_dir(), "new flow partition intact"


def test_prune_preserves_live_partition(tmp_path):
    pm = PersistenceManager(tmp_path)
    pm.ensure_directories()
    sm = StateMachine(project_root=tmp_path)
    flow = _paused_flow_with_cold(pm, sm, "task")
    partition = pm.steps_dir / flow.flow_id
    # engine.json still holds this flow -> the partition must NOT be pruned.
    pm._prune_cold_partition_if_orphan(flow.flow_id)
    assert partition.is_dir()


# --------------------------------------------------------------------------- #
# schema: full new-format header coverage
# --------------------------------------------------------------------------- #

def test_engine_schema_covers_emitted_header(tmp_path):
    pm = PersistenceManager(tmp_path)
    pm.ensure_directories()
    sm = StateMachine(project_root=tmp_path)
    flow = sm.create_flow("task", task_type="feature")
    flow.is_worktree_mode = True
    flow.worktree_path = "/tmp/wt"
    flow.worktree_original_branch = "main"
    flow.worktree_branch = "wt-x"
    flow.waiting_for_lock = True
    sid = next(iter(flow.state.steps))
    flow.state.steps[sid].outputs = {"b": "y" * 16}
    pm.save_flow(flow)

    data = json.loads((pm.state_dir / "engine.json").read_text())
    top_props = ENGINE_JSON_SCHEMA["properties"]
    state_props = top_props["state"]["properties"]
    assert [k for k in data if k not in top_props] == []
    assert [k for k in data["state"] if k not in state_props] == []
    # The specific fields the self-check flagged as missing:
    for k in ("worktree_path", "worktree_original_branch", "waiting_for_lock"):
        assert k in top_props
    for k in ("review_iterations", "fix_iterations", "baseline_failures",
              "session_token_usage"):
        assert k in state_props


# --------------------------------------------------------------------------- #
# persistence: archive selection is by recency, not lexical filename
# --------------------------------------------------------------------------- #

def test_load_archived_prefers_newest_by_mtime(tmp_path):
    pm = PersistenceManager(tmp_path)
    pm.ensure_directories()
    archive_dir = pm.state_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    flow_id = "abc123"
    base = {
        "flow_id": flow_id,
        "status": "completed",
        "task_description": "t",
        "task_type": "feature",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "completed_at": None,
        "state": {"steps": {}, "selected_steps": [], "step_history": [],
                  "current_step_id": None, "context": {}},
    }

    # Older archive under the timestamp scheme ('_' after digits).
    old = archive_dir / "engine_20260101_000000.json"
    old.write_text(json.dumps({**base, "task_description": "OLD"}))
    # Newer archive under the worktree-promotion slug scheme ('-' sorts BEFORE
    # '_', so descending-lexical would wrongly rank the timestamp file first).
    new = archive_dir / f"engine_{flow_id}-worktree.json"
    new.write_text(json.dumps({**base, "task_description": "NEW"}))

    old_t = 1_000_000_000
    new_t = 2_000_000_000
    os.utime(old, ns=(old_t, old_t))
    os.utime(new, ns=(new_t, new_t))

    loaded = pm.load_archived_flow_by_id(flow_id)
    assert loaded is not None
    assert loaded.task_description == "NEW"
