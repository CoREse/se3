"""Tests for the daemon-side project-roots registry management surface.

Covers the durable deletion path (:func:`_remove_project_root`), the registry
write lock that keeps a concurrent append from resurrecting a just-removed
entry, and the operator-facing :meth:`Daemon.request_add_project` /
:meth:`Daemon.request_remove_project` entry points with their validation and
live-flow refusal.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from tianluo.daemon.daemon import (
    Daemon,
    DaemonConfig,
    ProjectCommandError,
    _append_project_root,
    _read_project_roots_raw,
    _remove_project_root,
    _sanitize_project_roots,
)
from tianluo.daemon import daemon as daemon_mod
from tianluo.daemon.supervisor import DaemonSupervisor


def _write_registry(path: Path, roots: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"project_roots": roots}), encoding="utf-8")


def _make_project(base: Path, name: str) -> Path:
    proj = base / name
    (proj / "se3" / "state").mkdir(parents=True, exist_ok=True)
    return proj


def _make_worktree_dir(main_root: Path, wt_name: str = "wt-1") -> Path:
    wt = main_root / "se3" / "worktrees" / wt_name
    (wt / "se3" / "state").mkdir(parents=True, exist_ok=True)
    return wt


# ---- _remove_project_root --------------------------------------------------


def test_remove_project_root_deletes_matching_entry(tmp_path: Path) -> None:
    reg = tmp_path / "project_roots.json"
    a = _make_project(tmp_path, "a")
    b = _make_project(tmp_path, "b")
    _write_registry(reg, [str(a.resolve()), str(b.resolve())])

    assert _remove_project_root(reg, str(a)) is True
    assert _read_project_roots_raw(reg) == [str(b.resolve())]


def test_remove_project_root_miss_never_writes(tmp_path: Path) -> None:
    """A miss must leave the file byte-identical (no needless rewrite)."""
    reg = tmp_path / "project_roots.json"
    b = _make_project(tmp_path, "b")
    _write_registry(reg, [str(b.resolve())])
    before_mtime = reg.stat().st_mtime_ns
    before_bytes = reg.read_bytes()

    assert _remove_project_root(reg, str(tmp_path / "never-registered")) is False
    assert reg.stat().st_mtime_ns == before_mtime
    assert reg.read_bytes() == before_bytes


def test_remove_project_root_folds_worktree_back_to_main(tmp_path: Path) -> None:
    reg = tmp_path / "project_roots.json"
    main = _make_project(tmp_path, "main")
    wt = _make_worktree_dir(main)
    _write_registry(reg, [str(main.resolve())])

    assert _remove_project_root(reg, str(wt)) is True
    assert _read_project_roots_raw(reg) == []


def test_remove_project_root_matches_alias_spelling(tmp_path: Path) -> None:
    """A symlinked / relatively-spelled alias of the same dir still matches."""
    reg = tmp_path / "project_roots.json"
    real = _make_project(tmp_path, "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    _write_registry(reg, [str(real.resolve())])

    # Removed through the symlink, and through a relative-component spelling.
    assert _remove_project_root(reg, str(link)) is True
    assert _read_project_roots_raw(reg) == []

    _write_registry(reg, [str(real.resolve())])
    assert _remove_project_root(reg, str(tmp_path / "sub" / ".." / "real")) is True
    assert _read_project_roots_raw(reg) == []


def test_remove_project_root_matches_alias_entry_in_file(tmp_path: Path) -> None:
    """An alias *entry* (written by an older daemon) is matched too."""
    reg = tmp_path / "project_roots.json"
    real = _make_project(tmp_path, "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    _write_registry(reg, [str(link)])

    assert _remove_project_root(reg, str(real)) is True
    assert _read_project_roots_raw(reg) == []


def test_remove_project_root_removes_vanished_entry(tmp_path: Path) -> None:
    """A stale entry whose directory is gone is exactly what removal targets."""
    reg = tmp_path / "project_roots.json"
    gone = tmp_path / "gone"
    _write_registry(reg, [str(gone)])

    assert _remove_project_root(reg, str(gone)) is True
    assert _read_project_roots_raw(reg) == []


def test_remove_project_root_tolerates_missing_and_corrupt_file(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "absent.json"
    assert _remove_project_root(missing, str(tmp_path)) is False
    assert not missing.exists()

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert _remove_project_root(corrupt, str(tmp_path)) is False
    assert corrupt.read_text(encoding="utf-8") == "{not json"


# ---- registry write lock ---------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda reg, proj: _append_project_root(reg, str(proj)), id="append"
        ),
        pytest.param(
            lambda reg, proj: _remove_project_root(reg, str(proj)), id="remove"
        ),
        pytest.param(
            lambda reg, proj: _sanitize_project_roots(reg), id="sanitize"
        ),
    ],
)
def test_registry_writers_hold_the_lock_across_read_modify_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call
) -> None:
    """Every read-modify-write of the registry reads under ``_REGISTRY_LOCK``."""
    reg = tmp_path / "project_roots.json"
    proj = _make_project(tmp_path, "proj")
    _write_registry(reg, [str(proj.resolve()), str(tmp_path / "vanished")])

    held: list = []
    real_read = daemon_mod._read_project_roots_raw

    def _spy(path):
        held.append(daemon_mod._REGISTRY_LOCK.locked())
        return real_read(path)

    monkeypatch.setattr(daemon_mod, "_read_project_roots_raw", _spy)
    call(reg, proj)

    assert held and all(held)


def test_concurrent_add_and_remove_do_not_lose_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remove racing an append must not be clobbered by the append's rewrite.

    Both operations are non-atomic read→rewrite passes. Serialized by the lock,
    they commute and the result is the same either way; unserialized, whichever
    read first wins the whole file, resurrecting the removed root (or dropping
    the appended one).
    """
    reg = tmp_path / "project_roots.json"
    old = _make_project(tmp_path, "old")
    keep = _make_project(tmp_path, "keep")
    fresh = _make_project(tmp_path, "fresh")
    _write_registry(reg, [str(old.resolve()), str(keep.resolve())])

    real_write = daemon_mod._atomic_write_json

    def _slow_write(path, payload):
        # Widen the read→write window so an unlocked implementation reliably
        # interleaves; with the lock this only serializes the two writers.
        time.sleep(0.05)
        real_write(path, payload)

    monkeypatch.setattr(daemon_mod, "_atomic_write_json", _slow_write)

    threads = [
        threading.Thread(target=_remove_project_root, args=(reg, str(old))),
        threading.Thread(target=_append_project_root, args=(reg, str(fresh))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert set(_read_project_roots_raw(reg)) == {
        str(keep.resolve()),
        str(fresh.resolve()),
    }


# ---- Daemon.request_add_project -------------------------------------------


def _daemon(tmp_path: Path) -> Daemon:
    return Daemon(DaemonConfig(pid_dir=tmp_path / "rt"))


def test_request_add_project_registers_and_persists(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    proj = _make_project(tmp_path, "proj")

    returned = daemon.request_add_project(str(proj))

    assert returned == str(proj.resolve())
    assert proj.resolve() in daemon.aggregator.project_roots
    assert _read_project_roots_raw(daemon.config.project_roots_file) == [
        str(proj.resolve())
    ]


def test_request_add_project_normalizes_worktree_path(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    main = _make_project(tmp_path, "main")
    wt = _make_worktree_dir(main)

    assert daemon.request_add_project(str(wt)) == str(main.resolve())
    assert _read_project_roots_raw(daemon.config.project_roots_file) == [
        str(main.resolve())
    ]


def test_request_add_project_rejects_relative_path(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    with pytest.raises(ProjectCommandError) as exc:
        daemon.request_add_project("relative/path")
    assert exc.value.code == "invalid_path"


def test_request_add_project_rejects_empty_path(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    with pytest.raises(ProjectCommandError) as exc:
        daemon.request_add_project("   ")
    assert exc.value.code == "invalid_path"


def test_request_add_project_rejects_filesystem_root(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    with pytest.raises(ProjectCommandError) as exc:
        daemon.request_add_project("/")
    assert exc.value.code == "invalid_path"
    assert daemon.aggregator.project_roots == []


def test_request_add_project_rejects_missing_path(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    with pytest.raises(ProjectCommandError) as exc:
        daemon.request_add_project(str(tmp_path / "nope"))
    assert exc.value.code == "not_found"


def test_request_add_project_rejects_regular_file(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    plain = tmp_path / "a-file.txt"
    plain.write_text("x", encoding="utf-8")
    with pytest.raises(ProjectCommandError) as exc:
        daemon.request_add_project(str(plain))
    assert exc.value.code == "not_a_directory"
    assert daemon.aggregator.project_roots == []


def test_request_add_project_accepts_non_se3_directory(tmp_path: Path) -> None:
    """An uninitialized directory registers fine; init happens at task start."""
    daemon = _daemon(tmp_path)
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()

    assert daemon.request_add_project(str(plain_dir)) == str(plain_dir.resolve())


# ---- Daemon.request_remove_project ----------------------------------------


def test_request_remove_project_deregisters(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    proj = _make_project(tmp_path, "proj")
    daemon.request_add_project(str(proj))

    assert daemon.request_remove_project(str(proj)) == str(proj.resolve())
    assert _read_project_roots_raw(daemon.config.project_roots_file) == []
    assert proj.resolve() not in daemon.aggregator.project_roots
    assert str(proj.resolve()) not in daemon.aggregator.all_project_roots()


def test_request_remove_project_leaves_project_data_untouched(
    tmp_path: Path,
) -> None:
    """Deregistration is registry-only: nothing under the project is deleted."""
    daemon = _daemon(tmp_path)
    proj = _make_project(tmp_path, "proj")
    marker = proj / "se3" / "state" / "engine.json"
    marker.write_text('{"flow_id": "f1"}', encoding="utf-8")
    daemon.request_add_project(str(proj))

    daemon.request_remove_project(str(proj))

    assert marker.read_text(encoding="utf-8") == '{"flow_id": "f1"}'
    assert proj.is_dir()


def test_request_remove_project_refuses_live_flow(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    proj = _make_project(tmp_path, "proj")
    daemon.request_add_project(str(proj))
    registry = daemon.config.project_roots_file
    before = registry.read_bytes()
    daemon._live_project_roots = lambda: {str(proj.resolve())}

    with pytest.raises(ProjectCommandError) as exc:
        daemon.request_remove_project(str(proj))

    assert exc.value.code == "live_flow"
    # Refusal must be write-free: the entry survives on disk and in memory.
    assert registry.read_bytes() == before
    assert proj.resolve() in daemon.aggregator.project_roots


def test_request_remove_project_live_worktree_flow_protects_main_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flow running inside an isolation worktree protects its main root."""
    daemon = _daemon(tmp_path)
    main = _make_project(tmp_path, "main")
    wt = _make_worktree_dir(main)
    daemon.request_add_project(str(main))
    # ``_live_project_roots`` already folds a worktree-attributed record back to
    # ``<main>``; assert the removal key lines up with that folded spelling.
    monkeypatch.setattr(DaemonSupervisor, "is_alive", staticmethod(lambda pid: True))
    daemon.supervisor.register(4242, str(wt))

    with pytest.raises(ProjectCommandError) as exc:
        daemon.request_remove_project(str(main))
    assert exc.value.code == "live_flow"


def test_request_remove_project_unregistered_root(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    proj = _make_project(tmp_path, "proj")

    with pytest.raises(ProjectCommandError) as exc:
        daemon.request_remove_project(str(proj))
    assert exc.value.code == "not_registered"


def test_request_remove_project_registry_write_failure_is_not_not_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only / full daemon dir reports ``registry_error``, not a 404.

    Regression: the entry is still in project_roots.json, so telling the
    operator it "is not registered" would make them stop retrying.
    """
    daemon = _daemon(tmp_path)
    proj = _make_project(tmp_path, "proj")
    daemon.request_add_project(str(proj))
    registry = daemon.config.project_roots_file
    before = registry.read_bytes()
    # Simulate the durable rewrite failing, with the root absent from the active
    # set (the post-restart shape where the old code returned a bare False).
    daemon.aggregator.set_project_roots([])
    monkeypatch.setattr(
        daemon.aggregator,
        "_registry_remove",
        _raise_oserror,
    )

    with pytest.raises(ProjectCommandError) as exc:
        daemon.request_remove_project(str(proj))

    assert exc.value.code == "registry_error"
    assert registry.read_bytes() == before


def _raise_oserror(root: str) -> bool:
    raise OSError("read-only file system")


def test_request_remove_project_rejects_relative_path(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    with pytest.raises(ProjectCommandError) as exc:
        daemon.request_remove_project("relative/path")
    assert exc.value.code == "invalid_path"


def test_request_remove_project_removes_vanished_registration(
    tmp_path: Path,
) -> None:
    """A registration whose directory was deleted is still removable."""
    daemon = _daemon(tmp_path)
    gone = _make_project(tmp_path, "gone")
    daemon.request_add_project(str(gone))
    resolved = str(gone.resolve())
    import shutil

    shutil.rmtree(gone)

    assert daemon.request_remove_project(resolved) == resolved
    assert _read_project_roots_raw(daemon.config.project_roots_file) == []


def test_registered_projects_surface_reaches_the_snapshot(tmp_path: Path) -> None:
    """The Daemon's aggregator is wired to the raw-read / remove callbacks."""
    daemon = _daemon(tmp_path)
    proj = _make_project(tmp_path, "proj")
    daemon.request_add_project(str(proj))

    rows = daemon.aggregator.registered_projects()
    assert rows == [{"path": str(proj.resolve()), "exists": True, "active": True}]

    daemon.request_remove_project(str(proj))
    assert daemon.aggregator.registered_projects() == []
