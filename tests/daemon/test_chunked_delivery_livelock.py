"""Regression tests for the daemon history delivery livelock (Defect A).

The reported failure: on a supercomputing node behind a poor multi-proxy link,
the daemon↔server websocket was force-recycled roughly every ~40 s. A flow whose
``06_implement`` step held a single 8.4 MB / 815-line jsonl never rendered past
``implement`` in the web chat pane, even though every record was intact on disk
and ``se3 history show`` read all 16 steps in full. The server's cursor stayed
pinned and ``/api/history`` returned ``not_modified`` forever.

The cause was an all-or-nothing delivery frame. :meth:`DaemonClient._push_history`
shipped a flow's *entire* new backlog as one ``MSG_HISTORY_DATA`` frame and only
advanced the (delivery) cursor once that whole frame had left the socket. When
the backlog (multiple MB) could not finish transferring inside one ~40 s
connection window, the frame was discarded every window and the cursor never
advanced — a livelock insensitive to *why* the socket closed (proxy idle timeout,
heartbeat timeout, …): every window did zero net work.

The fix bounds each frame by BYTES (:data:`MAX_BYTES_PER_REPORT`) in addition to
the pre-existing record-count cap, so every frame is a chunk a very poor link can
transfer and confirm within seconds. Each confirmed chunk advances the cursor, so
every connection window makes net forward progress and a large backlog is caught
up monotonically across successive windows. A truncated round re-arms fast-push so
the next chunk drains without waiting out an idle-geared tick.

These tests drive the real :class:`~tianluo.daemon.history.DaemonHistoryReader` over a
real multi-MB jsonl backlog behind the same minimal provider the cursor-commit
suite uses, through a websocket stub that dies after a bounded per-window byte
budget — modelling "each connection is too short-lived to transfer all backlog" —
and replay exactly the frames that reached the socket into a real
:class:`~tianluo.server.state.ServerState` to assert the server ends up with the
complete, duplicate-free, monotonically-assembled history.

The async cases drive their own event loop via ``asyncio.run``: pytest-asyncio is
not a test dependency of this project, so the suite runs on a bare pytest.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from tianluo.daemon import protocol
from tianluo.daemon.client import (
    DaemonClient,
    _format_close_reason,
)
from tianluo.daemon.history import (
    DaemonHistoryReader,
    MAX_BYTES_PER_REPORT,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


#: One record's ``content`` is sized so a single record's jsonl line is a good
#: fraction of the byte cap — a handful of records fills one chunk, so a modest
#: total backlog still spans several chunks (and thus several windows) while the
#: file stays small enough to write quickly in a test.
_RECORD_CONTENT_BYTES = 12_000


def _write_backlog(root, flow_id: str, step: str, count: int) -> List[str]:
    """Write *count* fat records to one step file; return their content bodies.

    Each record body embeds its own index so the assembled order can be verified
    exactly, padded to :data:`_RECORD_CONTENT_BYTES` so the file is multiple MB —
    far larger than one :data:`MAX_BYTES_PER_REPORT` chunk.
    """
    path = root / "se3" / "history" / flow_id / step
    path.parent.mkdir(parents=True, exist_ok=True)
    bodies: List[str] = []
    with path.open("w", encoding="utf-8") as fh:
        for i in range(count):
            body = f"rec-{i:04d}-" + ("x" * _RECORD_CONTENT_BYTES)
            bodies.append(body)
            fh.write(json.dumps({"role": "assistant", "content": body}) + "\n")
    return bodies


def _keys(records: List[dict]) -> List[tuple]:
    """The ``(step_id, ordinal)`` identity the frontend reconciles by."""
    return [(r["step_id"], r["ordinal"]) for r in records]


def _bodies(records: List[dict]) -> List[str]:
    return [r["message"]["content"] for r in records]


class _Provider:
    """A HistoryProvider over a real reader, scoped to a fixed set of flows.

    Mirrors ``test_push_history_cursor_commit._Provider``: only the three methods
    ``_push_history`` calls are implemented, and the reads go through the real
    ``read_flow`` so the byte-bounded chunking and the "old cursor ⇒ re-read from
    that water mark" resume behaviour are the genuine ones.
    """

    def __init__(self, reader: DaemonHistoryReader, root, flow_ids: List[str]) -> None:
        self._reader = reader
        self._root = str(root)
        self._flow_ids = flow_ids

    def build_index(self) -> List[Any]:
        return []

    def read_active_flows(
        self, cursors: Optional[Dict[str, Dict[str, int]]] = None
    ) -> List[Any]:
        cursors = cursors or {}
        return [
            self._reader.read_flow(
                flow_id, project_root=self._root, cursor=cursors.get(flow_id)
            )
            for flow_id in self._flow_ids
        ]

    def read_flow(self, flow_id, *, project_root=None, cursor=None):
        # The on-demand HISTORY_REQUEST path calls read_flow directly; delegate to
        # the real reader so the byte-bounded chunking is the genuine one.
        return self._reader.read_flow(
            flow_id, project_root=project_root or self._root, cursor=cursor
        )

    def live_flow_ids(self) -> set:
        return set(self._flow_ids)


class _ShortLivedWS:
    """A socket stub that dies once a bounded per-window byte budget is spent.

    Every accepted frame's byte size is counted; the send that would push the
    running total past *budget_bytes* raises (and is NOT counted or recorded), so
    the frame it carried never leaves the process — exactly the partial-transfer
    loss a connection recycled mid-flight produces. Once dead, every later send
    raises too. A ``None`` budget never dies (the single long-lived-window
    control case).
    """

    def __init__(self, budget_bytes: Optional[int]) -> None:
        self._budget = budget_bytes
        self._spent = 0
        self.dead = False
        self.frames: List[dict] = []

    async def send(self, data: str) -> None:
        size = len(data.encode("utf-8"))
        if self._budget is not None and self._spent + size > self._budget:
            # This frame would overflow the window: the connection dies here and
            # the frame is dropped (never counted, never recorded).
            self.dead = True
            raise ConnectionError("connection window exhausted")
        self._spent += size
        self.frames.append(json.loads(data))

    def history_frames(self, flow_id: Optional[str] = None) -> List[dict]:
        out = [
            f["payload"]
            for f in self.frames
            if f.get("type") == protocol.MSG_HISTORY_DATA
        ]
        if flow_id is not None:
            out = [p for p in out if p["flow_id"] == flow_id]
        return out


def _client(provider: _Provider) -> DaemonClient:
    client = DaemonClient(
        "ws://test.invalid",
        machine_id="m1",
        hostname="testhost",
        se3_version="0.0.0",
        snapshot_provider=lambda: {"machine_id": "m1", "flows": []},
        history_provider=provider,
    )
    # Bind a fast-push event so the truncated-round re-arm (Task 2) is observable;
    # outside a running ``_session`` the event is ``None`` and ``_trigger_fast_push``
    # is a no-op.
    client._fast_push_event = asyncio.Event()
    return client


def _cursor_line(client: DaemonClient, flow_id: str, step: str) -> int:
    """The committed delivery water mark for *flow_id*'s *step* (0 if none)."""
    return int(client._history_cursors.get(flow_id, {}).get(step, 0) or 0)


