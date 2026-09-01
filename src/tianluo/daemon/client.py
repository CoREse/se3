"""The daemon's outbound WebSocket client to the central server.

:class:`DaemonClient` maintains a single outbound WebSocket connection from a
resident ``luo daemon`` to the central server. The connection is *outbound*
(the daemon dials the server) so SE3 machines never have to expose an inbound
port — this is the NAT-friendly half of the daemon↔server architecture.

Responsibilities:

* connect to ``ws://<server>/ws`` and announce itself with a
  :data:`~tianluo.daemon.protocol.MSG_HELLO`;
* push :data:`~tianluo.daemon.protocol.MSG_STATUS_UPDATE` snapshots on a fixed
  interval (the data source is the daemon's aggregator);
* answer server :data:`~tianluo.daemon.protocol.MSG_PING` heartbeats with a
  :data:`~tianluo.daemon.protocol.MSG_PONG`;
* route :data:`~tianluo.daemon.protocol.MSG_SPAWN_FLOW` to the daemon's spawner and
  :data:`~tianluo.daemon.protocol.MSG_RESPOND_CALL` to a ``se3/calls/`` response
  file;
* reconnect automatically with exponential backoff (capped at 60 s) and
  re-HELLO + push a full status snapshot after every reconnect.

The ``websockets`` library is an *optional* dependency (it ships in the
``tianluo[server]`` extra). The client imports it lazily: a daemon started without
``--server-url`` never touches it, and one started *with* a server URL but
without ``websockets`` installed logs a clear install hint and degrades to
local-only operation rather than crashing.
"""

from __future__ import annotations
from tianluo.runtime_paths import runtime_dir

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from . import history, protocol
from .supervisor import resolve_worktree_main_root
from .wire_metrics import WireMetrics

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

# Idle-gear cadences (seconds) used when the connected server (revision >= 4)
# reports zero browser viewers: nobody is watching the web UI, so neither the
# 1 s disk-signature tick nor the 5 s status heartbeat buys anything — both
# drop to a low-power cadence until presence returns. Module-level (looked up
# at call time) so tests can shrink them; server-command wakeups still bypass
# the cadence entirely via ``_fast_push_event``, so a SPAWN/INTERJECT/ISSUE
# command stays instant even in the idle gear.
_IDLE_FAST_INTERVAL = 30.0
_IDLE_STATUS_INTERVAL = 60.0

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
#: Type of the end-session handler — called with (flow_id, project_root, reason)
#: when an END_SESSION arrives. The handler terminates the flow's live process
#: and archives a worktree session (delegated to ``luo end-session``).
EndSessionHandler = Callable[[str, str, str], Any]
#: Type of the respond handler — called with (call_id, project_root, response).
RespondHandler = Callable[[str, str, Any], Any]
#: Type of the project-registry handler — called with (operation, project_root)
#: when a PROJECT_COMMAND arrives, returning the *normalized* path that was
#: actually registered / deregistered. It performs blocking disk I/O (registry
#: rewrite) so the client always invokes it off the event loop. It signals a
#: refusal by raising an exception carrying a stable ``code`` attribute
#: (``tianluo.daemon.daemon.ProjectCommandError``), which the client relays verbatim
#: as the reply's ``error_code``.
ProjectHandler = Callable[[str, str], str]
#: Type of the upload handler — called with (project_root, filename, data) when
#: an UPLOAD_COMMAND arrives, returning an object exposing ``path`` / ``size`` /
#: ``deduplicated`` (a :class:`~tianluo.daemon.uploads.UploadStored`). It writes
#: to disk, so the client always invokes it off the event loop. It signals a
#: refusal by raising an exception carrying a stable ``code`` attribute
#: (:class:`~tianluo.daemon.uploads.UploadError`), relayed verbatim as the
#: reply's ``error_code``.
UploadHandler = Callable[[str, str, bytes], Any]
#: Type of the fetch handler — called with (project_root, rel_path) when a
#: FETCH_COMMAND arrives, returning an object exposing ``data`` / ``size`` /
#: ``name`` (a :class:`~tianluo.daemon.uploads.UploadContent`). It reads from
#: disk, so the client always invokes it off the event loop. It signals a
#: refusal by raising an exception carrying a stable ``code`` attribute
#: (:class:`~tianluo.daemon.uploads.UploadError`), relayed verbatim as the
#: reply's ``error_code``.
FetchHandler = Callable[[str, str], Any]
#: Type of the history provider — a :class:`~tianluo.daemon.history.DaemonHistoryReader`
#: (or any object exposing ``build_index`` / ``read_flow`` / ``read_active_flows``).
HistoryProvider = Any
#: Type of the pending-calls signature provider — zero-arg callable returning a
#: cheap stat-based fingerprint dict of every ``tianluo/calls/`` file under every
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
    left :attr:`DaemonClient.last_error` blank and ``luo daemon status`` rendered
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


def _close_code_name(code: int) -> str:
    """Return the symbolic name for a WebSocket close *code* (or "").

    ``1000 NORMAL_CLOSURE`` / ``1008 POLICY_VIOLATION`` (a server-initiated
    close) reads very differently from ``1006 ABNORMAL_CLOSURE`` (a transport
    drop with no close frame — the proxy/timeout class of failure this project's
    livelock lived behind). Surfacing the symbolic name is what lets a human tell
    the two apart in ``daemon.log`` at a glance. Resolved via the websockets
    ``CloseCode`` enum, imported lazily because websockets is an optional dep.
    """
    try:
        from websockets.frames import CloseCode  # type: ignore

        return CloseCode(code).name
    except Exception:  # pragma: no cover - unknown/custom code or missing dep
        return ""


def _format_close_reason(ws: Any, exc: Optional[BaseException] = None) -> str:
    """Return a non-empty, credential-safe description of why a session ended.

    The daemon used to unwind a dropped session silently — the only trace was
    the *next* ``Dialing`` line, so a server-initiated close was indistinguishable
    from a proxy idle-timeout. This formats the close the peer/transport reported:

    - When a close code is known (``ws.close_code``), the code, its symbolic name
      and any UTF-8 close reason are rendered — a server close carries a real
      code/reason; a transport drop with no close frame surfaces as
      ``1006 ABNORMAL_CLOSURE``, itself a distinguishable network-class signal.
    - When no close code is available, the raised exception (if any) is formatted
      via :func:`_format_exc` so the reason is still non-empty and diagnosable.

    Neither the close frame nor any exception raised on this path carries the
    daemon key (the credential only ever travels on the HELLO wire), so the
    formatted reason is safe to log and to record in ``last_error``.
    """
    code = getattr(ws, "close_code", None)
    reason = (getattr(ws, "close_reason", None) or "").strip()
    if code is None:
        if exc is not None:
            return _format_exc(exc)
        return "connection closed without a close frame"
    label = _close_code_name(code)
    detail = f"{code} {label}" if label else str(code)
    return f"close {detail}: {reason}" if reason else f"close {detail}"


