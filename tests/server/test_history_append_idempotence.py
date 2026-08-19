"""Regression lock: a re-delivered history record may never double the bundle.

The live symptom: a running ``--worktree`` flow's WebUI chat rendered the plan
step's ``step_completed`` and the confirm step's user/assistant records FOUR
times and climbing. Every ``full_pull_throttled`` window, the running-worktree
self-heal reconcile fires one ``HISTORY_REQUEST``; the owning daemon drains the
WHOLE flow as a ``full`` HEAD (byte-capped, so far shorter than the bundle) plus
a run of ``append`` TAILS. The HEAD is refused as a shrinking full — which keeps
the bundle but overwrites its cursor with the HEAD's — so every following tail
now OVERLAPS the water mark. ``_detect_cursor_gap`` deliberately does not call
an overlap a gap (a routine retry must not trigger a pull storm), and the append
branch then simply ``extend``-ed, so each window grew the server's cached bundle
by the flow's entire length.

These tests drive ``ServerState`` directly (no daemon, no browser) at exactly
that seam: the same drain replayed N times must leave the bundle byte-for-byte
where it was — same record count, same ``(step_id, ordinal)`` set, same order,
same generation and signature — so an already-synced client keeps getting
``not_modified`` instead of a growing full snapshot.
"""

from __future__ import annotations

import asyncio

from tianluo.daemon import protocol
from tianluo.server.state import ServerState

FLOW = "20260819-104630_764d8485"
MACHINE = "m1"

DISCOVERY_FILE = "01_discovery_764d8485.jsonl"
DISCOVERY_ID = "01_discovery_764d8485"
PLAN_FILE = "05_plan_764d8485.jsonl"
PLAN_ID = "05_plan_764d8485"
CONFIRM_FILE = "06_confirm_764d8485.jsonl"
CONFIRM_ID = "06_confirm_764d8485"

# The live shape: a byte-capped full HEAD carried only the first 44 discovery
# records while the bundle already held 148.
HEAD_RECORDS = 44
DISCOVERY_TOTAL = 100
PLAN_TOTAL = 30
CONFIRM_TOTAL = 18


def _record(step_id: str, ordinal: int, content: str = "") -> dict:
    return {
        "step_id": step_id,
        "step_type": step_id.split("_")[1],
        "ordinal": ordinal,
        "message": {
            "role": "assistant",
            "content": content or f"{step_id} line {ordinal}",
        },
    }


def _usage_record(step_id: str, ordinal: int) -> dict:
    """A record carrying a modern per-call usage entry."""
    rec = _record(step_id, ordinal)
    rec["message"]["usage_records"] = [
        {
            "call_id": f"{step_id}:{ordinal}",
            "input_tokens": 100,
            "output_tokens": 10,
            "model": "claude-opus-5",
        }
    ]
    return rec


def _discovery(n: int) -> list:
    return [_record(DISCOVERY_ID, i) for i in range(n)]


def _plan(n: int) -> list:
    return [_record(PLAN_ID, i) for i in range(n)]


def _confirm(n: int) -> list:
    return [_record(CONFIRM_ID, i) for i in range(n)]


def _full_bundle_records() -> list:
    return _discovery(DISCOVERY_TOTAL) + _plan(PLAN_TOTAL) + _confirm(CONFIRM_TOTAL)


def _full_cursor() -> dict:
    return {
        DISCOVERY_FILE: DISCOVERY_TOTAL,
        PLAN_FILE: PLAN_TOTAL,
        CONFIRM_FILE: CONFIRM_TOTAL,
    }


async def _active_worktree_flow(state: ServerState, status: str = "running") -> None:
    """Make FLOW a live ``--worktree`` flow of MACHINE."""
    await state.update_status(
        MACHINE,
        {
            "machine_id": MACHINE,
            "flows": [
                {
                    "flow_id": FLOW,
                    "project_root": "/repo/tianluo/worktrees/wt-764d8485",
                    "status": status,
                }
            ],
        },
    )


async def _full(state: ServerState, records, cursor, **kw):
    return await state.apply_history_frame(
        FLOW, protocol.HISTORY_MODE_FULL, records,
        cursor=cursor, machine_id=MACHINE, **kw,
    )


async def _append(state: ServerState, records, cursor, cursor_base=None, **kw):
    return await state.apply_history_frame(
        FLOW, protocol.HISTORY_MODE_APPEND, records,
        cursor=cursor, cursor_base=cursor_base, machine_id=MACHINE, **kw,
    )


async def _seed(state: ServerState) -> None:
    """Establish the ~148-record bundle of a live worktree flow."""
    await _active_worktree_flow(state)
    await _full(state, _full_bundle_records(), _full_cursor())