# --------------------------------------------------------------------------
# a single read is bounded by the byte cap
# --------------------------------------------------------------------------


def test_read_flow_is_byte_bounded_and_resumes_without_loss(tmp_path):
    """One ``read_flow`` returns ≤ cap (+1 record); successive reads cover all."""
    flow_id = "20260720-163316_2df2d504"
    step = "06_implement_398863d6.jsonl"
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
    total = 60
    bodies = _write_backlog(tmp_path, flow_id, step, total)

    seen_keys: List[tuple] = []
    seen_bodies: List[str] = []
    cursor: Optional[Dict[str, int]] = None
    chunks = 0
    prev_line = 0
    while True:
        read = reader.read_flow(flow_id, project_root=str(tmp_path), cursor=cursor)
        if not read.records:
            break
        chunks += 1
        # Every chunk is byte-bounded: its records' line bytes stay within the cap
        # plus at most the one record that crossed the limit.
        frame_bytes = sum(
            len(json.dumps(r["message"]).encode("utf-8")) for r in read.records
        )
        one_record = len(json.dumps(read.records[-1]["message"]).encode("utf-8"))
        assert frame_bytes <= MAX_BYTES_PER_REPORT + one_record
        # A byte-truncated read still advances the cursor to the truncation point
        # and keeps ``append``-mode semantics for the resumed reads.
        line = read.cursor[step]
        assert line > prev_line  # strictly monotonic
        prev_line = line
        if read.truncated:
            assert read.mode in ("full", "append")
        seen_keys.extend(_keys(read.records))
        seen_bodies.extend(_bodies(read.records))
        cursor = read.cursor

    # A multi-MB single-file backlog needed several chunks (byte cap, not the
    # 2000-record cap, did the bounding: total is well under 2000 records).
    assert chunks >= 3
    assert total < 2000
    # No loss, no duplication, in ordinal order.
    assert seen_bodies == bodies
    assert [ordinal for _step, ordinal in seen_keys] == list(range(total))
    assert len(set(seen_keys)) == total


