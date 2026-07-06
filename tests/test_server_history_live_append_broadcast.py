"""Regression tests for the ``MSG_HISTORY_DATA`` broadcast-suppression rule
(symptom A of the running-flow live-chat bug, #193's other half).

The bug: after a ``respond`` / ``interject`` the daemon pushes the new
conversation records as a live ``mode: append`` increment. The frontend, in the
same window, may concurrently fire a cache-miss REST pull whose waiter is then
resolved by that very append. The old ``ws.py`` suppressed the ``/ws/ui``
broadcast for *any* frame that resolved a pull waiter, so the append silently
stopped reaching already-subscribed live views — the running flow froze and only
recovered on a full re-enter that re-pulled the snapshot.

The fix narrows the suppression to a resolved ``mode: full`` cache-miss reply
(whose records the REST response itself delivers, and whose re-broadcast would
clear every consumer's progress token). A ``mode: append`` increment is always
broadcast — even when it happens to resolve a waiter — because it carries the
real-time delta every subscriber needs; the REST-initiating client de-dupes the
overlap via ``dedupeAppendRecords``.

These tests drive ``_handle_message`` directly with a real ``UiHub`` and a real
``HistoryRequestRegistry`` and assert on what reaches the UI socket.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from se3.daemon import protocol
from se3.daemon.history import DaemonHistoryReader
from se3.server.state import ServerState
from se3.server.ws import (
    ConnectionManager,
    HistoryRequestRegistry,
    UiHub,
    _handle_message,
)


class _UiWS:
    """Minimal UI WebSocket stand-in capturing decoded frames it is sent."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def history_frames(self, flow_id: str | None = None) -> list:
        frames = [m for m in self.sent if m.get("type") == "history_data"]
        if flow_id is not None:
            frames = [m for m in frames if m.get("flow_id") == flow_id]
        return frames


async def _setup(owner_id: str = "owner-A"):
    """Return ``(state, hub, ui, registry)`` with one machine + UI client."""
    state = ServerState()
    await state.register_machine("m1", "host", "9.9.9", owner_id=owner_id)
    hub = UiHub()
    ui = _UiWS()
    await hub.register(ui, owner_id)
    registry = HistoryRequestRegistry()
    return state, hub, ui, registry


def _history_msg(flow_id: str, mode: str, records: list, cursor: dict | None = None):
    return protocol.make_history_data(flow_id, mode, records, cursor=cursor or {})


# --------------------------------------------------------------------------
# mode: full — a resolved cache-miss pull reply is suppressed
# --------------------------------------------------------------------------


def test_full_pull_reply_resolving_waiter_is_not_broadcast():
    async def scenario():
        state, hub, ui, registry = await _setup()
        # A REST handler parked a waiter for the cache-miss pull.
        fut = registry.register("f1", machine_id="m1")

        msg = _history_msg(
            "f1", protocol.HISTORY_MODE_FULL, [{"step": "s1", "line": 1}]
        )
        await _handle_message(msg, state, "m1", hub, registry)

        # The waiter is resolved (the REST handler will return the records),
        # and the full frame is NOT re-broadcast to /ws/ui (token protection).
        assert fut.done()
        assert ui.history_frames("f1") == []

    asyncio.run(scenario())


def test_unsolicited_full_without_waiter_is_broadcast():
    """A live ``mode: full`` replacement that resolves no waiter still streams."""

    async def scenario():
        state, hub, ui, registry = await _setup()

        msg = _history_msg(
            "f1", protocol.HISTORY_MODE_FULL, [{"step": "s1", "line": 1}]
        )
        await _handle_message(msg, state, "m1", hub, registry)

        frames = ui.history_frames("f1")
        assert len(frames) == 1
        assert frames[0]["mode"] == protocol.HISTORY_MODE_FULL

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# mode: append — always broadcast, even when it resolves a waiter
# --------------------------------------------------------------------------


