"""Tests for worktree-copy attribution and issue de-duplication.

A ``se3 run --worktree`` flow body executes inside
``<main>/tianluo/worktrees/<name>/`` — a transient isolation sandbox that clones the
main project's ``tianluo/`` tree (including ``tianluo/issues/``). Two regressions are
covered here:

* the worktree copy directory being registered as a *standalone project* (so it
  pollutes the WebUI project list / registry), and
* the worktree copy's ``tianluo/issues/`` being aggregated alongside the main
  project's, so every issue is counted twice during a run.

Both are solved by a single realpath attribution: a process / root located
under some main project's ``tianluo/worktrees/`` is attributed back to that main
project root rather than treated as its own project.
"""

from __future__ import annotations

import json
from pathlib import Path

from tianluo.daemon.aggregator import DaemonAggregator
from tianluo.daemon.supervisor import (
    DaemonSupervisor,
    is_worktree_copy_root,
    resolve_worktree_main_root,
)


# -- fixtures --------------------------------------------------------------


def _make_project(root: Path) -> Path:
    """Create a minimal se3 project root and return it."""
    (root / "tianluo" / "state").mkdir(parents=True, exist_ok=True)
    (root / "tianluo" / "specs" / "base").mkdir(parents=True, exist_ok=True)
    return root


def _write_issue(root: Path, issue_id: str, *, status: str = "open") -> None:
    """Write a single issue YAML under ``tianluo/issues/<status>/``."""
    subdir = "closed" if status in ("closed", "resolved", "wontfix") else "open"
    issues_dir = root / "tianluo" / "issues" / subdir
    issues_dir.mkdir(parents=True, exist_ok=True)
    (issues_dir / f"{issue_id}_x.yaml").write_text(
        "\n".join(
            [
                f"id: '{issue_id}'",
                "description: a sample issue",
                f"status: {status}",
                "source: system",
            ]
        ),
        encoding="utf-8",
    )


def _write_engine(root: Path, flow_id: str, *, worktree: bool) -> None:
    """Write a minimal engine.json for *root*."""
    state_dir = root / "tianluo" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "flow_id": flow_id,
        "task_description": "demo",
        "task_type": "feature",
        "status": "running",
        "is_worktree_mode": worktree,
        "state": {
            "current_step_id": "s1",
            "selected_steps": ["analyze"],
            "current_step_index": 0,
            "steps": {"s1": {"step_type": "analyze", "status": "running"}},
        },
    }
    (state_dir / "engine.json").write_text(json.dumps(payload), encoding="utf-8")


# -- resolve_worktree_main_root --------------------------------------------


def test_resolve_worktree_attributes_to_main_root(tmp_path: Path) -> None:
    main = _make_project(tmp_path / "main")
    wt = main / "tianluo" / "worktrees" / "wt-1"
    _make_project(wt)

    assert resolve_worktree_main_root(str(wt)) == str(main.resolve())
    assert is_worktree_copy_root(str(wt)) is True


def test_resolve_returns_none_for_plain_project(tmp_path: Path) -> None:
    main = _make_project(tmp_path / "main")
    assert resolve_worktree_main_root(str(main)) is None
    assert is_worktree_copy_root(str(main)) is False


def test_resolve_nested_worktree_stops_at_immediate_parent(tmp_path: Path) -> None:
    """A nested worktree resolves to its *immediate* parent, not the outermost.

    Guards the boundary where the main project itself lives under a parent
    worktree: ``…/wt1/tianluo/worktrees/wt2`` must resolve to ``…/wt1``, never to
    the outer ``…`` root.
    """
    outer = _make_project(tmp_path / "outer")
    wt1 = outer / "tianluo" / "worktrees" / "wt-1"
    _make_project(wt1)
    wt2 = wt1 / "tianluo" / "worktrees" / "wt-2"
    _make_project(wt2)

    assert resolve_worktree_main_root(str(wt2)) == str(wt1.resolve())


