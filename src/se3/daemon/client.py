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
from typing import Any, Awaitable, Callable, Dict, Optional, Set

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
#: (task_description, project_root, task_type, discover) for a fresh spawn, and
#: with an extra (… , from_issue_id) 5th positional when the SPAWN_FLOW carries
#: a non-empty ``from_issue_id``. The 5th argument is passed only on the
#: from-issue path so legacy 4-argument handlers keep working unchanged.
SpawnHandler = Callable[..., Any]
#: Type of the project-init handler — called with ``project_root`` before
#: SPAWN_FLOW is routed to the spawn handler. Returns an object whose
#: truthy ``.error`` attribute aborts the spawn; ``None`` means "skip the
#: pre-spawn check".
EnsureHandler = Callable[[str], Any]
#: Type of the resume handler — called with (flow_id, project_root) when a
#: SPAWN_FLOW carries a ``resume_flow_id``.
ResumeHandler = Callable[[str, str], Any]
#: Type of the respond handler — called with (call_id, project_root, response).
RespondHandler = Callable[[str, str, Any], Any]
#: Type of the history provider — a :class:`~se3.daemon.history.DaemonHistoryReader`
#: (or any object exposing ``build_index`` / ``read_flow`` / ``read_active_flows``).
HistoryProvider = Any
#: Type of the pending-calls signature provider — zero-arg callable returning a
#: cheap stat-based fingerprint dict of every ``se3/calls/`` file under every
#: tracked project root. Used by the push loop to fast-push a STATUS_UPDATE the
#: moment a call file appears or disappears (e.g. when an interjection file is
#: written or drained), without waiting for the steady 5 s status tick.
CallsSignatureProvider = Callable[[], Dict[str, Any]]


def _format_exc(exc: BaseException) -> str:
    """Return a non-empty, human-readable one-line description of *exc*.

    Several connection failures stringify to an empty string — most notably
    :class:`asyncio.TimeoutError`, raised by ``websockets.connect(open_timeout=…)``
    when a ``wss://`` URL is dialed against the wrong port (the classic
    "TLS to :8080 instead of :443" misconfiguration). A bare ``str(exc)`` then
    left :attr:`DaemonClient.last_error` blank and ``se3 daemon status`` rendered
    ``Connection: not connected ()`` — an empty, uninformative reason. Falling
    back to the exception's type name guarantees the recorded reason is always
    non-empty and usable for diagnosis.

    The exception text never carries the daemon key (the credential only ever
    travels on the HELLO wire, not through any exception raised here), so the
    formatted reason is safe to record in the status file and to log.
    """
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


