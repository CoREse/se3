"""Regression lock: the server's history bundle may never be head-truncated.

Two structural holes let a live worktree discovery flow lose the FIRST round of
its chat permanently — the browser opened the flow and saw everything *except*
the beginning, and no refresh brought it back:

  hole 1 — an EMPTY ``full`` frame landing on a flow the server holds NO bundle
    for was accepted as authoritative. The daemon emits exactly that frame when
    it fails to resolve a flow's history directory (``read_flow`` returns
    ``mode=full, records=[]``), so a single unresolved read pinned an empty
    bundle AND cleared ``requires_full`` / the recovery marker — disarming the
    self-heal that existed precisely for this.

  hole 2 — an ``append`` frame was extended onto the bundle without checking
    that it CONTINUES it. A frame whose cursor says "I cover lines 20..25" landing
    on a bundle that holds nothing (or only lines 0..5) silently established a
    bundle with a hole in its head, which every later poll then served as
    ``not_modified`` — the loss became permanent.

The gap check is the last-resort invariant of the whole history path: it assumes
nothing about the daemon's correctness and still detects a hole, because the
frame's own cursor states which lines it claims to cover. These tests drive
``ServerState`` directly (no daemon, no browser) at exactly that seam.
"""

from __future__ import annotations

import asyncio

import pytest

from tianluo.daemon import protocol
from tianluo.server.state import ServerState

FLOW = "20260714-093536_a4af4b75"
MACHINE = "m1"
STEP_FILE = "01_discovery_b287551e.jsonl"
STEP_ID = "01_discovery_b287551e"


def _record(ordinal: int) -> dict:
    """A daemon history record: ``step_id`` + its physical line ``ordinal``."""
    return {
        "step_id": STEP_ID,
        "step_type": "discovery",
        "ordinal": ordinal,
        "message": {"role": "assistant", "content": f"line {ordinal}"},
    }


def _cursor(lines: int) -> dict:
    return {STEP_FILE: lines}


async def _active_worktree_flow(state: ServerState, status: str = "paused") -> None:
    """Make FLOW a live ``--worktree`` discovery flow of MACHINE."""
    await state.update_status(
        MACHINE,
        {
            "machine_id": MACHINE,
            "flows": [
                {
                    "flow_id": FLOW,
                    "project_root": "/repo/se3/worktrees/wt-a4af4b75",
                    "status": status,
                }
            ],
        },
    )


async def _ordinary_flow(state: ServerState, status: str = "completed") -> None:
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


# --------------------------------------------------------------------------
# (a) an empty full may not establish an authoritative empty bundle
# --------------------------------------------------------------------------


def test_empty_full_on_empty_cache_is_rejected_and_keeps_selfheal_armed():
    async def scenario():
        state = ServerState()
        await _active_worktree_flow(state)

        outcome = await _full(state, [], {})

        # Nothing authoritative was established — a REST read stays a cache miss
        # and re-pulls, instead of serving a blank chat forever.
        assert await state.get_history(FLOW) is None
        # The frame is known-untrustworthy, so it is relayed nowhere and no REST
        # waiter is woken with "authoritatively empty" (it ends on the pull
        # timeout instead, and the client retries).
        assert outcome.rejected_full is True
        assert outcome.resolves_pull is False
        # The self-heal stays ARMED — this is the half the old guard lost.
        assert await state.take_recovery_pull(FLOW) is True
        # ...and fires exactly once (no pull storm while it is in flight).
        assert await state.take_recovery_pull(FLOW) is False

        # The daemon's next (resolved) full reply heals the flow completely.
        await _full(state, [_record(0), _record(1)], _cursor(2))
        bundle = await state.get_history(FLOW)
        assert [r["ordinal"] for r in bundle["records"]] == [0, 1]
        assert await state.take_recovery_pull(FLOW) is False

    asyncio.run(scenario())


def test_empty_full_on_ordinary_flow_still_establishes_a_bundle():
    """The guard is scoped to ACTIVE WORKTREE flows: a terminal/ordinary flow
    that genuinely has no records must still cache as an empty bundle (serving
    it from cache is correct and spares a pull on every open)."""

    async def scenario():
        state = ServerState()
        await _ordinary_flow(state)

        outcome = await _full(state, [], {})

        bundle = await state.get_history(FLOW)
        assert bundle is not None
        assert bundle["records"] == []
        assert outcome.rejected_full is False
        assert outcome.resolves_pull is True

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# (b) a gapped append is refused and self-heals
# --------------------------------------------------------------------------


def test_gapped_append_on_existing_bundle_is_refused_and_heals():
    """Cursor jump: the bundle holds lines 0..1, the frame claims 4..4."""

    async def scenario():
        state = ServerState()
        await _active_worktree_flow(state)
        await _full(state, [_record(0), _record(1)], _cursor(2))

        outcome = await _append(state, [_record(4)], _cursor(5))

        assert outcome.resolves_pull is False
        bundle = await state.get_history(FLOW)
        # Neither the records nor the cursor of a gapped frame are taken.
        assert [r["ordinal"] for r in bundle["records"]] == [0, 1]
        assert bundle["cursor"] == _cursor(2)
        # Exactly one recovery pull is armed, not one per discarded frame.
        assert await state.take_recovery_pull(FLOW) is True
        await _append(state, [_record(5)], _cursor(6))
        assert await state.take_recovery_pull(FLOW) is False

        # The full reply to that pull rebuilds the bundle from line 0.
        await _full(
            state, [_record(i) for i in range(6)], _cursor(6),
        )
        bundle = await state.get_history(FLOW)
        assert [r["ordinal"] for r in bundle["records"]] == [0, 1, 2, 3, 4, 5]
        assert await state.take_recovery_pull(FLOW) is False

    asyncio.run(scenario())


