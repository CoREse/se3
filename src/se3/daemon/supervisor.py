"""Process discovery and supervision for the SE3 daemon.

:class:`DaemonSupervisor` tracks the local machine's running ``se3 run``
processes. It maintains a ``PID -> FlowRecord`` mapping, detects process exit
by polling (so it works uniformly across platforms without relying on
``SIGCHLD``), and prunes records for dead processes.

Two sources feed the mapping:

* **Spawned** flows — registered by :class:`~se3.daemon.spawner.DaemonSpawner`
  when the daemon starts a ``se3 run`` child on behalf of a remote request.
* **Discovered** flows — externally-started ``se3 run`` processes found by
  scanning process command lines (best-effort, via ``psutil`` when available).

The supervisor is thread-safe: all mutation of the internal map is guarded by
a re-entrant lock, so several flows starting/exiting concurrently never race.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Best-effort optional dependency. The supervisor works without it (state-file
# scanning + liveness probes); psutil only enriches *external* process
# discovery. It is deliberately NOT a core dependency.
try:  # pragma: no cover - import guard
    import psutil  # type: ignore
except Exception:  # pragma: no cover - psutil absent
    psutil = None  # type: ignore


ExitCallback = Callable[["FlowRecord"], None]


@dataclass
class FlowRecord:
    """A single supervised ``se3 run`` process.

    Attributes:
        pid: Operating-system process id.
        project_root: Absolute path of the project the flow runs in.
        flow_id: The flow's id, resolved from ``engine.json`` when known.
        task_description: The task the flow is executing, when known.
        origin: ``"spawned"`` (started by the daemon) or ``"discovered"``
            (found running independently).
        started_at: Unix epoch seconds when the record was created.
    """

    pid: int
    project_root: str
    flow_id: Optional[str] = None
    task_description: Optional[str] = None
    origin: str = "spawned"
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, object]:
        """Return a JSON-serializable representation of this record."""
        return {
            "pid": self.pid,
            "project_root": self.project_root,
            "flow_id": self.flow_id,
            "task_description": self.task_description,
            "origin": self.origin,
            "started_at": self.started_at,
        }


def _is_alive(pid: int) -> bool:
    """Return whether *pid* names a live process.

    Uses ``os.kill(pid, 0)``, which raises ``ProcessLookupError`` for a dead
    pid and ``PermissionError`` for a live one owned by another user (still
    "alive" for our purposes).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def resolve_worktree_main_root(path: object) -> Optional[str]:
    """Return the main project root for an se3 *worktree isolation* directory.

    A ``se3 run --worktree`` flow body executes inside
    ``<main_root>/se3/worktrees/<name>/``. That directory is a transient
    execution sandbox created and managed by the worktree-management flows — it
    is gitignored and never a standalone project — so a process discovered
    running there MUST be attributed back to ``<main_root>`` rather than
    registered as its own project root (otherwise the worktree copy pollutes the
    WebUI project list / registry).

    The attribution strips exactly **one** ``/se3/worktrees/<name>`` suffix
    level, so a *nested* worktree (``…/wt1/se3/worktrees/wt2``) resolves to its
    immediate parent project (``…/wt1``) rather than over-collapsing to the
    outermost root — this is what keeps a main project that itself lives under a
    parent worktree from being mis-attributed.

    Returns ``None`` when *path* is not such a worktree directory (an ordinary
    run), so callers fall back to registering *path* unchanged.
    """
    if not path:
        return None
    try:
        resolved = Path(os.path.realpath(str(path)))
    except OSError:  # pragma: no cover - defensive
        return None
    parent = resolved.parent
    # The worktree directory is a direct child of ``<main_root>/se3/worktrees``.
    if parent.name != "worktrees" or parent.parent.name != "se3":
        return None
    main_root = parent.parent.parent
    # Only attribute back when the resolved main root is itself a plausible se3
    # project (carries an ``se3`` directory). Guards against an unrelated tree
    # that merely happens to nest a ``se3/worktrees`` path component.
    try:
        if not (main_root / "se3").is_dir():
            return None
    except OSError:  # pragma: no cover - defensive
        return None
    return str(main_root)


def is_worktree_copy_root(path: object) -> bool:
    """Return whether *path* is an se3 worktree isolation copy directory.

    True exactly when :func:`resolve_worktree_main_root` can attribute *path*
    back to a main project root — i.e. *path* is a ``<main>/se3/worktrees/<name>``
    isolation sandbox rather than a standalone project root.
    """
    return resolve_worktree_main_root(path) is not None


