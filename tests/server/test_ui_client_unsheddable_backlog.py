"""What a lagging console may lose: superseded state, and nothing else.

The outbound queue that keeps a slow browser from parking the daemon's receive
loop sheds frames when it overflows. Which frames it may shed is a correctness
question, not a policy one:

* ``status_update`` is the WHOLE machine list, so the newest copy supersedes
  every older one outright. Shedding a superseded copy is a coalesce, not a loss.
* ``history_data`` belongs to a history DELIVERY, and a delivery ends with an
  explicit completeness declaration carried by its final frame (or by the
  records-less ``history_cursor`` advisory standing in for it). Shedding a middle
  frame lets that declaration arrive intact, so the console would be told the
  delivery is whole while holding records it never got — a completeness statement
  that can be false is worth less than none, because the consumer stops repairing
  on the strength of it.
* ``spawn_failed``, ``interjection_event`` and the ``history_cursor`` advisory
  have NO replay path at all. No cursor carries them, no poll re-derives them. A
  console that loses one stays connected, believing it is current, and simply
  never applies the event — indistinguishable, from the user's side, from the
  event never having happened.

So the only loss the queue may inflict on a delivery is one the client can SEE:
a backlog it cannot shrink escalates to an explicit disconnect, and
``ws.onclose`` marks the view stale and re-reads everything on reconnect.
"""

from __future__ import annotations

import asyncio
import json

from tianluo.server import ws as ws_module
from tianluo.server.ws import UiHub

OWNER = "owner-A"


class _StuckConsole:
    """A client whose socket accepts nothing until it is released."""

    def __init__(self):
        self.gate = None
        self.frames = []
        self.closed_with = None

    async def send_text(self, data):
        if self.gate is not None:
            await self.gate.wait()
        self.frames.append(json.loads(data))

    async def close(self, code=1000):
        self.closed_with = code

    def typed(self, ptype):
        return [f for f in self.frames if f.get("type") == ptype]


def _event(index):
    """A one-shot lifecycle event: no cursor, no poll, no way to re-derive it."""
    return {
        "type": "spawn_failed",
        "machine_id": "m1",
        "error": "boom %d" % index,
        "task_id": "task-%d" % index,
    }


def _relay(index):
    return {
        "type": "history_data",
        "flow_id": "F",
        "mode": "append",
        "cursor": {"01_x.jsonl": index},
        "records": [{"step_id": "01_x", "ordinal": index, "message": {}}],
    }


def test_unsheddable_events_survive_a_backlog_the_trim_cannot_shrink():
    """Every one-shot event reaches a console that was merely slow."""

    async def scenario():
        console = _StuckConsole()
        console.gate = asyncio.Event()
        hub = UiHub()
        await hub.register(console, OWNER)
        # Twice the soft frame bound, so the old index-0 fallback would have
        # deleted the oldest half of them.
        total = ws_module.UI_CLIENT_QUEUE_MAX_FRAMES * 2
        for index in range(total):
            await hub.broadcast_owned(_event(index), OWNER)
        channel = hub._channels[console]
        assert channel.dropped == 0, "a one-shot event has no replay path"
        console.gate.set()
        assert await hub.wait_drained(timeout=10)
        return [f["task_id"] for f in console.typed("spawn_failed")], total

    delivered, total = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    assert delivered == ["task-%d" % i for i in range(total)]


def test_relay_frames_of_a_delivery_are_never_shed():
    """A mixed backlog keeps every ``history_data`` frame it was handed.

    A delivery's own final frame declares it complete. Dropping an earlier frame
    of the same delivery would let that declaration through over a hole, so the
    frames are kept and the backlog is left for the hard ceiling to escalate.
    """

    async def scenario():
        console = _StuckConsole()
        console.gate = asyncio.Event()
        hub = UiHub()
        await hub.register(console, OWNER)
        events = 0
        relays = 0
        for index in range(ws_module.UI_CLIENT_QUEUE_MAX_FRAMES * 3):
            if index % 8 == 0:
                events += 1
                await hub.broadcast_owned(_event(index), OWNER)
            else:
                relays += 1
                await hub.broadcast_owned(_relay(index), OWNER)
        channel = hub._channels[console]
        assert channel.dropped == 0, (
            "a frame of a history delivery has no silent-loss budget"
        )
        console.gate.set()
        assert await hub.wait_drained(timeout=10)
        return events, relays, console

    events, relays, console = asyncio.run(
        asyncio.wait_for(scenario(), timeout=30)
    )
    assert len(console.typed("history_data")) == relays, (
        "every relayed record must reach a console that was merely slow"
    )
    assert len(console.typed("spawn_failed")) == events, (
        "and the one-shot events between them likewise"
    )


