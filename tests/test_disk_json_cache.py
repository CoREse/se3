"""Unit tests for the unified daemon disk-JSON parse cache (issue #243 / Part A).

Mirrors the #209 parse-counting regression style: patch the single ``json.loads``
seam (:func:`disk_json_cache._parse_json`) to count *full* parses and assert an
unchanged file is parsed at most once across many ticks, a changed file triggers
a re-parse, and an oversized file is *never* full-parsed — instead its top-level
header keys (including the old-format *tail* key ``is_worktree_mode``) are
recovered by the bounded degraded read, with a warn-once on extraction failure
and no cache pollution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import se3.daemon.disk_json_cache as cache_mod
from se3.daemon.disk_json_cache import (
    SIZE_GUARD_BYTES,
    read_engine_header,
    read_json_cached,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    """Each test starts with an empty module-level cache and warn-once set."""
    cache_mod.clear_cache()
    yield
    cache_mod.clear_cache()


def _count_parses(monkeypatch) -> dict:
    """Patch ``_parse_json`` to count full-file parses; returns a counter dict."""
    counter = {"n": 0}
    original = cache_mod._parse_json

    def counting_parse(text: str):
        counter["n"] += 1
        return original(text)

    monkeypatch.setattr(cache_mod, "_parse_json", counting_parse)
    return counter


def _engine_json(flow_id: str, *, is_worktree_mode: bool, state_padding: int = 0) -> str:
    """Build an ``indent=2`` engine.json with the real top-level key ordering.

    ``flow_id``/``status``/``task_description``/``task_type`` come first (head
    cluster), then a fat ``state`` dict, then the ``worktree_*`` /
    ``is_worktree_mode``/``updated_at`` tail cluster — exactly the layout
    ``FlowInstance.to_dict`` emits, so the degraded head+tail scan is exercised
    the same way it is against a real file.
    """
    data = {
        "flow_id": flow_id,
        "status": "completed",
        "task_description": "do the thing",
        "task_type": "discovery",
        "state": {"steps": ["x" * state_padding]} if state_padding else {"steps": []},
        "created_at": "2026-07-04T00:00:00",
        "updated_at": "2026-07-04T01:00:00",
        "completed_at": None,
        "is_worktree_mode": is_worktree_mode,
        "worktree_branch": "feat/x" if is_worktree_mode else None,
        "worktree_path": "/tmp/wt" if is_worktree_mode else None,
    }
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)


def test_unchanged_file_parsed_once_across_many_calls(tmp_path, monkeypatch):
    path = tmp_path / "engine.json"
    path.write_text(_engine_json("flow-a", is_worktree_mode=False), encoding="utf-8")
    counter = _count_parses(monkeypatch)

    first = read_json_cached(path)
    for _ in range(20):
        again = read_json_cached(path)
        assert again == first

    assert first is not None
    assert first["flow_id"] == "flow-a"
    assert counter["n"] == 1  # parsed exactly once despite 21 reads


def test_reparsed_after_content_change(tmp_path, monkeypatch):
    path = tmp_path / "engine.json"
    path.write_text(_engine_json("flow-a", is_worktree_mode=False), encoding="utf-8")
    counter = _count_parses(monkeypatch)

    assert read_json_cached(path)["flow_id"] == "flow-a"
    assert counter["n"] == 1

    # A genuine rewrite changes mtime and size -> the (path, mtime, size) key
    # changes -> exactly one more parse.
    path.write_text(
        _engine_json("flow-b", is_worktree_mode=True, state_padding=100),
        encoding="utf-8",
    )
    assert read_json_cached(path)["flow_id"] == "flow-b"
    assert counter["n"] == 2

    # Unchanged again -> no further parse.
    for _ in range(5):
        read_json_cached(path)
    assert counter["n"] == 2


def _count_file_parses(monkeypatch) -> dict:
    """Count full-file read+parse calls (``_parse_json_file``) — the whole-file
    read the active path must NOT repeat while the file is unchanged."""
    counter = {"n": 0}
    original = cache_mod._parse_json_file

    def counting(path):
        counter["n"] += 1
        return original(path)

    monkeypatch.setattr(cache_mod, "_parse_json_file", counting)
    return counter


def test_active_unchanged_not_reparsed(tmp_path, monkeypatch):
    """An unchanged active engine.json (verify_content=True) is not re-PARSED on a
    stat + whole-content hit.

    The #260 fix re-reads the whole content each poll to hash it (that read is the
    bounded cost that catches a middle rewrite the old head+tail window masked),
    but the expensive ``json.loads`` (``_parse_json_file``) must still run at most
    once per real change — an unchanged file hashes equal every poll, so no
    re-parse. This is the bound that keeps the fix off the #209 parse sink.
    """
    path = tmp_path / "engine.json"
    path.write_text(_engine_json("flow-a", is_worktree_mode=False), encoding="utf-8")

    # Prime the cache (one full read+parse is expected here).
    first = read_json_cached(path, verify_content=True)
    assert first is not None and first["flow_id"] == "flow-a"

    file_parses = _count_file_parses(monkeypatch)
    for _ in range(10):
        again = read_json_cached(path, verify_content=True)
        assert again == first
    assert file_parses["n"] == 0, (
        "unchanged active file must not be re-parsed per poll"
    )


def test_active_large_underguard_parsed_once_but_middle_rewrite_caught(
    tmp_path, monkeypatch
):
    """A ~1 MiB (≤ guard) active engine.json: parsed once while unchanged, yet a
    same-``(mtime, size)`` MIDDLE rewrite is still caught.

    This is the #260 fix's core trade: the per-poll cost is a whole-content read +
    C-speed hash (never the whole-file ``json.loads``, which runs at most once per
    real change), and in exchange a rewrite buried deep in the ``state`` block —
    which the old head+tail window silently masked, serving a stale parse — is now
    detected because the hash covers the whole file.
    """
    import os

    # ~1 MiB of padding inside ``state`` — well under the 5 MiB guard but far
    # larger than the removed 64 KiB verify window, so the change lands squarely in
    # the middle the old window could not see.
    def _doc(flow_id: str) -> str:
        pad = "x" * (1024 * 1024)
        return json.dumps(
            {
                "flow_id": "stable-id",
                "status": "running",
                "state": {"head_pad": pad, "marker": flow_id, "tail_pad": pad},
                "is_worktree_mode": False,
            },
            indent=2,
        )

    path = tmp_path / "engine.json"
    v1 = _doc("M1")
    v2 = _doc("M2")
    assert len(v1.encode()) == len(v2.encode())
    assert len(v1.encode()) < SIZE_GUARD_BYTES
    assert len(v1.encode()) > 128 * 1024

    path.write_text(v1, encoding="utf-8")
    first = read_json_cached(path, verify_content=True)
    assert first["state"]["marker"] == "M1"

    # Unchanged polls: no re-parse (the whole-content hash matches every time).
    file_parses = _count_file_parses(monkeypatch)
    for _ in range(5):
        read_json_cached(path, verify_content=True)
    assert file_parses["n"] == 0, "unchanged large active file must not be re-parsed"

    # Same-(mtime, size) middle rewrite: the whole-content hash catches it.
    st = path.stat()
    path.write_text(v2, encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert path.stat().st_size == st.st_size
    assert path.stat().st_mtime_ns == st.st_mtime_ns

    got = read_json_cached(path, verify_content=True)
    assert got["state"]["marker"] == "M2", (
        "a middle rewrite at the same (mtime, size) must be caught by the "
        "whole-content hash, not masked as the old head+tail window did (#260)"
    )
    assert file_parses["n"] == 1, "exactly one re-parse for the one real change"


def test_active_same_size_same_mtime_swap_detected(tmp_path, monkeypatch):
    """A completed→new-flow swap that keeps size AND mtime is still caught.

    The correctness property the whole-content hash guarantees: without it the
    daemon would report the stale, just-superseded flow
    (test_read_active_flows_drops_stale_flow_after_flow_change).
    """
    import os

    path = tmp_path / "engine.json"
    a = _engine_json("flowXa", is_worktree_mode=False)
    b = _engine_json("flowXb", is_worktree_mode=False)
    assert len(a.encode()) == len(b.encode())  # identical byte size

    path.write_text(a, encoding="utf-8")
    st = path.stat()
    assert read_json_cached(path, verify_content=True)["flow_id"] == "flowXa"

    # Overwrite with the same-size sibling and force the *identical* mtime, the
    # coarse-mtime worst case a stat key cannot distinguish.
    path.write_text(b, encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert path.stat().st_size == st.st_size
    assert path.stat().st_mtime_ns == st.st_mtime_ns

    got = read_json_cached(path, verify_content=True)
    assert got["flow_id"] == "flowXb", "same-size/same-mtime swap must be detected"


def test_active_underguard_midfile_change_is_reparsed(tmp_path, monkeypatch):
    """An active under-guard file changed ONLY in its middle ``state`` block is
    reparsed — via either the advanced ``(mtime, size)`` key OR the whole-content
    hash.

    Two independent guards force the reparse: a normal rewrite advances mtime (the
    stat key), and even at an identical ``(mtime, size)`` the whole-content hash
    covers the middle. Here the mtime is advanced deterministically so the stat key
    is the trigger; the same-``(mtime, size)`` middle case is locked separately by
    ``test_active_large_underguard_parsed_once_but_middle_rewrite_caught``.
    """
    import os

    head_pad = "A" * 100_000
    tail_pad = "B" * 100_000

    def _doc(step_index: int) -> str:
        # head_pad/tail_pad bracket a middle current_step_index that is what
        # differs across versions; the whole-content hash covers it regardless of
        # where in the file it lands.
        return json.dumps(
            {
                "flow_id": "flow-mid",
                "status": "running",
                "state": {
                    "head_pad": head_pad,
                    "current_step_index": step_index,
                    "tail_pad": tail_pad,
                },
                "is_worktree_mode": False,
            },
            indent=2,
        )

    path = tmp_path / "engine.json"
    v1 = _doc(3)
    v2 = _doc(4)
    assert len(v1.encode()) == len(v2.encode())  # single-digit swap keeps size
    assert len(v1.encode()) > 128 * 1024

    path.write_text(v1, encoding="utf-8")
    first = read_json_cached(path, verify_content=True)
    assert first["state"]["current_step_index"] == 3

    # Rewrite only the middle value; a normal rewrite advances mtime — force it
    # deterministically so the stat key triggers the reparse here.
    path.write_text(v2, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))

    got = read_json_cached(path, verify_content=True)
    assert got["state"]["current_step_index"] == 4, (
        "mid-file state change must be reparsed, not masked by the window hash"
    )


def test_active_reparses_after_real_change(tmp_path, monkeypatch):
    """A genuine rewrite (new mtime/size) is re-parsed even in active mode."""
    path = tmp_path / "engine.json"
    path.write_text(_engine_json("flow-a", is_worktree_mode=False), encoding="utf-8")
    counter = _count_parses(monkeypatch)

    assert read_json_cached(path, verify_content=True)["flow_id"] == "flow-a"
    assert counter["n"] == 1

    path.write_text(
        _engine_json("flow-b", is_worktree_mode=True, state_padding=100),
        encoding="utf-8",
    )
    assert read_json_cached(path, verify_content=True)["flow_id"] == "flow-b"
    assert counter["n"] == 2


def test_parse_failure_returns_none_and_is_not_reparsed(tmp_path, monkeypatch):
    path = tmp_path / "engine.json"
    path.write_text("{ this is not valid json", encoding="utf-8")
    counter = _count_parses(monkeypatch)

    assert read_json_cached(path) is None
    # Corrupt-but-unchanged file must not be re-parsed every tick.
    for _ in range(5):
        assert read_json_cached(path) is None
    assert counter["n"] == 1


def test_missing_file_returns_none(tmp_path):
    assert read_json_cached(tmp_path / "nope.json") is None
    assert read_engine_header(tmp_path / "nope.json") is None


def test_small_engine_header_uses_full_cached_parse(tmp_path, monkeypatch):
    path = tmp_path / "engine.json"
    path.write_text(_engine_json("flow-small", is_worktree_mode=True), encoding="utf-8")
    counter = _count_parses(monkeypatch)

    header = read_engine_header(path)
    assert header is not None
    assert header["flow_id"] == "flow-small"
    assert header["is_worktree_mode"] is True
    # Under-threshold header is a full cached parse routed through _parse_json.
    assert counter["n"] == 1
    read_engine_header(path)
    assert counter["n"] == 1


def _write_oversized(path: Path, flow_id: str, *, is_worktree_mode: bool) -> None:
    """Write a well-formed engine.json larger than SIZE_GUARD_BYTES.

    The head cluster and tail cluster keep the real ordering; a padding blob in
    the middle of ``state`` inflates the file past the guard so the degraded
    head+tail read must straddle it to reach ``is_worktree_mode``.
    """
    padding = "x" * (SIZE_GUARD_BYTES + 1024 * 1024)
    data = {
        "flow_id": flow_id,
        "status": "completed",
        "task_description": "big legacy flow",
        "task_type": "implement",
        "state": {"blob": padding, "steps": []},
        "created_at": "2026-07-04T00:00:00",
        "updated_at": "2026-07-04T02:00:00",
        "completed_at": None,
        "is_worktree_mode": is_worktree_mode,
        "worktree_branch": "feat/big" if is_worktree_mode else None,
        "project_root": str(path.parent),
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def test_oversized_file_never_full_parsed_but_header_extracted(tmp_path, monkeypatch):
    path = tmp_path / "engine.json"
    _write_oversized(path, "flow-big", is_worktree_mode=True)
    assert path.stat().st_size > SIZE_GUARD_BYTES
    counter = _count_parses(monkeypatch)

    header = read_engine_header(path)
    assert header is not None
    # Head-cluster keys.
    assert header["flow_id"] == "flow-big"
    assert header["status"] == "completed"
    assert header["task_type"] == "implement"
    # Tail-cluster key that lives *after* the multi-MB ``state`` dict — proves
    # the tail slice is read and scanned, not just the head.
    assert header["is_worktree_mode"] is True
    assert header["project_root"] == str(tmp_path)

    # The oversized file is NEVER handed to the full parser...
    assert counter["n"] == 0
    # ...and nothing about it lands in the cache (memory ceiling).
    for _ in range(3):
        read_engine_header(path)
    assert counter["n"] == 0
    assert cache_mod._CACHE == {}


def test_oversized_extraction_failure_warns_once(tmp_path, monkeypatch, caplog):
    # Oversized file whose head/tail carry no recoverable top-level flow_id.
    path = tmp_path / "engine.json"
    path.write_text("y" * (SIZE_GUARD_BYTES + 1024 * 1024), encoding="utf-8")
    counter = _count_parses(monkeypatch)

    with caplog.at_level("WARNING", logger="se3.daemon.disk_json_cache"):
        assert read_engine_header(path) is None
        assert read_engine_header(path) is None
        assert read_engine_header(path) is None

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1  # warn-once across repeated ticks
    assert counter["n"] == 0  # never full-parsed
    assert cache_mod._CACHE == {}
