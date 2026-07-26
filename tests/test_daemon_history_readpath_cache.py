"""Regression lock for the daemon *history* read-path disk-JSON cache (#243 / A1-A2).

The daemon history reader (:mod:`tianluo.daemon.history`) re-touches the SAME
``se3/state/archive/engine_*.json`` and ``se3/history/<flow>/_meta.json`` files
on every historical enumeration. Group G3 routes those reads through the unified
``(path, mtime, size)``-keyed :mod:`tianluo.daemon.disk_json_cache`
(``read_engine_header`` / ``read_json_cached``), superseding the earlier
content-keyed ``_read_engine_cached``. Two guardrails must hold on this path and
are locked here (regression section item **(b)**):

* a *tens-of-MB legacy* ``engine_*.json`` is NEVER fully parsed — it is degraded
  to a bounded head+tail scan that still recovers the hot top-level keys
  (``project_root`` from the archive, ``is_worktree_mode`` from the legacy file's
  *tail*), so an oversized archive can never re-introduce the #209 per-tick CPU
  sink;
* an *unchanged* small ``_meta.json`` is full-parsed at most once across repeated
  enumerations (stat-keyed cache hit);
* a degraded read that extracts nothing usable is skipped and warned-once,
  without aborting the enumeration.

The parses are counted by patching the single full-parse seam
``disk_json_cache._parse_json`` (the GIL-bound ``json.loads``); the bounded
degraded read deliberately does not route through it, so a count of 0 proves the
oversized file was never fully parsed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import tianluo.daemon.disk_json_cache as disk_cache
import tianluo.daemon.history as history_mod
from tianluo.daemon.history import enumerate_historical_project_roots

# Comfortably above the 5 MiB guard so the file is always degraded, kept modest
# so the test's temp write stays fast.
_OVERSIZE_STATE_BYTES = disk_cache.MAX_PARSE_BYTES + (1024 * 1024)


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """Reset the process-wide caches / warn-dedup so each test starts clean."""
    disk_cache.clear_cache()
    history_mod._warned_unreadable_paths.clear()
    yield
    disk_cache.clear_cache()
    history_mod._warned_unreadable_paths.clear()


def _count_full_parses(monkeypatch) -> dict:
    """Patch the full-parse seam to count whole-file ``json.loads`` calls."""
    counter = {"n": 0}
    original = disk_cache._parse_json

    def counting_parse(raw):
        counter["n"] += 1
        return original(raw)

    monkeypatch.setattr(disk_cache, "_parse_json", counting_parse)
    return counter


def _write_oversized_archive(
    archive_file: Path,
    *,
    flow_id: str,
    status: str,
    project_root: str,
    is_worktree_mode: bool,
) -> None:
    """Write a legacy-format, >5 MiB ``engine_*.json`` archive snapshot.

    Mirrors ``json.dumps(..., indent=2)``: the hot keys ``flow_id`` / ``status``
    head the file, a multi-MB ``state`` value bloats the middle, and the legacy
    *tail* keys ``is_worktree_mode`` / ``project_root`` trail it — exactly the
    layout the bounded head+tail degraded read must recover from.
    """
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    with open(archive_file, "w", encoding="utf-8") as fh:
        fh.write("{\n")
        fh.write(f'  "flow_id": {json.dumps(flow_id)},\n')
        fh.write(f'  "status": {json.dumps(status)},\n')
        fh.write('  "state": "')
        fh.write("x" * _OVERSIZE_STATE_BYTES)
        fh.write('",\n')
        fh.write(f'  "is_worktree_mode": {json.dumps(is_worktree_mode)},\n')
        fh.write(f'  "project_root": {json.dumps(project_root)}\n')
        fh.write("}\n")


def test_oversized_archive_never_fully_parsed_but_project_root_extracted(
    tmp_path, monkeypatch
):
    """A giant legacy archive is degraded, not parsed, yet still yields its root.

    The extracted ``project_root`` points at a *distinct* real directory that no
    other enumeration path could contribute, so its presence in the result
    proves the bounded head+tail read recovered the tail key — while the
    full-parse count stays at 0.
    """
    root = tmp_path / "proj"
    extracted = tmp_path / "the_real_root"
    extracted.mkdir()

    archive_file = root / "se3" / "state" / "archive" / "engine_20260101_000000.json"
    _write_oversized_archive(
        archive_file,
        flow_id="arch-flow",
        status="completed",
        project_root=str(extracted),
        is_worktree_mode=True,
    )
    assert archive_file.stat().st_size > disk_cache.MAX_PARSE_BYTES

    counter = _count_full_parses(monkeypatch)
    roots = enumerate_historical_project_roots([root])

    assert counter["n"] == 0, "the oversized archive must never be fully parsed"
    assert str(extracted.resolve()) in roots, (
        "degraded head+tail read must recover the tail-positioned project_root"
    )


def test_oversized_archive_degraded_read_recovers_legacy_tail_key(tmp_path):
    """``read_engine_header`` recovers the legacy *tail* key is_worktree_mode.

    A direct-call companion to the enumeration test: the giant ``state`` blob
    sits between the head keys and the tail keys, so recovering
    ``is_worktree_mode`` proves the *tail* window (not just the head) is scanned.
    """
    archive_file = (
        tmp_path / "se3" / "state" / "archive" / "engine_20260101_000000.json"
    )
    _write_oversized_archive(
        archive_file,
        flow_id="arch-flow",
        status="completed",
        project_root=str(tmp_path),
        is_worktree_mode=True,
    )

    header = disk_cache.read_engine_header(archive_file)
    assert header is not None
    assert header.get("flow_id") == "arch-flow"
    assert header.get("status") == "completed"
    assert header.get("is_worktree_mode") is True
    assert header.get("project_root") == str(tmp_path)


def test_unchanged_meta_json_parsed_once_across_enumerations(tmp_path, monkeypatch):
    """An unchanged ``_meta.json`` is full-parsed once, not per enumeration.

    Two back-to-back enumerations of the same untouched history directory must
    parse its ``_meta.json`` exactly once — the second call is a stat-keyed
    cache hit — collapsing the repeated per-tick parse the read-path cache exists
    to eliminate.
    """
    root = tmp_path / "proj"
    other_root = tmp_path / "meta_root"
    other_root.mkdir()

    flow_dir = root / "se3" / "history" / "20260101-000000_flow"
    flow_dir.mkdir(parents=True)
    (flow_dir / "_meta.json").write_text(
        json.dumps({"type": "discovery", "project_root": str(other_root)}),
        encoding="utf-8",
    )

    counter = _count_full_parses(monkeypatch)

    first = enumerate_historical_project_roots([root])
    second = enumerate_historical_project_roots([root])

    assert str(other_root.resolve()) in first
    assert first == second
    assert counter["n"] == 1, (
        f"_meta.json was fully parsed {counter['n']} times across two "
        "enumerations; the stat-keyed cache must collapse the second to a hit"
    )


def test_degraded_extraction_failure_is_skipped_and_warned(
    tmp_path, monkeypatch, caplog
):
    """An oversized file yielding no hot keys is skipped + warned-once, not fatal.

    A tens-of-MB blob with no extractable top-level key degrades to ``None``; the
    enumeration must warn once (observability) and carry on — it must not crash
    and must never fully parse the file.
    """
    root = tmp_path / "proj"
    archive_file = root / "se3" / "state" / "archive" / "engine_20260101_000000.json"
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    # No ``  "key": value`` top-level lines anywhere → degraded read extracts
    # nothing → read_engine_header returns None.
    archive_file.write_text("x" * (_OVERSIZE_STATE_BYTES), encoding="utf-8")
    assert archive_file.stat().st_size > disk_cache.MAX_PARSE_BYTES

    counter = _count_full_parses(monkeypatch)

    with caplog.at_level("WARNING", logger="tianluo.daemon.history"):
        roots = enumerate_historical_project_roots([root])

    assert counter["n"] == 0, "an unparseable oversized file must not be fully parsed"
    # The enumeration did not abort — the artifact-bearing root is still returned.
    assert str(root.resolve()) in roots
    assert "unreadable archive file" in caplog.text
    # warn-once: a second enumeration of the same corrupt file does not re-warn.
    caplog.clear()
    with caplog.at_level("WARNING", logger="tianluo.daemon.history"):
        enumerate_historical_project_roots([root])
    assert "unreadable archive file" not in caplog.text


# --------------------------------------------------------------------------
# #260 (G2 task 1) — the live engine.json freshness check must catch a rewrite
# confined to the file's MIDDLE, even when (mtime, size) and the former head+tail
# windows stay byte-identical. Before the fix the head+tail window hash masked
# such a rewrite and ``read_engine_header(active=True)`` served a STALE parse into
# the dense discovery→analyze rewrite window; hashing the whole content closes it.
# --------------------------------------------------------------------------


def _build_large_engine(marker: int) -> str:
    """A >128 KiB indent=2 engine.json whose ONLY variable byte-run is a marker
    buried in the TRUE MIDDLE of the steps table — beyond the former 64 KiB head
    window AND before the former 64 KiB tail window.

    The head keys (``flow_id`` / ``status``) and the tail keys
    (``current_step_index`` / worktree fields) are held byte-for-byte constant, so
    only a deep-in-the-table step marker changes: a same-size rewrite the old
    head+tail window could not see.
    """
    steps: dict = {}
    for i in range(3000):
        blob = "x" * 40
        if i == 1500:  # middle of the file — outside both former 64 KiB windows
            blob = "MID%04d" % marker + "x" * 33
        steps["%04d_step" % i] = {"status": "pending", "blob": blob}
    obj = {
        "flow_id": "F1",
        "status": "RUNNING",
        "task_description": "td",
        "state": {"steps": steps, "current_step_index": 0},
        "is_worktree_mode": False,
    }
    return json.dumps(obj, indent=2)


def test_active_engine_middle_rewrite_same_stat_returns_fresh_parse(tmp_path):
    """Same ``(mtime, size)`` + middle-only rewrite → the FRESH parse, not stale.

    Two writes sharing an identical ``(st_mtime_ns, st_size)`` that differ ONLY in
    the file's middle. The whole-content hash must catch the change so
    ``read_engine_header(active=True)`` returns the freshly-written middle — the
    #260 primary daemon-side fix (this was the boundary e2e's ``xfail`` repro).
    """
    ej = tmp_path / "engine.json"
    first = _build_large_engine(0)
    ej.write_text(first, encoding="utf-8")
    assert ej.stat().st_size > 128 * 1024, "engine.json must exceed the former window"
    disk_cache.clear_cache()

    d1 = disk_cache.read_engine_header(ej, active=True)
    st = os.stat(ej)

    second = _build_large_engine(1)
    assert len(first) == len(second), "the rewrite must preserve the byte size"
    ej.write_text(second, encoding="utf-8")
    # Force the two writes to share an mtime tick (coarse-mtime filesystems and
    # two fast writes on ext4 do this naturally; made deterministic here).
    os.utime(ej, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(ej).st_mtime_ns == st.st_mtime_ns
    assert os.stat(ej).st_size == st.st_size

    d2 = disk_cache.read_engine_header(ej, active=True)
    assert d1["state"]["steps"]["1500_step"]["blob"][:7] == "MID0000"
    assert d2["state"]["steps"]["1500_step"]["blob"][:7] == "MID0001", (
        "read_engine_header(active=True) served a STALE parse for the live "
        "engine.json; the whole-content freshness hash must catch a middle rewrite"
    )


def test_active_engine_unchanged_parsed_once_across_ticks(tmp_path, monkeypatch):
    """A byte-identical active engine.json is full-parsed at most once per change.

    Ten push-loop-equivalent reads of an UNCHANGED >128 KiB active engine.json
    must full-parse it exactly once — the whole-content hash matches every tick,
    so the cost stays a bounded read + C-speed digest, never a re-``json.loads``.
    This is the bound that keeps the fix from re-introducing the #209 parse sink.
    """
    ej = tmp_path / "engine.json"
    ej.write_text(_build_large_engine(0), encoding="utf-8")
    disk_cache.clear_cache()
    counter = _count_full_parses(monkeypatch)

    for _ in range(10):
        header = disk_cache.read_engine_header(ej, active=True)
        assert header["flow_id"] == "F1"

    assert counter["n"] == 1, (
        f"unchanged active engine.json was fully parsed {counter['n']} times "
        "across 10 ticks; the content-hash cache must collapse to a single parse"
    )


def test_active_engine_small_file_middle_rewrite_returns_fresh_parse(tmp_path):
    """The small-file path stays correct: a same-``(mtime, size)`` middle rewrite
    of an UNDER-128 KiB active engine.json is still caught (unchanged behaviour).

    The former window already read a small file whole; this locks that the
    whole-content hash preserves that correctness for the degenerate small case.
    """
    ej = tmp_path / "engine.json"
    obj0 = {"flow_id": "F1", "status": "RUNNING", "state": {"n": 111}}
    obj1 = {"flow_id": "F1", "status": "RUNNING", "state": {"n": 222}}
    a = json.dumps(obj0)
    b = json.dumps(obj1)
    assert len(a) == len(b)
    ej.write_text(a, encoding="utf-8")
    disk_cache.clear_cache()

    d1 = disk_cache.read_engine_header(ej, active=True)
    st = os.stat(ej)
    ej.write_text(b, encoding="utf-8")
    os.utime(ej, ns=(st.st_atime_ns, st.st_mtime_ns))

    d2 = disk_cache.read_engine_header(ej, active=True)
    assert d1["state"]["n"] == 111
    assert d2["state"]["n"] == 222


def test_oversized_active_engine_degrades_unchanged(tmp_path, monkeypatch):
    """An oversized (> guard) active engine.json still degrades — never parsed.

    The size guard runs BEFORE the verify path, so an oversized active file takes
    the always-fresh degraded head+tail scan regardless of ``active=True``; the
    whole-content freshness change must not alter that (the #209 guard).
    """
    ej = tmp_path / "engine.json"
    with open(ej, "w", encoding="utf-8") as fh:
        fh.write("{\n")
        fh.write('  "flow_id": "F1",\n')
        fh.write('  "status": "RUNNING",\n')
        fh.write('  "state": "')
        fh.write("x" * _OVERSIZE_STATE_BYTES)
        fh.write('",\n')
        fh.write('  "is_worktree_mode": false\n')
        fh.write("}\n")
    assert ej.stat().st_size > disk_cache.MAX_PARSE_BYTES
    disk_cache.clear_cache()
    counter = _count_full_parses(monkeypatch)

    header = disk_cache.read_engine_header(ej, active=True)
    assert counter["n"] == 0, "an oversized active file must never be fully parsed"
    assert header is not None and header.get("flow_id") == "F1"


def test_force_fresh_bypasses_stale_same_stat_cache(tmp_path):
    """``force_fresh=True`` re-parses even on a ``(mtime, size)`` + content hit.

    The true-value fallback the active-flow *drop* decision uses: given a cached
    parse, a forced fresh read must re-read + re-parse and return disk truth. Here
    a same-``(mtime, size)`` middle rewrite is deliberately hidden from the normal
    hash by pre-seeding the cache, then ``force_fresh`` must still surface it.
    """
    ej = tmp_path / "engine.json"
    first = _build_large_engine(0)
    ej.write_text(first, encoding="utf-8")
    disk_cache.clear_cache()
    disk_cache.read_engine_header(ej, active=True)  # seed the cache with MID0000
    st = os.stat(ej)

    second = _build_large_engine(1)
    ej.write_text(second, encoding="utf-8")
    os.utime(ej, ns=(st.st_atime_ns, st.st_mtime_ns))

    fresh = disk_cache.read_engine_header(ej, active=True, force_fresh=True)
    assert fresh["state"]["steps"]["1500_step"]["blob"][:7] == "MID0001"