def test_append_resolving_waiter_is_still_broadcast():
    """The core regression: a respond/interject append that races a REST pull.

    The append resolves the pull waiter (so the REST handler returns) AND is
    broadcast to every subscribed /ws/ui client (so the live view keeps
    appending without a re-enter).
    """

    async def scenario():
        state, hub, ui, registry = await _setup()
        # Establish the authoritative full bundle first so the append applies.
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step": "s1", "line": 1}],
            cursor={"s1": 1},
            machine_id="m1",
        )
        # A REST pull races the live append and parks a waiter.
        fut = registry.register("f1", machine_id="m1")

        msg = _history_msg(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step": "s1", "line": 2}],
            cursor={"s1": 2},
        )
        await _handle_message(msg, state, "m1", hub, registry)

        # Both happen: waiter resolved AND the append broadcast.
        assert fut.done()
        frames = ui.history_frames("f1")
        assert len(frames) == 1
        assert frames[0]["mode"] == protocol.HISTORY_MODE_APPEND
        assert frames[0]["records"] == [{"step": "s1", "line": 2}]

    asyncio.run(scenario())


def test_live_append_without_waiter_is_broadcast():
    """An ordinary active-flow append (no pull waiter) streams unchanged."""

    async def scenario():
        state, hub, ui, registry = await _setup()
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step": "s1", "line": 1}],
            cursor={"s1": 1},
            machine_id="m1",
        )

        msg = _history_msg(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step": "s1", "line": 2}],
            cursor={"s1": 2},
        )
        await _handle_message(msg, state, "m1", hub, registry)

        frames = ui.history_frames("f1")
        assert len(frames) == 1
        assert frames[0]["mode"] == protocol.HISTORY_MODE_APPEND

    asyncio.run(scenario())


def test_consecutive_appends_after_respond_keep_broadcasting():
    """After respond: the agent's follow-up turns keep streaming, no re-enter.

    Each successive append — including ones that resolve a stray pull waiter —
    reaches the live view, so the conversation continues to grow.
    """

    async def scenario():
        state, hub, ui, registry = await _setup()
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step": "s1", "line": 1}],
            cursor={"s1": 1},
            machine_id="m1",
        )

        # Round 1: the respond echo append also resolves a racing REST pull.
        registry.register("f1", machine_id="m1")
        await _handle_message(
            _history_msg(
                "f1", protocol.HISTORY_MODE_APPEND, [{"step": "s1", "line": 2}]
            ),
            state,
            "m1",
            hub,
            registry,
        )
        # Round 2 & 3: the agent's auto-produced follow-up turns (no waiter).
        await _handle_message(
            _history_msg(
                "f1", protocol.HISTORY_MODE_APPEND, [{"step": "s1", "line": 3}]
            ),
            state,
            "m1",
            hub,
            registry,
        )
        await _handle_message(
            _history_msg(
                "f1", protocol.HISTORY_MODE_APPEND, [{"step": "s1", "line": 4}]
            ),
            state,
            "m1",
            hub,
            registry,
        )

        frames = ui.history_frames("f1")
        # All three appends streamed to the live view.
        assert len(frames) == 3
        lines = [f["records"][0]["line"] for f in frames]
        assert lines == [2, 3, 4]
        assert all(f["mode"] == protocol.HISTORY_MODE_APPEND for f in frames)

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Owner scoping is preserved through the append broadcast path
# --------------------------------------------------------------------------


def test_append_broadcast_is_owner_scoped():
    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "9.9.9", owner_id="owner-A")
        hub = UiHub()
        mine = _UiWS()
        other = _UiWS()
        await hub.register(mine, "owner-A")
        await hub.register(other, "owner-B")
        registry = HistoryRequestRegistry()

        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step": "s1", "line": 1}],
            cursor={"s1": 1},
            machine_id="m1",
        )
        registry.register("f1", machine_id="m1")
        await _handle_message(
            _history_msg(
                "f1", protocol.HISTORY_MODE_APPEND, [{"step": "s1", "line": 2}]
            ),
            state,
            "m1",
            hub,
            registry,
        )
        return mine, other

    mine, other = asyncio.run(scenario())
    assert len(mine.history_frames("f1")) == 1
    assert other.history_frames("f1") == []


# --------------------------------------------------------------------------
# state.py append/snapshot consistency (task 2 acceptance)
# --------------------------------------------------------------------------


