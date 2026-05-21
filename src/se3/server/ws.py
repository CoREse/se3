"""The SE3 central server's WebSocket endpoint and daemon connection pool.

Daemons dial the server's ``/ws`` endpoint and speak the protocol defined in
:mod:`se3.daemon.protocol`. This module owns:

* :class:`ConnectionManager` — the ``machine_id -> WebSocket`` pool, plus the
  downlink routing used by the REST API to push ``SPAWN_FLOW`` / ``RESPOND_CALL``
  instructions to a specific daemon;
* :func:`handle_daemon_connection` — the per-connection coroutine: validate the
  opening ``HELLO``, answer with ``WELCOME``, then run the receive loop and a
  heartbeat ``PING``/``PONG`` loop until the socket closes.

The server imports the protocol module straight from the core ``se3.daemon``
package, so the wire schema has a single source of truth shared by both ends.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from se3.daemon import protocol

from .state import ServerState

logger = logging.getLogger(__name__)

# Heartbeat tuning (seconds).
PING_INTERVAL = 15.0
#: A daemon is dropped if no PONG (or any frame) arrives within this window.
HEARTBEAT_TIMEOUT = 45.0

# Server identity advertised in WELCOME.
try:  # pragma: no cover - import guard
    import se3 as _se3

    SERVER_VERSION = str(getattr(_se3, "__version__", "unknown"))
except Exception:  # pragma: no cover - defensive
    SERVER_VERSION = "unknown"


class ConnectionManager:
    """Tracks live daemon WebSocket connections and routes downlink messages."""

    def __init__(self) -> None:
        self._connections: Dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def connect(self, machine_id: str, websocket: Any) -> None:
        """Register *websocket* as the live connection for *machine_id*.

        If the machine already had a connection, the stale one is dropped —
        a reconnecting daemon always supersedes its previous socket.
        """
        async with self._lock:
            stale = self._connections.get(machine_id)
            self._connections[machine_id] = websocket
        if stale is not None and stale is not websocket:
            try:
                await stale.close()
            except Exception:  # pragma: no cover - best effort
                pass
        logger.info("Daemon connected: %s", machine_id)

    async def disconnect(self, machine_id: str, websocket: Any = None) -> None:
        """Remove *machine_id* from the pool.

        When *websocket* is given, the entry is only removed if it still
        matches — avoids a late-closing old socket evicting a fresh one.
        """
        async with self._lock:
            current = self._connections.get(machine_id)
            if current is not None and (websocket is None or current is websocket):
                self._connections.pop(machine_id, None)
        logger.info("Daemon disconnected: %s", machine_id)

    def is_connected(self, machine_id: str) -> bool:
        """Whether *machine_id* currently has a live connection."""
        return machine_id in self._connections

    @property
    def machine_ids(self) -> list:
        """A snapshot list of currently-connected machine ids."""
        return list(self._connections.keys())

    async def send_to(self, machine_id: str, message: protocol.Message) -> bool:
        """Send *message* to one daemon; return ``False`` if not connected."""
        websocket = self._connections.get(machine_id)
        if websocket is None:
            return False
        try:
            await websocket.send_text(message.to_json())
            return True
        except Exception:
            logger.warning("Failed to send %s to %s", message.type, machine_id)
            return False


class HistoryRequestRegistry:
    """Tracks in-flight on-demand history pulls awaiting a daemon reply.

    A REST handler that needs a flow's history but finds the cache empty
    sends a ``MSG_HISTORY_REQUEST`` to the owning daemon and parks an
    :class:`asyncio.Future` here keyed by ``flow_id``. When the matching
    ``MSG_HISTORY_DATA`` arrives on the daemon receive loop it resolves every
    waiter for that flow. Lives entirely in process memory.
    """

    def __init__(self) -> None:
        self._waiters: Dict[str, list] = {}

    def register(self, flow_id: str) -> "asyncio.Future":
        """Park and return a future that resolves when *flow_id* data lands."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(flow_id, []).append(fut)
        return fut

    def resolve(self, flow_id: str, data: Any) -> None:
        """Resolve every waiter parked for *flow_id* with *data*."""
        for fut in self._waiters.pop(flow_id, []):
            if not fut.done():
                fut.set_result(data)

    def discard(self, flow_id: str, fut: "asyncio.Future") -> None:
        """Drop a single waiter (e.g. after a timeout) without resolving it."""
        waiters = self._waiters.get(flow_id)
        if not waiters:
            return
        if fut in waiters:
            waiters.remove(fut)
        if not waiters:
            self._waiters.pop(flow_id, None)


