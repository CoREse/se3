"""Tests for the worktree→main normalization seam and registry cleanup.

Bug2: an ``se3 run --worktree`` flow body lives under
``<main>/tianluo/worktrees/<name>/`` — a transient isolation sandbox, never a
standalone project. Two failure modes are covered here:

* a worktree path leaking into the *displayed* project-root set (the WebUI
  project list / New Task dropdown) through any of the registration entry
  points, and its persistence into the on-disk registry (so it survives
  daemon restarts);
* an already-polluted registry retaining stale worktree entries across restarts.

The fix concentrates the normalization at the single write-through seam
``DaemonAggregator.add_project_root`` (covering all five registration entry
points and the persistent registry in one place), backstops it at the disk
write point ``_append_project_root``, and adds a one-time startup
``_sanitize_project_roots`` cleanup of historical pollution.
"""

from __future__ import annotations

import os
from pathlib import Path

from tianluo.daemon.aggregator import DaemonAggregator
from tianluo.daemon.daemon import (
    Daemon,
    DaemonConfig,
    _append_project_root,
    _read_project_roots,
    _sanitize_project_roots,
)


# -- fixtures --------------------------------------------------------------


def _make_project(root: Path) -> Path:
    """Create a minimal se3 project root and return it."""
    (root / "tianluo" / "state").mkdir(parents=True, exist_ok=True)
    return root


def _make_worktree(main: Path, name: str = "wt-1") -> Path:
    """Create a worktree isolation copy under ``<main>/tianluo/worktrees/<name>``."""
    wt = main / "tianluo" / "worktrees" / name
    _make_project(wt)
    return wt


# -- add_project_root normalization seam -----------------------------------


def test_add_project_root_normalizes_active_set(tmp_path: Path) -> None:
    """A worktree path joins the active set as its main root, not the copy."""
    main = _make_project(tmp_path / "main")
    wt = _make_worktree(main)

    agg = DaemonAggregator(machine_id="m1")
    agg.add_project_root(str(wt))

    roots = [str(p) for p in agg.project_roots]
    assert str(main.resolve()) in roots
    assert str(wt.resolve()) not in roots


def test_add_project_root_normalizes_registry_persist(tmp_path: Path) -> None:
    """The registry_persist callback receives the main root, never the copy."""
    main = _make_project(tmp_path / "main")
    wt = _make_worktree(main)

    persisted: list = []
    agg = DaemonAggregator(
        machine_id="m1",
        registry_persist=persisted.append,
    )
    agg.add_project_root(str(wt))

    assert persisted == [str(main.resolve())]


def test_add_project_root_plain_path_unchanged(tmp_path: Path) -> None:
    """A non-worktree path is registered verbatim (resolve_worktree → None)."""
    proj = _make_project(tmp_path / "proj")

    persisted: list = []
    agg = DaemonAggregator(machine_id="m1", registry_persist=persisted.append)
    agg.add_project_root(str(proj))

    assert [str(p) for p in agg.project_roots] == [str(proj.resolve())]
    assert persisted == [str(proj.resolve())]


def test_all_entry_points_normalize_through_seam(tmp_path: Path) -> None:
    """All five registration entry points route through the same seam.

    The entry points (``__init__`` / ``request_spawn`` / ``request_resume`` /
    ``_handle_ensure_request`` / ``_resume_paused_flow``) each call
    ``aggregator.add_project_root``; normalizing there once covers them all.
    Driving a worktree path straight through the aggregator is the shared
    contract every entry point relies on.
    """
    main = _make_project(tmp_path / "main")
    wt = _make_worktree(main)

    config = DaemonConfig(pid_dir=tmp_path / "rt")
    daemon = Daemon(config)
    # Simulate any entry point handing the aggregator a worktree path.
    daemon.aggregator.add_project_root(str(wt))

    # In-memory active set carries only the main root.
    roots = [str(p) for p in daemon.aggregator.project_roots]
    assert str(main.resolve()) in roots
    assert str(wt.resolve()) not in roots

    # The persisted registry likewise carries only the main root.
    persisted = _read_project_roots(config.project_roots_file)
    assert str(main.resolve()) in persisted
    assert str(wt.resolve()) not in persisted


# -- _append_project_root defence-in-depth ---------------------------------