def _normalize_ws_url(server_url: str) -> str:
    """Return a ``ws(s)://host:port/ws`` URL from a user-supplied server URL.

    Accepts bare hosts (``host`` or ``host:8080``), ``http(s)://`` and
    ``ws(s)://`` URLs, and appends the ``/ws`` daemon endpoint path when none
    is present. When the host carries no explicit port, a *scheme-aware*
    default is filled in so the daemon and ``tianluo-server`` agree on the same
    default (a bare ``ws://host`` would otherwise fall back to the
    WebSocket-standard port 80):

    - ``ws://`` (and ``http://`` normalized to ``ws://``) → the plaintext
      :data:`~tianluo.daemon.protocol.DEFAULT_SERVER_PORT` (8080, the
      ``tianluo-server --port`` default).
    - ``wss://`` (and ``https://`` normalized to ``wss://``) →
      :data:`~tianluo.daemon.protocol.DEFAULT_SERVER_TLS_PORT` (443), because a
      TLS connection terminates at the reverse proxy's HTTPS port, not at
      tianluo-server's plaintext default.

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
        end_session_handler: Optional[EndSessionHandler] = None,
        respond_handler: Optional[RespondHandler] = None,
        project_handler: Optional[ProjectHandler] = None,
        upload_handler: Optional[UploadHandler] = None,
        fetch_handler: Optional[FetchHandler] = None,
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
                daemon to auto-run ``luo init`` on a brand-new target
                directory (and to register the root with the aggregator).
                When the returned object has a truthy ``.error`` attribute,
                the SPAWN_FLOW is aborted with that error logged.
            end_session_handler: Callable invoked for an incoming END_SESSION
                with ``(flow_id, project_root, reason)``. It terminates the
                flow's live ``luo run`` process and archives a worktree session
                (the daemon delegates this to an ``luo end-session``
                subprocess). When ``None`` an END_SESSION is logged and ignored.
            respond_handler: Callable invoked for an incoming RESPOND_CALL;
                when ``None`` the client writes the response file itself.
            project_handler: Callable invoked for an incoming PROJECT_COMMAND
                with ``(operation, project_root)``, returning the normalized
                path it registered / deregistered. When ``None`` the command is
                refused with ``error_code="unsupported"`` — a daemon build
                without registry management must say so rather than let the
                server's REST caller sit until its timeout.
            upload_handler: Callable invoked for an incoming UPLOAD_COMMAND
                with ``(project_root, filename, data)``, returning the stored
                attachment's project-relative path and size. When ``None`` the
                command is refused with ``error_code="unsupported"``, for the
                same reason ``project_handler`` is: an operator pasting a file
                must get an immediate, explainable refusal rather than watch
                the browser sit until the server's upload timeout.
            fetch_handler: Callable invoked for an incoming FETCH_COMMAND with
                ``(project_root, rel_path)``, returning the attachment's bytes.
                When ``None`` the command is refused with
                ``error_code="unsupported"`` immediately: the browser renders a
                whole conversation's inline images from these replies, so a
                daemon that cannot serve them must say so in one round trip
                rather than make every image on the page wait out the server's
                fetch timeout.
            history_provider: A :class:`~tianluo.daemon.history.DaemonHistoryReader`
                used to report the history index, push active-flow increments
                and answer HISTORY_REQUEST pulls. When ``None`` history support
                is disabled and the client behaves as before.
            calls_signature_provider: Zero-arg callable returning a cheap
                stat-based fingerprint dict of every ``tianluo/calls/`` file under
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
        self._end_session_handler = end_session_handler
        self._respond_handler = respond_handler or _default_respond_handler
        self._project_handler = project_handler
        self._upload_handler = upload_handler
        self._fetch_handler = fetch_handler
        self._interject_handler = _default_interject_handler
        self._history_provider = history_provider
        self._calls_signature_provider = calls_signature_provider
        self.status_interval = max(0.5, float(status_interval))
        self.history_poll_interval = max(
            0.1, min(float(history_poll_interval), self.status_interval)
        )

        # Per-message-type sent-byte accountant. Every ``_send`` records the
        # frame's type and encoded size here so the idle-vs-active traffic mix is
        # observable at runtime (surfaced in the daemon status file) — the
        # verification handle for "an idle daemon costs only keepalive-sized
        # traffic" and the regression guard for this optimization.
        self.metrics = WireMetrics()

        self._seq = 0
        self._connected = False
        self._last_error: Optional[str] = None
        # Content signature of the last *full* STATUS_UPDATE snapshot pushed. On
        # a status tick with an unchanged signature the client sends a tiny
        # MSG_KEEPALIVE instead of re-shipping the (now-slimmed but still
        # non-trivial) snapshot — the change-gate that makes steady-state idle
        # traffic collapse to heartbeats. Reset per session so a fresh server
        # always gets a full baseline snapshot first.
        self._last_status_sig: Optional[str] = None
        # Peer (server) protocol_version learned from WELCOME. Gates whether it
        # is safe to emit the revision-3 traffic-reduction frames (keepalive):
        # a legacy server that predates them would reject a keepalive as an
        # unknown type and lose the heartbeat, so against such a peer the client
        # keeps sending full STATUS_UPDATEs. ``None`` until WELCOME arrives —
        # treated as "no support" so the pre-WELCOME baseline push is always full.
        self._peer_protocol_version: Optional[Any] = None
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
        # Per-flow_id map of the last SessionMeta dict *actually pushed* to the
        # server, the baseline the incremental HISTORY_INDEX_DELTA diffs against.
        # A meta held back by the updated_at-only throttle is deliberately NOT
        # updated here, so the next status tick re-detects and flushes it.
        self._last_index_by_flow: Dict[str, Dict[str, Any]] = {}
        # Whether a full HISTORY_INDEX baseline has been sent this session. Delta
        # frames are only meaningful once the server holds a baseline to merge
        # into, so the first push (and every force_index) sends a full frame and
        # sets this; steady-state pushes then diff against ``_last_index_by_flow``.
        self._index_primed = False
        # Whether the connected server advertised protocol_version >= 3 in its
        # WELCOME. Until then (and for a legacy peer) we drive full-frame
        # semantics — a HISTORY_INDEX_DELTA sent to a v2 server would be rejected
        # as an unknown type and the index update lost (see protocol docstring).
        self._peer_supports_reduction = False
        self._history_cursors: Dict[str, Dict[str, int]] = {}
        # Flows with an in-flight multi-frame full-pull drain (the loop in
        # :meth:`_handle_history_request`).
        #
        # WHY: a full pull rebuilds the server's history bundle from a cursorless
        # snapshot and then advances its OWN local cursor across dozens of append
        # frames to catch up (a 4.6 MB active step needs 30~39 frames). While that
        # drain is running, the push loop (:meth:`_push_history`) is a concurrent
        # task that reads active flows off ``self._history_cursors`` — a cursor map
        # independent of the drain's local one. A push append emitted mid-drain
        # declares a ``cursor_base`` computed off the stale push-side water mark,
        # which lands PAST the server's half-rebuilt water mark and trips the
        # server's cursor-gap guard: the guard discards the whole bundle and
        # re-requests a full pull, which restarts the drain — a self-sustaining
        # loop the WebUI shows as the chat pane jumping between steps. Serialising
        # the two against this set closes the race: a flow is added for the whole
        # drain and removed at its end (see ``_drain_active`` and the skip in
        # ``_push_history``). Same-event-loop tasks only interleave at await
        # points, so a plain set (checked/mutated synchronously) is sufficient — no
        # lock is needed.
        self._history_draining: Set[str] = set()
        # Flows whose LAST push frame declared ``final=False`` — a byte/record
        # bounded chunk with backlog behind it — and so left an unfinished
        # delivery open on the server (``ServerState._OpenDelivery``).
        #
        # INVARIANT: a delivery this daemon opened is always closed by a frame
        # this daemon sends. WHY the set is needed to keep that promise: the
        # reader caps a read AFTER appending the record that crosses the bound
        # (see MAX_RECORDS_PER_REPORT / MAX_BYTES_PER_REPORT), so a flow whose
        # final record lands exactly ON the cap reports ``truncated`` with
        # nothing left behind it. The next read confirms EOF with an EMPTY
        # record list — and the push path ships no frame for an empty read, so
        # without this set the closing declaration is never sent: the server
        # keeps the delivery open, reports a COMPLETE bundle as ``incomplete``,
        # and fires a pointless repair pull once the stall grace expires (and
        # stays wrong if that pull fails). Tracking the open delivery lets the
        # EOF-confirming read go out as an empty ``final=True`` terminator.
        self._history_push_open: Set[str] = set()
        # Last active-flow disk signature seen by the push loop; an unchanged
        # signature means there is nothing new to push (debounce).
        self._last_history_signature: Dict[str, Any] = {}
        # Last ``tianluo/calls/`` file signature seen by the push loop. An unchanged
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
        # walks the whole ``tianluo/history`` tree) on the issue-command hot path —
        # the periodic STATUS_UPDATE loop already keeps it fresh. ``None`` means
        # "no snapshot built yet"; the issue handler then falls back to building
        # one snapshot to validate against.
        self._last_known_project_roots: Optional[set] = None
        # Browser-presence belief learned from the server: the MSG_VIEWERS
        # 0↔non-0 edges and the ``viewers`` level riding on every PING both
        # funnel into :meth:`_update_viewer_count`. ``None`` means "unknown"
        # and — like any count > 0 — keeps the full-speed cadence: the client
        # only downshifts on a trustworthy, explicit zero (fail-open, so any
        # lost/absent presence info can cost CPU but never real-time behavior).
        self._viewer_count: Optional[int] = None
        # Set on a 0→non-0 presence edge; the next push-loop tick consumes it
        # and forces a full STATUS_UPDATE + full HISTORY_INDEX so a just-opened
        # browser's first screen is fresh within one fast tick, closing the
        # 0→1 wake race left by the idle gear's long cadence.
        self._presence_wake_force = False

    # -- introspection -----------------------------------------------------

    @property
    def connected(self) -> bool:
        """Whether an active WebSocket session is currently established."""
        return self._connected

    @property
    def last_error(self) -> Optional[str]:
        """The most recent connection error string, if any."""
        return self._last_error

    @property
    def viewer_count(self) -> Optional[int]:
        """The server-reported browser viewer count, gated on peer support.

        Returns ``None`` — "unknown, assume watched" — until a revision >= 4
        server has been welcomed this session, so a count recorded against a
        legacy (< v4) server can never report an *effective* zero: such a
        server has no presence channel to wake the daemon back up with, and
        downshifting on its silence would trade away the web UI's real-time
        behavior. The daemon's poll loop reads this to pick its own gear.
        """
        if not protocol.supports_presence(self._peer_protocol_version):
            return None
        return self._viewer_count

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
                "installed. Install it with: pip install 'tianluo[server]'. "
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
                # in ``last_error`` for ``luo daemon status`` to surface.
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
            # WHY: relax our own keepalive tolerance for lossy/high-latency links
            # (e.g. node007). The library default ping_timeout=20 meant a single
            # PONG lost on a bad network tripped a "keepalive ping timeout" close
            # roughly every ~45s, and each drop truncated an in-flight full
            # history reload — driving the presence flap + chat-record jitter.
            # ping_interval=20 keeps liveness detection; ping_timeout=60 rides
            # out transient stalls. Server-side heartbeat is widened to match.
            ping_interval=20,
            ping_timeout=60,
            # Explicitly enable permessage-deflate. ``websockets`` negotiates it
            # by default, but pinning ``compression="deflate"`` here makes the
            # second layer of traffic reduction (WS-level compression, orthogonal
            # to the app-level slimming/gating) explicit and immune to a future
            # library default change — a real ``full`` history-data / status frame
            # is JSON that deflates ~5-10x.
            compression="deflate",
        ) as ws:
            self._connected = True
            self._last_error = None
            # A new session forgets the INDEX baseline so the server gets a fresh
            # full index after every reconnect — the index is a small per-flow
            # metadata list, cheap to re-establish in full.
            self._last_index = None
            self._last_index_by_flow = {}
            self._index_primed = False
            # A new session optimistically assumes a legacy peer until the
            # WELCOME reveals the server's protocol_version; the primer push is a
            # full frame regardless, so this never withholds the baseline.
            self._peer_supports_reduction = False
            # INVARIANT: the history DELIVERY cursor is PRESERVED across
            # reconnects — it must NOT be reset here. WHY: on the confirmed
            # failure environment the daemon↔server link is force-recycled roughly
            # every ~40 s; wiping the cursor made every new session re-read each
            # flow from line 0 (mode=full) and re-deliver the same leading
            # chunk(s), so a backlog larger than one connection window can carry
            # (the 8.4 MB ``06_implement``) never advanced past its first chunk —
            # the delivery livelock (Defect A). Combined with the byte-bounded
            # chunking (MAX_BYTES_PER_REPORT), retaining the cursor makes each new
            # window RESUME from the last CONFIRMED chunk, so a large backlog is
            # caught up monotonically across successive windows. This is safe even
            # when the peer lost the flow's bundle (a server restart, or a
            # reconnect to a peer that never saw the flow): the resumed frame
            # declares its coverage window (``cursor_base`` → ``cursor``), the
            # server's cursor-gap guard sees it start past its empty water mark and
            # arms ``requires_full`` so a self-heal full pull rebuilds the bundle
            # from scratch. The change-detection SIGNATURE is still reset (below),
            # so the reconnect always drives a fresh push that resumes the drain.
            self._last_history_signature = {}
            self._last_calls_signature = {}
            # Forget the prior status signature and peer version: a fresh (or
            # reconnected) server must receive a full baseline STATUS_UPDATE, and
            # its keepalive support is unknown until this session's WELCOME.
            self._last_status_sig = None
            self._peer_protocol_version = None
            # Presence belief is per-session: the server re-announces the
            # count right after the handshake (post-handshake MSG_VIEWERS +
            # the PING level), so a stale zero from the previous session must
            # not idle-gear the client against a server that can no longer
            # correct it. ``None`` fails open to full speed.
            self._viewer_count = None
            self._presence_wake_force = False
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
            # Prime the signatures so the fast push loop only fires on the *next*
            # disk change rather than immediately re-pushing what we just sent.
            # Both signature scans parse on-disk engine.json (the active-flow
            # signature, and the calls signature via the aggregator's worktree
            # scan), so they are offloaded to a worker thread — running them
            # synchronously here would parse JSON on the event loop, the exact
            # #209 / #243 starvation this hardening removes.
            await asyncio.to_thread(self._history_changed)
            await asyncio.to_thread(self._calls_changed)
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
            # Capture the first real (non-cancellation) exception a raced task
            # raised — a ConnectionClosedError from the receive/push loop carries
            # the close code/reason — so the disconnect can be reported instead of
            # unwinding silently. Cancellation of the pending tasks is expected.
            task_exc: Optional[BaseException] = None
            for task in (*done, *pending):
                try:
                    await task
                except asyncio.CancelledError:  # pragma: no cover - shutdown
                    pass
                except Exception as exc:  # pragma: no cover - exercised via stub
                    if task_exc is None:
                        task_exc = exc
            # A session that ends while neither shutting down nor auth-rejected
            # means the socket closed on us (server close or transport drop). Log
            # the close code/reason so a network-class drop and a server-initiated
            # close are distinguishable in daemon.log — the auth-rejected path is
            # reported by ``run`` and a clean shutdown needs no disconnect notice.
            if not stop_event.is_set() and not self._auth_rejected:
                reason = _format_close_reason(ws, task_exc)
                self._last_error = reason
                logger.warning("Central server connection closed: %s", reason)

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
        ``tianluo/calls/`` directory signature changed since the previous tick (so
        a freshly-written interjection or a freshly-drained call surfaces on
        the web within one fast tick instead of waiting the full status
        interval). A HISTORY push fires whenever the active-flow disk
        signature changed since the last check — so a CLI step that advances
        ``engine.json`` / appends a jsonl line reaches the web within one fast
        tick — and also on every status tick as a backstop in case a change is
        ever missed. When nothing changed, the signature checks are cheap
        stat-only scans and no frame is sent (debounce).

        Both cadences are the *effective* intervals from
        :meth:`_effective_intervals`: when the server reports zero browser
        viewers, the loop runs in the idle gear (30 s / 60 s) instead of the
        configured full-speed cadence. A 0→non-0 presence edge sets
        ``_presence_wake_force`` (and the fast-push event), which this loop
        consumes as "force a full STATUS_UPDATE + full HISTORY_INDEX now" so
        a just-opened browser gets fresh state within one fast tick rather
        than a stale idle-gear snapshot.

        Both pushes share this one coroutine so their ``ws.send`` calls never
        interleave (which two independent loops racing on the same socket could
        do), keeping every wire frame intact.
        """
        last_status = time.monotonic()
        while not stop_event.is_set():
            woke_for_fast_push = await self._wait_next_tick(stop_event)
            if stop_event.is_set():
                break
            # Consume the presence-wake flag exactly once. It rode in on the
            # fast-push event, so this tick started immediately after the edge.
            presence_wake = self._presence_wake_force
            self._presence_wake_force = False
            now = time.monotonic()
            _, effective_status_interval = self._effective_intervals()
            status_due = (now - last_status) >= effective_status_interval
            # A genuine call-file change drives an immediate STATUS_UPDATE so
            # the web sees the new / drained interjection chip within ~1 s. The
            # calls signature scans every tracked root AND every active
            # ``--worktree`` run subdir, and that worktree scan parses each
            # subdir's engine.json (via the aggregator) — a JSON parse that must
            # never run on the event loop, so it is offloaded like the history
            # check below (the push loop is this method's sole caller, so the
            # off-thread signature-state mutation is race-free).
            calls_changed = await asyncio.to_thread(self._calls_changed)
            # ``woke_for_fast_push`` is set by ``_handle_interject`` the
            # instant a server-delivered interjection has hit disk — push now
            # rather than waiting for the next ``has_changes``-style scan to
            # notice it on the next tick.
            push_status = (
                status_due or calls_changed or woke_for_fast_push or presence_wake
            )
            if push_status:
                last_status = now
                # A call-file change or a fast-push wake is a genuine content
                # event that must ship a *real* STATUS_UPDATE, so it bypasses the
                # keepalive content-gate. A plain heartbeat tick (status_due
                # alone) may collapse to a keepalive when nothing actually
                # changed since the last snapshot. A presence wake must also
                # bypass the gate: the browser that just opened holds no state
                # at all, so a keepalive would leave its first screen empty.
                force = calls_changed or woke_for_fast_push or presence_wake
                await self._push_status(ws, force=force)
            # Push history on a real disk change, or on the status tick (backstop).
            # The change check reads each active flow's engine.json + stats its
            # jsonl files; even with the reader's stat-keyed parse cache a cold
            # re-parse of a ~1 MB engine.json is CPU-bound, so it is offloaded to
            # a worker thread (the issue-#209 starvation fix) instead of running
            # synchronously on the event loop. The reader is the push loop's sole
            # caller of this method, so the off-thread mutation of its signature
            # state is race-free.
            #
            # The status tick's backstop role extends to the reader's dirty-
            # sentinel gate: the gate skips idle roots on fast ticks, trusting
            # the PersistenceManager-bumped sentinel — a change written by a
            # sentinel-unaware writer would stay gated forever. Dropping the
            # gate here (before the scan below) makes every status tick an
            # ungated full scan, bounding that staleness by one status
            # interval. A presence wake drops it too: the just-opened browser
            # must not inherit a gated (possibly stale) view. getattr-probed
            # like invalidate_index_cache so stub providers stay valid.
            if status_due or presence_wake:
                clear_gate = getattr(
                    self._history_provider, "clear_sentinel_gate", None
                )
                if clear_gate is not None:
                    clear_gate()
            history_changed = await asyncio.to_thread(self._history_changed)
            # ``woke_for_fast_push`` must ALSO drive the history push, not only a
            # STATUS_UPDATE. WHY: a truncated history round re-arms fast-push (see
            # _push_history) precisely so the NEXT chunk of a large backlog drains
            # at link rate instead of one chunk per status heartbeat. But a static
            # (fully-written, terminal) backlog produces NO disk change, so
            # ``_history_changed()`` stays False and, in the idle gear, ``status_due``
            # is up to 60 s away — draining an 8.4 MB backlog one 256 KB chunk per
            # 60 s (and, under ~40 s connection windows, at most one chunk per
            # window) is still the livelock in slow motion. Including the fast-push
            # wake here converts the re-arm into the very next _push_history call,
            # so consecutive chunks drain back-to-back within one connection window.
            if status_due or history_changed or presence_wake or woke_for_fast_push:
                # A real disk change (engine.json rewrite / jsonl append) means
                # the on-disk state diverged from the cached index.  Invalidate
                # so the next build_index() rebuilds from disk instead of
                # returning a stale snapshot for up to BUILD_INDEX_TTL. A
                # presence wake invalidates too: the idle gear may have skipped
                # ticks for long enough that a TTL-cached index predates real
                # changes, and the just-opened browser must see fresh state.
                if history_changed or presence_wake:
                    invalidate = getattr(
                        self._history_provider, "invalidate_index_cache", None
                    )
                    if invalidate is not None:
                        invalidate()
                # ``status_due`` doubles as the throttle window for updated_at-only
                # meta churn: an active flow's timestamp-only delta is held back on
                # a fast tick and only flushed on the status heartbeat, so it never
                # re-pushes more often than the heartbeat itself. ``force_index``
                # on a presence wake re-baselines the server's index mirror, the
                # HISTORY half of the 0→1 full refresh.
                await self._push_history(
                    ws, status_tick=status_due, force_index=presence_wake
                )

    async def _wait_next_tick(self, stop_event: asyncio.Event) -> bool:
        """Wait one fast tick, returning whether the fast-push event woke us.

        Races the fast-tick timeout against the stop event AND the
        ``_fast_push_event`` so a server-delivered interjection can drive an
        immediate push instead of waiting up to ``history_poll_interval``.
        The fast-push event is cleared inside the locked window so a
        concurrent ``set`` between two ticks still wakes the *next* tick.
        Returns ``True`` only when the fast-push event actually triggered the
        wakeup, so the caller can force a STATUS_UPDATE on that tick.

        The tick length is the *effective* fast interval — the configured
        ``history_poll_interval`` while watched, the idle-gear cadence when the
        server reports zero viewers — so the whole loop slows down together.
        """
        fast_interval, _ = self._effective_intervals()
        event = self._fast_push_event
        if event is None:  # pragma: no cover - defensive (set in _session)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=fast_interval)
            except asyncio.TimeoutError:
                pass
            return False
        stop_task = asyncio.create_task(stop_event.wait())
        push_task = asyncio.create_task(event.wait())
        try:
            done, pending = await asyncio.wait(
                {stop_task, push_task},
                timeout=fast_interval,
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
        """Return whether the ``tianluo/calls/`` directory signature changed.

        Updates :attr:`_last_calls_signature` as a side effect. Returns
        ``False`` when no calls-signature provider is configured, so a client
        wired without one keeps its prior behavior. A signature lookup failure
        conservatively reports a change so the next push still runs.

        The provider's signature is intentionally kind-agnostic — it captures
        every file under ``tianluo/calls/`` so that both an interjection file
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

    def _effective_intervals(self) -> Tuple[float, float]:
        """Return the ``(fast, status)`` cadences for the current gear.

        The idle gear — the module-level ``_IDLE_FAST_INTERVAL`` /
        ``_IDLE_STATUS_INTERVAL`` constants — applies only when the server
        reported an explicit ``viewers == 0`` *and* advertised revision >= 4
        (both folded into the :attr:`viewer_count` property): a legacy server
        or an unknown count keeps the configured full-speed cadence, so any
        presence-signal loss degrades to today's behavior, never to a stale
        web UI.
        """
        if self.viewer_count == 0:
            return (_IDLE_FAST_INTERVAL, _IDLE_STATUS_INTERVAL)
        return (self.history_poll_interval, self.status_interval)

    def _update_viewer_count(self, value: Any) -> None:
        """Fold one presence report (edge or PING level) into the gear state.

        A missing / malformed *value* is ignored — the current gear must never
        move on garbage, only on an explicit well-formed count. A 0→non-0
        transition (including the session's very first non-zero report) arms
        ``_presence_wake_force`` and wakes the push loop immediately, so the
        browser that just connected gets a forced full refresh within one fast
        tick instead of waiting out the idle-gear cadence.
        """
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return
        previous = self._viewer_count
        self._viewer_count = value
        if value > 0 and (previous is None or previous == 0):
            self._presence_wake_force = True
            self._trigger_fast_push()

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
        # HOP-3 DEBUG (daemon change-detection → push decision): the push loop
        # calls read_active_flows + _push_history ONLY when this returns True.
        # If the discovery→analyze boundary appends to disk but this stays False
        # for a whole tick, the delta is not read/sent that tick — the first
        # candidate #260 drop hop. Logged with the flow set so a live run shows
        # whether the boundary is seen as a change.
        if changed:
            logger.debug(
                "hist-diag _history_changed=True flows=%s (push will read+send)",
                sorted(signature),
            )
        else:
            logger.debug("hist-diag _history_changed=False (debounced, no push)")
        self._last_history_signature = signature
        return changed

    # -- message handling --------------------------------------------------

    async def _dispatch(self, ws: Any, message: protocol.Message) -> None:
        """Route one inbound server message to its handler."""
        if message.type == protocol.MSG_PING:
            # Answer first — the PONG is the liveness signal the server times
            # out on, so it must never wait behind presence bookkeeping. The
            # optional ``viewers`` level riding on the heartbeat is the
            # self-healing path for a lost MSG_VIEWERS edge (absent/malformed
            # → ignored, gear unchanged).
            await self._send(ws, protocol.make_pong(seq=message.seq))
            self._update_viewer_count(message.payload.get("viewers"))
        elif message.type == protocol.MSG_VIEWERS:
            self._update_viewer_count(message.payload.get("count"))
        elif message.type == protocol.MSG_WELCOME:
            self._handle_welcome(message.payload)
        elif message.type == protocol.MSG_SPAWN_FLOW:
            await self._handle_spawn(ws, message.payload)
        elif message.type == protocol.MSG_RESPOND_CALL:
            await self._handle_respond(message.payload)
        elif message.type == protocol.MSG_INTERJECT_FLOW:
            await self._handle_interject(message.payload)
        elif message.type == protocol.MSG_END_SESSION:
            await self._handle_end_session(message.payload)
        elif message.type == protocol.MSG_ISSUE_COMMAND:
            await self._handle_issue_command(ws, message.payload)
        elif message.type == protocol.MSG_PROJECT_COMMAND:
            await self._handle_project_command(ws, message.payload)
        elif message.type == protocol.MSG_UPLOAD_COMMAND:
            await self._handle_upload_command(ws, message.payload)
        elif message.type == protocol.MSG_FETCH_COMMAND:
            await self._handle_fetch_command(ws, message.payload)
        elif message.type == protocol.MSG_HISTORY_REQUEST:
            await self._handle_history_request(ws, message.payload)
        elif message.type == protocol.MSG_HISTORY_INDEX_REQUEST:
            await self._handle_history_index_request(ws)
        elif message.type == protocol.MSG_DETAIL_REQUEST:
            await self._handle_detail_request(ws, message.payload)
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
        # Record the server's advertised protocol_version so the status-push
        # gate knows whether it may emit a keepalive (revision >= 3) or must keep
        # sending full STATUS_UPDATEs to a legacy peer.
        self._peer_protocol_version = payload.get("protocol_version")
        # Learn whether this server understands the revision-3 traffic-reduction
        # frames. Recorded on both accept and reject paths (harmless on reject),
        # so once accepted the steady-state pushes may switch from full
        # HISTORY_INDEX to incremental deltas; a legacy (v2) server keeps
        # full-frame semantics.
        self._peer_supports_reduction = protocol.supports_traffic_reduction(
            payload.get("protocol_version")
        )
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
        auto-runs ``luo init`` there and registers it before the spawn
        proceeds. A truthy ``.error`` on the returned object aborts the spawn
        and is logged; nothing half-initialized leaks downstream.

        A failure on any of the three execution paths (resume / project-init /
        fresh spawn) does **not** silently return: the real error is sent back
        to the server as a :data:`~tianluo.daemon.protocol.MSG_SPAWN_FAILED` so the
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
                # request_resume reads engine.json / a resumable snapshot from
                # disk (size-guarded, but still blocking I/O + a bounded parse):
                # run it in a worker thread so no disk-JSON work ever lands on
                # the event loop (issue #243 A3), keeping heartbeats / pushes /
                # reconnects responsive while a resume is being preflighted.
                await asyncio.to_thread(
                    self._resume_handler, resume_flow_id, project_root
                )
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
        plan_decomposition = str(payload.get("plan_decomposition") or "").strip()
        plan_granularity = str(payload.get("plan_granularity") or "").strip()
        if plan_decomposition not in protocol.SPAWN_PLAN_DECOMPOSITION_VALUES:
            # A malformed value (or an empty one) simply stays off the argv:
            # the CLI then resolves project config / default. Never guess.
            plan_decomposition = ""
        if plan_granularity not in protocol.SPAWN_PLAN_GRANULARITY_VALUES:
            plan_granularity = ""
        legacy_strategy = str(payload.get("implementation_strategy") or "").strip()
        if legacy_strategy in protocol.SPAWN_STRATEGY_VALUES:
            # A pre-8 server still speaks the retired axis. Translate the
            # operator's intent onto the new options rather than dropping it;
            # an explicit new-model field always wins over the mapping.
            mapped_decomposition, mapped_granularity = (
                protocol.SPAWN_STRATEGY_PLAN_MODE_MAP[legacy_strategy]
            )
            logger.warning(
                "SPAWN_FLOW carried the deprecated implementation_strategy=%r; "
                "mapping it onto plan_decomposition=%r / plan_granularity=%r. "
                "The field is removed in the next major version.",
                legacy_strategy, mapped_decomposition, mapped_granularity,
            )
            if mapped_decomposition and not plan_decomposition:
                plan_decomposition = mapped_decomposition
            if mapped_granularity and not plan_granularity:
                plan_granularity = mapped_granularity
        elif legacy_strategy:
            logger.warning(
                "Ignoring SPAWN_FLOW implementation_strategy=%r: not a legal value",
                legacy_strategy,
            )
        if self._spawn_handler is None:
            logger.warning("Received SPAWN_FLOW but no spawn handler is configured")
            return
        if self._ensure_handler is not None and project_root:
            try:
                # The ensure hook may run ``luo init`` (a blocking subprocess,
                # up to a 120s timeout) for a not-yet-SE3 directory, then
                # register the root via ``add_project_root`` -> registry persist
                # -> a project_roots.json json.loads. All of that is blocking
                # I/O + disk-JSON parsing, so it must run in a worker thread and
                # never on the event loop (issue #243 A3) — otherwise a New Task
                # on a fresh path stalls heartbeats/pushes for the whole init.
                ensure = await asyncio.to_thread(
                    self._ensure_handler, project_root
                )
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
            # The from_issue_id 5th positional and the worktree / plan-mode
            # keywords are passed only when present/true so legacy 4-argument
            # spawn handlers stay backward compatible (a non-isolated fresh
            # spawn keeps the exact 4-positional call shape).
            # The spawn handler registers the new flow, which reads engine.json
            # for its flow_id (size-guarded, but still disk I/O), and blocks on
            # a subprocess launch — run it off the event loop (issue #243 A3).
            spawn_kwargs = {"worktree": True} if worktree else {}
            if plan_decomposition:
                spawn_kwargs["plan_decomposition"] = plan_decomposition
            if plan_granularity:
                spawn_kwargs["plan_granularity"] = plan_granularity
            if from_issue_id:
                await asyncio.to_thread(
                    self._spawn_handler,
                    task, project_root, task_type, discover, from_issue_id,
                    **spawn_kwargs,
                )
                logger.info("SPAWN_FLOW handled from issue %s", from_issue_id)
            else:
                await asyncio.to_thread(
                    self._spawn_handler,
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

    async def _handle_respond(self, payload: Dict[str, Any]) -> None:
        """Route a RESPOND_CALL instruction to the response-file writer.

        Both the history-index root resolution and the respond handler itself
        (which re-spawns a paused flow after reading its engine.json) touch disk
        JSON, so both are dispatched via ``asyncio.to_thread`` — no disk parse
        ever runs on the event loop (issue #243 A3).
        """
        call_id = str(payload.get("call_id") or "").strip()
        if not call_id:
            logger.warning("Ignoring RESPOND_CALL with empty call_id")
            return
        project_root = str(payload.get("project_root") or "").strip()
        if not project_root:
            # The server omitted the root (or sent an empty one).  Resolve it
            # from the history index — the SAME ``project_root`` the history
            # reader scopes its reads to — so a ``--worktree`` / discovery
            # session's ``.response`` lands under the root whose history is
            # being read and pushed, rather than falling back to the daemon's
            # own cwd (``_default_respond_handler``), which would never reach
            # the running ``luo run`` process. build_index parses engine.json /
            # snapshots / _meta.json, so it must not run on the loop.
            flow_id = str(payload.get("flow_id") or "").strip()
            if flow_id:
                project_root = await asyncio.to_thread(
                    self._resolve_flow_root_from_index, flow_id
                )
        response = payload.get("response")
        try:
            await asyncio.to_thread(
                self._respond_handler, call_id, project_root, response
            )
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
            snapshot = None
        for flow in (snapshot or {}).get("flows") or []:
            if isinstance(flow, dict) and str(flow.get("flow_id") or "") == flow_id:
                root = str(flow.get("project_root") or "").strip()
                if root:
                    return root
        # The snapshot may not yet list a just-started flow (the per-flow poll
        # set lags a fresh spawn).  Fall back to the history index, which is the
        # authoritative ``project_root`` the history reader itself uses, so an
        # interjection on a ``--worktree`` / discovery session resolves to the
        # same root its history is read from. build_index parses disk JSON, so
        # it runs in a worker thread (issue #243 A3).
        return await asyncio.to_thread(self._resolve_flow_root_from_index, flow_id)

    def _resolve_flow_root_from_index(self, flow_id: str) -> str:
        """Resolve *flow_id*'s ``project_root`` from the history index.

        Returns the ``project_root`` the :class:`~tianluo.daemon.history.DaemonHistoryReader`
        associates with *flow_id* — the exact root its ``read_flow`` /
        ``read_active_flows`` scope to — so the respond / interject write path
        stays consistent with the history-read path for ``--worktree`` /
        discovery sessions (whose root is the attributed main root, not the
        worktree sandbox).  ``build_index`` is TTL-cached, so this is cheap.
        Returns an empty string when the flow is unknown or no provider is set.
        """
        provider = self._history_provider
        if provider is None or not hasattr(provider, "build_index"):
            return ""
        try:
            for meta in provider.build_index():
                if str(getattr(meta, "flow_id", "") or "") == flow_id:
                    return str(getattr(meta, "project_root", "") or "").strip()
        except Exception:  # pragma: no cover - defensive
            logger.debug(
                "History-index lookup for flow %s failed", flow_id, exc_info=True
            )
        return ""

    async def _handle_end_session(self, payload: Dict[str, Any]) -> None:
        """Route an END_SESSION instruction to the daemon's end-session handler.

        Resolves the ``flow_id`` (safely ignoring an empty one) and, when the
        payload omits ``project_root``, reverse-resolves it from the history
        index — the same authoritative root the history reader scopes a
        ``--worktree`` / discovery session to — mirroring the RESPOND_CALL /
        INTERJECT_FLOW resolution. That reverse-resolution parses disk JSON
        (``build_index``), so it runs in a worker thread (issue #243 A3). The
        injected handler itself can spawn ``luo end-session`` and inspect
        supervisor / engine state, so it is dispatched via ``asyncio.to_thread``
        too — mirroring RESPOND_CALL — keeping the websocket receive loop,
        heartbeats, and push processing responsive instead of blocking on a
        synchronous handler. A handler exception is caught and logged so a bad
        end-session request never tears the connection down.
        """
        if self._end_session_handler is None:
            logger.warning(
                "Received END_SESSION but no end-session handler is configured"
            )
            return
        flow_id = str(payload.get("flow_id") or "").strip()
        if not flow_id:
            logger.warning("Ignoring END_SESSION with empty flow_id")
            return
        project_root = str(payload.get("project_root") or "").strip()
        if not project_root:
            project_root = await asyncio.to_thread(
                self._resolve_flow_root_from_index, flow_id
            )
        reason = str(payload.get("reason") or "").strip() or "user terminated"
        try:
            await asyncio.to_thread(
                self._end_session_handler, flow_id, project_root, reason
            )
            logger.info("END_SESSION handled for flow %s", flow_id)
        except Exception:
            logger.exception("END_SESSION handler failed for flow %s", flow_id)
            return
        # End-session archives the flow (engine.json → archive), a change the
        # active-flow signature cannot see when the flow was already terminal
        # (terminal flows are excluded from the signature). With the index TTL
        # now a long backstop, this explicit invalidation is what keeps the
        # active→archived row transition prompt instead of up-to-TTL stale.
        # (Chained getattr: partially-constructed clients in tests may carry no
        # history provider attribute at all.)
        invalidate = getattr(
            getattr(self, "_history_provider", None), "invalidate_index_cache", None
        )
        if invalidate is not None:
            invalidate()

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
        # whole tianluo/history tree) — that repeated heavyweight call is what
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
        # An explicit state-changing command drops the cached index so the next
        # push rebuilds from disk. Issue writes do not (today) surface in the
        # history index itself, but the index TTL is now a long backstop and
        # the invalidation contract is "every explicit write command refreshes"
        # — a rare, user-initiated rebuild, kept uniform with spawn / resume /
        # end-session rather than special-cased.
        invalidate = getattr(self._history_provider, "invalidate_index_cache", None)
        if invalidate is not None:
            invalidate()
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

    async def _handle_project_command(self, ws: Any, payload: Dict[str, Any]) -> None:
        """Register or deregister a project root on operator request.

        Validates the ``operation`` / ``project_root`` shape cheaply here, then
        hands the real work to the injected ``project_handler`` (the daemon's
        ``request_add_project`` / ``request_remove_project``), which owns the
        filesystem checks, the live-flow refusal and the registry write.

        A refusal travels back as a stable ``error_code`` rather than prose: the
        web UI's user-facing text must come from a localized key, so the code is
        the contract and the message is only a diagnostic fallback.
        """
        operation = str(payload.get("operation") or "").strip()
        project_root = str(payload.get("project_root") or "").strip()
        request_id = str(payload.get("request_id") or "").strip()

        async def _reply(
            *,
            ok: bool,
            error: str = "",
            error_code: str = "",
            registered: str = "",
        ) -> None:
            """Send a result back if we have a request_id and a live ws."""
            if not request_id:
                return
            try:
                await self._send(
                    ws,
                    protocol.make_project_result(
                        request_id,
                        ok=ok,
                        error=error,
                        error_code=error_code,
                        project_root=registered,
                    ),
                )
            except Exception:
                logger.debug(
                    "Failed to send PROJECT_RESULT for request %s",
                    request_id,
                    exc_info=True,
                )

        if operation not in protocol.PROJECT_OPERATIONS:
            logger.warning("Ignoring PROJECT_COMMAND with operation %r", operation)
            await _reply(
                ok=False,
                error=f"unknown project operation: {operation!r}",
                error_code="invalid_operation",
            )
            return
        if not project_root or not Path(project_root).is_absolute():
            logger.warning(
                "PROJECT_COMMAND: project_root must be an absolute path, got %r",
                project_root,
            )
            await _reply(
                ok=False,
                error="project_root must be an absolute path",
                error_code="invalid_path",
            )
            return
        handler = self._project_handler
        if handler is None:
            logger.warning("PROJECT_COMMAND received but no project handler is wired")
            await _reply(
                ok=False,
                error="project registry management is not available",
                error_code="unsupported",
            )
            return

        # The handler rewrites the on-disk registry — blocking I/O that must
        # never run on the event loop, which is also serving the status pushes
        # the web UI depends on.
        try:
            registered = await asyncio.to_thread(handler, operation, project_root)
        except Exception as exc:
            logger.warning(
                "PROJECT_COMMAND %s failed for %s: %s", operation, project_root, exc
            )
            await _reply(
                ok=False,
                error=str(exc) or type(exc).__name__,
                # Only a ProjectCommandError carries a code; anything else is an
                # unexpected fault, and an empty code lets the UI fall back to
                # its generic failure message rather than invent a wrong key.
                error_code=str(getattr(exc, "code", "") or ""),
            )
            return

        # Ack *before* the fast push, for the same reason ISSUE_COMMAND does:
        # the PROJECT_RESULT frame is what the server blocks on within its
        # command timeout, while the fast push only schedules a follow-up
        # STATUS_UPDATE. Pushing first would risk reporting a timeout for a
        # registry change that already landed on disk.
        await _reply(ok=True, registered=str(registered or project_root))
        # The registry just changed, so the cached root set is stale — drop it
        # so an ISSUE_COMMAND arriving before the next STATUS_UPDATE still sees
        # a freshly added project (and no longer accepts a removed one).
        self._last_known_project_roots = None
        self._trigger_fast_push()
        logger.info(
            "PROJECT_COMMAND %s handled for %s", operation, registered or project_root
        )

    async def _resolve_registered_root(
        self, project_root: str, *, label: str
    ) -> Optional[str]:
        """Resolve *project_root* to a registered root, or ``None`` if refused.

        Same registry gate as ISSUE_COMMAND, and for a stronger reason: the
        frames that come through here touch the filesystem. Restricting the
        target to a root the aggregator already tracks means a compromised or
        spoofed server still cannot name an arbitrary directory on this machine
        — neither to write into nor to read out of. Prefer the cache the
        STATUS_UPDATE loop refreshes so a paste or an inline image — both
        interactive, latency-sensitive — never pays for a full snapshot walk.

        A snapshot lookup failure is reported as a refusal rather than
        propagated: an unreadable registry means the gate cannot be evaluated,
        and an unevaluable gate must fail closed.
        """
        known_roots = self._last_known_project_roots
        if known_roots is None:
            try:
                snapshot = await asyncio.to_thread(self._snapshot_provider)
            except Exception:
                logger.debug("%s: snapshot lookup failed", label, exc_info=True)
                return None
            known_roots = set(snapshot.get("project_roots") or [])
            self._last_known_project_roots = known_roots
        resolved = str(Path(project_root).resolve())
        if resolved in known_roots:
            return resolved
        # A `luo run --worktree` flow reports its sandbox
        # (<main>/tianluo/worktrees/<name>) as project_root, and the aggregator
        # deliberately keeps those out of the registry — they are transient
        # copies, not projects. The gate must therefore accept a sandbox whose
        # *main* root is registered, otherwise every attachment to a
        # worktree-mode flow is refused. The resolved sandbox path is what is
        # returned, because the sandbox's own uploads dir is the one the flow's
        # agent reads the relative path from.
        main_root = resolve_worktree_main_root(resolved)
        if main_root and str(Path(main_root).resolve()) in known_roots:
            return resolved
        logger.warning(
            "%s: project_root %r is not a registered project; known roots: %s",
            label,
            project_root,
            sorted(known_roots)[:5],
        )
        return None

    async def _handle_upload_command(self, ws: Any, payload: Dict[str, Any]) -> None:
        """Land one operator-attached file in the project's uploads directory.

        Decodes and bounds-checks the frame here, re-validates the target
        against this daemon's own project registry, then hands the bytes to the
        injected ``upload_handler`` (``uploads.store_upload``) off the event
        loop. Every outcome travels back as a stable ``error_code`` rather than
        prose — the browser renders a localized string from that code, and the
        untranslated ``error`` is only a diagnostic fallback.

        The size and registry checks are deliberately re-done even though the
        server already performed them: the server is a separate trust domain
        reachable from the network, and this daemon's disk is the resource
        actually being protected.
        """
        request_id = str(payload.get("request_id") or "").strip()
        project_root = str(payload.get("project_root") or "").strip()
        filename = str(payload.get("filename") or "").strip()
        content_b64 = payload.get("content_b64") or ""

        async def _reply(
            *,
            ok: bool,
            path: str = "",
            error: str = "",
            error_code: str = "",
            size: int = 0,
            deduplicated: bool = False,
        ) -> None:
            """Send a result back if we have a request_id and a live ws."""
            if not request_id:
                return
            try:
                await self._send(
                    ws,
                    protocol.make_upload_result(
                        request_id,
                        ok=ok,
                        path=path,
                        error=error,
                        error_code=error_code,
                        size=size,
                        deduplicated=deduplicated,
                    ),
                )
            except Exception:
                logger.debug(
                    "Failed to send UPLOAD_RESULT for request %s",
                    request_id,
                    exc_info=True,
                )

        if not filename:
            logger.warning("Ignoring UPLOAD_COMMAND with empty filename")
            await _reply(
                ok=False,
                error="upload requires a non-empty filename",
                error_code=protocol.UPLOAD_ERR_INVALID_FILENAME,
            )
            return

        try:
            data = base64.b64decode(str(content_b64), validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            logger.warning("UPLOAD_COMMAND carried undecodable content: %s", exc)
            await _reply(
                ok=False,
                error="upload content is not valid base64",
                error_code=protocol.UPLOAD_ERR_INVALID_PAYLOAD,
            )
            return

        if len(data) > protocol.MAX_UPLOAD_BYTES:
            logger.warning(
                "UPLOAD_COMMAND: %d bytes exceeds the %d-byte limit",
                len(data),
                protocol.MAX_UPLOAD_BYTES,
            )
            await _reply(
                ok=False,
                error=(
                    f"upload of {len(data)} bytes exceeds the "
                    f"{protocol.MAX_UPLOAD_BYTES}-byte limit"
                ),
                error_code=protocol.UPLOAD_ERR_TOO_LARGE,
            )
            return

        if not project_root or not Path(project_root).is_absolute():
            logger.warning(
                "UPLOAD_COMMAND: project_root must be an absolute path, got %r",
                project_root,
            )
            await _reply(
                ok=False,
                error="project_root must be an absolute path",
                error_code=protocol.UPLOAD_ERR_INVALID_PATH,
            )
            return

        resolved = await self._resolve_registered_root(
            project_root, label="UPLOAD_COMMAND"
        )
        if resolved is None:
            await _reply(
                ok=False,
                error="project_root is not a registered project",
                error_code=protocol.UPLOAD_ERR_NOT_REGISTERED,
            )
            return

        handler = self._upload_handler
        if handler is None:
            logger.warning("UPLOAD_COMMAND received but no upload handler is wired")
            await _reply(
                ok=False,
                error="file uploads are not available",
                error_code=protocol.UPLOAD_ERR_UNSUPPORTED,
            )
            return

        # Writing up to MAX_UPLOAD_BYTES is blocking I/O that must never run on
        # the event loop, which is concurrently serving the status pushes and
        # heartbeats the whole web UI depends on.
        try:
            stored = await asyncio.to_thread(handler, resolved, filename, data)
        except Exception as exc:
            logger.warning(
                "UPLOAD_COMMAND failed for %s in %s: %s", filename, project_root, exc
            )
            # Only an UploadError carries a code we can relay; anything else is
            # an unexpected fault, reported as a write failure rather than
            # letting an unknown string reach make_upload_result's validator.
            code = str(getattr(exc, "code", "") or "")
            if code not in protocol.UPLOAD_ERROR_CODES:
                code = protocol.UPLOAD_ERR_WRITE_FAILED
            await _reply(
                ok=False,
                error=str(exc) or type(exc).__name__,
                error_code=code,
            )
            return

        # No fast push here, unlike ISSUE_COMMAND / PROJECT_COMMAND: an upload
        # adds a file the operator's prompt will reference but changes nothing
        # in the machine snapshot (no flow, call, issue or registry state), so
        # a push would ship an identical snapshot for nothing.
        await _reply(
            ok=True,
            path=str(getattr(stored, "path", "") or ""),
            size=int(getattr(stored, "size", 0) or 0),
            deduplicated=bool(getattr(stored, "deduplicated", False)),
        )
        logger.info(
            "UPLOAD_COMMAND stored %s in %s",
            getattr(stored, "path", "") or filename,
            project_root,
        )

    async def _handle_fetch_command(self, ws: Any, payload: Dict[str, Any]) -> None:
        """Hand one previously stored attachment back to the server.

        The read-back counterpart of :meth:`_handle_upload_command` and gated
        exactly like it: the target is re-validated against this daemon's own
        project registry before the injected ``fetch_handler``
        (``uploads.read_upload``) is invoked off the event loop, and every
        outcome travels back as a stable ``error_code`` the browser can act on.

        The gate is not redundant with the server's: the server is a separate
        trust domain reachable from the network, and this frame names a file on
        *this* machine's disk to be sent back over the wire. Containment inside
        the uploads directory is enforced one layer down, where a *resolved*
        path can be compared (see ``uploads.read_upload``).
        """
        request_id = str(payload.get("request_id") or "").strip()
        project_root = str(payload.get("project_root") or "").strip()
        rel_path = str(payload.get("path") or "").strip()

        async def _reply(
            *,
            ok: bool,
            content_b64: str = "",
            size: int = 0,
            name: str = "",
            error: str = "",
            error_code: str = "",
        ) -> None:
            """Send a result back if we have a request_id and a live ws."""
            if not request_id:
                return
            try:
                await self._send(
                    ws,
                    protocol.make_fetch_result(
                        request_id,
                        ok=ok,
                        content_b64=content_b64,
                        size=size,
                        name=name,
                        error=error,
                        error_code=error_code,
                    ),
                )
            except Exception:
                logger.debug(
                    "Failed to send FETCH_RESULT for request %s",
                    request_id,
                    exc_info=True,
                )

        if not rel_path:
            logger.warning("Ignoring FETCH_COMMAND with empty path")
            await _reply(
                ok=False,
                error="fetch requires a non-empty path",
                error_code=protocol.FETCH_ERR_INVALID_PATH,
            )
            return

        if not project_root or not Path(project_root).is_absolute():
            logger.warning(
                "FETCH_COMMAND: project_root must be an absolute path, got %r",
                project_root,
            )
            await _reply(
                ok=False,
                error="project_root must be an absolute path",
                error_code=protocol.FETCH_ERR_INVALID_PATH,
            )
            return

        resolved = await self._resolve_registered_root(
            project_root, label="FETCH_COMMAND"
        )
        if resolved is None:
            await _reply(
                ok=False,
                error="project_root is not a registered project",
                error_code=protocol.FETCH_ERR_NOT_REGISTERED,
            )
            return

        handler = self._fetch_handler
        if handler is None:
            logger.warning("FETCH_COMMAND received but no fetch handler is wired")
            await _reply(
                ok=False,
                error="file fetch is not available",
                error_code=protocol.FETCH_ERR_UNSUPPORTED,
            )
            return

        # Reading up to MAX_UPLOAD_BYTES is blocking I/O that must never run on
        # the event loop, which is concurrently serving the status pushes and
        # heartbeats the whole web UI depends on.
        try:
            content = await asyncio.to_thread(handler, resolved, rel_path)
        except Exception as exc:
            logger.warning(
                "FETCH_COMMAND failed for %s in %s: %s", rel_path, project_root, exc
            )
            # Only an UploadError carries a code we can relay; anything else is
            # an unexpected fault, reported as a read failure rather than
            # letting an unknown string reach make_fetch_result's validator.
            code = str(getattr(exc, "code", "") or "")
            if code not in protocol.FETCH_ERROR_CODES:
                code = protocol.FETCH_ERR_READ_FAILED
            await _reply(
                ok=False,
                error=str(exc) or type(exc).__name__,
                error_code=code,
            )
            return

        data = bytes(getattr(content, "data", b"") or b"")
        await _reply(
            ok=True,
            content_b64=base64.b64encode(data).decode("ascii"),
            size=len(data),
            name=str(getattr(content, "name", "") or ""),
        )
        logger.debug("FETCH_COMMAND served %s (%d bytes)", rel_path, len(data))

    def _drain_active(self, flow_id: str) -> bool:
        """Whether *flow_id* has an in-flight multi-frame full-pull drain.

        A non-blocking probe: the push loop uses it to skip a flow whose drain is
        running so its append frame is not emitted mid-rebuild (see
        ``self._history_draining``).
        """
        return flow_id in self._history_draining

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
        # WHY: a HISTORY_REQUEST is a ONE-SHOT on-demand pull — the server issues a
        # single request and caches whatever the reply carries; it does not
        # re-request while a reply is truncated, and an inactive flow is never
        # covered by the push loop's ``read_active_flows``. But ``read_flow`` is
        # byte-bounded (MAX_BYTES_PER_REPORT), so a flow whose history exceeds one
        # chunk (an archived session over a few hundred KB, still well under the
        # 2000-record cap that used to bound this) would answer with only its first
        # chunk. The server would then cache that prefix as the WHOLE history — its
        # cursor equals the truncation point, so the bundle looks self-consistent
        # and every later poll answers ``not_modified`` — silently truncating an
        # archived session's history pane forever. So the pull itself must drain
        # the entire backlog here: keep reading and sending chunks from the
        # advancing cursor until the read is no longer truncated. The first frame
        # is a full snapshot; each subsequent chunk is an append whose declared
        # ``cursor_base`` makes it contiguous, so the server extends one bundle.
        records_sent = 0
        frames_sent = 0
        # WHY: mark this flow draining for the WHOLE multi-frame drain so the push
        # loop (:meth:`_push_history`, a concurrent task) skips its append frames
        # for this flow. Otherwise a mid-drain push append lands past the server's
        # half-rebuilt water mark and trips the cursor-gap guard, restarting the
        # drain in a loop (see ``self._history_draining``). The try/finally makes
        # every exit — clean completion, read/send failure, or an unexpected raise
        # — release the marker so a flow is never left permanently skipped.
        self._history_draining.add(flow_id)
        # The drain's end-of-history water mark, captured on the last frame that
        # actually left the socket; ``None`` until at least one frame is sent.
        drain_cursor: Optional[Dict[str, int]] = None
        drain_completed = False
        try:
            while True:
                try:
                    # Disk I/O is offloaded to a thread so a large session's jsonl
                    # read cannot block the event loop past the server's pull
                    # timeout or the heartbeat-loss threshold (which would briefly
                    # mark the daemon offline and grey out the machine in the web
                    # UI).
                    read = await asyncio.to_thread(
                        provider.read_flow,
                        flow_id,
                        project_root=project_root,
                        cursor=cursor,
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
                            cursor_base=read.cursor_base,
                            usage=read.usage,
                            usage_catalog=read.usage_catalog,
                            # INVARIANT: this drain declares its own end. A
                            # truncated read means another frame of THIS reply
                            # follows, so the server may not settle the bundle on
                            # it — which is what makes a reply cut mid-drain
                            # (keepalive close, daemon restart) detectable
                            # instead of leaving a self-consistent prefix behind.
                            # The no-progress exit below is the one case where a
                            # frame declared ``final=False`` is nonetheless the
                            # last one we send; the server then treats the
                            # delivery as unfinished, which is the truth — the
                            # tail it was promised genuinely never arrives.
                            final=not read.truncated,
                            seq=self._next_seq(),
                        ),
                    )
                except Exception:
                    logger.debug("HISTORY_DATA send failed", exc_info=True)
                    return
                records_sent += len(read.records)
                frames_sent += 1
                # Only a frame that actually left the socket may advance the water
                # mark we later sync into the push cursor — a read/send failure
                # above returns without touching ``drain_cursor``.
                drain_cursor = read.cursor
                if not read.truncated:
                    drain_completed = True
                    break
                # A truncated read that failed to advance the cursor cannot make
                # progress (a reader bug, or a file that cannot move forward); stop
                # and let the delivered prefix stand rather than spin forever.
                if read.cursor == cursor:
                    logger.warning(
                        "HISTORY_REQUEST for flow %s truncated but cursor did not "
                        "advance (%s); stopping to avoid a spin",
                        flow_id, read.cursor,
                    )
                    drain_completed = True
                    break
                cursor = read.cursor
            # WHY: the drain is a cursorless full pull that advanced its OWN local
            # cursor to the server's true end-of-history water mark. Sync the push
            # loop's per-flow cursor to that end point so the NEXT push append's
            # ``cursor_base`` meets the server's water mark exactly — no cursor-gap,
            # no discard, no rebuild loop. Only on a clean drain completion (both
            # the not-truncated and the no-progress exits): a read/send failure
            # returned early above and must leave the push cursor untouched rather
            # than write a partially-drained (dirty) water mark that would itself
            # manufacture a gap.
            if drain_completed and drain_cursor is not None:
                self._history_cursors[flow_id] = dict(drain_cursor)
                # The drain's own closing frame settled the bundle, so any push
                # delivery this flow had open is closed too — drop the marker
                # rather than have the next push tick send a redundant
                # terminator for a delivery that no longer exists.
                self._history_push_open.discard(flow_id)
        finally:
            self._history_draining.discard(flow_id)
        logger.info(
            "HISTORY_REQUEST answered for flow %s (%d record(s) across %d frame(s))",
            flow_id, records_sent, frames_sent,
        )

    async def _handle_history_index_request(self, ws: Any) -> None:
        """Force a fresh rebuild + re-push of the history index.

        The server broadcasts :data:`~tianluo.daemon.protocol.MSG_HISTORY_INDEX_REQUEST`
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

    async def _handle_detail_request(self, ws: Any, payload: Dict[str, Any]) -> None:
        """Answer a server DETAIL_REQUEST with the full issue / call text.

        STATUS_UPDATE now carries only truncated issue descriptions and call
        prompts; when the operator opens a detail view the server routes a
        :data:`~tianluo.daemon.protocol.MSG_DETAIL_REQUEST` to the owning daemon,
        which reads the untruncated artifact off disk and replies with a
        :data:`~tianluo.daemon.protocol.MSG_DETAIL_DATA`.

        Failure is *always* reported as ``ok=false`` DETAIL_DATA rather than
        raising: a missing id, unreadable file, or read error must degrade to a
        visible "detail unavailable" on the web side, never tear down the
        connection. The disk read runs off the event loop (issue #243 A3) so a
        large call file / issue YAML parse never stalls heartbeats or pushes.
        """
        request_id = str(payload.get("request_id") or "").strip()
        kind = str(payload.get("kind") or "").strip()
        target_id = str(payload.get("target_id") or "").strip()
        project_root = str(payload.get("project_root") or "").strip()
        if kind not in protocol.DETAIL_KINDS:
            # A malformed request with no valid kind cannot be answered with a
            # well-formed DETAIL_DATA (its kind must be one of DETAIL_KINDS), so
            # there is nothing to correlate the reply to — log and drop.
            logger.warning("Ignoring DETAIL_REQUEST with unknown kind %r", kind)
            return

        async def _reply(
            *, ok: bool, detail: Optional[Dict[str, Any]] = None, error: str = ""
        ) -> None:
            try:
                await self._send(
                    ws,
                    protocol.make_detail_data(
                        request_id,
                        kind,
                        detail=detail,
                        ok=ok,
                        error=error,
                        seq=self._next_seq(),
                    ),
                )
            except Exception:
                logger.debug("DETAIL_DATA send failed", exc_info=True)

        if not target_id:
            await _reply(ok=False, error="missing target_id")
            return
        try:
            detail = await asyncio.to_thread(
                self._read_detail, kind, target_id, project_root
            )
        except Exception as exc:
            logger.debug(
                "DETAIL_REQUEST read failed for %s %s", kind, target_id,
                exc_info=True,
            )
            await _reply(ok=False, error=str(exc) or type(exc).__name__)
            return
        if detail is None:
            await _reply(ok=False, error=f"{kind} {target_id!r} not found")
            return
        await _reply(ok=True, detail=detail)
        logger.info("DETAIL_REQUEST answered: %s %s", kind, target_id)

    def _detail_root_candidates(self, project_root: str) -> List[str]:
        """Ordered project roots to search for a detail artifact.

        The server-supplied *project_root* (when present) is tried first; the
        roots the last STATUS_UPDATE reported are the fallback so a request that
        omits the root (or names a worktree copy) can still be resolved against
        the machine's known projects. Runs synchronously — it only assembles a
        small list — and is called from the worker thread in :meth:`_read_detail`.
        """
        roots: List[str] = []
        seen: Set[str] = set()
        for candidate in (project_root, *(self._last_known_project_roots or ())):
            candidate = str(candidate or "").strip()
            if candidate and candidate not in seen:
                seen.add(candidate)
                roots.append(candidate)
        return roots

    def _read_detail(
        self, kind: str, target_id: str, project_root: str
    ) -> Optional[Dict[str, Any]]:
        """Read one full-text detail artifact off disk (runs off-thread).

        Returns the full-text record dict, or ``None`` when the artifact cannot
        be located under any candidate root. Dispatches on *kind* to the issue
        (``IssueManager``) or call-file reader.
        """
        if kind == protocol.DETAIL_KIND_ISSUE:
            return self._read_issue_detail(target_id, project_root)
        if kind == protocol.DETAIL_KIND_CALL:
            return self._read_call_detail(target_id, project_root)
        return None  # pragma: no cover - kind validated by caller

    def _read_issue_detail(
        self, issue_id: str, project_root: str
    ) -> Optional[Dict[str, Any]]:
        """Load an issue's full record via :class:`IssueManager`.

        Tries each candidate root until the issue is found; returns its
        untruncated ``to_dict`` (full description) or ``None``.
        """
        from ..engine.issue_manager import IssueManager

        for root in self._detail_root_candidates(project_root):
            try:
                issue = IssueManager(Path(root)).load(issue_id)
            except Exception:
                logger.debug(
                    "issue detail load failed under %s", root, exc_info=True
                )
                continue
            if issue is not None:
                return issue.to_dict()
        return None

    def _read_call_detail(
        self, call_id: str, project_root: str
    ) -> Optional[Dict[str, Any]]:
        """Read a pending call's full file body (untruncated prompt).

        Locates the ``tianluo/calls/`` file whose stem is *call_id* (ignoring its
        ``.response`` answer siblings) under each candidate root and returns its
        parsed JSON body with the ``call_id`` folded in, or ``None``.
        """
        for root in self._detail_root_candidates(project_root):
            calls_dir = runtime_dir(Path(root)) / "calls"
            try:
                entries = sorted(calls_dir.iterdir())
            except OSError:
                continue
            for entry in entries:
                name = entry.name
                if name.endswith(".response") or name.endswith(".response.json"):
                    continue
                if entry.stem != call_id:
                    continue
                try:
                    body = json.loads(entry.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    logger.debug(
                        "call detail read failed for %s", entry, exc_info=True
                    )
                    return None
                if isinstance(body, dict):
                    detail = {"call_id": call_id, **body}
                    # Normalize the full text under the canonical ``prompt`` key
                    # even for legacy call files that stored it under ``message``
                    # or the discovery-call ``question`` field — mirroring the
                    # aggregator's _parse_call_file fallback chain. Without this,
                    # a legacy body carries no ``prompt`` key, the frontend reads
                    # null, and the clipped preview is never swapped for the full
                    # decision text.
                    if not isinstance(detail.get("prompt"), str):
                        fallback = body.get("message") or body.get("question")
                        if isinstance(fallback, str):
                            detail["prompt"] = fallback
                    return detail
                return {"call_id": call_id, "prompt": str(body)}
        return None

    # -- sending -----------------------------------------------------------

    async def _send(self, ws: Any, message: protocol.Message) -> None:
        """JSON-encode, send, then meter *message* on the socket.

        The encoded frame's byte length is recorded against its message type in
        :attr:`metrics` *after* ``ws.send`` returns, so a send that raises
        (connection closing / backpressured) is not counted as bytes that left
        the process — the per-type wire budget then reflects only frames that
        actually reached the socket, matching the server send paths. Metering is
        wrapped defensively inside :meth:`WireMetrics.record`, so accounting can
        never take down real traffic.
        """
        data = message.to_json()
        await ws.send(data)
        self.metrics.record(message.type, len(data.encode("utf-8")))

    def _peer_supports_traffic_reduction(self) -> bool:
        """Whether the connected server understands the revision-3 lean frames.

        Delegates to :func:`~tianluo.daemon.protocol.supports_traffic_reduction` on
        the peer version learned from WELCOME; ``None`` (pre-WELCOME / legacy)
        degrades to ``False`` so the client never emits a keepalive a peer would
        reject.
        """
        return protocol.supports_traffic_reduction(self._peer_protocol_version)

    async def _push_status(self, ws: Any, *, force: bool = False) -> None:
        """Build and send a STATUS_UPDATE, or a keepalive when unchanged.

        The aggregated snapshot is hashed (excluding its always-moving
        ``generated_at`` stamp). When *force* is false, the peer understands the
        revision-3 frames, and the hash matches the last full snapshot pushed,
        the client sends a tiny :data:`~tianluo.daemon.protocol.MSG_KEEPALIVE`
        carrying just that signature — enough for the server to refresh the
        daemon's last-seen time without re-shipping the whole snapshot. Otherwise
        a full STATUS_UPDATE is sent and the signature is refreshed. *force* is
        set on event-driven pushes (a call-file change / fast-push) that must
        deliver real state regardless of the gate.
        """
        try:
            # Building the snapshot walks ``tianluo/state`` and (via the aggregator's
            # all_project_roots → enumerate_historical_project_roots) the whole
            # ``tianluo/history`` tree, reading every ``_meta.json``. On a large
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
        signature = _status_signature(snapshot)
        if (
            not force
            and signature == self._last_status_sig
            and self._peer_supports_traffic_reduction()
        ):
            # Nothing changed since the last full snapshot and the server speaks
            # the lean protocol — send a keepalive rather than the whole snapshot.
            try:
                await self._send(
                    ws, protocol.make_keepalive(signature, seq=self._next_seq())
                )
            except Exception:
                logger.debug("KEEPALIVE send failed", exc_info=True)
            return
        message = protocol.make_status_update(snapshot, seq=self._next_seq())
        try:
            await self._send(ws, message)
            # Only advance the gate baseline once the full snapshot actually left
            # the socket, so a send failure re-sends a full snapshot next tick
            # rather than silently degrading to keepalives against stale state.
            self._last_status_sig = signature
        except Exception:
            # The receive loop will observe the closed socket and trigger a
            # reconnect; nothing more to do here.
            logger.debug("STATUS_UPDATE send failed", exc_info=True)

    async def _push_history(
        self, ws: Any, *, force_index: bool = False, status_tick: bool = False
    ) -> None:
        """Report the history index (delta or full) and push active-flow deltas.

        The index is diffed against the last push and only the changed meta rows
        travel — an incremental :data:`~tianluo.daemon.protocol.MSG_HISTORY_INDEX_DELTA`
        keyed by ``flow_id`` — so index traffic scales with the number of
        *changed* flows rather than the total flow count. A full
        :data:`~tianluo.daemon.protocol.MSG_HISTORY_INDEX` baseline is sent on
        (re)connect and on HISTORY_INDEX_REQUEST (*force_index*), and always to a
        legacy peer that does not understand the delta frame. *status_tick* marks
        the status-heartbeat tick, the only tick on which an updated_at-only meta
        change is allowed to flush (its throttle window).

        Active flows are read incrementally off ``self._history_cursors`` so each
        tick ships only the conversation lines appended since the previous push.
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
        await self._push_history_index(
            ws, index, force_index=force_index, status_tick=status_tick
        )
        try:
            # read_active_flows fans out into multiple jsonl reads; offload so a
            # big active session does not stall the event loop.
            reads = await asyncio.to_thread(
                provider.read_active_flows, self._history_cursors
            )
        except Exception:
            logger.exception("Active-flow history read failed")
            return
        # The cursor map tracks exactly the flows still producing records: every
        # active flow (always returned, even with an empty delta, so its cursor
        # keeps advancing) plus any terminal flow flushed one last time. Atomic
        # engine.json writes mean an active flow is never transiently missing, so
        # pruning cannot accidentally trigger a duplicate full re-read.
        #
        # INVARIANT: a flow's entry in ``self._history_cursors`` is the water mark
        # the PEER has received, never merely the one we have READ off disk. The
        # candidate cursors computed here are therefore only *committed* per flow
        # once that flow's frame has actually left the socket (or there was
        # nothing to send). WHY: the previous code installed the whole read-side
        # map up front and treated a send failure as a bare `log + return`, which
        # marked records as delivered that never reached the server — the next
        # round read past them and they were lost for the lifetime of the flow
        # (the "chat history is missing its first round" symptom, #287). Retaining
        # the old cursor makes ``read_flow`` fall back to a full re-read from that
        # water mark next round, re-sending the dropped batch; the resulting
        # overlap is absorbed by the server's history cache, whose append branch
        # folds each record onto the cached bundle by ``(step_id, ordinal)`` —
        # a re-delivered line is dropped, a rewritten one replaces its
        # predecessor in place — so a re-send can never double a record.
        candidates = {read.flow_id: read.cursor for read in reads}
        # Retain the cursor of a terminal flow that produced no records this
        # round but is still the live engine.json flow (e.g. a FAILED flow
        # awaiting `luo run --resume`). Without this it would drop out the round
        # after its final flush; a later resume would then find no cursor, force
        # a full re-read, and the web console would stay frozen on the failure
        # snapshot instead of receiving incremental appends. Bounded by the live
        # engine.json flows (one per root), so a fully-drained *and* archived
        # terminal flow still drops and the map stays bounded over a long run.
        # Offload: ``live_flow_ids`` reads each root's active engine.json, a disk
        # JSON parse that must never run on the event loop (#243 / #209 freeze).
        # It cannot rely on another reader having warmed the stat cache, because
        # the authoritative active-flow reads are deliberately un-cached fresh
        # reads, so this call would otherwise cache-miss and parse on the loop.
        resumable = await asyncio.to_thread(self._resumable_flow_ids, provider)
        previous = self._history_cursors
        committed: Dict[str, Dict[str, int]] = {
            flow_id: cursor
            for flow_id, cursor in previous.items()
            if flow_id not in candidates and flow_id in resumable
        }
        # WHY: a flow whose multi-frame full-pull drain (:meth:`_handle_history_request`)
        # is in flight must be skipped this tick — the drain is rebuilding the
        # server's bundle from a cursorless snapshot and advancing its own cursor,
        # so a push append computed off the stale push-side water mark would land
        # past the server's half-rebuilt mark and trip the cursor-gap guard, which
        # discards + rebuilds and re-triggers the drain (the WebUI chat-jump loop).
        # We SKIP (non-blocking) rather than block on the drain: a large drain runs
        # for tens of seconds, and blocking the shared push loop would stall the
        # STATUS_UPDATE heartbeat and every other flow's push for that whole window,
        # risking a false offline mark. A skipped flow keeps its old water mark
        # (``_keep_old``); the drain syncs the push cursor to its end point on
        # completion, and this flow's records are re-read on the next tick. Gated on
        # a non-empty draining set so the common (no-drain) path stays free.
        draining_now = (
            {read.flow_id for read in reads if self._drain_active(read.flow_id)}
            if self._history_draining
            else set()
        )
        # A flow with an empty delta and NO open delivery ships no frame, so its
        # candidate cursor is committed unconditionally — there is no delivery
        # that could fail. A flow whose last frame said "more is coming" ships an
        # empty terminator instead (see ``_history_push_open``), and is therefore
        # committed on the send path like any other frame. A draining flow is
        # exempt either way: its cursor is owned by the drain until it ends.
        for read in reads:
            if read.flow_id in draining_now:
                continue
            if not read.records and read.flow_id not in self._history_push_open:
                committed[read.flow_id] = read.cursor

        def _keep_old(flow_id: str) -> None:
            """Preserve *flow_id*'s pre-send water mark so the batch is re-read."""
            old = previous.get(flow_id)
            if old is not None:
                committed[flow_id] = old

        def _commit_history_cursors() -> None:
            """Install ``committed`` as the live push-cursor map without clobbering
            a cursor a concurrent drain synced onto the live attribute this tick.

            WHY: a flow's multi-frame drain (:meth:`_handle_history_request`) owns
            its cursor and, on completion, writes the end-of-history water mark
            DIRECTLY onto ``self._history_cursors`` — which may happen while this
            push tick is awaiting inside the send loop. Because we rebind the whole
            map wholesale, that fresh drain cursor must be re-read from the live map
            for every draining flow immediately before the rebind; otherwise the
            synced value is reverted to this tick's stale pre-drain snapshot, and the
            next push re-reads the whole just-drained batch from behind the server's
            water mark on every tick until the flow happens to re-sync. Read of the
            live map and the rebind are synchronous (no await between them), so they
            are atomic against the drain task. ``draining_now`` is consulted at call
            time, so a flow that only began draining mid-send-loop (added below) is
            covered too.
            """
            live = self._history_cursors
            for flow_id in draining_now:
                synced = live.get(flow_id)
                if synced is not None:
                    committed[flow_id] = synced
            self._history_cursors = committed

        # Hold back draining flows' old water marks so their records are re-read
        # after the drain ends, and drop them from the send list below.
        for flow_id in draining_now:
            _keep_old(flow_id)
        pending = [
            read
            for read in reads
            if (read.records or read.flow_id in self._history_push_open)
            and read.flow_id not in draining_now
        ]
        # Close deliveries whose flow left the read set entirely. A flow that
        # went TERMINAL is surfaced by ``read_active_flows`` only while its final
        # flush still has records, so the tick after a final flush that ended
        # exactly ON the chunk bound produces no read for it at all — and that is
        # precisely the flow whose delivery is open. Synthesize the empty
        # terminator at the water mark we last committed for it: it carries no
        # records, declares the empty window ``[cursor, cursor)`` so the server's
        # gap check reads it as contiguous by construction, and says ``final`` so
        # the bundle settles. Without it a COMPLETE bundle stays flagged
        # incomplete for the rest of the server's uptime.
        seen = {read.flow_id for read in reads}
        for flow_id in sorted(self._history_push_open - seen - draining_now):
            cursor = previous.get(flow_id)
            if cursor is None:
                # Nothing left to anchor a terminator to (the cursor was pruned):
                # the flow's next sighting is a full snapshot, which settles the
                # bundle on its own.
                self._history_push_open.discard(flow_id)
                continue
            pending.append(
                history.FlowRead(
                    flow_id=flow_id,
                    mode=protocol.HISTORY_MODE_APPEND,
                    records=[],
                    cursor=dict(cursor),
                    cursor_base=dict(cursor),
                )
            )
        for position, read in enumerate(pending):
            # Re-check drain state immediately before THIS flow's send. WHY: the
            # tick's ``draining_now`` was frozen before the send loop; a server
            # HISTORY_REQUEST may have arrived while we awaited an EARLIER flow's
            # send and started a drain for this (later) pending flow. Its append
            # frame was already computed off the stale push-side cursor, so letting
            # it out now would land past the server's half-rebuilt water mark and
            # trip the cursor-gap guard mid-drain — the very interleave the drain
            # marker exists to forbid. Skip it: the drain owns the cursor and syncs
            # it on completion, and these records are re-read next tick. Fold the
            # flow into ``draining_now`` so ``_commit_history_cursors`` preserves the
            # drain's synced cursor for it as well.
            if self._drain_active(read.flow_id):
                draining_now.add(read.flow_id)
                _keep_old(read.flow_id)
                continue
            # HOP-3 DEBUG (daemon→server send): the actual MSG_HISTORY_DATA frame
            # leaving the daemon. If this line appears for the analyze records but
            # the UI never renders them, the drop is downstream (server
            # append/broadcast or frontend); if it never appears, the drop is
            # upstream (change-detection or read).
            logger.debug(
                "hist-diag _push_history SEND flow=%s mode=%s records=%d cursor=%s",
                read.flow_id, read.mode, len(read.records), read.cursor,
            )
            try:
                await self._send(
                    ws,
                    protocol.make_history_data(
                        read.flow_id,
                        read.mode,
                        read.records,
                        cursor=read.cursor,
                        cursor_base=read.cursor_base,
                        usage=read.usage,
                        usage_catalog=read.usage_catalog,
                        # A byte-bounded catch-up chunk has backlog behind it
                        # (see the fast-push re-arm below), so it is no more a
                        # complete delivery than a drain's middle frame: say so,
                        # and the server keeps the bundle unsettled until the
                        # chunk that finally clears the backlog arrives.
                        final=not read.truncated,
                        seq=self._next_seq(),
                    ),
                )
            except Exception:
                logger.debug(
                    "hist-diag HISTORY_DATA send failed flow=%s cursor=%s "
                    "(cursor NOT advanced; batch will be re-read next round)",
                    read.flow_id, read.cursor, exc_info=True,
                )
                # The socket is down: this flow and every flow still queued behind
                # it keep their old water mark, so the next round re-reads and
                # re-sends them. Flows already sent this round stay committed —
                # rolling them back would only manufacture duplicate traffic.
                for unsent in pending[position:]:
                    _keep_old(unsent.flow_id)
                _commit_history_cursors()
                return
            # The frame is on the wire, so the server now holds exactly what its
            # ``final`` bit declared: a bounded chunk leaves a delivery open (and
            # obliges the next tick to close it — an empty read for this flow is
            # then shipped as the terminator above), an unbounded one settles it.
            if read.truncated:
                self._history_push_open.add(read.flow_id)
            else:
                self._history_push_open.discard(read.flow_id)
            committed[read.flow_id] = read.cursor
        _commit_history_cursors()
        # A bounded chunk was emitted for at least one flow that still has
        # backlog on disk past its cursor (read.truncated). Re-arm fast-push so
        # the next chunk goes out on the very next fast tick instead of waiting
        # out a possibly idle-geared cadence.
        #
        # WHY: byte-bounded chunking (see MAX_BYTES_PER_REPORT) makes each frame
        # small enough to clear a short-lived, proxy-throttled connection window,
        # but a multi-MB backlog still needs many chunks. If the catch-up rate is
        # throttled by the presence idle gear (viewers==0 slows the tick to tens
        # of seconds), draining a large backlog would take an impractically long
        # time. Re-arming fast-push on every truncated round keeps the drain rate
        # bound to the link's usable bandwidth — each bounded chunk still leaves
        # the socket independently, so a mid-drain disconnect only costs the
        # in-flight chunk (its cursor was never committed) and the next window
        # resumes from the last confirmed point. Reached only on the fully-sent
        # path; the send-failure branch above returns early (socket is down, and
        # a reconnect re-drives the push regardless).
        if any(read.truncated for read in reads):
            self._trigger_fast_push()

    async def _push_history_index(
        self,
        ws: Any,
        index: list,
        *,
        force_index: bool,
        status_tick: bool,
    ) -> None:
        """Push the history index as a full baseline or an incremental delta.

        A full :data:`~tianluo.daemon.protocol.MSG_HISTORY_INDEX` is sent when
        *force_index* is set (connect / reconnect / HISTORY_INDEX_REQUEST), when
        no baseline has been primed yet this session, or when the peer is a legacy
        server that does not understand the delta frame — in the legacy/unprimed
        case it is still debounced to a genuine change. Otherwise only the changed
        meta rows travel as a delta (see :meth:`_compute_index_delta`).
        """
        current = {m["flow_id"]: m for m in index if m.get("flow_id")}
        # Delta is safe only against a peer that advertised support AND a baseline
        # it already holds. force_index deliberately re-establishes that baseline.
        send_full = force_index or not self._peer_supports_reduction or not self._index_primed
        if send_full:
            # Debounce the baseline: a forced re-push always fires (the server's
            # waiter must be resolved), but an unchanged index on an ordinary tick
            # is not re-sent.
            if force_index or index != self._last_index:
                try:
                    await self._send(
                        ws, protocol.make_history_index(index, seq=self._next_seq())
                    )
                except Exception:
                    logger.debug("HISTORY_INDEX send failed", exc_info=True)
                    return
                # Advance the by-flow baseline only once the full frame left the
                # socket, so a send failure re-pushes a full baseline next tick
                # rather than leaving the server believing it holds rows it never
                # received. Priming here means the very next delta diffs against
                # exactly what the server now holds.
                self._last_index = index
                self._last_index_by_flow = dict(current)
                self._index_primed = True
            return
        upserts, removed = self._compute_index_delta(current, status_tick=status_tick)
        if not upserts and not removed:
            return
        try:
            await self._send(
                ws,
                protocol.make_history_index_delta(
                    upserts, removed, seq=self._next_seq()
                ),
            )
        except Exception:
            logger.debug("HISTORY_INDEX_DELTA send failed", exc_info=True)
            return
        # Only advance the baseline after the delta actually left the socket, so a
        # transient send failure re-emits the same upserts/removals next tick
        # instead of silently desyncing the server's mirror.
        last = self._last_index_by_flow
        for meta in upserts:
            flow_id = meta.get("flow_id")
            if flow_id:
                last[flow_id] = meta
        for flow_id in removed:
            last.pop(flow_id, None)

    def _compute_index_delta(
        self, current: Dict[str, Dict[str, Any]], *, status_tick: bool
    ) -> Tuple[list, list]:
        """Diff *current* meta rows against the last-pushed baseline.

        Returns ``(upserts, removed)`` where *upserts* are the meta dicts that are
        new or substantively changed since the last push and *removed* the
        flow_ids that disappeared. A meta whose *only* change is an updated_at-only
        liveness tick (see :func:`~tianluo.daemon.history.meta_change_is_throttleable`)
        is held back on a non-status tick — its baseline entry is left untouched so
        the next status tick re-detects and flushes it, capping such churn to the
        heartbeat cadence. This method is **pure**: it does not mutate
        ``self._last_index_by_flow``. The caller advances the baseline only after
        the delta frame is successfully sent, so a transient send failure re-emits
        the missed rows on the next tick rather than desyncing the server mirror.
        """
        last = self._last_index_by_flow
        upserts: list = []
        for flow_id, meta in current.items():
            prev = last.get(flow_id)
            if prev == meta:
                continue
            if (
                prev is not None
                and not status_tick
                and history.meta_change_is_throttleable(meta, prev)
            ):
                continue
            upserts.append(meta)
        removed = [flow_id for flow_id in list(last) if flow_id not in current]
        return upserts, removed

    def _resumable_flow_ids(self, provider: Any) -> Set[str]:
        """Return flow_ids still resumable from disk (live ``engine.json`` flows).

        Delegates to the provider's ``live_flow_ids`` when available so the
        cursor rebuild in :meth:`_push_history` can retain a final-flushed
        terminal flow's cursor (a FAILED flow awaiting ``luo run --resume``).
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


def _status_signature(snapshot: Dict[str, Any]) -> str:
    """Return a stable content hash of a machine-status *snapshot*.

    The snapshot's ``generated_at`` wall-clock stamp is excluded — it moves on
    every build, so hashing it would defeat the whole "unchanged since last
    push?" gate. Everything else (flows, truncated issues, machine-wide calls,
    project roots) is serialized deterministically (``sort_keys``) and hashed, so
    two builds of genuinely identical state produce the same signature and the
    push loop can send a keepalive instead of the full snapshot. Serialization
    failures degrade to ``repr`` so a signature is always produced (a spurious
    "changed" verdict is safe — it only forces a full push).
    """
    if isinstance(snapshot, dict):
        payload = {k: v for k, v in snapshot.items() if k != "generated_at"}
    else:  # pragma: no cover - snapshot provider always returns a dict
        payload = snapshot
    try:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        blob = repr(payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _default_respond_handler(call_id: str, project_root: str, response: Any) -> None:
    """Write a human-call response file under ``<project_root>/tianluo/calls/``.

    SE3's ``tianluo/calls/`` directory is the human-call queue; writing a
    ``<call_id>.response.json`` file there is how a server-delivered response
    re-enters a paused flow. The file is written atomically (temp + rename).
    """
    root = Path(project_root).resolve() if project_root else Path.cwd()
    calls_dir = runtime_dir(root) / "calls"
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
    """Write a mid-flow interjection request file under ``tianluo/calls/``.

    A server-delivered :data:`~tianluo.daemon.protocol.MSG_INTERJECT_FLOW` becomes
    an ``interjection``-kind call file; the running ``luo run`` process drains
    it at the next step boundary and folds it into ``user_interjections``.
    """
    from ..engine.interaction_calls import calls_dir_for, write_interjection_request

    root = Path(project_root).resolve() if project_root else Path.cwd()
    write_interjection_request(calls_dir_for(root), text, flow_id=flow_id)
