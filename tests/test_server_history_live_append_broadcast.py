"""Regression tests for the ``MSG_HISTORY_DATA`` broadcast-suppression rule
(symptom A of the running-flow live-chat bug, #193's other half).

The bug: after a ``respond`` / ``interject`` the daemon pushes the new
conversation records as a live ``mode: append`` increment. The frontend, in the
same window, may concurrently fire a cache-miss REST pull whose waiter is then
resolved by that very append. The old ``ws.py`` suppressed the ``/ws/ui``
broadcast for *any* frame that resolved a pull waiter, so the append silently
stopped reaching already-subscribed live views — the running flow froze and only
recovered on a full re-enter that re-pulled the snapshot.

The fix narrows the suppression to a resolved ``mode: full`` cache-miss reply
(whose records the REST response itself delivers, and whose re-broadcast would
clear every consumer's progress token). A ``mode: append`` increment is always
broadcast — even when it happens to resolve a waiter — because it carries the
real-time delta every subscriber needs; the REST-initiating client de-dupes the
overlap via ``dedupeAppendRecords``.

These tests drive ``_handle_message`` directly with a real ``UiHub`` and a real
``HistoryRequestRegistry`` and assert on what reaches the UI socket.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from se3.daemon import protocol
from se3.server.state import ServerState
from se3.server.ws import HistoryRequestRegistry, UiHub, _handle_message


class _UiWS:
    """Minimal UI WebSocket stand-in capturing decoded frames it is sent."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def history_frames(self, flow_id: str | None = None) -> list:
        frames = [m for m in self.sent if m.get("type") == "history_data"]
        if flow_id is not None:
            frames = [m for m in frames if m.get("flow_id") == flow_id]
        return frames


async def _setup(owner_id: str = "owner-A"):
    """Return ``(state, hub, ui, registry)`` with one machine + UI client."""
    state = ServerState()
    await state.register_machine("m1", "host", "9.9.9", owner_id=owner_id)
    hub = UiHub()
    ui = _UiWS()
    await hub.register(ui, owner_id)
    registry = HistoryRequestRegistry()
    return state, hub, ui, registry


def _history_msg(flow_id: str, mode: str, records: list, cursor: dict | None = None):
    return protocol.make_history_data(flow_id, mode, records, cursor=cursor or {})


# --------------------------------------------------------------------------
# mode: full — a resolved cache-miss pull reply is suppressed
# --------------------------------------------------------------------------


def test_full_pull_reply_resolving_waiter_is_not_broadcast():
    async def scenario():
        state, hub, ui, registry = await _setup()
        # A REST handler parked a waiter for the cache-miss pull.
        fut = registry.register("f1", machine_id="m1")

        msg = _history_msg(
            "f1", protocol.HISTORY_MODE_FULL, [{"step": "s1", "line": 1}]
        )
        await _handle_message(msg, state, "m1", hub, registry)

        # The waiter is resolved (the REST handler will return the records),
        # and the full frame is NOT re-broadcast to /ws/ui (token protection).
        assert fut.done()
        assert ui.history_frames("f1") == []

    asyncio.run(scenario())


def test_unsolicited_full_without_waiter_is_broadcast():
    """A live ``mode: full`` replacement that resolves no waiter still streams."""

    async def scenario():
        state, hub, ui, registry = await _setup()

        msg = _history_msg(
            "f1", protocol.HISTORY_MODE_FULL, [{"step": "s1", "line": 1}]
        )
        await _handle_message(msg, state, "m1", hub, registry)

        frames = ui.history_frames("f1")
        assert len(frames) == 1
        assert frames[0]["mode"] == protocol.HISTORY_MODE_FULL

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# mode: append — always broadcast, even when it resolves a waiter
# --------------------------------------------------------------------------


def test_append_resolving_waiter_is_still_broadcast():
    """The core regression: a respond/interject append that races a REST pull.

    The append resolves the pull waiter (so the REST handler returns) AND is
    broadcast to every subscribed /ws/ui client (so the live view keeps
    appending without a re-enter).
    """

    async def scenario():
        state, hub, ui, registry = await _setup()
        # Establish the authoritative full bundle first so the append applies.
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step": "s1", "line": 1}],
            cursor={"s1": 1},
            machine_id="m1",
        )
        # A REST pull races the live append and parks a waiter.
        fut = registry.register("f1", machine_id="m1")

        msg = _history_msg(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step": "s1", "line": 2}],
            cursor={"s1": 2},
        )
        await _handle_message(msg, state, "m1", hub, registry)

        # Both happen: waiter resolved AND the append broadcast.
        assert fut.done()
        frames = ui.history_frames("f1")
        assert len(frames) == 1
        assert frames[0]["mode"] == protocol.HISTORY_MODE_APPEND
        assert frames[0]["records"] == [{"step": "s1", "line": 2}]

    asyncio.run(scenario())


