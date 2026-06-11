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
from typing import TYPE_CHECKING, Any, Dict, Optional

from se3.daemon import protocol

from .state import ServerState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .identity import IdentityService

#: WS event type pushed to ``/ws/ui`` clients when an interjection chip's
#: lifecycle phase changes. Older frontends that do not recognise this type
#: simply ignore it (the standard "unknown ``type`` -> no-op" rule for the
#: ``/ws/ui`` channel), so introducing the event is backward-compatible.
UI_EVENT_INTERJECTION = "interjection_event"

#: Lifecycle phases emitted on :data:`UI_EVENT_INTERJECTION`. ``pending`` is
#: the moment the interjection call file appears in ``se3/calls/`` (the
#: server saw a brand-new interjection-kind ``call_id`` in a flow's
#: ``pending_calls`` snapshot); ``consumed`` is the moment that ``call_id``
#: disappears (the running ``se3 run`` drained the file).
INTERJECTION_PHASE_PENDING = "pending"
INTERJECTION_PHASE_CONSUMED = "consumed"

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


class IssueCommandRegistry:
    """Tracks in-flight issue write commands awaiting a daemon result.

    A REST handler that dispatches an issue write (create / edit / close /
    reopen) parks an :class:`asyncio.Future` here keyed by ``request_id``.
    When the daemon replies with :data:`protocol.MSG_ISSUE_RESULT` the
    matching waiter is resolved.  Lives entirely in process memory.
    """

    def __init__(self) -> None:
        self._waiters: Dict[str, list] = {}

    def register(self, request_id: str) -> "asyncio.Future":
        """Park and return a future that resolves when *request_id* lands."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(request_id, []).append(fut)
        return fut

    def resolve(self, request_id: str, data: Any) -> None:
        """Resolve every waiter parked for *request_id* with *data*."""
        for fut in self._waiters.pop(request_id, []):
            if not fut.done():
                fut.set_result(data)

    def discard(self, request_id: str, fut: "asyncio.Future") -> None:
        """Drop a single waiter (e.g. after a timeout) without resolving it."""
        waiters = self._waiters.get(request_id)
        if not waiters:
            return
        if fut in waiters:
            waiters.remove(fut)
        if not waiters:
            self._waiters.pop(request_id, None)


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


class InterjectionEventTracker:
    """Per-machine differ that emits ``interjection_event`` lifecycle events.

    Holds the last-known set of interjection-kind ``call_id`` values seen in
    each ``(machine_id, flow_id)`` slot of the most recent STATUS_UPDATE.
    On every fresh snapshot we compute the diff against that set:

    * call_ids present **now** but absent before fire :data:`INTERJECTION_PHASE_PENDING`;
    * call_ids present **before** but absent now fire :data:`INTERJECTION_PHASE_CONSUMED`.

    Each ``(machine_id, flow_id, call_id, phase)`` tuple is emitted **exactly
    once** — once a chip flips into ``pending`` it cannot fire ``pending``
    again without first being consumed, and a chip's ``consumed`` event
    cannot recur for a call_id the daemon never resurrects (drained call
    files are unlinked, never re-created with the same id). Per-machine
    bookkeeping makes a reconnecting daemon's first post-reconnect snapshot
    correctly emit ``pending`` events for whatever interjections are still
    on disk, instead of silently treating them as already-known.

    Held purely in process memory; nothing here ever hits disk.
    """

    def __init__(self) -> None:
        # machine_id -> flow_id -> {call_id: prompt_text}
        # The prompt is kept so the consumed event can carry a short label
        # back to the UI ("interjection 'fix the typo' was just consumed")
        # without the UI having to remember it.
        self._seen: Dict[str, Dict[str, Dict[str, str]]] = {}

    def reset_machine(self, machine_id: str) -> None:
        """Forget *machine_id*'s tracked state (e.g. on disconnect)."""
        self._seen.pop(machine_id, None)

    def diff_machine(
        self, machine_id: str, snapshot: Dict[str, Any]
    ) -> list:
        """Return the new :data:`UI_EVENT_INTERJECTION` events to broadcast.

        *snapshot* is the daemon's per-machine status dict (the same one
        :meth:`ServerState.update_status` receives). The diff is computed
        against the tracker's previous state for *machine_id* and the
        previous state is then replaced atomically — so two concurrent
        snapshots arriving back-to-back produce disjoint event sets and no
        duplicates.
        """
        prev = self._seen.get(machine_id, {})
        current: Dict[str, Dict[str, str]] = {}
        events: list = []
        now = time.time()
        flows = snapshot.get("flows") if isinstance(snapshot, dict) else None
        for flow_raw in flows or []:
            if not isinstance(flow_raw, dict):
                continue
            flow_id = flow_raw.get("flow_id")
            if not flow_id:
                continue
            flow_id_str = str(flow_id)
            chips: Dict[str, str] = {}
            for call in flow_raw.get("pending_calls") or []:
                if not isinstance(call, dict):
                    continue
                if call.get("kind") != protocol.CALL_KIND_INTERJECTION:
                    continue
                call_id = call.get("call_id")
                if not call_id:
                    continue
                prompt = call.get("prompt")
                chips[str(call_id)] = str(prompt) if isinstance(prompt, str) else ""
            current[flow_id_str] = chips
            prev_chips = prev.get(flow_id_str, {})
            for call_id, prompt in chips.items():
                if call_id in prev_chips:
                    continue
                events.append(
                    _make_interjection_event(
                        machine_id,
                        flow_id_str,
                        call_id,
                        INTERJECTION_PHASE_PENDING,
                        text=prompt,
                        ts=now,
                    )
                )
            for call_id, prompt in prev_chips.items():
                if call_id in chips:
                    continue
                events.append(
                    _make_interjection_event(
                        machine_id,
                        flow_id_str,
                        call_id,
                        INTERJECTION_PHASE_CONSUMED,
                        text=prompt,
                        ts=now,
                    )
                )
        # Any flow that vanished from the snapshot drops every chip it owned —
        # those drops are consumed events too, so the UI cleans up stale chips
        # even if a flow object disappears mid-life (e.g. on a fast-archived
        # flow that finished between two ticks).
        for flow_id_str, prev_chips in prev.items():
            if flow_id_str in current:
                continue
            for call_id, prompt in prev_chips.items():
                events.append(
                    _make_interjection_event(
                        machine_id,
                        flow_id_str,
                        call_id,
                        INTERJECTION_PHASE_CONSUMED,
                        text=prompt,
                        ts=now,
                    )
                )
        self._seen[machine_id] = current
        return events


