"""Spawns new ``se3 run`` flows on behalf of the SE3 daemon.

:class:`DaemonSpawner` starts ``se3 run --output-format json <task>`` child
processes via :class:`subprocess.Popen`. The daemon is a *parent* of the flow,
never an in-process caller: this keeps ``se3 run`` independently testable and
ensures a daemon crash does not take a running flow down with it.

The spawner deliberately passes ``--output-format json`` so the child emits the
unified structured event stream as NDJSON on stdout — :meth:`iter_events` then
yields those events back as parsed dicts.
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
    """

    process: subprocess.Popen
    project_root: str
    task_description: str
    args: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

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

    # -- spawning ----------------------------------------------------------

    def spawn(
        self,
        task_description: str,
        *,
        project_root: Optional[str] = None,
        task_type: str = "feature",
        extra_args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> SpawnedProcess:
        """Start a new ``se3 run`` flow and return its :class:`SpawnedProcess`.

        The child runs ``se3 run <task> --type <task_type> --output-format json``
        with the daemon's environment inherited (so the Python path stays
        correct) and ``cwd`` set to *project_root*.
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
        if extra_args:
            args.extend(extra_args)

        child_env = os.environ.copy()
        if env:
            child_env.update(env)

        logger.info("Spawning se3 run flow in %s", cwd)
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        spawned = SpawnedProcess(
            process=process,
            project_root=cwd,
            task_description=task_description,
            args=args,
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
        """Yield parsed NDJSON event dicts from a spawned child's stdout.

        Each non-blank stdout line of the ``--output-format json`` child is a
        single JSON object; malformed lines are skipped. Iteration ends when
        the child closes stdout.
        """
        spawned = self._processes.get(pid)
        if spawned is None or spawned.process.stdout is None:
            return
        for line in spawned.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                logger.debug("Skipping non-JSON child stdout line")
                continue

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
