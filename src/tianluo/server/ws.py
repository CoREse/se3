"""The SE3 central server's WebSocket endpoint and daemon connection pool.

Daemons dial the server's ``/ws`` endpoint and speak the protocol defined in
:mod:`tianluo.daemon.protocol`. This module owns:

* :class:`ConnectionManager` — the ``machine_id -> WebSocket`` pool, plus the
  downlink routing used by the REST API to push ``SPAWN_FLOW`` / ``RESPOND_CALL``
  instructions to a specific daemon;
* :func:`handle_daemon_connection` — the per-connection coroutine: validate the
  opening ``HELLO``, answer with ``WELCOME``, then run the receive loop and a
  heartbeat ``PING``/``PONG`` loop until the socket closes.

The server imports the protocol module straight from the core ``tianluo.daemon``
package, so the wire schema has a single source of truth shared by both ends.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections import deque
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    Optional,
    Tuple,
)

from tianluo.daemon import protocol
from tianluo.daemon.history import (
    MAX_BYTES_PER_REPORT,
    MAX_RECORDS_PER_REPORT,
)
from tianluo.daemon.wire_metrics import WireMetrics

from .history_summary import summarize_history_records
from .state import ServerState, records_reach_bytes

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .identity import IdentityService

#: WS event type pushed to ``/ws/ui`` clients when an interjection chip's
#: lifecycle phase changes. Older frontends that do not recognise this type
#: simply ignore it (the standard "unknown ``type`` -> no-op" rule for the
#: ``/ws/ui`` channel), so introducing the event is backward-compatible.
UI_EVENT_INTERJECTION = "interjection_event"

#: WS event type pushed to ``/ws/ui`` clients when a daemon reports that a
#: server-dispatched spawn / resume / project-init failed (a
#: :data:`~tianluo.daemon.protocol.MSG_SPAWN_FAILED`). The web console turns the
#: published task's pseudo-success into a visible error showing the reason.
#: Older frontends ignore the unknown ``type`` (backward-compatible).
UI_EVENT_SPAWN_FAILED = "spawn_failed"

#: Lifecycle phases emitted on :data:`UI_EVENT_INTERJECTION`. ``pending`` is
#: the moment the interjection call file appears in ``tianluo/calls/`` (the
#: server saw a brand-new interjection-kind ``call_id`` in a flow's
#: ``pending_calls`` snapshot); ``consumed`` is the moment that ``call_id``
#: disappears (the running ``luo run`` drained the file).
INTERJECTION_PHASE_PENDING = "pending"
INTERJECTION_PHASE_CONSUMED = "consumed"

logger = logging.getLogger(__name__)


# Heartbeat tuning (seconds).
# WHY: widened from 15/45 to 20/90 to tolerate lossy/high-latency daemon links
# (e.g. node007). Tighter thresholds evicted daemons on transient PONG loss ~every
# 45s, and each eviction truncated an in-flight full history reload. This pairs
# with the daemon's ping_timeout=60: a truly dead connection is still reclaimed
# within ~90s, while presence 60s debounce + incremental gap backfill absorb the
# brief reconnect windows so the WebUI shows no flap and never loses records.
PING_INTERVAL = 20.0
#: A daemon is dropped if no PONG (or any frame) arrives within this window.
HEARTBEAT_TIMEOUT = 90.0

#: Inbound-frame size above which the receive loop logs that the daemon-side
#: chunk bound was exceeded. It is a TRIPWIRE, not a cap: the frame is still
#: decoded and applied, because dropping history the daemon has already
#: committed its cursor past would punch a permanent hole in the bundle.
#:
#: WHY a tripwire is the right shape here: the inbound parse is bounded AT THE
#: SOURCE, not on this side. ``daemon.history.MAX_BYTES_PER_REPORT`` caps every
#: ``MSG_HISTORY_DATA`` at 256 KiB (a full pull of a large flow drains as a
#: bounded ``full`` head plus bounded ``append`` tails), and a single oversized
#: record is compacted by the daemon before it is billed against that cap — so
#: the frames this loop actually parses cost well under a millisecond. If that
#: source-side bound ever regresses, a silent 40 ms loop freeze per frame is
#: exactly the kind of stall that is impossible to attribute after the fact;
#: this line names the machine and the size instead.
LARGE_FRAME_WARN_BYTES = 4 * 1024 * 1024

#: Rendered-JSON bytes one batch of :func:`dump_json_chunked` aims to produce
#: before it yields to the event loop. ~0.5 MiB renders in single-digit
#: milliseconds on this host; a smaller budget would cap the gap tighter but pay
#: more scheduler round-trips over the whole render.
#:
#: WHY the budget is in BYTES and not in records: history records are
#: heavy-tailed. Sampling every record under this repo's own ``tianluo/history/``
#: gives mean 40.7 KB, p90 12.3 KB, p99 1.1 MB, max 161 MB — so a record count
#: sized on an assumed "~4 KB/record" is not a size bound at all. A real bundle
#: here (``02097bb3-73e/58f7f446.jsonl``, 10 records / 11.0 MiB) fits inside any
#: three-digit record batch: the render would reach no ``await`` and freeze the
#: loop for the full ~110 ms, exactly like the inline ``json.dumps`` this
#: function exists to replace.
JSON_RENDER_BATCH_BYTES = 512 * 1024

#: Ceiling on records per batch. The byte budget is what bounds a batch's cost;
#: this only keeps a stream of very small records from growing one batch's slice
#: (and its transient list copy) without limit when the budget alone would allow
#: tens of thousands.
JSON_RENDER_MAX_BATCH_RECORDS = 1024

#: How fast the adaptive batch size may GROW per step (it may shrink to the
#: budget in one). WHY asymmetric: the size for the next batch is predicted from
#: what the previous ones actually rendered, and the tail is on the heavy side —
#: overshooting is a loop stall, undershooting is one extra scheduler
#: round-trip. Capped growth reaches the steady-state size in a handful of
#: batches (1, 4, 16, 64 …) while keeping a run of small records from launching
#: the size straight past the next heavy one.
JSON_RENDER_BATCH_GROWTH = 4

#: Record count at or above which a ``/ws/ui`` fan-out frame is serialized in
#: record batches (see :func:`dump_json_chunked`) rather than in one call.
#: Aligned with ``app.HISTORY_RESPONSE_OFFLOAD_RECORDS``: the two render the same
#: records, so the point where the render outgrows a single uninterrupted pass is
#: the same for both.
UI_FRAME_CHUNKED_RECORDS = 200

#: Estimated payload size at or above which a ``/ws/ui`` fan-out frame takes the
#: batched render however FEW records it holds. Aligned with
#: ``app.HISTORY_RESPONSE_OFFLOAD_BYTES`` for the same reason the record gate is
#: aligned: it is the same bundle, relayed rather than served.
UI_FRAME_CHUNKED_BYTES = 1024 * 1024

#: How many event-loop turns a fan-out gives a client to accept the frame it was
#: just offered before moving on and leaving the rest to that client's own
#: outbound task (see :class:`_UiClientChannel`).
#:
#: WHY a turn budget and not a wall-clock grace: the point of the budget is to
#: keep "the broadcast returned" meaning "the frame is on the wire" for a client
#: whose send completes WITHOUT waiting on I/O — which is every in-process
#: consumer, so the hub's delivery stays observable synchronously and the fan-out
#: keeps its old pacing. A wall-clock grace G would instead be charged per frame
#: — ~150 * min(G, that client's per-frame cost) over one big flow's drain, which
#: is the very cost this change exists to remove, and no G both survives a loaded
#: CI worker and stays cheap for a slow link. Turns cost microseconds and are
#: independent of the client's link, so a slow console is billed nothing.
UI_FANOUT_HANDOFF_TURNS = 4

#: Bounds on ONE client's undelivered outbound backlog. A queue that a slow
#: console never drains is a memory leak, so the backlog is capped on both axes
#: (a relayed ``history_data`` frame of a big flow is ~0.7 MB, ordinary frames a
#: few KB) and the overflow is DROPPED rather than waited on.
UI_CLIENT_QUEUE_MAX_FRAMES = 64
UI_CLIENT_QUEUE_MAX_BYTES = 8 * 1024 * 1024

#: Frame types a lagging client may lose without losing correctness. A
#: ``status_update`` is the WHOLE machine list, so the newest one supersedes
#: every older one outright: shedding a superseded copy is a coalesce, not a
#: loss. (The connect-time ``snapshot`` is deliberately NOT in this set even
#: though it has the same shape — it is the baseline a console renders before any
#: update arrives.)
#:
#: INVARIANT: NOTHING ELSE IS EVER DROPPED — a frame that belongs to a history
#: DELIVERY least of all. A ``history_data`` frame used to be shed here on the
#: reasoning that the next frame's ``cursor`` / ``signature`` / ``pending`` let
#: the console detect the hole and backfill it. That reasoning does not survive
#: the delivery contract this module now implements: every delivery ends with an
#: explicit statement (``incomplete: false`` on its final frame, or on the
#: records-less ``history_cursor`` advisory that stands in for it), and a shed
#: middle frame leaves the LATER, completeness-declaring frame to arrive intact —
#: so the console would be told the delivery is whole while holding records it
#: never got. A completeness statement that can be false is worth less than none,
#: because the consumer stops repairing on the strength of it. Losing a frame of
#: a delivery must therefore be visible: the backlog is kept whole, and a client
#: too far behind to keep it is DISCONNECTED at :data:`UI_CLIENT_QUEUE_HARD_FRAMES`
#: (see :meth:`_UiClientChannel._trim`) — the one loss the frontend already
#: repairs, since ``ws.onclose`` marks the view stale and its reconnect re-reads
#: the whole bundle. The same holds, for a plainer reason, for the one-shot events
#: (``spawn_failed``, ``interjection_event``): no cursor carries them and no poll
#: re-derives them, so a console that loses one stays CONNECTED and simply never
#: applies it.
UI_DROPPABLE_FRAME_TYPES = frozenset({"status_update"})

#: Hard ceiling on a backlog that :data:`UI_DROPPABLE_FRAME_TYPES` cannot shrink.
#: Reaching it means the client has fallen far enough behind that keeping its
#: unsheddable frames in memory is no longer the lesser evil, so the connection
#: is dropped and the browser resynchronises from scratch on reconnect. Set well
#: above the soft bound: the soft bound is the steady-state working set of a
#: momentarily lagging console, and only a client that is effectively dead
#: reaches this one. It is also the ceiling a slow console's relayed history now
#: accumulates against, since none of those frames may be shed — 32 MiB of
#: already-rendered text per stuck client, freed the moment it is retired.
UI_CLIENT_QUEUE_HARD_FRAMES = UI_CLIENT_QUEUE_MAX_FRAMES * 8
UI_CLIENT_QUEUE_HARD_BYTES = UI_CLIENT_QUEUE_MAX_BYTES * 4


async def dump_json_chunked(payload: Dict[str, Any], **dumps_kwargs: Any) -> bytes:
    """Serialize *payload* to JSON bytes, yielding to the loop between batches.

    WHY this exists rather than ``await asyncio.to_thread(json.dumps, ...)``:
    a thread hop does NOT free the event loop here. CPython's C JSON encoder
    holds the GIL for nearly its whole run, so the loop thread wakes on a socket
    event and then blocks on the GIL until the worker finishes. Measured on this
    host on a 16 MiB history payload (idle loop, worst lateness of a 5 ms timer):
    inline ``json.dumps`` ~77 ms, the same call in a worker thread ~99 ms — the
    hop buys nothing and costs a round-trip. Rendering the records in batches and
    awaiting between them actually removes the stall: ~15 ms worst lateness for
    the same payload, at a few percent more total render time. Reproduce with
    ``scripts/measure_server_loop_stalls.py`` (its ``loop lateness`` table).

    Only the ``records`` list is batched — it is the only key whose size scales
    with the conversation. Batches are cut to a BYTE budget
    (:data:`JSON_RENDER_BATCH_BYTES`), not to a record count: a fixed count is
    not a size bound on records whose sizes span five orders of magnitude, and a
    batch that swallows the whole payload reaches no ``await`` and leaves the
    stall exactly where it was.

    INVARIANT: the bytes are identical to ``json.dumps(payload, **dumps_kwargs)``
    — key order included. This is a SCHEDULING change and must never become a
    content change: the same payload is served by the inline path for small
    replies and by this one for big ones, and a client (or a test) must not be
    able to tell which ran. That is why the surrounding keys are split at
    ``records``' own position instead of being emitted around it.
    """
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return json.dumps(payload, **dumps_kwargs).encode("utf-8")
    item_sep, key_sep = dumps_kwargs.get("separators") or (", ", ": ")
    item_sep_b = item_sep.encode("utf-8")
    keys = list(payload)
    split = keys.index("records")
    head_keys, tail_keys = keys[:split], keys[split + 1:]

    def _dump(value: Any) -> bytes:
        return json.dumps(value, **dumps_kwargs).encode("utf-8")

    # Chunks are collected and joined once at the end rather than appended into
    # a growing ``bytearray``: on a large bundle the incremental reallocs plus
    # the final ``bytes()`` copy are themselves an uninterruptible cost paid ON
    # the loop, and they land in the middle of the render where nothing can
    # yield around them. Measured on a 112 MiB payload (worst lateness of a 5 ms
    # timer): ~139 ms accumulating into a bytearray vs ~97 ms joining once.
    parts = []
    # ``{k:v,`` — the object opened, its pre-``records`` members already in it.
    parts.append(_dump({key: payload[key] for key in head_keys})[:-1])
    if head_keys:
        parts.append(item_sep_b)
    parts.append(_dump("records") + key_sep.encode("utf-8") + b"[")
    # Batch size is PREDICTED from what the previous batches actually rendered,
    # because the only way to know a record's serialized size is to serialize
    # it, and doing that one record at a time costs ~5x the bulk encode on a
    # small-record bundle. Starting at one record makes the first batch — the
    # one no measurement covers yet — the smallest slice this can yield on, so
    # a bundle that is entirely multi-MB records still releases the loop between
    # every record instead of rendering in a single frozen pass.
    #
    # The residual overshoot is a batch sized on light records that then hits a
    # heavy one; the growth cap bounds it, and one record is the floor no
    # predictor can go below (a single 161 MB record is one ``json.dumps``
    # whatever the budget says).
    size = 1
    index = 0
    while index < len(records):
        batch_records = records[index:index + size]
        batch = _dump(batch_records)
        if index:
            parts.append(item_sep_b)
        # Strip the batch's own ``[``/``]`` so the batches concatenate into one
        # array rather than nesting.
        parts.append(batch[1:-1])
        index += len(batch_records)
        per_record = max(1, (len(batch) - 2) // len(batch_records))
        size = max(
            1,
            min(
                JSON_RENDER_BATCH_BYTES // per_record,
                JSON_RENDER_MAX_BATCH_RECORDS,
                size * JSON_RENDER_BATCH_GROWTH,
            ),
        )
        await asyncio.sleep(0)
    parts.append(b"]")
    tail = _dump({key: payload[key] for key in tail_keys})
    # ``tail`` is ``{...}``; splicing off its leading brace appends its members
    # to the object already open. An empty tail just closes it.
    parts.append((item_sep_b + tail[1:]) if tail_keys else b"}")
    return b"".join(parts)


async def _decode_frame(raw: str) -> "protocol.Message":
    """Decode one inbound daemon frame on the event loop.

    WHY the parse is NOT handed to a worker thread: it would not help. CPython's
    C JSON scanner holds the GIL for its whole run, so the loop thread is blocked
    on the GIL for as long as the parse takes wherever the call runs. Measured on
    this host on a 16 MiB frame (idle loop, worst lateness of a 5 ms timer):
    ~48 ms inline vs ~42 ms in a worker thread — the hop adds latency and leaves
    the stall. And unlike the outbound render, an inbound parse cannot be batched
    into an incremental pass without replacing the C scanner with a pure-Python
    one that is an order of magnitude slower overall.

    What actually bounds this cost is the daemon side: history frames are
    byte-chunked at ``daemon.history.MAX_BYTES_PER_REPORT`` (256 KiB), which is a
    sub-millisecond parse. :data:`LARGE_FRAME_WARN_BYTES` is the tripwire that
    makes a regression of that bound visible instead of silent.

    Its own function so the receive loop's decode has a seam a test can assert
    on directly instead of only through timing.
    """
    if len(raw) >= LARGE_FRAME_WARN_BYTES:
        logger.warning(
            "Inbound frame of %d bytes exceeds the %d-byte daemon chunk bound; "
            "its parse blocks the event loop (see daemon.history."
            "MAX_BYTES_PER_REPORT)",
            len(raw),
            LARGE_FRAME_WARN_BYTES,
        )
    return protocol.decode(raw)


def _billed_bytes(record: Any) -> int:
    """The byte size the daemon's read budget charged for *record*.

    ``read_flow`` bills each record its post-compaction WIRE size, which for the
    ~99 % uncompacted path is the on-disk jsonl line — and that line is exactly
    ``json.dumps(message, ensure_ascii=False)`` as ``chat_history`` wrote it. So
    re-serializing the message the frame carried reproduces the daemon's own
    figure rather than approximating it.
    """
    if not isinstance(record, dict):
        return 0
    message = record.get("message")
    if not isinstance(message, dict):
        message = record
    try:
        return len(
            json.dumps(message, ensure_ascii=False, default=str).encode("utf-8")
        )
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return 0


def _frame_is_chunk_bounded(records: Any) -> bool:
    """Whether a ``HISTORY_DATA`` frame was cut short by the daemon's chunk bound.

    ``read_flow`` stops at :data:`~tianluo.daemon.history.MAX_RECORDS_PER_REPORT`
    records or :data:`~tianluo.daemon.history.MAX_BYTES_PER_REPORT` bytes and
    reports the read as truncated; the pull handler then keeps reading and
    sending from the advancing cursor for exactly as long as that keeps
    happening. So a frame AT the bound says "more of this reply is coming" and a
    frame under it is the reply's last — which is the only thing the wire says
    about a multi-frame reply, since nothing in the daemon→server protocol (which
    this split leaves untouched) carries the truncation flag itself.

    INVARIANT: the comparison is made on the daemon's OWN billing basis (see
    :func:`_billed_bytes`), never on the encoded frame's size. A frame wraps the
    billed lines in protocol and record-envelope bytes the budget never counted,
    so a final untruncated read sitting just under the cap encodes to just over
    it — read as "still draining", that left the marker open and consumed the
    next genuine live append as the reply's closing frame.

    :func:`~tianluo.server.state.records_reach_bytes` runs first as a cheap
    OVER-estimating pre-filter: it is far quicker than a re-serialization and
    can only over-count, so a frame it puts under the cap is under it for
    certain and the exact pass is skipped.
    """
    if not isinstance(records, list):
        return False
    if len(records) >= MAX_RECORDS_PER_REPORT:
        return True
    if not records_reach_bytes(records, MAX_BYTES_PER_REPORT):
        return False
    total = 0
    for record in records:
        total += _billed_bytes(record)
        if total >= MAX_BYTES_PER_REPORT:
            return True
    return False


def _mid_reply_tail(verdict: Any) -> bool:
    """Whether this frame is a NON-final tail of a reply already served by REST.

    Such a frame gets no cursor advisory, and that is a deliberate exception to
    "a suppressed frame still announces that the bundle moved".

    WHY: the advisory drives the client's completeness self-check, and a client
    whose records stop at the reply's head sees a gap of thousands of numbers —
    past ``MISSING_MAX_ORDINALS``, so the numbered backfill cannot express it and
    the check escalates to a token-less FULL re-pull of the whole bundle. Firing
    that while the very records it is missing are already being delivered to it
    — by the REST body it asked for plus the token-pinned poll that follows —
    turns a saved 100 MB relay into a re-downloaded bundle. So the reply speaks
    once it is settled: its head announces itself as before, its closing frame
    carries the final cursor, and everything in between is a water mark that was
    obsolete before the browser could act on it.
    """
    return bool(
        getattr(verdict, "rest_served", False)
        and not getattr(verdict, "closing", False)
    )


def _render_ui_frame(payload: Dict[str, Any]) -> str:
    """Serialize one ``/ws/ui`` frame in a single pass."""
    return json.dumps(payload, ensure_ascii=False, default=str)


async def _dump_ui_frame(payload: Dict[str, Any]) -> str:
    """Serialize a ``/ws/ui`` frame, keeping a big one from freezing the loop.

    A relayed ``history_data`` frame carries records, and the fan-out used to
    render it once PER CLIENT — so a large relay multiplied one bundle's render
    into a fan-out-wide loop stall. The caller now renders each distinct payload
    once (see :meth:`UiHub._fan_out`), and a big one is rendered in record
    batches that yield to the loop (see :func:`dump_json_chunked`), which is what
    actually removes the stall — a worker thread would not, because the C JSON
    encoder holds the GIL. Every other frame the hub sends — machine lists, index
    deltas, cursor advisories — renders inline, as it always did.
    """
    records = payload.get("records") if isinstance(payload, dict) else None
    # Either gate trips. A record count alone would classify a 10-record /
    # 11 MiB relay as small and render it inline; the byte estimate is only
    # walked for frames whose count did NOT trip, so it stays bounded (see
    # ``state.records_reach_bytes``).
    if isinstance(records, list) and (
        len(records) >= UI_FRAME_CHUNKED_RECORDS
        or records_reach_bytes(records, UI_FRAME_CHUNKED_BYTES)
    ):
        rendered = await dump_json_chunked(
            payload, ensure_ascii=False, default=str
        )
        return rendered.decode("utf-8")
    return _render_ui_frame(payload)


# Server identity advertised in WELCOME.
try:  # pragma: no cover - import guard
    import tianluo as _se3

    SERVER_VERSION = str(getattr(_se3, "__version__", "unknown"))
except Exception:  # pragma: no cover - defensive
    SERVER_VERSION = "unknown"


class ConnectionManager:
    """Tracks live daemon WebSocket connections and routes downlink messages."""

    def __init__(self, metrics: Optional[WireMetrics] = None) -> None:
        self._connections: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        # Per-message-type sent-byte accounting for the server→daemon downlink.
        # Shared with the rest of the server so idle vs active traffic can be
        # attributed by message type (the traffic-reduction acceptance surface).
        self._metrics = metrics

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

    def record_send(self, msg_type: str, nbytes: int) -> None:
        """Account a direct server→daemon send in the wire metrics.

        The handshake WELCOME and the heartbeat PING (and the reject-path
        WELCOME frames) are written straight to the socket rather than routed
        through :meth:`send_to` / :meth:`send_to_connection`, so their bytes
        would otherwise never reach the metrics. Recording them here keeps the
        per-type server→daemon total honest — an idle connection's only cost is
        those periodic pings, and the traffic diagnostics must show it.
        """
        if self._metrics is not None:
            self._metrics.record(msg_type, nbytes)

    @property
    def machine_ids(self) -> list:
        """A snapshot list of currently-connected machine ids."""
        return list(self._connections.keys())

    async def send_to(self, machine_id: str, message: protocol.Message) -> bool:
        """Send *message* to one daemon; return ``False`` if not connected."""
        websocket = self._connections.get(machine_id)
        if websocket is None:
            return False
        payload = message.to_json()
        try:
            await websocket.send_text(payload)
            if self._metrics is not None:
                self._metrics.record(message.type, len(payload.encode("utf-8")))
            return True
        except Exception:
            logger.warning("Failed to send %s to %s", message.type, machine_id)
            return False

    async def get_connection(self, machine_id: str) -> Any:
        """Return the currently registered socket for *machine_id*, if any."""
        async with self._lock:
            return self._connections.get(machine_id)

    async def is_current_connection(self, machine_id: str, websocket: Any) -> bool:
        """Whether *websocket* is still registered for *machine_id*."""
        async with self._lock:
            return self._connections.get(machine_id) is websocket

    async def broadcast_viewers(self, count: int) -> None:
        """Send a ``MSG_VIEWERS`` presence edge to every connected daemon.

        Fired by the :class:`UiHub` presence-edge callback when the browser
        connection count crosses the 0↔non-0 boundary, so each daemon can
        shift between its full-speed and low-power cadences. Per-machine
        delivery goes through :meth:`send_to`, which already swallows a single
        socket's failure and accounts the sent bytes — one stalled daemon must
        not keep the rest of the fleet in the wrong gear. A pre-v4 daemon that
        receives the frame drops it as an unknown type (its decode() rejects
        it), which is exactly the fail-open no-op the presence design wants.
        """
        message = protocol.make_viewers(count)
        for machine_id in self.machine_ids:
            await self.send_to(machine_id, message)

    async def send_to_connection(
        self,
        machine_id: str,
        websocket: Any,
        message: protocol.Message,
    ) -> bool:
        """Send only through the exact connection previously validated.

        The identity check is performed under the registry lock, but the lock
        is released **before** awaiting ``send_text`` — mirroring the lock-free
        send discipline of :meth:`send_to`. A send that blocks on a
        backpressured / stalled daemon socket must not hold the manager-wide
        lock, which would otherwise stall ``connect`` / ``disconnect`` /
        ``get_connection`` / ``is_current_connection`` for **every** machine
        until the socket unblocks or the connection dies.
        """
        async with self._lock:
            if self._connections.get(machine_id) is not websocket:
                return False
        payload = message.to_json()
        try:
            await websocket.send_text(payload)
            if self._metrics is not None:
                self._metrics.record(message.type, len(payload.encode("utf-8")))
            return True
        except Exception:
            logger.warning("Failed to send %s to %s", message.type, machine_id)
            return False


class PresenceDebouncer:
    """Debounces daemon-offline transitions with a per-machine grace window.

    WHY: on a lossy/high-latency link (e.g. node007) the daemon keepalive
    times out and reconnects every ~45s. Marking the machine offline the
    instant its socket drops makes the WebUI online badge flap online/offline
    on every one of those churns, even though the flow is still live and a new
    connection is seconds away. So on disconnect we do NOT mark offline
    immediately: we arm a *grace* task that only performs the mark_offline +
    push after ``delay`` seconds, and cancel it the moment the daemon
    reconnects. Net effect: a reconnect within the grace window is invisible to
    the UI (stays online throughout), and only a daemon that stays gone past
    the window is shown offline. No intermediate "reconnecting" state is
    introduced — the record simply keeps its last ``online`` value until the
    grace expires.
    """

    def __init__(self, delay: float = 60.0) -> None:
        self._delay = delay
        # machine_id -> the pending "go offline" grace task. At most one per
        # machine; a re-arm supersedes (and cancels) the previous one.
        self._pending: Dict[str, "asyncio.Task"] = {}

    def schedule_offline(
        self,
        machine_id: str,
        action: Callable[[], Awaitable[None]],
        delay: Optional[float] = None,
    ) -> None:
        """Arm a grace task that runs *action* after the grace window.

        *action* is the offline effect (mark_offline + push). It fires only if
        the task is not cancelled first (by a reconnect via :meth:`cancel`).
        Re-arming for the same machine cancels any prior pending task so the
        table never leaks or double-fires.
        """
        self.cancel(machine_id)
        wait = self._delay if delay is None else delay

        async def _grace() -> None:
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:  # reconnected within the window
                raise
            # Drop our own table entry before running the effect so a
            # concurrent cancel() during ``action`` cannot cancel a task that
            # has already committed to going offline.
            self._pending.pop(machine_id, None)
            await action()

        self._pending[machine_id] = asyncio.ensure_future(_grace())

    def cancel(self, machine_id: str) -> None:
        """Cancel and drop any pending offline task for *machine_id*.

        Called on reconnect (register/connect succeeded) so the machine that
        just came back is never subsequently marked offline by a stale grace
        task armed by the previous disconnect.
        """
        task = self._pending.pop(machine_id, None)
        if task is not None:
            task.cancel()

    def shutdown(self) -> None:
        """Cancel every pending offline task (server teardown)."""
        for task in self._pending.values():
            task.cancel()
        self._pending.clear()


class _PullAbandoned(Exception):
    """Internal signal: a shared-pull leader failed before dispatching.

    Raised into every follower parked behind a leader whose daemon send
    returned ``False`` or was cancelled *before* a ``MSG_HISTORY_REQUEST`` was
    successfully dispatched. The follower catches it and retries as a fresh
    leader instead of waiting out ``HISTORY_PULL_TIMEOUT`` behind a pull that
    will never be answered. It never escapes the REST handler.
    """


class HistoryRequestRegistry:
    """Tracks in-flight on-demand history pulls awaiting a daemon reply.

    A REST handler that needs a flow's history but finds the cache empty
    sends a ``MSG_HISTORY_REQUEST`` to the owning daemon and parks an
    :class:`asyncio.Future` here keyed by ``flow_id``. When the matching
    ``MSG_HISTORY_DATA`` arrives on the daemon receive loop it resolves every
    waiter for that flow and machine. Lives entirely in process memory.
    """

    def __init__(self) -> None:
        self._waiters: Dict[Tuple[str, Optional[str]], list] = {}
        # Keys with a daemon ``MSG_HISTORY_REQUEST`` already in flight. Lets
        # concurrent cache-miss REST handlers for the same (flow, machine)
        # share ONE daemon pull instead of each sending its own: only the
        # first (the leader) sends, the rest park and await the same reply.
        self._inflight: set = set()

    def register(
        self, flow_id: str, machine_id: Optional[str] = None
    ) -> "asyncio.Future":
        """Park a future for *flow_id*, optionally pinned to *machine_id*."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault((flow_id, machine_id), []).append(fut)
        return fut

    def begin_pull(
        self, flow_id: str, machine_id: Optional[str] = None
    ) -> Tuple["asyncio.Future", bool]:
        """Register a waiter and report whether the caller must send the pull.

        Returns ``(future, is_leader)``. ``is_leader`` is ``True`` only for the
        first caller to park a waiter for ``(flow_id, machine_id)`` while no
        pull is in flight — that caller sends the daemon ``MSG_HISTORY_REQUEST``
        and marks the key in flight. Concurrent callers (followers) get
        ``is_leader=False`` and simply await the same reply, so a single daemon
        pull serves them all. Without this, every concurrent cache-miss request
        sent its own request; the first reply resolved all waiters and was
        suppressed, but each later reply found no waiter, replaced the cache
        generation, and was broadcast as ``mode: full`` — clearing the progress
        tokens REST had just returned and forcing the next reconnect back into a
        full fetch + DOM rebuild.
        """
        fut = self.register(flow_id, machine_id)
        key = (flow_id, machine_id)
        is_leader = key not in self._inflight
        if is_leader:
            self._inflight.add(key)
        return fut, is_leader

    def resolve(
        self, flow_id: str, data: Any, machine_id: Optional[str] = None
    ) -> bool:
        """Resolve waiters for *flow_id* from the reporting *machine_id*.

        Returns ``True`` when at least one parked waiter existed for this flow
        (i.e. the frame answered an on-demand pull), so the caller can tell an
        on-demand pull reply apart from an unsolicited live push.
        """
        keys = [(flow_id, machine_id)]
        if machine_id is not None:
            # Preserve compatibility for callers that intentionally registered
            # an unpinned waiter.
            keys.append((flow_id, None))
        resolved = False
        for key in keys:
            for fut in self._waiters.pop(key, []):
                resolved = True
                if not fut.done():
                    fut.set_result(data)
            # The pull for this key is complete — let a later cache miss start
            # a fresh one.
            self._inflight.discard(key)
        return resolved

    def discard(
        self,
        flow_id: str,
        fut: "asyncio.Future",
        machine_id: Optional[str] = None,
    ) -> None:
        """Drop a single waiter (e.g. after a timeout) without resolving it."""
        key = (flow_id, machine_id)
        waiters = self._waiters.get(key)
        if not waiters:
            # No waiters tracked (already resolved / never parked); make sure a
            # stale in-flight marker cannot wedge future pulls.
            self._inflight.discard(key)
            return
        if fut in waiters:
            waiters.remove(fut)
        if not waiters:
            self._waiters.pop(key, None)
            # The last waiter for this key is gone (all timed out / failed to
            # send): clear the in-flight marker so the next request re-pulls.
            self._inflight.discard(key)

    def fail_pull(
        self,
        flow_id: str,
        machine_id: Optional[str] = None,
        *,
        exclude: "Optional[asyncio.Future]" = None,
    ) -> None:
        """Release every waiter for a key and clear its in-flight marker.

        Called when the *leader* of a shared pull fails BEFORE a successful
        daemon dispatch — its ``MSG_HISTORY_REQUEST`` send returned ``False``,
        or it was cancelled before / while sending. Unlike :meth:`discard`,
        which only clears the in-flight marker once the *last* waiter is gone,
        this wakes every parked follower at once and clears the marker
        unconditionally, so no follower is stranded until
        ``HISTORY_PULL_TIMEOUT`` behind a pull that will never be answered, and
        the next request leads a fresh pull instead of joining the abandoned
        one. Followers are failed with :class:`_PullAbandoned` so they can
        retry as a new leader; the leader's own future (``exclude``) is dropped
        without being failed, since the leader is already unwinding on this
        path and never awaits it.
        """
        key = (flow_id, machine_id)
        waiters = self._waiters.pop(key, [])
        for fut in waiters:
            if fut is exclude:
                continue
            if not fut.done():
                fut.set_exception(_PullAbandoned())
        self._inflight.discard(key)


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