async def _replay_rejected_drain(state: ServerState) -> None:
    """One self-heal window: a refused short full HEAD + overlapping tails.

    Exactly what the daemon's ``_handle_history_request`` puts on the wire for a
    flow whose history exceeds ``MAX_BYTES_PER_REPORT``: a ``full`` HEAD holding
    only what fit, then ``append`` tails for the remainder — each declaring the
    line window it covers, all of it BELOW the bundle's water mark because the
    daemon re-read the flow from line 0.
    """
    # The HEAD: 44 discovery records. Shorter than the cached bundle, so the
    # shrinking-full guard keeps the bundle and overwrites only the cursor.
    outcome = await _full(
        state, _discovery(HEAD_RECORDS), {DISCOVERY_FILE: HEAD_RECORDS}
    )
    assert outcome.rejected_full is True
    assert outcome.resolves_pull is True

    # The tails, each overlapping the (now rewound) water mark.
    await _append(
        state,
        _discovery(DISCOVERY_TOTAL)[HEAD_RECORDS:],
        {DISCOVERY_FILE: DISCOVERY_TOTAL},
        cursor_base={DISCOVERY_FILE: HEAD_RECORDS},
    )
    await _append(
        state,
        _plan(PLAN_TOTAL),
        {DISCOVERY_FILE: DISCOVERY_TOTAL, PLAN_FILE: PLAN_TOTAL},
        cursor_base={PLAN_FILE: 0},
    )
    await _append(
        state,
        _confirm(CONFIRM_TOTAL),
        _full_cursor(),
        cursor_base={CONFIRM_FILE: 0},
    )


def _keys(records) -> list:
    return [(r["step_id"], r["ordinal"]) for r in records]


# --------------------------------------------------------------------------
# (1) the live defect: N repeated rejected-full + overlapping-append drains
# --------------------------------------------------------------------------


def test_repeated_selfheal_drain_leaves_the_bundle_unchanged():
    async def scenario():
        state = ServerState()
        await _seed(state)

        baseline = await state.get_history(FLOW)
        assert len(baseline["records"]) == 148
        baseline_keys = _keys(baseline["records"])
        assert len(set(baseline_keys)) == len(baseline_keys)

        first = await state.get_history_snapshot(FLOW)
        token = first["progress"]
        signature = first["signature"]
        assert len(first["records"]) == 148

        # Five windows of the exact drain that grew the bundle by 104 records
        # per round in production.
        for _ in range(5):
            await _replay_rejected_drain(state)

            bundle = await state.get_history(FLOW)
            assert len(bundle["records"]) == 148, "bundle grew on a re-drain"
            assert _keys(bundle["records"]) == baseline_keys, "order/identity moved"
            assert bundle["generation"] == baseline["generation"]

            meta = await state.get_history_bundle_meta(FLOW)
            assert meta["total"] == 148
            assert meta["signature"] == signature

            # A client that synced before the drains stays in sync: no records
            # to re-fetch, no DOM rebuild.
            reply = await state.get_history_snapshot(
                FLOW, after=token, known_signature=signature
            )
            assert reply["delivery"] == "not_modified"
            assert reply["records"] == []

        # A fresh client's full delivery carries each record exactly once.
        fresh = await state.get_history_snapshot(FLOW)
        assert fresh["delivery"] == "full"
        assert len(fresh["records"]) == 148
        assert len(set(_keys(fresh["records"]))) == 148
        assert fresh["signature"] == signature

    asyncio.run(scenario())


def test_a_genuinely_new_tail_still_appends_after_repeated_drains():
    """Idempotence must not turn into inertia: real new records still land."""

    async def scenario():
        state = ServerState()
        await _seed(state)
        for _ in range(3):
            await _replay_rejected_drain(state)

        outcome = await _append(
            state,
            [_record(CONFIRM_ID, CONFIRM_TOTAL), _record(CONFIRM_ID, CONFIRM_TOTAL + 1)],
            {
                DISCOVERY_FILE: DISCOVERY_TOTAL,
                PLAN_FILE: PLAN_TOTAL,
                CONFIRM_FILE: CONFIRM_TOTAL + 2,
            },
            cursor_base={CONFIRM_FILE: CONFIRM_TOTAL},
        )
        assert outcome.resolves_pull is True

        bundle = await state.get_history(FLOW)
        assert len(bundle["records"]) == 150
        assert _keys(bundle["records"])[-2:] == [
            (CONFIRM_ID, CONFIRM_TOTAL),
            (CONFIRM_ID, CONFIRM_TOTAL + 1),
        ]

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# (2) same (step_id, ordinal), new content — an in-place update
# --------------------------------------------------------------------------


