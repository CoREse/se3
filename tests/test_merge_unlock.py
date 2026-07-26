"""Tests for the ``se3 merge-unlock`` command and its core logic.

Covers the pure decision functions in ``merge_lock.py``
(``inspect_lock`` / ``release_merge_lock`` / ``break_lock_file``) across all
scheme-A branches, plus a CliRunner wiring smoke test for the
``se3 merge-unlock`` Typer command.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tianluo.commands.merge.merge_lock import (
    LockStatus,
    ReleaseOutcome,
    break_lock_file,
    inspect_lock,
    release_merge_lock,
)

# Default lock path relative to a project root.
LOCK_REL = Path("se3/state/merge.lock")


def _lock_path(project_root: Path) -> Path:
    return project_root / LOCK_REL


def _write_lock(project_root: Path, content: str) -> Path:
    """Write *content* as the lock file body and return its path."""
    path = _lock_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _dead_pid() -> int:
    """Return a PID that is (almost certainly) not alive.

    Searches downward from a large value for a PID that os.kill(pid, 0)
    reports as non-existent (ESRCH), so the test does not depend on a
    hard-coded number that might happen to be live.
    """
    import errno

    for candidate in range(4_000_000, 1, -1):
        try:
            os.kill(candidate, 0)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return candidate
    raise AssertionError("could not find a dead PID")  # pragma: no cover


# ---------------------------------------------------------------------------
# inspect_lock
# ---------------------------------------------------------------------------


def test_inspect_lock_missing_file(tmp_path: Path) -> None:
    status = inspect_lock(tmp_path)
    assert status.exists is False
    assert status.holder_pid is None
    assert status.alive is False
    assert status.stale is True  # nothing to hold == reclaimable
    assert status.corrupt is False
    assert status.lock_file.is_absolute()
    assert status.lock_file == _lock_path(tmp_path)


def test_inspect_lock_alive_holder(tmp_path: Path) -> None:
    _write_lock(tmp_path, f"{os.getpid():016d}\n")
    status = inspect_lock(tmp_path)
    assert status.exists is True
    assert status.holder_pid == os.getpid()
    assert status.alive is True
    assert status.stale is False
    assert status.corrupt is False


def test_inspect_lock_dead_holder(tmp_path: Path) -> None:
    dead = _dead_pid()
    _write_lock(tmp_path, f"{dead:016d}\n")
    status = inspect_lock(tmp_path)
    assert status.holder_pid == dead
    assert status.alive is False
    assert status.stale is True
    assert status.corrupt is False


def test_inspect_lock_empty_file_is_stale(tmp_path: Path) -> None:
    _write_lock(tmp_path, "")
    status = inspect_lock(tmp_path)
    assert status.exists is True
    assert status.holder_pid is None
    assert status.stale is True
    assert status.corrupt is False


def test_inspect_lock_corrupt_record(tmp_path: Path) -> None:
    _write_lock(tmp_path, "not-a-pid\n")
    status = inspect_lock(tmp_path)
    assert status.exists is True
    assert status.holder_pid is None
    assert status.stale is True
    assert status.corrupt is True


def test_inspect_lock_does_not_write(tmp_path: Path) -> None:
    path = _write_lock(tmp_path, f"{os.getpid():016d}\n")
    before = path.read_bytes()
    inspect_lock(tmp_path)
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# break_lock_file
# ---------------------------------------------------------------------------


def test_break_lock_file_removes_existing(tmp_path: Path) -> None:
    path = _write_lock(tmp_path, f"{os.getpid():016d}\n")
    assert break_lock_file(tmp_path) is True
    assert not path.exists()


def test_break_lock_file_noop_when_absent(tmp_path: Path) -> None:
    assert break_lock_file(tmp_path) is False


# ---------------------------------------------------------------------------
# release_merge_lock — scheme A decision branches
# ---------------------------------------------------------------------------


def test_release_no_lock(tmp_path: Path) -> None:
    outcome = release_merge_lock(tmp_path, force=False)
    assert outcome.action == "no_lock"
    assert outcome.exit_code == 0
    assert isinstance(outcome.status, LockStatus)
    assert outcome.status.exists is False


def test_release_stale_dead_pid(tmp_path: Path) -> None:
    path = _write_lock(tmp_path, f"{_dead_pid():016d}\n")
    outcome = release_merge_lock(tmp_path, force=False)
    assert outcome.action == "released_stale"
    assert outcome.exit_code == 0
    assert not path.exists()  # cleaned up without --force


def test_release_stale_no_pid(tmp_path: Path) -> None:
    path = _write_lock(tmp_path, "")
    outcome = release_merge_lock(tmp_path, force=False)
    assert outcome.action == "released_stale"
    assert outcome.exit_code == 0
    assert not path.exists()


def test_release_stale_corrupt(tmp_path: Path) -> None:
    path = _write_lock(tmp_path, "garbage\n")
    outcome = release_merge_lock(tmp_path, force=False)
    assert outcome.action == "released_stale"
    assert outcome.exit_code == 0
    assert outcome.status.corrupt is True
    assert not path.exists()


def test_release_refused_when_alive_without_force(tmp_path: Path) -> None:
    path = _write_lock(tmp_path, f"{os.getpid():016d}\n")
    outcome = release_merge_lock(tmp_path, force=False)
    assert outcome.action == "refused_alive"
    assert outcome.exit_code != 0
    assert path.exists()  # lock preserved
    assert outcome.status.holder_pid == os.getpid()
    assert outcome.status.alive is True


def test_release_force_removes_live_lock(tmp_path: Path) -> None:
    path = _write_lock(tmp_path, f"{os.getpid():016d}\n")
    outcome = release_merge_lock(tmp_path, force=True)
    assert outcome.action == "released_force"
    assert outcome.exit_code == 0
    assert not path.exists()


def _make_unremovable(path: Path) -> None:
    """Make *path* impossible to unlink by stripping write perms on its dir.

    On POSIX, removing a file requires write+execute on the *parent*
    directory; clearing the parent's write bit makes ``Path.unlink`` raise
    ``PermissionError`` while the file itself stays on disk.
    """
    path.parent.chmod(0o500)


def _restore(path: Path) -> None:
    path.parent.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses dir permissions")
def test_release_stale_failed_remove_when_unlink_denied(tmp_path: Path) -> None:
    path = _write_lock(tmp_path, f"{_dead_pid():016d}\n")
    _make_unremovable(path)
    try:
        outcome = release_merge_lock(tmp_path, force=False)
        assert outcome.action == "failed_remove"
        assert outcome.exit_code != 0
        assert path.exists()  # NOT actually released
    finally:
        _restore(path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses dir permissions")
def test_release_force_failed_remove_when_unlink_denied(tmp_path: Path) -> None:
    path = _write_lock(tmp_path, f"{os.getpid():016d}\n")
    _make_unremovable(path)
    try:
        outcome = release_merge_lock(tmp_path, force=True)
        assert outcome.action == "failed_remove"
        assert outcome.exit_code != 0
        assert path.exists()
    finally:
        _restore(path)


# ---------------------------------------------------------------------------
# CliRunner wiring smoke tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_project(tmp_path, monkeypatch):
    """Point the CLI's get_project_root at tmp_path."""
    import tianluo.commands.run as run_mod

    monkeypatch.setattr(run_mod, "get_project_root", lambda: tmp_path)
    return tmp_path