class ProjectCommandRegistry:
    """Tracks in-flight project-registry commands awaiting a daemon result.

    The registry-management REST handlers (add / remove a project root) park an
    :class:`asyncio.Future` here keyed by ``request_id`` and wake on the
    daemon's :data:`protocol.MSG_PROJECT_RESULT`. Deliberately a separate
    registry from :class:`IssueCommandRegistry` rather than a shared one: the
    two legs mint their ids independently, so sharing a keyspace would let an
    issue ack resolve a project waiter (and vice versa) on an id collision.
    Lives entirely in process memory.
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


class UploadRequestRegistry:
    """Tracks in-flight file uploads awaiting a daemon result.

    ``POST /api/uploads`` base64s the browser-supplied bytes into a
    :data:`protocol.MSG_UPLOAD_COMMAND`, parks an :class:`asyncio.Future` here
    keyed by ``request_id``, and wakes on the daemon's
    :data:`protocol.MSG_UPLOAD_RESULT`. A third registry rather than a reuse of
    :class:`IssueCommandRegistry` / :class:`ProjectCommandRegistry` for the same
    reason those two are separate from each other: each leg mints its ids
    independently, so a shared keyspace would let an issue or project ack
    resolve an upload waiter on an id collision — here that would hand the
    browser a success with no stored path at all. Lives entirely in process
    memory.

    The revision-6 read-back leg (``GET /api/uploads/file`` ↔
    :data:`protocol.MSG_FETCH_RESULT`) parks its waiters in a *second instance*
    of this class rather than in this one: the two legs mint their ids
    independently, so a shared keyspace would let an upload ack resolve a fetch
    waiter — handing the browser a "file" whose payload is actually an upload
    receipt. The bookkeeping is identical, so the class is shared even though
    the instance is not.
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


