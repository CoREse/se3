"""The server tells a cursor gap that is *pending* apart from one that is *unfillable*.

Both are numbers the bundle holds no record for, but their causes are opposite and
the client must act on them oppositely (Defect B, server side):

  * ``unfillable`` — a number the bundle can PROVE it will never hold: a blank /
    unparseable physical line the daemon stepped over, which shows up as a hole
    BELOW a later record it did deliver. The client retires it.

  * ``pending`` — a number the bundle's cursor DECLARES but has not yet been sent:
    it lies past the highest ordinal delivered for its step, in the trailing
    window the daemon is still streaming (the livelock shape — cursor says 815
    lines, only the first tens of records have crossed a short-lived connection).
    The client keeps waiting; declaring it unfillable would have it retire a
    record that is genuinely on its way.

These tests drive ``ServerState`` directly (no daemon, no browser) at that seam,
plus one end-to-end check that the REST reply carries the new field with the
existing key set intact so an older frontend is unaffected.
"""

from __future__ import annotations

import asyncio

from se3.daemon import protocol
from se3.server.state import ServerState

FLOW = "20260720-163316_2df2d504"
MACHINE = "m1"
STEP_FILE = "06_implement_398863d6.jsonl"
STEP_ID = "06_implement_398863d6"


def _record(ordinal: int) -> dict:
    """A daemon history record: ``step_id`` + its physical line ``ordinal``."""
    return {
        "step_id": STEP_ID,
        "step_type": "implement",
        "ordinal": ordinal,
        "message": {"role": "assistant", "content": f"line {ordinal}"},
    }


async def _seed(state: ServerState, records, cursor) -> None:
    """Establish an authoritative bundle whose cursor is *cursor* (a full frame
    stores its cursor verbatim, so the cursor may legitimately DECLARE more lines
    than the frame delivered — the intermediate catch-up shape)."""
    await state.apply_history_frame(
        FLOW,
        protocol.HISTORY_MODE_FULL,
        records,
        cursor=cursor,
        machine_id=MACHINE,
    )


async def _token(state: ServerState):
    """A valid (in-sync) progress token + signature for the current bundle."""
    full = await state.get_history_snapshot(
        FLOW, expected_machine_id=MACHINE
    )
    return full["progress"], full["signature"]


async def _snap(state: ServerState, token, sig, missing=None):
    return await state.get_history_snapshot(
        FLOW,
        after=token,
        expected_machine_id=MACHINE,
        known_signature=sig,
        missing=missing,
    )


# --------------------------------------------------------------------------
# the pure classifier
# --------------------------------------------------------------------------


def test_pending_is_the_trailing_declared_but_unsent_window():
    """Records reach ordinal 2, the cursor declares 6 lines → 3, 4, 5 are pending."""
    pending = ServerState._bundle_pending_positions(
        [_record(0), _record(1), _record(2)], {STEP_FILE: 6}
    )
    assert pending == {STEP_ID: [3, 4, 5]}


def test_interior_blank_hole_is_not_pending():
    """A hole BELOW the highest delivered ordinal (a blank line the daemon stepped
    over) is a proven gap, never pending: both its neighbours were delivered."""
    # ordinals 0,1,3,4 held (line 2 was blank), cursor counts all 5 physical lines.
    held = [_record(0), _record(1), _record(3), _record(4)]
    pending = ServerState._bundle_pending_positions(held, {STEP_FILE: 5})
    assert pending == {}


def test_step_with_no_records_yet_is_wholly_pending():
    """A file the cursor declares but no record has arrived for → the whole range
    is pending (nothing delivered, so nothing is a proven hole)."""
    pending = ServerState._bundle_pending_positions([], {STEP_FILE: 4})
    assert pending == {STEP_ID: [0, 1, 2, 3]}


def test_full_bundle_without_a_cursor_names_nothing_pending():
    """A ``full`` frame carries no cursor, so there is no declared extent to be
    behind — pending is empty, exactly the pre-existing behaviour."""
    assert ServerState._bundle_pending_positions([_record(0)], {}) == {}


def test_unnumbered_step_is_skipped_not_claimed_pending():
    """A step carrying an un-numbered record is not soundly addressable by ordinal;
    such a request escalates to a full instead, so it must claim nothing pending."""
    legacy = {"step_id": STEP_ID, "message": {"role": "user", "content": "head"}}
    pending = ServerState._bundle_pending_positions(
        [legacy, _record(1)], {STEP_FILE: 6}
    )
    assert pending == {}


# --------------------------------------------------------------------------
# the snapshot reply: pending vs unfillable, and their coexistence
# --------------------------------------------------------------------------


def test_pending_gap_returns_pending_and_empty_unfillable():
    async def scenario():
        state = ServerState()
        await _seed(state, [_record(0), _record(1), _record(2)], {STEP_FILE: 6})
        token, sig = await _token(state)

        got = await _snap(state, token, sig, missing={STEP_ID: [3, 4, 5]})
        assert got["delivery"] == "backfill"
        # None served (they have not arrived), none unfillable (they are on the
        # way), all three named pending.
        assert got["records"] == []
        assert got["unfillable"] == {}
        assert got["pending"] == {STEP_ID: [3, 4, 5]}

    asyncio.run(scenario())


def test_interior_hole_is_unfillable_with_empty_pending():
    async def scenario():
        state = ServerState()
        await _seed(
            state, [_record(0), _record(1), _record(3), _record(4)], {STEP_FILE: 5}
        )
        token, sig = await _token(state)

        got = await _snap(state, token, sig, missing={STEP_ID: [2]})
        assert got["delivery"] == "backfill"
        assert got["records"] == []
        assert got["unfillable"] == {STEP_ID: [2]}
        assert got["pending"] == {}

    asyncio.run(scenario())


