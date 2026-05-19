"""The resident SE3 daemon process.

:class:`Daemon` composes the three control-plane pieces — :class:`DaemonSupervisor`
(process discovery), :class:`DaemonSpawner` (flow startup) and
:class:`DaemonAggregator` (on-disk state aggregation) — into a single resident
process driven by an ``asyncio`` event loop.

The daemon is intentionally long-lived: it outlives any individual ``se3 run``
and is the only component that keeps aggregating state and offering a stable
endpoint after the CLI has exited. A future WebSocket client to the central
server (see ``DaemonConfig.server_url``) shares this same event loop.

Lifecycle:

* :func:`start_daemon` — launch a background daemon (writes a pidfile to guard
  against duplicate starts).
* :func:`stop_daemon` — signal a running daemon to shut down gracefully.
* :func:`daemon_status` — report whether the daemon is up and what flows it
  currently tracks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .aggregator import DaemonAggregator, MachineStatus
from .spawner import DaemonSpawner, SpawnedProcess
from .supervisor import DaemonSupervisor

logger = logging.getLogger(__name__)

PID_FILENAME = "daemon.pid"
STATUS_FILENAME = "daemon_status.json"
LOG_FILENAME = "daemon.log"

#: Log format for the daemon process; the leading ``%(asctime)s`` is what makes
#: every line in ``~/.se3/daemon.log`` (and the foreground terminal) attributable
#: to a particular run.
DAEMON_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
#: Attribute stamped on the handler installed by :func:`_configure_daemon_logging`
#: so a re-entrant call can detect and skip a second install.
_DAEMON_HANDLER_TAG = "_se3_daemon_log_handler"


def _configure_daemon_logging() -> None:
    """Install a timestamped log handler for the daemon process.

    Called from the daemon process entry point (:meth:`Daemon.run_forever`),
    never from the core ``se3`` CLI, so it does not pollute the CLI's logging.
    In detached mode stderr is redirected to ``daemon.log``; in foreground mode
    it goes to the terminal — both inherit the ``%(asctime)s`` timestamp.

    Idempotent: the installed handler is tagged, so repeated calls within one
    process never stack handlers or duplicate log lines.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, _DAEMON_HANDLER_TAG, False):
            return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(DAEMON_LOG_FORMAT))
    setattr(handler, _DAEMON_HANDLER_TAG, True)
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


def _default_pid_dir() -> Path:
    """Return the default directory for daemon runtime files.

    Defaults to ``~/.se3``; overridable via the ``SE3_DAEMON_DIR`` environment
    variable so that isolated runs (and tests) do not collide on a shared
    pidfile.
    """
    override = os.environ.get("SE3_DAEMON_DIR")
    if override:
        return Path(override)
    return Path.home() / ".se3"


@dataclass
class DaemonConfig:
    """Configuration for a :class:`Daemon` instance.

    Attributes:
        server_url: Central-server URL the daemon dials out to. Stored now;
            the outbound WebSocket client is wired in a later group.
        pid_dir: Directory for the pidfile, status file and log.
        poll_interval: Seconds between aggregation polls.
        machine_id: Stable id for this machine; auto-derived when ``None``.
        project_roots: Initial project roots to aggregate, beyond those
            discovered from running flows.
        shutdown_grace: Seconds to wait for spawned flows to exit on shutdown.
    """

    server_url: Optional[str] = None
    pid_dir: Path = field(default_factory=_default_pid_dir)
    poll_interval: float = 2.0
    machine_id: Optional[str] = None
    project_roots: List[str] = field(default_factory=list)
    shutdown_grace: float = 10.0

    def __post_init__(self) -> None:
        self.pid_dir = Path(self.pid_dir)

    @property
    def pid_file(self) -> Path:
        return self.pid_dir / PID_FILENAME

    @property
    def status_file(self) -> Path:
        return self.pid_dir / STATUS_FILENAME

    @property
    def log_file(self) -> Path:
        return self.pid_dir / LOG_FILENAME