def test_appends_after_bundle_keep_applied_and_stable_generation():
    """Successive appends on an existing bundle apply and keep generation stable."""

    async def scenario():
        state = ServerState()
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step": "s1", "line": 1}],
            cursor={"s1": 1},
            machine_id="m1",
        )
        gen0 = (await state.get_history("f1"))["generation"]

        applied2 = await state.append_history(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step": "s1", "line": 2}],
            cursor={"s1": 2},
            machine_id="m1",
        )
        applied3 = await state.append_history(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step": "s1", "line": 3}],
            cursor={"s1": 3},
            machine_id="m1",
        )
        bundle = await state.get_history("f1")
        return applied2, applied3, gen0, bundle

    applied2, applied3, gen0, bundle = asyncio.run(scenario())
    assert applied2 is True
    assert applied3 is True
    # Generation unchanged across appends — outstanding progress tokens stay
    # valid, so the snapshot the broadcast and the REST reply see is consistent.
    assert bundle["generation"] == gen0
    assert [r["line"] for r in bundle["records"]] == [1, 2, 3]

    async def snap_scenario():
        # The full snapshot the REST reply serves contains every appended
        # record, matching what was broadcast incrementally (no drift / loss).
        state = ServerState()
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step": "s1", "line": 1}],
            cursor={"s1": 1},
            machine_id="m1",
        )
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step": "s1", "line": 2}],
            machine_id="m1",
        )
        return await state.get_history_snapshot("f1", expected_machine_id="m1")

    snap = asyncio.run(snap_scenario())
    assert snap is not None
    assert snap["delivery"] == "full"
    assert [r["line"] for r in snap["records"]] == [1, 2]


# ==========================================================================
# G4 — end-to-end console-consistency bridge:
#       daemon incremental read  →  server cache + /ws/ui broadcast
#       →  (golden fixture)  →  frontend node-stub consume
#
# This locks the long-standing running-flow *freeze* regression end-to-end, for
# BOTH triggering scenarios (discovery→analyze confirmation transition, and a
# step failure → manual retry). It exercises the REAL daemon ``DaemonHistoryReader``
# over a REAL on-disk ``se3/history/<flow>/<step>.jsonl`` + ``engine.json``
# evolution, feeds every incremental ``FlowRead`` delta through the REAL server
# ``_handle_message`` (cache write + ``/ws/ui`` broadcast), and asserts that the
# records broadcast to a subscribed live console — with NO ``mode: full`` reload —
# equal the authoritative full ``GET /api/history`` snapshot (no loss / no dup /
# no freeze).
#
# The exact broadcast frame sequence and the full snapshot are then frozen into a
# committed golden fixture (``tests/frontend/fixtures/console_e2e_frames.json``)
# that the frontend node-stub test (``live_append_e2e_consistency.test.mjs``)
# replays through the production ``app.js`` ``applyHistoryData`` consumer, so the
# SAME daemon-produced bytes prove the incremental render path converges on the
# full-reload render path. Regenerate the golden file with
# ``SE3_REGEN_GOLDEN=1 pytest tests/test_server_history_live_append_broadcast.py``.
# --------------------------------------------------------------------------

_GOLDEN_FIXTURE = (
    Path(__file__).resolve().parent
    / "frontend"
    / "fixtures"
    / "console_e2e_frames.json"
)


def _write_jsonl(path: Path, lines: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _append_jsonl(path: Path, lines: list) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _write_engine(root: Path, flow_id: str, status: str) -> None:
    state_dir = root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": flow_id, "status": status}), encoding="utf-8"
    )


def _hist(root: Path, flow_id: str) -> Path:
    return root / "se3" / "history" / flow_id


# ---- on-disk jsonl line builders (mirror what chat_history writes) ---------


def _chat(role: str, content: str, ts: int) -> dict:
    return {"role": role, "content": content, "timestamp": ts}


def _started(step_id: str, step_type: str, ts: int) -> dict:
    return {
        "type": "step_started",
        "step_id": step_id,
        "step_type": step_type,
        "status": "running",
        "timestamp": ts,
    }


def _status(step_id: str, step_type: str, status: str, ts: int) -> dict:
    return {
        "type": "step_status",
        "step_id": step_id,
        "step_type": step_type,
        "status": status,
        "timestamp": ts,
    }


def _completed(step_id: str, step_type: str, ts: int) -> dict:
    return {
        "type": "step_completed",
        "step_id": step_id,
        "step_type": step_type,
        "data": {
            "step": {
                "step_id": step_id,
                "step_type": step_type,
                "status": "completed",
                "outputs": {},
            }
        },
        "timestamp": ts,
    }


def _failed(step_id: str, step_type: str, ts: int, err: str = "spec gate failed") -> dict:
    return {
        "type": "step_failed",
        "step_id": step_id,
        "step_type": step_type,
        "data": {
            "step": {
                "step_id": step_id,
                "step_type": step_type,
                "status": "failed",
                "error_message": err,
            }
        },
        "timestamp": ts,
    }