def test_live_append_without_waiter_is_broadcast():
    """An ordinary active-flow append (no pull waiter) streams unchanged."""

    async def scenario():
        state, hub, ui, registry = await _setup()
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step": "s1", "line": 1}],
            cursor={"s1": 1},
            machine_id="m1",
        )

        msg = _history_msg(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step": "s1", "line": 2}],
            cursor={"s1": 2},
        )
        await _handle_message(msg, state, "m1", hub, registry)

        frames = ui.history_frames("f1")
        assert len(frames) == 1
        assert frames[0]["mode"] == protocol.HISTORY_MODE_APPEND

    asyncio.run(scenario())


def test_consecutive_appends_after_respond_keep_broadcasting():
    """After respond: the agent's follow-up turns keep streaming, no re-enter.

    Each successive append — including ones that resolve a stray pull waiter —
    reaches the live view, so the conversation continues to grow.
    """

    async def scenario():
        state, hub, ui, registry = await _setup()
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step": "s1", "line": 1}],
            cursor={"s1": 1},
            machine_id="m1",
        )

        # Round 1: the respond echo append also resolves a racing REST pull.
        registry.register("f1", machine_id="m1")
        await _handle_message(
            _history_msg(
                "f1", protocol.HISTORY_MODE_APPEND, [{"step": "s1", "line": 2}]
            ),
            state,
            "m1",
            hub,
            registry,
        )
        # Round 2 & 3: the agent's auto-produced follow-up turns (no waiter).
        await _handle_message(
            _history_msg(
                "f1", protocol.HISTORY_MODE_APPEND, [{"step": "s1", "line": 3}]
            ),
            state,
            "m1",
            hub,
            registry,
        )
        await _handle_message(
            _history_msg(
                "f1", protocol.HISTORY_MODE_APPEND, [{"step": "s1", "line": 4}]
            ),
            state,
            "m1",
            hub,
            registry,
        )

        frames = ui.history_frames("f1")
        # All three appends streamed to the live view.
        assert len(frames) == 3
        lines = [f["records"][0]["line"] for f in frames]
        assert lines == [2, 3, 4]
        assert all(f["mode"] == protocol.HISTORY_MODE_APPEND for f in frames)

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Owner scoping is preserved through the append broadcast path
# --------------------------------------------------------------------------


def test_append_broadcast_is_owner_scoped():
    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "9.9.9", owner_id="owner-A")
        hub = UiHub()
        mine = _UiWS()
        other = _UiWS()
        await hub.register(mine, "owner-A")
        await hub.register(other, "owner-B")
        registry = HistoryRequestRegistry()

        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step": "s1", "line": 1}],
            cursor={"s1": 1},
            machine_id="m1",
        )
        registry.register("f1", machine_id="m1")
        await _handle_message(
            _history_msg(
                "f1", protocol.HISTORY_MODE_APPEND, [{"step": "s1", "line": 2}]
            ),
            state,
            "m1",
            hub,
            registry,
        )
        return mine, other

    mine, other = asyncio.run(scenario())
    assert len(mine.history_frames("f1")) == 1
    assert other.history_frames("f1") == []


# --------------------------------------------------------------------------
# state.py append/snapshot consistency (task 2 acceptance)
# --------------------------------------------------------------------------


def test_appends_after_bundle_keep_applied_and_stable_generation():
    """Successive appends on an existing bundle apply and keep generation stable."""

    async def scenario():
        state = ServerState()
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step": "s1", "line": 1}],
            cursor={"s1": 1},
            machine_id="m1",
        )
        gen0 = (await state.get_history("f1"))["generation"]

        applied2 = await state.append_history(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step": "s1", "line": 2}],
            cursor={"s1": 2},
            machine_id="m1",
        )
        applied3 = await state.append_history(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step": "s1", "line": 3}],
            cursor={"s1": 3},
            machine_id="m1",
        )
        bundle = await state.get_history("f1")
        return applied2, applied3, gen0, bundle

    applied2, applied3, gen0, bundle = asyncio.run(scenario())
    assert applied2 is True
    assert applied3 is True
    # Generation unchanged across appends — outstanding progress tokens stay
    # valid, so the snapshot the broadcast and the REST reply see is consistent.
    assert bundle["generation"] == gen0
    assert [r["line"] for r in bundle["records"]] == [1, 2, 3]

    async def snap_scenario():
        # The full snapshot the REST reply serves contains every appended
        # record, matching what was broadcast incrementally (no drift / loss).
        state = ServerState()
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step": "s1", "line": 1}],
            cursor={"s1": 1},
            machine_id="m1",
        )
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step": "s1", "line": 2}],
            machine_id="m1",
        )
        return await state.get_history_snapshot("f1", expected_machine_id="m1")

    snap = asyncio.run(snap_scenario())
    assert snap is not None
    assert snap["delivery"] == "full"
    assert [r["line"] for r in snap["records"]] == [1, 2]