# --------------------------------------------------------------------------
# short-lived connection windows still catch up monotonically
# --------------------------------------------------------------------------


def test_short_lived_windows_make_monotonic_progress_and_catch_up(tmp_path):
    """Each too-short window nets one chunk; the cursor climbs to full delivery."""

    async def scenario():
        from tianluo.server.state import ServerState

        flow_id = "20260720-163316_2df2d504"
        step = "06_implement_398863d6.jsonl"
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        provider = _Provider(reader, tmp_path, [flow_id])
        total = 60
        bodies = _write_backlog(tmp_path, flow_id, step, total)

        client = _client(provider)
        state = ServerState()

        # A window budget wide enough for exactly one byte-bounded chunk but not
        # two — so no single connection can transfer the whole backlog and only
        # cross-window progress can ever catch up.
        window_budget = MAX_BYTES_PER_REPORT + 32 * 1024

        server_cursors: List[int] = []
        window_deliveries: List[int] = []
        windows = 0
        # Safety cap far above the handful of windows this backlog needs; a stuck
        # cursor (the original livelock) would hit it instead of hanging forever.
        while _cursor_line(client, flow_id, step) < total and windows < 50:
            windows += 1
            before = _cursor_line(client, flow_id, step)
            ws = _ShortLivedWS(window_budget)
            # Drive fast ticks within this one connection until it dies or the
            # flow has fully caught up.
            while not ws.dead and _cursor_line(client, flow_id, step) < total:
                client._fast_push_event.clear()
                await client._push_history(ws)
            # Feed exactly what reached the socket into the real server, in order.
            for frame in ws.history_frames(flow_id):
                await state.apply_history_frame(
                    frame["flow_id"],
                    frame["mode"],
                    frame["records"],
                    cursor=frame.get("cursor"),
                    machine_id="m1",
                )
            after = _cursor_line(client, flow_id, step)
            # Every window makes strictly positive net progress: the livelock is
            # precisely "a window advances the cursor by zero".
            assert after > before
            window_deliveries.append(after - before)
            cached = await state.get_history(flow_id)
            server_cursors.append(cached["cursor"][step] if cached else 0)

        # It genuinely took several windows (no single window sufficed).
        assert windows >= 3
        # The server's assembled cursor advanced monotonically, never regressing.
        assert server_cursors == sorted(server_cursors)
        assert server_cursors[-1] == total

        # The whole backlog reached the server intact, in order, once each.
        cached = await state.get_history(flow_id)
        assert _bodies(cached["records"]) == bodies
        assert len(set(_keys(cached["records"]))) == total
        assert cached["cursor"] == {step: total}

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# a truncated round re-arms fast-push (Task 2)
# --------------------------------------------------------------------------


