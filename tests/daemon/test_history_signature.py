"""Tests for the ``active_flow_signature`` cheap terminal-flow path.

The idle-disk hotspot being closed: the 1s fast tick used to route every
root's ``engine.json`` through the ``active=True`` verify_content read — a
whole-file read + hash per tick — even for a terminal (completed / failed)
flow whose file never changes again. The verify_content hash exists to catch
a same-``(mtime, size)`` in-place rewrite (PAUSED↔RUNNING flip), a hazard
only a *live* flow is exposed to; a terminal engine.json's next change is a
brand-new flow's full rewrite, which moves ``(mtime_ns, size)`` and busts the
stat-keyed cache anyway. So the signature pass now peeks the cached header
(``peek_cached_header`` — pure stat + dict probe, zero read/parse), skips
terminal flows before the verify_content read, and keeps the full
verify_content semantics for active flows unchanged. A peek miss (first
sighting / changed file) falls through to the verify read, so an unchanged
file is parsed at most once (the issue-#209 parse-once invariant).
"""

from __future__ import annotations

import json
import os

import pytest

import tianluo.daemon.disk_json_cache as disk_cache
from tianluo.daemon.history import DaemonHistoryReader


@pytest.fixture(autouse=True)
def _fresh_disk_cache():
    """Isolate the module-level disk JSON cache per test."""
    disk_cache.clear_cache()
    yield
    disk_cache.clear_cache()


def _make_reader(root):
    return DaemonHistoryReader(project_roots_provider=lambda: [root])


def _write_engine(root, payload, mtime_ns=None):
    state_dir = root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    engine = state_dir / "engine.json"
    engine.write_text(json.dumps(payload), encoding="utf-8")
    if mtime_ns is not None:
        # Pin the mtime so a rewrite keeps the same (mtime, size) stat key —
        # the coarse-mtime in-place-rewrite collision the verify_content hash
        # exists to catch.
        os.utime(engine, ns=(mtime_ns, mtime_ns))
    return engine


@pytest.fixture()
def full_reads(monkeypatch):
    """Count every verify_content whole-file read (``_read_active_content``)."""
    calls = []
    real = disk_cache._read_active_content

    def counting(path):
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(disk_cache, "_read_active_content", counting)
    return calls


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "completed"])
def test_terminal_flow_repeat_polls_zero_full_reads(tmp_path, full_reads, status):
    """Repeated signature polls over a terminal flow add zero full reads.

    Only the very first sighting pays one verify read (peek miss — the cache
    has never seen the file); every later poll peek-hits on the unchanged
    ``(mtime_ns, size)`` and skips before the verify_content path. This is
    the hotspot fix: the 1s tick's per-poll cost for a settled terminal root
    drops from a whole-file read + hash to a single stat.
    """
    _write_engine(tmp_path, {"flow_id": "done", "status": status})
    reader = _make_reader(tmp_path)

    assert reader.active_flow_signature() == {}
    warmup = len(full_reads)
    for _ in range(5):
        assert reader.active_flow_signature() == {}
    assert len(full_reads) == warmup


def test_terminal_flow_already_cached_pays_no_full_read(tmp_path, full_reads):
    """When another reader already cached the header (stat-keyed), the
    signature pass never touches the verify_content path at all."""
    engine = _write_engine(tmp_path, {"flow_id": "done", "status": "COMPLETED"})
    # Warm the shared cache the way the aggregator does (non-verify read).
    disk_cache.read_engine_header(engine, active=False)
    reader = _make_reader(tmp_path)

    for _ in range(5):
        assert reader.active_flow_signature() == {}
    assert full_reads == []


def test_terminal_flow_parsed_once_then_stat_only(tmp_path, monkeypatch):
    """After the first header parse, later polls are pure stat hits."""
    _write_engine(tmp_path, {"flow_id": "done", "status": "COMPLETED"})
    reader = _make_reader(tmp_path)

    parses = []
    real = disk_cache._parse_json_file

    def counting(path):
        parses.append(str(path))
        return real(path)

    monkeypatch.setattr(disk_cache, "_parse_json_file", counting)

    reader.active_flow_signature()
    assert len(parses) == 1
    for _ in range(5):
        reader.active_flow_signature()
    assert len(parses) == 1


