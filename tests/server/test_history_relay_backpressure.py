"""A big flow's history drain must not be paid for by the daemon socket.

The defect these tests pin down: opening a large COMPLETED flow answers one
``MSG_HISTORY_REQUEST`` with a ``full`` head plus ~146 chunk-bounded ``append``
tails, and every one of those tails used to be relayed to ``/ws/ui`` from inside
``ws._serve_loop.receive()`` — the single coroutine that reads the daemon socket.
The relay ended in ``await client.send_text(...)``, so a browser that could not
drain parked the DAEMON's receive path; the daemon's Pong stopped being consumed
behind the backlog and uvicorn's WS keepalive closed that connection with
``1011 INTERNAL_ERROR: keepalive ping timeout`` mid-drain. What survived was a
self-consistent PREFIX of the conversation (its cursor described exactly the step
files that had arrived, its pending window was empty), so neither side could tell
the tail was missing — the flow's commit/summarize records were simply gone.

Three things are asserted, and they are independent:

* the daemon receive path is never parked by a slow console (the head-of-line
  block), so the whole reply is ingested and the bundle reaches its LAST record;
* the event loop keeps turning between frames, so a ~150-frame reply does not
  become one uninterrupted stretch in which no other request is served;
* one open delivers the conversation ONCE. The head of a REST-served reply was
  already suppressed; its tails were not, so the same records went down
  ``/ws/ui`` a second time (103.9 MB measured for this flow, against an 18.9 MB
  gzipped REST body) — enough traffic on one link to starve the browser's own
  connection pool.

The load is the real ``20260831-095750_23865927`` from this repo's
``tianluo/history/``: 52 steps, 4324 records, 147 daemon frames. Its SHAPE — the
per-step record counts and the daemon's own per-frame chunk boundaries, read back
through ``DaemonHistoryReader.read_flow`` — is committed as
``tests/fixtures/history_relay_shape_20260831.json``; the record bodies are
synthesized here, and the daemon's byte budget is scaled down by the same factor
(``_CHUNK_BYTES``) so every frame still lands on the side of the chunk bound it
landed on in production while the whole load stays a couple of MB. Reproduce the
un-scaled numbers with ``scripts/measure_history_relay_backpressure.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from _authsrv import authed_app, authed_hello, login
from tianluo.daemon import protocol
from tianluo.server import ws as ws_module
from tianluo.server.state import ServerState
from tianluo.server.ws import (
    ConnectionManager,
    HistoryRequestRegistry,
    UiHub,
    _serve_loop,
)

MACHINE = "m1"
OWNER = "owner-A"
PROJECT_ROOT = "/tmp/relay-backpressure-repo"

#: The daemon's per-frame byte budget for these tests. Scaled down from the real
#: ``MAX_BYTES_PER_REPORT`` (256 KiB) so the fixture's 147 frames cost ~2.4 MB
#: instead of ~49 MB: what the server reads off a frame is whether it REACHED the
#: budget (``_frame_is_chunk_bounded`` — "more of this reply is coming"), and that
#: verdict is preserved exactly by scaling the budget and the padding together.
_CHUNK_BYTES = 16 * 1024

_SHAPE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures"
        / "history_relay_shape_20260831.json"
    ).read_text(encoding="utf-8")
)


def _flat_records():
    """Every record of the sample flow, in the order the daemon reads them."""
    records = []
    for step in _SHAPE["steps"]:
        step_id = step["step_id"]
        step_type = step_id.split("_", 1)[1].rsplit("_", 1)[0]
        for ordinal in range(step["records"]):
            records.append(
                {
                    "step_id": step_id,
                    "step_type": step_type,
                    "ordinal": ordinal,
                    "message": {
                        "role": "assistant",
                        "content": "record %s#%d" % (step_id, ordinal),
                    },
                }
            )
    return records


def _frames():
    """The 147 wire frames of one pull reply, cursors and all.

    Each frame but the last carries one padded record so it REACHES
    :data:`_CHUNK_BYTES` — the daemon's "this reply has more to come" signal —
    and the final zero-record frame stays under it, which is what retires the
    reply's replay marker.
    """
    records = _flat_records()
    frames = []
    cursor = {}
    index = 0
    for position, count in enumerate(_SHAPE["frames"]):
        chunk = records[index:index + count]
        index += count
        if chunk and position < len(_SHAPE["frames"]) - 1:
            chunk[0] = dict(
                chunk[0],
                message=dict(chunk[0]["message"], content="x" * _CHUNK_BYTES),
            )
        base = {
            rec["step_id"] + ".jsonl": cursor.get(rec["step_id"] + ".jsonl", 0)
            for rec in chunk
        }
        for rec in chunk:
            cursor[rec["step_id"] + ".jsonl"] = rec["ordinal"] + 1
        frames.append(
            {
                "mode": (
                    protocol.HISTORY_MODE_FULL
                    if not frames
                    else protocol.HISTORY_MODE_APPEND
                ),
                "records": chunk,
                "cursor": dict(cursor),
                "cursor_base": base,
            }
        )
    assert len(frames) == 147
    assert sum(len(f["records"]) for f in frames) == 4324
    return frames


@pytest.fixture()
def scaled_chunk_bound(monkeypatch):
    """Scale the daemon's frame budget down with the fixture's record bodies."""
    monkeypatch.setattr(ws_module, "MAX_BYTES_PER_REPORT", _CHUNK_BYTES)
    return _CHUNK_BYTES


class _SlowConsole:
    """A ``/ws/ui`` client that costs *delay* seconds to accept each frame."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.frames = []

    async def send_text(self, data):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.frames.append(json.loads(data))

    def typed(self, ptype):
        return [f for f in self.frames if f.get("type") == ptype]


