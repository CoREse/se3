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


def _msg(mode, records, cursor=None, cursor_base=None, usage=None):
    return protocol.make_history_data(
        "f1", mode, records, cursor=cursor or {}, cursor_base=cursor_base or {},
        usage=usage,
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
# history_data frames carry the backend usage payload
# --------------------------------------------------------------------------

USAGE_PAYLOAD = {
    "summary": {"totals": {"logical_input_tokens": 10, "output_tokens": 1}},
    "completeness": "complete",
}


def test_full_frame_relays_the_backend_usage_payload():
    """The WS push carries the SAME usage payload the REST bundle delivers."""

    async def scenario():
        state, hub, ui, registry = await _setup()
        await _handle_message(
            _msg(
                protocol.HISTORY_MODE_FULL,
                [HEAD, TAIL],
                {CURSOR_KEY: 2},
                usage=USAGE_PAYLOAD,
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
    assert frames[0]["usage"] == USAGE_PAYLOAD
    assert frames[0]["usage"] == snapshot["usage"]


def test_frame_without_usage_omits_the_key():
    """No bundle usage at all → the key is omitted, never a null/fake zero."""

    async def scenario():
        state, hub, ui, registry = await _setup()
        await _handle_message(
            _msg(protocol.HISTORY_MODE_FULL, [HEAD, TAIL], {CURSOR_KEY: 2}),
            state,
            "m1",
            hub,
            registry,
        )
        return ui.frames("history_data")

    frames = asyncio.run(scenario())
    assert len(frames) == 1
    assert "usage" not in frames[0]


def test_append_frame_relays_the_stored_usage_payload():
    """Once a full frame carried usage, a usage-free append relays the same
    stored backend payload — no rebuild on the hot append path."""

    async def scenario():
        state, hub, ui, registry = await _setup()
        await _handle_message(
            _msg(
                protocol.HISTORY_MODE_FULL,
                [HEAD],
                {CURSOR_KEY: 1},
                usage=USAGE_PAYLOAD,
            ),
            state,
            "m1",
            hub,
            registry,
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
        return ui.frames("history_data")

    frames = asyncio.run(scenario())
    assert len(frames) == 2
    assert frames[0]["usage"] == USAGE_PAYLOAD
    assert frames[1]["mode"] == protocol.HISTORY_MODE_APPEND
    assert frames[1]["usage"] == USAGE_PAYLOAD


def _usage_rec(ordinal=2):
    rec = _rec(ordinal, "assistant", "hello")
    rec["message"]["usage_records"] = [{
        "schema_version": 2,
        "call_id": "call-2",
        "attempt": 0,
        "usage_status": "available",
        "provider": "anthropic",
        "resolved_model": "claude-opus-5",
        "logical_input_tokens": 30,
        "uncached_input_tokens": 30,
        "output_tokens": 3,
    }]
    return rec


def test_usage_bearing_append_refreshes_the_stored_summary():
    """An append that carries new usage must not keep relaying the stale
    full-frame snapshot: the pushed frame (and the REST bundle) carries a
    backend summary derived from ALL cached records."""

    async def scenario():
        state, hub, ui, registry = await _setup()
        await _handle_message(
            _msg(
                protocol.HISTORY_MODE_FULL,
                [HEAD],
                {CURSOR_KEY: 1},
                usage=USAGE_PAYLOAD,
            ),
            state,
            "m1",
            hub,
            registry,
        )
        await _handle_message(
            _msg(
                protocol.HISTORY_MODE_APPEND,
                [_usage_rec()],
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
    assert len(frames) == 2
    assert frames[0]["usage"] == USAGE_PAYLOAD
    assert frames[1]["mode"] == protocol.HISTORY_MODE_APPEND
    # The appended record's call now shows up in the refreshed payload.
    assert frames[1]["usage"] != USAGE_PAYLOAD
    call_ids = [c["call_id"] for c in frames[1]["usage"]["calls"]]
    assert "call-2" in call_ids
    totals = frames[1]["usage"]["summary"]["totals"]
    assert totals["logical_input_tokens"] == 30
    assert totals["output_tokens"] == 3
    # The REST snapshot serves the same refreshed payload.
    assert snapshot["usage"] == frames[1]["usage"]


def test_full_frame_without_usage_rebuilds_from_records():
    """A daemon that never sends a usage payload (legacy) still gets the
    shared-backend rebuild on full frames — same as the REST path."""

    def _usage_rec():
        rec = _rec(0, "assistant", "hello")
        rec["message"]["usage_records"] = [{
            "schema_version": 2,
            "call_id": "call-1",
            "attempt": 0,
            "usage_status": "available",
            "logical_input_tokens": 10,
            "uncached_input_tokens": 10,
            "output_tokens": 1,
        }]
        return rec

    async def scenario():
        state, hub, ui, registry = await _setup()
        await _handle_message(
            _msg(protocol.HISTORY_MODE_FULL, [_usage_rec()], {CURSOR_KEY: 1}),
            state,
            "m1",
            hub,
            registry,
        )
        snapshot = await state.get_history_snapshot("f1")
        return ui.frames("history_data"), snapshot

    frames, snapshot = asyncio.run(scenario())
    assert len(frames) == 1
    assert frames[0]["usage"] == snapshot["usage"]
    assert frames[0]["usage"]["summary"]["totals"]["logical_input_tokens"] == 10


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


# --------------------------------------------------------------------------
# a budget-evicted flow still tells the console its bundle moved
# --------------------------------------------------------------------------


def test_evicted_flow_pushes_a_cursor_advisory_instead_of_records():
    """Silence would freeze a console that is DISPLAYING an evicted flow.

    The history-cache budget may evict a flow whose bundle is large and whose
    last UI read is old, and the cold marker then makes every later daemon frame
    a no-op so a push cannot re-establish what the budget refused. Relaying the
    RECORDS would defeat that; relaying nothing defeats the console. The WebUI
    History view has no poll timer — it self-checks only when a frame arrives —
    so the frame's own cursor is the only thing that can tell it to re-pull, and
    that pull is what re-admits the flow to the cache.
    """

    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await state.register_machine("m1", "host", "9.9.9", owner_id="owner-A")
        hub = UiHub()
        ui = _UiWS()
        await hub.register(ui, "owner-A")
        registry = HistoryRequestRegistry()

        await _handle_message(
            _msg(protocol.HISTORY_MODE_FULL, [HEAD], {CURSOR_KEY: 1}),
            state, "m1", hub, registry,
        )
        # The budget drops it while nobody is reading it.
        await state.report_history_cache()
        assert await state.get_history("f1", touch=False) is None
        ui.sent.clear()

        await _handle_message(
            _msg(
                protocol.HISTORY_MODE_APPEND,
                [TAIL],
                {CURSOR_KEY: 2},
                cursor_base={CURSOR_KEY: 1},
            ),
            state, "m1", hub, registry,
        )
        return ui, state

    ui, state = asyncio.run(scenario())
    # The records the cache refused reach nobody …
    assert ui.frames("history_data") == []
    # … but the console is told what the daemon holds, so its cursor self-check
    # can re-pull the flow and re-admit it.
    advisories = ui.frames("history_cursor")
    assert len(advisories) == 1
    assert advisories[0]["flow_id"] == "f1"
    assert advisories[0]["cursor"] == {CURSOR_KEY: 2}
    # No bundle exists to sign, so no generation/signature is claimed.
    assert "signature" not in advisories[0]
    assert advisories[0]["pending"] == {}
    # …but the delivery's completeness IS claimed, and it is unfinished: this
    # advisory stands in for a frame whose records went nowhere, so the console
    # must not read it as the settled declaration that would retire its bounded
    # repair. Every other field here describes a self-consistent prefix; this is
    # the only one that can contradict it.
    assert advisories[0]["incomplete"] is True


def test_the_cold_advisory_keeps_an_armed_repair_armed():
    """A dropped frame may never look like the end of a delivery.

    The sequence that used to weld the truncation shut: an interrupted drain
    leaves the bundle a prefix (the server answers ``incomplete: true``, so the
    console's bounded recovery is running), the budget then evicts the flow, and
    the advisory that replaces the next suppressed frame said nothing about
    completeness at all. A consumer reading an absent key as "settled" stops
    repairing — so the statement has to survive the eviction on the wire.
    """

    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await state.register_machine("m1", "host", "9.9.9", owner_id="owner-A")
        hub = UiHub()
        ui = _UiWS()
        await hub.register(ui, "owner-A")
        registry = HistoryRequestRegistry()

        await _handle_message(
            _msg(protocol.HISTORY_MODE_FULL, [HEAD], {CURSOR_KEY: 1}),
            state, "m1", hub, registry,
        )
        await state.report_history_cache()
        ui.sent.clear()
        await _handle_message(
            _msg(
                protocol.HISTORY_MODE_APPEND,
                [TAIL],
                {CURSOR_KEY: 2},
                cursor_base={CURSOR_KEY: 1},
            ),
            state, "m1", hub, registry,
        )
        return ui

    ui = asyncio.run(scenario())
    frames = ui.frames("history_cursor")
    assert len(frames) == 1
    # The one key whose ABSENCE the console cannot safely interpret.
    assert "incomplete" in frames[0]
    assert frames[0]["incomplete"] is True


def test_the_cold_advisory_is_scoped_to_the_owning_console():
    """An advisory is bundle state; it must not cross an owner boundary."""

    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await state.register_machine("m1", "host", "9.9.9", owner_id="owner-A")
        hub = UiHub()
        mine, other, admin = _UiWS(), _UiWS(), _UiWS()
        await hub.register(mine, "owner-A")
        await hub.register(other, "owner-B")
        await hub.register(admin, None)
        registry = HistoryRequestRegistry()

        await _handle_message(
            _msg(protocol.HISTORY_MODE_FULL, [HEAD], {CURSOR_KEY: 1}),
            state, "m1", hub, registry,
        )
        await state.report_history_cache()
        for client in (mine, other, admin):
            client.sent.clear()
        await _handle_message(
            _msg(
                protocol.HISTORY_MODE_APPEND,
                [TAIL],
                {CURSOR_KEY: 2},
                cursor_base={CURSOR_KEY: 1},
            ),
            state, "m1", hub, registry,
        )
        return mine, other, admin

    mine, other, admin = asyncio.run(scenario())
    assert len(mine.frames("history_cursor")) == 1
    assert len(admin.frames("history_cursor")) == 1
    assert other.frames("history_cursor") == []
