"""Regression lock: gap self-heal backfills incrementally, never head-truncates.

On a high-loss link (node007) the daemon's keepalive ping timed out roughly
every 45s; every reconnect truncated the previous *cursorless full* recovery
drain, so the server's bundle was perpetually rebuilt to a short prefix (the
"chat records shrink then grow" jitter) while the gap self-heal re-fired a fresh
full pull that the next disconnect truncated again — a DISCARD ⇄ full-pull loop
that never converged.

The fix routes the self-heal through :meth:`ServerState.plan_recovery_pull`:
when the server still holds a NON-EMPTY, cursor-bearing bundle produced by the
SAME machine (and the flow is not an active worktree flow whose history is split
across roots), the recovery is an ``append`` backfill anchored at the server's
own water mark rather than a whole-bundle rebuild. The append path only EXTENDS
the bundle, so:

  * the bundle's exposed coverage is monotonic non-decreasing within a
    generation (it never shrinks to a prefix), and
  * a truncated backfill drain leaves the bundle shorter-but-hole-free, and the
    next reconnect continues from the new water mark — it CONVERGES.

A cursorless full is reserved for the cases with genuinely nothing to extend:
no cached bundle, a machine change, or an active worktree flow.

These tests drive ``ServerState`` and the ``ws.py`` ``_handle_message``
dispatch directly (no daemon, no browser) at exactly that seam.
"""

from __future__ import annotations

import asyncio
import json
import logging

from tianluo.daemon import protocol
from tianluo.server.state import ServerState
from tianluo.server.ws import (
    ConnectionManager,
    HistoryRequestRegistry,
    UiHub,
    _handle_message,
)

FLOW = "20260723-125250_d58a7a90"
MACHINE = "m1"
STEP_FILE = "01_discovery_284388ec.jsonl"
STEP_ID = "01_discovery_284388ec"


def _record(ordinal: int) -> dict:
    return {
        "step_id": STEP_ID,
        "step_type": "discovery",
        "ordinal": ordinal,
        "message": {"role": "assistant", "content": f"line {ordinal}"},
    }


def _cursor(lines: int) -> dict:
    return {STEP_FILE: lines}


async def _full(state: ServerState, records, cursor):
    return await state.apply_history_frame(
        FLOW, protocol.HISTORY_MODE_FULL, records,
        cursor=cursor, machine_id=MACHINE,
    )


async def _append(state: ServerState, records, cursor, cursor_base):
    return await state.apply_history_frame(
        FLOW, protocol.HISTORY_MODE_APPEND, records,
        cursor=cursor, cursor_base=cursor_base, machine_id=MACHINE,
    )


async def _gap_append(state: ServerState, base: int, end: int):
    """A live append that starts PAST the cached water mark — flags requires_full.

    Mirrors what node007 pushed after a reconnect: the frame covers lines that
    lie beyond what the server holds, so :meth:`apply_history_frame` refuses it
    and arms the self-heal (bundle + cursor left intact).
    """
    return await state.apply_history_frame(
        FLOW, protocol.HISTORY_MODE_APPEND,
        [_record(i) for i in range(base, end)],
        cursor=_cursor(end), cursor_base=_cursor(base), machine_id=MACHINE,
    )


async def _active_worktree_flow(state: ServerState, status: str = "running") -> None:
    await state.update_status(
        MACHINE,
        {
            "machine_id": MACHINE,
            "flows": [
                {
                    "flow_id": FLOW,
                    "project_root": "/repo/se3/worktrees/wt-d58a7a90",
                    "status": status,
                }
            ],
        },
    )


# --------------------------------------------------------------------------
# (a) incremental decision: reuse the cached bundle's cursor, clear requires_full
# --------------------------------------------------------------------------


