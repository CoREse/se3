"""Regression tests for the history full-pull drain ↔ push-loop cursor race.

The reported failure (node007, flow ``20260722-104526_a82315a9``): with an active
multi-MB flow open in the WebUI, the server logged a ``reason=cursor-gap`` bundle
DISCARD every ~40-70 s and the daemon answered a fresh 30+-frame HISTORY_REQUEST
~2 s later, in lockstep — the chat pane jumped between steps and every poll
re-pulled the whole bundle.

The cause was a cursor race between two concurrent daemon tasks that share the
history path but NOT a cursor. :meth:`DaemonClient._handle_history_request` drains
a full pull across dozens of frames using a request-local cursor, while
:meth:`DaemonClient._push_history` (a separate task) reads active flows off
``self._history_cursors``. A push append emitted *during* a drain declares a
``cursor_base`` computed off the stale push-side water mark, which lands past the
server's half-rebuilt water mark and trips the server's cursor-gap guard — the
guard discards the bundle and re-requests a full pull, which restarts the drain:
a self-sustaining loop.

The fix (this group):

1. a per-flow ``draining`` marker held for the whole drain so ``_push_history``
   skips (non-blocking) any flow whose drain is in flight — its append is not
   emitted mid-rebuild; and
2. a drain-end cursor sync so ``self._history_cursors[flow_id]`` is set to the
   drain's end-of-history water mark, making the NEXT push append's ``cursor_base``
   meet the server's water mark exactly.

These tests drive the real :class:`~tianluo.daemon.history.DaemonHistoryReader` over a
real multi-frame backlog, interleave a concurrent ``_push_history`` into the
middle of a drain, and replay the frames — in the true socket order — into a real
:class:`~tianluo.server.state.ServerState` to assert the server never gaps and the
bundle converges monotonically to the file's end.

The async cases drive their own event loop via ``asyncio.run``: pytest-asyncio is
not a test dependency of this project, so the suite runs on a bare pytest.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional

from tianluo.daemon import protocol
from tianluo.daemon.client import DaemonClient
from tianluo.daemon.history import DaemonHistoryReader


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


#: One record's ``content`` is sized so a handful of records fills one byte-bounded
#: chunk — a modest total backlog still spans several drain frames while the file
#: stays small enough to write quickly.
_RECORD_CONTENT_BYTES = 12_000


def _step_path(root, flow_id: str, step: str):
    return root / "tianluo" / "history" / flow_id / step


def _append_backlog(root, flow_id: str, step: str, *, start: int, count: int) -> List[str]:
    """Append *count* fat records numbered from *start*; return their bodies.

    Records are appended (mode ``"a"``) so a mid-drain call models the active flow
    growing on disk while the drain is still catching up. Each body embeds its own
    index so the assembled order can be verified exactly.
    """
    path = _step_path(root, flow_id, step)
    path.parent.mkdir(parents=True, exist_ok=True)
    bodies: List[str] = []
    with path.open("a", encoding="utf-8") as fh:
        for i in range(start, start + count):
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

    Mirrors ``test_chunked_delivery_livelock._Provider``: only the methods
    ``_push_history`` / ``_handle_history_request`` call are implemented, and the
    reads go through the real ``read_flow`` so the byte-bounded chunking and the
    "old cursor ⇒ re-read from that water mark" resume behaviour are genuine.
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
        return self._reader.read_flow(
            flow_id, project_root=project_root or self._root, cursor=cursor
        )

    def live_flow_ids(self) -> set:
        return set(self._flow_ids)


class _RecordingWS:
    """A socket stub that records every frame and (optionally) fires a hook.

    *on_frame* is awaited after each accepted frame with ``(frame_count, frame)``;
    it is how a test injects a concurrent ``_push_history`` into the middle of a
    drain deterministically (the drain sends frame → hook runs the push → drain
    continues) rather than relying on event-loop timing.
    """

    def __init__(
        self, on_frame: Optional[Callable[[int, dict], Awaitable[None]]] = None
    ) -> None:
        self.frames: List[dict] = []
        self.on_frame = on_frame

    async def send(self, data: str) -> None:
        frame = json.loads(data)
        self.frames.append(frame)
        if self.on_frame is not None:
            await self.on_frame(len(self.frames), frame)

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
    # A truncated push round re-arms fast-push; bind the event so that is a no-op
    # here rather than reaching for an unbound attribute.
    client._fast_push_event = asyncio.Event()
    return client


async def _apply(state, payload: dict):
    """Replay one HISTORY_DATA payload into the server exactly as the ws layer does.

    ``cursor_base`` is passed through (unlike the older livelock replay helper) so
    the server's cursor-gap guard sees the real coverage window — the whole point
    of this regression is whether a mid-drain push manufactures a gap.
    """
    return await state.apply_history_frame(
        payload["flow_id"],
        payload["mode"],
        payload["records"],
        cursor=payload.get("cursor"),
        cursor_base=payload.get("cursor_base"),
        machine_id="m1",
    )


# --------------------------------------------------------------------------
# the race: a push during a drain neither gaps the server nor loses records
# --------------------------------------------------------------------------


def test_push_during_drain_is_skipped_and_server_never_gaps(tmp_path):
    """A concurrent push mid-drain sends NO frame for the draining flow; the
    server assembles the whole backlog with no cursor-gap discard."""

    async def scenario():
        from tianluo.server.state import ServerState

        flow_id = "20260722-104526_a82315a9"
        step = "05_implement_9772ce1d.jsonl"
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        provider = _Provider(reader, tmp_path, [flow_id])

        # An initial backlog large enough that the full pull drains across several
        # frames (byte cap, not the 2000-record cap: total stays well under 2000).
        initial = 60
        base_bodies = _append_backlog(tmp_path, flow_id, step, start=0, count=initial)

        client = _client(provider)
        # The push side believes it has already delivered up to the file tail — the
        # realistic race precondition: its cursor is AHEAD of the server's water
        # mark, which the drain is rebuilding from zero. A mid-drain push append
        # would therefore declare a cursor_base past the server's half-rebuilt mark.
        client._history_cursors = {flow_id: {step: initial}}

        push_ws = _RecordingWS()
        grown_bodies: List[str] = []
        injected = {"done": False}

        async def on_frame(count: int, frame: dict) -> None:
            # Exactly once, right after the drain's first (full) frame: grow the
            # active flow on disk and run one push while the flow is draining.
            if injected["done"]:
                return
            payload = frame.get("payload") or {}
            if (
                frame.get("type") == protocol.MSG_HISTORY_DATA
                and payload.get("mode") == protocol.HISTORY_MODE_FULL
            ):
                injected["done"] = True
                # The flow is marked draining for the whole request.
                assert client._drain_active(flow_id)
                grown_bodies.extend(
                    _append_backlog(tmp_path, flow_id, step, start=initial, count=10)
                )
                await client._push_history(push_ws)

        drain_ws = _RecordingWS(on_frame=on_frame)
        await client._handle_history_request(
            drain_ws, {"flow_id": flow_id, "project_root": str(tmp_path)}
        )

        # The injection actually happened and the drain spanned several frames.
        assert injected["done"]
        drain_frames = drain_ws.history_frames(flow_id)
        assert len(drain_frames) >= 3
        assert drain_frames[0]["mode"] == protocol.HISTORY_MODE_FULL
        assert all(f["mode"] == protocol.HISTORY_MODE_APPEND for f in drain_frames[1:])

        # Task 3: the push emitted NO history frame for the draining flow — its
        # append was skipped, so nothing lands past the server's mid-drain mark.
        assert push_ws.history_frames(flow_id) == []
        # The skipped flow kept its old water mark (re-read after the drain), never
        # advanced past it by the skipped push.
        assert client._history_cursors[flow_id][step] == initial + 10  # drain synced

        # Replay in the TRUE socket order: drain's first frame, then whatever the
        # push put on the wire (empty, with the fix), then the rest of the drain.
        state = ServerState()
        server_order = [drain_frames[0]] + push_ws.history_frames(flow_id) + drain_frames[1:]
        server_cursors: List[int] = []
        for payload in server_order:
            await _apply(state, payload)
            # The flow is NEVER flagged for a self-heal full pull — no cursor-gap.
            assert flow_id not in state._history_requires_full
            cached = await state.get_history(flow_id)
            if cached:
                server_cursors.append(cached["cursor"][step])

        # The server's assembled cursor advanced monotonically and reached the end.
        assert server_cursors == sorted(server_cursors)
        cached = await state.get_history(flow_id)
        total = initial + 10
        assert cached["cursor"] == {step: total}
        # The whole backlog assembled once each, in order — the initial rounds plus
        # the records that grew mid-drain.
        assert _bodies(cached["records"]) == base_bodies + grown_bodies
        assert len(set(_keys(cached["records"]))) == total

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Task 2: after a drain, the synced push cursor lets the next append through
# --------------------------------------------------------------------------


def test_drain_syncs_push_cursor_so_next_append_is_accepted(tmp_path):
    """After the drain, ``_history_cursors`` is the drain-end water mark, so the
    next ``_push_history`` append meets the server's mark and is extended."""

    async def scenario():
        from tianluo.server.state import ServerState

        flow_id = "20260722-104526_a82315a9"
        step = "05_implement_9772ce1d.jsonl"
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        provider = _Provider(reader, tmp_path, [flow_id])

        initial = 60
        base_bodies = _append_backlog(tmp_path, flow_id, step, start=0, count=initial)

        client = _client(provider)
        # A deliberately WRONG push cursor before the drain (behind the true tail),
        # to prove the sync REPLACES it with the drain's end mark rather than
        # leaving a value that would gap the next append.
        client._history_cursors = {flow_id: {step: 5}}

        state = ServerState()
        drain_ws = _RecordingWS()
        await client._handle_history_request(
            drain_ws, {"flow_id": flow_id, "project_root": str(tmp_path)}
        )
        for payload in drain_ws.history_frames(flow_id):
            await _apply(state, payload)

        # Task 2: the push cursor is now the drain's end-of-history water mark,
        # matching the server's cached cursor exactly.
        assert client._history_cursors[flow_id] == {step: initial}
        cached = await state.get_history(flow_id)
        assert cached["cursor"] == {step: initial}

        # The active flow grows again; a normal push append must now be accepted
        # (cursor_base == server water mark, no gap) rather than discarded.
        grown = _append_backlog(tmp_path, flow_id, step, start=initial, count=8)
        push_ws = _RecordingWS()
        await client._push_history(push_ws)

        appends = push_ws.history_frames(flow_id)
        assert appends and appends[-1]["mode"] == protocol.HISTORY_MODE_APPEND
        assert appends[-1]["cursor_base"] == {step: initial}

        for payload in appends:
            outcome = await _apply(state, payload)
            assert outcome.resolves_pull is True

        assert flow_id not in state._history_requires_full
        cached = await state.get_history(flow_id)
        total = initial + 8
        assert cached["cursor"] == {step: total}
        assert _bodies(cached["records"]) == base_bodies + grown
        assert len(set(_keys(cached["records"]))) == total

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# a drain that COMPLETES during a concurrent push's send loop keeps its sync
# --------------------------------------------------------------------------


