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


def test_active_unchanged_not_fully_reread(tmp_path, monkeypatch):
    """An unchanged active engine.json (verify_content=True) is not re-read in
    full nor re-parsed on a stat hit — only a bounded window is re-hashed.

    Pre-fix, active reads re-read the whole file every poll to hash it; that
    redundant full read+parse is what issue #243 fix iteration 3 removed. The
    swap-safety it protected is preserved by the bounded-window hash (see
    ``test_active_same_size_same_mtime_swap_detected``).
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
        "unchanged active file must not be re-read in full / re-parsed per poll"
    )


def test_active_large_underguard_file_reads_only_a_window(tmp_path, monkeypatch):
    """A ~1 MiB (≤ guard) active engine.json is not re-read whole on a poll.

    The bounded head+tail window caps the per-poll read far below the file size,
    which is the concrete cost the finding targets for a legacy under-guard file.
    """
    from se3.daemon import disk_json_cache as _djc

    # ~1 MiB of padding inside ``state`` keeps flow_id at the head, is_worktree at
    # the tail — well under the 5 MiB guard but far larger than the window.
    path = tmp_path / "engine.json"
    path.write_text(
        _engine_json("flow-a", is_worktree_mode=True, state_padding=1024 * 1024),
        encoding="utf-8",
    )
    assert path.stat().st_size < SIZE_GUARD_BYTES
    assert path.stat().st_size > _djc._VERIFY_WINDOW * 2

    read_json_cached(path, verify_content=True)  # prime

    import builtins

    real_open = builtins.open
    bytes_read = {"n": 0}

    class _CountingFH:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            data = self._fh.read(n)
            bytes_read["n"] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            self._fh.__enter__()
            return self

        def __exit__(self, *a):
            return self._fh.__exit__(*a)

    def counting_open(file, *a, **k):
        fh = real_open(file, *a, **k)
        if str(file) == str(path) and "b" in (a[0] if a else k.get("mode", "")):
            return _CountingFH(fh)
        return fh

    monkeypatch.setattr(builtins, "open", counting_open)

    again = read_json_cached(path, verify_content=True)
    assert again["flow_id"] == "flow-a"
    assert bytes_read["n"] <= _djc._VERIFY_WINDOW * 2, (
        f"poll read {bytes_read['n']} bytes; must stay within the bounded window"
    )


def test_active_same_size_same_mtime_swap_detected(tmp_path, monkeypatch):
    """A completed→new-flow swap that keeps size AND mtime is still caught.

    This is the correctness property the whole-file hash used to give and the
    bounded-window hash preserves: without it the daemon would report the stale,
    just-superseded flow (test_read_active_flows_drops_stale_flow_after_flow_change).
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
    reparsed, even when the head+tail windows stay byte-identical.

    The window hash is a same-stat SAFEGUARD, not a replacement for the
    ``(mtime, size)`` key. A normal rewrite of a legacy engine.json that flips,
    say, ``current_step_index`` deep inside ``state`` leaves the head/tail
    windows unchanged, so the digest alone would keep serving the stale parse —
    the WebUI progress/current-step staleness the finding targets. The advanced
    mtime must force the reparse.
    """
    import os

    head_pad = "A" * 100_000
    tail_pad = "B" * 100_000

    def _doc(step_index: int) -> str:
        # head_pad/tail_pad each exceed the 64 KiB verify window, so the head and
        # tail windows land entirely inside them and are byte-identical across
        # versions; only the middle current_step_index differs.
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
    assert len(v1.encode()) > 128 * 1024  # > 2×window ⇒ head/tail read separately

    path.write_text(v1, encoding="utf-8")
    first = read_json_cached(path, verify_content=True)
    assert first["state"]["current_step_index"] == 3

    # Rewrite only the middle value; head+tail windows stay byte-identical. A
    # normal rewrite advances mtime — force it deterministically so the stat key
    # (not the unchanged window hash) is what triggers the reparse.
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
