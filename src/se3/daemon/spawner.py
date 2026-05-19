"""Spawns new ``se3 run`` flows on behalf of the SE3 daemon.

:class:`DaemonSpawner` starts ``se3 run --output-format json <task>`` child
processes via :class:`subprocess.Popen`. The daemon is a *parent* of the flow,
never an in-process caller: this keeps ``se3 run`` independently testable and
ensures a daemon crash does not take a running flow down with it.

The spawner deliberately passes ``--output-format json`` so the child emits the
unified structured event stream as NDJSON on stdout. Because a real flow emits
far more output than the OS pipe buffer can hold (~64 KB), the child's stdout
and stderr are redirected to per-flow log files under
``<project_root>/se3/logs/daemon/`` rather than to ``subprocess.PIPE``. This
guarantees the child can always write without blocking — even when nothing
actively consumes its output — so a spawned flow never deadlocks. :meth:`iter_events`
tails the stdout log file and yields the NDJSON events back as parsed dicts.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

SpawnCallback = Callable[["SpawnedProcess"], None]


@dataclass
class SpawnedProcess:
    """A ``se3 run`` child process started by the daemon.

    Attributes:
        process: The underlying :class:`subprocess.Popen` handle.
        project_root: Directory the flow runs in (the child's ``cwd``).
        task_description: The task passed to ``se3 run``.
        args: The full argv used to launch the child.
        started_at: Unix epoch seconds at spawn time.
        stdout_log: File the child's stdout (NDJSON event stream) is written
            to, or ``None`` when output was redirected to ``/dev/null``.
        stderr_log: File the child's stderr is written to, or ``None`` when
            output was redirected to ``/dev/null``.
    """

    process: subprocess.Popen
    project_root: str
    task_description: str
    args: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    stdout_log: Optional[Path] = None
    stderr_log: Optional[Path] = None

    @property
    def pid(self) -> int:
        """The child's process id."""
        return self.process.pid

    @property
    def returncode(self) -> Optional[int]:
        """The child's exit code, or ``None`` while it is still running."""
        return self.process.returncode

    @property
    def is_running(self) -> bool:
        """Whether the child is still executing (non-blocking probe)."""
        return self.process.poll() is None

    def to_dict(self) -> Dict[str, object]:
        """Return a JSON-serializable representation of this process."""
        return {
            "pid": self.pid,
            "project_root": self.project_root,
            "task_description": self.task_description,
            "returncode": self.returncode,
            "is_running": self.is_running,
            "started_at": self.started_at,
            "stdout_log": str(self.stdout_log) if self.stdout_log else None,
            "stderr_log": str(self.stderr_log) if self.stderr_log else None,
        }


def _resolve_se3_command() -> List[str]:
    """Return the argv prefix that invokes the ``se3`` CLI.

    Prefers the installed ``se3`` console script; falls back to
    ``python -m se3`` so the daemon works even when the script directory is
    not on ``PATH``.
    """
    script = shutil.which("se3")
    if script:
        return [script]
    return [sys.executable, "-m", "se3"]


class DaemonSpawner:
    """Starts and manages ``se3 run`` child processes."""

    def __init__(
        self,
        supervisor: Optional[object] = None,
        *,
        on_spawn: Optional[SpawnCallback] = None,
        on_exit: Optional[SpawnCallback] = None,
    ) -> None:
        """Create a spawner.

        Args:
            supervisor: Optional :class:`~se3.daemon.supervisor.DaemonSupervisor`.
                When supplied, every spawned process is registered with it and
                de-registered on reap.
            on_spawn: Optional callback invoked just after a child starts.
            on_exit: Optional callback invoked when a child is reaped.
        """
        self._supervisor = supervisor
        self._on_spawn = on_spawn
        self._on_exit = on_exit
        self._processes: Dict[int, SpawnedProcess] = {}
        self._spawn_counter = 0

    # -- spawning ----------------------------------------------------------

    def spawn(
        self,
        task_description: str,
        *,
        project_root: Optional[str] = None,
        task_type: str = "feature",
        discover: bool = False,
        extra_args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> SpawnedProcess:
        """Start a new ``se3 run`` flow and return its :class:`SpawnedProcess`.

        The child runs ``se3 run <task> --type <task_type> --output-format json``
        with the daemon's environment inherited (so the Python path stays
        correct) and ``cwd`` set to *project_root*.

        When *discover* is true, ``--discover`` is appended so the flow starts
        from the discovery step (the web ``POST /api/flows`` "start from
        discovery" option threads its ``discover`` flag down to here via the
        SPAWN_FLOW payload).

        The child's stdout and stderr are redirected to per-flow log files (not
        to OS pipes) so the child can always write without blocking, even when
        no caller is draining its output. This is essential because a real flow
        emits far more than the ~64 KB OS pipe buffer would hold; piping
        without a reader would deadlock the child.
        """
        cwd = str(Path(project_root).resolve()) if project_root else os.getcwd()
        args = _resolve_se3_command() + [
            "run",
            task_description,
            "--type",
            task_type,
            "--output-format",
            "json",
        ]
        if discover:
            args.append("--discover")
        if extra_args:
            args.extend(extra_args)
        return self._launch(args, cwd, task_description, env)

    def resume(
        self,
        flow_id: str,
        *,
        project_root: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> SpawnedProcess:
        """Resume a paused flow by spawning ``se3 run --resume --flow-id``.

        A daemon-spawned flow (``se3 run --output-format json``) exits its
        process whenever it pauses for a human call — for example a discovery
        clarification. Once the web UI answers and the daemon writes the
        ``.response`` file, *something* must re-run ``se3 run --resume`` for the
        paused flow or it would stay PAUSED forever and never reach analyze /
        implement. This method is that something: it relaunches the flow with
        ``--resume --flow-id <flow_id>`` so the conversation continues from the
        persisted state.
        """
        cwd = str(Path(project_root).resolve()) if project_root else os.getcwd()
        args = _resolve_se3_command() + [
            "run",
            "--resume",
            "--flow-id",
            str(flow_id),
            "--output-format",
            "json",
        ]
        logger.info("Resuming flow %s in %s", flow_id, cwd)
        return self._launch(args, cwd, f"[resume {flow_id}]", env)

    def _launch(
        self,
        args: List[str],
        cwd: str,
        task_description: str,
        env: Optional[Dict[str, str]],
    ) -> SpawnedProcess:
        """Start *args* as a detached ``se3 run`` child and track it.

        Shared by :meth:`spawn` and :meth:`resume`: both differ only in the
        argv they build; the environment inheritance, per-flow log-file
        redirection, supervisor registration and ``on_spawn`` callback are
        identical.
        """
        child_env = os.environ.copy()
        if env:
            child_env.update(env)

        self._spawn_counter += 1
        stdout_path, stderr_path = self._allocate_log_paths(cwd)
        stdout_target, stderr_target, open_handles = self._open_log_targets(
            stdout_path, stderr_path
        )

        logger.info("Spawning se3 run flow in %s", cwd)
        try:
            process = subprocess.Popen(
                args,
                cwd=cwd,
                env=child_env,
                stdout=stdout_target,
                stderr=stderr_target,
                stdin=subprocess.DEVNULL,
            )
        finally:
            # The child has dup'd its own descriptors; the daemon must not keep
            # the parent-side handles open or it leaks fds over its lifetime.
            for handle in open_handles:
                try:
                    handle.close()
                except OSError:  # pragma: no cover - defensive
                    pass
        spawned = SpawnedProcess(
            process=process,
            project_root=cwd,
            task_description=task_description,
            args=args,
            stdout_log=stdout_path,
            stderr_log=stderr_path,
        )
        self._processes[spawned.pid] = spawned

        if self._supervisor is not None:
            try:
                self._supervisor.register(
                    spawned.pid,
                    cwd,
                    task_description=task_description,
                    origin="spawned",
                )
            except Exception:  # pragma: no cover - defensive
                logger.exception("Failed to register spawned flow with supervisor")

        if self._on_spawn is not None:
            try:
                self._on_spawn(spawned)
            except Exception:  # pragma: no cover - defensive
                logger.exception("on_spawn callback failed")

        return spawned

    def _allocate_log_paths(
        self, cwd: str
    ) -> tuple[Optional[Path], Optional[Path]]:
        """Return (stdout_log, stderr_log) paths for a new flow, or (None, None).

        Logs live under ``<project_root>/se3/logs/daemon/``. When that
        directory cannot be created, returns ``(None, None)`` so the caller
        falls back to ``/dev/null``.
        """
        log_dir = Path(cwd) / "se3" / "logs" / "daemon"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - defensive
            logger.warning("Could not create daemon log dir %s", log_dir)
            return None, None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = f"flow_{stamp}_{self._spawn_counter}"
        return log_dir / f"{base}.ndjson", log_dir / f"{base}.stderr.log"

    @staticmethod
    def _open_log_targets(
        stdout_path: Optional[Path], stderr_path: Optional[Path]
    ) -> tuple[object, object, List[object]]:
        """Open the log files for redirection.

        Returns ``(stdout_target, stderr_target, open_handles)`` where the
        targets are passed to :class:`subprocess.Popen` and ``open_handles`` is
        the list of file objects the caller must close once the child holds its
        own descriptors. Falls back to ``subprocess.DEVNULL`` when a path is
        ``None`` or cannot be opened.
        """
        handles: List[object] = []

        def _open(path: Optional[Path]) -> object:
            if path is None:
                return subprocess.DEVNULL
            try:
                handle = open(path, "wb")  # noqa: SIM115 - closed by caller
            except OSError:  # pragma: no cover - defensive
                logger.warning("Could not open daemon log file %s", path)
                return subprocess.DEVNULL
            handles.append(handle)
            return handle

        return _open(stdout_path), _open(stderr_path), handles

    # -- introspection -----------------------------------------------------

    def get(self, pid: int) -> Optional[SpawnedProcess]:
        """Return the tracked process for *pid*, or ``None``."""
        return self._processes.get(pid)

    @property
    def processes(self) -> List[SpawnedProcess]:
        """A snapshot list of all tracked spawned processes."""
        return list(self._processes.values())

    def poll(self, pid: int) -> Optional[int]:
        """Non-blocking exit-code probe for *pid* (``None`` while running)."""
        spawned = self._processes.get(pid)
        if spawned is None:
            return None
        return spawned.process.poll()

    def wait(self, pid: int, timeout: Optional[float] = None) -> Optional[int]:
        """Wait up to *timeout* seconds for *pid* to exit; return its code.

        Raises :class:`subprocess.TimeoutExpired` if the process is still
        running when the timeout elapses.
        """
        spawned = self._processes.get(pid)
        if spawned is None:
            return None
        return spawned.process.wait(timeout=timeout)

    def iter_events(self, pid: int) -> Iterator[Dict[str, object]]:
        """Yield parsed NDJSON event dicts by tailing a spawned child's stdout log.

        Each non-blank line of the ``--output-format json`` child's stdout log
        is a single JSON object; malformed lines are skipped. The tail keeps
        polling for new lines while the child runs and ends once the child has
        exited and the log has been fully drained.
        """
        spawned = self._processes.get(pid)
        if spawned is None or spawned.stdout_log is None:
            return
        try:
            handle = open(spawned.stdout_log, "r", encoding="utf-8")
        except OSError:
            return
        try:
            while True:
                line = handle.readline()
                if line == "":
                    # EOF: only stop once the child has exited (so no further
                    # writes are possible); otherwise wait for more output.
                    if spawned.process.poll() is not None:
                        # One final read to catch lines flushed between the
                        # poll and this read.
                        line = handle.readline()
                        if line == "":
                            break
                    else:
                        time.sleep(0.05)
                        continue
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    logger.debug("Skipping non-JSON child stdout line")
                    continue
        finally:
            handle.close()

    # -- termination -------------------------------------------------------

    def terminate(self, pid: int, *, grace: float = 10.0) -> Optional[int]:
        """Gracefully stop *pid*: ``SIGTERM``, wait *grace* sec, then ``SIGKILL``.

        Returns the child's exit code (``None`` if *pid* is not tracked).
        """
        spawned = self._processes.get(pid)
        if spawned is None:
            return None
        proc = spawned.process
        if proc.poll() is not None:
            return proc.returncode
        try:
            proc.terminate()
        except ProcessLookupError:
            return proc.returncode
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            logger.warning("Flow pid=%s did not exit on SIGTERM; sending SIGKILL", pid)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:  # pragma: no cover - extreme case
                logger.error("Flow pid=%s unresponsive to SIGKILL", pid)
        return proc.returncode

    def terminate_all(self, *, grace: float = 10.0) -> None:
        """Gracefully terminate every still-running spawned process."""
        for spawned in list(self._processes.values()):
            if spawned.is_running:
                self.terminate(spawned.pid, grace=grace)

    def orphans(self) -> List[SpawnedProcess]:
        """Return spawned processes still running (would be orphaned on exit)."""
        return [s for s in self._processes.values() if s.is_running]

    def reap(self) -> List[SpawnedProcess]:
        """Drop tracking for finished children; return the reaped processes.

        Invokes the ``on_exit`` callback and de-registers from the supervisor
        for each reaped process.
        """
        finished = [s for s in self._processes.values() if not s.is_running]
        for spawned in finished:
            self._processes.pop(spawned.pid, None)
            if self._supervisor is not None:
                try:
                    self._supervisor.unregister(spawned.pid)
                except Exception:  # pragma: no cover - defensive
                    logger.exception("Failed to unregister flow from supervisor")
            if self._on_exit is not None:
                try:
                    self._on_exit(spawned)
                except Exception:  # pragma: no cover - defensive
                    logger.exception("on_exit callback failed")
        return finished


# Re-export for callers that probe signal availability.
_HAS_SIGTERM = hasattr(signal, "SIGTERM")
