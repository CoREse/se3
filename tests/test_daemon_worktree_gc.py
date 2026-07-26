"""Tests for the daemon's periodic worktree-GC tick.

Covers the three behaviours G3 adds to :class:`tianluo.daemon.daemon.Daemon`:

* ``_maybe_run_gc`` fires ``_gc_once`` at most once per ``gc_interval`` (runs on
  the first poll, is skipped while still inside the interval, runs again once
  the interval has elapsed, and is fully disabled by a non-positive interval);
* ``_gc_once`` offloads the sweep to :func:`gc_worktree_runs` for every tracked
  main project root, forwarding ``gc_max_age_seconds``;
* a retained (completed-but-unmerged) worktree branch is surfaced as a WARNING.

``gc_worktree_runs`` itself is mocked so these tests isolate the daemon wiring
from the (separately unit-tested) core scavenger.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import tianluo.engine.merge.worktree_gc as gc_mod
from tianluo.daemon.daemon import Daemon, DaemonConfig
from tianluo.engine.merge.worktree_gc import WorktreeGCReport


def _make_daemon(tmp_path, *, gc_interval=3600.0, gc_max_age_seconds=86400.0,
                 with_root=True):
    """Build a Daemon with an isolated pid_dir (no ~/.se3 side effects)."""
    pid_dir = tmp_path / "pid"
    pid_dir.mkdir()
    roots = []
    root = None
    if with_root:
        root = tmp_path / "proj"
        (root / "tianluo").mkdir(parents=True)
        roots = [str(root)]
    config = DaemonConfig(
        pid_dir=pid_dir,
        project_roots=roots,
        gc_interval=gc_interval,
        gc_max_age_seconds=gc_max_age_seconds,
    )
    return Daemon(config), root


def _run(coro):
    return asyncio.run(coro)


def test_gc_config_defaults():
    config = DaemonConfig()
    assert config.gc_interval == 3600.0
    assert config.gc_max_age_seconds == 86400.0


def test_maybe_run_gc_fires_on_first_poll(tmp_path, monkeypatch):
    daemon, _root = _make_daemon(tmp_path)
    calls = {"n": 0}
    monkeypatch.setattr(daemon, "_gc_once", lambda: calls.__setitem__("n", calls["n"] + 1))

    _run(daemon._maybe_run_gc())

    assert calls["n"] == 1
    assert daemon._last_gc_at is not None


def test_maybe_run_gc_skips_within_interval(tmp_path, monkeypatch):
    daemon, _root = _make_daemon(tmp_path, gc_interval=3600.0)
    calls = {"n": 0}
    monkeypatch.setattr(daemon, "_gc_once", lambda: calls.__setitem__("n", calls["n"] + 1))

    # First call runs; a second immediate call is inside the interval → skipped.
    _run(daemon._maybe_run_gc())
    _run(daemon._maybe_run_gc())

    assert calls["n"] == 1


def test_maybe_run_gc_reruns_after_interval(tmp_path, monkeypatch):
    daemon, _root = _make_daemon(tmp_path, gc_interval=3600.0)
    calls = {"n": 0}
    monkeypatch.setattr(daemon, "_gc_once", lambda: calls.__setitem__("n", calls["n"] + 1))

    _run(daemon._maybe_run_gc())
    assert calls["n"] == 1
    # Pretend the last sweep was more than one interval ago.
    daemon._last_gc_at = daemon._last_gc_at - 3601.0
    _run(daemon._maybe_run_gc())

    assert calls["n"] == 2


def test_maybe_run_gc_disabled_when_interval_nonpositive(tmp_path, monkeypatch):
    daemon, _root = _make_daemon(tmp_path, gc_interval=0.0)
    calls = {"n": 0}
    monkeypatch.setattr(daemon, "_gc_once", lambda: calls.__setitem__("n", calls["n"] + 1))

    _run(daemon._maybe_run_gc())

    assert calls["n"] == 0
    assert daemon._last_gc_at is None


def test_gc_once_sweeps_each_root_with_max_age(tmp_path, monkeypatch):
    daemon, root = _make_daemon(tmp_path, gc_max_age_seconds=1234.0)
    seen = []

    def fake_gc(project_root, *, max_age_seconds, dry_run=False):
        seen.append((str(project_root), max_age_seconds, dry_run))
        return WorktreeGCReport()

    monkeypatch.setattr(gc_mod, "gc_worktree_runs", fake_gc)

    daemon._gc_once()

    assert len(seen) == 1
    swept_root, max_age, dry_run = seen[0]
    # all_project_roots realpaths its entries, so compare on realpath.
    import os
    assert swept_root == os.path.realpath(str(root))
    assert max_age == 1234.0
    assert dry_run is False


def test_gc_once_survives_one_root_failure(tmp_path, monkeypatch):
    daemon, _root = _make_daemon(tmp_path)

    def boom(project_root, *, max_age_seconds, dry_run=False):
        raise RuntimeError("git blew up")

    monkeypatch.setattr(gc_mod, "gc_worktree_runs", boom)

    # Must not propagate — a single root's failure is swallowed and logged.
    daemon._gc_once()


def test_retained_unmerged_branch_warns(tmp_path, monkeypatch, caplog):
    daemon, _root = _make_daemon(tmp_path)
    report = WorktreeGCReport(
        archived=[("wt-run", None, 100)],
        retained_unmerged=[
            ("tianluo/worktrees/webui-discovery", "master", "branch has commits not in HEAD (unmerged)"),
        ],
        reclaimed_bytes=100,
    )
    monkeypatch.setattr(
        gc_mod, "gc_worktree_runs",
        lambda project_root, *, max_age_seconds, dry_run=False: report,
    )

    with caplog.at_level(logging.WARNING, logger="tianluo.daemon.daemon"):
        daemon._gc_once()

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("未 merge" in m and "webui-discovery" in m for m in warnings), warnings


def test_clean_report_emits_no_warning(tmp_path, monkeypatch, caplog):
    daemon, _root = _make_daemon(tmp_path)
    monkeypatch.setattr(
        gc_mod, "gc_worktree_runs",
        lambda project_root, *, max_age_seconds, dry_run=False: WorktreeGCReport(),
    )

    with caplog.at_level(logging.WARNING, logger="tianluo.daemon.daemon"):
        daemon._gc_once()

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