def test_active_flow_still_verifies_content_each_poll(tmp_path, full_reads):
    """A live flow keeps the per-poll verify_content read (unchanged semantics)."""
    _write_engine(tmp_path, {"flow_id": "live", "status": "RUNNING"})
    reader = _make_reader(tmp_path)

    sig = reader.active_flow_signature()
    assert set(sig) == {"live"}
    first = len(full_reads)
    assert first >= 1

    reader.active_flow_signature()
    # Every poll of an active flow re-reads + re-hashes; that is the whole
    # point of verify_content and must not be optimized away.
    assert len(full_reads) > first


def test_active_same_stat_inplace_rewrite_still_moves_signature(tmp_path):
    """The same-(mtime, size) in-place rewrite detection survives the cheap path."""
    mtime_ns = 1_700_000_000_000_000_000
    a = {"flow_id": "live", "status": "RUNNING", "nonce": "A"}
    b = {"flow_id": "live", "status": "RUNNING", "nonce": "B"}
    assert len(json.dumps(a)) == len(json.dumps(b))

    engine = _write_engine(tmp_path, a, mtime_ns)
    reader = _make_reader(tmp_path)
    sig1 = reader.active_flow_signature()
    assert set(sig1) == {"live"}

    _write_engine(tmp_path, b, mtime_ns)
    st = engine.stat()
    assert (st.st_mtime_ns, st.st_size) == (mtime_ns, len(json.dumps(a)))

    sig2 = reader.active_flow_signature()
    assert sig2 != sig1


def test_active_same_stat_flip_to_terminal_drops_flow(tmp_path):
    """A same-stat in-place flip to FAILED is still caught by verify_content.

    The cheap pre-pass sees the stale RUNNING header (stat hit), but the
    verify_content re-read that follows re-derives the status from the fresh
    parse, so the now-terminal flow leaves the signature the same poll.
    """
    mtime_ns = 1_700_000_000_000_000_000
    running = {"flow_id": "live", "status": "RUNNING"}
    failed = {"flow_id": "live", "status": "FAILED "}  # padded to same length
    assert len(json.dumps(running)) == len(json.dumps(failed))

    _write_engine(tmp_path, running, mtime_ns)
    reader = _make_reader(tmp_path)
    assert set(reader.active_flow_signature()) == {"live"}

    _write_engine(tmp_path, failed, mtime_ns)
    assert reader.active_flow_signature() == {}


def test_signature_structure_unchanged_for_active_flow(tmp_path):
    """The per-flow token keeps its exact structure: __engine__ (with the
    folded content digest), __status__, then one (name, mtime, size) part per
    history jsonl."""
    _write_engine(tmp_path, {"flow_id": "live", "status": "RUNNING"})
    hist_dir = tmp_path / "se3" / "history" / "live"
    hist_dir.mkdir(parents=True)
    (hist_dir / "01_discovery.jsonl").write_text(
        json.dumps({"role": "user", "content": "q"}) + "\n", encoding="utf-8"
    )

    sig = _make_reader(tmp_path).active_flow_signature()
    parts = sig["live"]
    assert parts[0][0] == "__engine__"
    assert len(parts[0]) == 4  # marker, mtime, size, content digest
    assert parts[0][3] is not None  # active flow always folds a digest
    assert parts[1] == ("__status__", "running")
    assert parts[2][0] == "01_discovery.jsonl"
    assert len(parts) == 3


def test_terminal_then_new_flow_rewrite_detected(tmp_path):
    """A completed engine.json replaced by a new run's active flow is picked
    up on the next poll — the full rewrite moves (mtime_ns, size), so the
    peek misses and the verify read sees the fresh flow."""
    _write_engine(tmp_path, {"flow_id": "old", "status": "COMPLETED"})
    reader = _make_reader(tmp_path)
    assert reader.active_flow_signature() == {}

    _write_engine(
        tmp_path, {"flow_id": "new", "status": "RUNNING", "pad": "x" * 8}
    )
    assert set(reader.active_flow_signature()) == {"new"}


def test_jsonl_append_still_moves_signature(tmp_path):
    """History jsonl fingerprinting is untouched by the cheap path."""
    _write_engine(tmp_path, {"flow_id": "live", "status": "RUNNING"})
    hist_dir = tmp_path / "se3" / "history" / "live"
    hist_dir.mkdir(parents=True)
    jsonl = hist_dir / "02_analyze.jsonl"
    jsonl.write_text("{}\n", encoding="utf-8")

    reader = _make_reader(tmp_path)
    before = reader.active_flow_signature()
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write("{}\n")
    after = reader.active_flow_signature()
    assert before["live"] != after["live"]