def test_backfill_and_pending_coexist_in_one_reply():
    """One request names a number the bundle HOLDS, an interior blank, and a
    pending tail number: each is routed to its own verdict without collision."""

    async def scenario():
        state = ServerState()
        # ordinals 0,1,3 held (line 2 blank); cursor declares 6 lines total.
        await _seed(
            state, [_record(0), _record(1), _record(3)], {STEP_FILE: 6}
        )
        token, sig = await _token(state)

        got = await _snap(state, token, sig, missing={STEP_ID: [3, 2, 5]})
        assert got["delivery"] == "backfill"
        # ordinal 3 the bundle HOLDS → served back; ordinal 2 is a proven interior
        # hole → unfillable; ordinal 5 is past the highest delivered → pending.
        assert [r["ordinal"] for r in got["records"]] == [3]
        assert got["unfillable"] == {STEP_ID: [2]}
        assert got["pending"] == {STEP_ID: [4, 5]}

    asyncio.run(scenario())


def test_pending_converges_to_empty_as_records_arrive():
    """The livelock's cure: as the daemon delivers the trailing window, the pending
    set shrinks monotonically and vanishes when the records cover the cursor."""

    async def scenario():
        state = ServerState()
        await _seed(state, [_record(0), _record(1), _record(2)], {STEP_FILE: 6})
        token, sig = await _token(state)

        first = await _snap(state, token, sig)
        assert first["delivery"] == "not_modified"
        assert first["pending"] == {STEP_ID: [3, 4, 5]}

        # One more record crosses the wire (ordinal 3), cursor unchanged at 6.
        await state.apply_history_frame(
            FLOW,
            protocol.HISTORY_MODE_APPEND,
            [_record(3)],
            cursor={STEP_FILE: 6},
            cursor_base={STEP_FILE: 3},
            machine_id=MACHINE,
        )
        mid = await _snap(state, token, sig)
        assert mid["delivery"] == "delta"
        assert [r["ordinal"] for r in mid["records"]] == [3]
        assert mid["pending"] == {STEP_ID: [4, 5]}

        # The rest arrive; records now cover the whole cursor extent.
        await state.apply_history_frame(
            FLOW,
            protocol.HISTORY_MODE_APPEND,
            [_record(4), _record(5)],
            cursor={STEP_FILE: 6},
            cursor_base={STEP_FILE: 4},
            machine_id=MACHINE,
        )
        token2, sig2 = await _token(state)
        done = await _snap(state, token2, sig2)
        assert done["delivery"] == "not_modified"
        assert done["pending"] == {}

    asyncio.run(scenario())


def test_pending_is_present_on_every_delivery():
    """The key set is uniform: ``pending`` (like ``unfillable``) rides every reply,
    so the client's merge path never branches on its presence."""

    async def scenario():
        state = ServerState()
        await _seed(state, [_record(0), _record(1)], {STEP_FILE: 2})
        token, sig = await _token(state)

        not_modified = await _snap(state, token, sig)
        full = await state.get_history_snapshot(FLOW, expected_machine_id=MACHINE)
        backfill = await _snap(state, token, sig, missing={STEP_ID: [0]})
        for reply in (not_modified, full, backfill):
            assert "pending" in reply
            assert "unfillable" in reply
            # A bundle whose records cover its cursor names nothing pending.
            assert reply["pending"] == {}

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# push face / poll face agree
# --------------------------------------------------------------------------


def test_push_meta_reports_the_same_pending_as_the_poll():
    """``get_history_bundle_meta`` (the WS push source) and ``get_history_snapshot``
    (the REST poll) read one bundle, so they cannot disagree about what is pending."""

    async def scenario():
        state = ServerState()
        await _seed(state, [_record(0), _record(1), _record(2)], {STEP_FILE: 6})
        token, sig = await _token(state)

        meta = await state.get_history_bundle_meta(FLOW)
        poll = await _snap(state, token, sig)
        assert meta["pending"] == poll["pending"] == {STEP_ID: [3, 4, 5]}

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# end to end through the REST route (backward-compatible field addition)
# --------------------------------------------------------------------------


def test_rest_reply_carries_pending_key():
    """The route spreads the snapshot, so ``pending`` reaches the JSON body — a new
    key added beside ``unfillable``, leaving every existing field untouched."""
    from fastapi.testclient import TestClient

    from _authsrv import authed_app, authed_hello, login

    app, key = authed_app()
    with TestClient(app) as client:
        login(client)
        daemon = client.websocket_connect("/ws")
        sock = daemon.__enter__()
        sock.send_text(authed_hello(app, MACHINE, "host", "6.4.0"))
        protocol.decode(sock.receive_text())  # WELCOME
        sock.send_text(
            protocol.make_history_data(
                FLOW,
                protocol.HISTORY_MODE_FULL,
                [_record(0), _record(1)],
            ).to_json()
        )
        try:
            body = None
            for _ in range(50):
                resp = client.get(f"/api/history/{FLOW}")
                if resp.status_code == 200 and resp.json().get("cached"):
                    body = resp.json()
                    break
            assert body is not None, "bundle never became cache-visible"
            # New key present; a full push carries no cursor, so nothing is pending.
            assert body["pending"] == {}
            assert body["unfillable"] == {}
        finally:
            daemon.__exit__(None, None, None)