def _make_interjection_event(
    machine_id: str,
    flow_id: str,
    call_id: str,
    phase: str,
    *,
    text: str = "",
    ts: float,
) -> Dict[str, Any]:
    """Build one :data:`UI_EVENT_INTERJECTION` payload (no I/O)."""
    payload: Dict[str, Any] = {
        "type": UI_EVENT_INTERJECTION,
        "machine_id": machine_id,
        "flow_id": flow_id,
        "call_id": call_id,
        "phase": phase,
        "ts": ts,
    }
    if text:
        payload["text"] = text
    return payload


class UiHub:
    """Owner-scoped fan-out hub for web-frontend WebSocket clients.

    The frontend (``static/app.js``) dials ``/ws/ui`` and expects a realtime
    push of the machine list whenever a daemon's state changes. This hub tracks
    those browser sockets and broadcasts to them — but, unlike a flat fan-out,
    each client is registered with the **owner** it authenticated as, and every
    push is filtered to that owner's trust domain so no client ever sees
    another owner's machines / flows / history.

    The owner mapping is ``websocket -> owner_id`` where ``owner_id`` may be
    ``None`` for the unscoped/admin view (an operator console, or a deployment
    that has not yet wired authentication): a ``None`` client receives the
    unfiltered stream, exactly as before multi-tenancy existed.
    """

    def __init__(self) -> None:
        # websocket -> owner_id (None == unscoped/admin view)
        self._clients: Dict[Any, Optional[str]] = {}
        self._lock = asyncio.Lock()

    async def register(self, websocket: Any, owner: Optional[str] = None) -> None:
        async with self._lock:
            self._clients[websocket] = owner
        logger.info("UI client connected (%d total)", len(self._clients))

    async def unregister(self, websocket: Any) -> None:
        async with self._lock:
            self._clients.pop(websocket, None)
        logger.info("UI client disconnected (%d total)", len(self._clients))

    @property
    def client_count(self) -> int:
        """Number of currently-connected frontend clients."""
        return len(self._clients)

    def distinct_owners(self) -> set:
        """The set of distinct owners currently connected (may include ``None``).

        The owner-scoped push helpers use this to compute one filtered payload
        per owner rather than per client, since clients of the same owner all
        receive identical frames.
        """
        return set(self._clients.values())

    async def broadcast(self, payload: Dict[str, Any]) -> None:
        """Send *payload* to every UI client, unfiltered.

        Retained for owner-agnostic frames (none currently); owner-sensitive
        pushes use :meth:`broadcast_scoped` / :meth:`broadcast_owned` instead.
        """
        await self._send_map({owner: payload for owner in self.distinct_owners()})

    async def broadcast_scoped(
        self, payload_by_owner: Dict[Optional[str], Optional[Dict[str, Any]]]
    ) -> None:
        """Send each owner the payload precomputed for it.

        *payload_by_owner* maps an owner id (or ``None`` for the admin view) to
        the frame that owner should receive. A ``None`` value (or a missing
        key) means "send nothing to that owner".
        """
        await self._send_map(payload_by_owner)

    async def broadcast_owned(
        self, payload: Dict[str, Any], owner: Optional[str]
    ) -> None:
        """Send *payload* only to clients allowed to see *owner*'s data.

        A client sees the frame when it is the unscoped/admin view
        (``client_owner is None``) or when its owner equals *owner* (the trust
        domain the data belongs to). Data with no owner (``owner is None``,
        e.g. an unbound machine) is therefore visible only to the admin view —
        fail-closed for every scoped client.
        """
        async with self._lock:
            clients = list(self._clients.items())
        await self._fan_out(
            [
                (ws, payload)
                for ws, client_owner in clients
                if client_owner is None or client_owner == owner
            ]
        )

    async def _send_map(
        self, payload_by_owner: Dict[Optional[str], Optional[Dict[str, Any]]]
    ) -> None:
        async with self._lock:
            clients = list(self._clients.items())
        targets = []
        for ws, owner in clients:
            payload = payload_by_owner.get(owner)
            if payload is None:
                continue
            targets.append((ws, payload))
        await self._fan_out(targets)

    async def _fan_out(self, targets: list) -> None:
        if not targets:
            return
        import json

        dead = []
        for client, payload in targets:
            try:
                await client.send_text(
                    json.dumps(payload, ensure_ascii=False, default=str)
                )
            except Exception:  # pragma: no cover - best effort
                dead.append(client)
        if dead:
            async with self._lock:
                for client in dead:
                    self._clients.pop(client, None)