def test_append_project_root_normalizes_worktree(tmp_path: Path) -> None:
    """The disk write point folds a worktree path back to its main root."""
    main = _make_project(tmp_path / "main")
    wt = _make_worktree(main)
    path = tmp_path / "project_roots.json"

    _append_project_root(path, str(wt))

    assert _read_project_roots(path) == [str(main.resolve())]


# -- _sanitize_project_roots cleanup ---------------------------------------


def test_sanitize_removes_worktree_entry(tmp_path: Path) -> None:
    """A registry already polluted with a worktree entry is cleaned + rewritten."""
    main = _make_project(tmp_path / "main")
    wt = _make_worktree(main)
    path = tmp_path / "project_roots.json"

    # Pre-pollute the registry with a worktree-copy entry plus the main root,
    # bypassing the (now-normalizing) _append_project_root.
    from tianluo.daemon.daemon import _atomic_write_json

    _atomic_write_json(
        path,
        {"project_roots": sorted([str(main.resolve()), str(wt.resolve())])},
    )

    _sanitize_project_roots(path)

    roots = _read_project_roots(path)
    assert str(main.resolve()) in roots
    assert str(wt.resolve()) not in roots


def test_sanitize_no_change_leaves_file_untouched(tmp_path: Path) -> None:
    """A clean registry is not rewritten (no needless write)."""
    main = _make_project(tmp_path / "main")
    path = tmp_path / "project_roots.json"
    _append_project_root(path, str(main))
    mtime_before = path.stat().st_mtime

    import time

    time.sleep(0.01)
    _sanitize_project_roots(path)

    assert path.stat().st_mtime == mtime_before
    assert _read_project_roots(path) == [str(main.resolve())]


def test_sanitize_missing_file_is_noop(tmp_path: Path) -> None:
    """Sanitizing a missing registry file is a fault-tolerant no-op."""
    path = tmp_path / "does-not-exist.json"
    _sanitize_project_roots(path)  # must not raise
    assert not path.exists()


def test_sanitize_corrupt_file_is_noop(tmp_path: Path) -> None:
    """A corrupt registry reads as empty and is left untouched."""
    path = tmp_path / "project_roots.json"
    path.write_text("{ not valid json", encoding="utf-8")
    _sanitize_project_roots(path)  # must not raise
    assert path.read_text(encoding="utf-8") == "{ not valid json"


def test_daemon_init_sanitizes_on_startup(tmp_path: Path) -> None:
    """Daemon construction runs the one-time registry sanitize.

    A worktree entry persisted before normalization existed is purged the
    next time a daemon starts against the same ``pid_dir``.
    """
    main = _make_project(tmp_path / "main")
    wt = _make_worktree(main)
    pid_dir = tmp_path / "rt"
    pid_dir.mkdir(parents=True, exist_ok=True)
    registry = pid_dir / "project_roots.json"

    from tianluo.daemon.daemon import _atomic_write_json

    _atomic_write_json(
        registry,
        {"project_roots": sorted([str(main.resolve()), str(wt.resolve())])},
    )

    Daemon(DaemonConfig(pid_dir=pid_dir))

    roots = _read_project_roots(registry)
    assert str(main.resolve()) in roots
    assert str(wt.resolve()) not in roots


# -- co-regression: observe but never register -----------------------------


def test_worktree_observable_but_not_registered(tmp_path: Path) -> None:
    """The worktree stays observable via the run-roots channel yet never
    appears in the displayed project-root set after normalization."""
    import json

    main = _make_project(tmp_path / "main")
    wt = _make_worktree(main)
    # An is_worktree_mode engine.json makes the worktree observable.
    (wt / "tianluo" / "state" / "engine.json").write_text(
        json.dumps({"flow_id": "flow-wt", "is_worktree_mode": True}),
        encoding="utf-8",
    )

    agg = DaemonAggregator(machine_id="m1")
    agg.add_project_root(str(main))
    # Even if a caller mistakenly hands the worktree path in, it normalizes.
    agg.add_project_root(str(wt))

    # Observable set includes the worktree (the "only observe" channel) ...
    observable = agg.all_observable_roots()
    assert str(wt.resolve()) in observable
    # ... but the displayed project roots never list it.
    assert str(wt.resolve()) not in agg.all_project_roots()
    assert str(main.resolve()) in agg.all_project_roots()