# ---- scenario scripts: a list of disk mutations, one read per mutation -----


def _transition_mutations(root: Path, flow_id: str):
    """discovery runs multiple confirm rounds, COMPLETES, then analyze starts."""
    disc = _hist(root, flow_id) / "01_discovery_ab12.jsonl"
    anal = _hist(root, flow_id) / "02_analyze_cd34.jsonl"
    D = "01_discovery_ab12"
    A = "02_analyze_cd34"

    def m0():
        _write_engine(root, flow_id, "RUNNING")
        _write_jsonl(
            disc,
            [
                _started(D, "discovery", 1),
                _chat("assistant", "Round 1 — which option?", 2),
            ],
        )

    def m1():  # pause to await the round-1 answer
        _append_jsonl(disc, [_status(D, "discovery", "paused", 3)])
        _write_engine(root, flow_id, "PAUSED")

    def m2():  # operator answers "1"; resume (same wall-clock second)
        _append_jsonl(disc, [_chat("user", "1", 3), _started(D, "discovery", 3)])
        _write_engine(root, flow_id, "RUNNING")

    def m3():
        _append_jsonl(disc, [_chat("assistant", "Round 2 — confirm the plan?", 4)])

    def m4():  # pause again
        _append_jsonl(disc, [_status(D, "discovery", "paused", 5)])
        _write_engine(root, flow_id, "PAUSED")

    def m5():  # operator confirms "按1确定"; resume (same second, distinct text)
        _append_jsonl(disc, [_chat("user", "按1确定", 5), _started(D, "discovery", 5)])
        _write_engine(root, flow_id, "RUNNING")

    def m6():  # *** THE TRANSITION: discovery COMPLETES + analyze starts ***
        _append_jsonl(disc, [_completed(D, "discovery", 6)])
        _write_jsonl(
            anal,
            [
                _started(A, "analyze", 7),
                _chat("assistant", "Analyzing the spec…", 8),
            ],
        )

    def m7():  # analyze keeps producing
        _append_jsonl(anal, [_chat("assistant", "Analysis complete.", 9)])

    return [m0, m1, m2, m3, m4, m5, m6, m7]


def _retry_mutations(root: Path, flow_id: str):
    """update_spec runs, FAILS, the operator retries, and it re-runs to success."""
    step = _hist(root, flow_id) / "06_update_spec_9f3a.jsonl"
    S = "06_update_spec_9f3a"

    def m0():
        _write_engine(root, flow_id, "RUNNING")
        _write_jsonl(
            step,
            [
                _started(S, "update_spec", 1),
                _chat("assistant", "Drafting the spec update…", 2),
            ],
        )

    def m1():  # the attempt FAILS
        _append_jsonl(step, [_failed(S, "update_spec", 3)])

    def m2():  # operator retries → retrying status + re-run running (same second)
        _append_jsonl(
            step,
            [_status(S, "update_spec", "retrying", 4), _started(S, "update_spec", 4)],
        )

    def m3():  # the retry re-emits SIMILAR content (later ts → distinct)
        _append_jsonl(step, [_chat("assistant", "Drafting the spec update…", 5)])

    def m4():  # the retry succeeds
        _append_jsonl(step, [_chat("assistant", "Spec update applied.", 6)])

    return [m0, m1, m2, m3, m4]


async def _drive_scenario(root: Path, flow_id: str, mutations) -> dict:
    """Drive the real daemon reader → real server broadcast; capture frames.

    Returns ``{"flow_id", "frames": [{mode, records}], "snapshot": [records]}``
    where ``frames`` are exactly what reaches a subscribed ``/ws/ui`` client and
    ``snapshot`` is the authoritative full ``GET /api/history`` record list.
    """
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(root)])
    state = ServerState()
    await state.register_machine("m1", "host", "1.0", owner_id="owner-A")
    hub = UiHub()
    ui = _UiWS()
    await hub.register(ui, "owner-A")
    registry = HistoryRequestRegistry()

    cursors: dict = {}
    for mutate in mutations:
        mutate()
        reads = reader.read_active_flows(cursors)
        cursors = {r.flow_id: r.cursor for r in reads}
        for r in reads:
            if not r.records:
                continue
            msg = protocol.make_history_data(
                r.flow_id, r.mode, r.records, cursor=r.cursor
            )
            await _handle_message(msg, state, "m1", hub, registry)

    snap = await state.get_history_snapshot(flow_id, expected_machine_id="m1")
    frames = [
        {"mode": f["mode"], "records": f["records"]}
        for f in ui.history_frames(flow_id)
    ]
    return {"flow_id": flow_id, "frames": frames, "snapshot": snap["records"]}


