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
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from .aggregator import DaemonAggregator, MachineStatus

if TYPE_CHECKING:  # pragma: no cover - typing only; runtime import is lazy in _gc_once
    from ..engine.merge.worktree_gc import WorktreeGCReport
from .disk_json_cache import read_engine_header
from .history import DaemonHistoryReader
from .spawner import DaemonSpawner, SpawnedProcess
from .supervisor import (
    DaemonSupervisor,
    is_worktree_copy_root,
    resolve_worktree_main_root,
)

logger = logging.getLogger(__name__)

PID_FILENAME = "daemon.pid"
STATUS_FILENAME = "daemon_status.json"
LOG_FILENAME = "daemon.log"
#: Machine-local registry of every project root that has ever run an ``se3``
#: flow through this daemon. Lives next to the pidfile / status file under
#: ``pid_dir`` so it inherits the same ``SE3_DAEMON_DIR`` / ``DaemonConfig``
#: overrides (and test isolation). Persisting roots here lets the history index
#: and the New Task project dropdown stay populated even when no ``se3 run``
#: process is currently live — and across daemon restarts.
PROJECT_ROOTS_FILENAME = "project_roots.json"

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
        daemon_key: Secret daemon credential carried in HELLO so the
            multi-tenant server can resolve this daemon to an owner. ``None``
            / empty means "no credential" (local / legacy single-tenant
            operation). Held only in memory and passed to the outbound client;
            it is never written to the status file or any log.
        pid_dir: Directory for the pidfile, status file and log.
        poll_interval: Seconds between aggregation polls.
        machine_id: Stable id for this machine; auto-derived when ``None``.
        project_roots: Initial project roots to aggregate, beyond those
            discovered from running flows.
        shutdown_grace: Seconds to wait for spawned flows to exit on shutdown.
        history_poll_interval: Fast cadence (seconds) at which the outbound
            client samples active flows' on-disk signature to drive incremental
            history pushes off real ``engine.json`` / jsonl changes instead of
            only the 5 s status tick.
        gc_interval: Low-frequency cadence (seconds) at which the daemon sweeps
            leaked ``se3 run --worktree`` runs (see
            :func:`se3.engine.merge.worktree_gc.gc_worktree_runs`). Deliberately
            decoupled from ``poll_interval`` (2 s): GC is heavy IO and reclaiming
            stranded worktrees needs no high frequency. ``<= 0`` disables it.
        gc_max_age_seconds: Idle threshold a terminal worktree run must exceed
            before GC reclaims it, forwarded to ``gc_worktree_runs`` so a
            just-completed run awaiting a human merge is left alone.
    """

    server_url: Optional[str] = None
    daemon_key: Optional[str] = None
    pid_dir: Path = field(default_factory=_default_pid_dir)
    poll_interval: float = 2.0
    machine_id: Optional[str] = None
    project_roots: List[str] = field(default_factory=list)
    shutdown_grace: float = 10.0
    history_poll_interval: float = 1.0
    gc_interval: float = 3600.0
    gc_max_age_seconds: float = 86400.0

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

    @property
    def project_roots_file(self) -> Path:
        return self.pid_dir / PROJECT_ROOTS_FILENAME


class Daemon:
    """The resident control-plane process."""

    def __init__(self, config: Optional[DaemonConfig] = None) -> None:
        self.config = config or DaemonConfig()
        self.supervisor = DaemonSupervisor()
        self.spawner = DaemonSpawner(supervisor=self.supervisor)
        # Machine-local persistent registry of every project root that has run
        # a flow through this daemon. Wiring the load/persist callbacks makes
        # aggregator.add_project_root write through to ``project_roots_file`` so
        # the history index and New Task dropdown stay populated with no live
        # ``se3 run`` process and across daemon restarts.
        registry_file = self.config.project_roots_file
        # One-time cleanup of any worktree-copy entry that a pre-normalization
        # daemon may have persisted into the registry, so historical pollution
        # is eliminated across restarts (not merely prevented going forward).
        _sanitize_project_roots(registry_file)
        self.aggregator = DaemonAggregator(
            machine_id=self.config.machine_id,
            poll_interval=self.config.poll_interval,
            registry_load=lambda: _read_project_roots(registry_file),
            registry_persist=lambda root: _append_project_root(registry_file, root),
            # Live-process roots for the RUNNING-flow resumable gate. Computed
            # from the same ``supervisor.flows`` + ``is_alive`` view that
            # ``request_resume`` uses for its double-spawn refusal, so the Resume
            # button's visibility and "can this actually resume right now"
            # decision stay in lockstep.
            live_roots_provider=self._live_project_roots,
        )
        for root in self.config.project_roots:
            self.aggregator.add_project_root(root)
        # Reads se3/history of every root the aggregator tracks; injected into
        # the outbound DaemonClient as its history_provider. Uses the
        # worktree-inclusive root view (active ∪ registry ∪ disk-history ∪
        # active ``--worktree`` run subdirs) so build_index / active_flow_signature
        # see the registry roots even with no live flow AND surface a
        # ``se3 run --worktree`` flow's engine.json / history live during its
        # flow body, not only after the trailing merge syncs history back.
        self.history_reader = DaemonHistoryReader(
            project_roots_provider=lambda: self.aggregator.all_observable_roots()
        )
        self._stop_event: Optional[asyncio.Event] = None
        self._running = False
        # The outbound WebSocket client to the central server. Created lazily
        # in serve() only when a server_url is configured.
        self._client: Optional["object"] = None
        # Wall-clock timestamp of the last worktree-GC sweep, gating the sweep
        # to ``gc_interval`` cadence even though it is polled from the 2 s
        # ``_poll_loop``. ``None`` means "never run", so the first eligible poll
        # runs one sweep at startup (leaks are the problem GC exists to fix).
        self._last_gc_at: Optional[float] = None

    # -- public control surface -------------------------------------------

    def request_spawn(
        self,
        task_description: str,
        *,
        project_root: Optional[str] = None,
        task_type: str = "feature",
        discover: bool = False,
        worktree: bool = False,
        from_issue_id: str = "",
    ) -> SpawnedProcess:
        """Spawn a new ``se3 run`` flow (entry point for remote requests).

        The spawned flow's project root is registered with the aggregator so
        the next poll picks up its state. When *discover* is true the flow
        starts from the discovery step. When *worktree* is true the flow runs
        in an isolated worktree (``se3 run --worktree``) and auto-merges back
        on success. When *from_issue_id* is non-empty the flow is started from
        that issue (``se3 run --from-issue <id>``), in which case the CLI
        sources the task from the issue and drives its status lifecycle.
        """
        spawned = self.spawner.spawn(
            task_description,
            project_root=project_root,
            task_type=task_type,
            discover=discover,
            worktree=worktree,
            from_issue_id=from_issue_id,
        )
        self.aggregator.add_project_root(spawned.project_root)
        return spawned

    def request_resume(
        self,
        flow_id: str,
        *,
        project_root: Optional[str] = None,
    ) -> SpawnedProcess:
        """Resume a paused/interrupted flow (entry point for remote requests).

        The daemon locates the requested *flow_id* by the same per-flow lookup
        the CLI ``se3 run --resume --flow-id`` path uses: it prefers the active
        ``engine.json`` when it still describes the flow, and otherwise falls
        back to the per-flow resumable snapshot
        (``se3/state/resumable/<flow_id>.json``), which survives a later run
        overwriting the single-slot ``engine.json``. Any flow that did not
        finish normally is resumable — COMPLETED is the only terminal status
        that is rejected; FAILED / PAUSED / RUNNING-interrupted / RECOVERING /
        INIT snapshots are all permitted. A double-spawn is blocked only when a
        live process is actually running *this* flow (a later flow B running in
        the same project root must not block resuming an earlier interrupted
        flow A).

        Raises :class:`ValueError` when the resume is not permitted (the
        caller surfaces the message as a protocol error).
        """
        root = Path(project_root).resolve() if project_root else Path.cwd()
        state_dir = root / "se3" / "state"
        engine_json = state_dir / "engine.json"

        # Locate the flow by id. Prefer the active engine.json when it holds
        # the requested flow; otherwise fall back to the resumable snapshot so
        # a flow whose engine.json slot was overwritten by a later run can
        # still be resumed. Both reads use the size-guarded header extractor
        # (issue #243 A2/A3): this preflight only needs flow_id/status, so a
        # tens-of-MB legacy engine.json / resumable snapshot is read head+tail
        # rather than fully parsed — the whole call runs off the event loop
        # (dispatched via asyncio.to_thread in the client), but the guard also
        # keeps it from burning a core there.
        data: Optional[Dict[str, Any]] = None
        engine_data = read_engine_header(engine_json, active=True)
        if engine_data is not None and str(engine_data.get("flow_id") or "") == flow_id:
            data = engine_data
        else:
            snapshot_file = state_dir / "resumable" / f"{flow_id}.json"
            snapshot_data = read_engine_header(snapshot_file)
            # The snapshot's embedded flow_id MUST match the requested id; a
            # snapshot whose payload describes a different flow (stale, misnamed,
            # or operator-created artifact) is rejected so the resume preflight
            # never authorizes resuming the wrong flow.
            if (
                snapshot_data is not None
                and str(snapshot_data.get("flow_id") or "") == flow_id
            ):
                data = snapshot_data
            elif snapshot_data is not None:
                logger.warning(
                    "Resumable snapshot %s contains mismatched flow_id %r "
                    "(requested %r); treating as not found",
                    snapshot_file,
                    snapshot_data.get("flow_id"),
                    flow_id,
                )

        if data is None:
            raise ValueError(
                f"Flow {flow_id!r} not found in engine.json or resumable "
                f"snapshots under {root}"
            )

        status = str(data.get("status") or "").upper()
        # COMPLETED is the only terminal status that is never resumable. Every
        # other status represents a flow that did not finish normally and
        # retains valid intermediate state, including a RUNNING flow that was
        # interrupted (its saved status never advanced past running).
        if status == "COMPLETED":
            raise ValueError(
                f"Flow {flow_id} is COMPLETED and cannot be resumed"
            )

        # Guard against double-spawn: refuse to resume into a project root that
        # already has ANY live ``se3 run`` process. The flow engine persists to
        # a single-slot ``se3/state/engine.json`` per project, so resuming flow
        # A while a different live process B is writing that file would race two
        # writers on the same engine.json (B's archival/attribution would read
        # A's data). Matching by project_root — not flow_id — mirrors the
        # sibling ``_resume_paused_flow`` guard. Resuming an earlier interrupted
        # flow after the later flow has finished is still permitted, because no
        # live process remains in the root.
        for record in self.supervisor.flows:
            if record.project_root == str(root) and DaemonSupervisor.is_alive(
                record.pid
            ):
                raise ValueError(
                    f"Project {root} already has a live process "
                    f"(pid={record.pid}); cannot resume flow {flow_id}"
                )

        spawned = self.spawner.resume(str(flow_id), project_root=str(root))
        self.aggregator.add_project_root(str(root))
        logger.info("Resumed flow %s in %s", flow_id, root)
        return spawned

    def request_end_session(
        self,
        flow_id: str,
        *,
        project_root: Optional[str] = None,
        reason: str = "user terminated",
    ) -> SpawnedProcess:
        """End (terminate + archive) a supervised flow (entry point for remote requests).

        Locates *flow_id* among the supervisor's tracked flows, takes its live
        pid and the main project root it is attributed to, and off-loads the
        heavy work to an ``se3 end-session`` subprocess: the subprocess
        gracefully terminates the live ``se3 run`` process (SIGTERM → grace →
        SIGKILL) and, for a worktree session, archives it the way a
        normally-completed run is cleaned up. The grace wait and the on-disk
        archival are therefore never performed on the daemon's event loop.

        Raises :class:`ValueError` when no tracked flow matches *flow_id*; the
        caller surfaces the message as a protocol-level error.
        """
        if not flow_id:
            raise ValueError("request_end_session: empty flow_id")
        record = None
        for rec in self.supervisor.flows:
            if str(rec.flow_id or "") == flow_id:
                record = rec
                break
        # A flow that is NOT among the supervised (live) flows can still be a
        # dangling worktree session left by a PAUSED / FAILED / interrupted run
        # whose ``se3 run`` process already exited — the ``se3 end-session`` CLI
        # archives those without a live process, so as long as the caller (the
        # server) supplied the project root, we still dispatch the subprocess
        # with no pid hint. We only refuse when neither a tracked flow nor a
        # caller-supplied root gives us a place to look.
        if record is None and not project_root:
            raise ValueError(
                f"Flow {flow_id!r} not found among supervised flows "
                "and no project_root supplied"
            )
        # Fold whichever root we use back to its owning ``<main>`` (a
        # worktree-copy root resolves to its main project), matching the
        # attribution every other registration path uses so the subprocess
        # archives under the right root. The server reports a worktree session's
        # ``project_root`` as the ``<main>/se3/worktrees/<name>`` sandbox itself
        # (that is the root its engine.json lives under), so a caller-supplied
        # root must be normalized too — otherwise ``se3 end-session -p`` would
        # run against the worktree dir and never locate/archive the worktree.
        raw_root = project_root if project_root else record.project_root
        resolved = resolve_worktree_main_root(raw_root)
        main_root = (
            resolved if resolved is not None else str(Path(raw_root).resolve())
        )
        pid = record.pid if record is not None else None
        spawned = self.spawner.end_session(
            flow_id, project_root=main_root, pid=pid
        )
        logger.info(
            "Dispatched end-session for flow %s in %s (pid=%s, reason=%s)",
            flow_id,
            main_root,
            pid,
            reason,
        )
        return spawned

    def _live_project_roots(self) -> Set[str]:
        """Return the set of project roots with a live ``se3 run`` process.

        Injected into :class:`DaemonAggregator` as its ``live_roots_provider``
        so the aggregator can gate the ``resumable`` flag of a ``RUNNING`` flow
        whose process is still alive (a Resume button that would only be refused
        by :meth:`request_resume` must not appear).

        The computation is deliberately **the same source** as
        :meth:`request_resume`'s double-spawn guard: it walks
        ``supervisor.flows`` and keeps each record whose ``pid`` passes
        :meth:`DaemonSupervisor.is_alive`, collecting its ``project_root``
        normalized via :func:`resolve_worktree_main_root` (a worktree-copy root
        folds back to its owning ``<main>``) so the key space matches the
        aggregator's normalized flow ``project_root``. It is fully fault
        tolerant — any unexpected error degrades to an empty set rather than
        blocking the daemon poll loop (which would stall ``STATUS_UPDATE``).
        """
        roots: Set[str] = set()
        try:
            for record in self.supervisor.flows:
                if not DaemonSupervisor.is_alive(record.pid):
                    continue
                main_root = resolve_worktree_main_root(record.project_root)
                roots.add(str(main_root if main_root is not None else record.project_root))
        except Exception:  # pragma: no cover - defensive
            logger.exception("daemon: _live_project_roots failed")
            return set()
        return roots

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
        # _write_pidfile reads the existing pidfile via json.loads; run it off the
        # loop thread so no disk JSON parse ever executes on the event loop (A3),
        # even for this tiny one-time startup read.
        await asyncio.to_thread(self._write_pidfile)
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
            daemon_key=self.config.daemon_key or "",
            snapshot_provider=lambda: self.aggregator.get_snapshot().to_dict(),
            spawn_handler=self._handle_spawn_request,
            ensure_handler=self._handle_ensure_request,
            resume_handler=self._handle_resume_request,
            end_session_handler=self._handle_end_session_request,
            respond_handler=self._handle_respond_request,
            history_provider=self.history_reader,
            calls_signature_provider=self.aggregator.pending_calls_signature,
            history_poll_interval=self.config.history_poll_interval,
        )
        self._client = client
        assert self._stop_event is not None
        return asyncio.create_task(client.run(self._stop_event))

    def _handle_spawn_request(
        self,
        task_description: str,
        project_root: str,
        task_type: str,
        discover: bool = False,
        from_issue_id: str = "",
        *,
        worktree: bool = False,
    ) -> SpawnedProcess:
        """Adapt a server SPAWN_FLOW into a :meth:`request_spawn` call."""
        return self.request_spawn(
            task_description,
            project_root=project_root or None,
            task_type=task_type or "feature",
            discover=discover,
            worktree=worktree,
            from_issue_id=from_issue_id,
        )

    def _handle_resume_request(
        self,
        flow_id: str,
        project_root: str,
    ) -> SpawnedProcess:
        """Adapt a server SPAWN_FLOW with resume_flow_id into a resume call."""
        return self.request_resume(
            flow_id,
            project_root=project_root or None,
        )

    def _handle_end_session_request(
        self,
        flow_id: str,
        project_root: str,
        reason: str = "user terminated",
    ) -> SpawnedProcess:
        """Adapt a server END_SESSION into a :meth:`request_end_session` call."""
        return self.request_end_session(
            flow_id,
            project_root=project_root or None,
            reason=reason or "user terminated",
        )

    def _handle_ensure_request(self, project_root: str) -> Any:
        """Pre-spawn hook: run ``se3 init`` in *project_root* if needed.

        Lets the web *New Task* form target a directory that is not yet an
        SE3 project (the user may have just typed a fresh absolute path into
        the "Other path…" input). The daemon auto-initializes the directory,
        then registers it with the aggregator so the freshly-created root
        appears in subsequent ``project_roots`` snapshots. Returns the
        :class:`~se3.daemon.spawner.EnsureResult` so the caller can short-
        circuit on a non-empty ``error``.
        """
        result = self.spawner.ensure_se3_project(project_root)
        if not result.error:
            try:
                self.aggregator.add_project_root(project_root)
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "Failed to register %s with aggregator after init", project_root
                )
        return result

    def _handle_respond_request(
        self, call_id: str, project_root: str, response: object
    ) -> None:
        """Adapt a server RESPOND_CALL: write the response file, then resume.

        Writing ``<call_id>.response.json`` into ``se3/calls/`` is only half the
        job. A daemon-spawned flow (``se3 run --output-format json``) exits its
        process when it pauses for a human call — e.g. a discovery
        clarification — so after the response file is written nothing would
        re-run ``se3 run``: the flow would stay PAUSED forever and a
        web-published discovery task would never advance past its first
        question. So once the answer is durably on disk we re-spawn the paused
        flow with ``--resume`` to carry the conversation forward.
        """
        from .client import _default_respond_handler

        _default_respond_handler(call_id, project_root, response)
        self._resume_paused_flow(project_root)

    def _resume_paused_flow(self, project_root: str) -> None:
        """Re-spawn the project's flow with ``--resume`` when it is PAUSED.

        Best-effort: a missing/unreadable ``engine.json``, a non-PAUSED flow,
        or a flow that already has a live ``se3 run`` process is left alone.
        """
        root = Path(project_root).resolve() if project_root else Path.cwd()
        engine_json = root / "se3" / "state" / "engine.json"
        # Only flow_id/status are needed, so read the size-guarded header rather
        # than fully parsing a possibly tens-of-MB legacy engine.json (issue
        # #243 A2). The caller dispatches this via asyncio.to_thread, so the
        # guard is the second line of defence against burning a core.
        data = read_engine_header(engine_json, active=True)
        if not isinstance(data, dict):
            logger.debug("No readable engine.json under %s; skipping resume", root)
            return
        flow_id = data.get("flow_id")
        status = str(data.get("status") or "").upper()
        if not flow_id:
            return
        if status != "PAUSED":
            # RUNNING flows have a live process; COMPLETED/FAILED must not be
            # re-run. Only a PAUSED flow needs (and is safe for) a resume.
            logger.debug("Flow %s is %s, not PAUSED; skipping resume", flow_id, status)
            return
        # Guard against a double-spawn: if a se3 run process is already alive
        # for this project (e.g. an interactive run), resuming would race two
        # writers on the same engine.json.
        for record in self.supervisor.flows:
            if record.project_root == str(root) and DaemonSupervisor.is_alive(
                record.pid
            ):
                logger.info(
                    "Flow %s already has a live process (pid=%s); skipping resume",
                    flow_id,
                    record.pid,
                )
                return
        try:
            self.spawner.resume(str(flow_id), project_root=str(root))
            self.aggregator.add_project_root(str(root))
            logger.info("Resumed paused flow %s after web call response", flow_id)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed to resume paused flow %s", flow_id)

    async def _poll_loop(self) -> None:
        """Aggregate local state on a fixed interval until the stop event fires."""
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self._poll_once()
            except Exception:  # pragma: no cover - defensive
                logger.exception("Daemon poll iteration failed")
            # Piggy-backed on the poll tick but internally gated to the far
            # lower ``gc_interval`` cadence, so no poll tick ever pays the GC
            # cost unless a full interval has elapsed.
            try:
                await self._maybe_run_gc()
            except Exception:  # pragma: no cover - defensive
                logger.exception("Daemon GC iteration failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.config.poll_interval
                )
            except asyncio.TimeoutError:
                continue

    async def _poll_once(self) -> None:
        """A single aggregation tick: discover flows, snapshot, persist status.

        Discovery, root registration, and the snapshot build can each parse
        engine.json / project_roots.json off disk, so all three are offloaded to
        worker threads and nothing on this loop touches ``json.loads``.
        ``discover_flows`` reaps dead pids then scans for external ``se3 run``
        processes; registering a newly-discovered external flow calls
        ``_read_flow_id`` -> ``read_engine_header``, which parses an
        at-or-under-guard engine.json synchronously (issue #243 A3 — no disk
        JSON parse may run on the loop thread). Registration itself
        (``add_project_root``) is likewise offloaded: a brand-new root triggers
        ``registry_persist`` -> ``_read_project_roots`` -> ``json.loads`` of
        project_roots.json, which must not run on the loop either. The snapshot
        build fans out through ``get_snapshot`` -> ``_merge_project_roots`` ->
        ``all_project_roots`` -> ``enumerate_historical_project_roots`` into a
        full ``se3/history`` walk (reading every ``_meta.json``) whenever the
        aggregator's historical-root TTL cache is cold, expired, or freshly
        invalidated — and ``add_project_root`` invalidates that cache exactly
        when a brand-new project root is registered. Running any of these on the
        loop would block heartbeats and inbound SPAWN_FLOW for its whole
        duration, the very hazard the offload in
        :meth:`DaemonClient._push_status` was meant to relieve.
        """
        flows = await asyncio.to_thread(self.supervisor.discover_flows)
        await asyncio.to_thread(self._register_discovered_roots, flows)
        self.spawner.reap()
        snapshot = await asyncio.to_thread(self.aggregator.get_snapshot)
        self._write_status(snapshot, flows)

    def _register_discovered_roots(self, flows: List["object"]) -> None:
        """Register each discovered flow's project root (worker-thread only).

        Runs off the event loop because ``add_project_root`` can, for a
        genuinely new root, synchronously parse project_roots.json via
        ``registry_persist`` (issue #243 A3 — no disk JSON parse on the loop).
        """
        for record in flows:
            # Defense in depth: a flow whose root is a ``se3/worktrees/`` copy
            # is attributed back to its main project root, so an isolation
            # worktree never registers as a standalone project (which would
            # double the WebUI project list / issue counts). ``_scan_external``
            # already resolves discovered processes, but resolving here too
            # covers any other registration path and is idempotent for an
            # already-main root.
            main_root = resolve_worktree_main_root(record.project_root)
            self.aggregator.add_project_root(main_root or record.project_root)

    # -- periodic worktree GC ----------------------------------------------

    async def _maybe_run_gc(self) -> None:
        """Run one worktree-GC sweep at most once per ``gc_interval``.

        Invoked from the high-frequency ``_poll_loop`` but gated on
        ``_last_gc_at`` so the destructive sweep fires only at the low
        ``gc_interval`` cadence, never on every 2 s poll tick. The sweep itself
        (git subprocesses, ``copytree``, json parsing) is offloaded wholesale
        via :meth:`asyncio.to_thread`, keeping the loop thread free of blocking
        IO and json parsing per the #243 hot-path constraint.
        """
        interval = self.config.gc_interval
        if interval <= 0:  # opt-out: a non-positive interval disables GC.
            return
        now = time.time()
        if self._last_gc_at is not None and (now - self._last_gc_at) < interval:
            return
        # Stamp before running so a slow sweep cannot double-trigger and so a
        # sweep that raises still waits a full interval before retrying.
        self._last_gc_at = now
        await asyncio.to_thread(self._gc_once)

    def _gc_once(self) -> None:
        """Sweep leaked worktree runs across every tracked project root.

        Worker-thread only (dispatched from :meth:`_maybe_run_gc` via
        ``asyncio.to_thread``). Sweeps each *main* project root — not
        ``all_observable_roots`` (which includes the worktree run subdirs GC is
        reclaiming) — and logs every retained-unmerged branch at WARNING so an
        operator sees a completed-but-never-merged worktree branch the safety
        net deliberately kept. One root's failure never aborts the others.

        Imported lazily so the engine merge package is only pulled in when GC
        actually runs, not on daemon import.
        """
        from ..engine.merge.worktree_gc import gc_worktree_runs

        for root in self.aggregator.all_project_roots():
            try:
                report = gc_worktree_runs(
                    Path(root),
                    max_age_seconds=self.config.gc_max_age_seconds,
                )
            except Exception:  # pragma: no cover - defensive isolation
                logger.exception(
                    "Worktree GC failed for project root %s", root
                )
                continue
            self._log_gc_report(root, report)

    def _log_gc_report(self, root: str, report: "WorktreeGCReport") -> None:
        """Emit a worktree-GC sweep's outcome to the daemon log.

        Retained-unmerged branches are the safety-critical signal (a completed
        flow whose work was never merged back), so each one is a WARNING an
        operator can act on; reclamations and per-run errors are logged for
        auditability.
        """
        if report.archived:
            logger.info(
                "Worktree GC (%s): archived %d run(s), reclaimed %d bytes",
                root, len(report.archived), report.reclaimed_bytes,
            )
        for branch, original, reason in report.retained_unmerged:
            logger.warning(
                "存在 completed 但未 merge 的 worktree 分支: kept ref '%s' "
                "(original: %s) — %s. Merge or delete it manually; GC never "
                "removes unmerged work.",
                branch, original or "?", reason,
            )
        for name, reason in report.errors:
            logger.warning("Worktree GC error for run '%s': %s", name, reason)

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


def _read_project_roots_raw(path: Path) -> List[str]:
    """Return the persisted project roots verbatim, without existence filtering.

    The registry is a ``{"project_roots": [...]}`` JSON document written by
    :func:`_append_project_root`. A missing or corrupt file is treated as an
    empty registry (returns ``[]``) so a first-run or partially-written file
    never aborts the daemon's snapshot / history index.

    This raw view (as opposed to the existence-filtered :func:`_read_project_roots`)
    is the on-disk truth :func:`_sanitize_project_roots` compares against: if the
    read-side existence filter were applied before that length comparison, stale
    (deleted) entries would already be gone from the comparison basis and the
    self-healing rewrite would never fire, leaving the file dirty forever.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    roots = data.get("project_roots")
    if not isinstance(roots, list):
        return []
    return [str(r) for r in roots if isinstance(r, str) and r]


def _read_project_roots(path: Path) -> List[str]:
    """Return the *live* persisted project roots from the registry at *path*.

    Reads the raw registry and drops every root whose directory no longer exists
    on disk, so a consumer (snapshot / history index / WebUI project list) is
    never shown a project root that has since been deleted — e.g. a pytest
    tempdir registered by a live test run and then torn down. A missing or
    corrupt file is treated as an empty registry (returns ``[]``).
    """
    return [r for r in _read_project_roots_raw(path) if os.path.exists(r)]


def _append_project_root(path: Path, root: object) -> None:
    """Append *root* to the registry file at *path* (realpath-deduplicated).

    *root* is normalised via :func:`os.path.realpath` before comparison so the
    same directory reached by different relative / symlinked spellings is stored
    once. The file is rewritten (atomically, with a sorted root list) only when
    a genuinely new root appears — an already-registered root is a no-op and
    leaves the file untouched.

    When a rewrite does fire, stale entries whose directory no longer exists on
    disk are pruned in the same write. Otherwise a registry holding a deleted
    root (e.g. a torn-down pytest tempdir persisted while the daemon was up)
    would carry that dead entry forward unchanged every time a *different* live
    root is appended, keeping the file dirty until the next startup
    :func:`_sanitize_project_roots` pass — the very resurfacing the existence
    self-heal exists to prevent.

    A worktree isolation copy (``<main>/se3/worktrees/<name>``) is folded back
    to its owning ``<main>`` here as a defence-in-depth backstop: the primary
    normalization seam is :meth:`DaemonAggregator.add_project_root` (which feeds
    this callback already-normalized), but normalizing again at the disk write
    point guarantees no worktree path can ever land in the persistent registry
    regardless of how a future caller reaches it.
    """
    main_root = resolve_worktree_main_root(root)
    if main_root is not None:
        root = main_root
    try:
        resolved = os.path.realpath(str(root))
    except OSError:  # pragma: no cover - defensive
        return
    if not resolved:
        return
    # Skip a root that no longer exists on disk: registering a since-deleted
    # directory (e.g. a torn-down pytest tempdir) would only re-pollute the
    # persistent list that the read-side / sanitize self-heal exists to keep clean.
    if not os.path.exists(resolved):
        return
    existing = _read_project_roots_raw(path)
    # Prune since-deleted roots from the persisted set so the rewrite triggered
    # by this new root also erases stale entries, rather than carrying them
    # forward untouched until the next startup sanitize pass.
    live = [r for r in existing if os.path.exists(r)]
    if resolved in live:
        return
    live.append(resolved)
    _atomic_write_json(path, {"project_roots": sorted(set(live))})


def _sanitize_project_roots(path: Path) -> None:
    """One-time startup cleanup of stale entries from the registry file.

    Two classes of stale entry are dropped:

    * **Worktree copies.** The worktree→main normalization now applied at every
      registration entry point (see :meth:`DaemonAggregator.add_project_root`
      and :func:`_append_project_root`) prevents *new* pollution, but a registry
      written before the normalization existed may already hold persisted
      ``<main>/se3/worktrees/<name>`` entries.
    * **Deleted roots.** A project root that no longer exists on disk — most
      commonly a pytest tempdir that a live test run registered and then tore
      down — would otherwise keep surfacing a dead project in the WebUI list.

    Because the file survives restarts, either kind of stale entry would keep
    resurfacing indefinitely. This scans the registry once at daemon startup,
    drops every worktree-copy (per
    :func:`~se3.daemon.supervisor.is_worktree_copy_root`) and every
    no-longer-existent root, and atomically rewrites the file **only** when
    something was actually removed (a clean registry is left untouched).

    The comparison is made against the *raw* registry
    (:func:`_read_project_roots_raw`) rather than the existence-filtered read so
    the self-heal actually detects — and erases from disk — the deleted entries
    the read-side filter would otherwise silently hide.

    Fully fault-tolerant: a missing / corrupt file reads as an empty registry
    and a write failure is logged, never propagated — sanitation must never
    block daemon startup.
    """
    try:
        existing = _read_project_roots_raw(path)
    except Exception:  # pragma: no cover - defensive
        return
    if not existing:
        return
    cleaned = [
        r for r in existing if not is_worktree_copy_root(r) and os.path.exists(r)
    ]
    if len(cleaned) == len(existing):
        # No stale entries — leave the file untouched (no needless write).
        return
    try:
        _atomic_write_json(path, {"project_roots": sorted(set(cleaned))})
        logger.info(
            "Sanitized project-roots registry: removed %d stale entr%s",
            len(existing) - len(cleaned),
            "y" if len(existing) - len(cleaned) == 1 else "ies",
        )
    except OSError:  # pragma: no cover - defensive
        logger.debug("Failed to rewrite sanitized project roots", exc_info=True)


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
    # Pass the daemon credential through the environment, never on argv — a key
    # on the command line would be visible in the process list. The foreground
    # child reads SE3_DAEMON_KEY back when it rebuilds its DaemonConfig.
    if config.daemon_key:
        child_env["SE3_DAEMON_KEY"] = config.daemon_key
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