def test_truncated_round_rearms_fast_push(tmp_path):
    """A round that leaves backlog re-arms fast-push; the final round does not."""

    async def scenario():
        flow_id = "20260720-163316_2df2d504"
        step = "06_implement_398863d6.jsonl"
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        provider = _Provider(reader, tmp_path, [flow_id])
        total = 60
        _write_backlog(tmp_path, flow_id, step, total)

        client = _client(provider)
        # A never-dying window (the control): a single long-lived connection drains
        # the whole backlog across successive fast-push rounds, each re-armed by the
        # previous truncated round.
        ws = _ShortLivedWS(None)

        rearmed_rounds = 0
        rounds = 0
        while _cursor_line(client, flow_id, step) < total and rounds < 100:
            rounds += 1
            client._fast_push_event.clear()
            await client._push_history(ws)
            if client._fast_push_event.is_set():
                rearmed_rounds += 1

        # Every non-final round left backlog and therefore re-armed fast-push, so
        # the drain never waits out an idle-geared tick.
        assert rearmed_rounds == rounds - 1
        # The final round delivered the remainder with nothing left, so it did NOT
        # re-arm (the last _push_history call left the event clear).
        assert not client._fast_push_event.is_set()
        # One long-lived window did catch the whole backlog up.
        assert _cursor_line(client, flow_id, step) == total
        assert len(ws.history_frames(flow_id)) == rounds

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# G2 — disconnect observability: close code / reason logging
# --------------------------------------------------------------------------
#
# The livelock lived behind a socket recycled ~every 40 s, but the daemon
# unwound each dropped session *silently* — the only trace in daemon.log was the
# *next* ``Dialing`` line, so a server-initiated close was indistinguishable from
# a proxy idle-timeout. These tests pin the fix: every session that ends without
# a clean shutdown or an auth rejection now logs a close code / reason (and
# records it in ``last_error``), and that reason never leaks the daemon key.


class _ClosingWS:
    """A socket stub that (optionally) yields a frame, then reports itself closed.

    The receive loop's ``async for`` ends the moment ``__anext__`` raises: a
    :class:`StopAsyncIteration` models a clean 1000/1001 iteration end while a
    supplied exception models a non-1000 close. Either way ``close_code`` /
    ``close_reason`` are already populated as the transport would leave them, and
    :func:`_format_close_reason` reads them off the socket regardless of which
    task raised.
    """

    def __init__(
        self,
        *,
        close_code=None,
        close_reason: str = "",
        raise_exc: Optional[BaseException] = None,
        preframes: Optional[List[str]] = None,
    ) -> None:
        self.close_code = close_code
        self.close_reason = close_reason
        self._raise_exc = raise_exc
        self._preframes = list(preframes or [])
        self.sent: List[str] = []

    def __aiter__(self) -> "_ClosingWS":
        return self

    async def __anext__(self) -> str:
        if self._preframes:
            return self._preframes.pop(0)
        if self._raise_exc is not None:
            raise self._raise_exc
        raise StopAsyncIteration

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _FakeConnect:
    """The async context manager ``websockets.connect(...)`` returns."""

    def __init__(self, ws: _ClosingWS) -> None:
        self._ws = ws

    async def __aenter__(self) -> _ClosingWS:
        return self._ws

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeWebsockets:
    """A stand-in for the ``websockets`` module passed into ``_session``."""

    def __init__(self, ws: _ClosingWS) -> None:
        self._ws = ws

    def connect(self, *args: Any, **kwargs: Any) -> _FakeConnect:
        return _FakeConnect(self._ws)


def _session_client(*, daemon_key: str = "") -> DaemonClient:
    """A client with no providers — ``_session`` runs HELLO + primer then races."""
    return DaemonClient(
        "ws://test.invalid",
        machine_id="m1",
        hostname="testhost",
        se3_version="0.0.0",
        snapshot_provider=lambda: {"machine_id": "m1", "flows": []},
        daemon_key=daemon_key,
        # Fast cadences so the (immediately-cancelled) push loop never stalls the
        # session teardown behind a default-length idle tick.
        status_interval=0.05,
        history_poll_interval=0.02,
    )