def test_drain_synced_cursor_survives_concurrent_push_rebind(tmp_path):
    """With a second active flow producing records, a push tick that awaits inside
    its send loop must not revert a flow whose drain finished (and synced its
    cursor) during that await back to the stale pre-drain snapshot."""

    async def scenario():
        flow_f = "20260722-104526_a82315a9"  # mid-drain
        flow_g = "20260722-090000_bbbbbbbb"  # still producing
        step_f = "05_implement_9772ce1d.jsonl"
        step_g = "01_discovery_11111111.jsonl"
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        provider = _Provider(reader, tmp_path, [flow_f, flow_g])

        _append_backlog(tmp_path, flow_f, step_f, start=0, count=20)
        # G's push cursor is behind its tail, so this tick has a real G frame to
        # send — the frame the push loop awaits through while F's drain completes.
        _append_backlog(tmp_path, flow_g, step_g, start=0, count=5)

        client = _client(provider)
        f_old = {step_f: 3}
        f_synced = {step_f: 20}
        client._history_cursors = {flow_f: dict(f_old), flow_g: {step_g: 0}}
        # F's drain is in flight for the whole tick, so the push loop skips it.
        client._history_draining.add(flow_f)

        completed = {"done": False}

        async def on_frame(count: int, frame: dict) -> None:
            payload = frame.get("payload") or {}
            if payload.get("flow_id") != flow_g or completed["done"]:
                return
            completed["done"] = True
            # While the push tick awaits G's send, F's drain finishes and writes
            # its end-of-history water mark straight onto the live cursor map, then
            # clears the draining marker — exactly the interleave issue 1 describes.
            client._history_cursors[flow_f] = dict(f_synced)
            client._history_draining.discard(flow_f)

        push_ws = _RecordingWS(on_frame=on_frame)
        await client._push_history(push_ws)

        assert completed["done"]
        # F emitted no push frame (drain owned delivery) and — the fix — the tick's
        # wholesale rebind preserved F's drain-synced cursor rather than reverting
        # it to the stale ``f_old`` snapshot.
        assert push_ws.history_frames(flow_f) == []
        assert client._history_cursors[flow_f] == f_synced
        # G advanced normally through the same tick.
        assert push_ws.history_frames(flow_g)

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# a drain that STARTS mid-send-loop skips that (later pending) flow's append
# --------------------------------------------------------------------------