class _DaemonLeg:
    """The daemon socket ``_serve_loop`` reads, replaying one reply's frames.

    It measures the PARK: the gap between a frame being handed to the receive
    loop and the loop coming back for the next one. That gap is the window in
    which the daemon's inbound queue goes undrained — so a Pong queued behind
    the reply is not processed and the transport keepalive fires. Measuring it
    here rather than around ``_handle_message`` keeps the test on the same code
    path production runs, decode and all.
    """

    def __init__(self, frames):
        self._frames = list(frames)
        self._index = 0
        self._handed_at = None
        self.parks = []

    async def receive_text(self):
        now = time.perf_counter()
        if self._handed_at is not None:
            self.parks.append(now - self._handed_at)
        if self._index >= len(self._frames):
            raise RuntimeError("reply exhausted")
        frame = self._frames[self._index]
        self._index += 1
        self._handed_at = time.perf_counter()
        return frame

    async def send_text(self, text):
        return None

    async def close(self):
        return None


async def _drain(state, hub, registry, frames):
    """Replay *frames* through the real daemon receive loop; return its parks."""
    leg = _DaemonLeg(
        [
            protocol.make_history_data(
                "flow-relay",
                frame["mode"],
                frame["records"],
                cursor=frame["cursor"],
                cursor_base=frame["cursor_base"],
            ).to_json()
            for frame in frames
        ]
    )
    manager = ConnectionManager()
    await manager.connect(MACHINE, leg)
    await _serve_loop(leg, manager, state, MACHINE, hub, registry)
    return leg.parks


async def _armed_state(hub_client):
    """A state + hub with one console attached, ready to take daemon frames."""
    state = ServerState()
    await state.register_machine(MACHINE, "host", "1.2.3", owner_id=OWNER)
    hub = UiHub()
    await hub.register(hub_client, OWNER)
    registry = HistoryRequestRegistry()
    return state, hub, registry


# --------------------------------------------------------------------------
# the head-of-line block
# --------------------------------------------------------------------------