def test_resolve_skips_non_worktree_structure(tmp_path: Path) -> None:
    """A path not shaped like ``<main>/tianluo/worktrees/<name>`` is left alone."""
    # ``worktrees`` directly under the root (no intervening ``se3`` segment) must
    # NOT be treated as an se3 isolation sandbox.
    plain = tmp_path / "worktrees" / "x"
    plain.mkdir(parents=True, exist_ok=True)
    assert resolve_worktree_main_root(str(plain)) is None
    # An empty / falsy path is handled gracefully.
    assert resolve_worktree_main_root("") is None


# -- supervisor registration -----------------------------------------------


class _FakeProc:
    def __init__(self, pid: int, cmdline: list, cwd: str) -> None:
        self.info = {"pid": pid, "cmdline": cmdline, "cwd": cwd}


class _FakePsutil:
    NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    AccessDenied = type("AccessDenied", (Exception,), {})

    def __init__(self, procs: list) -> None:
        self._procs = procs

    def Process(self) -> object:  # noqa: N802 - mirror psutil API
        return type("P", (), {"pid": 999999})()

    def process_iter(self, _attrs: list) -> list:
        return self._procs


def test_scan_external_registers_main_root_for_worktree_proc(
    tmp_path: Path, monkeypatch
) -> None:
    """A discovered ``se3 run`` process running in a worktree is attributed
    to the main project root, not the worktree path."""
    import tianluo.daemon.supervisor as sup_mod

    main = _make_project(tmp_path / "main")
    wt = main / "tianluo" / "worktrees" / "wt-1"
    _make_project(wt)

    fake = _FakePsutil(
        [_FakeProc(12345, ["se3", "run", "--worktree"], str(wt))]
    )
    monkeypatch.setattr(sup_mod, "psutil", fake)

    sup = DaemonSupervisor()
    sup._scan_external()

    roots = {rec.project_root for rec in sup.flows}
    assert str(main.resolve()) in roots
    assert str(wt.resolve()) not in roots


# -- aggregator issue de-duplication ---------------------------------------


def test_collect_issues_skips_worktree_copy(tmp_path: Path) -> None:
    main = _make_project(tmp_path / "main")
    wt = main / "tianluo" / "worktrees" / "wt-1"
    _make_project(wt)
    _write_issue(main, "001")
    _write_issue(wt, "001")  # the worktree's clone of the same issue

    agg = DaemonAggregator(machine_id="m1")
    assert len(agg._collect_issues(main)) == 1
    assert agg._collect_issues(wt) == []


def test_snapshot_does_not_double_count_worktree_issues(tmp_path: Path) -> None:
    """During a worktree run the snapshot must surface each issue exactly once."""
    main = _make_project(tmp_path / "main")
    _write_issue(main, "001")
    _write_issue(main, "002")
    _write_engine(main, "flow-main", worktree=False)

    wt = main / "tianluo" / "worktrees" / "wt-1"
    _make_project(wt)
    # The worktree clones tianluo/issues/ AND runs an is_worktree_mode flow, so it
    # is an *observable* root (flow card) but its issue copy must be ignored.
    _write_issue(wt, "001")
    _write_issue(wt, "002")
    _write_engine(wt, "flow-wt", worktree=True)

    agg = DaemonAggregator(machine_id="m1")
    agg.add_project_root(str(main))
    snapshot = agg.get_snapshot()

    # The worktree run is observable as a flow ...
    flow_ids = {f.flow_id for f in snapshot.flows}
    assert "flow-wt" in flow_ids
    assert "flow-main" in flow_ids

    # ... but its issues are NOT double-counted.
    ids = sorted(i.id for i in snapshot.issues)
    assert ids == ["001", "002"]

    # And the worktree copy directory never appears as a standalone project.
    for root in snapshot.project_roots:
        assert "worktrees" not in Path(root).parts or not is_worktree_copy_root(root)


def test_worktree_root_excluded_from_project_roots(tmp_path: Path) -> None:
    """The dropdown-facing ``project_roots`` never lists a worktree copy."""
    main = _make_project(tmp_path / "main")
    _write_engine(main, "flow-main", worktree=False)
    wt = main / "tianluo" / "worktrees" / "wt-1"
    _make_project(wt)
    _write_engine(wt, "flow-wt", worktree=True)

    agg = DaemonAggregator(machine_id="m1")
    agg.add_project_root(str(main))
    snapshot = agg.get_snapshot()

    assert str(wt.resolve()) not in snapshot.project_roots
    assert str(main.resolve()) in snapshot.project_roots


