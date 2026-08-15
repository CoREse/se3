"""Compacted daemon frames assemble into a clean server bundle.

Record compaction changes what a frame *contains* (an oversized record ships
shrunken) but must change nothing about how frames *chain*: the cursor is a
physical line count, so the server's continuity check — the last-resort guard
that refuses a frame which would bake a hole into a flow's history — has to see
exactly the same sequence it saw before.

This drives the real ``DaemonHistoryReader`` over a step file holding a 16 MB+
record and replays its actual chunked frames into a real ``ServerState``. It is
therefore also the negative test for the two explicit non-goals: no bundle-level
total cap and no on-demand fetch endpoint were introduced, so the head-loss /
gap self-heal must stay disarmed all the way through the drain and the assembled
bundle must hold every physical line exactly once.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tianluo.daemon import protocol
from tianluo.daemon import record_budget as rb
from tianluo.daemon.history import DaemonHistoryReader
from tianluo.server.state import ServerState

FLOW = "20260815-194018_54be5f6a"
MACHINE = "m1"
STEP_FILE = "01_discovery.jsonl"


# --------------------------------------------------------------------------
# fixture flow
# --------------------------------------------------------------------------


def _thinking_event(index):
    return {
        "type": "system",
        "subtype": "thinking_tokens",
        "estimated_tokens": 50 * index,
        "uuid": "uuid-%d" % index,
    }


def _chip_events(index, body):
    return [
        {
            "type": "assistant",
            "message": {
                "id": "msg-%d" % index,
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_%04d" % index,
                             "name": "Bash", "input": {"command": "run %d" % index}}],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"tool_use_id": "toolu_%04d" % index,
                             "type": "tool_result", "is_error": False,
                             "content": body}],
            },
        },
    ]


def _oversized_record(chips=206, body_bytes=80 * 1024):
    events = []
    for chip in range(chips):
        for i in range(8):
            events.append(_thinking_event(chip * 8 + i))
        events.extend(
            _chip_events(chip, ("payload-%05d " % chip) * (body_bytes // 15))
        )
    events.append({"type": "result", "subtype": "success",
                   "usage": {"input_tokens": 49, "output_tokens": 14317}})
    return {
        "role": "assistant",
        "content": "final answer",
        "raw_json": events,
        "step_type": "discovery",
    }


def _small_record(index):
    return {
        "role": "assistant",
        "content": "round %d %s" % (index, "y" * 6000),
        "raw_json": [{"type": "assistant",
                      "message": {"role": "assistant", "content": "hi"}}],
        "step_type": "discovery",
    }


TOTAL_LINES = 121
BIG_INDEX = 60


@pytest.fixture(scope="module")
def drained_frames(tmp_path_factory):
    """Every frame a real reader emits draining a flow with one 16 MB record."""
    root = tmp_path_factory.mktemp("root")
    flow_dir = root / "tianluo" / "history" / FLOW
    flow_dir.mkdir(parents=True)
    with (flow_dir / STEP_FILE).open("w", encoding="utf-8") as fh:
        for index in range(TOTAL_LINES):
            record = _oversized_record() if index == BIG_INDEX else _small_record(index)
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write("\n")

    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(root)])
    frames = []
    cursor = None
    for _ in range(500):
        result = reader.read_flow(FLOW, cursor=cursor)
        frames.append(result)
        cursor = result.cursor
        if not result.truncated:
            break
    else:  # pragma: no cover - a runaway drain is a test bug
        pytest.fail("drain did not terminate")
    assert len(frames) > 3, "fixture did not exercise multi-frame chunking"
    return frames


async def _replay(frames):
    """Feed *frames* to a real ServerState the way the daemon relay does."""
    state = ServerState()
    await state.update_status(
        MACHINE,
        {"machine_id": MACHINE,
         "flows": [{"flow_id": FLOW, "project_root": "/repo", "status": "running"}]},
    )
    outcomes = []
    for frame in frames:
        outcomes.append(
            await state.apply_history_frame(
                FLOW,
                protocol.HISTORY_MODE_FULL if frame.mode == "full"
                else protocol.HISTORY_MODE_APPEND,
                frame.records,
                cursor=frame.cursor,
                cursor_base=frame.cursor_base,
                machine_id=MACHINE,
            )
        )
    return state, outcomes


# --------------------------------------------------------------------------
# bundle assembly
# --------------------------------------------------------------------------


def test_compacted_frames_assemble_without_gaps_or_duplicates(drained_frames):
    async def scenario():
        state, _ = await _replay(drained_frames)
        bundle = await state.get_history(FLOW)

        keys = [(r["step_id"], r["ordinal"]) for r in bundle["records"]]
        assert len(keys) == len(set(keys)), "a record was delivered twice"
        assert [ordinal for _, ordinal in keys] == list(range(TOTAL_LINES))

    asyncio.run(scenario())


def test_every_frame_is_accepted_as_authoritative(drained_frames):
    async def scenario():
        _, outcomes = await _replay(drained_frames)
        assert all(o.resolves_pull for o in outcomes)
        assert not any(getattr(o, "rejected_full", False) for o in outcomes)

    asyncio.run(scenario())


def test_head_loss_and_gap_selfheal_are_never_triggered(drained_frames):
    """The non-goal check: no bundle-level cap, so nothing to self-heal from."""

    async def scenario():
        state, _ = await _replay(drained_frames)
        assert await state.take_recovery_pull(FLOW) is False

    asyncio.run(scenario())


def test_cursor_advances_monotonically_across_the_drain(drained_frames):
    async def scenario():
        state, _ = await _replay(drained_frames)

        seen = 0
        for frame in drained_frames:
            lines = frame.cursor.get(STEP_FILE, 0)
            assert lines >= seen, "cursor moved backwards"
            seen = lines
        assert seen == TOTAL_LINES

        bundle = await state.get_history(FLOW)
        assert bundle["cursor"] == drained_frames[-1].cursor

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# what the browser ends up with
# --------------------------------------------------------------------------


def test_bundled_oversized_record_is_within_budget_with_all_chips(drained_frames):
    async def scenario():
        state, _ = await _replay(drained_frames)
        bundle = await state.get_history(FLOW)

        big = next(r for r in bundle["records"] if r["ordinal"] == BIG_INDEX)
        raw_json = big["message"]["raw_json"]
        size = len(json.dumps(raw_json, ensure_ascii=False).encode("utf-8"))
        assert size <= rb.MAX_RECORD_RAW_JSON_BYTES

        tool_uses = [
            block["id"]
            for event in raw_json
            if isinstance(event, dict)
            for block in ((event.get("message") or {}).get("content") or [])
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        assert tool_uses == ["toolu_%04d" % i for i in range(206)]
        assert raw_json[-1]["type"] == "result"

    asyncio.run(scenario())


def test_small_records_reach_the_bundle_unmodified(drained_frames):
    async def scenario():
        state, _ = await _replay(drained_frames)
        bundle = await state.get_history(FLOW)

        first = next(r for r in bundle["records"] if r["ordinal"] == 0)
        assert first["message"] == _small_record(0)

    asyncio.run(scenario())
