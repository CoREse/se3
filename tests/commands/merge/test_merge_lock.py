"""Tests for MergeLock fcntl-based exclusive locking."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from se3.commands.merge.merge_lock import (
    MergeLock,
    MergeLockBusy,
    MergeLockStale,
    acquire_merge_lock,
)


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Return a temporary project root with se3/state/ created."""
    (tmp_path / "se3" / "state").mkdir(parents=True)
    return tmp_path


class TestMergeLockAcquireRelease:
    """Basic acquire / release lifecycle."""

    def test_acquire_creates_lock_file(self, tmp_project: Path) -> None:
        lock = MergeLock(tmp_project)
        lock.acquire()
        assert lock._resolved_path.exists()
        lock.release()

    def test_release_removes_fd(self, tmp_project: Path) -> None:
        lock = MergeLock(tmp_project)
        lock.acquire()
        assert lock._fd is not None
        lock.release()
        assert lock._fd is None

    def test_context_manager(self, tmp_project: Path) -> None:
        lock = MergeLock(tmp_project)
        with lock:
            assert lock._fd is not None
            assert lock._resolved_path.exists()
        assert lock._fd is None

    def test_lock_file_contains_pid(self, tmp_project: Path) -> None:
        lock = MergeLock(tmp_project)
        with lock:
            pid_str = lock._resolved_path.read_text(encoding="utf-8").strip()
            assert int(pid_str) == os.getpid()

    def test_idempotent_start(self, tmp_project: Path) -> None:
        lock = MergeLock(tmp_project)
        lock.acquire()
        lock.acquire()  # second acquire is a no-op because _started-like check
        assert lock._fd is not None
        lock.release()


class TestMergeLockContention:
    """Two processes competing for the same lock."""

    def test_second_acquire_raises_busy(self, tmp_project: Path) -> None:
        lock1 = MergeLock(tmp_project)
        lock1.acquire()
        try:
            lock2 = MergeLock(tmp_project)
            with pytest.raises(MergeLockBusy):
                lock2.acquire()
        finally:
            lock1.release()

    def test_second_acquire_sees_holder_pid(self, tmp_project: Path) -> None:
        lock1 = MergeLock(tmp_project)
        lock1.acquire()
        try:
            lock2 = MergeLock(tmp_project)
            with pytest.raises(MergeLockBusy) as exc_info:
                lock2.acquire()
            assert exc_info.value.holder_pid == os.getpid()
        finally:
            lock1.release()

    def test_factory_function(self, tmp_project: Path) -> None:
        lock = acquire_merge_lock(tmp_project)
        assert lock._fd is not None
        lock.release()


class TestMergeLockStale:
    """Stale lock detection and optional breaking."""

    def test_stale_lock_raises_stale(self, tmp_project: Path) -> None:
        # Write a PID that definitely does not exist.
        lock_path = tmp_project / "se3" / "state" / "merge.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("999999\n", encoding="utf-8")

        lock = MergeLock(tmp_project)
        with pytest.raises(MergeLockStale) as exc_info:
            lock.acquire()
        assert exc_info.value.holder_pid == 999999

    def test_break_stale_removes_and_reacquires(self, tmp_project: Path) -> None:
        lock_path = tmp_project / "se3" / "state" / "merge.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("999999\n", encoding="utf-8")

        lock = MergeLock(tmp_project)
        lock.acquire(break_stale=True)
        assert lock._fd is not None
        pid_str = lock_path.read_text(encoding="utf-8").strip()
        assert int(pid_str) == os.getpid()
        lock.release()


class TestMergeLockSubprocess:
    """Verify lock is actually exclusive across subprocesses."""

    def test_subprocess_cannot_acquire_while_parent_holds(self, tmp_project: Path) -> None:
        lock = MergeLock(tmp_project)
        lock.acquire()
        try:
            # Run a small Python script in a subprocess that tries to acquire.
            script = f"""
import sys
sys.path.insert(0, "/data/cre/workspace/se3.0/se3/worktrees/impl-20260507-101152_5f945094-G1/src")
from se3.commands.merge.merge_lock import MergeLock, MergeLockBusy
lock = MergeLock({str(tmp_project)!r})
try:
    lock.acquire()
    print("ACQUIRED")
except MergeLockBusy:
    print("BUSY")
"""
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert "BUSY" in result.stdout
        finally:
            lock.release()
