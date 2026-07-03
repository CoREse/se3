"""Regression lock for the daemon read-path hardening (issue #243, group G2).

issue #243 traced two daemon病灶. G2 owns the *aggregator read-path + event-loop
off-load* half:

* **病灶 1 (event-loop freeze).**  ``DaemonAggregator._active_worktree_run_roots``
  parsed *every* worktree subdir's ``engine.json`` from scratch on every ~1 s
  push tick, and — because it is reachable from the client's ``_calls_changed`` /
  ``_history_changed`` signature checks (via ``all_observable_roots`` /
  ``pending_calls_signature``) — those GIL-bound parses could run *on the event
  loop thread*, starving the push loop until the WebUI froze (the same freeze
  class as #209).

The G2 fix routes every aggregator engine.json / resumable-snapshot read through
the unified ``(path, mtime, size)``-keyed :mod:`se3.daemon.disk_json_cache`
(parsed at most once per actual change) and offloads the client's per-tick
signature checks to worker threads. These tests lock both properties in place,
mirroring the #209 parse-counting regression pattern:

* **(a)** the same unchanged engine.json is parsed at most once across many push
  ticks — the stat-keyed cache collapses the per-tick re-parse;
* **(c)** no disk JSON parse ever runs on the event-loop thread — every parse the
  push loop triggers happens on an ``asyncio.to_thread`` worker.

A supporting guardrail test covers the 5 MiB threshold and the degraded
head+tail header scan the cache falls back to for a giant legacy engine.json.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import se3.daemon.disk_json_cache as djc
import se3.daemon.history as history_mod
from se3.daemon.aggregator import DaemonAggregator
from se3.daemon.history import DaemonHistoryReader


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _write_engine(root: Path, flow_id: str, status: str, *, worktree: bool = False) -> None:
    """Write a small active ``engine.json`` under *root*."""
    state_dir = root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "flow_id": flow_id,
        "status": status,
        "task_description": "do a thing",
        "task_type": "feature",
        "state": {"selected_steps": ["a", "b"], "current_step_index": 1, "steps": {}},
        "is_worktree_mode": worktree,
    }
    (state_dir / "engine.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def _write_resumable(root: Path, flow_id: str, status: str) -> None:
    """Write a per-flow resumable snapshot under ``se3/state/resumable/``."""
    rdir = root / "se3" / "state" / "resumable"
    rdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "flow_id": flow_id,
        "status": status,
        "task_description": "paused work",
        "task_type": "feature",
        "state": {"selected_steps": ["a"], "current_step_index": 0, "steps": {}},
    }
    (rdir / f"{flow_id}.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def _count_djc_parses(monkeypatch) -> list:
    """Patch ``disk_json_cache._json_loads`` to record every parsed file's text.

    The cache always *stats* the file (cheap) but only calls ``_json_loads``
    (the GIL-bound ``json.loads``) when the ``(path, mtime, size)`` key misses;
    recording each call measures exactly the expensive operation the fix
    collapses. Returns a list appended to once per actual parse.
    """
    parses: list = []
    original = djc._json_loads

    def counting(raw):
        parses.append(raw)
        return original(raw)

    monkeypatch.setattr(djc, "_json_loads", counting)
    return parses


# --------------------------------------------------------------------------
# (a) parsed at most once per actual change across ticks
# --------------------------------------------------------------------------


def test_unchanged_engine_json_parsed_once_across_push_ticks(tmp_path, monkeypatch):
    """The active + worktree + resumable engine.json parse once, not per tick.

    Drives the aggregator's snapshot build and worktree scan repeatedly with the
    on-disk files UNCHANGED and asserts each engine.json-shaped file is parsed at
    most once for the whole run — pre-fix each tick re-parsed every worktree
    engine.json from scratch, the event-loop CPU sink #243 traced.
    """
    djc.clear_cache()
    main = tmp_path / "proj"
    _write_engine(main, "f_active", "RUNNING")
    _write_resumable(main, "f_paused", "PAUSED")
    # An active ``--worktree`` run under se3/worktrees/<name>/.
    wt = main / "se3" / "worktrees" / "wt1"
    _write_engine(wt, "f_wt", "RUNNING", worktree=True)

    parses = _count_djc_parses(monkeypatch)

    agg = DaemonAggregator()
    agg.add_project_root(main)

    # Several push-loop-equivalent rounds with nothing on disk changing.
    for _ in range(10):
        agg.get_snapshot()
        agg.all_observable_roots()
        agg.pending_calls_signature()

    # One parse per distinct engine.json-shaped file (main + worktree +
    # resumable = 3), never more — no double-parse within a round, no re-parse
    # across rounds.
    assert len(parses) == len(set(parses)), "a file was parsed more than once"
    assert 1 <= len(parses) <= 3, (
        f"expected <=3 parses (one per file) across 10 unchanged rounds, "
        f"got {len(parses)}"
    )


def test_changed_engine_json_is_reparsed(tmp_path, monkeypatch):
    """A genuine engine.json rewrite (new mtime/size) is re-parsed, not stale."""
    djc.clear_cache()
    main = tmp_path / "proj"
    _write_engine(main, "f1", "RUNNING")
    parses = _count_djc_parses(monkeypatch)

    agg = DaemonAggregator()
    agg.add_project_root(main)

    snap = agg.get_snapshot()
    assert any(f.flow_id == "f1" for f in snap.flows)
    first = len(parses)
    assert first >= 1

    # Unchanged re-read — served from the (path, mtime, size) cache, no re-parse.
    agg.get_snapshot()
    assert len(parses) == first

    # A step transition rewrites engine.json with different content/size.
    _write_engine(main, "f1", "COMPLETED")
    snap = agg.get_snapshot()
    assert len(parses) == first + 1, "rewrite must invalidate the stat-keyed cache"
    assert any(f.status == "COMPLETED" for f in snap.flows)


# --------------------------------------------------------------------------
# (c) no disk JSON parse ever runs on the event-loop thread
# --------------------------------------------------------------------------


def _patch_all_parse_seams(monkeypatch, sink: list) -> None:
    """Record the calling thread ident on every daemon disk-JSON parse seam."""
    for mod, name in (
        (djc, "_json_loads"),
        (history_mod, "_parse_engine_json"),
        (history_mod, "_read_json"),
    ):
        original = getattr(mod, name)

        def wrapper(*args, _orig=original, **kwargs):
            sink.append(threading.get_ident())
            return _orig(*args, **kwargs)

        monkeypatch.setattr(mod, name, wrapper)


def test_signature_checks_are_genuine_disk_parse_points(tmp_path, monkeypatch):
    """``_calls_changed`` / ``_history_changed`` really do parse engine.json.

    Documents *why* they must be offloaded: calling either directly triggers a
    disk JSON parse (the worktree scan / active-flow signature). If a future
    refactor made these parse-free the (c) guard below would pass vacuously, so
    this test pins the premise.
    """
    from se3.daemon.client import DaemonClient

    djc.clear_cache()
    main = tmp_path / "proj"
    _write_engine(main, "f_active", "RUNNING")
    wt = main / "se3" / "worktrees" / "wt1"
    _write_engine(wt, "f_wt", "RUNNING", worktree=True)

    agg = DaemonAggregator()
    agg.add_project_root(main)
    reader = DaemonHistoryReader(project_roots_provider=agg.all_observable_roots)
    client = DaemonClient(
        "ws://localhost:9",
        machine_id="m",
        hostname="h",
        se3_version="0",
        snapshot_provider=lambda: agg.get_snapshot().to_dict(),
        history_provider=reader,
        calls_signature_provider=agg.pending_calls_signature,
    )

    parses: list = []
    _patch_all_parse_seams(monkeypatch, parses)

    client._calls_changed()
    client._history_changed()
    assert parses, "signature checks are expected to trigger disk JSON parses"


def test_push_loop_never_parses_json_on_event_loop_thread(tmp_path, monkeypatch):
    """Every parse the push loop triggers happens off the event-loop thread.

    Runs the real :meth:`DaemonClient._push_loop` for a few fast ticks against a
    live aggregator + history reader over a project that has an active flow and
    an active ``--worktree`` run (so both the calls-signature worktree scan and
    the active-flow signature parse engine.json). The parse seams record their
    calling thread; the event-loop thread ident must never appear — pre-fix the
    synchronous ``_calls_changed`` / ``_history_changed`` calls parsed on the
    loop, the starvation that froze the WebUI.
    """
    from se3.daemon.client import DaemonClient

    djc.clear_cache()
    main = tmp_path / "proj"
    _write_engine(main, "f_active", "RUNNING")
    _write_resumable(main, "f_paused", "PAUSED")
    wt = main / "se3" / "worktrees" / "wt1"
    _write_engine(wt, "f_wt", "RUNNING", worktree=True)

    agg = DaemonAggregator()
    agg.add_project_root(main)
    reader = DaemonHistoryReader(project_roots_provider=agg.all_observable_roots)

    client = DaemonClient(
        "ws://localhost:9",
        machine_id="m",
        hostname="h",
        se3_version="0",
        snapshot_provider=lambda: agg.get_snapshot().to_dict(),
        history_provider=reader,
        calls_signature_provider=agg.pending_calls_signature,
        status_interval=0.5,
        history_poll_interval=0.02,
    )

    parse_threads: list = []
    _patch_all_parse_seams(monkeypatch, parse_threads)

    class _FakeWS:
        async def send(self, _data):  # the push loop's only ws use
            return None

    async def scenario():
        loop_ident = threading.get_ident()
        stop = asyncio.Event()
        task = asyncio.create_task(client._push_loop(_FakeWS(), stop))
        # Enough wall-clock for several fast ticks (each offloads both signature
        # checks + a status/history push).
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        return loop_ident

    loop_ident = asyncio.run(scenario())

    assert parse_threads, "the push loop should have triggered disk JSON parses"
    assert loop_ident not in parse_threads, (
        "a disk JSON parse ran on the event-loop thread; the daemon read path "
        "must offload every parse to a worker thread (issue #243 / #209 freeze)"
    )


# --------------------------------------------------------------------------
# (b) size guardrail + degraded head+tail header scan
# --------------------------------------------------------------------------


def _write_oversized_engine(path: Path, flow_id: str, status: str, *, worktree: bool) -> int:
    """Write an indent=2 engine.json larger than the 5 MiB threshold.

    Mirrors the legacy inlined format: ``flow_id`` / ``status`` at the head
    (before the giant ``state``) and ``is_worktree_mode`` at the tail (after it),
    so the degraded head+tail scan must reach both ends.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    steps = {
        f"{i:04d}_step_{i:08x}": {"status": "COMPLETED", "blob": "x" * 1000}
        for i in range(7000)
    }
    payload = {
        "flow_id": flow_id,
        "status": status,
        "task_description": "huge legacy flow",
        "task_type": "feature",
        "state": {"selected_steps": [], "steps": steps},
        "is_worktree_mode": worktree,
        "worktree_branch": "impl/x",
    }
    text = json.dumps(payload, indent=2)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def test_oversized_engine_json_uses_degraded_scan_no_full_parse(tmp_path, monkeypatch):
    """A >5 MiB engine.json is never full-parsed; hot keys come from head+tail.

    The degraded scan must recover the head keys (``flow_id`` / ``status``) *and*
    the tail key (``is_worktree_mode``, which sits after the giant ``state``
    dict), and must NOT invoke ``_json_loads`` on the whole file — nor cache it.
    """
    djc.clear_cache()
    engine = tmp_path / "se3" / "state" / "engine.json"
    size = _write_oversized_engine(engine, "f_big", "RUNNING", worktree=True)
    assert size > djc.MAX_PARSE_BYTES

    parses = _count_djc_parses(monkeypatch)

    header = djc.read_engine_header(engine)
    assert isinstance(header, dict)
    assert header.get("flow_id") == "f_big"          # head key
    assert header.get("status") == "RUNNING"          # head key
    assert header.get("is_worktree_mode") is True     # tail key (after state)

    # No full parse happened, and repeated reads never parse or cache it.
    djc.read_engine_header(engine)
    djc.read_engine_header(engine)
    assert parses == [], "an oversized file must never be full-parsed"
    assert str(engine) not in djc._CACHE, "an oversized file must never be cached"


