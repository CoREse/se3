"""The daemon's outbound WebSocket client to the central server.

:class:`DaemonClient` maintains a single outbound WebSocket connection from a
resident ``se3 daemon`` to the central server. The connection is *outbound*
(the daemon dials the server) so SE3 machines never have to expose an inbound
port — this is the NAT-friendly half of the daemon↔server architecture.

Responsibilities:

* connect to ``ws://<server>/ws`` and announce itself with a
  :data:`~se3.daemon.protocol.MSG_HELLO`;
* push :data:`~se3.daemon.protocol.MSG_STATUS_UPDATE` snapshots on a fixed
  interval (the data source is the daemon's aggregator);
* answer server :data:`~se3.daemon.protocol.MSG_PING` heartbeats with a
  :data:`~se3.daemon.protocol.MSG_PONG`;
* route :data:`~se3.daemon.protocol.MSG_SPAWN_FLOW` to the daemon's spawner and
  :data:`~se3.daemon.protocol.MSG_RESPOND_CALL` to a ``se3/calls/`` response
  file;
* reconnect automatically with exponential backoff (capped at 60 s) and
  re-HELLO + push a full status snapshot after every reconnect.

The ``websockets`` library is an *optional* dependency (it ships in the
``se3[server]`` extra). The client imports it lazily: a daemon started without
``--server-url`` never touches it, and one started *with* a server URL but
without ``websockets`` installed logs a clear install hint and degrades to
local-only operation rather than crashing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from . import protocol

logger = logging.getLogger(__name__)

# Reconnect backoff bounds (seconds).
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0
_BACKOFF_FACTOR = 2.0

# Default seconds between STATUS_UPDATE pushes.
_STATUS_INTERVAL = 5.0

# Default fast cadence (seconds) at which the push loop checks the active-flow
# disk signature so a CLI step that advances ``engine.json`` / appends a jsonl
# line is reflected on the web within a tick instead of a full status interval.
_HISTORY_POLL_INTERVAL = 1.0

#: Type of the snapshot provider — returns a JSON-serializable machine snapshot.
SnapshotProvider = Callable[[], Dict[str, Any]]
#: Type of the spawn handler — called with
#: (task_description, project_root, task_type, discover).
SpawnHandler = Callable[[str, str, str, bool], Any]
#: Type of the project-init handler — called with ``project_root`` before
#: SPAWN_FLOW is routed to the spawn handler. Returns an object whose
#: truthy ``.error`` attribute aborts the spawn; ``None`` means "skip the
#: pre-spawn check".
EnsureHandler = Callable[[str], Any]
#: Type of the respond handler — called with (call_id, project_root, response).
RespondHandler = Callable[[str, str, Any], Any]
#: Type of the history provider — a :class:`~se3.daemon.history.DaemonHistoryReader`
#: (or any object exposing ``build_index`` / ``read_flow`` / ``read_active_flows``).
HistoryProvider = Any


def _normalize_ws_url(server_url: str) -> str:
    """Return a ``ws://host:port/ws`` URL from a user-supplied server URL.

    Accepts bare hosts (``host`` or ``host:8080``), ``http(s)://`` and
    ``ws(s)://`` URLs, and appends the ``/ws`` daemon endpoint path when none
    is present. When the host carries no explicit port, the shared
    :data:`~se3.daemon.protocol.DEFAULT_SERVER_PORT` is filled in so the
    daemon and ``se3-server`` agree on the same default (a bare ``ws://host``
    would otherwise fall back to the WebSocket-standard port 80).
    """
    url = server_url.strip()
    if url.startswith("http://"):
        url = "ws://" + url[len("http://") :]
    elif url.startswith("https://"):
        url = "wss://" + url[len("https://") :]
    elif not url.startswith(("ws://", "wss://")):
        url = "ws://" + url
    # Split off any path component already supplied.
    scheme, _, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    # Fill in the default port when the host carries none. An IPv6 literal is
    # bracketed (``[::1]``), so only a ``:`` *after* the closing bracket is a
    # port separator; a bare host has a port iff it contains a ``:``.
    if host:
        if host.startswith("["):
            has_port = "]:" in host
        else:
            has_port = ":" in host
        if not has_port:
            host = f"{host}:{protocol.DEFAULT_SERVER_PORT}"
    if not slash or not path or path == "/":
        return f"{scheme}://{host}/ws"
    return f"{scheme}://{host}/{path}"


class DaemonClient:
    """Outbound WebSocket client linking a daemon to the central server."""

    def __init__(
        self,
        server_url: str,
        *,
        machine_id: str,
        hostname: str,
        se3_version: str,
        snapshot_provider: SnapshotProvider,
        spawn_handler: Optional[SpawnHandler] = None,
        ensure_handler: Optional[EnsureHandler] = None,
        respond_handler: Optional[RespondHandler] = None,
        history_provider: Optional[HistoryProvider] = None,
        status_interval: float = _STATUS_INTERVAL,
        history_poll_interval: float = _HISTORY_POLL_INTERVAL,
    ) -> None:
        """Create a client.

        Args:
            server_url: Central-server URL; normalized to ``ws://.../ws``.
            machine_id: Stable id advertised in HELLO.
            hostname: Human-readable host name advertised in HELLO.
            se3_version: SE3 version advertised in HELLO.
            snapshot_provider: Zero-arg callable returning the current machine
                snapshot dict (typically ``aggregator.get_snapshot().to_dict()``).
            spawn_handler: Callable invoked for an incoming SPAWN_FLOW.
            ensure_handler: Optional pre-spawn hook called with the resolved
                ``project_root`` *before* the spawn handler. Used by the
                daemon to auto-run ``se3 init`` on a brand-new target
                directory (and to register the root with the aggregator).
                When the returned object has a truthy ``.error`` attribute,
                the SPAWN_FLOW is aborted with that error logged.
            respond_handler: Callable invoked for an incoming RESPOND_CALL;
                when ``None`` the client writes the response file itself.
            history_provider: A :class:`~se3.daemon.history.DaemonHistoryReader`
                used to report the history index, push active-flow increments
                and answer HISTORY_REQUEST pulls. When ``None`` history support
                is disabled and the client behaves as before.
            status_interval: Seconds between STATUS_UPDATE pushes.
            history_poll_interval: Fast cadence (seconds) at which the push loop
                samples the active-flow disk signature to decide whether to push
                an incremental history delta. Clamped to ``status_interval`` so
                a very low status interval still polls history at least as
                often; the steady STATUS_UPDATE heartbeat keeps its own
                ``status_interval`` cadence regardless.
        """
        self.server_url = _normalize_ws_url(server_url)
        self.machine_id = machine_id
        self.hostname = hostname
        self.se3_version = se3_version
        self._snapshot_provider = snapshot_provider
        self._spawn_handler = spawn_handler
        self._ensure_handler = ensure_handler
        self._respond_handler = respond_handler or _default_respond_handler
        self._interject_handler = _default_interject_handler
        self._history_provider = history_provider
        self.status_interval = max(0.5, float(status_interval))
        self.history_poll_interval = max(
            0.1, min(float(history_poll_interval), self.status_interval)
        )

        self._seq = 0
        self._connected = False
        self._last_error: Optional[str] = None
        # History push state, reset on every (re)connection so a freshly
        # connected server always receives a fresh index and full snapshots.
        self._last_index: Optional[list] = None
        self._history_cursors: Dict[str, Dict[str, int]] = {}
        # Last active-flow disk signature seen by the push loop; an unchanged
        # signature means there is nothing new to push (debounce).
        self._last_history_signature: Dict[str, Any] = {}

    # -- introspection -----------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether an active WebSocket session is currently established."""
        return self._connected

    @property
    def last_error(self) -> Optional[str]:
        """The most recent connection error string, if any."""
        return self._last_error

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # -- run loop ----------------------------------------------------------

    async def run(self, stop_event: asyncio.Event) -> None:
        """Connect, serve, and reconnect until *stop_event* is set.

        This is the client's top-level coroutine; it shares the daemon's
        event loop. It returns cleanly when *stop_event* fires or when the
        ``websockets`` dependency is unavailable.
        """
        try:
            import websockets  # type: ignore  # noqa: F401
        except Exception:  # pragma: no cover - exercised via degraded path
            logger.warning(
                "Cannot dial central server %s: the 'websockets' package is not "
                "installed. Install it with: pip install 'se3[server]'. "
                "The daemon continues in local-only mode.",
                self.server_url,
            )
            self._last_error = "websockets not installed"
            return

        backoff = _BACKOFF_INITIAL
        while not stop_event.is_set():
            try:
                await self._session(stop_event, websockets)
                backoff = _BACKOFF_INITIAL  # clean exit -> reset backoff
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("Server connection lost (%s); retrying in %.0fs", exc, backoff)
            finally:
                self._connected = False
            if stop_event.is_set():
                break
            # Wait out the backoff, but wake immediately on shutdown.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(_BACKOFF_MAX, backoff * _BACKOFF_FACTOR)

    async def _session(self, stop_event: asyncio.Event, websockets: Any) -> None:
        """Run one full WebSocket session: HELLO, then receive/push loops.

        Returns cleanly when *stop_event* fires or when the socket closes; in
        both cases :meth:`run` decides whether to exit or reconnect. The
        receive and push loops are raced against the stop event so a shutdown
        is honoured even while blocked waiting for an inbound frame.
        """
        logger.info("Dialing central server at %s", self.server_url)
        async with websockets.connect(self.server_url, open_timeout=10) as ws:
            self._connected = True
            self._last_error = None
            # A new session: forget prior history state so the server gets a
            # fresh index and full active-flow snapshots after every reconnect.
            self._last_index = None
            self._history_cursors = {}
            self._last_history_signature = {}
            # Announce ourselves, then push a full snapshot immediately so a
            # freshly (re)connected server has state before the first tick.
            await self._send(ws, protocol.make_hello(self.machine_id, self.hostname, self.se3_version))
            await self._push_status(ws)
            await self._push_history(ws, force_index=True)
            # Prime the signature so the fast push loop only fires on the *next*
            # disk change rather than immediately re-pushing what we just sent.
            self._history_changed()
            logger.info("Connected to central server; HELLO sent")

            recv_task = asyncio.create_task(self._receive_loop(ws, stop_event))
            push_task = asyncio.create_task(self._push_loop(ws, stop_event))
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                {recv_task, push_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in (*done, *pending):
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # pragma: no cover
                    pass

    async def _receive_loop(self, ws: Any, stop_event: asyncio.Event) -> None:
        """Read and dispatch inbound server messages until the socket closes."""
        async for raw in ws:
            if stop_event.is_set():
                break
            try:
                message = protocol.decode(raw)
            except protocol.ProtocolError as exc:
                logger.warning("Dropping malformed server frame: %s", exc)
                continue
            await self._dispatch(ws, message)

    async def _push_loop(self, ws: Any, stop_event: asyncio.Event) -> None:
        """Drive status + history pushes on a single loop until shutdown.

        The loop wakes on the fast ``history_poll_interval`` cadence. A
        STATUS_UPDATE is sent on the steady ``status_interval`` heartbeat. A
        HISTORY push fires whenever the active-flow disk signature changed since
        the last check — so a CLI step that advances ``engine.json`` / appends a
        jsonl line reaches the web within one fast tick instead of waiting a
        whole status interval — and also on every status tick as a backstop in
        case a change is ever missed. When nothing changed, the history check is
        a cheap stat-only scan and no frame is sent (debounce).

        Both pushes share this one coroutine so their ``ws.send`` calls never
        interleave (which two independent loops racing on the same socket could
        do), keeping every wire frame intact.
        """
        last_status = time.monotonic()
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.history_poll_interval
                )
            except asyncio.TimeoutError:
                pass
            else:
                break
            now = time.monotonic()
            status_due = (now - last_status) >= self.status_interval
            if status_due:
                last_status = now
                await self._push_status(ws)
            # Push history on a real disk change, or on the status tick (backstop).
            if status_due or self._history_changed():
                await self._push_history(ws)

    def _history_changed(self) -> bool:
        """Return whether any active flow's disk signature changed since last check.

        Updates :attr:`_last_history_signature` as a side effect. Returns
        ``False`` when no history provider (or signature support) is configured,
        so a provider-less client never spuriously pushes. A signature lookup
        failure conservatively reports a change so the next push still runs.
        """
        provider = self._history_provider
        if provider is None or not hasattr(provider, "active_flow_signature"):
            return False
        try:
            signature = provider.active_flow_signature()
        except Exception:
            logger.debug(
                "active_flow_signature failed; forcing a history push",
                exc_info=True,
            )
            return True
        changed = signature != self._last_history_signature
        self._last_history_signature = signature
        return changed

    # -- message handling --------------------------------------------------

    async def _dispatch(self, ws: Any, message: protocol.Message) -> None:
        """Route one inbound server message to its handler."""
        if message.type == protocol.MSG_PING:
            await self._send(ws, protocol.make_pong(seq=message.seq))
        elif message.type == protocol.MSG_WELCOME:
            accepted = message.payload.get("accepted", True)
            logger.info("Server WELCOME received (accepted=%s)", accepted)
        elif message.type == protocol.MSG_SPAWN_FLOW:
            self._handle_spawn(message.payload)
        elif message.type == protocol.MSG_RESPOND_CALL:
            self._handle_respond(message.payload)
        elif message.type == protocol.MSG_INTERJECT_FLOW:
            self._handle_interject(message.payload)
        elif message.type == protocol.MSG_HISTORY_REQUEST:
            await self._handle_history_request(ws, message.payload)
        elif message.type == protocol.MSG_HISTORY_INDEX_REQUEST:
            await self._handle_history_index_request(ws)
        else:  # pragma: no cover - defensive; decode() already validates
            logger.debug("Ignoring unexpected server message type %s", message.type)

    def _handle_spawn(self, payload: Dict[str, Any]) -> None:
        """Route a SPAWN_FLOW instruction to the daemon's spawner.

        When an ``ensure_handler`` is configured, it runs first against the
        target ``project_root`` — that is what lets the web *New Task* form
        send a brand-new empty directory: the daemon auto-runs ``se3 init``
        there and registers it before the spawn proceeds. A truthy
        ``.error`` on the returned object aborts the spawn and is logged;
        nothing half-initialized leaks downstream.
        """
        task = str(payload.get("task_description") or "").strip()
        if not task:
            logger.warning("Ignoring SPAWN_FLOW with empty task_description")
            return
        project_root = str(payload.get("project_root") or "")
        task_type = str(payload.get("task_type") or "feature")
        discover = bool(payload.get("discover", False))
        if self._spawn_handler is None:
            logger.warning("Received SPAWN_FLOW but no spawn handler is configured")
            return
        if self._ensure_handler is not None and project_root:
            try:
                ensure = self._ensure_handler(project_root)
            except Exception:
                logger.exception(
                    "SPAWN_FLOW ensure-project handler failed for %s; aborting spawn",
                    project_root,
                )
                return
            error = getattr(ensure, "error", "") if ensure is not None else ""
            if error:
                logger.error(
                    "SPAWN_FLOW aborted: cannot initialize %s: %s",
                    project_root,
                    error,
                )
                return
        try:
            self._spawn_handler(task, project_root, task_type, discover)
            logger.info("SPAWN_FLOW handled: %s", task[:80])
        except Exception:
            logger.exception("SPAWN_FLOW handler failed")

    def _handle_respond(self, payload: Dict[str, Any]) -> None:
        """Route a RESPOND_CALL instruction to the response-file writer."""
        call_id = str(payload.get("call_id") or "").strip()
        if not call_id:
            logger.warning("Ignoring RESPOND_CALL with empty call_id")
            return
        project_root = str(payload.get("project_root") or "")
        response = payload.get("response")
        try:
            self._respond_handler(call_id, project_root, response)
            logger.info("RESPOND_CALL handled for call %s", call_id)
        except Exception:
            logger.exception("RESPOND_CALL handler failed")

    def _handle_interject(self, payload: Dict[str, Any]) -> None:
        """Route an INTERJECT_FLOW instruction to the interjection-file writer."""
        text = str(payload.get("text") or "").strip()
        if not text:
            logger.warning("Ignoring INTERJECT_FLOW with empty text")
            return
        flow_id = str(payload.get("flow_id") or "")
        project_root = str(payload.get("project_root") or "").strip()
        if not project_root:
            project_root = self._resolve_interject_root(flow_id)
        if not project_root:
            logger.warning(
                "INTERJECT_FLOW: cannot resolve project root for flow %s; dropping",
                flow_id,
            )
            return
        try:
            self._interject_handler(flow_id, project_root, text)
            logger.info("INTERJECT_FLOW handled for flow %s", flow_id)
        except Exception:
            logger.exception("INTERJECT_FLOW handler failed")

    def _resolve_interject_root(self, flow_id: str) -> str:
        """Resolve a flow's project root from the current machine snapshot.

        Used when an INTERJECT_FLOW payload carries no ``project_root`` — the
        daemon matches *flow_id* against its own snapshot. Returns an empty
        string when the flow cannot be located.
        """
        try:
            snapshot = self._snapshot_provider()
        except Exception:
            logger.debug("Snapshot lookup for INTERJECT_FLOW failed", exc_info=True)
            return ""
        for flow in (snapshot or {}).get("flows") or []:
            if isinstance(flow, dict) and str(flow.get("flow_id") or "") == flow_id:
                return str(flow.get("project_root") or "").strip()
        return ""

    async def _handle_history_request(self, ws: Any, payload: Dict[str, Any]) -> None:
        """Answer a server HISTORY_REQUEST with a HISTORY_DATA reply.

        The server sends this when the web UI opens a flow whose records the
        server has not cached. The (optional) ``cursor`` lets the server ask
        for an incremental delta; an absent cursor requests a full snapshot.
        """
        provider = self._history_provider
        if provider is None:
            logger.warning("Received HISTORY_REQUEST but no history provider configured")
            return
        flow_id = str(payload.get("flow_id") or "").strip()
        if not flow_id:
            logger.warning("Ignoring HISTORY_REQUEST with empty flow_id")
            return
        project_root = str(payload.get("project_root") or "") or None
        cursor = payload.get("cursor") or {}
        try:
            # Disk I/O is offloaded to a thread so a large session's jsonl read
            # cannot block the event loop past the server's pull timeout or the
            # heartbeat-loss threshold (which would briefly mark the daemon
            # offline and grey out the machine in the web UI).
            read = await asyncio.to_thread(
                provider.read_flow, flow_id, project_root=project_root, cursor=cursor
            )
        except Exception:
            logger.exception("HISTORY_REQUEST read failed for flow %s", flow_id)
            return
        try:
            await self._send(
                ws,
                protocol.make_history_data(
                    read.flow_id,
                    read.mode,
                    read.records,
                    cursor=read.cursor,
                    seq=self._next_seq(),
                ),
            )
            logger.info(
                "HISTORY_REQUEST answered for flow %s (%d record(s), %s)",
                flow_id,
                len(read.records),
                read.mode,
            )
        except Exception:
            logger.debug("HISTORY_DATA send failed", exc_info=True)

    async def _handle_history_index_request(self, ws: Any) -> None:
        """Force a fresh rebuild + re-push of the history index.

        The server broadcasts :data:`~se3.daemon.protocol.MSG_HISTORY_INDEX_REQUEST`
        when a browser enters the history view, so we rebuild the index from
        disk and push it immediately. ``force_index=True`` bypasses the
        change-debounce in :meth:`_push_history`, so the server's waiter is
        resolved even when the index has not changed since the last push. A
        missing history provider is a safe no-op, and any failure is swallowed
        (and logged) so the connection survives.
        """
        if self._history_provider is None:
            logger.debug("Ignoring HISTORY_INDEX_REQUEST: no history provider")
            return
        try:
            await self._push_history(ws, force_index=True)
            logger.info("HISTORY_INDEX_REQUEST handled: forced index re-push")
        except Exception:
            logger.exception("HISTORY_INDEX_REQUEST handling failed")

    # -- sending -----------------------------------------------------------

    async def _send(self, ws: Any, message: protocol.Message) -> None:
        """JSON-encode and send *message* on the socket."""
        await ws.send(message.to_json())

    async def _push_status(self, ws: Any) -> None:
        """Build and send a STATUS_UPDATE from the snapshot provider."""
        try:
            snapshot = self._snapshot_provider()
        except Exception:
            logger.exception("Snapshot provider failed; skipping STATUS_UPDATE")
            return
        message = protocol.make_status_update(snapshot, seq=self._next_seq())
        try:
            await self._send(ws, message)
        except Exception:
            # The receive loop will observe the closed socket and trigger a
            # reconnect; nothing more to do here.
            logger.debug("STATUS_UPDATE send failed", exc_info=True)

    async def _push_history(self, ws: Any, *, force_index: bool = False) -> None:
        """Report the history index (on change) and push active-flow deltas.

        The index is re-sent only when it actually changed since the last push
        (or when *force_index* is set, used right after a (re)connect). Active
        flows are read incrementally off ``self._history_cursors`` so each tick
        ships only the conversation lines appended since the previous push.
        """
        provider = self._history_provider
        if provider is None:
            return
        try:
            # Index rebuild walks history directories from disk; offload it so a
            # forced rebuild on a machine with many archived flows does not
            # block the event loop (heartbeats, PONGs, other pushes).
            metas = await asyncio.to_thread(provider.build_index)
            index = [meta.to_dict() for meta in metas]
        except Exception:
            logger.exception("History index build failed; skipping history push")
            return
        if force_index or index != self._last_index:
            self._last_index = index
            try:
                await self._send(
                    ws, protocol.make_history_index(index, seq=self._next_seq())
                )
            except Exception:
                logger.debug("HISTORY_INDEX send failed", exc_info=True)
                return
        try:
            # read_active_flows fans out into multiple jsonl reads; offload so a
            # big active session does not stall the event loop.
            reads = await asyncio.to_thread(
                provider.read_active_flows, self._history_cursors
            )
        except Exception:
            logger.exception("Active-flow history read failed")
            return
        # Rebuild the cursor map from this read so it tracks exactly the flows
        # still producing records: every active flow (always returned, even with
        # an empty delta, so its cursor keeps advancing) plus any terminal flow
        # flushed one last time. A fully-drained terminal flow drops out, which
        # bounds the map's growth over a long-lived daemon. Atomic engine.json
        # writes mean an active flow is never transiently missing, so pruning
        # cannot accidentally trigger a duplicate full re-read.
        self._history_cursors = {read.flow_id: read.cursor for read in reads}
        for read in reads:
            if not read.records:
                continue
            try:
                await self._send(
                    ws,
                    protocol.make_history_data(
                        read.flow_id,
                        read.mode,
                        read.records,
                        cursor=read.cursor,
                        seq=self._next_seq(),
                    ),
                )
            except Exception:
                logger.debug("HISTORY_DATA send failed", exc_info=True)
                return