def _generate_all(tmp_path: Path) -> dict:
    transition = asyncio.run(
        _drive_scenario(
            tmp_path / "transition", "live", _transition_mutations(tmp_path / "transition", "live")
        )
    )
    retry = asyncio.run(
        _drive_scenario(
            tmp_path / "retry", "live", _retry_mutations(tmp_path / "retry", "live")
        )
    )
    return {"transition": transition, "retry": retry}


def _flat_records(frames: list) -> list:
    out: list = []
    for f in frames:
        out.extend(f["records"])
    return out


# --------------------------------------------------------------------------
# daemon → server invariants (the back half of the e2e bridge)
# --------------------------------------------------------------------------


def test_transition_broadcast_equals_full_snapshot_no_loss_no_dup(tmp_path):
    scenario = asyncio.run(
        _drive_scenario(
            tmp_path, "live", _transition_mutations(tmp_path, "live")
        )
    )
    # First broadcast is the initial full snapshot; the rest are live appends.
    assert scenario["frames"][0]["mode"] == protocol.HISTORY_MODE_FULL
    assert all(
        f["mode"] == protocol.HISTORY_MODE_APPEND for f in scenario["frames"][1:]
    )
    # The concatenation of every broadcast frame == the authoritative full
    # snapshot, in order, with nothing lost and nothing delivered twice.
    streamed = _flat_records(scenario["frames"])
    assert streamed == scenario["snapshot"]
    # No record body appears twice across the live stream (no dup).
    keyed = [
        (r["step_id"], json.dumps(r["message"], sort_keys=True, ensure_ascii=False))
        for r in streamed
    ]
    assert len(keyed) == len(set(keyed)), "a record was broadcast twice"


def test_transition_analyze_arrives_as_live_delta_not_swallowed(tmp_path):
    """The freeze symptom would be: nothing post-confirmation reaches /ws/ui.

    Prove the analyze (post-transition) records arrive in a LIVE append frame
    AFTER the discovery rounds — the daemon→server side never swallows the
    transition batch.
    """
    scenario = asyncio.run(
        _drive_scenario(
            tmp_path, "live", _transition_mutations(tmp_path, "live")
        )
    )
    append_records = _flat_records(scenario["frames"][1:])  # live deltas only
    bodies = [r["message"].get("content") for r in append_records]
    assert "Analyzing the spec…" in bodies
    assert "Analysis complete." in bodies
    # The analyze records carry the file-name-derived authoritative step_type.
    analyze = [r for r in append_records if r["step_type"] == "analyze"]
    assert analyze, "analyze step records reached the live broadcast"


def test_retry_broadcast_equals_full_snapshot_no_loss_no_dup(tmp_path):
    scenario = asyncio.run(
        _drive_scenario(tmp_path, "live", _retry_mutations(tmp_path, "live"))
    )
    assert scenario["frames"][0]["mode"] == protocol.HISTORY_MODE_FULL
    streamed = _flat_records(scenario["frames"])
    assert streamed == scenario["snapshot"]
    # The retry success turn arrives as a live delta (no reload needed).
    append_bodies = [
        r["message"].get("content") for r in _flat_records(scenario["frames"][1:])
    ]
    assert "Spec update applied." in append_bodies
    # The similar-looking retry draft is delivered too — not mistaken for a dup.
    assert append_bodies.count("Drafting the spec update…") >= 1


# --------------------------------------------------------------------------
# golden fixture: the wire between this daemon→server test and the frontend
# node-stub consumer test (live_append_e2e_consistency.test.mjs)
# --------------------------------------------------------------------------


