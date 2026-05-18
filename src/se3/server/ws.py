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


async def handle_daemon_connection(
    websocket: Any, manager: ConnectionManager, state: ServerState
) -> None:
    """Serve one daemon WebSocket connection end to end.

    Accepts the socket, validates the opening ``HELLO``, registers the machine,
    answers with ``WELCOME``, then runs the receive + heartbeat loops until the
    daemon disconnects or the heartbeat times out.
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

        await _serve_loop(websocket, manager, state, machine_id)
    except Exception:  # WebSocketDisconnect and friends
        logger.debug("Daemon connection ended", exc_info=True)
    finally:
        if machine_id is not None:
            await manager.disconnect(machine_id, websocket)
            await state.mark_offline(machine_id)


async def _serve_loop(
    websocket: Any,
    manager: ConnectionManager,
    state: ServerState,
    machine_id: str,
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
            await _handle_message(message, state, machine_id)

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
    message: protocol.Message, state: ServerState, machine_id: str
) -> None:
    """Apply one inbound daemon message to the server state."""
    if message.type == protocol.MSG_STATUS_UPDATE:
        snapshot = message.payload.get("snapshot") or {}
        if isinstance(snapshot, dict):
            await state.update_status(machine_id, snapshot)
    elif message.type == protocol.MSG_PONG:
        await state.touch(machine_id)
    elif message.type == protocol.MSG_CALL_NOTIFICATION:
        # A pending call is also surfaced by the next STATUS_UPDATE; the
        # notification just refreshes liveness so the UI reacts promptly.
        await state.touch(machine_id)
        logger.info("Call notification from %s: %s", machine_id, message.payload.get("call"))
    else:  # pragma: no cover - decode() restricts to known daemon->server types
        logger.debug("Ignoring unexpected daemon message type %s", message.type)