def _default_respond_handler(call_id: str, project_root: str, response: Any) -> None:
    """Write a human-call response file under ``<project_root>/se3/calls/``.

    SE3's ``se3/calls/`` directory is the human-call queue; writing a
    ``<call_id>.response.json`` file there is how a server-delivered response
    re-enters a paused flow. The file is written atomically (temp + rename).
    """
    root = Path(project_root).resolve() if project_root else Path.cwd()
    calls_dir = root / "se3" / "calls"
    calls_dir.mkdir(parents=True, exist_ok=True)
    target = calls_dir / f"{call_id}.response.json"
    payload = {
        "call_id": call_id,
        "response": response,
        "responded_at": time.time(),
        "source": "daemon-client",
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    tmp.replace(target)


def _default_interject_handler(flow_id: str, project_root: str, text: str) -> None:
    """Write a mid-flow interjection request file under ``se3/calls/``.

    A server-delivered :data:`~se3.daemon.protocol.MSG_INTERJECT_FLOW` becomes
    an ``interjection``-kind call file; the running ``se3 run`` process drains
    it at the next step boundary and folds it into ``user_interjections``.
    """
    from ..engine.interaction_calls import calls_dir_for, write_interjection_request

    root = Path(project_root).resolve() if project_root else Path.cwd()
    write_interjection_request(calls_dir_for(root), text, flow_id=flow_id)