def test_e2e_frames_match_committed_golden_fixture(tmp_path):
    """The committed golden fixture stays in lock-step with the real reader/server.

    Set ``SE3_REGEN_GOLDEN=1`` to regenerate the fixture after an intentional
    change to the daemon record shape or the scenario scripts.
    """
    generated = _generate_all(tmp_path)

    if os.environ.get("SE3_REGEN_GOLDEN"):
        _GOLDEN_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN_FIXTURE.write_text(
            json.dumps(generated, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    assert _GOLDEN_FIXTURE.exists(), (
        "golden fixture missing — regenerate with SE3_REGEN_GOLDEN=1"
    )
    committed = json.loads(_GOLDEN_FIXTURE.read_text(encoding="utf-8"))
    assert generated == committed, (
        "daemon→server e2e frames drifted from the committed golden fixture; "
        "regenerate with SE3_REGEN_GOLDEN=1 and re-run the frontend node-stub test"
    )


# ==========================================================================
# G3 — server relay robustness: the ``requires_full`` stuck-state self-heal.
#
# When a live ``mode: append`` frame lands with no authoritative bundle to
# extend (a first sighting after a server restart, or a cross-machine/version
# desync), ``append_history`` discards it and flags the flow ``requires_full``.
# Historically the flow then FROZE: the daemon push loop only ever sends
# ``append`` frames, so every later increment was dropped until a ``full`` frame
# arrived — which only happened when the user exited and re-entered the chat and
# its REST cache-miss pull fetched one. That is the "must re-enter" persistence
# the user reports at the discovery→analyze boundary.
#
# The fix: the receive loop asks the owning daemon (over the exact socket the
# frame arrived on) for one cursorless — hence ``full`` — pull the FIRST time a
# flow is stuck. Its reply repopulates the bundle and clears the flag, so
# subsequent appends flow again with no manual re-enter. These tests prove the
# marker fires once per stuck flow, clears on the healing full frame, and — end
# to end over the REAL daemon reader — recovers a first-sighting freeze while a
# healthy boundary never triggers a recovery at all (no regression).
# --------------------------------------------------------------------------


def test_take_recovery_pull_fires_once_per_stuck_flow():
    async def scenario():
        state = ServerState()
        # A first-sighting append flags the flow requires_full (discarded).
        applied = await state.append_history(
            "f1", protocol.HISTORY_MODE_APPEND, [{"line": 1}], machine_id="m1"
        )
        assert applied is False
        first = await state.take_recovery_pull("f1")
        second = await state.take_recovery_pull("f1")
        # A flow that never went stuck never arms a recovery.
        untouched = await state.take_recovery_pull("other")
        return first, second, untouched

    first, second, untouched = asyncio.run(scenario())
    assert first is True       # first stuck-flow consult arms exactly one pull
    assert second is False     # deduped while the pull is in flight (no storm)
    assert untouched is False  # a healthy flow never arms a recovery


def test_recovery_marker_cleared_when_full_frame_heals_bundle():
    async def scenario():
        state = ServerState()
        await state.append_history(
            "f1", protocol.HISTORY_MODE_APPEND, [{"line": 1}], machine_id="m1"
        )
        assert await state.take_recovery_pull("f1") is True
        # The recovery's full reply repopulates the bundle and clears both the
        # requires_full flag and the in-flight recovery marker.
        healed = await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}], machine_id="m1"
        )
        assert healed is True
        return state._history_requires_full, state._history_recovery_inflight

    requires_full, recovery_inflight = asyncio.run(scenario())
    assert "f1" not in requires_full
    assert "f1" not in recovery_inflight


def test_clear_recovery_pull_re_arms_after_failed_send():
    async def scenario():
        state = ServerState()
        await state.append_history(
            "f1", protocol.HISTORY_MODE_APPEND, [{"line": 1}], machine_id="m1"
        )
        assert await state.take_recovery_pull("f1") is True
        # The recovery send failed (daemon vanished) — release the marker so a
        # later append can re-arm a fresh recovery instead of wedging.
        await state.clear_recovery_pull("f1")
        return await state.take_recovery_pull("f1")

    assert asyncio.run(scenario()) is True


class _RecordingDaemonWS:
    """Fake daemon socket capturing the ``MSG_HISTORY_REQUEST`` frames sent it.

    The server's self-heal dispatches a ``MSG_HISTORY_REQUEST`` down the daemon
    socket; this stand-in records each so a test can assert on how many recovery
    pulls fired and then feed back the daemon's ``full`` reply.
    """

    def __init__(self) -> None:
        self.requests: list = []

    async def send_text(self, data: str) -> None:
        msg = protocol.decode(data)
        if msg.type == protocol.MSG_HISTORY_REQUEST:
            self.requests.append(msg.payload)


