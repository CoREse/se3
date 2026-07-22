"""Regression lock: one self-heal recovery pull, deduped across a whole drain.

A full pull of a large ACTIVE flow does not come back as one frame — the owning
daemon drains it as a ``full`` HEAD followed by dozens of ``append`` TAILS
(a 4.6 MB flow ⇒ 30~39 frames). The server marks a recovery ``in flight`` when
it dispatches the pull and MUST keep exactly one in flight until the drain
converges (or its TTL lapses).

The defect these tests lock: ``append_history``'s full branch used to POP the
recovery marker on the drain HEAD. That reopened the dedup window for the entire
tail-draining span — a cursor-gap discard among the still-arriving tails (e.g.
the push loop inserting a fresh append past the head's water mark) re-armed
``requires_full``, ``take_recovery_pull`` then found NO marker and dispatched a
RIVAL full pull, and the two pulls kept discarding each other's tails: the
observed ``reason=cursor-gap`` DISCARD ⇄ multi-frame HISTORY_REQUEST livelock
that made the WebUI chat jump between steps.

These drive ``ServerState`` directly (no daemon, no browser) at exactly that
seam. They do NOT touch the cursor-gap guard itself — a genuine forward gap is
still detected and still discarded; only the recovery *dispatch* is deduped.
"""

from __future__ import annotations

import asyncio
import time

from se3.daemon import protocol
from se3.server.state import ServerState

FLOW = "20260722-104526_a82315a9"
MACHINE = "node007"
STEP_FILE = "05_implement_9772ce1d.jsonl"
STEP_ID = "05_implement_9772ce1d"


def _record(ordinal: int) -> dict:
    return {
        "step_id": STEP_ID,
        "step_type": "implement",
        "ordinal": ordinal,
        "message": {"role": "assistant", "content": f"line {ordinal}"},
    }


def _cursor(lines: int) -> dict:
    return {STEP_FILE: lines}


async def _flow(state: ServerState, status: str = "running") -> None:
    await state.update_status(
        MACHINE,
        {
            "machine_id": MACHINE,
            "flows": [
                {
                    "flow_id": FLOW,
                    "project_root": "/repo",
                    "status": status,
                }
            ],
        },
    )


async def _full(state: ServerState, records, cursor):
    return await state.apply_history_frame(
        FLOW, protocol.HISTORY_MODE_FULL, records,
        cursor=cursor, machine_id=MACHINE,
    )


async def _append(state: ServerState, records, cursor):
    return await state.apply_history_frame(
        FLOW, protocol.HISTORY_MODE_APPEND, records,
        cursor=cursor, machine_id=MACHINE,
    )


def test_recovery_marker_survives_drain_head_no_rival_pull():
    """The drain HEAD keeps the recovery in flight — a mid-drain gap discard does
    NOT dispatch a second pull."""

    async def scenario():
        state = ServerState()
        await _flow(state)

        # A first-sighting append (no bundle to extend) is discarded and arms
        # ``requires_full`` — the desync that provokes a self-heal pull.
        out = await _append(state, [_record(4)], _cursor(5))
        assert out.resolves_pull is False

        # The receive loop dispatches exactly one recovery pull for the stuck
        # flow: first call True (arm), a same-cycle append storm cannot re-fire.
        assert await state.take_recovery_pull(FLOW) is True
        assert await state.take_recovery_pull(FLOW) is False

        # The pull's reply DRAINS as many frames. Its HEAD is a ``full`` frame
        # carrying the flow's head; it repopulates the bundle and clears
        # ``requires_full`` — but the tails are still on their way.
        await _full(state, [_record(0), _record(1)], _cursor(2))
        bundle = await state.get_history(FLOW)
        assert [r["ordinal"] for r in bundle["records"]] == [0, 1]

        # Mid-drain race: the push loop inserts an append past the head's water
        # mark (bundle covers 2 lines, this frame claims to start at 4). The
        # cursor-gap guard correctly refuses it and re-arms ``requires_full`` —
        # the guard's defensive semantics are untouched.
        out = await _append(state, [_record(4)], _cursor(5))
        assert out.resolves_pull is False
        bundle = await state.get_history(FLOW)
        assert [r["ordinal"] for r in bundle["records"]] == [0, 1]

        # THE FIX: the recovery marker survived the drain HEAD, so NO rival pull
        # is dispatched while the original drain is still converging. Before the
        # fix this returned True and the two pulls livelocked.
        assert await state.take_recovery_pull(FLOW) is False

        # When the drain converges (a full reply that heals the bundle from line
        # 0), the flow leaves ``requires_full`` and stays quiescent.
        await _full(state, [_record(i) for i in range(6)], _cursor(6))
        bundle = await state.get_history(FLOW)
        assert [r["ordinal"] for r in bundle["records"]] == [0, 1, 2, 3, 4, 5]
        assert await state.take_recovery_pull(FLOW) is False

    asyncio.run(scenario())


def test_recovery_marker_rearms_after_ttl():
    """A drain that never converges must not wedge the flow forever: once the
    marker ages past the TTL, a fresh recovery pull re-arms."""

    async def scenario():
        state = ServerState()
        await _flow(state)

        out = await _append(state, [_record(4)], _cursor(5))
        assert out.resolves_pull is False
        assert await state.take_recovery_pull(FLOW) is True

        # Drain HEAD arrives (marker refreshed, not cleared), then a mid-drain
        # gap re-arms requires_full — and the fresh marker still dedups.
        await _full(state, [_record(0), _record(1)], _cursor(2))
        await _append(state, [_record(4)], _cursor(5))
        assert await state.take_recovery_pull(FLOW) is False

        # Simulate the drain stalling past the recovery TTL by aging the marker.
        # The flow is still flagged requires_full (the last gap re-armed it), so
        # the TTL fallback must let a fresh pull re-arm rather than wedge.
        state._history_recovery_inflight[FLOW] = (
            time.monotonic() - state._HISTORY_RECOVERY_TTL - 1.0
        )
        assert await state.take_recovery_pull(FLOW) is True
        # ...and that fresh pull is itself deduped (at most one in flight).
        assert await state.take_recovery_pull(FLOW) is False

    asyncio.run(scenario())