def test_plan_recovery_pull_incremental_carries_cursor_and_clears_flag():
    """A gap on a good same-machine bundle plans an incremental append backfill."""

    async def scenario():
        state = ServerState()
        # Establish an authoritative bundle at water mark 5.
        await _full(state, [_record(i) for i in range(5)], _cursor(5))
        gen0 = (await state.get_history(FLOW))["generation"]

        # A reconnect pushes an append starting at line 8 (lines 5..7 never
        # arrived) — refused, flow armed requires_full, bundle left intact.
        assert (await _gap_append(state, 8, 10)).resolves_pull is False
        assert FLOW in state._history_requires_full

        plan = await state.plan_recovery_pull(FLOW, MACHINE)
        # Decision: incremental, carrying the server's OWN water mark so the
        # daemon replies with an append from there.
        assert plan == ("incremental", _cursor(5))
        # requires_full is cleared so the daemon's backfill append is accepted;
        # a recovery is armed in flight for TTL dedup.
        assert FLOW not in state._history_requires_full
        assert FLOW in state._history_recovery_inflight

        # The daemon's backfill append (from water mark 5) is contiguous and
        # applies — the bundle GROWS, generation is preserved (no rebuild).
        outcome = await _append(
            state, [_record(i) for i in range(5, 10)],
            cursor=_cursor(10), cursor_base=_cursor(5),
        )
        assert outcome.resolves_pull is True
        bundle = await state.get_history(FLOW)
        return gen0, bundle

    gen0, bundle = asyncio.run(scenario())
    assert [r["ordinal"] for r in bundle["records"]] == list(range(10))
    # INVARIANT: an incremental backfill only EXTENDS — generation unchanged.
    assert bundle["generation"] == gen0


# --------------------------------------------------------------------------
# (b) monotonic non-decreasing: backfill appends, never replaces or shrinks
# --------------------------------------------------------------------------


def test_incremental_backfill_is_monotonic_non_decreasing():
    """Across a gap→plan→append cycle the record count never regresses."""

    async def scenario():
        state = ServerState()
        await _full(state, [_record(i) for i in range(5)], _cursor(5))
        counts = [len((await state.get_history(FLOW))["records"])]

        # Three successive reconnect gaps, each healed by an incremental append.
        for water, nxt in ((5, 8), (8, 12), (12, 15)):
            await _gap_append(state, nxt, nxt + 2)
            # Each reconnect is >30s of bad network apart, so the prior in-flight
            # recovery marker has aged past the TTL and this gap re-arms.
            state._history_recovery_inflight.pop(FLOW, None)
            kind, cursor = await state.plan_recovery_pull(FLOW, MACHINE)
            assert kind == "incremental"
            assert cursor == _cursor(water)
            await _append(
                state, [_record(i) for i in range(water, nxt)],
                cursor=_cursor(nxt), cursor_base=_cursor(water),
            )
            counts.append(len((await state.get_history(FLOW))["records"]))
        return counts

    counts = asyncio.run(scenario())
    # Monotonic non-decreasing: [5, 8, 12, 15].
    assert counts == sorted(counts)
    assert counts == [5, 8, 12, 15]


# --------------------------------------------------------------------------
# (c) truncation convergence: a cut-short backfill leaves no hole and resumes
# --------------------------------------------------------------------------


