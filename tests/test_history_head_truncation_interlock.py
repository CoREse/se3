"""Regression lock: the CONFIRMED live trigger chain of the #287 head truncation.

G1-G3 each fixed one hole and each locked its own hole in isolation. This test
locks the chain that actually fired in the field, which is what the isolated
tests cannot see: **neither hole truncates history on its own — only their
interlock does.** A DEBUG replay of the pre-fix code over a real worktree layout
(daemon reader + server state, no browser) settled it:

  hole 1 alone (daemon commits a cursor for a frame whose send failed) — the
    dropped batch leaves the server with NO bundle, so the daemon's next
    ``append`` hits the pre-existing "first-sighting-append" guard, which flags
    ``requires_full`` and self-heals via ``take_recovery_pull``. History ends
    COMPLETE. The safety net does its job.

  hole 2 alone (an empty ``full`` establishes an authoritative empty bundle) —
    harmless by itself, because the daemon's cursor stays at ``{}``: its next
    read is therefore still a *full* carrying every record, which replaces the
    empty bundle. History ends COMPLETE.

  hole 1 + hole 2 — fatal. The empty bundle from hole 2 is exactly what DEFEATS
    the safety net that would have caught hole 1: the late ``append`` is no
    longer a "first sighting" (``existing`` is not ``None`` any more), so it is
    extended straight onto the empty bundle. The head is gone, ``requires_full``
    is never set, the self-heal never fires, and every later poll serves the
    truncated bundle as ``not_modified`` — permanently. That is precisely the
    reported symptom: chat correct from the second message on, first round
    missing forever, no refresh brings it back.

The lesson the isolated tests cannot encode: hole 2's real damage was never the
empty bundle itself, it was DISARMING the recovery path for hole 1. So this test
drives the whole chain through the real components and asserts the head survives.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from tianluo.daemon.history import DaemonHistoryReader
from tianluo.server.state import ServerState

FLOW = "20260714-093536_a4af4b75"
MACHINE = "m1"
STEP_FILE = "01_discovery_b287551e.jsonl"

ROUND1 = 26  # records the discovery step flushed before the frame was lost
ROUND2 = 5   # records appended afterwards


def _flush(root: Path, count: int, *, start: int) -> None:
    """Append *count* jsonl records to the flow's discovery step file."""
    path = root / "tianluo" / "history" / FLOW / STEP_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for ordinal in range(start, start + count):
            handle.write(
                json.dumps(
                    {
                        "step_id": STEP_FILE[:-6],
                        "step_type": "discovery",
                        "ordinal": ordinal,
                        "message": {"role": "assistant", "content": f"line {ordinal}"},
                    }
                )
                + "\n"
            )


async def _mark_active_worktree_flow(state: ServerState, root: Path) -> None:
    await state.update_status(
        MACHINE,
        {
            "machine_id": MACHINE,
            "flows": [
                {"flow_id": FLOW, "project_root": str(root), "status": "running"},
            ],
        },
    )


async def _apply(state: ServerState, read) -> None:
    await state.apply_history_frame(
        FLOW, read.mode, read.records, cursor=read.cursor, machine_id=MACHINE,
    )


def _ordinals(state: ServerState) -> list[int]:
    bundle = state._history_data.get(FLOW)
    if bundle is None:
        return []
    return [record.get("ordinal") for record in bundle["records"]]


def test_lost_frame_then_empty_full_still_yields_complete_history(tmp_path):
    """The confirmed live chain: a lost frame AND an empty full, head survives."""

    # The scenario drives its own loop: pytest-asyncio is not a test dependency
    # of this project, so the suite must run on a bare pytest install.
    async def scenario():
        # A real `se3 run --worktree` layout: the flow body lives under
        # <main>/tianluo/worktrees/<name>/ and keeps its own tianluo/history there. The
        # path shape matters — it is what makes the server treat the flow as an
        # active worktree flow, which scopes the empty-full guard.
        root = tmp_path / "tianluo" / "worktrees" / "wt-a4af4b75"
        (root / "tianluo" / "history" / FLOW).mkdir(parents=True)

        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(root)])
        state = ServerState()
        await _mark_active_worktree_flow(state, root)

        # T0 — the daemon sees the flow the moment engine.json says "running",
        # which is BEFORE the discovery step has flushed its first jsonl line.
        # read_flow honestly reports "no records", indistinguishable on the wire
        # from a failed root resolution. This is the empty full (hole 2).
        empty_full = reader.read_flow(FLOW, project_root=str(root), cursor=None)
        assert empty_full.records == []
        await _apply(state, empty_full)

        # It must NOT have become the authoritative bundle: an active worktree
        # flow cannot legitimately have zero records, and installing it here is
        # what would disarm the recovery path the lost frame below depends on.
        assert state._history_data.get(FLOW) is None, (
            "an empty full frame was installed as the authoritative bundle for an "
            "active worktree flow — this is the interlock that made the head loss "
            "permanent"
        )

        # T1 — round 1 lands on disk and the daemon reads it, but the frame never
        # reaches the server (socket down mid-send). The daemon must NOT commit
        # the cursor for a batch it failed to deliver (hole 1); here we simulate
        # the loss by simply not applying the frame, and — reproducing the pre-fix
        # bug exactly — carrying its cursor forward anyway, so the batch is never
        # re-sent.
        _flush(root, ROUND1, start=0)
        lost = reader.read_flow(FLOW, project_root=str(root), cursor=None)
        assert len(lost.records) == ROUND1
        advanced_cursor = lost.cursor  # the water mark the daemon wrongly kept

        # T2 — round 2 lands, and the daemon appends from that advanced water
        # mark. The frame's own cursor says it covers lines 26..30, but the server
        # holds nothing: the head never arrived. It must be refused, not extended
        # onto an empty bundle.
        _flush(root, ROUND2, start=ROUND1)
        tail = reader.read_flow(FLOW, project_root=str(root), cursor=advanced_cursor)
        assert len(tail.records) == ROUND2
        await _apply(state, tail)

        assert _ordinals(state) != list(range(ROUND1, ROUND1 + ROUND2)), (
            "a head-truncated bundle was established from an append that starts "
            "past the cached water mark"
        )

        # T3 — the refusal armed the self-heal, so the server pulls a full
        # snapshot and the daemon re-reads the flow from line 0.
        assert await state.take_recovery_pull(FLOW) is True
        healed = reader.read_flow(FLOW, project_root=str(root), cursor=None)
        await _apply(state, healed)

        # The invariant the user actually cares about: the browser sees round 1.
        assert _ordinals(state) == list(range(ROUND1 + ROUND2))
        assert FLOW not in state._history_requires_full

    asyncio.run(scenario())