def _run_session(client: DaemonClient, ws: _ClosingWS) -> None:
    """Drive one full ``_session`` against *ws* to its (non-shutdown) unwind."""

    async def scenario():
        await asyncio.wait_for(
            client._session(asyncio.Event(), _FakeWebsockets(ws)), timeout=5.0
        )

    asyncio.run(scenario())


def test_format_close_reason_variants():
    """The formatter renders each close class into a non-empty, readable reason."""
    # A server-initiated close carries a real code + reason.
    ws = _ClosingWS(close_code=1008, close_reason="policy violation")
    text = _format_close_reason(ws)
    assert "1008" in text and "POLICY_VIOLATION" in text and "policy violation" in text

    # A transport drop leaves ABNORMAL_CLOSURE (1006) with no reason — still a
    # distinguishable, non-empty network-class signal.
    text = _format_close_reason(_ClosingWS(close_code=1006, close_reason=""))
    assert "1006" in text and "ABNORMAL_CLOSURE" in text

    # No close frame *and* no code: fall back to the raised exception so the
    # reason is never empty.
    text = _format_close_reason(
        _ClosingWS(close_code=None), exc=ConnectionError("transport reset")
    )
    assert "transport reset" in text and text.strip()

    # No code and no exception still yields a non-empty reason.
    assert _format_close_reason(_ClosingWS(close_code=None)).strip()


def test_session_logs_server_initiated_close(caplog):
    """A server close (code + reason) is logged and recorded, not swallowed."""
    client = _session_client()
    ws = _ClosingWS(close_code=1001, close_reason="server going away")

    with caplog.at_level("WARNING", logger="tianluo.daemon.client"):
        _run_session(client, ws)

    messages = [r.getMessage() for r in caplog.records]
    closed = [m for m in messages if "Central server connection closed" in m]
    assert closed, "the server-initiated close must be logged, not silent"
    assert "1001" in closed[0] and "server going away" in closed[0]
    assert client.last_error and "1001" in client.last_error


def test_session_logs_transport_drop_with_distinguishable_reason(caplog):
    """A transport drop with no close frame logs a distinguishable, safe reason."""
    from websockets.exceptions import ConnectionClosedError

    client = _session_client(daemon_key="SUPER-SECRET-DAEMON-KEY")
    # No close frame was exchanged: the peer's ConnectionClosedError carries no
    # rcvd/sent Close, and the transport marks the socket ABNORMAL_CLOSURE (1006).
    ws = _ClosingWS(
        close_code=1006,
        close_reason="",
        raise_exc=ConnectionClosedError(None, None),
    )

    with caplog.at_level("WARNING", logger="tianluo.daemon.client"):
        _run_session(client, ws)

    messages = [r.getMessage() for r in caplog.records]
    closed = [m for m in messages if "Central server connection closed" in m]
    assert closed, "a transport drop must not unwind silently"
    reason = closed[0]
    # Distinguishable network-class signal, non-empty, and no credential leak.
    assert "1006" in reason and "ABNORMAL_CLOSURE" in reason
    assert "SUPER-SECRET-DAEMON-KEY" not in reason
    assert client.last_error and "SUPER-SECRET-DAEMON-KEY" not in client.last_error


def test_auth_rejected_session_does_not_log_a_disconnect(caplog):
    """The auth-rejected branch is untouched: no spurious disconnect warning."""
    client = _session_client()
    # A real WELCOME(accepted=false): the receive loop dispatches it, which flags
    # ``_auth_rejected`` and unwinds the session via the abort event.
    welcome = protocol.make_welcome("srv", accepted=False, reason="unknown daemon key")
    ws = _ClosingWS(preframes=[welcome.to_json()])

    with caplog.at_level("WARNING", logger="tianluo.daemon.client"):
        _run_session(client, ws)

    messages = [r.getMessage() for r in caplog.records]
    assert not [m for m in messages if "Central server connection closed" in m]
    # The rejection reason (set by _handle_welcome) is preserved untouched.
    assert client._auth_rejected is True
    assert client.last_error == "unknown daemon key"


