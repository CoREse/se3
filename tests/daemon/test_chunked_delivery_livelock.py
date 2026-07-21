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

These tests drive the real :class:`~se3.daemon.history.DaemonHistoryReader` over a
real multi-MB jsonl backlog behind the same minimal provider the cursor-commit
suite uses, through a websocket stub that dies after a bounded per-window byte
budget — modelling "each connection is too short-lived to transfer all backlog" —
and replay exactly the frames that reached the socket into a real
:class:`~se3.server.state.ServerState` to assert the server ends up with the
complete, duplicate-free, monotonically-assembled history.

The async cases drive their own event loop via ``asyncio.run``: pytest-asyncio is
not a test dependency of this project, so the suite runs on a bare pytest.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from se3.daemon import protocol
from se3.daemon.client import DaemonClient
from se3.daemon.history import (
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
        from se3.server.state import ServerState

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
