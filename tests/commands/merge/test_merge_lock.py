"""Tests for MergeLock fcntl-based exclusive locking."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from pathlib import Path

import tianluo

from tianluo.commands.merge.merge_lock import (
    MergeLock,
    MergeLockBusy,
    MergeLockStale,
    acquire_merge_lock,
)

# Dynamically resolve the src/ root so subprocess imports work regardless
# of the current working directory or worktree layout.
_SRC_ROOT = str(Path(tianluo.__file__).resolve().parent.parent)


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

    def test_unparseable_pid_raises_stale_with_none(self, tmp_project: Path) -> None:
        """When the lock file contains an unparseable PID and another process
        holds the flock, MergeLockStale carries holder_pid=None (not the
        ambiguous sentinel 0)."""
        lock_path = tmp_project / "se3" / "state" / "merge.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("not-a-number\n", encoding="utf-8")

        # Hold the lock in a subprocess so the main-process acquire fails.
        script = f"""
import fcntl, os, time
fd = os.open({str(lock_path)!r}, os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX)
os.write(fd, b"999999\\n")
os.fsync(fd)
time.sleep(5)
"""
        holder = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Wait for the subprocess to acquire the lock.
            import time
            time.sleep(0.3)

            lock = MergeLock(tmp_project)
            with pytest.raises(MergeLockStale) as exc_info:
                lock.acquire()
            assert exc_info.value.holder_pid is None
            assert "unparseable" in str(exc_info.value).lower()
        finally:
            holder.terminate()
            holder.wait(timeout=5)


class TestMergeLockBlocking:
    """Blocking (queueing) acquisition semantics — ``acquire(blocking=True)``.

    A blocking acquire must wait for the current holder to release the
    lock instead of failing fast with :class:`MergeLockBusy`. PID stale
    detection and ``_write_pid`` behaviour remain intact: a blocking
    acquire still records its own PID, and an unheld stale lock file does
    not wedge the blocking path (the kernel grants the lock immediately
    since no process holds the flock).
    """

    def test_blocking_acquire_waits_for_release(self, tmp_project: Path) -> None:
        import threading
        import time

        lock1 = MergeLock(tmp_project)
        lock1.acquire()  # non-blocking; lock1 now holds the flock

        release_delay = 0.5
        released_at: list[float] = []

        def _release_later() -> None:
            time.sleep(release_delay)
            released_at.append(time.monotonic())
            lock1.release()

        releaser = threading.Thread(target=_release_later)
        releaser.start()
        try:
            lock2 = MergeLock(tmp_project)
            start = time.monotonic()
            # Blocking acquire MUST wait until lock1 is released rather
            # than raising MergeLockBusy.
            lock2.acquire(blocking=True)
            acquired_at = time.monotonic()
            try:
                assert lock2._fd is not None
                # It waited for the holder to release (allow a small slack
                # for timer granularity).
                assert acquired_at - start >= release_delay - 0.1
                assert released_at and acquired_at >= released_at[0]
                # _write_pid recorded OUR pid after acquiring.
                pid_str = lock2._resolved_path.read_text(
                    encoding="utf-8"
                ).strip()
                assert int(pid_str) == os.getpid()
            finally:
                lock2.release()
        finally:
            releaser.join(timeout=5)
            lock1.release()

    def test_blocking_context_manager_default(self, tmp_project: Path) -> None:
        """``MergeLock(root, blocking=True)`` seeds the CM acquire mode."""
        lock = MergeLock(tmp_project, blocking=True)
        assert lock.blocking is True
        with lock:
            assert lock._fd is not None
        assert lock._fd is None

    def test_blocking_acquire_over_unheld_stale_file(
        self, tmp_project: Path
    ) -> None:
        """A stale lock file (dead PID, no holder) does not block.

        Nothing holds the flock, so ``LOCK_EX`` is granted immediately and
        our PID overwrites the stale record — blocking mode does not get
        wedged on a leftover lock file from a crashed process.
        """
        lock_path = tmp_project / "se3" / "state" / "merge.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("999999\n", encoding="utf-8")

        lock = MergeLock(tmp_project)
        lock.acquire(blocking=True)
        try:
            assert lock._fd is not None
            assert int(lock_path.read_text(encoding="utf-8").strip()) == os.getpid()
        finally:
            lock.release()

    def test_factory_passes_blocking(self, tmp_project: Path) -> None:
        lock = acquire_merge_lock(tmp_project, blocking=True)
        try:
            assert lock._fd is not None
        finally:
            lock.release()

    def test_blocking_serializes_across_processes(
        self, tmp_project: Path
    ) -> None:
        """A blocking acquire queues behind a holder in another process."""
        import time

        script = f"""
import sys, time
sys.path.insert(0, {_SRC_ROOT!r})
from tianluo.commands.merge.merge_lock import MergeLock
lock = MergeLock({str(tmp_project)!r})
lock.acquire()
print("HOLDING", flush=True)
time.sleep(1.0)
lock.release()
"""
        holder = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            # Wait for the subprocess to actually hold the lock.
            assert holder.stdout is not None
            line = holder.stdout.readline()
            assert "HOLDING" in line

            lock = MergeLock(tmp_project)
            start = time.monotonic()
            lock.acquire(blocking=True)  # queues until subprocess releases
            elapsed = time.monotonic() - start
            try:
                assert lock._fd is not None
                # It blocked rather than failing fast immediately.
                assert elapsed >= 0.3
            finally:
                lock.release()
        finally:
            holder.wait(timeout=10)


class TestMergeLockSubprocess:
    """Verify lock is actually exclusive across subprocesses."""

    def test_subprocess_cannot_acquire_while_parent_holds(self, tmp_project: Path) -> None:
        lock = MergeLock(tmp_project)
        lock.acquire()
        try:
            # Run a small Python script in a subprocess that tries to acquire.
            script = f"""
import sys
sys.path.insert(0, {_SRC_ROOT!r})
from tianluo.commands.merge.merge_lock import MergeLock, MergeLockBusy
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
