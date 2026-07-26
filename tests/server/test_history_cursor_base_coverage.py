"""An append frame's declared coverage window — not its record count — decides
contiguity (#287 follow-up).

The daemon's cursor counts every PHYSICAL line it consumed, while only parseable
dict lines become records: a delta that stepped over a blank / mid-write /
unparseable line carries FEWER records than its cursor advanced. Deriving the
frame's start line from ``cursor - len(records)`` (or from its records' lowest
ordinal) therefore lands past the cached water mark and condemns a perfectly
contiguous frame as a hole — the live delta is discarded, ``requires_full`` is
armed and a recovery pull fires, so subscribed consoles stall on a stale chat
until it round-trips.

The frame now states the window it covers (``cursor_base`` → ``cursor``). That is
the only sound source for the start line, and it is what keeps the two cases
apart: a frame that SKIPPED a line (nothing lost) and a frame that never received
one (lines lost) can be record-for-record identical, so no count can separate
them — see the discriminating pair below.
"""

from __future__ import annotations

import asyncio
import json

from tianluo.daemon import protocol
from tianluo.daemon.history import DaemonHistoryReader
from tianluo.server.state import ServerState

FLOW = "20260714-093536_a4af4b75"
MACHINE = "m1"
STEP_FILE = "01_discovery_b287551e.jsonl"
STEP_ID = "01_discovery_b287551e"


def _record(ordinal: int) -> dict:
    return {
        "step_id": STEP_ID,
        "step_type": "discovery",
        "ordinal": ordinal,
        "message": {"role": "assistant", "content": f"line {ordinal}"},
    }


async def _running_flow(state: ServerState) -> None:
    await state.update_status(
        MACHINE,
        {
            "machine_id": MACHINE,
            "flows": [
                {
                    "flow_id": FLOW,
                    "project_root": "/repo/tianluo/worktrees/wt-a4af4b75",
                    "status": "running",
                }
            ],
        },
    )


async def _seed_bundle(state: ServerState, lines: int) -> None:
    """Establish an authoritative bundle whose water mark is *lines*."""
    await state.apply_history_frame(
        FLOW,
        protocol.HISTORY_MODE_FULL,
        [_record(i) for i in range(lines)],
        cursor={STEP_FILE: lines},
        machine_id=MACHINE,
    )


async def _append(state: ServerState, records, cursor, cursor_base):
    return await state.apply_history_frame(
        FLOW,
        protocol.HISTORY_MODE_APPEND,
        records,
        cursor=cursor,
        cursor_base=cursor_base,
        machine_id=MACHINE,
    )


# --------------------------------------------------------------------------
# the discriminating pair: identical records + cursor, opposite verdicts
# --------------------------------------------------------------------------


def test_append_that_skipped_a_line_is_contiguous_and_applies():
    """The reported divergence: the cache is at line 25, the daemon reads lines
    25..30 where line 25 is blank (or fails ``json.loads``), and ships 5 records
    (ordinals 26..30) with cursor 31. Nothing was lost — the frame's window starts
    exactly at the water mark — so the delta must reach the bundle immediately."""

    async def scenario():
        state = ServerState()
        await _running_flow(state)
        await _seed_bundle(state, 25)

        outcome = await _append(
            state,
            [_record(i) for i in range(26, 31)],
            {STEP_FILE: 31},
            {STEP_FILE: 25},
        )

        assert outcome.resolves_pull is True
        bundle = await state.get_history(FLOW)
        assert [r["ordinal"] for r in bundle["records"]] == list(range(25)) + [
            26, 27, 28, 29, 30
        ]
        assert bundle["cursor"] == {STEP_FILE: 31}
        # No self-heal was armed: there is no hole to heal.
        assert await state.take_recovery_pull(FLOW) is False

    asyncio.run(scenario())


def test_append_whose_window_starts_past_the_water_mark_is_still_a_gap():
    """The other half of the pair. Same record count, same cursor, same ordinals'
    span as a legitimate skip — but the frame declares it began reading at line 28
    while the cache holds only 25, so lines 25..27 were consumed and never
    delivered. A record count cannot tell this from the test above; the declared
    window can."""

    async def scenario():
        state = ServerState()
        await _running_flow(state)
        await _seed_bundle(state, 25)

        outcome = await _append(
            state,
            [_record(i) for i in range(28, 31)],
            {STEP_FILE: 31},
            {STEP_FILE: 28},
        )

        assert outcome.resolves_pull is False
        bundle = await state.get_history(FLOW)
        # Nothing taken from the frame — neither records nor cursor.
        assert [r["ordinal"] for r in bundle["records"]] == list(range(25))
        assert bundle["cursor"] == {STEP_FILE: 25}
        assert await state.take_recovery_pull(FLOW) is True

    asyncio.run(scenario())