def test_truncated_backfill_leaves_no_hole_and_resumes_from_new_mark():
    """A backfill cut short by a disconnect still converges, no cursorless full.

    Simulates node007: the incremental append drain is truncated after only a
    partial window, the link drops, then reconnects. The bundle must not
    regress or gain a hole, and the next self-heal continues incrementally from
    the NEW water mark — never a whole-bundle rebuild — until it catches up.
    """

    async def scenario():
        state = ServerState()
        await _full(state, [_record(i) for i in range(5)], _cursor(5))
        gen0 = (await state.get_history(FLOW))["generation"]

        # Reconnect 1: live append jumps to line 20 → gap, self-heal planned.
        await _gap_append(state, 20, 22)
        plan1 = await state.plan_recovery_pull(FLOW, MACHINE)
        assert plan1 == ("incremental", _cursor(5))
        # The backfill drain is TRUNCATED: only lines 5..9 arrive before the
        # link drops (the remaining append tail never lands).
        await _append(
            state, [_record(i) for i in range(5, 10)],
            cursor=_cursor(10), cursor_base=_cursor(5),
        )
        mid = await state.get_history(FLOW)
        # No hole, no regression: a contiguous 0..9 prefix, same generation.
        assert [r["ordinal"] for r in mid["records"]] == list(range(10))
        assert mid["generation"] == gen0

        # The TTL marker from plan1 is still in flight; force it expired so the
        # next reconnect's gap can re-arm (a real reconnect is >30s of bad net).
        state._history_recovery_inflight[FLOW] = -1e9

        # Reconnect 2: live append still ahead at line 20 → gap again.
        await _gap_append(state, 20, 22)
        plan2 = await state.plan_recovery_pull(FLOW, MACHINE)
        # Continues incrementally from the NEW water mark (10), NOT a cursorless
        # full rebuild — this is what makes it converge.
        assert plan2 == ("incremental", _cursor(10))
        # The rest of the backfill arrives (10..20).
        await _append(
            state, [_record(i) for i in range(10, 20)],
            cursor=_cursor(20), cursor_base=_cursor(10),
        )
        final = await state.get_history(FLOW)
        return gen0, final

    gen0, final = asyncio.run(scenario())
    # Fully caught up, no hole, generation never rolled across the whole ordeal.
    assert [r["ordinal"] for r in final["records"]] == list(range(20))
    assert final["generation"] == gen0


# --------------------------------------------------------------------------
# (c2) racing live push re-arms requires_full mid-backfill — the cursored
# backfill anchored at the water mark must still apply, not stall a TTL cycle
# --------------------------------------------------------------------------


def test_racing_push_rearm_does_not_void_anchored_backfill():
    """A stale live push that re-arms requires_full cannot discard the backfill.

    node007 timeline: the incremental plan clears requires_full and dispatches a
    cursored request; a live append the daemon queued ~1 RTT earlier arrives
    FIRST (still gapped → re-arms requires_full). The backfill frames that then
    land are anchored EXACTLY at the server's water mark, so they must be
    accepted despite the flag rather than discarded for a whole recovery TTL.
    """

    async def scenario():
        state = ServerState()
        await _full(state, [_record(i) for i in range(5)], _cursor(5))
        gen0 = (await state.get_history(FLOW))["generation"]

        # Gap → incremental plan: requires_full cleared, cursor 5 requested.
        await _gap_append(state, 20, 22)
        assert await state.plan_recovery_pull(FLOW, MACHINE) == (
            "incremental", _cursor(5),
        )
        assert FLOW not in state._history_requires_full

        # The racing live push (queued before our request) lands first: still
        # gapped at line 20 → RE-ARMS requires_full.
        assert (await _gap_append(state, 20, 22)).resolves_pull is False
        assert FLOW in state._history_requires_full

        # Now the cursored backfill arrives, anchored at the water mark (5).
        # Despite requires_full being armed by the racing push, it is accepted:
        # the flag is cleared and the bundle GROWS — no TTL-cycle stall.
        outcome = await _append(
            state, [_record(i) for i in range(5, 10)],
            cursor=_cursor(10), cursor_base=_cursor(5),
        )
        assert outcome.resolves_pull is True
        assert FLOW not in state._history_requires_full
        bundle = await state.get_history(FLOW)
        return gen0, bundle

    gen0, bundle = asyncio.run(scenario())
    assert [r["ordinal"] for r in bundle["records"]] == list(range(10))
    # Still an EXTEND, not a rebuild.
    assert bundle["generation"] == gen0