def test_drain_starting_mid_send_loop_skips_that_flows_append(tmp_path):
    """If a drain for a later pending flow starts while the send loop is awaiting
    an earlier flow's send, that flow's already-computed append must not go out —
    the per-flow drain re-check before each send holds the serialization."""

    async def scenario():
        flow_a = "20260722-104526_a82315a9"
        flow_b = "20260722-090000_bbbbbbbb"
        step_a = "05_implement_9772ce1d.jsonl"
        step_b = "01_discovery_11111111.jsonl"
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        provider = _Provider(reader, tmp_path, [flow_a, flow_b])

        _append_backlog(tmp_path, flow_a, step_a, start=0, count=5)
        _append_backlog(tmp_path, flow_b, step_b, start=0, count=5)

        client = _client(provider)
        b_old = {step_b: 0}
        client._history_cursors = {flow_a: {step_a: 0}, flow_b: dict(b_old)}
        # No flow is draining when the tick starts: both A and B are pending.

        started = {"done": False}

        async def on_frame(count: int, frame: dict) -> None:
            payload = frame.get("payload") or {}
            if payload.get("flow_id") != flow_a or started["done"]:
                return
            started["done"] = True
            # A server HISTORY_REQUEST for B lands while we await A's send and
            # starts B's drain (its full HEAD is already on the wire).
            client._history_draining.add(flow_b)

        push_ws = _RecordingWS(on_frame=on_frame)
        await client._push_history(push_ws)

        assert started["done"]
        # A's append went out; B's pre-computed append did NOT — the send loop
        # re-checked B's drain state immediately before its send and skipped it.
        assert push_ws.history_frames(flow_a)
        assert push_ws.history_frames(flow_b) == []
        # B kept its old water mark so its records are re-read after the drain.
        assert client._history_cursors[flow_b] == b_old

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# a read/send failure mid-drain leaves the push cursor untouched (no dirty write)
# --------------------------------------------------------------------------


