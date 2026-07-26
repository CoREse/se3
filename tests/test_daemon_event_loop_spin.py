"""Regression tests for daemon event-loop spin caused by expensive repeated work.

The daemon client's ``_push_loop`` calls ``build_index`` every fast tick
(1 s) via ``_push_history``.  On a machine with a large ``tianluo/history``
tree the full directory walk + JSON parse is expensive enough to saturate
thread-pool workers and starve the event loop of CPU — the same class of
stall the aggregator's ``HISTORICAL_ROOTS_TTL`` fixed for
``all_project_roots`` (commit f0f3f44, v8.5.1).

This file encodes the spin trigger condition as bounded / counting assertions:

* ``build_index`` disk I/O must be collapsed to at most one execution per
  :data:`~tianluo.daemon.history.BUILD_INDEX_TTL` window, regardless of how
  many times the caller invokes it.
* ``all_project_roots`` disk I/O for historical-root enumeration must be
  collapsed to at most one execution per
  :data:`~tianluo.daemon.aggregator.HISTORICAL_ROOTS_TTL` window.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import tianluo.daemon.aggregator as agg_mod
import tianluo.daemon.history as history_mod
from tianluo.daemon.aggregator import DaemonAggregator
from tianluo.daemon.history import BUILD_INDEX_TTL, DaemonHistoryReader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, lines: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def _write_engine(root: Path, flow_id: str, status: str) -> None:
    state_dir = root / "tianluo" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": flow_id, "status": status}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# build_index TTL cache
# ---------------------------------------------------------------------------


def test_build_index_caches_within_ttl(tmp_path):
    """Repeated ``build_index`` calls within the TTL window trigger at most one
    disk walk.  This is the primary defence against the event-loop spin: the
    daemon client's per-tick ``_push_history`` must not re-read the full
    history tree every second."""
    root = tmp_path / "proj"
    _write_engine(root, "f1", "RUNNING")
    _write_jsonl(
        root / "tianluo" / "history" / "f1" / "01_analyze.jsonl",
        [_msg("user", "hello")],
    )

    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])
    # Monkey-patch _index_root to count invocations.
    index_count = {"n": 0}
    original = reader._index_root

    def counting_index_root(r, metas, seen):
        index_count["n"] += 1
        return original(r, metas, seen)

    reader._index_root = counting_index_root  # type: ignore[assignment]

    # Multiple rapid calls within the TTL window.
    r1 = reader.build_index()
    r2 = reader.build_index()
    r3 = reader.build_index()

    # Only one actual disk walk despite three calls.
    assert index_count["n"] == 1
    # All calls return the same cached object.
    assert r1 is r2 is r3


def test_build_index_rebuilds_after_ttl(tmp_path):
    """Once the TTL elapses, ``build_index`` rebuilds from disk."""
    root = tmp_path / "proj"
    _write_engine(root, "f1", "RUNNING")
    _write_jsonl(
        root / "tianluo" / "history" / "f1" / "01_analyze.jsonl",
        [_msg("user", "hello")],
    )

    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])
    index_count = {"n": 0}
    original = reader._index_root

    def counting_index_root(r, metas, seen):
        index_count["n"] += 1
        return original(r, metas, seen)

    reader._index_root = counting_index_root  # type: ignore[assignment]

    reader.build_index()
    assert index_count["n"] == 1

    # Advance past the TTL.
    reader._index_cache_at -= BUILD_INDEX_TTL + 1

    reader.build_index()
    assert index_count["n"] == 2


def test_build_index_cache_invalidation(tmp_path):
    """``invalidate_index_cache`` forces the next call to rebuild."""
    root = tmp_path / "proj"
    _write_engine(root, "f1", "RUNNING")
    _write_jsonl(
        root / "tianluo" / "history" / "f1" / "01_analyze.jsonl",
        [_msg("user", "hello")],
    )

    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])
    index_count = {"n": 0}
    original = reader._index_root

    def counting_index_root(r, metas, seen):
        index_count["n"] += 1
        return original(r, metas, seen)

    reader._index_root = counting_index_root  # type: ignore[assignment]

    reader.build_index()
    assert index_count["n"] == 1

    # Explicit invalidation.
    reader.invalidate_index_cache()
    reader.build_index()
    assert index_count["n"] == 2


def test_build_index_cache_returns_fresh_data_after_invalidation(tmp_path):
    """After invalidation, build_index reflects newly added history-only flows."""
    root = tmp_path / "proj"
    # First flow: history-only (no engine.json needed).
    _write_jsonl(
        root / "tianluo" / "history" / "f1" / "01_analyze.jsonl",
        [_msg("user", "hello")],
    )

    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])
    r1 = reader.build_index()
    assert [m.flow_id for m in r1] == ["f1"]

    # Add a new history-only flow and invalidate the cache.
    _write_jsonl(
        root / "tianluo" / "history" / "f2" / "01_analyze.jsonl",
        [_msg("user", "world")],
    )
    reader.invalidate_index_cache()

    r2 = reader.build_index()
    assert set(m.flow_id for m in r2) == {"f1", "f2"}


# ---------------------------------------------------------------------------
# _is_still_active flow_id guard
# ---------------------------------------------------------------------------


def test_is_still_active_rejects_stale_flow_when_different_flow_is_active(tmp_path):
    """_is_still_active must return False when the engine.json describes a
    *different* flow_id, even if that flow's status is active.  Without the
    flow_id check, a stale meta for a completed flow F1 would read F2's
    RUNNING status and incorrectly return True."""
    root = tmp_path / "proj"
    # F1 was the active flow, now completed.
    _write_engine(root, "f1", "COMPLETED")
    # F2 is the new active flow.
    _write_engine(root, "f2", "RUNNING")

    f1_meta = history_mod.SessionMeta(
        flow_id="f1",
        project_root=str(root),
        active=True,
        source="active",
        status="COMPLETED",
    )
    f2_meta = history_mod.SessionMeta(
        flow_id="f2",
        project_root=str(root),
        active=True,
        source="active",
        status="RUNNING",
    )

    # F1 is no longer the engine.json flow — must be inactive.
    assert DaemonHistoryReader._is_still_active(f1_meta) is False
    # F2 is the current engine.json flow and RUNNING — must be active.
    assert DaemonHistoryReader._is_still_active(f2_meta) is True


def test_is_still_active_returns_false_for_non_active_source(tmp_path):
    """Archived / history-only metas are never re-checked as active."""
    root = tmp_path / "proj"
    _write_engine(root, "f1", "RUNNING")

    meta = history_mod.SessionMeta(
        flow_id="f1",
        project_root=str(root),
        active=True,
        source="archived",
    )
    assert DaemonHistoryReader._is_still_active(meta) is False


def test_is_still_active_returns_false_when_engine_json_missing(tmp_path):
    """When engine.json is gone, the flow is not active."""
    root = tmp_path / "proj"
    meta = history_mod.SessionMeta(
        flow_id="f1",
        project_root=str(root / "nonexistent"),
        active=True,
        source="active",
    )
    assert DaemonHistoryReader._is_still_active(meta) is False


# ---------------------------------------------------------------------------
# read_active_flows correctness with flow_id mismatch
# ---------------------------------------------------------------------------


def test_read_active_flows_drops_stale_flow_after_flow_change(tmp_path):
    """When F1 completes and F2 starts in the same root, read_active_flows
    must report F2 (not F1) as the active flow."""
    root = tmp_path / "proj"
    # Initially F1 is active.
    _write_engine(root, "f1", "RUNNING")
    _write_jsonl(
        root / "tianluo" / "history" / "f1" / "01_analyze.jsonl",
        [_msg("user", "hello")],
    )

    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])
    # Prime the cache.
    reader.build_index()

    # F1 completes, F2 starts.
    _write_engine(root, "f2", "RUNNING")
    _write_jsonl(
        root / "tianluo" / "history" / "f2" / "01_discovery.jsonl",
        [_msg("user", "new task")],
    )
    # Advance past TTL so the next build_index picks up F2.
    reader._index_cache_at -= BUILD_INDEX_TTL + 1

    reads = reader.read_active_flows(cursors={})
    active_ids = {r.flow_id for r in reads}
    # F2 should be active; F1 should not.
    assert "f2" in active_ids
    assert "f1" not in active_ids


# ---------------------------------------------------------------------------
# all_project_roots historical enumeration TTL (existing defence)
# ---------------------------------------------------------------------------


def test_all_project_roots_historical_walk_bounded_per_ttl(monkeypatch):
    """The aggregator's ``all_project_roots`` runs at most one disk walk for
    historical-root enumeration per HISTORICAL_ROOTS_TTL window.  This is the
    defence added in f0f3f44 (v8.5.1); this test persists as a regression
    guard."""
    calls: list = []

    def spy(base):
        calls.append(1)
        return []

    monkeypatch.setattr(agg_mod, "enumerate_historical_project_roots", spy)

    aggregator = DaemonAggregator()
    aggregator.set_project_roots(["/p/one"])

    # Many rapid calls.
    for _ in range(20):
        aggregator.all_project_roots()

    # Only one disk enumeration despite 20 calls.
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Simulated daemon tick cadence: combined poll + push does not spin
# ---------------------------------------------------------------------------


def test_combined_poll_push_cadence_bounded(tmp_path):
    """Simulate the daemon's poll loop (every 2s) + client push loop (every 1s)
    calling ``get_snapshot`` and ``build_index`` repeatedly.  The total number
    of expensive disk operations must be bounded, not grow linearly with tick
    count."""
    root = tmp_path / "proj"
    _write_engine(root, "f1", "RUNNING")
    _write_jsonl(
        root / "tianluo" / "history" / "f1" / "01_analyze.jsonl",
        [_msg("user", "hello")],
    )

    # Aggregator: set up the historical-roots enumeration counter.
    agg_enum_count = {"n": 0}
    original_enum = agg_mod.enumerate_historical_project_roots

    def counting_enum(base):
        agg_enum_count["n"] += 1
        return original_enum(base)

    monkeypatch_enum = counting_enum

    import unittest.mock

    with unittest.mock.patch.object(
        agg_mod, "enumerate_historical_project_roots", side_effect=counting_enum
    ):
        aggregator = DaemonAggregator()
        aggregator.add_project_root(root)

        reader = DaemonHistoryReader(
            project_roots_provider=lambda: aggregator.all_project_roots()
        )
        index_count = {"n": 0}
        original_index = reader._index_root

        def counting_index_root(r, metas, seen):
            index_count["n"] += 1
            return original_index(r, metas, seen)

        reader._index_root = counting_index_root  # type: ignore[assignment]

        # Simulate 30 ticks (30 seconds of daemon operation).
        for _ in range(30):
            # Poll loop: get_snapshot -> all_project_roots (every 2s)
            aggregator.get_snapshot()
            # Push loop: _push_history -> build_index (every 1s)
            reader.build_index()

        # The historical-root enumeration should have run at most once
        # (within the HISTORICAL_ROOTS_TTL window).
        assert agg_enum_count["n"] <= 2, (
            f"Expected <=2 historical-root enumerations, got {agg_enum_count['n']}"
        )
        # build_index should have run at most ~10 times (30 ticks / 3s TTL).
        assert index_count["n"] <= 15, (
            f"Expected <=15 build_index rebuilds, got {index_count['n']}"
        )
