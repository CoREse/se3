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
