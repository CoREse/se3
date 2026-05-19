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

#: Type of the snapshot provider — returns a JSON-serializable machine snapshot.
SnapshotProvider = Callable[[], Dict[str, Any]]
#: Type of the spawn handler — called with
#: (task_description, project_root, task_type, discover).
SpawnHandler = Callable[[str, str, str, bool], Any]
#: Type of the respond handler — called with (call_id, project_root, response).
RespondHandler = Callable[[str, str, Any], Any]


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
        respond_handler: Optional[RespondHandler] = None,
        status_interval: float = _STATUS_INTERVAL,
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
            respond_handler: Callable invoked for an incoming RESPOND_CALL;
                when ``None`` the client writes the response file itself.
            status_interval: Seconds between STATUS_UPDATE pushes.
        """
        self.server_url = _normalize_ws_url(server_url)
        self.machine_id = machine_id
        self.hostname = hostname
        self.se3_version = se3_version
        self._snapshot_provider = snapshot_provider
        self._spawn_handler = spawn_handler
        self._respond_handler = respond_handler or _default_respond_handler
        self.status_interval = max(0.5, float(status_interval))

        self._seq = 0
        self._connected = False
        self._last_error: Optional[str] = None

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
            # Announce ourselves, then push a full snapshot immediately so a
            # freshly (re)connected server has state before the first tick.
            await self._send(ws, protocol.make_hello(self.machine_id, self.hostname, self.se3_version))
            await self._push_status(ws)
            logger.info("Connected to central server; HELLO sent")

            recv_task = asyncio.create_task(self._receive_loop(ws, stop_event))
            push_task = asyncio.create_task(self._status_loop(ws, stop_event))
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

    async def _status_loop(self, ws: Any, stop_event: asyncio.Event) -> None:
        """Push a STATUS_UPDATE every ``status_interval`` seconds."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.status_interval)
            except asyncio.TimeoutError:
                pass
            else:
                break
            await self._push_status(ws)

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
        else:  # pragma: no cover - defensive; decode() already validates
            logger.debug("Ignoring unexpected server message type %s", message.type)

    def _handle_spawn(self, payload: Dict[str, Any]) -> None:
        """Route a SPAWN_FLOW instruction to the daemon's spawner."""
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