class Daemon:
    """The resident control-plane process."""

    def __init__(self, config: Optional[DaemonConfig] = None) -> None:
        self.config = config or DaemonConfig()
        self.supervisor = DaemonSupervisor()
        self.spawner = DaemonSpawner(supervisor=self.supervisor)
        self.aggregator = DaemonAggregator(
            machine_id=self.config.machine_id,
            poll_interval=self.config.poll_interval,
        )
        for root in self.config.project_roots:
            self.aggregator.add_project_root(root)
        self._stop_event: Optional[asyncio.Event] = None
        self._running = False
        # The outbound WebSocket client to the central server. Created lazily
        # in serve() only when a server_url is configured.
        self._client: Optional["object"] = None

    # -- public control surface -------------------------------------------

    def request_spawn(
        self,
        task_description: str,
        *,
        project_root: Optional[str] = None,
        task_type: str = "feature",
    ) -> SpawnedProcess:
        """Spawn a new ``se3 run`` flow (entry point for remote requests).

        The spawned flow's project root is registered with the aggregator so
        the next poll picks up its state.
        """
        spawned = self.spawner.spawn(
            task_description,
            project_root=project_root,
            task_type=task_type,
        )
        self.aggregator.add_project_root(spawned.project_root)
        return spawned

    def snapshot(self) -> MachineStatus:
        """Return a fresh :class:`MachineStatus` aggregation snapshot."""
        return self.aggregator.get_snapshot()

    def stop(self) -> None:
        """Signal the running event loop to shut down gracefully."""
        if self._stop_event is not None and not self._stop_event.is_set():
            self._stop_event.set()

    # -- run loop ----------------------------------------------------------

    def run_forever(self) -> None:
        """Run the daemon in the foreground until stopped (blocking)."""
        _configure_daemon_logging()
        try:
            asyncio.run(self.serve())
        except KeyboardInterrupt:  # pragma: no cover - interactive
            logger.info("Daemon interrupted")

    async def serve(self) -> None:
        """The async main: write pidfile, poll, dial the server, then clean up.

        The aggregation poll loop and the optional outbound server client both
        run as concurrent tasks on this single event loop and share the same
        ``stop_event``.
        """
        self.config.pid_dir.mkdir(parents=True, exist_ok=True)
        self._write_pidfile()
        self._stop_event = asyncio.Event()
        self._running = True
        self._install_signal_handlers()
        logger.info("SE3 daemon started (pid=%s)", os.getpid())

        tasks = [asyncio.create_task(self._poll_loop())]
        client_task = self._start_server_client()
        if client_task is not None:
            tasks.append(client_task)
        try:
            await asyncio.gather(*tasks)
        finally:
            self._running = False
            self._shutdown()

    def _start_server_client(self) -> Optional["asyncio.Task"]:
        """Create the outbound :class:`DaemonClient` task when a server is set.

        Returns ``None`` when no ``server_url`` is configured, so a daemon run
        purely for local supervision pays no WebSocket cost.
        """
        if not self.config.server_url:
            return None
        # Deferred import: the client module touches the optional 'websockets'
        # dependency, so it must not load on the local-only path.
        from .client import DaemonClient

        client = DaemonClient(
            self.config.server_url,
            machine_id=self.aggregator.machine_id,
            hostname=self.aggregator.hostname,
            se3_version=_se3_version(),
            snapshot_provider=lambda: self.aggregator.get_snapshot().to_dict(),
            spawn_handler=self._handle_spawn_request,
        )
        self._client = client
        assert self._stop_event is not None
        return asyncio.create_task(client.run(self._stop_event))

    def _handle_spawn_request(
        self, task_description: str, project_root: str, task_type: str
    ) -> SpawnedProcess:
        """Adapt a server SPAWN_FLOW into a :meth:`request_spawn` call."""
        return self.request_spawn(
            task_description,
            project_root=project_root or None,
            task_type=task_type or "feature",
        )

    async def _poll_loop(self) -> None:
        """Aggregate local state on a fixed interval until the stop event fires."""
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception:  # pragma: no cover - defensive
                logger.exception("Daemon poll iteration failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.config.poll_interval
                )
            except asyncio.TimeoutError:
                continue

    def _poll_once(self) -> None:
        """A single aggregation tick: discover flows, snapshot, persist status."""
        flows = self.supervisor.discover_flows()
        for record in flows:
            self.aggregator.add_project_root(record.project_root)
        self.spawner.reap()
        snapshot = self.aggregator.get_snapshot()
        self._write_status(snapshot, flows)

    # -- signal handling ---------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """Wire ``SIGTERM`` / ``SIGINT`` to a graceful stop on the event loop."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop)
            except (NotImplementedError, ValueError):  # pragma: no cover - win32
                signal.signal(sig, lambda *_: self.stop())

    # -- shutdown ----------------------------------------------------------

    def _shutdown(self) -> None:
        """Terminate spawned flows and remove runtime files."""
        logger.info("SE3 daemon shutting down")
        orphans = self.spawner.orphans()
        if orphans:
            logger.info("Terminating %d spawned flow(s)", len(orphans))
            self.spawner.terminate_all(grace=self.config.shutdown_grace)
        for path in (self.config.status_file, self.config.pid_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - defensive
                pass

    # -- runtime files -----------------------------------------------------

    def _write_pidfile(self) -> None:
        """Write the pidfile, refusing to start if another daemon is alive."""
        existing = _read_pidfile(self.config.pid_file)
        if existing and _pid_alive(existing.get("pid", 0)):
            raise DaemonAlreadyRunning(existing.get("pid", 0))
        payload = {
            "pid": os.getpid(),
            "started_at": time.time(),
            "server_url": self.config.server_url,
            "machine_id": self.aggregator.machine_id,
        }
        _atomic_write_json(self.config.pid_file, payload)

    def _write_status(
        self, snapshot: MachineStatus, flows: List["object"]
    ) -> None:
        """Persist the latest snapshot so ``se3 daemon status`` can read it.

        The outbound :class:`~se3.daemon.client.DaemonClient`'s live
        ``connected`` / ``last_error`` are written here so ``se3 daemon
        status`` reflects the *real* connection state rather than merely
        echoing the configured URL. When no client exists (no ``server_url``
        configured) the connection fields mark an unconfigured outbound link
        instead of misreporting a connection.
        """
        client = self._client
        if client is not None:
            server_configured = True
            connected = bool(getattr(client, "connected", False))
            last_error = getattr(client, "last_error", None)
        else:
            # No server_url configured: the daemon runs local-only and never
            # opens an outbound connection.
            server_configured = False
            connected = False
            last_error = None
        payload = {
            "pid": os.getpid(),
            "updated_at": time.time(),
            "server_url": self.config.server_url,
            "machine_id": snapshot.machine_id,
            "hostname": snapshot.hostname,
            "server_configured": server_configured,
            "connected": connected,
            "last_error": last_error,
            "tracked_flows": [
                rec.to_dict() for rec in flows if hasattr(rec, "to_dict")
            ],
            "snapshot": snapshot.to_dict(),
        }
        try:
            _atomic_write_json(self.config.status_file, payload)
        except OSError:  # pragma: no cover - defensive
            logger.debug("Failed to write daemon status file", exc_info=True)


class DaemonAlreadyRunning(RuntimeError):
    """Raised when a daemon is started while another instance is alive."""

    def __init__(self, pid: int) -> None:
        super().__init__(f"SE3 daemon already running (pid={pid})")
        self.pid = pid


# -- module-level lifecycle helpers ---------------------------------------


def _se3_version() -> str:
    """Return the installed SE3 version string (best-effort)."""
    try:
        import se3

        return str(getattr(se3, "__version__", "unknown"))
    except Exception:  # pragma: no cover - defensive
        return "unknown"


def _read_pidfile(pid_file: Path) -> Optional[Dict[str, object]]:
    """Read the daemon pidfile payload; return ``None`` if absent/corrupt."""
    try:
        return json.loads(pid_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pid_alive(pid: object) -> bool:
    """Return whether *pid* (an int-like) names a live process."""
    try:
        pid_int = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _atomic_write_json(path: Path, payload: Dict[str, object]) -> None:
    """Atomically write *payload* as JSON to *path* (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)


def start_daemon(
    config: Optional[DaemonConfig] = None, *, foreground: bool = False
) -> Dict[str, object]:
    """Start the SE3 daemon.

    When *foreground* is true the daemon runs in-process and this call blocks
    until it stops. Otherwise the daemon is fully detached via the classic
    double-fork so its parent becomes ``init`` — it therefore cannot be left
    as a zombie of a long-lived launcher — and this call returns immediately
    with a status dict.

    Raises :class:`DaemonAlreadyRunning` if a live daemon is already present.
    """
    config = config or DaemonConfig()
    existing = _read_pidfile(config.pid_file)
    if existing and _pid_alive(existing.get("pid", 0)):
        raise DaemonAlreadyRunning(int(existing.get("pid", 0)))  # type: ignore[arg-type]

    if foreground:
        Daemon(config).run_forever()
        return {"status": "stopped"}

    config.pid_dir.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "fork"):
        return _start_detached_fork(config)
    return _start_detached_subprocess(config)  # pragma: no cover - win32


def _start_detached_fork(config: DaemonConfig) -> Dict[str, object]:
    """Detach the daemon via a double-fork; runs the daemon in the grandchild.

    The intermediate child is reaped immediately by this (launcher) process,
    and the grandchild — the actual daemon — is reparented to ``init`` once the
    intermediate exits. The launcher then polls the pidfile and returns.
    """
    intermediate = os.fork()
    if intermediate > 0:
        # Launcher: reap the intermediate child so it cannot zombie, then
        # wait for the grandchild daemon to claim the pidfile.
        try:
            os.waitpid(intermediate, 0)
        except ChildProcessError:  # pragma: no cover - already reaped
            pass
        deadline = time.time() + 5.0
        while time.time() < deadline:
            payload = _read_pidfile(config.pid_file)
            if payload and _pid_alive(payload.get("pid", 0)):
                return {"status": "started", "pid": payload.get("pid")}
            time.sleep(0.1)
        return {"status": "starting"}

    # Intermediate child: become a session leader, fork again, then exit so
    # the grandchild is orphaned onto init.
    try:
        os.setsid()
        grandchild = os.fork()
        if grandchild > 0:
            os._exit(0)
        # Grandchild: the real daemon. Detach std streams to the log file.
        _detach_streams(config)
        try:
            Daemon(config).run_forever()
        finally:
            os._exit(0)
    except Exception:  # pragma: no cover - defensive
        os._exit(1)


def _detach_streams(config: DaemonConfig) -> None:
    """Redirect stdin/stdout/stderr of the detached daemon to log/devnull."""
    try:
        log_fd = os.open(
            str(config.log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644
        )
        null_fd = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null_fd, 0)
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        if null_fd > 2:
            os.close(null_fd)
        if log_fd > 2:
            os.close(log_fd)
    except OSError:  # pragma: no cover - defensive
        pass


def _start_detached_subprocess(config: DaemonConfig) -> Dict[str, object]:  # pragma: no cover - win32
    """Fallback detach path for platforms without ``os.fork`` (e.g. Windows)."""
    args = [sys.executable, "-m", "se3", "daemon", "start", "--foreground"]
    if config.server_url:
        args += ["--server-url", config.server_url]
    child_env = os.environ.copy()
    try:
        import se3 as _se3

        se3_parent = str(Path(_se3.__file__).resolve().parent.parent)
        existing = child_env.get("PYTHONPATH", "")
        child_env["PYTHONPATH"] = os.pathsep.join(
            p for p in (se3_parent, existing) if p
        )
    except Exception:
        pass
    log_handle = open(config.log_file, "a", encoding="utf-8")  # noqa: SIM115
    proc = subprocess.Popen(
        args,
        env=child_env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 5.0
    while time.time() < deadline:
        payload = _read_pidfile(config.pid_file)
        if payload and _pid_alive(payload.get("pid", 0)):
            return {"status": "started", "pid": payload.get("pid")}
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    return {"status": "starting", "pid": proc.pid}


def stop_daemon(
    config: Optional[DaemonConfig] = None, *, timeout: float = 15.0
) -> Dict[str, object]:
    """Signal a running daemon to shut down and wait for it to exit."""
    config = config or DaemonConfig()
    payload = _read_pidfile(config.pid_file)
    if not payload:
        return {"status": "not_running"}
    pid = payload.get("pid", 0)
    if not _pid_alive(pid):
        config.pid_file.unlink(missing_ok=True)
        return {"status": "not_running"}
    try:
        os.kill(int(pid), signal.SIGTERM)  # type: ignore[arg-type]
    except (ProcessLookupError, ValueError, TypeError):
        config.pid_file.unlink(missing_ok=True)
        return {"status": "not_running"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return {"status": "stopped", "pid": pid}
        time.sleep(0.2)
    return {"status": "stop_timeout", "pid": pid}


def daemon_status(config: Optional[DaemonConfig] = None) -> Dict[str, object]:
    """Report the daemon's running state and most recently tracked flows."""
    config = config or DaemonConfig()
    payload = _read_pidfile(config.pid_file)
    if not payload or not _pid_alive(payload.get("pid", 0)):
        return {"running": False}
    result: Dict[str, object] = {
        "running": True,
        "pid": payload.get("pid"),
        "started_at": payload.get("started_at"),
        "server_url": payload.get("server_url"),
        "machine_id": payload.get("machine_id"),
    }
    status_payload = _read_pidfile(config.status_file)
    if status_payload:
        result["updated_at"] = status_payload.get("updated_at")
        result["tracked_flows"] = status_payload.get("tracked_flows", [])
        result["snapshot"] = status_payload.get("snapshot", {})
        # Real outbound-connection state, written by Daemon._write_status.
        result["server_configured"] = status_payload.get(
            "server_configured", bool(payload.get("server_url"))
        )
        result["connected"] = status_payload.get("connected", False)
        result["last_error"] = status_payload.get("last_error")
    else:
        # No status file yet: fall back to safe defaults.
        result["tracked_flows"] = []
        result["server_configured"] = bool(payload.get("server_url"))
        result["connected"] = False
        result["last_error"] = None
    return result