def test_advanced_file_with_no_declared_window_is_a_gap():
    """A frame that declares windows but advances a file's water mark WITHOUT one
    consumed those lines and put them nowhere — the daemon's cursor ran ahead of
    its own delivery (the #287 root shape). Judged a gap, not silently adopted."""

    async def scenario():
        state = ServerState()
        await _running_flow(state)
        await _seed_bundle(state, 2)
        other = "02_analyze_c001.jsonl"

        outcome = await _append(
            state,
            [_record(2)],
            {STEP_FILE: 3, other: 4},   # 02_analyze advanced...
            {STEP_FILE: 2},             # ...but the frame never read it
        )

        assert outcome.resolves_pull is False
        assert (await state.get_history(FLOW))["cursor"] == {STEP_FILE: 2}
        assert await state.take_recovery_pull(FLOW) is True

    asyncio.run(scenario())


def test_overlapping_resend_window_still_applies():
    """A window that starts BEFORE the water mark is a re-send (the daemon's
    cursor only advances on a successful send), not a gap: it re-delivers records
    the bundle already holds and the frontend's dedupe collapses them."""

    async def scenario():
        state = ServerState()
        await _running_flow(state)
        await _seed_bundle(state, 3)

        outcome = await _append(
            state,
            [_record(1), _record(2), _record(3)],
            {STEP_FILE: 4},
            {STEP_FILE: 1},
        )

        assert outcome.resolves_pull is True
        assert await state.take_recovery_pull(FLOW) is False

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# end to end: the real reader's frame, applied by the real server state
# --------------------------------------------------------------------------


def test_real_daemon_delta_over_a_blank_line_reaches_the_bundle(tmp_path):
    """Drive the actual ``DaemonHistoryReader`` over a step file that grows a
    blank line mid-stream, and feed its frames to the actual ``ServerState``. The
    blank line makes the cursor advance by one more than the records delivered —
    the exact shape that used to be misread as a hole."""

    jsonl = tmp_path / "tianluo" / "history" / FLOW / STEP_FILE
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text(
        json.dumps({"role": "user", "content": "q1"}) + "\n"
        + json.dumps({"role": "assistant", "content": "a1"}) + "\n",
        encoding="utf-8",
    )
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])

    full = reader.read_flow(FLOW, project_root=str(tmp_path))
    assert full.cursor == {STEP_FILE: 2}
    assert full.cursor_base == {STEP_FILE: 0}

    # A blank line lands between the rounds (a flush artifact / mid-write line
    # the reader steps over), then two real records follow.
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write("\n")
        fh.write(json.dumps({"role": "user", "content": "q2"}) + "\n")
        fh.write(json.dumps({"role": "assistant", "content": "a2"}) + "\n")

    delta = reader.read_flow(
        FLOW, project_root=str(tmp_path), cursor=dict(full.cursor)
    )
    # 2 records but a cursor advanced by 3 — the count can no longer locate the
    # frame's start, while the declared base still anchors it at the water mark.
    assert len(delta.records) == 2
    assert delta.cursor == {STEP_FILE: 5}
    assert delta.cursor_base == {STEP_FILE: 2}

    async def scenario():
        state = ServerState()
        await _running_flow(state)
        await state.apply_history_frame(
            FLOW, full.mode, full.records,
            cursor=full.cursor, cursor_base=full.cursor_base,
            machine_id=MACHINE,
        )
        outcome = await state.apply_history_frame(
            FLOW, delta.mode, delta.records,
            cursor=delta.cursor, cursor_base=delta.cursor_base,
            machine_id=MACHINE,
        )

        assert outcome.resolves_pull is True
        bundle = await state.get_history(FLOW)
        assert [r["message"]["content"] for r in bundle["records"]] == [
            "q1", "a1", "q2", "a2",
        ]
        assert await state.take_recovery_pull(FLOW) is False

    asyncio.run(scenario())
