"""Degraded-header result cache for oversized files (idle-CPU issue, group G2).

The residual ~1.1 MB/s idle disk read came from oversized (> ``MAX_PARSE_BYTES``)
archive snapshots: every ``build_index`` enumeration paid a 256 KiB head+tail
degraded read per file because the extraction result was never cached. These
tests lock the fix: the extracted header is cached keyed by
``(path, st_mtime_ns, st_size)`` — one ``stat`` per unchanged re-read, zero disk
opens — while a stat change re-scans, a ``None`` extraction failure is cached
too (warn still fires exactly once), and ``clear_cache()`` drops the store.

The counting seam is :func:`disk_json_cache._degraded_header` itself — the only
function that opens an oversized file on this path — so a count of 0 across
repeat reads proves zero disk opens.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import se3.daemon.disk_json_cache as djc


@pytest.fixture(autouse=True)
def _clean_cache():
    """Each test starts with empty parse/degraded caches and warn-once set."""
    djc.clear_cache()
    yield
    djc.clear_cache()


def _count_degraded_scans(monkeypatch) -> dict:
    """Patch ``_degraded_header`` to count head+tail disk scans."""
    counter = {"n": 0}
    original = djc._degraded_header

    def counting(path, size):
        counter["n"] += 1
        return original(path, size)

    monkeypatch.setattr(djc, "_degraded_header", counting)
    return counter


def _write_oversized(path: Path, flow_id: str, *, status: str = "completed") -> None:
    """Write a well-formed ``indent=2`` engine.json above the size guard.

    Head cluster (flow_id/status) + a fat middle ``state`` blob + tail cluster
    (``is_worktree_mode``) — the layout the degraded head+tail scan targets.
    """
    data = {
        "flow_id": flow_id,
        "status": status,
        "task_description": "big archived flow",
        "state": {"blob": "x" * (djc.MAX_PARSE_BYTES + 1024 * 1024)},
        "updated_at": "2026-07-17T00:00:00",
        "is_worktree_mode": False,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_second_read_zero_disk_scans(tmp_path, monkeypatch):
    """An unchanged oversized file is head+tail-scanned exactly once."""
    path = tmp_path / "engine_20260717.json"
    _write_oversized(path, "flow-big")
    assert path.stat().st_size > djc.MAX_PARSE_BYTES
    counter = _count_degraded_scans(monkeypatch)

    first = djc.read_engine_header(path)
    assert first is not None
    assert first["flow_id"] == "flow-big"
    assert first["status"] == "completed"
    assert first["is_worktree_mode"] is False
    assert counter["n"] == 1

    for _ in range(10):
        assert djc.read_engine_header(path) == first
    assert counter["n"] == 1, (
        "an unchanged oversized file must cost one stat per read, "
        "never a repeated 256 KiB head+tail scan"
    )


def test_stat_change_rescans_and_updates_cache(tmp_path, monkeypatch):
    path = tmp_path / "engine_20260717.json"
    _write_oversized(path, "flow-v1")
    counter = _count_degraded_scans(monkeypatch)

    assert djc.read_engine_header(path)["flow_id"] == "flow-v1"
    assert counter["n"] == 1

    # A genuine replacement (new flow_id changes size, mtime advances) is
    # picked up on the next read.
    _write_oversized(path, "flow-v2-longer")
    assert djc.read_engine_header(path)["flow_id"] == "flow-v2-longer"
    assert counter["n"] == 2

    # Same content, mtime bumped (an atomic re-write of identical bytes): the
    # stat key moved, so one more scan — then cached again.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    assert djc.read_engine_header(path)["flow_id"] == "flow-v2-longer"
    assert counter["n"] == 3
    djc.read_engine_header(path)
    assert counter["n"] == 3


def test_extraction_failure_cached_and_warns_once(tmp_path, monkeypatch, caplog):
    """A broken oversized file (no flow_id) is scanned once, warned once."""
    path = tmp_path / "engine_bad.json"
    path.write_text("y" * (djc.MAX_PARSE_BYTES + 1024 * 1024), encoding="utf-8")
    counter = _count_degraded_scans(monkeypatch)

    with caplog.at_level("WARNING", logger="se3.daemon.disk_json_cache"):
        for _ in range(5):
            assert djc.read_engine_header(path) is None

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert counter["n"] == 1, (
        "a cached None failure must stop the per-tick re-scan of a broken file"
    )


def test_clear_cache_drops_degraded_store(tmp_path, monkeypatch):
    path = tmp_path / "engine_20260717.json"
    _write_oversized(path, "flow-big")
    counter = _count_degraded_scans(monkeypatch)

    djc.read_engine_header(path)
    assert counter["n"] == 1
    djc.clear_cache()
    djc.read_engine_header(path)
    assert counter["n"] == 2, "clear_cache must also flush the degraded cache"


def test_full_parse_cache_never_polluted_by_oversized(tmp_path):
    """The multi-MB body is still never cached — only the tiny header is."""
    path = tmp_path / "engine_20260717.json"
    _write_oversized(path, "flow-big")

    header = djc.read_engine_header(path)
    assert header is not None
    # The main full-parse store stays empty (memory ceiling preserved)...
    assert djc._CACHE == {}
    # ...and read_json_cached still refuses over-guard files outright.
    assert djc.read_json_cached(path) is None
    # The degraded store holds only the extracted header, never the body.
    cached = djc._DEGRADED_CACHE[str(path)]
    assert cached[2] == header
    assert "state" not in cached[2]


def test_deleted_oversized_file_drops_degraded_entry(tmp_path):
    path = tmp_path / "engine_20260717.json"
    _write_oversized(path, "flow-big")
    assert djc.read_engine_header(path) is not None
    assert str(path) in djc._DEGRADED_CACHE

    path.unlink()
    assert djc.read_engine_header(path) is None
    assert str(path) not in djc._DEGRADED_CACHE, (
        "a vanished path must not pin its degraded header for the daemon's life"
    )


def test_force_fresh_bypasses_degraded_cache(tmp_path, monkeypatch):
    """``force_fresh=True`` reaches disk even on a degraded-cache stat hit.

    The drop-decision true-value re-confirmation must not echo the cached
    header it is double-checking: a same-``(mtime_ns, size)`` in-place rewrite
    hidden from the stat key must still be surfaced by the forced read.
    """
    path = tmp_path / "engine.json"
    _write_oversized(path, "flow-AAA", status="running")
    st = path.stat()
    assert djc.read_engine_header(path, active=True)["flow_id"] == "flow-AAA"

    # Same-length swap with the identical stat token (the collision the
    # stat-keyed degraded cache accepts on the normal path).
    _write_oversized(path, "flow-BBB", status="running")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert path.stat().st_size == st.st_size
    assert path.stat().st_mtime_ns == st.st_mtime_ns

    # Normal read: served from the stat-keyed cache (the accepted trade-off).
    assert djc.read_engine_header(path, active=True)["flow_id"] == "flow-AAA"
    # Forced read: disk truth, and the cache is refreshed with it.
    fresh = djc.read_engine_header(path, active=True, force_fresh=True)
    assert fresh["flow_id"] == "flow-BBB"
    assert djc.read_engine_header(path, active=True)["flow_id"] == "flow-BBB"


def test_degraded_lru_bounded(tmp_path, monkeypatch):
    """The degraded store honours the same LRU cap as the main store."""
    monkeypatch.setattr(djc, "_MAX_CACHE_ENTRIES", 3)
    paths = []
    for i in range(5):
        p = tmp_path / f"engine_{i}.json"
        _write_oversized(p, f"flow-{i}")
        paths.append(p)
        djc.read_engine_header(p)
    assert len(djc._DEGRADED_CACHE) == 3
    # Most-recently-read entries survive; the oldest were evicted.
    assert str(paths[-1]) in djc._DEGRADED_CACHE
    assert str(paths[0]) not in djc._DEGRADED_CACHE