def test_slow_console_never_parks_the_daemon_receive_path(scaled_chunk_bound):
    """The whole 147-frame reply drains without the browser's link in the way.

    A console costing 50 ms per frame would have charged the daemon's receive
    coroutine ~7.4 s over this drain — and a real one, measured against the live
    server, 8–14 s on a SINGLE frame, which is what the 20 s keepalive killed.
    """
    console = _SlowConsole(delay=0.05)
    frames = _frames()
    parks = []
    lateness = []

    async def scenario():
        state, hub, registry = await _armed_state(console)
        registry.register("flow-relay", machine_id=MACHINE)
        await state.mark_history_replay("flow-relay", cursor=None)

        stop = asyncio.Event()
        turns = {"n": 0}

        async def ticker():
            while not stop.is_set():
                due = time.perf_counter() + 0.005
                await asyncio.sleep(0.005)
                lateness.append(time.perf_counter() - due)

        async def spinner():
            # Counts how many chances ANY other coroutine got to run while the
            # drain was in progress — a REST handler, the sidebar read, another
            # daemon's frame.
            while not stop.is_set():
                turns["n"] += 1
                await asyncio.sleep(0)

        tick = asyncio.ensure_future(ticker())
        spin = asyncio.ensure_future(spinner())
        started = time.perf_counter()
        parks.extend(await _drain(state, hub, registry, frames))
        wall = time.perf_counter() - started
        stop.set()
        await tick
        await spin
        bundle = await state.get_history("flow-relay", touch=False)
        return wall, turns["n"], bundle["records"]

    wall, turns, records = asyncio.run(asyncio.wait_for(scenario(), timeout=120))

    # The console's own cost for this drain is 147 * 50 ms = 7.35 s. The receive
    # path must not have paid it.
    assert max(parks) < 0.5, "one frame parked the daemon receive path"
    assert wall < 3.5, "the drain ran at the browser's pace, not the daemon's"
    # Nothing CPU-shaped got introduced in exchange (the keepalive-level bar),
    # and the loop kept turning THROUGHOUT — a drain that holds the loop for its
    # whole length serves no other request while it runs, which is how a busy
    # server stops answering the sidebar.
    assert max(lateness) < 1.0
    # …and the loop turned at least once per inbound frame: a 147-frame reply
    # applied as one uninterrupted stretch serves no other request while it runs,
    # which is how a busy server stops answering the sidebar.
    assert turns >= len(frames), "the drain monopolised the event loop"

    # …and the whole reply landed: the bundle reaches its last record.
    assert len(records) == 4324
    assert records[-1]["step_id"] == "52_summarize_6201ea5c"
    assert records[-1]["ordinal"] == _SHAPE["steps"][-1]["records"] - 1


def test_lagging_console_backlog_stays_bounded(scaled_chunk_bound):
    """A console that cannot keep up costs bounded memory — never lost records.

    The backlog of a slow console is bounded, but NOT by shedding the delivery's
    own frames: a delivery ends with an explicit completeness declaration, and
    dropping a middle frame would let that declaration reach a console holding a
    hole. So the frames are kept and the bound that applies is the hard ceiling,
    past which the client is disconnected instead — the one loss the frontend can
    see and repair (``ws.onclose`` → stale → reconnect re-reads the bundle).
    """
    console = _SlowConsole(delay=0.05)
    frames = _frames()

    async def scenario():
        state, hub, registry = await _armed_state(console)
        # No pull marker: every frame is live traffic, so all of them are
        # relayed and the queue is under real pressure.
        await _drain(state, hub, registry, frames)
        channel = hub._channels[console]
        return (
            len(channel._queue), channel._bytes, channel.dropped,
            channel.overflowed,
        )

    depth, backlog, dropped, overflowed = asyncio.run(
        asyncio.wait_for(scenario(), timeout=120)
    )
    assert dropped == 0, "a frame of a history delivery is never shed"
    assert depth <= ws_module.UI_CLIENT_QUEUE_HARD_FRAMES
    assert backlog <= ws_module.UI_CLIENT_QUEUE_HARD_BYTES
    assert depth > 0, "the console kept up after all; the case proves nothing"
    # This console is merely slow, not dead: a whole 147-frame reply of the
    # sample flow stays well inside the ceiling, so it keeps its socket.
    assert not overflowed


def test_healthy_console_still_has_its_frames_on_return():
    """A client that can keep up is still delivered to synchronously.

    The queue is a backpressure valve, not a deferral: for every consumer whose
    send completes without waiting on I/O, ``broadcast_owned`` returning still
    means the frame has been handed over — which is what the rest of the suite
    (and the hub's own accounting) reads.
    """
    console = _SlowConsole()

    async def scenario():
        hub = UiHub()
        await hub.register(console, OWNER)
        await hub.broadcast_owned({"type": "machines", "machines": []}, OWNER)
        return len(console.frames)

    assert asyncio.run(scenario()) == 1