def test_first_sighting_append_dispatches_one_recovery_pull_then_heals():
    """A first-sighting append self-heals: one recovery pull, then appends flow.

    Without the fix the flow stays frozen (every append discarded) until a full
    frame arrives via a manual re-enter. Here the server asks the daemon for one
    full pull; its reply repopulates the bundle and the next live append applies
    and broadcasts — no re-enter.
    """

    async def scenario():
        state, hub, ui, registry = await _setup()
        manager = ConnectionManager()
        daemon = _RecordingDaemonWS()
        await manager.connect("m1", daemon)

        def append(line):
            return _history_msg(
                "f1",
                protocol.HISTORY_MODE_APPEND,
                [{"step": "s1", "line": line}],
                cursor={"s1": line},
            )

        # Server restarted mid-flow: a live append lands with no cached bundle.
        await _handle_message(
            append(5), state, "m1", hub, registry, manager=manager, connection=daemon
        )
        # Exactly one self-heal MSG_HISTORY_REQUEST was dispatched.
        assert len(daemon.requests) == 1
        # A second discarded append while the pull is in flight does NOT fan out
        # another request (no per-cycle storm).
        await _handle_message(
            append(6), state, "m1", hub, registry, manager=manager, connection=daemon
        )
        assert len(daemon.requests) == 1

        # The daemon answers with the authoritative full snapshot.
        await _handle_message(
            _history_msg(
                "f1",
                protocol.HISTORY_MODE_FULL,
                [{"step": "s1", "line": 5}, {"step": "s1", "line": 6}],
                cursor={"s1": 6},
            ),
            state,
            "m1",
            hub,
            registry,
            manager=manager,
            connection=daemon,
        )
        # A subsequent live append now APPLIES (bundle healed) and broadcasts.
        await _handle_message(
            append(7), state, "m1", hub, registry, manager=manager, connection=daemon
        )
        bundle = await state.get_history("f1")
        return ui, bundle

    ui, bundle = asyncio.run(scenario())
    frames = ui.history_frames("f1")
    modes = [f["mode"] for f in frames]
    # The recovery full reply reached the live view, and the post-heal append
    # streamed after it (the last frame the live view saw).
    assert protocol.HISTORY_MODE_FULL in modes
    assert modes[-1] == protocol.HISTORY_MODE_APPEND
    assert frames[-1]["records"] == [{"step": "s1", "line": 7}]
    # The healed bundle carries the recovered snapshot plus the live append —
    # exactly what a fresh REST full pull would serve, no re-enter needed.
    assert [r["line"] for r in bundle["records"]] == [5, 6, 7]


def test_recovery_pull_skipped_when_no_daemon_connection():
    """With no connection manager the handler degrades — no crash, no recovery.

    Unit tests drive ``_handle_message`` without a manager; the discarded append
    must simply not attempt a recovery (and the flow stays flagged so a later
    manager-backed call can heal it).
    """

    async def scenario():
        state, hub, ui, registry = await _setup()
        await _handle_message(
            _history_msg(
                "f1", protocol.HISTORY_MODE_APPEND, [{"step": "s1", "line": 1}]
            ),
            state,
            "m1",
            hub,
            registry,
        )
        # The flow is flagged, but no recovery could be dispatched (no manager);
        # the marker was not consumed, so a manager-backed retry can still arm.
        return "f1" in state._history_requires_full, "f1" in state._history_recovery_inflight

    flagged, in_flight = asyncio.run(scenario())
    assert flagged is True
    assert in_flight is False


