"""Tests for the WS delivery path of history frames (G3).

Two delivery-side defects made the head-loss defect possible, and both are fixed
here rather than being papered over by the client's backfill:

* a ``history_data`` frame carried only records — no ``cursor`` — so a client on
  the push path had no authoritative statement of what the bundle contains and
  could not tell that it was short of records;
* an append the cache DISCARDED was broadcast anyway, handing consoles an
  unanchored tail that is a suffix of no bundle, while the ``full`` frame that
  later repaired the bundle could be suppressed — so the head existed on the
  server and was announced to nobody.
"""

from __future__ import annotations

import asyncio
import json

from tianluo.daemon import protocol
from tianluo.server.state import ServerState
from tianluo.server.ws import (
    ConnectionManager,
    HistoryRequestRegistry,
    UiHub,
    _handle_message,
)

STEP = "01_discovery_9ed2a95c"
CURSOR_KEY = f"{STEP}.jsonl"


def _rec(ordinal, role, content):
    return {
        "step_id": STEP,
        "step_type": "discovery",
        "ordinal": ordinal,
        "message": {"role": role, "content": content},
    }


HEAD = _rec(0, "user", "the head prompt nobody ever saw")
TAIL = _rec(1, "assistant", "the tail the client does hold")


class _UiWS:
    """Minimal UI WebSocket stand-in capturing decoded frames it is sent."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def frames(self, frame_type: str) -> list:
        return [m for m in self.sent if m.get("type") == frame_type]


class _DaemonWS:
    """Daemon stand-in that records the requests the server pushes at it."""

    def __init__(self) -> None:
        self.requests: list = []

    async def send_text(self, data: str) -> None:
        message = protocol.decode(data)
        if message is not None and message.type == protocol.MSG_HISTORY_REQUEST:
            self.requests.append(message.payload)


async def _setup(owner_id: str = "owner-A"):
    state = ServerState()
    await state.register_machine("m1", "host", "9.9.9", owner_id=owner_id)
    hub = UiHub()
    ui = _UiWS()
    await hub.register(ui, owner_id)
    return state, hub, ui, HistoryRequestRegistry()


def _msg(mode, records, cursor=None, cursor_base=None):
    return protocol.make_history_data(
        "f1", mode, records, cursor=cursor or {}, cursor_base=cursor_base or {}
    )


# --------------------------------------------------------------------------
# history_data frames carry the post-frame cursor + signature
# --------------------------------------------------------------------------


def test_full_frame_carries_cursor_and_signature():
    async def scenario():
        state, hub, ui, registry = await _setup()
        await _handle_message(
            _msg(protocol.HISTORY_MODE_FULL, [HEAD, TAIL], {CURSOR_KEY: 2}),
            state,
            "m1",
            hub,
            registry,
        )
        snapshot = await state.get_history_snapshot("f1")
        return ui.frames("history_data"), snapshot

    frames, snapshot = asyncio.run(scenario())
    assert len(frames) == 1
    # The pushed counts and signature are the SAME values a REST read would hand
    # the client at this instant — one bundle, one truth.
    assert frames[0]["cursor"] == {CURSOR_KEY: 2}
    assert frames[0]["cursor"] == snapshot["cursor"]
    assert frames[0]["signature"] == snapshot["signature"]


def test_append_frame_carries_cursor_after_the_append():
    """The cursor describes the bundle AFTER the append, not before it."""

    async def scenario():
        state, hub, ui, registry = await _setup()
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [HEAD],
            cursor={CURSOR_KEY: 1},
            machine_id="m1",
        )
        await _handle_message(
            _msg(
                protocol.HISTORY_MODE_APPEND,
                [TAIL],
                {CURSOR_KEY: 2},
                cursor_base={CURSOR_KEY: 1},
            ),
            state,
            "m1",
            hub,
            registry,
        )
        snapshot = await state.get_history_snapshot("f1")
        return ui.frames("history_data"), snapshot

    frames, snapshot = asyncio.run(scenario())
    assert len(frames) == 1
    assert frames[0]["mode"] == protocol.HISTORY_MODE_APPEND
    assert frames[0]["records"] == [TAIL]
    assert frames[0]["cursor"] == {CURSOR_KEY: 2}
    assert frames[0]["signature"] == snapshot["signature"]


# --------------------------------------------------------------------------
# a discarded append is relayed to nobody
# --------------------------------------------------------------------------


def test_first_sighting_append_is_not_broadcast():
    """The head-loss shape: an unanchored tail the cache threw away.

    The client used to receive this append, apply it to an empty pane, and end up
    holding a conversation with no head — records that are a suffix of no bundle.
    """

    async def scenario():
        state, hub, ui, registry = await _setup()
        await _handle_message(
            _msg(protocol.HISTORY_MODE_APPEND, [TAIL], {CURSOR_KEY: 2}),
            state,
            "m1",
            hub,
            registry,
        )
        # The cache refused it, so the flow holds no bundle at all.
        assert await state.get_history("f1") is None
        return ui.sent

    assert asyncio.run(scenario()) == []


def test_gapped_append_is_not_broadcast():
    """An append starting past the cached water mark is discarded — and unrelayed."""

    async def scenario():
        state, hub, ui, registry = await _setup()
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [HEAD],
            cursor={CURSOR_KEY: 1},
            machine_id="m1",
        )
        await _handle_message(
            _msg(
                protocol.HISTORY_MODE_APPEND,
                [_rec(4, "assistant", "line past the gap")],
                {CURSOR_KEY: 5},
                cursor_base={CURSOR_KEY: 4},
            ),
            state,
            "m1",
            hub,
            registry,
        )
        bundle = await state.get_history("f1")
        return ui.frames("history_data"), bundle

    frames, bundle = asyncio.run(scenario())
    assert frames == []
    assert bundle["records"] == [HEAD]


# --------------------------------------------------------------------------
# a suppressed frame still announces the bundle state
# --------------------------------------------------------------------------


def test_suppressed_full_pull_reply_still_pushes_a_cursor_advisory():
    """The records stay suppressed (token protection) but the counts go out.

    This is the frame that repairs a bundle after a discarded append. Its records
    ride back to the requesting client over REST, but every OTHER console used to
    be told nothing at all — including that the head exists.
    """

    async def scenario():
        state, hub, ui, registry = await _setup()
        registry.register("f1", machine_id="m1")  # a REST pull is parked
        await _handle_message(
            _msg(protocol.HISTORY_MODE_FULL, [HEAD, TAIL], {CURSOR_KEY: 2}),
            state,
            "m1",
            hub,
            registry,
        )
        snapshot = await state.get_history_snapshot("f1")
        return ui, snapshot

    ui, snapshot = asyncio.run(scenario())
    assert ui.frames("history_data") == []  # records suppressed, as before
    advisories = ui.frames("history_cursor")
    assert len(advisories) == 1
    assert advisories[0]["flow_id"] == "f1"
    assert advisories[0]["cursor"] == {CURSOR_KEY: 2}
    assert advisories[0]["signature"] == snapshot["signature"]
    assert "records" not in advisories[0]


def test_head_loss_shape_end_to_end_the_head_is_announced():
    """The live defect, end to end: the client is never left unaware of the head.

    A first-sighting append (the tail) arrives with no bundle behind it. It is
    discarded and relayed nowhere, a recovery pull fetches the authoritative
    bundle, and the frame carrying it tells the console — via its cursor — that
    the step file holds TWO records, head included.
    """

    async def scenario():
        state, hub, ui, registry = await _setup()
        manager = ConnectionManager()
        daemon = _DaemonWS()
        await manager.connect("m1", daemon)

        await _handle_message(
            _msg(protocol.HISTORY_MODE_APPEND, [TAIL], {CURSOR_KEY: 2}),
            state,
            "m1",
            hub,
            registry,
            manager=manager,
            connection=daemon,
        )
        assert len(daemon.requests) == 1  # one self-heal pull

        await _handle_message(
            _msg(protocol.HISTORY_MODE_FULL, [HEAD, TAIL], {CURSOR_KEY: 2}),
            state,
            "m1",
            hub,
            registry,
            manager=manager,
            connection=daemon,
        )
        return ui, await state.get_history("f1")

    ui, bundle = asyncio.run(scenario())
    assert bundle["records"] == [HEAD, TAIL]
    frames = ui.frames("history_data")
    # Exactly one frame reached the console: the authoritative full, head first.
    assert len(frames) == 1
    assert frames[0]["mode"] == protocol.HISTORY_MODE_FULL
    assert frames[0]["records"] == [HEAD, TAIL]
    assert frames[0]["cursor"] == {CURSOR_KEY: 2}