# --------------------------------------------------------------------------
# the duplicate delivery
# --------------------------------------------------------------------------


def test_rest_served_pull_reply_is_not_also_pushed(scaled_chunk_bound):
    """One open delivers the conversation once, not once per transport."""
    console = _SlowConsole()
    frames = _frames()

    async def scenario():
        state, hub, registry = await _armed_state(console)
        waiter = registry.register("flow-relay", machine_id=MACHINE)
        await state.mark_history_replay("flow-relay", cursor=None)
        await _drain(state, hub, registry, frames)
        await hub.wait_drained(timeout=10)
        bundle = await state.get_history("flow-relay", touch=False)
        return waiter, bundle["records"]

    waiter, records = asyncio.run(asyncio.wait_for(scenario(), timeout=120))

    # The REST caller was served…
    assert waiter.done()
    # …so not one record of that reply was pushed a second time.
    assert console.typed("history_data") == []
    # The consoles are still told the bundle moved, with the cursor they need to
    # notice they are short of records and ask for exactly those — but ONCE per
    # reply, not once per frame: a mid-drain water mark is obsolete before a
    # browser can act on it, and acting on it means a full re-pull of the very
    # bundle already being delivered (see ws._mid_reply_tail).
    advisories = console.typed("history_cursor")
    assert advisories, "the suppressed reply told the console nothing"
    assert len(advisories) <= 2, "one advisory per reply, not one per frame"
    assert advisories[-1]["cursor"]
    # The last word is the settled bundle, so a client that IS short of records
    # repairs against the final cursor rather than a moving one.
    assert sum(advisories[-1]["cursor"].values()) == 4324
    # And the server holds the whole conversation for those requests to read.
    assert len(records) == 4324


def test_pull_reply_with_no_rest_caller_still_streams(scaled_chunk_bound):
    """Suppression is scoped to the duplicate, not to replays in general.

    A self-heal / reconnect-backfill pull has no REST caller behind it, so its
    records are the console's only copy and must still be relayed.
    """
    console = _SlowConsole()
    frames = _frames()[:4]

    async def scenario():
        state, hub, registry = await _armed_state(console)
        await state.mark_history_replay("flow-relay", cursor=None)
        await _drain(state, hub, registry, frames)
        await hub.wait_drained(timeout=10)
        return console.typed("history_data")

    pushed = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert len(pushed) == len(_frames()[:4])
    assert sum(len(f["records"]) for f in pushed) == sum(
        len(f["records"]) for f in _frames()[:4]
    )


def test_live_appends_are_unaffected(scaled_chunk_bound):
    """A real-time increment behind no pull marker still rides whole."""
    console = _SlowConsole()

    async def scenario():
        state, hub, registry = await _armed_state(console)
        first = _frames()[0]
        live = {
            "mode": protocol.HISTORY_MODE_APPEND,
            "records": [
                {
                    "step_id": "01_discovery_ffd5c452",
                    "step_type": "discovery",
                    "ordinal": first["cursor"]["01_discovery_ffd5c452.jsonl"],
                    "message": {"role": "assistant", "content": "live tail"},
                }
            ],
            "cursor": dict(
                first["cursor"],
                **{
                    "01_discovery_ffd5c452.jsonl": first["cursor"][
                        "01_discovery_ffd5c452.jsonl"
                    ]
                    + 1
                },
            ),
            "cursor_base": {
                "01_discovery_ffd5c452.jsonl": first["cursor"][
                    "01_discovery_ffd5c452.jsonl"
                ]
            },
        }
        await _drain(state, hub, registry, [first, live])
        await hub.wait_drained(timeout=10)
        return console.typed("history_data")

    pushed = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert pushed, "the live append never reached the console"
    tail = pushed[-1]["records"][-1]
    assert tail["message"]["content"] == "live tail"


# --------------------------------------------------------------------------
# end to end: the browser's copy still reaches the last record
# --------------------------------------------------------------------------