class IndexRefreshRegistry:
    """Tracks in-flight history-index refreshes awaiting a daemon's re-push.

    ``GET /api/history`` broadcasts a ``MSG_HISTORY_INDEX_REQUEST`` to every
    connected daemon and parks one :class:`asyncio.Future` per ``machine_id``
    here. When that daemon's forced ``MSG_HISTORY_INDEX`` lands on the receive
    loop the matching waiter is resolved, so the REST handler can return the
    freshly aggregated index instead of a stale relayed snapshot. Mirrors
    :class:`HistoryRequestRegistry` but is keyed by machine rather than flow.
    Lives entirely in process memory.
    """

    def __init__(self) -> None:
        self._waiters: Dict[str, list] = {}

    def register(self, machine_id: str) -> "asyncio.Future":
        """Park and return a future that resolves when *machine_id* re-pushes."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(machine_id, []).append(fut)
        return fut

    def resolve(self, machine_id: str) -> None:
        """Resolve every waiter parked for *machine_id* (no-op when none)."""
        for fut in self._waiters.pop(machine_id, []):
            if not fut.done():
                fut.set_result(True)

    def discard(self, machine_id: str, fut: "asyncio.Future") -> None:
        """Drop a single waiter (e.g. after a timeout) without resolving it."""
        waiters = self._waiters.get(machine_id)
        if not waiters:
            return
        if fut in waiters:
            waiters.remove(fut)
        if not waiters:
            self._waiters.pop(machine_id, None)


async def broadcast_index_refresh(
    manager: ConnectionManager,
    registry: "IndexRefreshRegistry",
) -> Dict[str, "asyncio.Future"]:
    """Ask every connected daemon to rebuild and immediately re-push its index.

    Sends a ``MSG_HISTORY_INDEX_REQUEST`` down each live daemon socket and
    parks one waiter future per machine that accepted the request. Returns a
    ``{machine_id: future}`` mapping the caller can await with a bounded
    timeout; each future resolves when that daemon's forced
    ``MSG_HISTORY_INDEX`` is applied to the cache. Returns an empty mapping
    when no daemon is connected. A machine whose send fails has its waiter
    discarded immediately so no future is left dangling.
    """
    waiters: Dict[str, "asyncio.Future"] = {}
    for machine_id in manager.machine_ids:
        fut = registry.register(machine_id)
        sent = await manager.send_to(machine_id, protocol.make_history_index_request())
        if sent:
            waiters[machine_id] = fut
        else:
            registry.discard(machine_id, fut)
    return waiters


async def request_history(
    manager: ConnectionManager,
    state: ServerState,
    flow_id: str,
    *,
    cursor: Optional[Dict[str, Any]] = None,
    project_root: str = "",
) -> bool:
    """Send a ``MSG_HISTORY_REQUEST`` to the daemon owning *flow_id*.

    Resolves the owning machine via the history index / live flow set and
    routes the request down its WebSocket. Returns ``False`` when no
    connected daemon owns the flow.
    """
    machine_id = await state.find_machine_for_history_flow(flow_id)
    if machine_id is None or not manager.is_connected(machine_id):
        return False
    message = protocol.make_history_request(
        flow_id, project_root=project_root, cursor=cursor
    )
    return await manager.send_to(machine_id, message)


class UiHub:
    """Fan-out hub for web-frontend WebSocket clients.

    The frontend (``static/app.js``) dials ``/ws/ui`` and expects a realtime
    push of the whole machine list whenever a daemon's state changes. This hub
    tracks those browser sockets and broadcasts to all of them at once. Unlike
    :class:`ConnectionManager` it is keyed by nothing — frontend clients are
    anonymous and interchangeable.
    """

    def __init__(self) -> None:
        self._clients: set = set()
        self._lock = asyncio.Lock()

    async def register(self, websocket: Any) -> None:
        async with self._lock:
            self._clients.add(websocket)
        logger.info("UI client connected (%d total)", len(self._clients))

    async def unregister(self, websocket: Any) -> None:
        async with self._lock:
            self._clients.discard(websocket)
        logger.info("UI client disconnected (%d total)", len(self._clients))

    @property
    def client_count(self) -> int:
        """Number of currently-connected frontend clients."""
        return len(self._clients)

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        """Send *payload* (a JSON-serializable dict) to every UI client."""
        async with self._lock:
            clients = list(self._clients)
        if not clients:
            return
        import json

        text = json.dumps(payload, ensure_ascii=False, default=str)
        dead = []
        for client in clients:
            try:
                await client.send_text(text)
            except Exception:  # pragma: no cover - best effort
                dead.append(client)
        if dead:
            async with self._lock:
                for client in dead:
                    self._clients.discard(client)


async def _push_state(hub: Optional["UiHub"], state: ServerState, kind: str) -> None:
    """Broadcast the current machine list to all UI clients (best effort)."""
    if hub is None or hub.client_count == 0:
        return
    machines = await state.get_machines_full()
    await hub.broadcast({"type": kind, "machines": machines})


async def _push_history_index(hub: Optional["UiHub"], state: ServerState) -> None:
    """Broadcast the aggregated history index to all UI clients (best effort)."""
    if hub is None or hub.client_count == 0:
        return
    sessions = await state.get_history_index()
    await hub.broadcast({"type": "history_index", "sessions": sessions})


async def _push_history_data(
    hub: Optional["UiHub"],
    flow_id: str,
    mode: str,
    records: list,
) -> None:
    """Broadcast a history-data delta for *flow_id* to all UI clients."""
    if hub is None or hub.client_count == 0:
        return
    await hub.broadcast(
        {"type": "history_data", "flow_id": flow_id, "mode": mode, "records": records}
    )


async def handle_ui_connection(
    websocket: Any, hub: "UiHub", state: ServerState
) -> None:
    """Serve one web-frontend WebSocket connection.

    Accepts the socket, sends an initial full ``snapshot``, registers the
    client with the hub, then idles reading frames purely so a client
    disconnect is detected promptly. Frontend clients are not expected to send
    anything meaningful; any frame they do send is ignored.
    """
    await websocket.accept()
    await hub.register(websocket)
    try:
        machines = await state.get_machines_full()
        await websocket.send_text(
            __import__("json").dumps(
                {"type": "snapshot", "machines": machines},
                ensure_ascii=False,
                default=str,
            )
        )
        while True:
            # The frontend only listens; reading drives disconnect detection.
            await websocket.receive_text()
    except Exception:  # WebSocketDisconnect and friends
        logger.debug("UI connection ended", exc_info=True)
    finally:
        await hub.unregister(websocket)


async def handle_daemon_connection(
    websocket: Any,
    manager: ConnectionManager,
    state: ServerState,
    hub: Optional["UiHub"] = None,
    registry: Optional["HistoryRequestRegistry"] = None,
    index_registry: Optional["IndexRefreshRegistry"] = None,
) -> None:
    """Serve one daemon WebSocket connection end to end.

    Accepts the socket, validates the opening ``HELLO``, registers the machine,
    answers with ``WELCOME``, then runs the receive + heartbeat loops until the
    daemon disconnects or the heartbeat times out. Connection and state changes
    are mirrored to web-frontend clients via *hub*. Inbound ``MSG_HISTORY_DATA``
    frames resolve any on-demand pull parked in *registry*; inbound
    ``MSG_HISTORY_INDEX`` frames resolve any index-refresh parked in
    *index_registry*.
    """
    await websocket.accept()
    machine_id: Optional[str] = None
    try:
        # The first frame MUST be a HELLO identifying the machine.
        hello_raw = await websocket.receive_text()
        try:
            hello = protocol.decode(hello_raw)
        except protocol.ProtocolError as exc:
            logger.warning("Rejecting connection: bad HELLO frame (%s)", exc)
            await websocket.send_text(
                protocol.make_welcome(SERVER_VERSION, accepted=False, reason=str(exc)).to_json()
            )
            await websocket.close()
            return
        if hello.type != protocol.MSG_HELLO:
            reason = f"expected HELLO, got {hello.type}"
            logger.warning("Rejecting connection: %s", reason)
            await websocket.send_text(
                protocol.make_welcome(SERVER_VERSION, accepted=False, reason=reason).to_json()
            )
            await websocket.close()
            return

        machine_id = str(hello.payload.get("machine_id") or "").strip()
        if not machine_id:
            await websocket.send_text(
                protocol.make_welcome(
                    SERVER_VERSION, accepted=False, reason="missing machine_id"
                ).to_json()
            )
            await websocket.close()
            return

        hostname = str(hello.payload.get("hostname") or "")
        se3_version = str(hello.payload.get("se3_version") or "")
        await state.register_machine(machine_id, hostname, se3_version)
        await manager.connect(machine_id, websocket)
        await websocket.send_text(protocol.make_welcome(SERVER_VERSION).to_json())
        await _push_state(hub, state, "status_update")

        await _serve_loop(
            websocket, manager, state, machine_id, hub, registry, index_registry
        )
    except Exception:  # WebSocketDisconnect and friends
        logger.debug("Daemon connection ended", exc_info=True)
    finally:
        if machine_id is not None:
            await manager.disconnect(machine_id, websocket)
            await state.mark_offline(machine_id)
            await _push_state(hub, state, "status_update")


async def _serve_loop(
    websocket: Any,
    manager: ConnectionManager,
    state: ServerState,
    machine_id: str,
    hub: Optional["UiHub"] = None,
    registry: Optional["HistoryRequestRegistry"] = None,
    index_registry: Optional["IndexRefreshRegistry"] = None,
) -> None:
    """Run the receive loop alongside a heartbeat loop; stop when either ends."""
    last_seen = {"ts": time.time()}

    async def receive() -> None:
        while True:
            raw = await websocket.receive_text()
            last_seen["ts"] = time.time()
            try:
                message = protocol.decode(raw)
            except protocol.ProtocolError as exc:
                logger.warning("Dropping malformed frame from %s: %s", machine_id, exc)
                continue
            await _handle_message(
                message, state, machine_id, hub, registry, index_registry
            )

    async def heartbeat() -> None:
        seq = 0
        while True:
            await asyncio.sleep(PING_INTERVAL)
            if time.time() - last_seen["ts"] > HEARTBEAT_TIMEOUT:
                logger.warning("Heartbeat timeout for %s; closing", machine_id)
                try:
                    await websocket.close()
                except Exception:  # pragma: no cover - best effort
                    pass
                return
            seq += 1
            try:
                await websocket.send_text(protocol.make_ping(seq=seq).to_json())
            except Exception:
                return

    recv_task = asyncio.create_task(receive())
    beat_task = asyncio.create_task(heartbeat())
    done, pending = await asyncio.wait(
        {recv_task, beat_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    for task in (*done, *pending):
        # Retrieve results/exceptions so a finished receive() task does not
        # leave an unhandled WebSocketDisconnect dangling.
        try:
            await task
        except (asyncio.CancelledError, Exception):  # pragma: no cover - defensive
            pass


async def _handle_message(
    message: protocol.Message,
    state: ServerState,
    machine_id: str,
    hub: Optional["UiHub"] = None,
    registry: Optional["HistoryRequestRegistry"] = None,
    index_registry: Optional["IndexRefreshRegistry"] = None,
) -> None:
    """Apply one inbound daemon message to the server state."""
    if message.type == protocol.MSG_STATUS_UPDATE:
        snapshot = message.payload.get("snapshot") or {}
        if isinstance(snapshot, dict):
            await state.update_status(machine_id, snapshot)
            await _push_state(hub, state, "status_update")
    elif message.type == protocol.MSG_PONG:
        await state.touch(machine_id)
    elif message.type == protocol.MSG_CALL_NOTIFICATION:
        # A pending call is also surfaced by the next STATUS_UPDATE; the
        # notification just refreshes liveness so the UI reacts promptly.
        await state.touch(machine_id)
        logger.info("Call notification from %s: %s", machine_id, message.payload.get("call"))
    elif message.type == protocol.MSG_HISTORY_INDEX:
        # The daemon reports the complete index of history sessions it can
        # serve; cache it and let UI clients refresh their history list.
        sessions = message.payload.get("sessions") or []
        if isinstance(sessions, list):
            await state.update_history_index(machine_id, sessions)
            await _push_history_index(hub, state)
        # Resolve any GET /api/history refresh waiting on this machine's
        # re-push (after the cache has been updated above), even when the
        # session list was malformed — the daemon answered.
        if index_registry is not None:
            index_registry.resolve(machine_id)
    elif message.type == protocol.MSG_HISTORY_DATA:
        # History records — either an on-demand pull's reply or an active
        # flow's incremental append. Cache them, resolve any waiting REST
        # handler, and stream the delta to UI clients.
        flow_id = str(message.payload.get("flow_id") or "")
        mode = str(message.payload.get("mode") or "")
        records = message.payload.get("records") or []
        cursor = message.payload.get("cursor") or {}
        if flow_id and isinstance(records, list):
            await state.append_history(
                flow_id,
                mode,
                records,
                cursor=cursor if isinstance(cursor, dict) else {},
                machine_id=machine_id,
            )
            if registry is not None:
                registry.resolve(flow_id, await state.get_history(flow_id))
            await _push_history_data(hub, flow_id, mode, records)
    else:  # pragma: no cover - decode() restricts to known daemon->server types
        logger.debug("Ignoring unexpected daemon message type %s", message.type)