def _normalize_ws_url(server_url: str) -> str:
    """Return a ``ws(s)://host:port/ws`` URL from a user-supplied server URL.

    Accepts bare hosts (``host`` or ``host:8080``), ``http(s)://`` and
    ``ws(s)://`` URLs, and appends the ``/ws`` daemon endpoint path when none
    is present. When the host carries no explicit port, a *scheme-aware*
    default is filled in so the daemon and ``se3-server`` agree on the same
    default (a bare ``ws://host`` would otherwise fall back to the
    WebSocket-standard port 80):

    - ``ws://`` (and ``http://`` normalized to ``ws://``) → the plaintext
      :data:`~se3.daemon.protocol.DEFAULT_SERVER_PORT` (8080, the
      ``se3-server --port`` default).
    - ``wss://`` (and ``https://`` normalized to ``wss://``) →
      :data:`~se3.daemon.protocol.DEFAULT_SERVER_TLS_PORT` (443), because a
      TLS connection terminates at the reverse proxy's HTTPS port, not at
      se3-server's plaintext default.

    An explicit port in the URL is always preserved, including IPv6 literals
    (``[::1]``) and custom paths (``/daemon``, an already-supplied ``/ws``).
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
    # Fill in the default port when the host carries none. The default is
    # scheme-aware: ``wss`` terminates at the TLS port (443), ``ws`` at the
    # plaintext default (8080). An IPv6 literal is bracketed (``[::1]``), so
    # only a ``:`` *after* the closing bracket is a port separator; a bare host
    # has a port iff it contains a ``:``.
    if host:
        if host.startswith("["):
            has_port = "]:" in host
        else:
            has_port = ":" in host
        if not has_port:
            default_port = (
                protocol.DEFAULT_SERVER_TLS_PORT
                if scheme == "wss"
                else protocol.DEFAULT_SERVER_PORT
            )
            host = f"{host}:{default_port}"
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
        daemon_key: str = "",
        spawn_handler: Optional[SpawnHandler] = None,
        ensure_handler: Optional[EnsureHandler] = None,
        resume_handler: Optional[ResumeHandler] = None,
        respond_handler: Optional[RespondHandler] = None,
        history_provider: Optional[HistoryProvider] = None,
        calls_signature_provider: Optional[CallsSignatureProvider] = None,
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
            daemon_key: Optional daemon credential carried in HELLO. The
                multi-tenant server resolves it to an owner and binds this
                machine to that trust domain; an empty key means "no
                credential" (local / legacy single-tenant operation). The key
                is held only in memory and on the wire — it is never logged.
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
            calls_signature_provider: Zero-arg callable returning a cheap
                stat-based fingerprint dict of every ``se3/calls/`` file under
                every tracked project root. When provided, the push loop checks
                it on the fast tick and fires an immediate STATUS_UPDATE the
                moment the signature changes — so an interjection file being
                written or drained is reflected on the web within one fast tick
                instead of waiting a full ``status_interval``. When ``None``
                no extra polling happens and the client behaves as before.
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
        # Secret daemon credential carried in HELLO. Kept private and never
        # logged; only ``make_hello`` ever reads it.
        self._daemon_key = daemon_key or ""
        self._snapshot_provider = snapshot_provider
        self._spawn_handler = spawn_handler
        self._ensure_handler = ensure_handler
        self._resume_handler = resume_handler
        self._respond_handler = respond_handler or _default_respond_handler
        self._interject_handler = _default_interject_handler
        self._history_provider = history_provider
        self._calls_signature_provider = calls_signature_provider
        self.status_interval = max(0.5, float(status_interval))
        self.history_poll_interval = max(
            0.1, min(float(history_poll_interval), self.status_interval)
        )

        self._seq = 0
        self._connected = False
        self._last_error: Optional[str] = None
        # Set when the server answers HELLO with ``WELCOME(accepted=false)``.
        # The run loop checks it after each session and stops reconnecting
        # rather than hammering the server with a key it has already rejected
        # (fail-closed: an unauthenticated daemon degrades to local-only).
        self._auth_rejected = False
        # Bound to the running loop inside :meth:`_session`; set by the WELCOME
        # handler so the session unwinds immediately on a rejection instead of
        # waiting for the server to close the socket.
        self._auth_rejected_event: Optional[asyncio.Event] = None
        # History push state, reset on every (re)connection so a freshly
        # connected server always receives a fresh index and full snapshots.
        self._last_index: Optional[list] = None
        self._history_cursors: Dict[str, Dict[str, int]] = {}
        # Last active-flow disk signature seen by the push loop; an unchanged
        # signature means there is nothing new to push (debounce).
        self._last_history_signature: Dict[str, Any] = {}
        # Last ``se3/calls/`` file signature seen by the push loop. An unchanged
        # signature means no call file appeared or disappeared since the previous
        # tick (debounce); a change drives an immediate STATUS_UPDATE so the web
        # console sees pending / consumed interjection chips within ~1 s.
        self._last_calls_signature: Dict[str, Any] = {}
        # Event used to wake the push loop *immediately* (bypassing the fast
        # tick) when a server-delivered interjection has just hit disk, so the
        # pending chip appears in the web within ~1 s of the API call. Created
        # lazily inside :meth:`_session` because :class:`asyncio.Event` must
        # bind to a running event loop.
        self._fast_push_event: Optional[asyncio.Event] = None
        # Cache of the most recent snapshot's ``project_roots`` set, refreshed
        # on every successful :meth:`_push_status`. ``_handle_issue_command``
        # validates an incoming ``project_root`` against this cache so a webui
        # issue write no longer has to re-run the heavy snapshot provider (which
        # walks the whole ``se3/history`` tree) on the issue-command hot path —
        # the periodic STATUS_UPDATE loop already keeps it fresh. ``None`` means
        # "no snapshot built yet"; the issue handler then falls back to building
        # one snapshot to validate against.
        self._last_known_project_roots: Optional[set] = None

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
                # Format to a guaranteed non-empty, readable reason: a bare
                # str(exc) is empty for asyncio.TimeoutError (the open_timeout
                # fired — typically a wss:// URL dialed at the wrong port), which
                # is exactly the empty-parens root cause this fixes. The reason
                # never contains the daemon key, so it is safe to record/log.
                reason = _format_exc(exc)
                self._last_error = reason
                logger.warning(
                    "Server connection lost (%s); retrying in %.0fs", reason, backoff
                )
            finally:
                self._connected = False
            if stop_event.is_set():
                break
            if self._auth_rejected:
                # The server rejected our HELLO credential. Retrying would just
                # replay the same rejected key in a tight loop, so we stop the
                # reconnect storm and stay local-only. The reason is preserved
                # in ``last_error`` for ``se3 daemon status`` to surface.
                logger.error(
                    "Central server rejected this daemon's credential (%s); "
                    "not reconnecting. The daemon continues in local-only mode. "
                    "Re-issue / fix the daemon key and restart the daemon to retry.",
                    self._last_error or "no reason given",
                )
                return
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
        async with websockets.connect(
            self.server_url,
            open_timeout=10,
            max_size=protocol.MAX_WS_MESSAGE_BYTES,
        ) as ws:
            self._connected = True
            self._last_error = None
            # A new session: forget prior history state so the server gets a
            # fresh index and full active-flow snapshots after every reconnect.
            self._last_index = None
            self._history_cursors = {}
            self._last_history_signature = {}
            self._last_calls_signature = {}
            # Bind the fast-push event to *this* session's running loop; the
            # previous session's event (if any) is dropped along with it.
            self._fast_push_event = asyncio.Event()
            # Bind the auth-rejection event to this session's loop. A new
            # session optimistically assumes acceptance until a WELCOME says
            # otherwise.
            self._auth_rejected_event = asyncio.Event()
            # Announce ourselves (carrying the daemon credential, when set),
            # then push a full snapshot immediately so a freshly (re)connected
            # server has state before the first tick. The key is passed to
            # make_hello only — it is never written to a log line.
            await self._send(
                ws,
                protocol.make_hello(
                    self.machine_id,
                    self.hostname,
                    self.se3_version,
                    self._daemon_key,
                ),
            )
            await self._push_status(ws)
            await self._push_history(ws, force_index=True)
            # Prime the signature so the fast push loop only fires on the *next*
            # disk change rather than immediately re-pushing what we just sent.
            self._history_changed()
            self._calls_changed()
            logger.info("Connected to central server; HELLO sent")

            recv_task = asyncio.create_task(self._receive_loop(ws, stop_event))
            push_task = asyncio.create_task(self._push_loop(ws, stop_event))
            stop_task = asyncio.create_task(stop_event.wait())
            # A WELCOME(accepted=false) sets this event so the session unwinds
            # at once rather than idling until the server closes the socket.
            abort_task = asyncio.create_task(self._auth_rejected_event.wait())
            done, pending = await asyncio.wait(
                {recv_task, push_task, stop_task, abort_task},
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

        The loop wakes on the fast ``history_poll_interval`` cadence, on the
        ``_fast_push_event`` (set the moment a server-delivered interjection
        hits disk), or on shutdown — whichever fires first. A STATUS_UPDATE is
        sent on the steady ``status_interval`` heartbeat AND whenever the
        ``se3/calls/`` directory signature changed since the previous tick (so
        a freshly-written interjection or a freshly-drained call surfaces on
        the web within one fast tick instead of waiting the full status
        interval). A HISTORY push fires whenever the active-flow disk
        signature changed since the last check — so a CLI step that advances
        ``engine.json`` / appends a jsonl line reaches the web within one fast
        tick — and also on every status tick as a backstop in case a change is
        ever missed. When nothing changed, the signature checks are cheap
        stat-only scans and no frame is sent (debounce).

        Both pushes share this one coroutine so their ``ws.send`` calls never
        interleave (which two independent loops racing on the same socket could
        do), keeping every wire frame intact.
        """
        last_status = time.monotonic()
        while not stop_event.is_set():
            woke_for_fast_push = await self._wait_next_tick(stop_event)
            if stop_event.is_set():
                break
            now = time.monotonic()
            status_due = (now - last_status) >= self.status_interval
            # A genuine call-file change drives an immediate STATUS_UPDATE so
            # the web sees the new / drained interjection chip within ~1 s.
            calls_changed = self._calls_changed()
            # ``woke_for_fast_push`` is set by ``_handle_interject`` the
            # instant a server-delivered interjection has hit disk — push now
            # rather than waiting for the next ``has_changes``-style scan to
            # notice it on the next tick.
            push_status = status_due or calls_changed or woke_for_fast_push
            if push_status:
                last_status = now
                await self._push_status(ws)
            # Push history on a real disk change, or on the status tick (backstop).
            history_changed = self._history_changed()
            if status_due or history_changed:
                # A real disk change (engine.json rewrite / jsonl append) means
                # the on-disk state diverged from the cached index.  Invalidate
                # so the next build_index() rebuilds from disk instead of
                # returning a stale snapshot for up to BUILD_INDEX_TTL.
                if history_changed:
                    invalidate = getattr(
                        self._history_provider, "invalidate_index_cache", None
                    )
                    if invalidate is not None:
                        invalidate()
                await self._push_history(ws)

    async def _wait_next_tick(self, stop_event: asyncio.Event) -> bool:
        """Wait one fast tick, returning whether the fast-push event woke us.

        Races the fast-tick timeout against the stop event AND the
        ``_fast_push_event`` so a server-delivered interjection can drive an
        immediate push instead of waiting up to ``history_poll_interval``.
        The fast-push event is cleared inside the locked window so a
        concurrent ``set`` between two ticks still wakes the *next* tick.
        Returns ``True`` only when the fast-push event actually triggered the
        wakeup, so the caller can force a STATUS_UPDATE on that tick.
        """
        event = self._fast_push_event
        if event is None:  # pragma: no cover - defensive (set in _session)
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.history_poll_interval
                )
            except asyncio.TimeoutError:
                pass
            return False
        stop_task = asyncio.create_task(stop_event.wait())
        push_task = asyncio.create_task(event.wait())
        try:
            done, pending = await asyncio.wait(
                {stop_task, push_task},
                timeout=self.history_poll_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (stop_task, push_task):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # pragma: no cover
                        pass
        woke_for_fast_push = push_task in done and event.is_set()
        if event.is_set():
            event.clear()
        return woke_for_fast_push

    def _calls_changed(self) -> bool:
        """Return whether the ``se3/calls/`` directory signature changed.

        Updates :attr:`_last_calls_signature` as a side effect. Returns
        ``False`` when no calls-signature provider is configured, so a client
        wired without one keeps its prior behavior. A signature lookup failure
        conservatively reports a change so the next push still runs.

        The provider's signature is intentionally kind-agnostic — it captures
        every file under ``se3/calls/`` so that both an interjection file
        landing on disk (new chip → ``pending``) and any call file disappearing
        from disk (drain → ``consumed``) flip the signature. The downstream
        diff that classifies the chip kind happens in the server's WS layer.
        """
        provider = self._calls_signature_provider
        if provider is None:
            return False
        try:
            signature = provider()
        except Exception:
            logger.debug(
                "calls_signature_provider failed; forcing a status push",
                exc_info=True,
            )
            return True
        changed = signature != self._last_calls_signature
        self._last_calls_signature = signature
        return changed

    def _trigger_fast_push(self) -> None:
        """Wake the push loop immediately for an out-of-band STATUS_UPDATE.

        Used when the client has just performed a side-effect that the web
        console needs to see *now* (e.g. writing an interjection call file
        from :meth:`_handle_interject`). The event is sticky until the push
        loop consumes it inside :meth:`_wait_next_tick`, so a fast-push set
        while the loop is already pushing is honored on the next tick rather
        than lost. A no-op when the event has not been bound to a running
        loop yet (between the constructor and the first ``_session``).
        """
        event = self._fast_push_event
        if event is not None:
            event.set()

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
            self._handle_welcome(message.payload)
        elif message.type == protocol.MSG_SPAWN_FLOW:
            await self._handle_spawn(ws, message.payload)
        elif message.type == protocol.MSG_RESPOND_CALL:
            self._handle_respond(message.payload)
        elif message.type == protocol.MSG_INTERJECT_FLOW:
            await self._handle_interject(message.payload)
        elif message.type == protocol.MSG_ISSUE_COMMAND:
            await self._handle_issue_command(ws, message.payload)
        elif message.type == protocol.MSG_HISTORY_REQUEST:
            await self._handle_history_request(ws, message.payload)
        elif message.type == protocol.MSG_HISTORY_INDEX_REQUEST:
            await self._handle_history_index_request(ws)
        else:  # pragma: no cover - defensive; decode() already validates
            logger.debug("Ignoring unexpected server message type %s", message.type)

    def _handle_welcome(self, payload: Dict[str, Any]) -> None:
        """Process the server's WELCOME, honouring an ``accepted=false`` reject.

        On acceptance this is just an informational log line. On rejection — the
        server could not resolve this daemon's credential to an owner — we
        record the reason in :attr:`last_error`, flag :attr:`_auth_rejected` so
        the run loop stops reconnecting, and signal the session to unwind now.
        The ``reason`` is server-supplied prose (e.g. ``"unknown daemon key"``)
        and never contains the key itself, so it is safe to log; we never echo
        the credential we sent.
        """
        accepted = bool(payload.get("accepted", True))
        if accepted:
            logger.info("Server WELCOME received (accepted=True)")
            return
        reason = str(payload.get("reason") or "").strip() or "server rejected HELLO"
        self._auth_rejected = True
        self._last_error = reason
        logger.warning("Server WELCOME received (accepted=False): %s", reason)
        event = self._auth_rejected_event
        if event is not None:
            event.set()

    async def _handle_spawn(self, ws: Any, payload: Dict[str, Any]) -> None:
        """Route a SPAWN_FLOW instruction to the daemon's spawner.

        When a ``resume_flow_id`` is present in the payload, the message is
        treated as a **resume** request rather than a fresh spawn.  The
        ``task_description`` is ignored — the flow's own persisted state
        supplies the task.  The resume path skips the ``ensure_handler``
        (the project must already be initialised for a flow to exist there).

        When an ``ensure_handler`` is configured (fresh-spawn path only), it
        runs first against the target ``project_root`` — that is what lets
        the web *New Task* form send a brand-new empty directory: the daemon
        auto-runs ``se3 init`` there and registers it before the spawn
        proceeds. A truthy ``.error`` on the returned object aborts the spawn
        and is logged; nothing half-initialized leaks downstream.

        A failure on any of the three execution paths (resume / project-init /
        fresh spawn) does **not** silently return: the real error is sent back
        to the server as a :data:`~se3.daemon.protocol.MSG_SPAWN_FAILED` so the
        web UI can surface it instead of leaving the task stuck on the
        "published" pseudo-success state. Pure *input-validation* drops (no
        handler configured, empty task) still log-and-return because nothing
        was ever genuinely launched.
        """
        project_root = str(payload.get("project_root") or "")
        from_issue_id = str(payload.get("from_issue_id") or "").strip()
        task = str(payload.get("task_description") or "").strip()
        resume_flow_id = str(payload.get("resume_flow_id") or "").strip()

        async def _report_failure(error: str) -> None:
            """Send a SPAWN_FAILED back to the server (best effort)."""
            try:
                await self._send(
                    ws,
                    protocol.make_spawn_failed(
                        project_root,
                        error,
                        task_description=task,
                        from_issue_id=from_issue_id,
                        resume_flow_id=resume_flow_id,
                    ),
                )
            except Exception:
                logger.debug("SPAWN_FAILED send failed", exc_info=True)

        # -- resume path (resume_flow_id present) --------------------------
        if resume_flow_id:
            if self._resume_handler is None:
                logger.warning(
                    "Received SPAWN_FLOW with resume_flow_id=%s "
                    "but no resume handler is configured",
                    resume_flow_id,
                )
                return
            try:
                self._resume_handler(resume_flow_id, project_root)
                logger.info(
                    "SPAWN_FLOW resume handled: flow %s", resume_flow_id
                )
            except Exception as exc:
                logger.exception("SPAWN_FLOW resume handler failed")
                await _report_failure(
                    f"resume failed: {exc or type(exc).__name__}"
                )
                return
            invalidate = getattr(
                self._history_provider, "invalidate_index_cache", None
            )
            if invalidate is not None:
                invalidate()
            return

        # -- fresh-spawn path (no resume_flow_id) --------------------------
        # A ``from_issue_id`` selects the from-issue spawn variant: the CLI
        # sources the task from the issue itself, so an empty task_description
        # is allowed here (it would be ignored on the argv anyway).
        if not task and not from_issue_id:
            logger.warning("Ignoring SPAWN_FLOW with empty task_description")
            return
        task_type = str(payload.get("task_type") or "feature")
        discover = bool(payload.get("discover", False))
        worktree = bool(payload.get("worktree", False))
        if self._spawn_handler is None:
            logger.warning("Received SPAWN_FLOW but no spawn handler is configured")
            return
        if self._ensure_handler is not None and project_root:
            try:
                ensure = self._ensure_handler(project_root)
            except Exception as exc:
                logger.exception(
                    "SPAWN_FLOW ensure-project handler failed for %s; aborting spawn",
                    project_root,
                )
                await _report_failure(
                    f"project init failed: {exc or type(exc).__name__}"
                )
                return
            error = getattr(ensure, "error", "") if ensure is not None else ""
            if error:
                logger.error(
                    "SPAWN_FLOW aborted: cannot initialize %s: %s",
                    project_root,
                    error,
                )
                await _report_failure(f"project init failed: {error}")
                return
        try:
            # The from_issue_id 5th positional and the worktree keyword are
            # passed only when present/true so legacy 4-argument spawn handlers
            # stay backward compatible (a non-isolated fresh spawn keeps the
            # exact 4-positional call shape).
            spawn_kwargs = {"worktree": True} if worktree else {}
            if from_issue_id:
                self._spawn_handler(
                    task, project_root, task_type, discover, from_issue_id,
                    **spawn_kwargs,
                )
                logger.info("SPAWN_FLOW handled from issue %s", from_issue_id)
            else:
                self._spawn_handler(
                    task, project_root, task_type, discover, **spawn_kwargs
                )
                logger.info("SPAWN_FLOW handled: %s", task[:80])
        except Exception as exc:
            logger.exception("SPAWN_FLOW handler failed")
            await _report_failure(f"spawn failed: {exc or type(exc).__name__}")
            return
        # The new flow's engine.json is now on disk.  Invalidate the history
        # index cache so the next _push_history call rebuilds from disk and
        # includes the freshly-spawned flow in both the index and the active-
        # flow reads, instead of waiting out the BUILD_INDEX_TTL window.
        invalidate = getattr(self._history_provider, "invalidate_index_cache", None)
        if invalidate is not None:
            invalidate()

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

    async def _handle_interject(self, payload: Dict[str, Any]) -> None:
        """Route an INTERJECT_FLOW instruction to the interjection-file writer."""
        text = str(payload.get("text") or "").strip()
        if not text:
            logger.warning("Ignoring INTERJECT_FLOW with empty text")
            return
        flow_id = str(payload.get("flow_id") or "")
        project_root = str(payload.get("project_root") or "").strip()
        if not project_root:
            project_root = await self._resolve_interject_root(flow_id)
        if not project_root:
            logger.warning(
                "INTERJECT_FLOW: cannot resolve project root for flow %s; dropping",
                flow_id,
            )
            return
        try:
            self._interject_handler(flow_id, project_root, text)
        except Exception:
            logger.exception("INTERJECT_FLOW handler failed")
            return
        # The interjection file is on disk: wake the push loop so the
        # web-side ``pending`` chip surfaces within ~1 s rather than waiting
        # for the next steady status tick. The aggregator picks the new file
        # up the moment it is asked for a snapshot, so we just need to
        # trigger that snapshot now.
        self._trigger_fast_push()
        logger.info("INTERJECT_FLOW handled for flow %s", flow_id)

    async def _resolve_interject_root(self, flow_id: str) -> str:
        """Resolve a flow's project root from the current machine snapshot.

        Used when an INTERJECT_FLOW payload carries no ``project_root`` — the
        daemon matches *flow_id* against its own snapshot. Returns an empty
        string when the flow cannot be located.

        The snapshot build is the same heavy disk walk offloaded in
        :meth:`_push_status`, so it is likewise run in a worker thread to keep
        the event loop free for heartbeats / SPAWN_FLOW / reconnects.
        """
        try:
            snapshot = await asyncio.to_thread(self._snapshot_provider)
        except Exception:
            logger.debug("Snapshot lookup for INTERJECT_FLOW failed", exc_info=True)
            return ""
        for flow in (snapshot or {}).get("flows") or []:
            if isinstance(flow, dict) and str(flow.get("flow_id") or "") == flow_id:
                return str(flow.get("project_root") or "").strip()
        return ""

    async def _handle_issue_command(self, ws: Any, payload: Dict[str, Any]) -> None:
        """Execute an issue write command via :class:`IssueManager`.

        Validates ``project_root`` (must be a registered, absolute path) and
        delegates to the appropriate ``IssueManager`` method.  Web-initiated
        creates are forced to ``source=human``.  On completion (success or
        failure) a :data:`protocol.MSG_ISSUE_RESULT` is sent back to the
        server so the REST caller receives actionable feedback.
        """
        operation = str(payload.get("operation") or "").strip()
        project_root = str(payload.get("project_root") or "").strip()
        request_id = str(payload.get("request_id") or "").strip()

        async def _reply(
            *, ok: bool, error: str = "", issue_id: str = "",
        ) -> None:
            """Send a result back if we have a request_id and a live ws."""
            if not request_id:
                return
            try:
                result_msg = protocol.make_issue_result(
                    request_id, ok=ok, error=error, issue_id=issue_id,
                )
                await self._send(ws, result_msg)
            except Exception:
                logger.debug(
                    "Failed to send ISSUE_RESULT for request %s",
                    request_id,
                    exc_info=True,
                )

        if not operation:
            logger.warning("Ignoring ISSUE_COMMAND with empty operation")
            await _reply(ok=False, error="empty operation")
            return
        if not project_root or not Path(project_root).is_absolute():
            logger.warning(
                "ISSUE_COMMAND: project_root must be an absolute path, got %r",
                project_root,
            )
            await _reply(ok=False, error="project_root must be an absolute path")
            return

        # The project must be registered with the aggregator (live or
        # persistent) so it is an actual SE3 project on this machine. Prefer the
        # cache refreshed by the periodic STATUS_UPDATE loop so the issue-command
        # hot path does not re-run the heavy snapshot provider (which walks the
        # whole se3/history tree) — that repeated heavyweight call is what
        # delayed the ack past the server's ISSUE_COMMAND_TIMEOUT. Only fall back
        # to building one snapshot when no cache exists yet (e.g. an issue
        # command arrives before the first STATUS_UPDATE).
        known_roots = self._last_known_project_roots
        if known_roots is None:
            try:
                snapshot = await asyncio.to_thread(self._snapshot_provider)
            except Exception:
                logger.debug(
                    "ISSUE_COMMAND: snapshot lookup failed", exc_info=True
                )
                await _reply(ok=False, error="snapshot lookup failed")
                return
            known_roots = set(snapshot.get("project_roots") or [])
            self._last_known_project_roots = known_roots
        resolved = str(Path(project_root).resolve())
        if resolved not in known_roots:
            logger.warning(
                "ISSUE_COMMAND: project_root %r is not a registered project; "
                "known roots: %s",
                project_root,
                sorted(known_roots)[:5],
            )
            await _reply(ok=False, error="project_root is not a registered project")
            return

        # All IssueManager operations are disk I/O — run in a thread.
        try:
            result = await asyncio.to_thread(
                self._execute_issue_operation, operation, resolved, payload
            )
        except Exception as exc:
            logger.exception(
                "ISSUE_COMMAND %s failed for project %s", operation, project_root
            )
            await _reply(ok=False, error=str(exc) or type(exc).__name__)
            return

        # Reply with the ack *before* triggering the fast push. The ack
        # (MSG_ISSUE_RESULT) is the frame the server blocks on within
        # ISSUE_COMMAND_TIMEOUT; the write has already succeeded, so send it
        # immediately and let the heavier fast-push (which only schedules a
        # follow-up STATUS_UPDATE) run afterwards. This keeps end-to-end ack
        # latency low and avoids the server falsely reporting a timeout for an
        # issue that already landed on disk.
        await _reply(ok=True, issue_id=str(result or ""))
        # The issue file changed on disk — trigger a fast push so the web
        # sees the update on the next tick.
        self._trigger_fast_push()
        logger.info(
            "ISSUE_COMMAND %s handled for project %s", operation, project_root
        )

    def _execute_issue_operation(
        self, operation: str, project_root: str, payload: Dict[str, Any]
    ) -> str:
        """Dispatch an issue operation to :class:`IssueManager`.

        Runs synchronously (called from a thread via ``asyncio.to_thread``).
        Returns the issue ID of the affected issue.
        """
        from pathlib import Path as _Path

        from ..engine.issue_manager import IssueManager

        mgr = IssueManager(_Path(project_root))

        if operation == "create":
            description = str(payload.get("description") or "").strip()
            if not description:
                raise ValueError("ISSUE_COMMAND create: description is required")
            title = str(payload.get("title") or "").strip() or None
            priority = str(payload.get("priority") or "").strip() or None
            issue_type = str(payload.get("type") or "").strip() or None
            tags = payload.get("tags")
            if not isinstance(tags, list):
                tags = None
            # Web-initiated creates are always source=human
            created = mgr.create(
                description=description,
                title=title,
                priority=priority,
                type=issue_type,
                tags=tags,
                source="human",
            )
            return created.id

        elif operation == "edit":
            issue_id = str(payload.get("issue_id") or "").strip()
            if not issue_id:
                raise ValueError("ISSUE_COMMAND edit: issue_id is required")
            kwargs: Dict[str, Any] = {}
            for field in ("title", "description", "priority", "type"):
                val = payload.get(field)
                if val is not None and isinstance(val, str):
                    kwargs[field] = val
            tags = payload.get("tags")
            if isinstance(tags, list):
                kwargs["tags"] = tags
            if not kwargs:
                raise ValueError("ISSUE_COMMAND edit: no fields to update")
            updated = mgr.update_fields(issue_id, **kwargs)
            return updated.id

        elif operation == "close":
            issue_id = str(payload.get("issue_id") or "").strip()
            if not issue_id:
                raise ValueError("ISSUE_COMMAND close: issue_id is required")
            reason = str(payload.get("reason") or "").strip()
            closed = mgr.close_issue(issue_id, reason=reason)
            return closed.id

        elif operation == "reopen":
            issue_id = str(payload.get("issue_id") or "").strip()
            if not issue_id:
                raise ValueError("ISSUE_COMMAND reopen: issue_id is required")
            reopened = mgr.reopen_issue(issue_id)
            return reopened.id

        else:
            raise ValueError(f"unknown issue operation: {operation!r}")

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
            # Bypass the build_index TTL cache so the re-push reflects
            # on-disk state rather than returning a (possibly stale)
            # cached snapshot.  Guard for compatibility with older reader
            # stubs that may not have the method.
            invalidate = getattr(self._history_provider, "invalidate_index_cache", None)
            if invalidate is not None:
                invalidate()
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
            # Building the snapshot walks ``se3/state`` and (via the aggregator's
            # all_project_roots → enumerate_historical_project_roots) the whole
            # ``se3/history`` tree, reading every ``_meta.json``. On a large
            # history this synchronous walk is heavy enough to stall the event
            # loop for seconds each tick, which makes the daemon miss heartbeats
            # (server marks it offline → machine greys out) and stops it from
            # consuming inbound SPAWN_FLOW or triggering a reconnect. Offload it
            # to a worker thread, the same pattern used for read_flow /
            # build_index / read_active_flows above.
            snapshot = await asyncio.to_thread(self._snapshot_provider)
        except Exception:
            logger.exception("Snapshot provider failed; skipping STATUS_UPDATE")
            return
        # Cache the snapshot's project_roots so the issue-command hot path can
        # validate project_root without re-running the heavy snapshot provider.
        self._last_known_project_roots = set(snapshot.get("project_roots") or [])
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
        # flushed one last time. Atomic engine.json writes mean an active flow is
        # never transiently missing, so pruning cannot accidentally trigger a
        # duplicate full re-read.
        new_cursors = {read.flow_id: read.cursor for read in reads}
        # Retain the cursor of a terminal flow that produced no records this
        # round but is still the live engine.json flow (e.g. a FAILED flow
        # awaiting `se3 run --resume`). Without this it would drop out the round
        # after its final flush; a later resume would then find no cursor, force
        # a full re-read, and the web console would stay frozen on the failure
        # snapshot instead of receiving incremental appends. Bounded by the live
        # engine.json flows (one per root), so a fully-drained *and* archived
        # terminal flow still drops and the map stays bounded over a long run.
        resumable = self._resumable_flow_ids(provider)
        for flow_id, cursor in self._history_cursors.items():
            if flow_id not in new_cursors and flow_id in resumable:
                new_cursors[flow_id] = cursor
        self._history_cursors = new_cursors
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

    def _resumable_flow_ids(self, provider: Any) -> Set[str]:
        """Return flow_ids still resumable from disk (live ``engine.json`` flows).

        Delegates to the provider's ``live_flow_ids`` when available so the
        cursor rebuild in :meth:`_push_history` can retain a final-flushed
        terminal flow's cursor (a FAILED flow awaiting ``se3 run --resume``).
        A provider without the method (older reader, test stub) yields an empty
        set, which preserves the prior prune-on-drain behavior. Failures are
        logged and swallowed — a degraded lookup must never break the push.
        """
        live = getattr(provider, "live_flow_ids", None)
        if not callable(live):
            return set()
        try:
            return set(live())
        except Exception:
            logger.debug(
                "live_flow_ids failed; not retaining terminal-flow cursors",
                exc_info=True,
            )
            return set()


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