def _run(args):
    from tianluo.cli import app

    return CliRunner().invoke(app, args)


def _flat(text: str) -> str:
    """Collapse rich line-wrapping (newlines + padding) for substring checks."""
    return text.replace("\n", "").replace(" ", "")


def test_cli_no_lock_reports_and_exits_zero(cli_project) -> None:
    result = _run(["merge-unlock"])
    assert result.exit_code == 0
    assert str(_lock_path(cli_project)) in _flat(result.stdout)
    assert "Nomergelocktorelease." in _flat(result.stdout)


def test_cli_refuses_live_holder(cli_project) -> None:
    _write_lock(cli_project, f"{os.getpid():016d}\n")
    result = _run(["merge-unlock"])
    assert result.exit_code != 0
    assert str(os.getpid()) in _flat(result.stdout)
    assert "--force" in _flat(result.stdout)
    assert _lock_path(cli_project).exists()


def test_cli_force_flag_releases_live_holder(cli_project) -> None:
    _write_lock(cli_project, f"{os.getpid():016d}\n")
    result = _run(["merge-unlock", "--force"])
    assert result.exit_code == 0
    assert "WARNING" in result.stdout
    assert not _lock_path(cli_project).exists()


def test_cli_short_force_flag(cli_project) -> None:
    _write_lock(cli_project, f"{os.getpid():016d}\n")
    result = _run(["merge-unlock", "-f"])
    assert result.exit_code == 0
    assert not _lock_path(cli_project).exists()


def test_cli_stale_cleanup_without_force(cli_project) -> None:
    _write_lock(cli_project, f"{_dead_pid():016d}\n")
    result = _run(["merge-unlock"])
    assert result.exit_code == 0
    assert "Releasedstale" in _flat(result.stdout)
    assert not _lock_path(cli_project).exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses dir permissions")
def test_cli_failed_remove_reports_error_and_nonzero(cli_project) -> None:
    path = _write_lock(cli_project, f"{_dead_pid():016d}\n")
    _make_unremovable(path)
    try:
        result = _run(["merge-unlock"])
        assert result.exit_code != 0
        assert "ERROR" in result.stdout
        assert path.exists()  # the operator is NOT told it was released
        assert "Releasedstale" not in _flat(result.stdout)
    finally:
        _restore(path)
