"""Merge lock — the project's main-worktree mutex.

Wraps a dedicated lock file with ``fcntl.flock``. Two acquisition modes
are supported:

* Non-blocking (default, ``acquire()`` / ``acquire(blocking=False)``):
  uses ``LOCK_EX | LOCK_NB`` so competing processes receive an immediate
  error (:class:`MergeLockBusy`) rather than queuing.
* Blocking (``acquire(blocking=True)`` or ``MergeLock(root, blocking=True)``):
  uses ``LOCK_EX`` (no ``LOCK_NB``) so competing callers queue and wait
  until the current holder releases the lock. This is the "main-worktree
  mutex" semantics used by ``luo merge`` and synchronous ``luo run`` so
  that only one holder mutates the main working tree at a time.

The lock is automatically released when the file descriptor is closed
(process exit, context-manager exit, or explicit release) — including a
crashed holder, which the kernel releases on process exit so the blocking
queue cannot wedge indefinitely.

Staleness detection (non-blocking mode) is based on the lock-holder's
PID: if the PID stored in the lock file no longer exists, the lock is
considered stale and can be broken. The blocking mode does not need this
because the kernel guarantees exclusivity on return from ``flock``.
"""

from __future__ import annotations
from tianluo.runtime_paths import runtime_dir

import errno
import fcntl
import logging
import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default path relative to project root.
# Sentinel default: resolved root-aware (tianluo/ or legacy se3/) at use
# time via _resolve_lock_path(); an explicit lock_path bypasses resolution.
_DEFAULT_LOCK_PATH = Path("tianluo/state/merge.lock")


def _resolve_lock_path(project_root: Path, lock_path: Path) -> Path:
    """Resolve *lock_path* against *project_root*, honouring the runtime dir.

    The default sentinel maps to ``<runtime>/state/merge.lock`` for the
    project's actual layout; any other relative path joins the root as-is.
    """
    if lock_path.is_absolute():
        return lock_path
    if lock_path == _DEFAULT_LOCK_PATH:
        return runtime_dir(project_root) / "state" / "merge.lock"
    return Path(project_root) / lock_path


# Module-level registry of resolved lock paths held by *this* process.
# Used by :func:`is_lock_held_in_process` so that an inner caller (e.g.
# the orchestrator) can detect that an outer caller (e.g. the CLI
# wrapper in ``merge_cmd.run_merge``) already holds the lock and skip
# re-acquisition rather than risk a same-process flock collision whose
# semantics vary by platform (Linux same-OFD success vs different-OFD
# EAGAIN).  Keyed by the absolute resolved lock-file path string so two
# distinct project roots that happen to point at the same lock file
# (rare but possible via symlinks / bind mounts) share an entry.
_HELD_LOCK_PATHS: set[str] = set()


def is_lock_held_in_process(project_root: Path, lock_path: Path = _DEFAULT_LOCK_PATH) -> bool:
    """Return True when this process already holds the merge lock.

    The check is purely advisory and based on the in-memory registry
    populated by :class:`MergeLock` itself — it makes no syscalls and
    cannot detect a lock held by some other process.  Callers use this
    to short-circuit redundant acquisition attempts (the orchestrator's
    ``acquire_lock=True`` default behaviour when the CLI wrapper
    already holds the lock).
    """
    resolved = _resolve_lock_path(project_root, lock_path)
    return str(resolved) in _HELD_LOCK_PATHS


def _stale_break_jitter() -> float:
    """Return a random fraction in ``[0, 1)`` for backoff jitter.

    Wrapped in a module-level function so tests can monkeypatch a
    deterministic value without touching ``random`` globally.
    """
    return random.random()


