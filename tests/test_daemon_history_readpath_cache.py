"""Regression lock for the daemon *history* read-path disk-JSON cache (#243 / A1-A2).

The daemon history reader (:mod:`se3.daemon.history`) re-touches the SAME
``se3/state/archive/engine_*.json`` and ``se3/history/<flow>/_meta.json`` files
on every historical enumeration. Group G3 routes those reads through the unified
``(path, mtime, size)``-keyed :mod:`se3.daemon.disk_json_cache`
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
from pathlib import Path

import pytest

import se3.daemon.disk_json_cache as disk_cache
import se3.daemon.history as history_mod
from se3.daemon.history import enumerate_historical_project_roots

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

    with caplog.at_level("WARNING", logger="se3.daemon.history"):
        roots = enumerate_historical_project_roots([root])

    assert counter["n"] == 0, "an unparseable oversized file must not be fully parsed"
    # The enumeration did not abort — the artifact-bearing root is still returned.
    assert str(root.resolve()) in roots
    assert "unreadable archive file" in caplog.text
    # warn-once: a second enumeration of the same corrupt file does not re-warn.
    caplog.clear()
    with caplog.at_level("WARNING", logger="se3.daemon.history"):
        enumerate_historical_project_roots([root])
    assert "unreadable archive file" not in caplog.text
