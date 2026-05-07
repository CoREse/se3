"""Merge lock — prevents concurrent ``se3 merge`` invocations.

Uses ``fcntl.flock(LOCK_EX | LOCK_NB)`` on a dedicated lock file so
that competing processes receive an immediate error (non-blocking)
rather than queuing.  The lock is automatically released when the
file descriptor is closed (process exit, context-manager exit, or
explicit release).

Staleness detection is based on the lock-holder's PID: if the PID
stored in the lock file no longer exists, the lock is considered
stale and can be broken.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default path relative to project root.
_DEFAULT_LOCK_PATH = Path("se3/state/merge.lock")


class MergeLockBusy(RuntimeError):
    """Another ``se3 merge`` process holds the lock."""

    def __init__(self, lock_file: Path, holder_pid: Optional[int] = None) -> None:
        self.lock_file = lock_file
        self.holder_pid = holder_pid
        msg = f"Merge lock is held by another process: {lock_file}"
        if holder_pid is not None:
            msg += f" (pid={holder_pid})"
        super().__init__(msg)


class MergeLockStale(RuntimeError):
    """The lock file appears stale (holder PID does not exist).

    This is raised as a warning signal; callers may choose to break
    the stale lock and retry.
    """

    def __init__(self, lock_file: Path, holder_pid: int) -> None:
        self.lock_file = lock_file
        self.holder_pid = holder_pid
        super().__init__(
            f"Merge lock appears stale (pid={holder_pid} does not exist): {lock_file}"
        )


@dataclass
class MergeLock:
    """Exclusive merge lock using ``fcntl.flock``.

    Usage (context manager):

        with MergeLock(project_root):
            ...  # exclusive merge critical section

    Usage (explicit):

        lock = MergeLock(project_root)
        lock.acquire()
        try:
            ...
        finally:
            lock.release()
    """

    project_root: Path
    lock_path: Path = field(default=_DEFAULT_LOCK_PATH)  # type: ignore[var-annotated]
    _fd: Optional[int] = None

    def __post_init__(self) -> None:
        if self.lock_path.is_absolute():
            self._resolved_path = self.lock_path
        else:
            self._resolved_path = self.project_root / self.lock_path

    def _read_holder_pid(self) -> Optional[int]:
        """Return the PID stored in the lock file, or None."""
        try:
            text = self._resolved_path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        except (OSError, ValueError):
            pass
        return None

    def _write_pid(self) -> None:
        """Atomically write the current PID into the lock file."""
        pid_str = str(os.getpid())
        self._resolved_path.write_text(pid_str, encoding="utf-8")
        # fsync the directory to ensure the file is durable.
        dir_fd = os.open(self._resolved_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _is_pid_alive(self, pid: int) -> bool:
        """Check whether *pid* exists in the current process namespace."""
        try:
            os.kill(pid, 0)
            return True
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return False
            # EPERM means the process exists but we lack permission to
            # signal it — for lock staleness purposes, treat as alive.
            return True

    def acquire(self, break_stale: bool = False) -> None:
        """Acquire the exclusive merge lock.

        Args:
            break_stale: If ``True`` and the lock appears stale (holder
                PID does not exist), remove the old lock file and retry
                once.  Defaults to ``False`` to avoid races.

        Raises:
            MergeLockBusy: Another process holds the lock.
            MergeLockStale: The lock appears stale (only when
                ``break_stale=False``).
        """
        # Idempotent: if this instance already holds the lock, noop.
        if self._fd is not None:
            return

        self._resolved_path.parent.mkdir(parents=True, exist_ok=True)

        # Open in read-write mode (required for flock on some platforms).
        fd = os.open(
            str(self._resolved_path),
            os.O_RDWR | os.O_CREAT,
            0o644,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                holder = self._read_holder_pid()
                if holder is not None and not self._is_pid_alive(holder):
                    if break_stale:
                        logger.warning(
                            "Breaking stale merge lock (pid=%s)", holder
                        )
                        try:
                            self._resolved_path.unlink()
                        except OSError:
                            pass
                        # Retry once.
                        fd = os.open(
                            str(self._resolved_path),
                            os.O_RDWR | os.O_CREAT,
                            0o644,
                        )
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except OSError as exc2:
                            os.close(fd)
                            if exc2.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                                raise MergeLockBusy(self._resolved_path, self._read_holder_pid())
                            raise
                        self._fd = fd
                        self._write_pid()
                        return
                    raise MergeLockStale(self._resolved_path, holder)
                raise MergeLockBusy(self._resolved_path, holder)
            raise

        # flock succeeded — no other process holds the lock.  Check the
        # recorded PID to detect a stale lock left by a crashed process.
        recorded = self._read_holder_pid()
        if recorded is not None and recorded != os.getpid():
            if not self._is_pid_alive(recorded):
                if not break_stale:
                    # Stale lock detected and caller does not want to break it.
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                    raise MergeLockStale(self._resolved_path, recorded)
                # Stale lock — overwrite with our PID.
                logger.warning("Overwriting stale merge lock (pid=%s)", recorded)

        self._fd = fd
        self._write_pid()
        logger.debug("Acquired merge lock: %s", self._resolved_path)

    def release(self) -> None:
        """Release the lock and close the file descriptor."""
        if self._fd is not None:
            try:
                # LOCK_UN is optional — closing the fd releases the lock
                # on all POSIX systems.  We call it explicitly for clarity.
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            logger.debug("Released merge lock: %s", self._resolved_path)

    def __enter__(self) -> MergeLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def acquire_merge_lock(
    project_root: Path, *, break_stale: bool = False
) -> MergeLock:
    """Convenience factory: acquire and return a held :class:`MergeLock`.

    The caller is responsible for calling ``release()`` or using the
    context-manager protocol.

    Raises:
        MergeLockBusy: Another process holds the lock.
        MergeLockStale: The lock appears stale.
    """
    lock = MergeLock(project_root)
    lock.acquire(break_stale=break_stale)
    return lock
