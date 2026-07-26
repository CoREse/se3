"""Regression tests for issue #243 — daemon disk-JSON read-path guards.

Reuses the #209 parse-counting pattern (patch the single parse seam and count
calls) to lock the two病灶 the fix targets:

* (a) an unchanged engine.json is parsed at most once across many ticks/readers;
* (b) an over-guard (tens-of-MB) file is never fully parsed — it is scanned
  head+tail for the hot top-level keys, including the *tail* key
  ``is_worktree_mode``; unextractable garbage degrades to ``None`` with a
  warn-once;
* (c) the daemon hot path (``_active_worktree_run_roots`` / ``_snapshot_for_root``)
  performs zero full JSON parses when two giant legacy engine.json files are in
  place — the deterministic stand-in for "daemon CPU stays low / push loop is
  not starved", and a giant legacy active worktree run stays *visible*.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tianluo.daemon import disk_json_cache as djc
from tianluo.daemon.aggregator import DaemonAggregator


@pytest.fixture(autouse=True)
def _clear_cache():
    djc.clear_cache()
    yield
    djc.clear_cache()


def _count_full_parses(monkeypatch) -> dict:
    """Patch the single ``json.loads`` seam to count full-file parses."""
    counter = {"n": 0}
    original = djc._parse_json_file

    def counting(path):
        counter["n"] += 1
        return original(path)

    monkeypatch.setattr(djc, "_parse_json_file", counting)
    return counter


def _write_engine(
    path: Path, *, flow_id: str, size_key_bytes: int = 0, worktree: bool = False
) -> None:
    """Write an ``indent=2`` engine.json with top-level head/tail hot keys.

    ``size_key_bytes`` inflates the (middle) ``state`` object so the file crosses
    the guard, mimicking a legacy inline engine.json. ``flow_id`` / ``status``
    sit at the head; ``is_worktree_mode`` sits at the tail (after ``state``),
    exactly as ``persistence`` emits them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "flow_id": flow_id,
        "status": "running",
        "task_description": "giant legacy flow",
        "state": {"blob": "Z" * size_key_bytes},
        "is_worktree_mode": worktree,
        "worktree_branch": "impl/x/G1" if worktree else None,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


# -- (a) parse once per change ---------------------------------------------


def test_unchanged_file_parsed_once(tmp_path, monkeypatch):
    eng = tmp_path / "engine.json"
    _write_engine(eng, flow_id="f1")
    counter = _count_full_parses(monkeypatch)

    for _ in range(10):
        data = djc.read_engine_header(eng)
        assert data["flow_id"] == "f1"
    assert counter["n"] == 1  # cached by (path, mtime, size)


def test_reparse_on_change(tmp_path, monkeypatch):
    eng = tmp_path / "engine.json"
    _write_engine(eng, flow_id="f1")
    counter = _count_full_parses(monkeypatch)

    assert djc.read_engine_header(eng)["flow_id"] == "f1"
    # Rewrite with different content (and size) → new (mtime,size) key → reparse.
    _write_engine(eng, flow_id="f2", size_key_bytes=128)
    assert djc.read_engine_header(eng)["flow_id"] == "f2"
    assert counter["n"] == 2


# -- (b) size guard + degraded head/tail read ------------------------------


def test_oversized_file_never_fully_parsed(tmp_path, monkeypatch):
    eng = tmp_path / "engine.json"
    # >5 MiB via the middle state blob → over guard.
    _write_engine(eng, flow_id="giant", size_key_bytes=6 * 1024 * 1024, worktree=True)
    assert eng.stat().st_size > djc.MAX_PARSE_BYTES
    counter = _count_full_parses(monkeypatch)

    hdr = djc.read_engine_header(eng)
    assert counter["n"] == 0  # NEVER fully parsed
    assert hdr["flow_id"] == "giant"
    assert hdr["status"] == "running"
    # Tail key (after the giant middle state object) is still extracted.
    assert hdr["is_worktree_mode"] is True
    assert hdr["worktree_branch"] == "impl/x/G1"


def test_oversized_file_not_cached(tmp_path):
    eng = tmp_path / "engine.json"
    _write_engine(eng, flow_id="giant", size_key_bytes=6 * 1024 * 1024)
    djc.read_engine_header(eng)
    # read_json_cached refuses to store/return over-guard files (memory guard).
    assert djc.read_json_cached(eng) is None


def test_degraded_extraction_failure_warns_once(tmp_path, monkeypatch):
    eng = tmp_path / "engine.json"
    # Over guard but no recognizable top-level flow_id line → extraction fails.
    eng.write_text("[" + "0," * (3 * 1024 * 1024) + "0]")
    assert eng.stat().st_size > djc.MAX_PARSE_BYTES

    warnings: list = []
    monkeypatch.setattr(
        djc.logger, "warning", lambda msg, *a, **k: warnings.append(msg)
    )
    assert djc.read_engine_header(eng) is None
    assert djc.read_engine_header(eng) is None
    assert len(warnings) == 1  # warn-once, not per tick


# -- (c) daemon hot path stays parse-free under giant legacy files ---------


def _make_worktree_run(base: Path, name: str, flow_id: str, *, giant: bool) -> Path:
    wt = base / "tianluo" / "worktrees" / name
    eng = wt / "tianluo" / "state" / "engine.json"
    _write_engine(
        eng,
        flow_id=flow_id,
        size_key_bytes=(20 * 1024 * 1024 if giant else 0),
        worktree=True,
    )
    return wt


def test_worktree_scan_no_full_parse_on_giant_files(tmp_path, monkeypatch):
    """Two 20MB legacy worktree engine.json in place → zero full parses, still visible."""
    base = tmp_path / "repo"
    base.mkdir()
    _make_worktree_run(base, "wt1", "flow-a", giant=True)
    _make_worktree_run(base, "wt2", "flow-b", giant=True)

    agg = DaemonAggregator()
    agg.add_project_root(base)

    counter = _count_full_parses(monkeypatch)
    # Multiple ticks, as the push loop would.
    roots = None
    for _ in range(5):
        roots = agg._active_worktree_run_roots()
    assert counter["n"] == 0  # giant files degraded, never fully parsed
    # Both giant legacy worktree runs remain observable in the WebUI.
    assert len(roots) == 2


def test_snapshot_for_root_reads_new_format_header_only(tmp_path, monkeypatch):
    """A new-format engine.json yields flow_id/status without touching cold files."""
    from tianluo.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType
    from tianluo.engine.persistence import PersistenceManager

    root = tmp_path / "proj"
    pm = PersistenceManager(root)
    flow = FlowInstance(task_description="hot", status=FlowStatus.RUNNING)
    flow.is_worktree_mode = True
    s = Step(step_type=StepType.IMPLEMENT, status=StepStatus.RUNNING)
    s.inputs = {"blob": "Q" * 200_000}
    flow.state.add_step(s)
    flow.state.selected_steps = [StepType.IMPLEMENT]
    flow.state.current_step_id = s.step_id
    pm.save_flow(flow)

    agg = DaemonAggregator()
    agg.add_project_root(root)
    snap = agg._snapshot_for_root(root)
    assert snap is not None
    assert snap.flow_id == flow.flow_id
    assert snap.status == "running"
    assert snap.total_steps == 1  # header carries the step status table