# --------------------------------------------------------------------------
# the delivery cursor survives a reconnect (Defect A core — the livelock)
# --------------------------------------------------------------------------
#
# The byte-bounded chunking + per-chunk commit only defeat the livelock if the
# delivery cursor SURVIVES the ~40 s reconnect. The original _session zeroed
# ``_history_cursors`` on every connection, so each new window re-read every flow
# from line 0 and re-delivered the same leading chunk — net cross-window progress
# of zero once the backlog exceeded one window. These pin that the reset is gone.


def test_session_preserves_delivery_cursor_across_reconnect():
    """A new session must NOT wipe the history delivery cursor."""
    client = _session_client()
    # A committed delivery water mark left by the previous (now-dropped) session.
    prior = {"20260720-163316_2df2d504": {"06_implement_398863d6.jsonl": 42}}
    client._history_cursors = {
        flow: dict(cur) for flow, cur in prior.items()
    }
    # A socket that closes immediately (transport drop): _session runs HELLO +
    # primer, then unwinds. With no history provider the primer touches no cursor,
    # so what survives here is exactly what the old reset would have zeroed.
    ws = _ClosingWS(close_code=1006, close_reason="")
    _run_session(client, ws)

    # The cursor is inherited by the fresh session; the next window therefore
    # resumes from the last confirmed chunk instead of re-reading from line 0.
    assert client._history_cursors == prior


def test_reconnect_resumes_drain_instead_of_restarting_from_zero(tmp_path):
    """Across real reconnects (cursor preserved), the backlog catches up once.

    Unlike ``test_short_lived_windows_...`` (one client, direct _push_history), this
    reuses the client's real reset path by re-priming a fresh session's per-session
    state between windows the way _session does — except the cursor, which the fix
    now preserves — and asserts the server assembles the whole backlog exactly once
    with no re-delivery of the leading chunk every window.
    """

    async def scenario():
        from tianluo.server.state import ServerState

        flow_id = "20260720-163316_2df2d504"
        step = "06_implement_398863d6.jsonl"
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        provider = _Provider(reader, tmp_path, [flow_id])
        total = 60
        bodies = _write_backlog(tmp_path, flow_id, step, total)

        client = _client(provider)
        state = ServerState()
        window_budget = MAX_BYTES_PER_REPORT + 32 * 1024

        windows = 0
        while _cursor_line(client, flow_id, step) < total and windows < 50:
            windows += 1
            # Emulate _session's per-connection reset of the NON-cursor session
            # state (the parts the fix still zeroes). The cursor is deliberately
            # left intact — that is the whole fix.
            client._last_history_signature = {}
            client._index_primed = False
            client._last_index = None
            client._fast_push_event = asyncio.Event()
            ws = _ShortLivedWS(window_budget)
            while not ws.dead and _cursor_line(client, flow_id, step) < total:
                client._fast_push_event.clear()
                await client._push_history(ws)
            for frame in ws.history_frames(flow_id):
                await state.apply_history_frame(
                    frame["flow_id"],
                    frame["mode"],
                    frame["records"],
                    cursor=frame.get("cursor"),
                    machine_id="m1",
                )

        assert windows >= 3  # no single window sufficed
        cached = await state.get_history(flow_id)
        # The whole backlog assembled once each, in order — not a leading chunk
        # re-delivered every window (which would duplicate ordinals).
        assert _bodies(cached["records"]) == bodies
        assert len(set(_keys(cached["records"]))) == total
        assert cached["cursor"] == {step: total}

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# a fast-push wake actually DRIVES the drain in the push loop (Task 2)
# --------------------------------------------------------------------------