def test_oversized_worktree_run_stays_visible_via_degraded_scan(tmp_path):
    """An active worktree run with a giant legacy engine.json is still discovered.

    The whole point of degrading (rather than skipping) an oversized file: the
    aggregator's worktree scan must still surface the run so the WebUI shows it.
    """
    djc.clear_cache()
    main = tmp_path / "proj"
    _write_engine(main, "f_active", "RUNNING")  # small main engine
    wt_engine = main / "se3" / "worktrees" / "wt1" / "se3" / "state" / "engine.json"
    _write_oversized_engine(wt_engine, "f_wt_big", "RUNNING", worktree=True)

    agg = DaemonAggregator()
    agg.add_project_root(main)

    roots = agg._active_worktree_run_roots()
    assert any("wt1" in r for r in roots), (
        "an active worktree run with an oversized legacy engine.json must stay "
        "discoverable via the degraded header scan"
    )


def test_oversized_unextractable_file_warns_once_and_skips(tmp_path, monkeypatch):
    """A giant file yielding no header keys is skipped with a single warning."""
    djc.clear_cache()
    engine = tmp_path / "engine.json"
    # >5 MiB of content with no top-level indent=2 keys to extract.
    engine.write_text("x" * (djc.MAX_PARSE_BYTES + 1024), encoding="utf-8")

    warnings: list = []
    monkeypatch.setattr(
        djc, "_warn_once_degraded", lambda p, _w=warnings.append: _w(str(p))
    )
    assert djc.read_engine_header(engine) is None
    assert warnings == [str(engine)]