def test_drain_send_failure_does_not_write_a_dirty_cursor(tmp_path):
    """If a drain frame fails to leave the socket, the push cursor is untouched —
    a partially-drained water mark would itself manufacture a gap."""

    async def scenario():
        flow_id = "20260722-104526_a82315a9"
        step = "05_implement_9772ce1d.jsonl"
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        provider = _Provider(reader, tmp_path, [flow_id])
        _append_backlog(tmp_path, flow_id, step, start=0, count=60)

        client = _client(provider)
        prior = {step: 12}
        client._history_cursors = {flow_id: dict(prior)}

        class _DyingWS:
            """Sends the first frame, then dies — modelling a mid-drain drop."""

            def __init__(self) -> None:
                self.frames: List[dict] = []

            async def send(self, data: str) -> None:
                if self.frames:
                    raise ConnectionError("socket dropped mid-drain")
                self.frames.append(json.loads(data))

        ws = _DyingWS()
        await client._handle_history_request(
            ws, {"flow_id": flow_id, "project_root": str(tmp_path)}
        )

        # The drain aborted early (send failure), so the marker is released and the
        # push cursor is left exactly as it was — no dirty, partially-drained mark.
        assert not client._drain_active(flow_id)
        assert client._history_cursors[flow_id] == prior

    asyncio.run(scenario())