async def _drive_server_restart_midflow(root, flow_id, mutations, restart_before):
    """Drive the real daemon reader with a server restart mid-flow.

    Phase 1 runs the daemon while the server is DOWN, advancing the daemon's
    read cursor across ``mutations[:restart_before]`` but relaying nothing. Phase
    2 brings the server back with an EMPTY cache: the daemon kept its cursor, so
    its next relayed frame is an ``append`` with no bundle to anchor — the
    first-sighting stuck-state. Each self-heal ``MSG_HISTORY_REQUEST`` the server
    dispatches is answered by reading the flow's authoritative full snapshot
    (cursorless), exactly as the owning daemon would.

    Returns ``{"frames", "snapshot", "recovery_pulls"}``.
    """
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(root)])
    manager = ConnectionManager()
    daemon = _RecordingDaemonWS()
    await manager.connect("m1", daemon)

    cursors: dict = {}
    # Phase 1 — server DOWN: advance the daemon cursor, relay nothing.
    for mutate in mutations[:restart_before]:
        mutate()
        reads = reader.read_active_flows(cursors)
        cursors = {r.flow_id: r.cursor for r in reads}

    # Phase 2 — server restarts with an empty cache.
    state = ServerState()
    await state.register_machine("m1", "host", "1.0", owner_id="owner-A")
    hub = UiHub()
    ui = _UiWS()
    await hub.register(ui, "owner-A")
    registry = HistoryRequestRegistry()
    recovery_pulls = 0

    async def feed(read):
        await _handle_message(
            protocol.make_history_data(
                read.flow_id, read.mode, read.records, cursor=read.cursor
            ),
            state,
            "m1",
            hub,
            registry,
            manager=manager,
            connection=daemon,
        )

    for mutate in mutations[restart_before:]:
        mutate()
        reads = reader.read_active_flows(cursors)
        cursors = {r.flow_id: r.cursor for r in reads}
        for r in reads:
            if r.records:
                await feed(r)
        # The owning daemon answers each dispatched self-heal pull by reading the
        # authoritative full snapshot. Draining here (no disk write in between)
        # keeps the push cursor aligned with the recovered end, so the following
        # appends continue cleanly with no overlap.
        while daemon.requests:
            daemon.requests.pop(0)
            recovery_pulls += 1
            full = reader.read_flow(flow_id, project_root=str(root), cursor=None)
            await feed(full)

    snap = await state.get_history_snapshot(flow_id, expected_machine_id="m1")
    frames = [
        {"mode": f["mode"], "records": f["records"]}
        for f in ui.history_frames(flow_id)
    ]
    return {
        "frames": frames,
        "snapshot": snap["records"],
        "recovery_pulls": recovery_pulls,
    }


def test_restart_at_discovery_analyze_boundary_self_heals_no_reenter(tmp_path):
    """A server restart across the discovery→analyze boundary recovers itself.

    The first post-restart frame is an ``append`` (first sighting) — the exact
    freeze the user reports. Assert: exactly one recovery pull fires, a full
    frame reaches the live view, and the recovered snapshot plus the following
    live appends converge on the authoritative full history (no loss, no dup, no
    manual re-enter).
    """
    scenario = asyncio.run(
        _drive_server_restart_midflow(
            tmp_path,
            "live",
            _transition_mutations(tmp_path, "live"),
            restart_before=6,  # restart right before the discovery→analyze m6
        )
    )
    # Exactly one self-heal pull recovered the first-sighting freeze.
    assert scenario["recovery_pulls"] == 1
    frames = scenario["frames"]
    full_idxs = [
        i for i, f in enumerate(frames) if f["mode"] == protocol.HISTORY_MODE_FULL
    ]
    assert full_idxs, "the recovery full frame must reach the live view"
    # From the recovery full onward, the streamed records equal the authoritative
    # snapshot — the freeze healed and analyze continued live without a re-enter.
    converged = _flat_records(frames[full_idxs[-1]:])
    assert converged == scenario["snapshot"]
    # The post-transition analyze content arrived live after the recovery.
    bodies = [r["message"].get("content") for r in _flat_records(frames[full_idxs[-1]:])]
    assert "Analysis complete." in bodies


def test_healthy_boundary_never_triggers_recovery_pull(tmp_path):
    """No regression: a healthy boundary (bundle established first) self-heals 0×.

    With no restart the first frame is a ``full`` snapshot, so the flow never
    lands in requires_full and no recovery pull is ever dispatched — the healthy
    path stays exactly as before, and the stream still equals the full snapshot.
    """
    scenario = asyncio.run(
        _drive_server_restart_midflow(
            tmp_path,
            "live",
            _transition_mutations(tmp_path, "live"),
            restart_before=0,  # no server downtime → first frame is mode: full
        )
    )
    assert scenario["recovery_pulls"] == 0
    frames = scenario["frames"]
    assert frames[0]["mode"] == protocol.HISTORY_MODE_FULL
    assert all(f["mode"] == protocol.HISTORY_MODE_APPEND for f in frames[1:])
    # The full incremental stream equals the authoritative snapshot (no loss/dup).
    assert _flat_records(frames) == scenario["snapshot"]