def test_requires_full_still_discards_unanchored_append():
    """The water-mark exemption is precise: a non-anchored append stays discarded.

    An append whose ``cursor_base`` sits PAST the water mark (a genuine gap) must
    NOT be smuggled past the ``requires_full`` guard by the backfill exemption —
    only a base meeting the mark exactly is let through.
    """

    async def scenario():
        state = ServerState()
        await _full(state, [_record(i) for i in range(5)], _cursor(5))
        # Arm requires_full with a forward-jumped gap.
        await _gap_append(state, 20, 22)
        assert FLOW in state._history_requires_full

        # An append starting at line 12 (base 12 != water mark 5) is a real hole
        # — it must be discarded, and the flag must stay armed.
        outcome = await _append(
            state, [_record(i) for i in range(12, 14)],
            cursor=_cursor(14), cursor_base=_cursor(12),
        )
        assert outcome.resolves_pull is False
        assert FLOW in state._history_requires_full
        bundle = await state.get_history(FLOW)
        return bundle

    bundle = asyncio.run(scenario())
    # Bundle untouched at the original 5 records — no hole baked in.
    assert [r["ordinal"] for r in bundle["records"]] == list(range(5))


def test_backfill_completion_logged_at_info(caplog):
    """A backfill append applied while a recovery is in flight logs at INFO.

    Operators verify convergence from ``journalctl`` at the default INFO level;
    the START (plan_recovery_pull kind=incremental) already logs at INFO, so the
    COMPLETION must too — otherwise a converged backfill is indistinguishable
    from a discarded one at INFO.
    """

    async def scenario():
        state = ServerState()
        await _full(state, [_record(i) for i in range(5)], _cursor(5))
        await _gap_append(state, 20, 22)
        # Incremental plan arms the recovery-inflight marker.
        await state.plan_recovery_pull(FLOW, MACHINE)
        with caplog.at_level(logging.INFO, logger="tianluo.server.state"):
            await _append(
                state, [_record(i) for i in range(5, 10)],
                cursor=_cursor(10), cursor_base=_cursor(5),
            )
        return caplog.text

    text = asyncio.run(scenario())
    assert "BACKFILL-APPLIED" in text


# --------------------------------------------------------------------------
# (d) downgrade branches: no bundle / machine-change / active-worktree → full
# --------------------------------------------------------------------------


def test_plan_recovery_pull_full_when_no_bundle():
    """A first-sighting append (no bundle to extend) plans a cursorless full."""

    async def scenario():
        state = ServerState()
        # A first-sighting append: no bundle, flagged requires_full, discarded.
        outcome = await _append(
            state, [_record(20)], cursor=_cursor(21), cursor_base=_cursor(20),
        )
        assert outcome.resolves_pull is False
        assert FLOW in state._history_requires_full

        plan = await state.plan_recovery_pull(FLOW, MACHINE)
        # No bundle to extend → full rebuild, and requires_full STAYS armed.
        assert plan == ("full", None)
        assert FLOW in state._history_requires_full
        return state

    asyncio.run(scenario())


def test_plan_recovery_pull_full_on_machine_change():
    """A gap-triggered self-heal for a DIFFERENT machine plans a cursorless full."""

    async def scenario():
        state = ServerState()
        await _full(state, [_record(i) for i in range(5)], _cursor(5))
        # A gap arm on the ORIGINAL machine.
        await _gap_append(state, 8, 10)
        assert FLOW in state._history_requires_full

        # A different daemon now owns the flow: its bundle is not extendable by
        # us, so even though a bundle is cached the plan must be a full rebuild.
        plan = await state.plan_recovery_pull(FLOW, "m2")
        assert plan == ("full", None)
        assert FLOW in state._history_requires_full
        return state

    asyncio.run(scenario())


def test_plan_recovery_pull_full_for_active_worktree_flow():
    """An active worktree flow (history split across roots) plans a full pull."""

    async def scenario():
        state = ServerState()
        await _active_worktree_flow(state, status="running")
        await _full(state, [_record(i) for i in range(5)], _cursor(5))
        await _gap_append(state, 8, 10)
        assert FLOW in state._history_requires_full

        plan = await state.plan_recovery_pull(FLOW, MACHINE)
        # A worktree flow's cursorless full is the ONLY pull that reunites the
        # discovery slice (main root) with the later steps (worktree root).
        assert plan == ("full", None)
        assert FLOW in state._history_requires_full
        return state

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# TTL dedup / no-flag: plan_recovery_pull mirrors take_recovery_pull's gating
# --------------------------------------------------------------------------