class DetailRequestRegistry:
    """Tracks in-flight on-demand issue/call detail pulls awaiting a daemon reply.

    STATUS_UPDATE now carries only truncated issue descriptions / call prompts;
    when the operator opens a detail view the REST handler routes a
    :data:`~tianluo.daemon.protocol.MSG_DETAIL_REQUEST` to the owning daemon and
    parks an :class:`asyncio.Future` here keyed by ``request_id``. When the
    matching :data:`~tianluo.daemon.protocol.MSG_DETAIL_DATA` lands on the daemon
    receive loop the waiter is resolved with the daemon's payload. Concurrent
    REST requests for the *same physical target* — the full
    ``(kind, target_id, machine_id, project_root)`` tuple — share ONE downlink
    pull via :meth:`begin` (leader/follower), mirroring
    :class:`HistoryRequestRegistry`, so opening the same detail twice never
    fans out two DETAIL_REQUESTs. The machine id and project root are part of
    the coalescing key on purpose: a local issue/call id (e.g. ``1``) is only
    unique *within* one project on one machine, so two different owners /
    projects that happen to share a numeric id must NOT join the same pull —
    otherwise the single reply would resolve both futures with the first
    caller's body, leaking one owner's issue/call detail to another. Lives
    entirely in process memory.
    """

    def __init__(self) -> None:
        self._waiters: Dict[str, list] = {}
        #: (kind, target_id, machine_id, project_root) keys with a downlink
        #: DETAIL_REQUEST already in flight, so a second opener of the SAME
        #: physical target joins the first pull instead of sending its own. The
        #: machine/root are in the key so a same-id target on a different
        #: machine/project never coalesces across owners.
        self._inflight: Dict[Tuple[str, str, str, str], str] = {}

    def begin(
        self,
        request_id: str,
        kind: str,
        target_id: str,
        machine_id: str,
        project_root: str,
    ) -> Tuple["asyncio.Future", bool, str]:
        """Park a waiter and report whether this caller must send the downlink.

        Returns ``(future, is_leader, active_request_id)``. ``is_leader`` is
        ``True`` only for the first caller for the physical target
        ``(kind, target_id, machine_id, project_root)`` with no pull in flight —
        that caller sends the daemon ``MSG_DETAIL_REQUEST`` under *request_id*. A
        follower parks a waiter under the LEADER's already in-flight
        ``request_id`` (returned as *active_request_id*) so the single
        DETAIL_DATA reply resolves leader and followers together. Coalescing is
        scoped to the exact machine + project root, never just the local id, so
        a reply can never resolve a waiter that asked about a different owner's
        target.
        """
        key = (kind, target_id, machine_id, project_root)
        active = self._inflight.get(key)
        if active is None:
            self._inflight[key] = request_id
            active = request_id
            is_leader = True
        else:
            is_leader = False
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(active, []).append(fut)
        return fut, is_leader, active

    def resolve(self, request_id: str, data: Any) -> None:
        """Resolve every waiter parked under *request_id* with *data*."""
        for fut in self._waiters.pop(request_id, []):
            if not fut.done():
                fut.set_result(data)
        # The pull for whatever physical target this request_id led is done;
        # let a later open start a fresh one.
        self._inflight = {
            key: rid for key, rid in self._inflight.items() if rid != request_id
        }

    def discard(self, request_id: str, fut: "asyncio.Future") -> None:
        """Drop a single waiter (e.g. after a timeout) without resolving it."""
        waiters = self._waiters.get(request_id)
        if waiters and fut in waiters:
            waiters.remove(fut)
        if not self._waiters.get(request_id):
            self._waiters.pop(request_id, None)
            # No waiter left for this leader's pull — clear its in-flight marker
            # so the next opener leads a fresh DETAIL_REQUEST rather than joining
            # an abandoned one.
            self._inflight = {
                key: rid
                for key, rid in self._inflight.items()
                if rid != request_id
            }