def _read_flow_id(project_root: str) -> Optional[str]:
    """Best-effort read of ``flow_id`` from a project's ``engine.json``."""
    engine_json = Path(project_root) / "se3" / "state" / "engine.json"
    try:
        import json

        data = json.loads(engine_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    fid = data.get("flow_id")
    return str(fid) if fid else None


class DaemonSupervisor:
    """Discovers and tracks local ``se3 run`` processes."""

    def __init__(self) -> None:
        self._flows: Dict[int, FlowRecord] = {}
        self._lock = threading.RLock()
        self._exit_callbacks: List[ExitCallback] = []

    # -- registration ------------------------------------------------------

    def register(
        self,
        pid: int,
        project_root: str,
        *,
        flow_id: Optional[str] = None,
        task_description: Optional[str] = None,
        origin: str = "spawned",
    ) -> FlowRecord:
        """Track *pid* as a supervised flow and return its :class:`FlowRecord`.

        Registering an already-known pid updates the existing record's
        metadata in place rather than creating a duplicate.
        """
        with self._lock:
            existing = self._flows.get(pid)
            if existing is not None:
                if flow_id:
                    existing.flow_id = flow_id
                if task_description:
                    existing.task_description = task_description
                return existing
            record = FlowRecord(
                pid=pid,
                project_root=str(Path(project_root).resolve()),
                flow_id=flow_id or _read_flow_id(project_root),
                task_description=task_description,
                origin=origin,
            )
            self._flows[pid] = record
            logger.debug("Supervising flow pid=%s (%s)", pid, origin)
            return record

    def unregister(self, pid: int) -> Optional[FlowRecord]:
        """Stop tracking *pid*; return its record if it was tracked."""
        with self._lock:
            return self._flows.pop(pid, None)

    def on_exit(self, callback: ExitCallback) -> None:
        """Register *callback*, invoked once per process found dead by :meth:`reap`."""
        with self._lock:
            self._exit_callbacks.append(callback)

    # -- liveness ----------------------------------------------------------

    @staticmethod
    def is_alive(pid: int) -> bool:
        """Return whether *pid* names a live process."""
        return _is_alive(pid)

    def reap(self) -> List[FlowRecord]:
        """Prune records for processes that have exited.

        Returns the list of reaped records. Registered exit callbacks are
        invoked for each reaped record (callback exceptions are swallowed so
        one bad listener cannot break supervision).
        """
        with self._lock:
            dead = [rec for pid, rec in self._flows.items() if not _is_alive(pid)]
            for rec in dead:
                self._flows.pop(rec.pid, None)
            callbacks = list(self._exit_callbacks)
        for rec in dead:
            logger.debug("Flow pid=%s exited; reaped", rec.pid)
            for cb in callbacks:
                try:
                    cb(rec)
                except Exception:  # pragma: no cover - defensive isolation
                    logger.exception("Supervisor exit callback failed")
        return dead

    # -- discovery ---------------------------------------------------------

    def discover_flows(self, *, scan_external: bool = True) -> List[FlowRecord]:
        """Return all currently-running supervised flows.

        Dead processes are reaped first. When *scan_external* is true and
        ``psutil`` is installed, externally-started ``se3 run`` processes are
        discovered and added to the mapping.
        """
        self.reap()
        if scan_external:
            self._scan_external()
        with self._lock:
            return sorted(self._flows.values(), key=lambda r: r.started_at)

    def _scan_external(self) -> None:
        """Discover ``se3 run`` processes not started by this daemon.

        Best-effort: requires ``psutil``. A no-op when psutil is unavailable.
        """
        if psutil is None:
            return
        try:
            current = psutil.Process().pid
            for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
                try:
                    info = proc.info
                    pid = info.get("pid")
                    cmdline = info.get("cmdline") or []
                    if pid is None or pid == current:
                        continue
                    if not _cmdline_is_se3_run(cmdline):
                        continue
                    with self._lock:
                        if pid in self._flows:
                            continue
                    cwd = info.get("cwd") or os.getcwd()
                    # A ``se3 run --worktree`` flow body found running with its
                    # cwd inside ``<main>/se3/worktrees/<name>`` is a transient
                    # isolation sandbox, not a standalone project. Attribute it
                    # back to its main project root so the worktree copy never
                    # gets registered (and surfaced in the WebUI) as its own
                    # project.
                    main_root = resolve_worktree_main_root(cwd)
                    self.register(pid, main_root or cwd, origin="discovered")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:  # pragma: no cover - defensive
            logger.debug("External process scan failed", exc_info=True)

    # -- accessors ---------------------------------------------------------

    def get(self, pid: int) -> Optional[FlowRecord]:
        """Return the record for *pid*, or ``None`` if not tracked."""
        with self._lock:
            return self._flows.get(pid)

    @property
    def flows(self) -> List[FlowRecord]:
        """A snapshot list of all tracked records (alive or not yet reaped)."""
        with self._lock:
            return list(self._flows.values())

    @property
    def count(self) -> int:
        """Number of currently-tracked records."""
        with self._lock:
            return len(self._flows)


def _cmdline_is_se3_run(cmdline: List[str]) -> bool:
    """Heuristically decide whether *cmdline* is an ``se3 run`` invocation."""
    if not cmdline:
        return False
    joined = " ".join(cmdline)
    if "run" not in cmdline:
        return False
    # Match both the console-script form (``se3 run ...``) and the module
    # form (``python -m se3 run ...``).
    return "se3" in joined and (
        any(part == "se3" or part.endswith("/se3") for part in cmdline)
        or "se3" in cmdline
    )