def test_plan_recovery_pull_none_when_not_flagged_or_inflight():
    async def scenario():
        state = ServerState()
        await _full(state, [_record(i) for i in range(5)], _cursor(5))
        # A healthy flow (never went stuck) plans nothing.
        assert await state.plan_recovery_pull(FLOW, MACHINE) is None

        # Arm a gap, take the first plan, then a second within TTL is deduped.
        await _gap_append(state, 8, 10)
        first = await state.plan_recovery_pull(FLOW, MACHINE)
        assert first[0] == "incremental"
        # requires_full was cleared by the incremental decision; re-arm a gap to
        # prove the in-flight marker (not the flag) is what dedups a rival pull.
        await _gap_append(state, 8, 10)
        assert await state.plan_recovery_pull(FLOW, MACHINE) is None
        return state

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# end-to-end via the ws.py dispatch: a gap append drives an INCREMENTAL request
# --------------------------------------------------------------------------


class _RecordingDaemonWS:
    def __init__(self) -> None:
        self.requests: list = []

    async def send_text(self, data: str) -> None:
        msg = protocol.decode(data)
        if msg.type == protocol.MSG_HISTORY_REQUEST:
            self.requests.append(msg.payload)


async def _ws_setup(owner_id: str = "owner-A"):
    state = ServerState()
    await state.register_machine(MACHINE, "host", "9.9.9", owner_id=owner_id)
    hub = UiHub()
    registry = HistoryRequestRegistry()
    manager = ConnectionManager()
    daemon = _RecordingDaemonWS()
    await manager.connect(MACHINE, daemon)
    return state, hub, registry, manager, daemon


def _hist_frame(mode, records, cursor, cursor_base=None):
    return protocol.make_history_data(
        FLOW, mode, records, cursor=cursor, cursor_base=cursor_base or {},
    )


def test_ws_dispatch_sends_incremental_request_carrying_server_cursor():
    """Through _handle_message, a gap append on a good bundle pulls WITH a cursor."""

    async def scenario():
        state, hub, registry, manager, daemon = await _ws_setup()

        # Seed the authoritative bundle (as a resolved full pull reply would).
        await _handle_message(
            _hist_frame(
                protocol.HISTORY_MODE_FULL,
                [_record(i) for i in range(5)], _cursor(5),
            ),
            state, MACHINE, hub, registry, manager=manager, connection=daemon,
        )
        assert daemon.requests == []  # a full reply arms no recovery

        # A reconnect append jumps to line 8 → gap → self-heal dispatch.
        await _handle_message(
            _hist_frame(
                protocol.HISTORY_MODE_APPEND,
                [_record(8), _record(9)], _cursor(10), _cursor(8),
            ),
            state, MACHINE, hub, registry, manager=manager, connection=daemon,
        )
        return daemon.requests

    requests = asyncio.run(scenario())
    # Exactly one self-heal request, and it carries the server's OWN water mark
    # (an incremental append backfill) rather than a cursorless full.
    assert len(requests) == 1
    assert requests[0]["cursor"] == _cursor(5)


def test_ws_dispatch_sends_cursorless_full_when_no_bundle():
    """A first-sighting append (no bundle) still pulls a cursorless full."""

    async def scenario():
        state, hub, registry, manager, daemon = await _ws_setup()
        await _handle_message(
            _hist_frame(
                protocol.HISTORY_MODE_APPEND,
                [_record(20)], _cursor(21), _cursor(20),
            ),
            state, MACHINE, hub, registry, manager=manager, connection=daemon,
        )
        return daemon.requests

    requests = asyncio.run(scenario())
    assert len(requests) == 1
    # No bundle to extend → cursorless full rebuild.
    assert requests[0]["cursor"] == {}