class _DrainDaemon:
    """A connected daemon that answers one HISTORY_REQUEST with the whole reply.

    The REST pull parks inside a blocking ``TestClient.get``, so the reply has to
    be produced from another thread — exactly as the real daemon produces it,
    frame after frame down one socket.
    """

    def __init__(self, client, app, flow_id, frames):
        self.flow_id = flow_id
        self.frames = frames
        self.requests = 0
        self.sent = threading.Event()
        self._ctx = client.websocket_connect("/ws")
        self.sock = self._ctx.__enter__()
        self.sock.send_text(authed_hello(app, MACHINE, "host", "12.14.0"))
        protocol.decode(self.sock.receive_text())  # WELCOME
        self.sock.send_text(
            protocol.make_status_update(
                {
                    "machine_id": MACHINE,
                    "hostname": "host",
                    "project_roots": [PROJECT_ROOT],
                    "flows": [
                        {
                            "flow_id": flow_id,
                            "status": "completed",
                            "project_root": PROJECT_ROOT,
                        }
                    ],
                }
            ).to_json()
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                msg = protocol.decode(self.sock.receive_text())
            except Exception:
                return
            if msg.type != protocol.MSG_HISTORY_REQUEST:
                continue
            self.requests += 1
            for frame in self.frames:
                self.sock.send_text(
                    protocol.make_history_data(
                        self.flow_id,
                        frame["mode"],
                        frame["records"],
                        cursor=frame["cursor"],
                        cursor_base=frame["cursor_base"],
                    ).to_json()
                )
            self.sent.set()

    def close(self):
        self._stop.set()
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass


class _Console:
    """A ``/ws/ui`` subscriber reading in its own thread, as a browser does."""

    def __init__(self, client):
        self._ctx = client.websocket_connect("/ws/ui")
        self.sock = self._ctx.__enter__()
        self.frames = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self):
        while not self._stop.is_set():
            try:
                self.frames.append(json.loads(self.sock.receive_text()))
            except Exception:
                return

    def typed(self, ptype):
        return [f for f in self.frames if f.get("type") == ptype]

    def close(self):
        self._stop.set()
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass


def test_open_of_a_big_completed_flow_reaches_the_tail(scaled_chunk_bound):
    """End to end: a cache-miss open converges on all 4324 records.

    This is the user-visible acceptance: the console's copy must reach the
    ``52_summarize`` step — the exact records the keepalive-killed drain used to
    drop — and it must get there WITHOUT the reply also being pushed to it.
    """
    from fastapi.testclient import TestClient

    flow_id = "20260831-095750_23865927"
    frames = _frames()
    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        console = _Console(client)
        daemon = _DrainDaemon(client, app, flow_id, frames)
        try:
            # The open: a cache miss, so the server pulls from the daemon and
            # parks until the reply's first applied frame lands.
            first = client.get(f"/api/history/{flow_id}")
            assert first.status_code == 200, first.text
            body = first.json()
            assert body["delivery"] == "full"
            assert daemon.sent.wait(timeout=60), "the daemon reply never finished"

            # From here the console is on the ordinary token-pinned poll — the
            # path a browser uses once it holds a bundle.
            held = {
                (r["step_id"], r["ordinal"]) for r in body.get("records") or []
            }
            token, sig = body.get("progress"), body.get("sig")
            deadline = time.time() + 60
            while len(held) < 4324 and time.time() < deadline:
                url = f"/api/history/{flow_id}"
                if token:
                    url += f"?after={token}&sig={sig}"
                poll = client.get(url)
                assert poll.status_code == 200, poll.text
                data = poll.json()
                for record in data.get("records") or []:
                    held.add((record["step_id"], record["ordinal"]))
                token, sig = data.get("progress") or token, data.get("sig") or sig
                if data.get("delivery") == "not_modified":
                    time.sleep(0.05)

            assert len(held) == 4324, "the conversation stopped short of its tail"
            assert ("52_summarize_6201ea5c", 26) in held

            # The same records were NOT also pushed down /ws/ui.
            time.sleep(0.2)
            relayed = sum(
                len(f.get("records") or []) for f in console.typed("history_data")
            )
            assert relayed == 0, (
                "the reply was delivered twice: %d records also pushed" % relayed
            )
        finally:
            daemon.close()
            console.close()