def test_changed_content_for_a_held_ordinal_updates_in_place():
    async def scenario():
        state = ServerState()
        await _seed(state)
        before = await state.get_history(FLOW)
        generation = before["generation"]
        keys = _keys(before["records"])
        signature = (await state.get_history_bundle_meta(FLOW))["signature"]

        # A retried FAILED step rewrote its jsonl line in place: the same
        # physical line, new content.
        rewritten = _record(PLAN_ID, 3, content="plan attempt 2 — rewritten")
        outcome = await _append(
            state,
            [rewritten],
            _full_cursor(),
            cursor_base={PLAN_FILE: 3},
        )
        assert outcome.resolves_pull is True

        bundle = await state.get_history(FLOW)
        # Length and order untouched — so the signature and every outstanding
        # progress-token offset still mean what they meant.
        assert len(bundle["records"]) == 148
        assert _keys(bundle["records"]) == keys
        assert bundle["generation"] == generation
        assert (await state.get_history_bundle_meta(FLOW))["signature"] == signature

        at = keys.index((PLAN_ID, 3))
        assert bundle["records"][at]["message"]["content"] == (
            "plan attempt 2 — rewritten"
        )

        # Re-delivering the SAME rewritten record is a no-op.
        await _append(state, [rewritten], _full_cursor(), cursor_base={PLAN_FILE: 3})
        assert len((await state.get_history(FLOW))["records"]) == 148

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# (3) legacy records carrying no ordinal / no step_id keep the old behaviour
# --------------------------------------------------------------------------


def test_records_without_an_ordinal_are_appended_verbatim():
    """An un-numbered record's identity cannot be proven, so it is never folded."""

    async def scenario():
        state = ServerState()
        await _seed(state)

        legacy = {
            "step_id": CONFIRM_ID,
            "step_type": "confirm",
            "message": {"role": "assistant", "content": "pre-ordinal daemon line"},
        }
        no_step = {
            "step_type": "confirm",
            "ordinal": 0,
            "message": {"role": "assistant", "content": "no step id"},
        }
        cursor = dict(_full_cursor(), **{CONFIRM_FILE: CONFIRM_TOTAL + 2})
        await _append(
            state,
            [legacy, no_step, dict(legacy), dict(no_step)],
            cursor,
            cursor_base={CONFIRM_FILE: CONFIRM_TOTAL},
        )

        bundle = await state.get_history(FLOW)
        # All four land: the pair of un-numbered clones is NOT de-duped here
        # (the frontend's snapshot guard is where a clone is collapsed).
        assert len(bundle["records"]) == 152
        assert [r["message"]["content"] for r in bundle["records"][-4:]] == [
            "pre-ordinal daemon line",
            "no step id",
            "pre-ordinal daemon line",
            "no step id",
        ]

        # ...and a numbered record still folds normally alongside them.
        await _append(state, _confirm(CONFIRM_TOTAL), cursor, cursor_base={CONFIRM_FILE: 0})
        assert len((await state.get_history(FLOW))["records"]) == 152

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# (4) usage does not inflate with the number of re-drains
# --------------------------------------------------------------------------


def test_usage_summary_does_not_grow_with_repeated_usage_bearing_appends():
    async def scenario():
        state = ServerState()
        await _active_worktree_flow(state)
        await _full(
            state,
            _discovery(4) + [_usage_record(PLAN_ID, 0), _usage_record(PLAN_ID, 1)],
            {DISCOVERY_FILE: 4, PLAN_FILE: 2},
        )

        tail = [_usage_record(PLAN_ID, 2), _usage_record(PLAN_ID, 3)]
        cursor = {DISCOVERY_FILE: 4, PLAN_FILE: 4}
        await _append(state, tail, cursor, cursor_base={PLAN_FILE: 2})

        after_first = await state.get_history_usage(FLOW)
        assert after_first is not None
        total = after_first["summary"]["totals"]["logical_input_tokens"]
        calls = len(after_first["calls"])
        # The per-step source cache is what a re-delivered append EXTENDS, so
        # its record count is the sharpest witness of the double-count.
        sources = after_first["steps"][PLAN_ID]["record_count"]

        # The self-heal re-drains the SAME usage-bearing tail three more times.
        for _ in range(3):
            await _append(state, tail, cursor, cursor_base={PLAN_FILE: 2})

        after_redrains = await state.get_history_usage(FLOW)
        assert after_redrains["summary"]["totals"]["logical_input_tokens"] == total
        assert len(after_redrains["calls"]) == calls
        assert after_redrains["steps"][PLAN_ID]["record_count"] == sources
        assert len((await state.get_history(FLOW))["records"]) == 8

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# (5) the bundle's private key index never leaks onto the wire
# --------------------------------------------------------------------------


def test_key_index_is_never_serialized_into_a_payload():
    async def scenario():
        state = ServerState()
        await _seed(state)
        await _replay_rejected_drain(state)

        payloads = [
            await state.get_history(FLOW),
            await state.get_history_bundle_meta(FLOW),
            await state.get_history_snapshot(FLOW),
        ]
        for payload in payloads:
            assert "_key_index" not in payload
            assert "_key_index_len" not in payload
            assert "_usage_sources" not in payload

    asyncio.run(scenario())
