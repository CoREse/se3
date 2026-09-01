"""A history delivery cut in the middle must be detectable AND repairable.

The defect these tests pin down is the SECOND line of defence behind the relay
fix (``test_history_relay_backpressure.py``): whatever cuts a multi-frame
history delivery — a keepalive close, a daemon restart, a link drop — the server
used to be left holding a self-consistent PREFIX of the conversation and no way
to know it. ``ServerState.apply_history_frame`` derives a bundle's cursor,
signature and pending window from the frames that ACTUALLY arrived, never from
what the sender set out to deliver, so a reply that stopped at frame 38 of 147
produced a bundle whose cursor named exactly the 26 step files it held, whose
``pending`` window was empty, and against which the browser's ``stepId#ordinal``
self-check found no hole. Every later poll answered ``not_modified``, the daemon
kept its own delivery cursor across the reconnect and never re-sent, and the one
re-pull branch that could have fixed it was gated on ``is_active_worktree_flow``
— so a COMPLETED flow, which is exactly the kind whose open triggers a 147-frame
pull, could never reconcile. The commit/summarize tail was gone for good.

Two mechanisms are asserted here, and they are independent:

* COMPLETENESS IS DECLARED, not inferred. ``HISTORY_DATA`` now carries a
  ``final`` bit (``not read.truncated`` on both of the daemon's multi-frame
  paths), and while a delivery has not declared itself finished the bundle
  reports ``incomplete`` — the one field of a snapshot that is not derived from
  the records, hence the only one that can contradict a truncated bundle's own
  self-consistency. A daemon too old to send the bit degrades to the
  pre-existing chunk-bound estimate, which still covers a pull reply.
* THE REPAIR REACHES A COMPLETED FLOW. The poll's reconcile branch now also
  fires for a bundle with an interrupted delivery, whatever kind of flow it
  belongs to, and pulls an INCREMENTAL backfill anchored at the server's own
  water mark so repeated repairs converge instead of re-drained-from-zero
  oscillating.

The load is the real ``20260831-095750_23865927`` (52 steps / 4324 records / 147
frames), through the same committed shape fixture the relay tests use.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from _authsrv import authed_app, authed_hello, login
from tianluo.daemon import protocol
from tianluo.server import ws as ws_module
from tianluo.server.state import ServerState
from tianluo.server.ws import (
    ConnectionManager,
    HistoryRequestRegistry,
    UiHub,
    _serve_loop,
)

MACHINE = "m1"
OWNER = "owner-A"
PROJECT_ROOT = "/tmp/interrupted-drain-repo"
FLOW = "20260831-095750_23865927"

#: Same scaled-down frame budget the relay tests use: what the server reads off
#: a frame is whether it REACHED the daemon's bound, and scaling the bound and
#: the padding together preserves that verdict for all 147 frames at ~2.4 MB.
_CHUNK_BYTES = 16 * 1024

#: The frame the drain is cut after. Deep enough into the reply that the prefix
#: is a plausible-looking conversation (the shape the live incident left behind
#: was 3435 of 4324 records) and far enough from the end that the tail cannot be
#: mistaken for a rounding difference.
_CUT_AFTER = 38

_SHAPE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures"
        / "history_relay_shape_20260831.json"
    ).read_text(encoding="utf-8")
)


def _flat_records():
    """Every record of the sample flow, in the order the daemon reads them."""
    records = []
    for step in _SHAPE["steps"]:
        step_id = step["step_id"]
        step_type = step_id.split("_", 1)[1].rsplit("_", 1)[0]
        for ordinal in range(step["records"]):
            records.append(
                {
                    "step_id": step_id,
                    "step_type": step_type,
                    "ordinal": ordinal,
                    "message": {
                        "role": "assistant",
                        "content": "record %s#%d" % (step_id, ordinal),
                    },
                }
            )
    return records


def _frames():
    """The 147 wire frames of one pull reply, cursors and completeness bits.

    Every frame but the last is padded to REACH ``_CHUNK_BYTES`` and declares
    ``final=False`` — the two independent ways the wire says "more of this reply
    is coming", so the same fixture drives both the current daemon and the
    old-daemon degradation.
    """
    records = _flat_records()
    frames = []
    cursor = {}
    index = 0
    for position, count in enumerate(_SHAPE["frames"]):
        chunk = records[index:index + count]
        index += count
        last = position == len(_SHAPE["frames"]) - 1
        if chunk and not last:
            chunk[0] = dict(
                chunk[0],
                message=dict(chunk[0]["message"], content="x" * _CHUNK_BYTES),
            )
        base = {
            rec["step_id"] + ".jsonl": cursor.get(rec["step_id"] + ".jsonl", 0)
            for rec in chunk
        }
        for rec in chunk:
            cursor[rec["step_id"] + ".jsonl"] = rec["ordinal"] + 1
        frames.append(
            {
                "mode": (
                    protocol.HISTORY_MODE_FULL
                    if not frames
                    else protocol.HISTORY_MODE_APPEND
                ),
                "records": chunk,
                "cursor": dict(cursor),
                "cursor_base": base,
                "final": last,
            }
        )
    assert len(frames) == 147
    assert sum(len(f["records"]) for f in frames) == 4324
    return frames


def _encode(frame, *, flow_id=FLOW, declare_final=True):
    """One wire frame; *declare_final* off models a pre-``final`` daemon."""
    return protocol.make_history_data(
        flow_id,
        frame["mode"],
        frame["records"],
        cursor=frame["cursor"],
        cursor_base=frame["cursor_base"],
        final=frame["final"] if declare_final else None,
    ).to_json()


@pytest.fixture()
def scaled_chunk_bound(monkeypatch):
    """Scale the daemon's frame budget down with the fixture's record bodies."""
    monkeypatch.setattr(ws_module, "MAX_BYTES_PER_REPORT", _CHUNK_BYTES)
    return _CHUNK_BYTES


class _CuttableLeg:
    """A daemon socket that hands over *frames* and then dies, as a close does."""

    def __init__(self, texts):
        self._texts = list(texts)
        self._index = 0

    async def receive_text(self):
        if self._index >= len(self._texts):
            # What a 1011 keepalive close looks like to the receive loop: the
            # next read raises and the loop unwinds mid-reply.
            raise RuntimeError("connection closed")
        text = self._texts[self._index]
        self._index += 1
        return text

    async def send_text(self, text):
        return None

    async def close(self):
        return None


async def _feed(state, texts):
    """Replay *texts* through the real daemon receive loop, then let it die."""
    leg = _CuttableLeg(texts)
    manager = ConnectionManager()
    hub = UiHub()
    registry = HistoryRequestRegistry()
    await manager.connect(MACHINE, leg)
    await _serve_loop(leg, manager, state, MACHINE, hub, registry)
    return manager


async def _state_with_pull(*, cursor=None):
    """A state with one dispatched pull armed for the sample flow."""
    state = ServerState()
    await state.register_machine(MACHINE, "host", "1.2.3", owner_id=OWNER)
    await state.mark_history_replay(FLOW, cursor=cursor)
    return state


# --------------------------------------------------------------------------
# a cut delivery is detectable
# --------------------------------------------------------------------------


def test_cut_drain_leaves_a_prefix_that_admits_it_is_one(scaled_chunk_bound):
    """The truncated bundle is self-consistent — and still says it is partial.

    The first four assertions are the DEFECT, reproduced: nothing derived from
    the delivered records can see the loss. The last one is the fix.
    """
    frames = _frames()

    async def scenario():
        state = await _state_with_pull()
        await _feed(state, [_encode(f) for f in frames[:_CUT_AFTER]])
        bundle = await state.get_history(FLOW, touch=False)
        snapshot = await state.get_history_snapshot(FLOW)
        resync = await state.get_history_snapshot(
            FLOW,
            after=snapshot["progress"],
            known_signature=snapshot["signature"],
        )
        return bundle, snapshot, resync

    bundle, snapshot, resync = asyncio.run(
        asyncio.wait_for(scenario(), timeout=120)
    )

    # A prefix landed, not the conversation.
    held = len(bundle["records"])
    assert 0 < held < 4324
    assert bundle["records"][-1]["step_id"] != "52_summarize_6201ea5c"
    # …and it is internally flawless: the cursor declares exactly the step files
    # that arrived and nothing is pending, so a client's own self-check finds no
    # hole to report and the poll answers "you are in sync".
    assert sum(bundle["cursor"].values()) == held
    assert snapshot["pending"] == {}
    assert resync["delivery"] == "not_modified"
    # The one field that is NOT derived from the records contradicts all of it.
    assert snapshot["incomplete"] is True
    assert resync["incomplete"] is True


def test_a_completed_delivery_is_settled(scaled_chunk_bound):
    """The flag is not vacuous: the whole reply clears it, mid-reply does not."""
    frames = _frames()

    async def scenario():
        state = await _state_with_pull()
        await _feed(state, [_encode(f) for f in frames[:_CUT_AFTER]])
        midway = (await state.get_history_snapshot(FLOW))["incomplete"]
        # A fresh pull whose reply runs to its declared end.
        await state.mark_history_replay(FLOW, cursor=None)
        await _feed(state, [_encode(f) for f in frames])
        settled = await state.get_history_snapshot(FLOW)
        return midway, settled

    midway, settled = asyncio.run(asyncio.wait_for(scenario(), timeout=180))

    assert midway is True
    assert settled["incomplete"] is False
    assert len(settled["records"]) == 4324
    assert settled["records"][-1]["step_id"] == "52_summarize_6201ea5c"


def test_push_and_poll_agree_about_completeness(scaled_chunk_bound):
    """The WS advisory carries the same bit the REST snapshot does.

    A console that hears about the bundle through a pushed cursor advisory must
    not be told a different story from one that polled — the two paths share one
    source of truth for ``cursor`` / ``pending`` and now for this too.
    """
    frames = _frames()

    async def scenario():
        state = await _state_with_pull()
        await _feed(state, [_encode(f) for f in frames[:_CUT_AFTER]])
        meta = await state.get_history_bundle_meta(FLOW)
        snapshot = await state.get_history_snapshot(FLOW)
        return meta, snapshot

    meta, snapshot = asyncio.run(asyncio.wait_for(scenario(), timeout=120))
    assert meta["incomplete"] is snapshot["incomplete"] is True
    assert meta["cursor"] == snapshot["cursor"]


def test_old_daemon_without_the_bit_still_detects_a_cut_reply(
    scaled_chunk_bound,
):
    """The declared degradation: a pre-``final`` daemon keeps the estimate.

    Its frames say nothing, so the chunk-bound heuristic — "a frame AT the
    daemon's bound has more coming" — remains the signal for a pull reply, and a
    reply cut mid-drain is still flagged. What such a daemon loses is only the
    sharper statement, never the pre-existing behaviour.
    """
    frames = _frames()

    async def scenario():
        state = await _state_with_pull()
        await _feed(
            state,
            [_encode(f, declare_final=False) for f in frames[:_CUT_AFTER]],
        )
        cut = (await state.get_history_snapshot(FLOW))["incomplete"]
        await state.mark_history_replay(FLOW, cursor=None)
        await _feed(state, [_encode(f, declare_final=False) for f in frames])
        whole = await state.get_history_snapshot(FLOW)
        return cut, whole

    cut, whole = asyncio.run(asyncio.wait_for(scenario(), timeout=180))
    assert cut is True
    assert whole["incomplete"] is False
    assert len(whole["records"]) == 4324


def test_a_live_append_does_not_settle_a_drain_it_raced(scaled_chunk_bound):
    """A push frame that overtook a pull's dispatch speaks only for itself.

    The daemon pauses its push loop once a drain STARTS, so an append emitted in
    the dispatch→drain-start window arrives BEFORE the reply's head — carrying
    ``final=True``, because that one-frame delivery IS complete. It must not be
    allowed to declare the reply it raced finished.
    """
    frames = _frames()
    head = frames[0]

    async def scenario():
        state = await _state_with_pull()
        # First establish a bundle, then arm a pull and let a live append race
        # its dispatch.
        await _feed(state, [_encode(f) for f in frames[:2]])
        await state.mark_history_replay(FLOW, cursor=None)
        racer = {
            "mode": protocol.HISTORY_MODE_APPEND,
            "records": [
                {
                    "step_id": head["records"][0]["step_id"],
                    "step_type": "discovery",
                    "ordinal": 9000,
                    "message": {"role": "assistant", "content": "live tail"},
                }
            ],
            "cursor": dict(frames[1]["cursor"]),
            "cursor_base": dict(frames[1]["cursor"]),
            "final": True,
        }
        # Frame 2 of the reply is chunk-bounded and declares final=False.
        await _feed(state, [_encode(frames[2]), _encode(racer)])
        return (await state.get_history_snapshot(FLOW))["incomplete"]

    assert asyncio.run(asyncio.wait_for(scenario(), timeout=120)) is True


# --------------------------------------------------------------------------
# a cut delivery is repairable
# --------------------------------------------------------------------------


def test_a_later_live_append_cannot_settle_a_dead_delivery(scaled_chunk_bound):
    """A one-frame append is complete in itself — and says nothing about the hole.

    After the reconnect the daemon's push loop resumes with ordinary
    (untruncated, hence ``final=True``) appends. Letting one of those clear the
    marker would hand back a bundle calling itself whole while the tail of the
    dead reply is still missing.
    """
    frames = _frames()

    async def scenario():
        state = await _state_with_pull()
        await _feed(state, [_encode(f) for f in frames[:_CUT_AFTER]])
        live = {
            "mode": protocol.HISTORY_MODE_APPEND,
            "records": [
                {
                    "step_id": "01_discovery_ffd5c452",
                    "step_type": "discovery",
                    "ordinal": 9001,
                    "message": {"role": "assistant", "content": "live tail"},
                }
            ],
            "cursor": dict(frames[_CUT_AFTER - 1]["cursor"]),
            "cursor_base": dict(frames[_CUT_AFTER - 1]["cursor"]),
            "final": True,
        }
        await _feed(state, [_encode(live)])
        return (
            await state.history_delivery_incomplete(FLOW),
            await state.history_delivery_repair_due(FLOW),
        )

    incomplete, due = asyncio.run(asyncio.wait_for(scenario(), timeout=120))
    assert incomplete is True
    assert due is True


def test_socket_death_arms_an_incremental_repair(scaled_chunk_bound):
    """The disconnect is the detection point, and the plan is add-only.

    A repair that re-drains from zero re-runs the risk that broke the bundle: a
    second interruption would leave a SHORTER prefix. Anchoring at the server's
    own water mark makes the bundle monotonic across any number of failed
    repairs.
    """
    frames = _frames()

    async def scenario():
        state = await _state_with_pull()
        await _feed(state, [_encode(f) for f in frames[:_CUT_AFTER]])
        water_mark = (await state.get_history(FLOW, touch=False))["cursor"]
        due = await state.history_delivery_repair_due(FLOW)
        plan = await state.plan_recovery_pull(FLOW, MACHINE, repair=True)
        # At most one repair per flow is in flight, so an immediately following
        # poll must not stack a second pull on it.
        again = await state.plan_recovery_pull(FLOW, MACHINE, repair=True)
        return water_mark, due, plan, again

    water_mark, due, plan, again = asyncio.run(
        asyncio.wait_for(scenario(), timeout=120)
    )
    assert due is True
    assert plan is not None
    kind, cursor = plan
    assert kind == "incremental"
    assert cursor == water_mark
    assert again is None


def test_a_delivery_still_arriving_is_not_repaired(scaled_chunk_bound):
    """No rival pull against the drain that is already filling the bundle."""
    frames = _frames()

    async def scenario():
        state = await _state_with_pull()
        leg = _CuttableLeg([_encode(f) for f in frames[:_CUT_AFTER]])
        manager = ConnectionManager()
        hub = UiHub()
        registry = HistoryRequestRegistry()
        await manager.connect(MACHINE, leg)
        # Drive the receive loop far enough to be mid-reply, but do NOT let the
        # socket die: the delivery is unfinished and alive.
        loop_task = asyncio.ensure_future(
            _serve_loop(leg, manager, state, MACHINE, hub, registry)
        )
        for _ in range(200):
            await asyncio.sleep(0)
            if (await state.get_history(FLOW, touch=False)) is not None:
                break
        incomplete = await state.history_delivery_incomplete(FLOW)
        due = await state.history_delivery_repair_due(FLOW)
        await loop_task
        return incomplete, due

    incomplete, due = asyncio.run(asyncio.wait_for(scenario(), timeout=120))
    assert incomplete is True
    assert due is False, "a live drain was mistaken for an interrupted one"


def test_a_quiet_delivery_repairs_after_the_grace(scaled_chunk_bound):
    """The time-based backstop for an interruption no disconnect reported."""
    frames = _frames()

    async def scenario():
        state = await _state_with_pull()
        leg = _CuttableLeg([_encode(f) for f in frames[:_CUT_AFTER]])
        manager = ConnectionManager()
        hub = UiHub()
        registry = HistoryRequestRegistry()
        await manager.connect(MACHINE, leg)
        loop_task = asyncio.ensure_future(
            _serve_loop(leg, manager, state, MACHINE, hub, registry)
        )
        for _ in range(200):
            await asyncio.sleep(0)
            if (await state.get_history(FLOW, touch=False)) is not None:
                break
        # Age the delivery past the grace without a disconnect: a half-open
        # socket that simply stops delivering, which no close frame reports.
        state._history_deliveries[FLOW].last_frame_at -= (
            ServerState._HISTORY_DELIVERY_STALL_GRACE + 1
        )
        due = await state.history_delivery_repair_due(FLOW)
        await loop_task
        return due

    assert asyncio.run(asyncio.wait_for(scenario(), timeout=120)) is True


def test_asking_whether_to_repair_does_not_freeze_a_live_flow(
    scaled_chunk_bound,
):
    """The decision is a query; only a dispatched repair arms the latch.

    ``requires_full`` makes every live append be DISCARDED until a rebuild
    lands, so arming it from the poll's gate — which may then dispatch nothing
    at all (no connected daemon, a repair already in flight) — would freeze a
    still-running flow for a whole recovery TTL. The arm therefore lives inside
    the plan, and the plan is only returned when a pull follows it.
    """
    frames = _frames()

    async def scenario():
        state = await _state_with_pull()
        await _feed(state, [_encode(f) for f in frames[:_CUT_AFTER]])
        assert await state.history_delivery_repair_due(FLOW) is True
        # Asked and answered, but nobody pulled: the next live append must still
        # be applied rather than discarded behind a latch.
        held = len((await state.get_history(FLOW, touch=False))["records"])
        await _feed(state, [_encode(frames[_CUT_AFTER])])
        return held, len((await state.get_history(FLOW, touch=False))["records"])

    before, after = asyncio.run(asyncio.wait_for(scenario(), timeout=120))
    assert after > before, "a mere repair-due query latched the flow shut"


def test_an_evicted_flow_is_not_repaired(scaled_chunk_bound):
    """The budget's decision wins: no repair for a bundle it just dropped.

    Repairing an evicted flow would re-pull exactly the conversation the budget
    refused to hold — the eviction⇄回拉 storm the cold marker exists to stop.
    """
    frames = _frames()

    async def scenario():
        state = ServerState(history_cache_budget_bytes=0)
        await state.register_machine(MACHINE, "host", "1.2.3", owner_id=OWNER)
        await state.mark_history_replay(FLOW, cursor=None)
        await _feed(state, [_encode(f) for f in frames[:_CUT_AFTER]])
        await state.sweep_history_cache()
        return (
            await state.history_delivery_incomplete(FLOW),
            await state.history_delivery_repair_due(FLOW),
        )

    incomplete, due = asyncio.run(asyncio.wait_for(scenario(), timeout=120))
    assert incomplete is False
    assert due is False


# --------------------------------------------------------------------------
# end to end: a COMPLETED flow repairs itself through the poll
# --------------------------------------------------------------------------


class _InterruptibleDaemon:
    """A daemon that cuts its first reply and answers the repair from a cursor.

    It models the incident exactly: the first ``HISTORY_REQUEST`` is answered
    with a PREFIX of the reply and then the socket dies (as the 1011 keepalive
    close did); the daemon reconnects and, when the server asks again — this
    time with the server's own water mark — serves the remainder as ``append``
    frames anchored there.
    """

    def __init__(self, client, app, frames, cut_after):
        self._client = client
        self._app = app
        self._frames = frames
        self._cut_after = cut_after
        self.requests = []
        self.cut = threading.Event()
        self.repaired = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._ctx = None
        self.sock = None
        self._connect()

    def _connect(self):
        self._ctx = self._client.websocket_connect("/ws")
        self.sock = self._ctx.__enter__()
        self.sock.send_text(authed_hello(self._app, MACHINE, "host", "12.14.0"))
        protocol.decode(self.sock.receive_text())  # WELCOME
        self.sock.send_text(
            protocol.make_status_update(
                {
                    "machine_id": MACHINE,
                    "hostname": "host",
                    "project_roots": [PROJECT_ROOT],
                    "flows": [
                        {
                            "flow_id": FLOW,
                            # COMPLETED, and not a worktree flow: the exact kind
                            # the old reconcile gate excluded.
                            "status": "completed",
                            "project_root": PROJECT_ROOT,
                        }
                    ],
                }
            ).to_json()
        )
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                msg = protocol.decode(self.sock.receive_text())
            except Exception:
                return
            if msg.type != protocol.MSG_HISTORY_REQUEST:
                continue
            cursor = msg.payload.get("cursor") or {}
            self.requests.append(cursor)
            if not self.cut.is_set():
                for frame in self._frames[:self._cut_after]:
                    self.sock.send_text(_encode(frame))
                self.cut.set()
                # Drop the socket mid-reply and come back, keeping our own
                # delivery cursor — so nothing is ever re-sent unasked.
                self._reconnect()
                return
            # The repair. The server asks from its own water mark, which is
            # exactly where the cut reply stopped, so the remaining frames are
            # contiguous appends.
            assert cursor == self._frames[self._cut_after - 1]["cursor"], (
                "the repair pull was not anchored at the server's water mark"
            )
            for frame in self._frames[self._cut_after:]:
                self.sock.send_text(_encode(frame))
            self.repaired.set()

    def _reconnect(self):
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass
        if not self._stop.is_set():
            self._connect()

    def close(self):
        self._stop.set()
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:
            pass


def test_completed_flow_repairs_its_cut_drain_through_the_poll(
    scaled_chunk_bound, monkeypatch
):
    """The user-visible acceptance: the tail comes back without a re-open.

    A COMPLETED flow gets no further appends, so the append-driven self-heal
    never fires for it and the poll is its ONLY repair trigger. Before the fix
    that trigger was gated on ``is_active_worktree_flow`` and this flow's
    conversation simply ended at ``34_test`` forever.
    """
    from fastapi.testclient import TestClient

    # The repair honours the same full-pull floor the cache-miss path uses; the
    # open a moment earlier already spent it, so shorten the window rather than
    # sleeping it out. The floor itself is asserted below.
    monkeypatch.setattr(
        ServerState, "_HISTORY_FULL_PULL_MIN_INTERVAL", 0.05
    )
    frames = _frames()
    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        daemon = _InterruptibleDaemon(client, app, frames, _CUT_AFTER)
        try:
            first = client.get(f"/api/history/{FLOW}")
            assert first.status_code == 200, first.text
            body = first.json()
            assert body["delivery"] == "full"
            assert daemon.cut.wait(timeout=60), "the drain never started"

            held = {
                (r["step_id"], r["ordinal"]) for r in body.get("records") or []
            }
            token, sig = body.get("progress"), body.get("signature")
            saw_incomplete = bool(body.get("incomplete"))
            deadline = time.time() + 90
            while len(held) < 4324 and time.time() < deadline:
                url = f"/api/history/{FLOW}"
                if token:
                    url += f"?after={token}&sig={sig}"
                poll = client.get(url)
                assert poll.status_code == 200, poll.text
                data = poll.json()
                saw_incomplete = saw_incomplete or bool(data.get("incomplete"))
                for record in data.get("records") or []:
                    held.add((record["step_id"], record["ordinal"]))
                token = data.get("progress") or token
                sig = data.get("signature") or sig
                time.sleep(0.05)

            # The bundle the browser can read reaches the last record of the
            # conversation — the commit/summarize tail the cut used to lose.
            assert len(held) == 4324, (
                "the conversation stopped short of its tail: %d records"
                % len(held)
            )
            assert ("52_summarize_6201ea5c", 26) in held
            assert saw_incomplete, "the truncated bundle never admitted it"
            assert daemon.repaired.is_set()
            # Exactly one repair beyond the opening pull: the throttle and the
            # at-most-one-recovery dedup must keep a 3 s poll from fanning out
            # one multi-MB pull per tick.
            assert len(daemon.requests) == 2, daemon.requests
            assert daemon.requests[0] == {}, "the open was not a full pull"
            assert daemon.requests[1], "the repair was not an incremental pull"

            # …and once settled, the flow stops asking: an ordinary completed
            # flow's idle poll costs exactly what it did before.
            settled = client.get(f"/api/history/{FLOW}?after={token}&sig={sig}")
            assert settled.json()["delivery"] == "not_modified"
            assert settled.json()["incomplete"] is False
            time.sleep(0.3)
            for _ in range(3):
                client.get(f"/api/history/{FLOW}?after={token}&sig={sig}")
                time.sleep(0.1)
            assert len(daemon.requests) == 2, (
                "a settled completed flow kept pulling from the daemon"
            )
        finally:
            daemon.close()


def test_a_missing_backfill_never_spends_the_repair_budget(
    scaled_chunk_bound, monkeypatch
):
    """A ``missing`` read is a targeted read of records the cache HOLDS.

    It is not a claim that the cache is behind the daemon, and letting it
    dispatch a pull would also spend the full-pull floor the genuine self-heal
    poll depends on — leaving the repair throttled out by the very requests that
    are not repairing anything.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        ServerState, "_HISTORY_FULL_PULL_MIN_INTERVAL", 0.05
    )
    frames = _frames()
    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        daemon = _InterruptibleDaemon(client, app, frames, _CUT_AFTER)
        try:
            first = client.get(f"/api/history/{FLOW}")
            assert first.status_code == 200, first.text
            assert daemon.cut.wait(timeout=60)
            body = first.json()
            token, sig = body.get("progress"), body.get("signature")
            # Let the daemon's reconnect land before measuring.
            time.sleep(0.5)
            before = len(daemon.requests)
            for _ in range(3):
                resp = client.get(
                    f"/api/history/{FLOW}"
                    f"?after={token}&sig={sig}&missing=01_discovery_ffd5c452:0"
                )
                assert resp.status_code == 200, resp.text
                assert resp.json()["delivery"] == "backfill"
                time.sleep(0.1)
            assert len(daemon.requests) == before, (
                "a missing-directed read dispatched a repair pull"
            )
        finally:
            daemon.close()