def test_nonzero_start_append_on_empty_bundle_is_refused():
    """Same hole, other shape: the bundle is empty (a legitimately empty full for
    a flow that had not written yet) and the first append starts at line 3 — the
    flow's head never arrived, so it must not be extended onto nothing."""

    async def scenario():
        state = ServerState()
        await _ordinary_flow(state, status="running")
        await _full(state, [], {})
        assert (await state.get_history(FLOW))["records"] == []

        outcome = await _append(state, [_record(3)], _cursor(4))

        assert outcome.resolves_pull is False
        bundle = await state.get_history(FLOW)
        assert bundle["records"] == []
        assert bundle["cursor"] == {}
        assert await state.take_recovery_pull(FLOW) is True

        await _full(state, [_record(i) for i in range(4)], _cursor(4))
        bundle = await state.get_history(FLOW)
        assert [r["ordinal"] for r in bundle["records"]] == [0, 1, 2, 3]

    asyncio.run(scenario())


def test_gap_in_any_one_file_refuses_the_whole_frame():
    """Per-file water marks are checked independently: a frame contiguous on one
    step file but gapped on another is refused whole (accepting its good half
    would advance the cursor past the lines the bad half lost)."""

    async def scenario():
        state = ServerState()
        await _active_worktree_flow(state)
        a, b = "01_discovery.jsonl", "02_analyze.jsonl"
        await state.apply_history_frame(
            FLOW, protocol.HISTORY_MODE_FULL,
            [{"step_id": "01_discovery", "ordinal": 0}],
            cursor={a: 1}, machine_id=MACHINE,
        )

        outcome = await state.apply_history_frame(
            FLOW, protocol.HISTORY_MODE_APPEND,
            [
                {"step_id": "01_discovery", "ordinal": 1},   # contiguous
                {"step_id": "02_analyze", "ordinal": 2},     # starts at line 2 (gap)
            ],
            cursor={a: 2, b: 3}, machine_id=MACHINE,
        )

        assert outcome.resolves_pull is False
        bundle = await state.get_history(FLOW)
        assert len(bundle["records"]) == 1
        assert bundle["cursor"] == {a: 1}
        assert await state.take_recovery_pull(FLOW) is True

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# (c) negative cases — the guard must not fire on legitimate frames
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "records, cursor, expected",
    [
        # Contiguous: the frame starts exactly at the cached water mark.
        ([2, 3], 4, [0, 1, 2, 3]),
        # Overlap: a daemon re-sending a frame whose previous send failed (its
        # cursor only advances on a successful send) re-delivers line 1. Not a
        # gap — the frontend's dedupe collapses it; refusing it would turn every
        # retry into a full pull.
        ([1, 2], 3, [0, 1, 1, 2]),
    ],
)
def test_contiguous_and_overlapping_appends_apply(records, cursor, expected):
    async def scenario():
        state = ServerState()
        await _active_worktree_flow(state)
        await _full(state, [_record(0), _record(1)], _cursor(2))

        outcome = await _append(
            state, [_record(i) for i in records], _cursor(cursor)
        )

        assert outcome.resolves_pull is True
        bundle = await state.get_history(FLOW)
        assert [r["ordinal"] for r in bundle["records"]] == expected
        assert await state.take_recovery_pull(FLOW) is False

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# (d) the live timeline that produced the bug
# --------------------------------------------------------------------------


def test_timeline_empty_full_then_appends_still_serves_a_complete_history():
    """The observed live chain, end to end.

    Round 1 of a worktree discovery is written to disk, but the daemon's read
    resolves nothing and answers with an empty ``full``; the live push loop then
    keeps appending later rounds on top of it. Before the fix that empty full
    became the authoritative bundle and the appends piled onto it, so every
    client that opened the flow got a chat with its head missing — permanently.
    Now the empty full is refused, the appends find no bundle to extend, and the
    self-heal pull's full reply serves EVERY client the history from line 0.
    """

    async def scenario():
        state = ServerState()
        await _active_worktree_flow(state, status="running")

        # 1. The daemon's unresolved read → empty full. Refused.
        assert (await _full(state, [], {})).rejected_full is True

        # 2. The push loop's later appends (rounds 2+) find nothing to anchor to.
        assert (await _append(state, [_record(3)], _cursor(4))).resolves_pull is False
        assert (await _append(state, [_record(4)], _cursor(5))).resolves_pull is False
        assert await state.get_history(FLOW) is None

        # 3. The self-heal fires ONCE and the daemon's now-resolved full answers
        #    with the flow's complete history.
        assert await state.take_recovery_pull(FLOW) is True
        await _full(state, [_record(i) for i in range(5)], _cursor(5))

        # 4. A newly subscribing client reads the bundle: complete from line 0,
        #    no head truncation.
        bundle = await state.get_history(FLOW)
        assert [r["ordinal"] for r in bundle["records"]] == [0, 1, 2, 3, 4]

        # 5. And the live stream flows again — later appends now anchor cleanly.
        assert (await _append(state, [_record(5)], _cursor(6))).resolves_pull is True
        bundle = await state.get_history(FLOW)
        assert [r["ordinal"] for r in bundle["records"]] == [0, 1, 2, 3, 4, 5]

    asyncio.run(scenario())