async def _push_state(hub: Optional["UiHub"], state: ServerState, kind: str) -> None:
    """Broadcast the machine list to UI clients, scoped per owner (best effort).

    Each distinct connected owner gets a list filtered to its own machines;
    the unscoped/admin view (``owner is None``) gets the full list.
    """
    if hub is None or hub.client_count == 0:
        return
    payload_by_owner: Dict[Optional[str], Optional[Dict[str, Any]]] = {}
    for owner in hub.distinct_owners():
        machines = await state.get_machines_full(owner=owner)
        payload_by_owner[owner] = {"type": kind, "machines": machines}
    await hub.broadcast_scoped(payload_by_owner)


async def _push_history_index(hub: Optional["UiHub"], state: ServerState) -> None:
    """Broadcast the history index to UI clients, scoped per owner (best effort)."""
    if hub is None or hub.client_count == 0:
        return
    payload_by_owner: Dict[Optional[str], Optional[Dict[str, Any]]] = {}
    for owner in hub.distinct_owners():
        sessions = await state.get_history_index(owner=owner)
        payload_by_owner[owner] = {"type": "history_index", "sessions": sessions}
    await hub.broadcast_scoped(payload_by_owner)


async def _push_history_data(
    hub: Optional["UiHub"],
    state: ServerState,
    machine_id: str,
    flow_id: str,
    mode: str,
    records: list,
) -> None:
    """Broadcast a history-data delta for *flow_id* to its owner's UI clients.

    History records originate from a specific daemon (*machine_id*), so the
    delta is visible only to that machine's owner (plus the admin view) — never
    to another owner's console.
    """
    if hub is None or hub.client_count == 0:
        return
    owner = await state.get_machine_owner(machine_id)
    await hub.broadcast_owned(
        {"type": "history_data", "flow_id": flow_id, "mode": mode, "records": records},
        owner,
    )