# -- registration-seam normalization + registry sanitize (Bug2 / G3) -------
#
# The single write-through seam ``DaemonAggregator.add_project_root`` covers all
# five registration entry points (``__init__`` / ``request_spawn`` /
# ``request_resume`` / ``_handle_ensure_request`` / ``_resume_paused_flow``),
# the ``_append_project_root`` disk write backstops it, and the one-time
# ``_sanitize_project_roots`` startup pass purges historical pollution. These
# cases lock the displayed project-root set worktree-free both for new
# registrations and for an already-polluted on-disk registry.


def test_entry_point_seam_keeps_worktree_out_of_active_and_registry(
    tmp_path: Path,
) -> None:
    """A worktree path handed to any entry point lands only as its main root.

    All five entry points delegate to ``aggregator.add_project_root``; driving a
    worktree path straight through the live ``Daemon``'s aggregator exercises the
    shared seam and asserts neither the in-memory active set nor the persisted
    registry retains the ``/tianluo/worktrees/`` copy.
    """
    from tianluo.daemon.daemon import Daemon, DaemonConfig, _read_project_roots

    main = _make_project(tmp_path / "main")
    wt = main / "tianluo" / "worktrees" / "wt-1"
    _make_project(wt)

    config = DaemonConfig(pid_dir=tmp_path / "rt")
    daemon = Daemon(config)
    daemon.aggregator.add_project_root(str(wt))

    active = [str(p) for p in daemon.aggregator.project_roots]
    assert str(main.resolve()) in active
    assert str(wt.resolve()) not in active
    assert all("/tianluo/worktrees/" not in r for r in active)

    persisted = _read_project_roots(config.project_roots_file)
    assert str(main.resolve()) in persisted
    assert str(wt.resolve()) not in persisted


def test_append_project_root_defence_normalizes_worktree(tmp_path: Path) -> None:
    """The disk write backstop folds a worktree path to its main root."""
    from tianluo.daemon.daemon import _append_project_root, _read_project_roots

    main = _make_project(tmp_path / "main")
    wt = main / "tianluo" / "worktrees" / "wt-1"
    _make_project(wt)
    registry = tmp_path / "project_roots.json"

    _append_project_root(registry, str(wt))

    assert _read_project_roots(registry) == [str(main.resolve())]


def test_startup_sanitize_cleans_polluted_registry_to_main_only(
    tmp_path: Path,
) -> None:
    """A registry polluted with a worktree entry is rewritten to main-only.

    Mirrors a registry persisted before normalization existed: the one-time
    sanitize must atomically rewrite it dropping the worktree copy.
    """
    from tianluo.daemon.daemon import (
        _atomic_write_json,
        _read_project_roots,
        _sanitize_project_roots,
    )

    main = _make_project(tmp_path / "main")
    wt = main / "tianluo" / "worktrees" / "wt-1"
    _make_project(wt)
    registry = tmp_path / "project_roots.json"
    _atomic_write_json(
        registry,
        {"project_roots": sorted([str(main.resolve()), str(wt.resolve())])},
    )

    _sanitize_project_roots(registry)

    cleaned = _read_project_roots(registry)
    assert cleaned == [str(main.resolve())]
    assert all("/tianluo/worktrees/" not in r for r in cleaned)


def test_startup_sanitize_clean_registry_not_rewritten(tmp_path: Path) -> None:
    """A registry with no worktree pollution is left byte-for-byte untouched."""
    import time

    from tianluo.daemon.daemon import (
        _append_project_root,
        _read_project_roots,
        _sanitize_project_roots,
    )

    main = _make_project(tmp_path / "main")
    registry = tmp_path / "project_roots.json"
    _append_project_root(registry, str(main))
    mtime_before = registry.stat().st_mtime

    time.sleep(0.01)
    _sanitize_project_roots(registry)

    assert registry.stat().st_mtime == mtime_before
    assert _read_project_roots(registry) == [str(main.resolve())]