class HistoryWindowRegistry:
    """Tracks in-flight step-window history reads awaiting their daemon reply.

    The windowed WebUI open asks the owning daemon for a few STEP BLOCKS of a
    flow (:data:`~tianluo.daemon.protocol.MSG_HISTORY_WINDOW_REQUEST`) and parks
    an :class:`asyncio.Future` here keyed by ``request_id``. The daemon may
    answer in several :data:`~tianluo.daemon.protocol.MSG_HISTORY_WINDOW_DATA`
    frames — a window can be tens of megabytes — so frames ACCUMULATE and only
    the one flagged ``final`` settles the waiter.

    WHY there is no leader/follower coalescing here (unlike
    :class:`HistoryRequestRegistry` / :class:`DetailRequestRegistry`): those two
    coalesce whole-flow pulls, which several viewers of the same flow issue
    simultaneously and which cost megabytes each. A window read is
    scroll-driven, its key would have to include the anchor block (two readers
    at different scroll positions want different windows), and its results are
    never cached — so the shared-pull machinery would add a keyspace and a
    cross-owner leakage surface to buy nothing.

    Lives entirely in process memory.
    """

    def __init__(self) -> None:
        self._waiters: Dict[str, "asyncio.Future"] = {}
        #: request_id -> the frames accumulated so far for that request.
        self._chunks: Dict[str, Dict[str, Any]] = {}

    def begin(self, request_id: str) -> "asyncio.Future":
        """Park a waiter for *request_id* and return its future."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters[request_id] = fut
        self._chunks[request_id] = {
            "ok": True,
            "error": "",
            "records": [],
            "steps": [],
            "window": [],
            "counts": {},
            "signature": "",
            "not_modified": False,
        }
        return fut

    def accumulate(self, request_id: str, payload: Dict[str, Any]) -> None:
        """Fold one reply frame in; resolve the waiter on the ``final`` frame.

        A frame for an unknown / already-settled request is dropped: it is the
        residue of a waiter that timed out, and re-creating its accumulator here
        would be a slow leak keyed by a request nobody is waiting on.
        """
        acc = self._chunks.get(request_id)
        if acc is None:
            return
        if not payload.get("ok", True):
            acc["ok"] = False
            acc["error"] = str(payload.get("error") or "history window read failed")
        records = payload.get("records")
        if isinstance(records, list):
            acc["records"].extend(records)
        # The window description rides every frame (see make_history_window_data)
        # so a first frame lost to a reconnect costs no descriptive state; take
        # the latest non-empty one.
        for key in ("steps", "window"):
            value = payload.get(key)
            if isinstance(value, list) and value:
                acc[key] = value
        counts = payload.get("counts")
        if isinstance(counts, dict) and counts:
            acc["counts"] = counts
        signature = payload.get("signature")
        if isinstance(signature, str) and signature:
            acc["signature"] = signature
        # A conditional read that matched is a single ``final`` frame carrying
        # nothing else; it is latched (never un-set by a later frame) so the
        # caller reads one unambiguous verdict off the settled accumulator.
        if payload.get("not_modified"):
            acc["not_modified"] = True
        if not payload.get("final", True):
            return
        fut = self._waiters.pop(request_id, None)
        self._chunks.pop(request_id, None)
        if fut is not None and not fut.done():
            fut.set_result(acc)

    def discard(self, request_id: str) -> None:
        """Drop a waiter and its accumulator (timeout / cancellation)."""
        self._waiters.pop(request_id, None)
        self._chunks.pop(request_id, None)


async def request_history_window(
    manager: ConnectionManager,
    machine_id: str,
    flow_id: str,
    request_id: str,
    *,
    project_root: str = "",
    count: int = 10,
    before_step: str = "",
    steps: Any = (),
    if_signature: str = "",
) -> bool:
    """Send a ``MSG_HISTORY_WINDOW_REQUEST`` to *machine_id*.

    Returns ``False`` when the machine has no live connection, so the caller can
    fall back to the cursorless full pull rather than park on a reply that will
    never come.

    *if_signature* makes the read CONDITIONAL: a daemon that recognises it and
    finds the flow unchanged answers ``not_modified`` instead of re-reading and
    re-shipping the window (see :func:`~tianluo.daemon.protocol.make_history_window_request`).
    """
    if not manager.is_connected(machine_id):
        return False
    message = protocol.make_history_window_request(
        flow_id,
        request_id=request_id,
        project_root=project_root,
        count=count,
        before_step=before_step,
        steps=steps,
        if_signature=if_signature,
    )
    return await manager.send_to(machine_id, message)


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
    machine_id: Optional[str] = None,
    connection: Any = None,
    cursor: Optional[Dict[str, Any]] = None,
    project_root: str = "",
) -> bool:
    """Send a ``MSG_HISTORY_REQUEST`` to the daemon owning *flow_id*.

    When *machine_id* is supplied, routes directly to that already-validated
    machine without resolving ownership again. When *connection* is supplied,
    the request is sent only if that exact socket is still current, preventing
    a same-machine-id reconnect from receiving a request validated against the
    previous daemon connection.
    """
    target_machine = machine_id
    if target_machine is None:
        target_machine = await state.find_machine_for_history_flow(flow_id)
    if target_machine is None or not manager.is_connected(target_machine):
        return False
    message = protocol.make_history_request(
        flow_id, project_root=project_root, cursor=cursor
    )
    # Everything this request brings back is a REPLAY of already-persisted
    # history — a cache-miss bundle, a reconnect backfill, a requires_full
    # repair — so the browser leg summarizes it exactly like the REST bundle
    # response, whatever ``mode`` each frame of the reply wears. This is the one
    # funnel every回程 pull leaves through, which is why the marker is armed
    # here rather than at each call site. The cursor rides along because it
    # fixes the shape of the head this reply must start with, which is how the
    # marker later tells that head apart from a live append that raced this
    # dispatch.
    #
    # INVARIANT: armed BEFORE the send, retracted when the send fails. The
    # daemon's reply can be read off the receive loop while this send coroutine
    # is still resuming, so a marker armed afterwards would miss its own reply's
    # head — and every chunked tail behind that head would then fail to open the
    # (still-expecting-a-head) marker and be broadcast whole.
    await state.mark_history_replay(flow_id, cursor=cursor)
    if connection is not None:
        sent = await manager.send_to_connection(
            target_machine, connection, message
        )
    else:
        sent = await manager.send_to(target_machine, message)
    if not sent:
        # Retract by the shape armed above: a rival pull for the same flow may
        # have been armed and dispatched successfully in between, and it must
        # keep the replay identity its dispatch earned.
        await state.unmark_history_replay(flow_id, cursor=cursor)
    return sent


async def request_detail(
    manager: ConnectionManager,
    machine_id: str,
    kind: str,
    target_id: str,
    request_id: str,
    *,
    project_root: str = "",
) -> bool:
    """Send a ``MSG_DETAIL_REQUEST`` to *machine_id* for one issue / call's full text.

    Returns ``False`` when the machine has no live connection (the REST handler
    maps that to a 503). *request_id* correlates the eventual
    ``MSG_DETAIL_DATA`` back to the waiting request via
    :class:`DetailRequestRegistry`.
    """
    if not manager.is_connected(machine_id):
        return False
    message = protocol.make_detail_request(
        kind, target_id, project_root=project_root, request_id=request_id
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


class _UiClientChannel:
    """One ``/ws/ui`` client's outbound queue, drained by its own task.

    INVARIANT: no fan-out ever awaits a browser socket on the path that produced
    the frame. Every send happens in THIS channel's task; :meth:`offer` only ever
    appends to a bounded deque and returns.

    WHY this exists (the defect it closes): the relay of a daemon's history reply
    runs inside ``_serve_loop.receive()`` — one coroutine that reads the daemon
    socket and awaits ``_handle_message`` per frame. With the fan-out awaiting
    ``client.send_text`` inline, a browser that could not drain parked that
    coroutine, so the DAEMON socket's inbound queue stopped being consumed, its
    Pong was never processed, and uvicorn's WS keepalive closed the daemon
    connection with ``1011 keepalive ping timeout`` — in the middle of a 147-frame
    drain, which is how a large flow's conversation lost its tail. Measured with
    ``scripts/measure_history_relay_backpressure.py`` over this repo's real
    ``20260831-095750_23865927`` (147 frames / 4324 records): a browser costing
    50 ms per frame parked the receive path for 11.3 s in total (2.5 s after this
    change) while the event loop's worst lateness stayed at 60 ms throughout —
    the loop was never the problem, the parked coroutine was. A slow console must
    cost that console its frames and nothing else.
    """

    def __init__(self, hub: "UiHub", client: Any) -> None:
        self._hub = hub
        self.client = client
        # (frame type, rendered text, encoded byte size)
        self._queue: "deque" = deque()
        self._bytes = 0
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self.closed = False
        self.dropped = 0
        #: Set once the unsheddable backlog breached the hard ceiling and this
        #: client was scheduled for disconnection (see :meth:`_trim`).
        self.overflowed = False
        self._overflow_task: Optional["asyncio.Future"] = None
        self._task = asyncio.ensure_future(self._run())

    def offer(self, ptype: str, text: str, nbytes: int) -> None:
        """Queue one rendered frame for delivery. Never blocks, never raises."""
        if self.closed:
            return
        self._queue.append((ptype, text, nbytes))
        self._bytes += nbytes
        self._trim()
        self._idle.clear()
        self._wake.set()

    def _trim(self) -> None:
        """Shed the oldest sheddable frames until the backlog is back in bounds.

        Oldest-first because the newest frame carries the most advanced cursor:
        a console that receives it learns the widest gap in one shot and repairs
        it with one request, whereas keeping the oldest would hand it a stale
        picture it must then repair twice.

        Only :data:`UI_DROPPABLE_FRAME_TYPES` are ever shed. A backlog with
        nothing sheddable left in it is kept whole and escalated to
        :meth:`_overflow` instead — see the invariant on that constant.
        """
        # ``> 1``: the budget bounds a BACKLOG, so a single frame that is itself
        # bigger than the byte budget (a whole-bundle relay of a big flow) is
        # still delivered — it is already rendered and in memory, and dropping
        # every such frame would mean a big flow never streams at all.
        before = self.dropped
        while len(self._queue) > 1 and (
            len(self._queue) > UI_CLIENT_QUEUE_MAX_FRAMES
            or self._bytes > UI_CLIENT_QUEUE_MAX_BYTES
        ):
            index = next(
                (
                    i
                    for i, (ptype, _text, _n) in enumerate(self._queue)
                    if ptype in UI_DROPPABLE_FRAME_TYPES
                ),
                None,
            )
            if index is None:
                # Nothing here may be lost silently. Stop trimming and let the
                # backlog stand: it is bounded from above by the hard ceiling
                # checked below, which disconnects rather than deletes.
                break
            self._bytes -= self._queue[index][2]
            del self._queue[index]
            self.dropped += 1
        # One line per queue-ful of loss, whatever the drop rate per offer:
        # a lagging console must be visible in the log without becoming the log.
        if self.dropped // UI_CLIENT_QUEUE_MAX_FRAMES != (
            before // UI_CLIENT_QUEUE_MAX_FRAMES
        ):
            logger.warning(
                "UI client is lagging; %d superseded frames coalesced away "
                "(backlog %d frames / %.1f MiB). Only whole-state frames a newer "
                "copy replaces are shed; nothing belonging to a history delivery "
                "is.",
                self.dropped, len(self._queue), self._bytes / (1024 * 1024),
            )
        # ``> 1`` for the same reason the trim loop carries it: one frame is not
        # a backlog. A single whole-bundle relay can be larger than the hard byte
        # ceiling on its own, and retiring the client for it would disconnect
        # every console that opens a big flow — then do it again on reconnect.
        if len(self._queue) > 1 and (
            len(self._queue) > UI_CLIENT_QUEUE_HARD_FRAMES
            or self._bytes > UI_CLIENT_QUEUE_HARD_BYTES
        ):
            self._overflow()

    def _overflow(self) -> None:
        """Retire a client whose unsheddable backlog breached the hard ceiling.

        WHY a disconnect and not one more drop: the frames still queued at this
        point are the ones that may not be lost silently — a one-shot lifecycle
        event nothing will ever re-send, or a frame of a history delivery whose
        later frames would then declare that delivery complete on the console's
        behalf. Deleting either leaves the console connected and believing it is
        current. Closing the socket is the one loss the frontend can actually
        see: ``ws.onclose`` flags the view stale and the reconnect re-reads the
        machine list, the history index and the open flow — so the console
        converges instead of silently diverging.
        """
        if self.overflowed:
            return
        self.overflowed = True
        logger.warning(
            "UI client backlog is unsheddable at %d frames / %.1f MiB; "
            "disconnecting it so the browser resynchronises on reconnect "
            "(dropping these frames would lose events that have no replay path)",
            len(self._queue), self._bytes / (1024 * 1024),
        )
        self._overflow_task = asyncio.ensure_future(self._retire())

    async def _retire(self) -> None:
        """Unregister and close this client's socket, then free its backlog."""
        try:
            await self._hub._client_overflowed(self.client)
        finally:
            self.close()

    async def _run(self) -> None:
        while True:
            if not self._queue:
                self._idle.set()
                self._wake.clear()
                await self._wake.wait()
                continue
            ptype, text, nbytes = self._queue.popleft()
            self._bytes -= nbytes
            try:
                await self.client.send_text(text)
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            except Exception:  # pragma: no cover - best effort
                self.closed = True
                self._idle.set()
                await self._hub._client_failed(self.client)
                return
            if self._hub._metrics is not None:
                self._hub._metrics.record(f"ui:{ptype or 'unknown'}", nbytes)

    async def handoff(self, turns: int) -> None:
        """Give this client *turns* loop turns to accept what it was offered.

        Costs the caller nothing but a few scheduler round-trips, whatever the
        client's link speed (see :data:`UI_FANOUT_HANDOFF_TURNS`).
        """
        for _ in range(max(0, turns)):
            if self.closed or self._idle.is_set():
                return
            await asyncio.sleep(0)

    async def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Block until this client's backlog is empty (tests / diagnostics)."""
        if self.closed:
            return True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    def close(self) -> None:
        self.closed = True
        self._queue.clear()
        self._bytes = 0
        self._idle.set()
        self._task.cancel()


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

    Delivery is per-client and QUEUED: a fan-out renders each distinct payload
    once, hands it to each target's :class:`_UiClientChannel`, and returns
    without ever awaiting a browser socket — because the coroutine it runs on is
    frequently the daemon's receive loop, which must keep reading. A client that
    cannot drain loses its own relay frames (bounded queue, oldest sheddable
    first) and repairs itself from the cursor the next frame carries; it can no
    longer cost the daemon its connection.

    The hub is also the single point that knows the exact browser connection
    count, so it doubles as the *presence* source (protocol revision 4): when
    that count crosses the 0↔non-0 boundary, the injected *on_presence_edge*
    callback fires so the daemons can be told to shift gears. Intermediate
    1→2 / 2→1 changes are deliberately silent — the daemons' gear selection
    only needs the single "anyone watching?" bit.
    """

    def __init__(
        self,
        metrics: Optional[WireMetrics] = None,
        on_presence_edge: Optional[Any] = None,
        *,
        handoff_turns: int = UI_FANOUT_HANDOFF_TURNS,
    ) -> None:
        # websocket -> owner_id (None == unscoped/admin view)
        self._clients: Dict[Any, Optional[str]] = {}
        # websocket -> its outbound queue + drain task. Created on register so
        # no asyncio primitive is bound to a loop at construction time.
        self._channels: Dict[Any, "_UiClientChannel"] = {}
        self._lock = asyncio.Lock()
        # Per-frame-type sent-byte accounting for the server→browser leg,
        # recorded under a ``ui:<type>`` key so the /ws/ui fan-out shows up
        # distinctly from the server→daemon downlink in the metrics snapshot.
        self._metrics = metrics
        # Async ``callable(count)`` awaited on each 0↔non-0 client-count edge
        # (``None`` keeps the pre-presence behaviour exactly). Held as an
        # opaque attribute rather than typed, since the hub must import
        # nothing about what the edge drives (the daemon broadcast lives in
        # ConnectionManager).
        self._on_presence_edge = on_presence_edge
        # How many loop turns one fan-out gives a client to accept a frame
        # before leaving it to that client's own drain task (see
        # :data:`UI_FANOUT_HANDOFF_TURNS`).
        self._handoff_turns = handoff_turns

    async def register(self, websocket: Any, owner: Optional[str] = None) -> None:
        # The edge decision is computed under the lock (from the counts before
        # and after the mutation) so two concurrent registers can never both
        # observe "I was the 0→1 transition"; the callback itself is awaited
        # OUTSIDE the lock so a slow daemon broadcast cannot stall every other
        # register/unregister/fan-out.
        async with self._lock:
            before = len(self._clients)
            self._clients[websocket] = owner
            if websocket not in self._channels:
                self._channels[websocket] = _UiClientChannel(self, websocket)
            after = len(self._clients)
        logger.info("UI client connected (%d total)", len(self._clients))
        if before == 0 and after > 0:
            await self._fire_presence_edge(after)

    async def unregister(self, websocket: Any) -> None:
        async with self._lock:
            before = len(self._clients)
            self._clients.pop(websocket, None)
            channel = self._channels.pop(websocket, None)
            after = len(self._clients)
        if channel is not None:
            channel.close()
        logger.info("UI client disconnected (%d total)", len(self._clients))
        if before > 0 and after == 0:
            await self._fire_presence_edge(0)

    async def _client_failed(self, websocket: Any) -> None:
        """Retire a client whose socket raised while its channel was sending.

        Called from the channel's own task, which is the only place a send
        failure is now observable — the fan-out no longer touches the socket.
        The presence edge is detected HERE for the same reason the fan-out used
        to detect it: a client pruned on a failed send is gone before its
        connection handler's ``unregister`` runs, so that unregister would see
        no count change and fire no edge, leaving the daemons in the full-speed
        gear until the PING level self-healed.
        """
        async with self._lock:
            before = len(self._clients)
            self._clients.pop(websocket, None)
            channel = self._channels.pop(websocket, None)
            after = len(self._clients)
        if channel is not None:
            channel.closed = True
        if before > 0 and after == 0:
            await self._fire_presence_edge(0)

    async def _client_overflowed(self, websocket: Any) -> None:
        """Retire and CLOSE a client whose backlog could not be trimmed.

        Called from the channel when the frames it is holding are all
        unsheddable (see :data:`UI_DROPPABLE_FRAME_TYPES`). Retiring alone would
        leave the browser holding an open socket that never receives anything
        again; the explicit close is what turns an invisible divergence into the
        one signal the frontend already repairs — a reconnect that re-reads
        everything. Best effort throughout: a socket that is already gone needs
        no closing.
        """
        await self._client_failed(websocket)
        close = getattr(websocket, "close", None)
        if close is None:
            return
        try:
            # 1013 "try again later": the client is not at fault and should come
            # straight back, which is exactly what the frontend's reconnect does.
            try:
                result = close(code=1013)
            except TypeError:  # pragma: no cover - test doubles / older ASGI
                result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:  # pragma: no cover - best effort
            logger.debug("closing an overflowed UI client failed", exc_info=True)

    async def wait_drained(self, timeout: Optional[float] = None) -> bool:
        """Block until every connected client's outbound backlog is empty.

        For tests and diagnostics only — nothing on the serving path waits for a
        browser (that is the whole point of :class:`_UiClientChannel`).
        """
        async with self._lock:
            channels = list(self._channels.values())
        results = [await channel.wait_idle(timeout) for channel in channels]
        return all(results)

    async def _fire_presence_edge(self, count: int) -> None:
        """Await the injected presence-edge callback, swallowing its failures.

        Presence is a power optimisation, never a correctness dependency: a
        callback failure must not break UI client registration (the daemons
        self-heal from the ``viewers`` level riding every heartbeat PING).
        """
        if self._on_presence_edge is None:
            return
        try:
            await self._on_presence_edge(count)
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "presence edge callback failed (count=%d)", count, exc_info=True
            )

    @property
    def client_count(self) -> int:
        """Number of currently-connected frontend clients."""
        return len(self._clients)

    def record_send(self, ptype: str, nbytes: int) -> None:
        """Attribute *nbytes* of a direct (non-fan-out) /ws/ui send by frame type.

        The per-connection initial ``snapshot`` is sent straight down the new
        socket rather than through :meth:`_fan_out`, so it bypasses the fan-out
        accounting; this passthrough keeps that (potentially large) first frame
        visible in the same ``ui:<type>`` breakdown.
        """
        if self._metrics is not None:
            self._metrics.record(f"ui:{ptype or 'unknown'}", nbytes)

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

        # Serialize each DISTINCT payload once, up front, instead of once per
        # client inside the send loop.
        #
        # WHY: the owner-scoped helpers hand every client of one owner the SAME
        # payload object, and a ``history_data`` frame carries a whole relayed
        # bundle — ~11 ms of ``json.dumps`` at 3 MiB, ~53 ms at 16 MiB (measured;
        # see UI_FRAME_CHUNKED_RECORDS). Re-rendering that per client multiplied
        # one relayed frame into a fan-out-wide loop stall for byte-identical
        # output. Keying by object identity is exact here (the helpers build one
        # dict per owner and never mutate it afterwards) and degrades to the old
        # per-payload cost if some future caller passes distinct dicts.
        #
        # Fan-out ORDER is unchanged: rendering is a separate pass, and the send
        # loop below still walks *targets* in the order it was given.
        rendered: Dict[int, Tuple[str, int]] = {}
        for _client, payload in targets:
            if id(payload) not in rendered:
                text = await _dump_ui_frame(payload)
                rendered[id(payload)] = (text, len(text.encode("utf-8")))

        # Hand each frame to its client's own outbound queue instead of awaiting
        # the client's socket here. WHY: this method runs on whatever coroutine
        # produced the frame — and for relayed history that is the DAEMON's
        # receive loop, which must keep reading (see :class:`_UiClientChannel`).
        # The turn budget below preserves the old "returned means delivered"
        # behaviour for every client that can keep up.
        channels = []
        for client, payload in targets:
            text, nbytes = rendered[id(payload)]
            channel = self._channels.get(client)
            if channel is None:  # pragma: no cover - unregistered target
                continue
            ptype = payload.get("type") if isinstance(payload, dict) else ""
            channel.offer(ptype or "unknown", text, nbytes)
            channels.append(channel)
        if len(channels) == 1:
            await channels[0].handoff(self._handoff_turns)
        elif channels:
            # Concurrently, so the budget is per fan-out and not per client:
            # three consoles must not cost three budgets in series.
            await asyncio.gather(
                *(channel.handoff(self._handoff_turns) for channel in channels)
            )


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


async def _push_history_index_delta(
    hub: Optional["UiHub"],
    state: ServerState,
    machine_id: str,
    upserts: list,
    removed: list,
) -> None:
    """Broadcast an incremental history-index update to *machine_id*'s owner.

    Instead of re-fanning the whole aggregated index to every ``/ws/ui`` client
    on any active flow's ``updated_at`` tick (which scales with the *total* flow
    count), relay only the changed SessionMeta rows the daemon reported. Each
    upsert is annotated with its owning ``machine_id`` so the frontend can merge
    the aggregated index by ``flow_id`` exactly as the full index push does. The
    delta belongs to the reporting machine, so it is visible only to that
    machine's owner (plus the admin view) — never another owner's console. A
    frontend that predates this frame ignores the unknown ``type`` and instead
    picks the change up on its next full ``GET /api/history`` refresh, so the
    delta broadcast is backward compatible.
    """
    if hub is None or hub.client_count == 0:
        return
    if not upserts and not removed:
        return
    owner = await state.get_machine_owner(machine_id)
    annotated = []
    for row in upserts or []:
        if not isinstance(row, dict):
            continue
        entry = dict(row)
        entry.setdefault("machine_id", machine_id)
        annotated.append(entry)
    await hub.broadcast_owned(
        {
            "type": "history_index_delta",
            "machine_id": machine_id,
            "upserts": annotated,
            "removed": [str(r) for r in (removed or [])],
        },
        owner,
    )


async def _push_history_data(
    hub: Optional["UiHub"],
    state: ServerState,
    machine_id: str,
    flow_id: str,
    mode: str,
    records: list,
    *,
    replay: bool = False,
) -> None:
    """Broadcast a history-data delta for *flow_id* to its owner's UI clients.

    History records originate from a specific daemon (*machine_id*), so the
    delta is visible only to that machine's owner (plus the admin view) — never
    to another owner's console.

    WHY the frame also carries the post-frame ``cursor`` + ``signature``: without
    them a pushed frame is unverifiable. A client that joins the stream late (it
    was still at the login gate — ``/api/auth/me`` 401 keeps the WebSocket
    unopened — or the flow's pane was not yet open) sees only the tail appends
    that happen after it arrives, and NOTHING in the frame tells it that the
    bundle holds earlier records it never received. It then grows a headless
    conversation and, because the server's progress receipt separately advances
    to "fully delivered", every later poll answers ``not_modified`` and the hole
    is welded in for good. The cursor is the bundle's own statement of what it
    contains (per-step-file record counts), so the client can check its held
    ``stepId#ordinal`` set against it and ask for exactly the numbers it lacks;
    the signature says which bundle generation those counts describe.

    INVARIANT: the summarize/whole verdict is ONE decision per frame, taken from
    the server-side MECHANISM that produced it (``replay`` — see
    :meth:`ServerState.take_history_replay`) and from nothing else. It never
    consults a record's own creation stamp, the daemon's clock offset, or any
    browser's subscription instant, so one frame leaves the server in exactly
    one shape and every subscriber of the owner receives that same shape.
    WHY: those inputs cannot decide the question they were asked. A record's
    stamp is a naive daemon-local ISO string, so a daemon a timezone away reads
    hours into the future or hours into the past; and a push loop that lags by a
    tick makes a genuine tail append look older than a console that just opened.
    Both misreadings turned real-time increments into lazified chips — the
    running console then fetching back a body it had just been handed — and
    split one append into a whole copy for the early browser and a summarized
    one for the late browser.
    """
    if hub is None or hub.client_count == 0:
        return
    owner = await state.get_machine_owner(machine_id)
    frame: Dict[str, Any] = {
        "type": "history_data",
        "flow_id": flow_id,
        "mode": mode,
    }
    # Read the bundle AFTER the frame was applied, so the counts describe what
    # the client should hold once it merges these records — the same values the
    # REST snapshot would hand it at this instant. An older frontend simply
    # ignores the extra keys.
    #
    # INVARIANT: when there is no bundle to read (the cache dropped it between
    # the apply and this read), the frame carries NO ``incomplete`` key at all
    # rather than a fabricated one. Silence is the honest answer — the server has
    # nothing to state — and the consumer is required to leave its last
    # completeness statement standing on a frame that makes none (see
    # ``noteBundleCompleteness`` in static/app.js). Defaulting the absent key
    # either way would be a statement: ``false`` retires a live repair streak on
    # no evidence, ``true`` arms one against a bundle nothing is wrong with.
    meta = await state.get_history_bundle_meta(flow_id)
    if meta is not None:
        frame["cursor"] = meta["cursor"]
        frame["signature"] = meta["signature"]
        # The generation names WHICH bundle these counts belong to, so the client
        # can scope its repair budget and its retired-unfillable numbers to that
        # bundle and drop both the moment it is replaced.
        frame["generation"] = meta["generation"]
        # The pending window (cursor declares it, records have not caught up) is
        # carried alongside the cursor so a client self-checking off this pushed
        # frame reaches the SAME pending/unfillable verdict a REST poll would —
        # the two faces read one bundle via one source. An older frontend ignores
        # the extra key.
        frame["pending"] = meta["pending"]
        # INVARIANT: every face of the bundle states its completeness the same
        # way. This is the ONE field a truncated bundle cannot contradict from
        # its own records (cursor, signature and pending all describe the
        # self-consistent PREFIX that survived), so a console driven purely by
        # pushed frames must be told it here or it never learns the tail is
        # missing — exactly the silent truncation ``_OpenDelivery`` exists to
        # make visible.
        frame["incomplete"] = meta["incomplete"]
    # Mirror the daemon-to-server leg: carry the post-frame backend usage
    # payload (same shared backend the REST bundle delivers) so a WS-only
    # history view never freezes at its connect-time usage snapshot and the
    # frontend's ``msg.usage`` adoption path is live. Omitted (not ``null``)
    # when the bundle holds no usage at all; an older frontend ignores it.
    # A usage-bearing append refreshed the stored payload incrementally
    # (see ServerState._refresh_bundle_usage), so this read reflects every
    # record the frame adds; the O(records) rebuild fallback still runs on
    # full frames when no daemon payload has ever arrived.
    usage = await state.get_history_usage(
        flow_id, rebuild=mode == protocol.HISTORY_MODE_FULL,
    )
    if usage is not None:
        frame["usage"] = usage
    # A replay frame carries already-persisted history back to a browser, so it
    # is shaped exactly like the REST bundle response: collapsed-state fields,
    # bodies fetched on expand. The judgement is the frame's ORIGIN, never its
    # transport — such a frame either answers a pull the server dispatched (head
    # and every chunked tail of that reply) or replaces the whole bundle
    # wholesale (a daemon that restarted and lost its cursors re-sends a flow's
    # entire persisted history as one ``full``). A large recovery arrives as a
    # ``full`` head plus ``append`` tails, so transport alone would let most of a
    # big session's download and eager panel-building through untouched.
    #
    # Anything the pull markers do not name came off the live tail-append path
    # and rides WHOLE: it is the browser's real-time increment, already in its
    # hands, so lazifying it would only make the running console ask for a body
    # it was just given.
    frame["records"] = (
        summarize_history_records(records, flow_id) if replay else records
    )
    await hub.broadcast_owned(frame, owner)


async def _push_history_cursor(
    hub: Optional["UiHub"],
    state: ServerState,
    machine_id: str,
    flow_id: str,
) -> None:
    """Broadcast a records-less bundle-state advisory for *flow_id*.

    WHY this exists: some frames are deliberately NOT relayed to the UI — a
    ``mode: full`` reply that resolved a cache-miss pull (re-broadcasting it
    would blow away the progress token the parked REST response just delivered)
    and a ``full`` the cache rejected as truncating (#287). Suppressing the
    RECORDS is right, but suppressing the fact that the bundle changed is not:
    the frame that repairs a bundle after a discarded append is precisely one of
    these, so the consoles that most need to know the head exists were the ones
    told nothing. This advisory carries only the authoritative ``cursor`` +
    ``signature`` — enough for a client to notice it is missing records and pull
    exactly those numbers, with no full-frame DOM rebuild and no token reset.
    """
    if hub is None or hub.client_count == 0:
        return
    meta = await state.get_history_bundle_meta(flow_id)
    if meta is None:
        return
    owner = await state.get_machine_owner(machine_id)
    await hub.broadcast_owned(
        {
            "type": "history_cursor",
            "flow_id": flow_id,
            "cursor": meta["cursor"],
            "signature": meta["signature"],
            "generation": meta["generation"],
            # Same pending window as the REST snapshot and the history_data frame,
            # so an advisory-triggered self-check draws the pending/unfillable line
            # identically (see get_history_bundle_meta).
            "pending": meta["pending"],
            # …and the same completeness statement, for the same one-source rule:
            # this advisory is the ONLY signal a console gets for a frame whose
            # records are suppressed, so if the delivery it belongs to is still
            # unfinished, the advisory is where that has to be said.
            "incomplete": meta["incomplete"],
        },
        owner,
    )


async def _push_history_cursor_advisory(
    hub: Optional["UiHub"],
    state: ServerState,
    machine_id: str,
    flow_id: str,
    cursor: Dict[str, Any],
) -> None:
    """Broadcast a bundle-state advisory for a flow the cache no longer holds.

    The twin of :func:`_push_history_cursor` for the one case where there IS no
    bundle to read meta from: a budget-evicted flow whose daemon frames are
    being suppressed. *cursor* is the suppressed frame's own declaration of what
    the DAEMON holds, which is exactly the fact a console displaying the flow
    needs in order to notice it is short of records and re-pull.

    ``pending`` is sent empty on purpose: it means "records the server knows are
    still streaming", and an evicted flow has no bundle for anything to be
    pending against. Declaring an empty window makes the client treat a gap as a
    real hole and repair it — which is the intended outcome here, since the
    repair read re-admits the flow.

    INVARIANT: this advisory states the delivery ``incomplete``, unconditionally.
    It is emitted in place of a frame whose RECORDS the cache threw away, and a
    delivery whose records went nowhere is by construction unfinished — the
    server holds no bundle for this flow at all, so there is nothing it could be
    complete with respect to. WHY it must be said rather than left out: this
    advisory is the only signal a push-driven console gets for the suppressed
    frame, and its consumer latches the LAST statement it was given; staying
    silent leaves an earlier ``incomplete: true`` (from the drain this eviction
    interrupted) as the standing statement, which is at least not a lie, but says
    nothing about the frame just dropped. Saying it here is what keeps the
    bounded recovery streak armed across the eviction, and that streak's re-read
    is exactly the REST read that re-admits the flow to the cache.
    """
    if hub is None or hub.client_count == 0 or not cursor:
        return
    owner = await state.get_machine_owner(machine_id)
    await hub.broadcast_owned(
        {
            "type": "history_cursor",
            "flow_id": flow_id,
            "cursor": cursor,
            "pending": {},
            "incomplete": True,
        },
        owner,
    )


async def _push_spawn_failed(
    hub: Optional["UiHub"],
    state: ServerState,
    machine_id: str,
    payload: Dict[str, Any],
) -> None:
    """Broadcast a spawn-failure event to *machine_id*'s owner UI clients.

    A spawn failure belongs to the daemon that reported it (*machine_id*), so
    it is visible only to that machine's owner (plus the admin view) — never to
    another owner's console. The frame echoes the project root, the real error
    and the originating task / issue / resume ids so the frontend can correlate
    it with the task the user just published and flip it from "published" to a
    visible error state.
    """
    if hub is None or hub.client_count == 0:
        return
    owner = await state.get_machine_owner(machine_id)
    event: Dict[str, Any] = {
        "type": UI_EVENT_SPAWN_FAILED,
        "machine_id": machine_id,
        "project_root": str(payload.get("project_root") or ""),
        "error": str(payload.get("error") or ""),
    }
    for key in ("task_description", "from_issue_id", "resume_flow_id"):
        val = payload.get(key)
        if val:
            event[key] = str(val)
    await hub.broadcast_owned(event, owner)


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
        snapshot_text = __import__("json").dumps(
            {"type": "snapshot", "machines": machines},
            ensure_ascii=False,
            default=str,
        )
        await websocket.send_text(snapshot_text)
        hub.record_send("snapshot", len(snapshot_text.encode("utf-8")))
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
    detail_registry: Optional["DetailRequestRegistry"] = None,
    window_registry: Optional["HistoryWindowRegistry"] = None,
    project_registry: Optional["ProjectCommandRegistry"] = None,
    upload_registry: Optional["UploadRequestRegistry"] = None,
    fetch_registry: Optional["UploadRequestRegistry"] = None,
    presence_debouncer: Optional["PresenceDebouncer"] = None,
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

    async def _send_welcome(message: protocol.Message) -> None:
        """Send a WELCOME straight to the socket and account its bytes.

        Every WELCOME — accept and reject alike — bypasses the ConnectionManager
        routing path, so it must be recorded here to keep the per-type
        server→daemon byte total complete.
        """
        text = message.to_json()
        await websocket.send_text(text)
        manager.record_send(message.type, len(text.encode("utf-8")))

    try:
        # The first frame MUST be a HELLO identifying the machine.
        hello_raw = await websocket.receive_text()
        try:
            hello = protocol.decode(hello_raw)
        except protocol.ProtocolError as exc:
            logger.warning("Rejecting connection: bad HELLO frame (%s)", exc)
            await _send_welcome(
                protocol.make_welcome(SERVER_VERSION, accepted=False, reason=str(exc))
            )
            await websocket.close()
            return
        if hello.type != protocol.MSG_HELLO:
            reason = f"expected HELLO, got {hello.type}"
            logger.warning("Rejecting connection: %s", reason)
            await _send_welcome(
                protocol.make_welcome(SERVER_VERSION, accepted=False, reason=reason)
            )
            await websocket.close()
            return

        machine_id = str(hello.payload.get("machine_id") or "").strip()
        if not machine_id:
            await _send_welcome(
                protocol.make_welcome(
                    SERVER_VERSION, accepted=False, reason="missing machine_id"
                )
            )
            await websocket.close()
            return

        hostname = str(hello.payload.get("hostname") or "")
        se3_version = str(hello.payload.get("se3_version") or "")
        # Record the peer's wire revision so the detail leg can degrade to
        # full-frame semantics for a pre-v3 daemon (which would silently drop a
        # MSG_DETAIL_REQUEST). Missing/blank reads as legacy downstream.
        peer_protocol_version = str(hello.payload.get("protocol_version") or "")

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
                await _send_welcome(
                    protocol.make_welcome(
                        SERVER_VERSION,
                        accepted=False,
                        reason="unauthorized daemon key",
                    )
                )
                await websocket.close()
                return
            identity.bind_machine(machine_id, owner_id)

        await state.register_machine(
            machine_id,
            hostname,
            se3_version,
            owner_id=owner_id,
            protocol_version=peer_protocol_version,
        )
        await manager.connect(machine_id, websocket)
        # The daemon is back: defuse any grace task armed by the previous
        # disconnect so a reconnect inside the window keeps the machine shown
        # online throughout (no offline flap). Done after register/connect
        # succeed so the record is already online again when we cancel.
        if presence_debouncer is not None:
            presence_debouncer.cancel(machine_id)
        await _send_welcome(protocol.make_welcome(SERVER_VERSION))
        # Hand a presence-aware daemon the current viewers level right after
        # the handshake: a daemon that reconnects while browsers are watching
        # must not idle in the low-power gear for up to a full PING interval
        # before the level self-heals it. Gated on the peer's revision — a
        # pre-v4 daemon would only reject the frame as an unknown type, so
        # skipping it spares a per-connect warning in its logs.
        if hub is not None and protocol.supports_presence(peer_protocol_version):
            await manager.send_to(
                machine_id, protocol.make_viewers(hub.client_count)
            )
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
            detail_registry,
            window_registry,
            project_registry,
            upload_registry,
            fetch_registry,
        )
    except Exception:  # WebSocketDisconnect and friends
        logger.debug("Daemon connection ended", exc_info=True)
    finally:
        if machine_id is not None:
            await manager.disconnect(machine_id, websocket)
            if presence_debouncer is not None:
                # Defer the offline transition: only mark offline + push if the
                # daemon has not reconnected within the grace window. A reconnect
                # cancels this task (see the register path above), so a fast
                # keepalive-churn reconnect never surfaces as an offline flap.
                mid = machine_id

                async def _go_offline() -> None:
                    # BACKSTOP for the reconnect-before-old-cleanup overlap: on a
                    # silent link drop (1006, no close frame) the daemon can
                    # redial and register a NEW connection while this old handler
                    # is still parked in receive(). manager.connect closes the
                    # stale socket, waking THIS handler only afterwards — so its
                    # cancel() at register time already ran as a no-op (nothing
                    # was pending yet) and the grace task armed here has nothing
                    # to defuse it. The widened HEARTBEAT_TIMEOUT (90s) makes that
                    # overlap MORE likely. So before flipping the badge offline,
                    # verify the machine is still actually disconnected: a live
                    # newer connection means the daemon is back and healthy and
                    # must stay shown online.
                    if manager.is_connected(mid):
                        return
                    await state.mark_offline(mid)
                    await _push_state(hub, state, "status_update")

                presence_debouncer.schedule_offline(machine_id, _go_offline)
            else:
                # No debouncer wired (bare tests / pre-debounce behaviour):
                # mark offline immediately.
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
    detail_registry: Optional["DetailRequestRegistry"] = None,
    window_registry: Optional["HistoryWindowRegistry"] = None,
    project_registry: Optional["ProjectCommandRegistry"] = None,
    upload_registry: Optional["UploadRequestRegistry"] = None,
    fetch_registry: Optional["UploadRequestRegistry"] = None,
) -> None:
    """Run the receive loop alongside a heartbeat loop; stop when either ends."""
    last_seen = {"ts": time.time()}

    async def receive() -> None:
        while True:
            raw = await websocket.receive_text()
            last_seen["ts"] = time.time()
            try:
                message = await _decode_frame(raw)
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
                detail_registry,
                window_registry,
                project_registry,
                upload_registry,
                fetch_registry,
                manager=manager,
                connection=websocket,
            )
            # Hand the loop back between frames. One frame's work is bounded
            # (the daemon caps every HISTORY_DATA at MAX_BYTES_PER_REPORT), but a
            # drain is ~150 of them and nothing inside applying one is guaranteed
            # to suspend — an uncontended lock, a queue that already holds the
            # next frame — so the whole reply could otherwise run as one
            # uninterrupted stretch of loop time: 1.7 s measured over this repo's
            # 20260831-095750_23865927 (see
            # ``scripts/measure_history_relay_backpressure.py``), during which no
            # other request is served. This caps the uninterrupted stretch at one
            # frame; it costs a scheduler round-trip per inbound frame.
            await asyncio.sleep(0)

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
                # Piggyback the current browser-presence count on the heartbeat
                # (the *level* half of the presence scheme): a MSG_VIEWERS edge
                # lost across a disconnect window is repaired within one PING
                # interval, at zero extra frames. Read live at send time so the
                # level always reflects the count of this instant, not the one
                # at connect. No hub wired (bare test harnesses) sends the
                # revision-3-identical payload.
                ping_text = protocol.make_ping(
                    seq=seq,
                    viewers=hub.client_count if hub is not None else None,
                ).to_json()
                await websocket.send_text(ping_text)
                # Account the heartbeat: on an idle connection the periodic PING
                # is the ONLY server→daemon traffic, so it must show in the
                # per-type metrics rather than being silently omitted.
                manager.record_send(protocol.MSG_PING, len(ping_text.encode("utf-8")))
            except Exception:
                return

    recv_task = asyncio.create_task(receive())
    beat_task = asyncio.create_task(heartbeat())
    try:
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
    finally:
        # INVARIANT: a history delivery this socket was still sending cannot
        # finish, so the bundle it was extending must not be left presenting
        # itself as the whole conversation. This is the exact detection point for
        # the failure the whole marker exists for — a 147-frame reply cut by a
        # keepalive close — and it fires here rather than in the endpoint's own
        # teardown so a bare ``_serve_loop`` (the harness the relay tests drive)
        # sees the same repair path production does.
        await state.note_machine_deliveries_interrupted(machine_id)


async def _handle_message(
    message: protocol.Message,
    state: ServerState,
    machine_id: str,
    hub: Optional["UiHub"] = None,
    registry: Optional["HistoryRequestRegistry"] = None,
    index_registry: Optional["IndexRefreshRegistry"] = None,
    interjection_tracker: Optional["InterjectionEventTracker"] = None,
    issue_registry: Optional["IssueCommandRegistry"] = None,
    detail_registry: Optional["DetailRequestRegistry"] = None,
    window_registry: Optional["HistoryWindowRegistry"] = None,
    project_registry: Optional["ProjectCommandRegistry"] = None,
    upload_registry: Optional["UploadRequestRegistry"] = None,
    fetch_registry: Optional["UploadRequestRegistry"] = None,
    *,
    manager: Optional["ConnectionManager"] = None,
    connection: Any = None,
) -> None:
    """Apply one inbound daemon message to the server state.

    *manager* and *connection* are the live daemon socket this frame arrived on;
    they let the ``MSG_HISTORY_DATA`` branch dispatch a self-heal full pull back
    to the same daemon when an ``append`` is discarded because the flow needs a
    full bundle. Both default to ``None`` (the recovery is simply skipped) so
    unit tests can drive this handler without a connection manager.
    """
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
    elif message.type == protocol.MSG_KEEPALIVE:
        # A keepalive stands in for a periodic STATUS_UPDATE when the daemon's
        # aggregated snapshot signature is unchanged: nothing changed, so there
        # is nothing to re-cache and nothing to re-broadcast to browsers. It
        # carries the same liveness meaning as a STATUS_UPDATE — refresh the
        # machine's last-seen so offline detection stays identical — but MUST
        # NOT trigger a full ``_push_state`` fan-out (that would defeat the whole
        # point of sending a keepalive instead of the ~573 KB snapshot).
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
    elif message.type == protocol.MSG_HISTORY_INDEX_DELTA:
        # Incremental index update: merge only the changed SessionMeta rows into
        # the in-memory full index, then relay the SAME delta (not the whole
        # aggregated index) to this owner's /ws/ui clients. This keeps index
        # traffic scaling with the number of *changed* flows on both the
        # daemon→server and server→browser legs. A delta is NOT a forced re-push,
        # so it does not resolve an IndexRefreshRegistry waiter (those await the
        # full MSG_HISTORY_INDEX a HISTORY_INDEX_REQUEST triggers).
        upserts = message.payload.get("upserts") or []
        removed = message.payload.get("removed") or []
        if isinstance(upserts, list) and isinstance(removed, list):
            await state.merge_history_index_delta(machine_id, upserts, removed)
            await _push_history_index_delta(
                hub, state, machine_id, upserts, removed
            )
    elif message.type == protocol.MSG_DETAIL_DATA:
        # On-demand issue/call full-text reply. Resolve the parked REST waiter so
        # the /api/issues/{id}/detail (or /api/calls/{id}/detail) handler that
        # dispatched the MSG_DETAIL_REQUEST can return the untruncated content.
        await state.touch(machine_id)
        request_id = str(message.payload.get("request_id") or "")
        if request_id and detail_registry is not None:
            detail_registry.resolve(request_id, message.payload)
    elif message.type == protocol.MSG_HISTORY_WINDOW_DATA:
        # One chunk of a step-window read. Unlike MSG_HISTORY_DATA this is NEVER
        # applied to the bundle cache: the whole point of the window leg is that
        # a flow larger than the cache budget can be served straight through to
        # the browser without a bundle for it ever existing (which is what the
        # eviction⇄full-pull storm was). The registry accumulates the chunks and
        # settles the parked REST request on the ``final`` one.
        await state.touch(machine_id)
        request_id = str(message.payload.get("request_id") or "")
        if request_id and window_registry is not None:
            window_registry.accumulate(request_id, message.payload)
    elif message.type == protocol.MSG_HISTORY_DATA:
        # History records — either an on-demand pull's reply or an active
        # flow's incremental append. Cache them, resolve any waiting REST
        # handler, and stream the delta to UI clients.
        flow_id = str(message.payload.get("flow_id") or "")
        mode = str(message.payload.get("mode") or "")
        records = message.payload.get("records") or []
        cursor = message.payload.get("cursor") or {}
        # Absent from a version-skewed daemon's frame; the gap check falls back
        # to its count-derived estimate when it is empty.
        cursor_base = message.payload.get("cursor_base") or {}
        if flow_id and isinstance(records, list):
            outcome = await state.apply_history_frame(
                flow_id,
                mode,
                records,
                cursor=cursor if isinstance(cursor, dict) else {},
                cursor_base=(
                    cursor_base if isinstance(cursor_base, dict) else {}
                ),
                machine_id=machine_id,
                usage=(
                    message.payload.get("usage")
                    if isinstance(message.payload.get("usage"), dict)
                    else None
                ),
                usage_catalog=(
                    message.payload.get("usage_catalog")
                    if isinstance(message.payload.get("usage_catalog"), dict)
                    else None
                ),
            )
            applied = outcome.resolves_pull
            # Resolve an on-demand pull waiter ONLY when this frame left the
            # cache authoritative. A periodic push-loop ``append`` that
            # ``apply_history_frame`` discarded (a first-sighting append after a
            # server restart, or a flow still flagged ``_history_requires_full``)
            # must not wake the REST handler: it would re-read the still-empty
            # cache and raise a spurious 409 while the daemon's authoritative
            # full reply to our ``MSG_HISTORY_REQUEST`` is still in flight. The
            # waiter keeps parking until the applied full reply lands (or the
            # pull times out).
            resolved_pull = False
            if registry is not None and applied:
                resolved_pull = registry.resolve(
                    flow_id,
                    # ``touch=False``: this read serves the DAEMON's frame back
                    # to a parked pull waiter, it is not a human opening the
                    # flow. Counting it as UI interest would mark every actively
                    # pushed flow hot and neuter the history-cache eviction
                    # recency (see ServerState._HISTORY_VIEW_HOT_WINDOW).
                    await state.get_history(flow_id, touch=False),
                    machine_id=machine_id,
                )
            # Accounted for EVERY frame that arrives, relayed or not, and
            # BEFORE the self-heal below can arm a pull of its own: a reply's
            # ``full`` head is suppressed from the fan-out whenever a REST caller
            # is parked on the pull, and a frame the cache discarded still cost
            # the daemon a chunk of its reply — so skipping either, or letting
            # this frame consume the marker for the pull it just triggered, would
            # leave the marker out of step with the reply it tracks and release
            # the rest of a recovery to ship whole. ``cursor_base`` goes along so
            # an append that raced an outstanding pull's dispatch — anchored at
            # the daemon's push水位, hence PAST the cursor we asked from — is not
            # mistaken for that pull's head.
            verdict = await state.take_history_replay_verdict(
                flow_id,
                mode_full=mode == protocol.HISTORY_MODE_FULL,
                chunk_bounded=lambda: _frame_is_chunk_bounded(records),
                cursor_base=cursor_base if isinstance(cursor_base, dict) else {},
                # The frame's own statement of whether its delivery is finished.
                # Absent from a pre-``final`` daemon's frame, where the
                # chunk-bound estimate above remains the only signal: such a
                # daemon's PULL replies are still tracked for completeness (the
                # estimate says whether a reply has more to come), it is only its
                # live push backlog that goes unstated — exactly the behaviour
                # that shipped before the bit existed.
                final=(
                    bool(message.payload["final"])
                    if isinstance(message.payload.get("final"), bool)
                    else None
                ),
                machine_id=machine_id,
                # …and whether the bundle actually GREW by it. ``final`` is what
                # the daemon read; this is what the cache stored, and only the
                # latter may make a bundle look more complete. A rejected full
                # counts as not applied for this purpose even though it resolves
                # a waiter: its records were refused, so it brought the bundle no
                # closer to the sender's water mark.
                applied=applied and not outcome.rejected_full,
            )
            replay = verdict.replay
            if verdict.from_pull and resolved_pull:
                # This frame of the reply woke the parked REST handler, so the
                # records of the WHOLE reply are on their way back over REST.
                # Carry that forward to the tails still to come — they are the
                # same conversation, and relaying them is the duplicate download
                # (see ServerState.mark_history_reply_served).
                await state.mark_history_reply_served(flow_id)
            # Anything the pull markers do not name came off the live
            # tail-append path, so the whole frame rides to every subscriber
            # untouched (``_push_history_data``). The verdict is ONE per frame
            # and identical for all browsers: it reads the mechanism that
            # produced the frame and nothing else — not a record's own creation
            # stamp (a naive daemon-local string a timezone apart reads hours
            # off), not a browser's subscription instant (a push loop that lags
            # one tick makes a genuine append look older than a console that
            # just opened). Both readings lazified real-time increments and made
            # one frame leave the server in two shapes.
            # Decide whether to broadcast this frame to ``/ws/ui``.
            #
            # A ``mode: full`` reply that answered an on-demand cache-miss pull
            # is suppressed: the parked REST handler(s)
            # re-read the populated cache and return the full records plus a
            # fresh ``progress`` token to exactly the clients that requested
            # them; re-broadcasting the same ``mode: full`` frame over
            # ``/ws/ui`` would make every history consumer reset its progress to
            # null (the WS full-frame path clears it), discarding the token the
            # REST response just delivered and forcing the next reconnect into
            # another full fetch + full DOM rebuild despite an unchanged cache
            # generation.
            #
            # A LIVE ``mode: append`` — one the pull markers do not name — carries
            # a real-time increment and MUST always be broadcast to
            # already-subscribed ``/ws/ui`` clients, even when it happens to
            # ``resolve`` a pull waiter. After a ``respond``/``interject`` the
            # frontend may concurrently fire a REST pull whose waiter is resolved
            # by the very ``append`` that also carries the new conversation
            # records; if we suppressed that append, every *other* subscribed
            # console (and the live view itself, until it re-enters and triggers a
            # full snapshot) would silently stop receiving new records. The
            # REST-initiating client de-duplicates the overlap via
            # ``dedupeAppendRecords``, so broadcasting it is safe.
            #
            # WHY the tails of a REST-SERVED pull reply are suppressed too: that
            # rule used to read one frame at a time, and only the head of a reply
            # knows a REST caller is parked on it — so a cache-miss open of a big
            # flow suppressed its ``full`` head and then relayed all 146
            # chunk-bounded ``append`` tails of the SAME reply, records the same
            # browser was already receiving through the REST body and its
            # token-pinned polls. Measured on this repo's own
            # ``20260831-095750_23865927``: 103.9 MB pushed down ``/ws/ui`` for
            # one open whose REST body was 18.9 MB gzipped (reproduce with
            # ``scripts/measure_history_relay_backpressure.py``). A reply is one
            # delivery, so the whole of it is suppressed once any frame of it has
            # woken a REST waiter (``ReplayVerdict.rest_served``), and the
            # consoles are told the bundle moved by the records-less
            # ``history_cursor`` advisory below — the same signal they already
            # act on for the suppressed head.
            #
            # WHY (#287) the second suppression: a ``full`` frame the cache
            # layer REJECTED as destructive (``rejected_full`` — same machine,
            # fewer records than the non-empty cached bundle: an unresolved or
            # partially resolved history directory on the daemon side) carries
            # known-truncated records. This fires for an ORDINARY flow too, and
            # must: the cache guard refuses an EMPTY full for every flow (a
            # zero-record full is never a legitimate answer for a flow the
            # server already holds records for), and only the narrower
            # shorter-but-non-empty refusal is worktree-only (an ordinary flow
            # may legitimately shrink when a failed step is retried, and that
            # frame is applied and relayed exactly as before #287).
            # Keeping it out of the CACHE is only half the defence: this
            # fan-out relays the raw daemon frame, and a WS ``mode: full`` push
            # rebuilds the receiving chat pane wholesale and clears its held
            # progress token. Relaying a rejected frame would therefore blank the
            # later rounds out of every open console — the exact loss the cache
            # guard just refused — until the next REST poll restored them from
            # the (still intact) bundle. The rejected records are authoritative
            # nowhere, so they go nowhere: clients stay on the cached bundle,
            # which the next poll serves unchanged.
            #
            # INVARIANT: records the cache DISCARDED are relayed to nobody. An
            # append the cache refused (first sighting with no bundle behind it,
            # a flow already flagged ``requires_full``, a cross-machine delta, a
            # cursor gap) is authoritative NOWHERE — yet it used to be broadcast
            # anyway, because the fan-out rule only ever looked at ``full``
            # frames. That is how a console came to hold a headless tail: it
            # applied an unanchored append the server itself had thrown away, so
            # its records could not be a suffix of any bundle, and no later frame
            # said otherwise. The bundle is repaired by the recovery pull just
            # below; the client learns of it from that pull's frame (or from the
            # advisory, when that frame is suppressed) — never from a record the
            # server does not itself vouch for.
            suppress_broadcast = (
                (resolved_pull and mode == protocol.HISTORY_MODE_FULL)
                or (verdict.from_pull and (resolved_pull or verdict.rest_served))
                or outcome.rejected_full
                or not applied
            )
            # HOP-4 DEBUG (server → UI fanout decision): whether this frame was
            # applied to the bundle and whether it will be broadcast to /ws/ui.
            # ``applied=False`` on a boundary append means state.append_history
            # discarded it (first-sighting or _history_requires_full) — the
            # persistent-freeze mode where every increment is dropped until a
            # full frame (exit/re-enter) arrives; such a frame is no longer fanned
            # out. ``suppress_broadcast=True`` also fires for a resolved mode:full
            # pull reply, and for a full frame the cache rejected as truncating
            # (``rejected_full``).
            logger.debug(
                "hist-diag ws HISTORY_DATA flow=%s mode=%s records=%d "
                "applied=%s rejected_full=%s resolved_pull=%s "
                "suppress_broadcast=%s delivery_incomplete=%s",
                flow_id, mode, len(records), applied, outcome.rejected_full,
                resolved_pull, suppress_broadcast, verdict.delivery_incomplete,
            )
            # Self-heal the requires_full stuck-state. A live ``append`` that
            # ``append_history`` discarded (first-sighting after a server
            # restart, a cross-machine desync, or a flow already flagged
            # requires_full) leaves the flow frozen: the push loop only ever
            # sends appends, so without intervention EVERY later increment is
            # dropped until a ``full`` frame lands — which historically required
            # the user to exit and re-enter the chat. Instead, ask the owning
            # daemon (over the exact socket this frame arrived on) for one
            # cursorless — hence ``full`` — pull. Its reply repopulates the
            # bundle and clears the flag, so subsequent appends flow again with
            # no manual re-enter. ``take_recovery_pull`` fires at most one pull
            # per stuck flow, so a per-cycle append storm cannot fan out.
            #
            # WHY (in-flight dedup spans the whole multi-frame drain): the reply
            # to a large flow's pull is not one frame — it is a ``full`` head plus
            # dozens of ``append`` tails, and the state marker deliberately stays
            # armed until that drain converges (or its TTL expires). So if a
            # cursor-gap discard among the still-arriving tails re-flags the flow
            # ``requires_full``, ``plan_recovery_pull`` returns ``None`` and we do
            # NOT dispatch a rival pull that would fight the drain already in
            # flight — closing the DISCARD ⇄ HISTORY_REQUEST livelock at the
            # dispatch side too.
            #
            # WHY (incremental backfill over cursorless full): on a high-loss link
            # the daemon's keepalive ping times out every ~45s and every
            # reconnect truncated the previous cursorless full drain, so the
            # bundle was perpetually rebuilt to a short prefix and the UI saw
            # records vanish then re-grow. When the server still holds a good
            # bundle from THIS machine, ``plan_recovery_pull`` returns
            # ``('incremental', cursor)`` and we ask the daemon for an ``append``
            # anchored at the server's own water mark: the reply only extends the
            # bundle, so a truncated drain leaves it shorter-but-hole-free and the
            # next reconnect continues from the new mark — it converges instead of
            # oscillating. A cursorless full is reserved for the cases where there
            # is genuinely no bundle to extend (``('full', None)``).
            plan = (
                await state.plan_recovery_pull(flow_id, machine_id)
                if (
                    not applied
                    and mode == protocol.HISTORY_MODE_APPEND
                    and manager is not None
                )
                else None
            )
            if plan is not None:
                kind, recovery_cursor = plan
                # Resolve the authoritative root exactly as the REST cache-miss
                # pull does: a worktree-mode flow splits its history across the
                # main repo root (discovery) and the worktree root (later steps),
                # so a pull with the wrong root would return only the discovery
                # slice — the freeze the worktree fix already closed.
                flow_project_root = await state.get_history_flow_project_root(
                    flow_id
                )
                sent = await request_history(
                    manager,
                    state,
                    flow_id,
                    machine_id=machine_id,
                    connection=connection,
                    # ``incremental`` carries the server's water mark so the
                    # daemon replies with an ``append`` from there; ``full``
                    # carries no cursor and rebuilds the whole bundle.
                    cursor=recovery_cursor,
                    project_root=flow_project_root or "",
                )
                logger.info(
                    "hist-diag ws HISTORY_DATA recovery-pull flow=%s kind=%s "
                    "sent=%s (self-heal requires_full)",
                    flow_id, kind, sent,
                )
                if not sent:
                    # The daemon vanished between the append and this dispatch;
                    # release the marker so a later append can re-arm recovery.
                    await state.clear_recovery_pull(flow_id)
            if not suppress_broadcast:
                await _push_history_data(
                    hub, state, machine_id, flow_id, mode, records,
                    replay=replay,
                )
            elif applied and not _mid_reply_tail(verdict):
                # The frame changed the bundle but its records may not be
                # relayed (a resolved full pull whose records are already on
                # their way back over REST, or a rejected truncating full whose
                # records are authoritative nowhere). Suppressing the records is
                # right; leaving the consoles unaware that the bundle moved is
                # not — the very frame that repairs a bundle after a discarded
                # append lands here, and a console that missed the head would
                # otherwise never be told it exists. Send the cursor/signature
                # alone: it costs nothing, rebuilds nothing, resets no token, and
                # lets a client that is short of records ask for exactly those.
                await _push_history_cursor(hub, state, machine_id, flow_id)
            elif outcome.cold_suppressed and outcome.suppressed_cursor:
                # The budget had evicted this flow and nobody had read it since,
                # so the cache took nothing and armed no recovery (see
                # ``ServerState._history_cold``). Relaying the RECORDS would
                # re-establish exactly the bundle the budget refused; saying
                # nothing at all would freeze a console that is DISPLAYING the
                # flow, because the History view self-checks only when a frame
                # arrives. So relay the frame's own cursor and nothing else: a
                # console holding fewer records than it declares re-pulls over
                # REST, and that read is what re-admits the flow to the cache.
                # No bundle meta exists to sign, so no ``signature`` /
                # ``generation`` is claimed — the client's repair budget then
                # keys the flow-scoped null bucket, which is bounded, and the
                # rebuilt bundle's real generation re-arms it.
                await _push_history_cursor_advisory(
                    hub, state, machine_id, flow_id,
                    outcome.suppressed_cursor,
                )
    elif message.type == protocol.MSG_SPAWN_FAILED:
        # The daemon could not carry out a server-dispatched spawn / resume /
        # project-init *after* the REST handler already answered 202. Relay the
        # failure to the owning console so the published task surfaces as a
        # visible error instead of staying stuck on the "published" state.
        await state.touch(machine_id)
        logger.warning(
            "SPAWN_FAILED from %s: %s (project_root=%s)",
            machine_id,
            message.payload.get("error"),
            message.payload.get("project_root"),
        )
        await _push_spawn_failed(hub, state, machine_id, message.payload)
    elif message.type == protocol.MSG_ISSUE_RESULT:
        # Daemon acknowledges an issue write command. Resolve the parked
        # future so the originating REST endpoint can return the outcome.
        request_id = str(message.payload.get("request_id") or "")
        if request_id and issue_registry is not None:
            issue_registry.resolve(request_id, message.payload)
    elif message.type == protocol.MSG_PROJECT_RESULT:
        # Daemon acknowledges a project-registry add / remove. The ack lands
        # BEFORE the daemon's follow-up fast push, so resolving here is what
        # lets the REST handler answer without waiting on a status round-trip.
        await state.touch(machine_id)
        request_id = str(message.payload.get("request_id") or "")
        if not request_id or project_registry is None:
            # An unwired registry (bare test harness) or an ack the daemon sent
            # without echoing an id: nothing to wake, and dropping it is safe —
            # the waiting REST call simply degrades to its own timeout.
            logger.debug(
                "Ignoring PROJECT_RESULT from %s with no waiter (request_id=%r)",
                machine_id,
                request_id,
            )
        else:
            project_registry.resolve(request_id, message.payload)
    elif message.type == protocol.MSG_UPLOAD_RESULT:
        # Daemon acknowledges a file upload. The ack is the ONLY signal the
        # upload produced anything: storing a file changes no snapshot state, so
        # no follow-up STATUS_UPDATE will ever carry the stored path. Touch
        # first — a daemon that just wrote a 20MB file is demonstrably alive,
        # and on a machine whose only other traffic is the idle heartbeat that
        # evidence should not be thrown away.
        await state.touch(machine_id)
        request_id = str(message.payload.get("request_id") or "")
        if not request_id or upload_registry is None:
            # An unwired registry (bare test harness) or an ack the daemon sent
            # without echoing an id: nothing to wake, and dropping it is safe —
            # the waiting REST call simply degrades to its own timeout.
            logger.debug(
                "Ignoring UPLOAD_RESULT from %s with no waiter (request_id=%r)",
                machine_id,
                request_id,
            )
        else:
            upload_registry.resolve(request_id, message.payload)
    elif message.type == protocol.MSG_FETCH_RESULT:
        # Daemon answers a file read-back. Same shape as UPLOAD_RESULT above and
        # for the same reason: reading a file changes no snapshot state, so this
        # ack is the only signal the fetch produced anything. Deliberately NOT
        # touched here, unlike the upload leg — a fetch backs a rendering path
        # that fires many times per conversation, so letting it refresh
        # last-seen would make a browser scrolling history the thing that keeps
        # a dead daemon looking alive.
        request_id = str(message.payload.get("request_id") or "")
        if not request_id or fetch_registry is None:
            # An unwired registry (bare test harness) or a reply with no echoed
            # id: nothing to wake, and dropping it is safe — the waiting REST
            # call degrades to its own timeout, and from there the browser
            # degrades to plain path text.
            logger.debug(
                "Ignoring FETCH_RESULT from %s with no waiter (request_id=%r)",
                machine_id,
                request_id,
            )
        else:
            fetch_registry.resolve(request_id, message.payload)
    else:  # pragma: no cover - decode() restricts to known daemon->server types
        logger.debug("Ignoring unexpected daemon message type %s", message.type)