async def handle_ui_connection(
    websocket: Any,
    hub: "UiHub",
    state: ServerState,
    *,
    owner: Optional[str] = None,
    require_owner: bool = False,
) -> None:
    """Serve one web-frontend WebSocket connection, scoped to *owner*.

    Accepts the socket, sends an initial ``snapshot`` filtered to *owner*'s
    machines, registers the client with the hub under that owner, then idles
    reading frames purely so a client disconnect is detected promptly. Frontend
    clients are not expected to send anything meaningful; any frame they do
    send is ignored.

    *owner* is the trust domain the connecting human authenticated as (resolved
    by the auth layer before this coroutine runs). ``None`` is the
    unscoped/admin view. When *require_owner* is true a ``None`` owner is
    rejected fail-closed — the socket is accepted and immediately closed so an
    unauthenticated UI connection never receives any machine data.
    """
    if require_owner and owner is None:
        await websocket.accept()
        await websocket.close(code=4401)
        return
    await websocket.accept()
    await hub.register(websocket, owner)
    try:
        machines = await state.get_machines_full(owner=owner)
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
    interjection_tracker: Optional["InterjectionEventTracker"] = None,
    identity: Optional["IdentityService"] = None,
    issue_registry: Optional["IssueCommandRegistry"] = None,
) -> None:
    """Serve one daemon WebSocket connection end to end.

    Accepts the socket, validates the opening ``HELLO``, registers the machine,
    answers with ``WELCOME``, then runs the receive + heartbeat loops until the
    daemon disconnects or the heartbeat times out. Connection and state changes
    are mirrored to web-frontend clients via *hub*. Inbound ``MSG_HISTORY_DATA``
    frames resolve any on-demand pull parked in *registry*; inbound
    ``MSG_HISTORY_INDEX`` frames resolve any index-refresh parked in
    *index_registry*.

    When an *identity* service is supplied the HELLO is authenticated:
    the daemon's ``key`` is resolved to an internal ``owner_id`` and the machine
    is bound to that trust domain (its :class:`MachineRecord.owner_id`). A
    missing or invalid key is rejected fail-closed — the server answers
    ``WELCOME(accepted=false)`` and closes without entering the receive loop, so
    an unauthenticated daemon contributes nothing to any owner's view. When
    *identity* is ``None`` the channel is unauthenticated (the pre-multi-tenant
    behaviour / a deployment that has not yet wired auth): the machine is
    registered with no owner.
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

        # Authenticate the daemon's key → owner_id when an identity service is
        # wired. The key is a secret credential and is NEVER logged (only the
        # accept/reject outcome and the resolved owner are).
        owner_id: Optional[str] = None
        if identity is not None:
            key = str(hello.payload.get("key") or "")
            owner_id = identity.resolve_owner_for_key(key)
            if owner_id is None:
                logger.warning(
                    "Rejecting daemon %s: unauthorized or missing daemon key",
                    machine_id,
                )
                await websocket.send_text(
                    protocol.make_welcome(
                        SERVER_VERSION,
                        accepted=False,
                        reason="unauthorized daemon key",
                    ).to_json()
                )
                await websocket.close()
                return
            identity.bind_machine(machine_id, owner_id)

        await state.register_machine(
            machine_id, hostname, se3_version, owner_id=owner_id
        )
        await manager.connect(machine_id, websocket)
        await websocket.send_text(protocol.make_welcome(SERVER_VERSION).to_json())
        await _push_state(hub, state, "status_update")

        await _serve_loop(
            websocket,
            manager,
            state,
            machine_id,
            hub,
            registry,
            index_registry,
            interjection_tracker,
            issue_registry,
        )
    except Exception:  # WebSocketDisconnect and friends
        logger.debug("Daemon connection ended", exc_info=True)
    finally:
        if machine_id is not None:
            await manager.disconnect(machine_id, websocket)
            await state.mark_offline(machine_id)
            await _push_state(hub, state, "status_update")
            if interjection_tracker is not None:
                # A reconnecting daemon's first STATUS_UPDATE should be
                # treated as a brand-new state — drop the per-machine
                # bookkeeping so genuinely-still-pending interjections re-emit
                # ``pending`` events instead of being silently skipped.
                interjection_tracker.reset_machine(machine_id)


async def _serve_loop(
    websocket: Any,
    manager: ConnectionManager,
    state: ServerState,
    machine_id: str,
    hub: Optional["UiHub"] = None,
    registry: Optional["HistoryRequestRegistry"] = None,
    index_registry: Optional["IndexRefreshRegistry"] = None,
    interjection_tracker: Optional["InterjectionEventTracker"] = None,
    issue_registry: Optional["IssueCommandRegistry"] = None,
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
                message,
                state,
                machine_id,
                hub,
                registry,
                index_registry,
                interjection_tracker,
                issue_registry,
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
    interjection_tracker: Optional["InterjectionEventTracker"] = None,
    issue_registry: Optional["IssueCommandRegistry"] = None,
) -> None:
    """Apply one inbound daemon message to the server state."""
    if message.type == protocol.MSG_STATUS_UPDATE:
        snapshot = message.payload.get("snapshot") or {}
        if isinstance(snapshot, dict):
            await state.update_status(machine_id, snapshot)
            await _push_state(hub, state, "status_update")
            # After the cache has been refreshed and the broadcast has fired,
            # compute the interjection-kind chip diff and emit one
            # lightweight ``interjection_event`` per phase transition. Older
            # frontends ignore the unknown ``type`` so this is fully
            # backward-compatible.
            if interjection_tracker is not None and hub is not None:
                events = interjection_tracker.diff_machine(machine_id, snapshot)
                if events:
                    # Interjection chips belong to this machine's owner, so the
                    # lifecycle events are scoped to that owner (plus the admin
                    # view) rather than fanned out to every console.
                    owner = await state.get_machine_owner(machine_id)
                    for event in events:
                        await hub.broadcast_owned(event, owner)
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
            await _push_history_data(hub, state, machine_id, flow_id, mode, records)
    elif message.type == protocol.MSG_ISSUE_RESULT:
        # Daemon acknowledges an issue write command. Resolve the parked
        # future so the originating REST endpoint can return the outcome.
        request_id = str(message.payload.get("request_id") or "")
        if request_id and issue_registry is not None:
            issue_registry.resolve(request_id, message.payload)
    else:  # pragma: no cover - decode() restricts to known daemon->server types
        logger.debug("Ignoring unexpected daemon message type %s", message.type)