def test_push_loop_fast_push_wake_drains_static_backlog(tmp_path):
    """A truncated-round fast-push wake drives the drain with NO disk change.

    A fully-written (terminal) backlog produces no disk change, so
    ``_history_changed()`` stays False and, in the idle gear, ``status_due`` is up
    to 60 s away. Only by converting the fast-push wake into a history push does the
    drain proceed at link rate; before the fix the loop consumed the wake as a bare
    STATUS_UPDATE and the cursor never moved.
    """

    async def scenario():
        flow_id = "20260720-163316_2df2d504"
        step = "06_implement_398863d6.jsonl"
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        provider = _Provider(reader, tmp_path, [flow_id])
        total = 60
        _write_backlog(tmp_path, flow_id, step, total)

        client = _client(provider)
        ws = _ShortLivedWS(None)  # one never-dying window
        stop = asyncio.Event()

        # Neutralise every OTHER push driver so the ONLY thing that can advance the
        # drain is the fast-push wake: no disk-change signal, no calls change, and a
        # status interval so long that ``status_due`` never fires within the test.
        client._history_changed = lambda: False
        client._calls_changed = lambda: False
        client._effective_intervals = lambda: (0.001, 10_000.0)

        # Report a fast-push wake on every tick (as a truncated round's re-arm
        # would), stopping once the backlog is fully caught up.
        async def fake_wait(stop_event):
            if _cursor_line(client, flow_id, step) >= total:
                stop.set()
                return False
            client._fast_push_event.clear()
            return True

        client._wait_next_tick = fake_wait

        await asyncio.wait_for(client._push_loop(ws, stop), timeout=5.0)

        # The whole backlog drained purely on fast-push wakes — no status tick and
        # no disk change ever fired. Before the fix the wake drove only a
        # STATUS_UPDATE and this timed out with the cursor pinned at line 0.
        assert _cursor_line(client, flow_id, step) == total
        assert len(ws.history_frames(flow_id)) >= 3

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# an on-demand pull delivers the COMPLETE history despite the byte cap (Task 3)
# --------------------------------------------------------------------------


def test_history_request_delivers_full_backlog_despite_byte_cap(tmp_path):
    """A HISTORY_REQUEST for a large inactive flow still delivers every record.

    The byte cap must not silently truncate an on-demand pull: the server issues a
    single request and caches whatever the reply carries, so the daemon must drain
    the whole backlog across chunk frames itself. Before the fix only the first
    ~256 KB chunk shipped and the archived history pane rendered a few dozen records
    forever.
    """

    async def scenario():
        from tianluo.server.state import ServerState

        flow_id = "20260714-120000_archived1"
        step = "06_implement_deadbeef.jsonl"
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        provider = _Provider(reader, tmp_path, [flow_id])
        total = 60  # well under the 2000-record cap; only the BYTE cap chunks it
        bodies = _write_backlog(tmp_path, flow_id, step, total)

        client = _client(provider)
        ws = _ShortLivedWS(None)

        await client._handle_history_request(
            ws, {"flow_id": flow_id, "project_root": str(tmp_path)}
        )

        frames = ws.history_frames(flow_id)
        # Multiple chunk frames left the socket (byte cap, not record cap).
        assert len(frames) >= 3
        # First frame is the full baseline; the rest are contiguous appends.
        assert frames[0]["mode"] == protocol.HISTORY_MODE_FULL
        assert all(
            f["mode"] == protocol.HISTORY_MODE_APPEND for f in frames[1:]
        )

        # Replay into a real server: the assembled bundle is the COMPLETE history.
        state = ServerState()
        for frame in frames:
            await state.apply_history_frame(
                frame["flow_id"],
                frame["mode"],
                frame["records"],
                cursor=frame.get("cursor"),
                machine_id="m1",
            )
        cached = await state.get_history(flow_id)
        assert _bodies(cached["records"]) == bodies
        assert len(set(_keys(cached["records"]))) == total
        assert cached["cursor"] == {step: total}

    asyncio.run(scenario())