class MergeLockBusy(RuntimeError):
    """Another ``luo merge`` process holds the lock."""

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

    def __init__(
        self,
        lock_file: Path,
        holder_pid: Optional[int] = None,
        *,
        corrupt: bool = False,
    ) -> None:
        self.lock_file = lock_file
        self.holder_pid = holder_pid
        # Distinguish "no PID recorded" (legitimately empty file) from
        # "PID record present but unparseable" so operators can see in
        # the error message whether the lock file is corrupt and needs
        # manual cleanup, vs. simply not yet populated.
        self.corrupt = corrupt
        if holder_pid is None:
            if corrupt:
                msg = (
                    f"Merge lock PID record is corrupt and could not be "
                    f"parsed as an integer (unparseable): {lock_file}. "
                    f"Operator action required: inspect the file and "
                    f"delete it if no other process is holding the lock."
                )
            else:
                msg = f"Merge lock appears stale (no pid recorded): {lock_file}"
        else:
            msg = f"Merge lock appears stale (pid={holder_pid} does not exist): {lock_file}"
        super().__init__(msg)


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
    lock_path: Path = field(default_factory=lambda: _DEFAULT_LOCK_PATH)
    # Default acquisition mode used by the context-manager protocol
    # (``with MergeLock(...)``). When True, ``__enter__`` blocks on
    # ``LOCK_EX`` until the lock is free (queueing semantics) instead of
    # failing fast with ``MergeLockBusy``. The explicit ``acquire(...)``
    # call can still override per-invocation via its own ``blocking``
    # argument; this field only seeds the context-manager default so
    # callers can write ``with MergeLock(root, blocking=True):``.
    blocking: bool = False
    _fd: Optional[int] = None
    # Sticky flag set by ``_read_holder_pid`` when the on-disk PID record
    # could not be parsed as an integer.  Read by ``acquire()`` to surface
    # corruption-specific diagnostics in ``MergeLockStale`` rather than
    # conflating it with "no PID recorded".
    _last_read_corrupt: bool = False

    def __post_init__(self) -> None:
        if self.lock_path.is_absolute():
            self._resolved_path = self.lock_path
        else:
            self._resolved_path = self.project_root / self.lock_path

    # PID record is fixed-width: 16-digit zero-padded PID + newline = 17 bytes.
    # Readers and writers BOTH respect this length so a single os.pwrite that
    # overwrites the prefix produces a fully consistent record without ever
    # going through a transient empty state.
    _PID_RECORD_LEN = 17

    # Bounded retry policy for the stale-lock break path.  Multiple
    # processes that simultaneously detect the same stale lock could
    # otherwise loop unlink → create → unlink → create indefinitely.
    # The retry cap plus exponential backoff with jitter ensures any
    # such race terminates quickly.
    _MAX_BREAK_STALE_ATTEMPTS = 3
    _BREAK_STALE_BASE_BACKOFF_S = 0.05  # 50ms base
    _MAX_BREAK_STALE_BACKOFF_S = 0.5    # 500ms cap

    @property
    def held(self) -> bool:
        """Return True when *this* instance currently holds the lock fd.

        Unlike :func:`is_lock_held_in_process` (which consults the
        process-wide held-paths registry by path), this reflects only
        whether this specific :class:`MergeLock` object has an open,
        acquired descriptor. Callers that lazily acquire a single lock
        instance over the lifetime of a run use it to decide whether the
        lock still needs to be taken.
        """
        return self._fd is not None

    def _read_holder_pid(self) -> Optional[int]:
        """Return the PID stored in the lock file, or None.

        Side effect: clears ``self._last_read_corrupt`` at the start of
        the call and sets it to ``True`` if the on-disk bytes could not
        be parsed as an integer.  Callers that distinguish "no PID
        recorded" from "corrupt PID" SHOULD inspect this flag after the
        call returns ``None``.

        Reads at most ``_PID_RECORD_LEN`` bytes so that any trailing
        bytes left over from a longer legacy record (or from the brief
        window inside ``_write_pid`` between the prefix overwrite and
        the truncation) cannot poison the integer parse.  Combined with
        ``_write_pid``'s fixed-width prefix overwrite, this gives the
        reader an atomic view of the PID even when contended.

        Uses ``O_NOFOLLOW`` for TOCTOU consistency with ``acquire()``:
        if a symlink exists at the lock-file path, refuse to follow it
        rather than reading an unrelated file's content as the PID.

        Reset the corruption flag at the start so each call gives an
        accurate signal — a previous corrupt read does not leak into a
        subsequent successful read.

        FD ownership: when ``os.fdopen`` succeeds, the returned file
        object adopts the descriptor — its context manager (``with``)
        closes the fd on exit, including on exceptions.  We therefore
        only call ``os.close`` ourselves when ``os.fdopen`` itself
        raised before adoption.  Calling ``os.close`` after the with
        block exited would double-close (raising EBADF, which the outer
        except previously swallowed silently and could mask legitimate
        I/O errors and trigger unwarranted stale-lock-break paths).
        """
        # Reset corruption flag so callers see only THIS read's status.
        self._last_read_corrupt = False
        try:
            fd = os.open(
                str(self._resolved_path),
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError:
            return None

        # Wrap fdopen separately so a failure there can still close fd
        # without conflicting with the file object's own ownership.
        try:
            file_obj = os.fdopen(fd, "rb")
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            return None

        # file_obj now owns fd; closing it (via the with block) closes fd.
        # Do NOT call os.close(fd) anywhere below — the with block handles it.
        try:
            with file_obj as f:
                data = f.read(self._PID_RECORD_LEN)
        except OSError:
            return None

        try:
            text = data.decode("utf-8", errors="ignore").strip()
            if text:
                return int(text)
        except ValueError:
            # Unparseable PID record (legacy format, partial write, etc.).
            # Distinguish this from "no PID recorded" by setting a
            # sticky flag the caller (acquire()) can read so the
            # surfaced ``MergeLockStale`` message points at corruption
            # rather than the legitimate "fresh file" case.  Without
            # this, a corrupted lock file becomes indistinguishable
            # from "no PID recorded" and operators have no signal to
            # act on.
            self._last_read_corrupt = True
            logger.warning(
                "Merge lock PID record at %s is corrupt and could not be "
                "parsed as an integer: data=%r — treating as stale. "
                "Operators may need to delete this file manually.",
                self._resolved_path, text,
            )
            return None
        return None

    def _write_pid(self) -> None:
        """Update the PID inside the lock file via the held fd.

        Writes the fixed-width PID record at offset 0 BEFORE truncating
        any trailing bytes from a previous longer record.  Because the
        record length is constant (``_PID_RECORD_LEN`` = 17 bytes) and
        ``_read_holder_pid`` reads at most that many bytes, a concurrent
        reader never sees a partial state:

          * Before our write begins: reader sees the full previous record.
          * After ``os.pwrite`` returns: the first ``_PID_RECORD_LEN``
            bytes are the new record; trailing bytes (if any) are stale,
            but readers cap their read at ``_PID_RECORD_LEN`` so they
            never observe them.
          * After ``os.ftruncate`` returns: trailing bytes are gone.

        This eliminates the truncate-then-write window where a reader
        could observe an empty file (``""`` → ``int("")`` raises and
        the holder appears stale).

        Writing through the held fd preserves the ``flock`` binding
        (a temp-file + rename approach would replace the inode the
        lock is held on, defeating the lock's mutual exclusion).
        """
        if self._fd is None:
            raise RuntimeError(
                "_write_pid called without an acquired lock fd"
            )
        # Fixed-width PID record so every write overwrites the entire
        # previous content.  A 16-digit field plus newline covers all
        # realistic PIDs (max 2^22 on Linux ≈ 4 million, well under
        # 16 digits).
        pid_str = f"{os.getpid():016d}\n"
        data = pid_str.encode("utf-8")
        # Explicit raise rather than ``assert`` so the invariant survives
        # ``python -O`` (which strips assert statements) and so the lint
        # rule "no production assert as runtime validation" is satisfied.
        # The PID format is fixed-width, so a mismatch would indicate a
        # programmer error in the encoding above; raise loudly.
        if len(data) != self._PID_RECORD_LEN:
            raise RuntimeError(
                f"PID record length mismatch: {len(data)} != {self._PID_RECORD_LEN}"
            )
        # Atomic prefix overwrite via os.pwrite (does not move the fd's
        # file offset, so concurrent readers using a separate fd are not
        # affected).  Falls back to lseek+write on platforms without
        # pwrite (extremely rare on POSIX).
        if hasattr(os, "pwrite"):
            written = 0
            while written < len(data):
                n = os.pwrite(self._fd, data[written:], written)
                if n <= 0:
                    raise OSError("os.pwrite returned non-positive")
                written += n
        else:
            os.lseek(self._fd, 0, os.SEEK_SET)
            written = 0
            while written < len(data):
                n = os.write(self._fd, data[written:])
                if n <= 0:
                    raise OSError("os.write returned non-positive")
                written += n
        # Truncate AFTER the prefix overwrite so any trailing bytes from
        # a previous longer record are removed.  Readers cap their read
        # at _PID_RECORD_LEN so the trailing bytes are never observable.
        os.ftruncate(self._fd, len(data))
        os.fsync(self._fd)
        # fsync the parent directory to ensure file metadata is durable.
        dir_fd = os.open(self._resolved_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _is_pid_alive(self, pid: int) -> bool:
        """Check whether *pid* exists in the current process namespace.

        Sends signal 0 (no-op) to *pid* and interprets the result:

        * Success → process exists and we can signal it → alive.
        * ESRCH  → process does not exist → dead.
        * EPERM  → process exists but we lack permission (different
          UID, capabilities) → conservatively treat as alive so we
          do NOT break a lock that may belong to a still-running
          owner of another user.

        Container/PID-namespace caveat: when the lock file lives on a
        shared volume mounted across containers with isolated PID
        namespaces (e.g. Docker/Kubernetes pods sharing a persistent
        volume), a stale lock written by container A's pid 12345 will
        be checked against container B's pid 12345 — which in B's
        namespace is a *different* (or missing) process.  In that
        environment ``os.kill(12345, 0)`` either returns success
        (B has its own pid 12345 unrelated to A's) or ESRCH.  We
        cannot disambiguate "alive in another namespace" from "alive
        in mine" with a kill probe.  Operators running luo across
        container boundaries on a shared volume should not rely on
        ``break_stale=True`` recovery — they should instead bind-mount
        a per-container lock directory or share the host PID namespace.
        Documented here rather than fixed because the correct fix is
        deployment-specific (PID namespace inspection is not available
        from inside a sandboxed container).
        """
        try:
            os.kill(pid, 0)
            return True
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return False
            # EPERM means the process exists but we lack permission to
            # signal it — for lock staleness purposes, treat as alive.
            return True

    def _try_break_stale_and_acquire(
        self,
        open_flags: int,
        log_message: str,
        busy_pid: Optional[int],
    ) -> int:
        """Break a stale lock file and re-acquire flock once.

        Encapsulates the unlink-then-retry pattern so the holder-dead
        and unparseable-pid branches share the same backoff and retry
        cap.

        Args:
            open_flags: The ``os.open`` flags to reuse for the retry.
            log_message: Warning to log before unlinking.
            busy_pid: PID to surface in :class:`MergeLockBusy` if the
                retry's flock call still fails (None when the original
                holder PID was unparseable).

        Returns:
            The acquired file descriptor (caller MUST assign it to
            ``self._fd`` and call ``_write_pid`` after this returns).

        Raises:
            MergeLockBusy: When the retry's flock fails — another
                process won the race to acquire after our unlink.
            OSError: For non-EAGAIN errno values from flock.
        """
        # Bounded retry to avoid a thundering-herd race when multiple
        # would-be holders all detect the same stale lock simultaneously
        # and end up looping unlink → create → unlink → create.  We try
        # up to ``_MAX_BREAK_STALE_ATTEMPTS`` times with exponential
        # backoff capped by ``_MAX_BREAK_STALE_BACKOFF_S`` plus a tiny
        # randomized jitter so co-firing processes desynchronise.
        # Explicit terminal-attempt-state tracking: each attempt's
        # outcome ('busy' = a foreign holder still owns the lock, or
        # 'oserror' = some other errno from flock) is recorded in a
        # single `last_outcome` variable instead of relying on which
        # of two parallel sentinels was last cleared.  A future
        # refactor cannot accidentally surface a stale 'busy' verdict
        # when the latest attempt actually died on a non-EAGAIN errno
        # (or vice versa) because the variable always describes the
        # *most recent* attempt, not "whichever sentinel happened to
        # be set".
        last_busy_pid: Optional[int] = busy_pid
        last_oserror: Optional[OSError] = None
        last_outcome: Optional[str] = "busy" if busy_pid is not None else None
        for attempt in range(self._MAX_BREAK_STALE_ATTEMPTS):
            if attempt == 0:
                logger.warning("%s (attempt %d)", log_message, attempt + 1)
            else:
                logger.info(
                    "Retrying stale-lock break (attempt %d/%d)",
                    attempt + 1, self._MAX_BREAK_STALE_ATTEMPTS,
                )
                # Exponential backoff with jitter, capped.
                delay = min(
                    self._BREAK_STALE_BASE_BACKOFF_S * (2 ** (attempt - 1)),
                    self._MAX_BREAK_STALE_BACKOFF_S,
                )
                # Half-jitter (10%–110% of base) to reduce co-firing.
                jitter = delay * (0.1 + 0.9 * _stale_break_jitter())
                import time as _time

                _time.sleep(jitter)

            try:
                self._resolved_path.unlink()
            except OSError:
                pass

            fd = os.open(
                str(self._resolved_path),
                open_flags,
                0o644,
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc2:
                os.close(fd)
                if exc2.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    # Refresh the holder PID so the surfaced error
                    # carries the latest known value.
                    last_busy_pid = self._read_holder_pid()
                    last_outcome = "busy"
                    continue
                last_oserror = exc2
                last_outcome = "oserror"
                continue
            return fd

        # Surface based on the explicit terminal-attempt outcome rather
        # than which sentinel happened to be non-None.
        if last_outcome == "oserror" and last_oserror is not None:
            raise last_oserror
        raise MergeLockBusy(self._resolved_path, last_busy_pid)

    def acquire(self, break_stale: bool = False, blocking: bool = False) -> None:
        """Acquire the exclusive merge lock.

        Args:
            break_stale: If ``True`` and the lock appears stale (holder
                PID does not exist), remove the old lock file and retry
                once.  Defaults to ``False`` to avoid races. Has no
                effect when ``blocking=True`` (see below).
            blocking: If ``True``, queue on ``fcntl.flock(LOCK_EX)``
                (without ``LOCK_NB``) and wait until the current holder
                releases the lock, then acquire it. This gives the merge
                lock blocking "main-worktree mutex" semantics — competing
                ``luo merge`` / synchronous ``luo run`` callers serialise
                rather than failing fast. Defaults to ``False`` (the
                legacy non-blocking ``LOCK_EX | LOCK_NB`` fail-fast path
                that raises :class:`MergeLockBusy` / :class:`MergeLockStale`
                on contention).

        Raises:
            MergeLockBusy: Another process holds the lock (non-blocking
                mode only).
            MergeLockStale: The lock appears stale (non-blocking mode and
                ``break_stale=False`` only).
        """
        # Idempotent: if this instance already holds the lock, noop.
        if self._fd is not None:
            return

        self._resolved_path.parent.mkdir(parents=True, exist_ok=True)

        # Open in read-write mode (required for flock on some platforms).
        # Add O_NOFOLLOW for TOCTOU consistency with runtime_sync: if a
        # symlink exists at the lock-file path, refuse to follow it rather
        # than silently binding flock to an unintended file.
        open_flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            open_flags |= os.O_NOFOLLOW
        fd = os.open(
            str(self._resolved_path),
            open_flags,
            0o644,
        )

        if blocking:
            # Blocking acquisition: queue on LOCK_EX (no LOCK_NB) until
            # the current holder releases the lock. The kernel releases
            # an flock automatically when the holding process exits — so
            # a crashed holder cannot wedge the queue indefinitely and no
            # explicit PID stale-detection / break path is needed here
            # (it remains intact on the non-blocking path below). Once
            # flock returns we are guaranteed to be the sole holder, so we
            # simply overwrite the recorded PID with our own.
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                os.close(fd)
                raise
            self._fd = fd
            self._write_pid()
            _HELD_LOCK_PATHS.add(str(self._resolved_path))
            logger.debug(
                "Acquired merge lock (blocking): %s", self._resolved_path
            )
            return

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                holder = self._read_holder_pid()
                if holder is not None and not self._is_pid_alive(holder):
                    if break_stale:
                        fd = self._try_break_stale_and_acquire(
                            open_flags=open_flags,
                            log_message=(
                                f"Breaking stale merge lock (pid={holder})"
                            ),
                            busy_pid=holder,
                        )
                        self._fd = fd
                        self._write_pid()
                        _HELD_LOCK_PATHS.add(str(self._resolved_path))
                        return
                    raise MergeLockStale(self._resolved_path, holder)
                # K8: If the lock file exists but the PID could not be parsed
                # (legacy format, partial read, or corruption), treat it as
                # stale when break_stale is True so operators have a path
                # forward; when break_stale is False, raise MergeLockStale
                # with a None holder so the caller sees actionable advice.
                if holder is None:
                    is_corrupt = self._last_read_corrupt
                    if break_stale:
                        fd = self._try_break_stale_and_acquire(
                            open_flags=open_flags,
                            log_message=(
                                "Breaking stale merge lock (corrupt pid record)"
                                if is_corrupt
                                else "Breaking stale merge lock (no pid recorded)"
                            ),
                            busy_pid=None,
                        )
                        self._fd = fd
                        self._write_pid()
                        _HELD_LOCK_PATHS.add(str(self._resolved_path))
                        return
                    raise MergeLockStale(
                        self._resolved_path, None, corrupt=is_corrupt,
                    )
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
        # Register in the module-level held-paths set so an inner
        # caller (orchestrator) can detect that this process already
        # holds the lock and skip redundant acquisition.
        _HELD_LOCK_PATHS.add(str(self._resolved_path))
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
            # Discard so a future ``is_lock_held_in_process`` reflects
            # the real state.  ``discard`` is a no-op when the path is
            # not present (idempotent on double release).
            _HELD_LOCK_PATHS.discard(str(self._resolved_path))
            logger.debug("Released merge lock: %s", self._resolved_path)

    def __enter__(self) -> MergeLock:
        self.acquire(blocking=self.blocking)
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


@dataclass
class LockStatus:
    """Read-only snapshot of the merge lock file's state.

    Produced by :func:`inspect_lock` and shared by the release decision
    logic and the CLI status display so a single probe drives both.

    Attributes:
        lock_file: Absolute path of the lock file (whether or not it exists).
        exists: Whether the lock file is present on disk.
        holder_pid: PID recorded in the lock file, or ``None`` when the
            file is absent, empty, or carries an unparseable record.
        alive: Whether ``holder_pid`` refers to a live process. Always
            ``False`` when ``holder_pid`` is ``None``.
        stale: Whether the lock can be cleaned up without ``--force`` —
            true when the file is absent, the holder PID is dead, no PID
            is recorded, or the record is corrupt.
        corrupt: Whether a PID record was present but could not be parsed
            as an integer.
    """

    lock_file: Path
    exists: bool
    holder_pid: Optional[int]
    alive: bool
    stale: bool
    corrupt: bool


@dataclass
class ReleaseOutcome:
    """Result of a :func:`release_merge_lock` decision.

    Attributes:
        exit_code: ``0`` on success (released, or nothing to release);
            non-zero (``1``) when release was refused because the holder
            is still alive and ``force`` was not given, or when the lock
            file could not actually be removed (e.g. permission error).
        status: The :class:`LockStatus` observed before any action, for
            the caller to print.
        action: One of ``'no_lock'``, ``'released_stale'``,
            ``'released_force'``, ``'refused_alive'``, or ``'failed_remove'``.
    """

    exit_code: int
    status: LockStatus
    action: str


def inspect_lock(
    project_root: Path, lock_path: Path = _DEFAULT_LOCK_PATH
) -> LockStatus:
    """Read-only probe of the merge lock file's current state.

    Performs no writes. Reuses :meth:`MergeLock._read_holder_pid` and
    :meth:`MergeLock._is_pid_alive` so the staleness semantics match the
    acquisition path exactly. A non-existent lock file is reported as
    ``stale=True`` (nothing to hold means it is freely reclaimable).
    """
    probe = MergeLock(project_root, lock_path=lock_path)
    resolved = probe._resolved_path
    if not resolved.is_absolute():
        resolved = (Path.cwd() / resolved)

    if not resolved.exists():
        return LockStatus(
            lock_file=resolved,
            exists=False,
            holder_pid=None,
            alive=False,
            stale=True,
            corrupt=False,
        )

    holder = probe._read_holder_pid()
    corrupt = probe._last_read_corrupt
    if holder is None:
        # No PID recorded, or an unparseable (corrupt) record — both are
        # treated as stale (reclaimable without --force).
        return LockStatus(
            lock_file=resolved,
            exists=True,
            holder_pid=None,
            alive=False,
            stale=True,
            corrupt=corrupt,
        )

    alive = probe._is_pid_alive(holder)
    return LockStatus(
        lock_file=resolved,
        exists=True,
        holder_pid=holder,
        alive=alive,
        stale=not alive,
        corrupt=False,
    )


def break_lock_file(
    project_root: Path, lock_path: Path = _DEFAULT_LOCK_PATH
) -> bool:
    """Remove the lock file, the same way the stale-break path does.

    flock's kernel lock is bound to the holding process's fd, so an
    external process cannot truly revoke it — it can only unlink the lock
    file so the next acquirer recreates it. This mirrors the unlink in
    :meth:`MergeLock._try_break_stale_and_acquire`.

    Returns:
        ``True`` when a file was actually removed, ``False`` when there
        was nothing to remove.
    """
    probe = MergeLock(project_root, lock_path=lock_path)
    resolved = probe._resolved_path
    existed = resolved.exists()
    try:
        resolved.unlink(missing_ok=True)
    except OSError:
        # A race (another process removed it first) or a permission issue;
        # report removal as not-performed rather than raising.
        return False
    return existed


def release_merge_lock(
    project_root: Path, *, force: bool, lock_path: Path = _DEFAULT_LOCK_PATH
) -> ReleaseOutcome:
    """Decide and perform a manual merge-lock release (scheme A semantics).

    Pure decision logic — does not print. Combines :func:`inspect_lock`
    with :func:`break_lock_file`:

    * No lock file → ``no_lock`` (exit 0, no unlink).
    * Stale lock (dead holder / no PID / corrupt record) → ``released_stale``
      (exit 0, lock file removed) without requiring ``force``.
    * Live holder and ``force`` is False → ``refused_alive`` (exit 1, lock
      preserved).
    * Live holder and ``force`` is True → ``released_force`` (exit 0, lock
      file removed).
    """
    status = inspect_lock(project_root, lock_path=lock_path)

    if not status.exists:
        return ReleaseOutcome(exit_code=0, status=status, action="no_lock")

    if status.stale:
        if _lock_file_removed(project_root, lock_path=lock_path):
            return ReleaseOutcome(
                exit_code=0, status=status, action="released_stale"
            )
        return ReleaseOutcome(
            exit_code=1, status=status, action="failed_remove"
        )

    # Holder is alive from here on.
    if not force:
        return ReleaseOutcome(exit_code=1, status=status, action="refused_alive")

    if _lock_file_removed(project_root, lock_path=lock_path):
        return ReleaseOutcome(exit_code=0, status=status, action="released_force")
    return ReleaseOutcome(exit_code=1, status=status, action="failed_remove")


def _lock_file_removed(
    project_root: Path, lock_path: Path = _DEFAULT_LOCK_PATH
) -> bool:
    """Attempt to remove the lock file and confirm it is actually gone.

    :func:`break_lock_file` swallows ``OSError`` (e.g. ``PermissionError``,
    which ``Path.unlink`` raises even with ``missing_ok=True`` since that flag
    only suppresses ``FileNotFoundError``). Its boolean return alone cannot
    distinguish "nothing to remove" from "could not remove", so we re-probe
    the path afterwards: the release only succeeded if the file no longer
    exists on disk.
    """
    break_lock_file(project_root, lock_path=lock_path)
    probe = MergeLock(project_root, lock_path=lock_path)
    return not probe._resolved_path.exists()


def acquire_merge_lock(
    project_root: Path, *, break_stale: bool = False, blocking: bool = False
) -> MergeLock:
    """Convenience factory: acquire and return a held :class:`MergeLock`.

    The caller is responsible for calling ``release()`` or using the
    context-manager protocol.

    Args:
        break_stale: Forwarded to :meth:`MergeLock.acquire` (non-blocking
            mode only).
        blocking: When ``True``, queue on the lock until it is free rather
            than failing fast (see :meth:`MergeLock.acquire`).

    Raises:
        MergeLockBusy: Another process holds the lock (non-blocking mode).
        MergeLockStale: The lock appears stale (non-blocking mode).
    """
    lock = MergeLock(project_root)
    lock.acquire(break_stale=break_stale, blocking=blocking)
    return lock