def test_a_backlog_that_cannot_be_shed_disconnects_instead_of_deleting():
    """At the hard ceiling the socket is closed, not quietly emptied.

    A console this far behind is effectively dead. Closing it is a loss the
    frontend can SEE and repair; deleting its events is one it cannot.
    """

    async def scenario():
        console = _StuckConsole()
        console.gate = asyncio.Event()
        hub = UiHub()
        await hub.register(console, OWNER)
        for index in range(ws_module.UI_CLIENT_QUEUE_HARD_FRAMES + 8):
            await hub.broadcast_owned(_event(index), OWNER)
            if console not in hub._channels:
                break
        # Let the retire task run: it unregisters, closes, and frees the backlog.
        for _ in range(20):
            await asyncio.sleep(0)
        console.gate.set()
        for _ in range(20):
            await asyncio.sleep(0)
        return console, hub

    console, hub = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    assert console.closed_with == 1013, (
        "the client must be closed (1013 try-again-later), so its reconnect "
        "re-reads everything"
    )
    assert hub.client_count == 0, "and it must be retired from the hub"


def test_a_delivery_backlog_disconnects_rather_than_declaring_it_complete():
    """The one loss a history delivery may suffer is a visible one.

    A console too far behind to hold the delivery's frames is closed, so its
    reconnect re-reads the bundle. What must NOT happen is the alternative the
    trim used to take: drop the middle frames, deliver the final one, and leave
    the console holding a partial conversation it has been told is whole.
    """

    async def scenario():
        console = _StuckConsole()
        console.gate = asyncio.Event()
        hub = UiHub()
        await hub.register(console, OWNER)
        sent = 0
        for index in range(ws_module.UI_CLIENT_QUEUE_HARD_FRAMES + 8):
            await hub.broadcast_owned(_relay(index), OWNER)
            sent += 1
            if console not in hub._channels:
                break
        # The terminator of the same delivery, emitted after the client is gone.
        final = dict(_relay(sent), final=True, incomplete=False)
        await hub.broadcast_owned(final, OWNER)
        for _ in range(20):
            await asyncio.sleep(0)
        console.gate.set()
        for _ in range(20):
            await asyncio.sleep(0)
        return console, hub

    console, hub = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    assert console.closed_with == 1013, "the client must be disconnected"
    assert hub.client_count == 0
    assert not [f for f in console.typed("history_data") if f.get("final")], (
        "a console that lost frames of this delivery must never receive its "
        "completeness declaration"
    )


def test_superseded_whole_state_frames_are_coalesced_not_kept():
    """A ``status_update`` is the whole machine list: the newest wins.

    Shedding a superseded copy is a coalesce, not a loss — which is why it is
    droppable, while the one-shot events interleaved with it are not.
    """

    async def scenario():
        console = _StuckConsole()
        console.gate = asyncio.Event()
        hub = UiHub()
        await hub.register(console, OWNER)
        total = ws_module.UI_CLIENT_QUEUE_MAX_FRAMES * 3
        for index in range(total):
            await hub.broadcast_owned(
                {"type": "status_update", "machines": [{"machine_id": index}]},
                OWNER,
            )
        await hub.broadcast_owned(_event(999), OWNER)
        console.gate.set()
        assert await hub.wait_drained(timeout=10)
        return console, total

    console, total = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    updates = console.typed("status_update")
    assert len(updates) < total, "superseded machine lists must be coalesced"
    assert updates[-1]["machines"][0]["machine_id"] == total - 1, (
        "the newest list — the only one that is still true — must survive"
    )
    assert len(console.typed("spawn_failed")) == 1, (
        "and the one-shot event queued behind them must not be collateral"
    )


def test_a_single_oversized_relay_frame_is_still_delivered():
    """The byte budget bounds a BACKLOG, not one frame.

    A whole-bundle relay of a big flow is legitimately larger than the budget;
    dropping every such frame would mean a big flow never streams at all.
    """

    async def scenario():
        console = _StuckConsole()
        hub = UiHub()
        await hub.register(console, OWNER)
        big = {
            "type": "history_data",
            "flow_id": "F",
            "records": [{"m": "x" * (ws_module.UI_CLIENT_QUEUE_MAX_BYTES + 1024)}],
        }
        await hub.broadcast_owned(big, OWNER)
        assert await hub.wait_drained(timeout=10)
        return console.typed("history_data")

    assert len(asyncio.run(asyncio.wait_for(scenario(), timeout=30))) == 1
