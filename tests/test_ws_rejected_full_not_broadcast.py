"""#287: a full frame the cache REJECTS must not be relayed to ``/ws/ui`` either.

``ServerState.apply_history_frame`` refuses an ACTIVE WORKTREE flow's same-machine
``full`` snapshot that carries FEWER records than the cached bundle (an unresolved
/ partially resolved history directory on the daemon side — the paused-worktree
reconcile's failure mode). An ordinary flow is exempt: it may shrink legitimately
(a failed step retried with a shorter batch), so its frames apply and broadcast
exactly as before. Keeping the truncated records out of the CACHE is only half the
defence:
the daemon receive loop also fans the raw frame out to every subscribed console,
and a WS ``mode: full`` push rebuilds the chat pane wholesale. Relaying a
rejected frame would therefore blank the later rounds out of every open console
until the next REST poll restored them from the intact bundle.

These tests pin both halves: the state layer must REPORT the rejection
(``rejected_full``), and the WS handler must act on it by suppressing the
broadcast — while a legitimate, growing full still reaches the browsers.
"""

from __future__ import annotations

import asyncio

from se3.daemon import protocol
from se3.server.state import ServerState
from se3.server.ws import _handle_message


class RecordingHub:
    """Minimal UiHub stand-in that records every broadcast for assertions."""

    def __init__(self, clients: int = 1):
        self._n = clients
        self.owned = []
        self.scoped = []

    @property
    def client_count(self) -> int:
        return self._n

    def distinct_owners(self):
        return {None}

    async def broadcast_scoped(self, payload_by_owner):
        self.scoped.append(payload_by_owner)

    async def broadcast_owned(self, payload, owner):
        self.owned.append((payload, owner))


ROUND_1 = [{"line": 1, "message": {"content": "round 1"}}]
ROUND_1_2 = ROUND_1 + [{"line": 2, "message": {"content": "round 2"}}]


def _history_pushes(hub: RecordingHub):
    return [
        payload
        for payload, _owner in hub.owned
        if payload.get("type") == "history_data"
    ]


async def _register_flow(state, *, status: str, project_root: str, flow_id="f1"):
    """Register *flow_id* on machine ``m1``.

    The rejection is scoped to flows the worktree self-heal reconcile can fire
    for, so which flow the frame belongs to decides whether it is refused at all
    — these tests must therefore say.
    """
    await state.register_machine("m1", owner_id=None)
    await state.update_status(
        "m1",
        {
            "machine_id": "m1",
            "flows": [
                {
                    "flow_id": flow_id,
                    "status": status,
                    "project_root": project_root,
                }
            ],
        },
    )


async def _paused_worktree_flow(state, flow_id="f1"):
    await _register_flow(
        state,
        status="paused",
        project_root="/repo/se3/worktrees/wt-a",
        flow_id=flow_id,
    )


def test_apply_history_frame_flags_a_rejected_shrinking_full():
    """The write outcome distinguishes "refused" from "applied".

    Both resolve a parked pull waiter (the daemon answered; the cache is
    authoritative either way), so only the explicit ``rejected_full`` flag can
    tell the fan-out that the frame's records are untrustworthy.
    """
    state = ServerState()

    async def scenario():
        await _paused_worktree_flow(state)
        seeded = await state.apply_history_frame(
            "f1", protocol.HISTORY_MODE_FULL, ROUND_1_2, machine_id="m1"
        )
        assert seeded.resolves_pull is True
        assert seeded.rejected_full is False

        # A shorter same-machine full — the partially-resolved daemon read.
        shrinking = await state.apply_history_frame(
            "f1", protocol.HISTORY_MODE_FULL, ROUND_1, machine_id="m1"
        )
        assert shrinking.rejected_full is True
        # Still resolves the waiter: the REST handler reads the (intact) cache.
        assert shrinking.resolves_pull is True

        # The degenerate case — an empty full — is a rejection too.
        empty = await state.apply_history_frame(
            "f1", protocol.HISTORY_MODE_FULL, [], machine_id="m1"
        )
        assert empty.rejected_full is True

        # An identical full is a benign no-op, NOT a rejection: its records match
        # the bundle, so relaying them changes nothing.
        same = await state.apply_history_frame(
            "f1", protocol.HISTORY_MODE_FULL, ROUND_1_2, machine_id="m1"
        )
        assert same.rejected_full is False

        # The cache never lost round 2 through any of the above.
        bundle = await state.get_history("f1")
        assert len(bundle["records"]) == 2

    asyncio.run(scenario())


def test_rejected_shrinking_full_is_not_broadcast_to_ui():
    """The exact #287 fan-out leak: a truncated full must reach NO console.

    The daemon's late/first-sighting cursorless read returns only round 1 while
    the server already holds rounds 1+2. No pull waiter is parked (the REST pull
    timed out, or this is the push loop), so nothing else suppresses the frame.
    """
    state = ServerState()
    hub = RecordingHub()

    async def scenario():
        await _paused_worktree_flow(state)
        await state.apply_history_frame(
            "f1", protocol.HISTORY_MODE_FULL, ROUND_1_2, machine_id="m1"
        )
        hub.owned.clear()

        await _handle_message(
            protocol.make_history_data("f1", protocol.HISTORY_MODE_FULL, ROUND_1),
            state,
            "m1",
            hub,
        )

        # No browser is handed the truncated conversation…
        assert _history_pushes(hub) == []
        # …and the cache still holds both rounds for the next REST poll.
        bundle = await state.get_history("f1")
        assert [r["line"] for r in bundle["records"]] == [1, 2]

    asyncio.run(scenario())


def test_shrinking_full_for_a_non_worktree_flow_is_still_broadcast():
    """An ordinary flow's fan-out is untouched by #287 — shrink included.

    Only an active worktree flow can be truncated by a mis-resolved daemon read;
    an ordinary flow shrinks legitimately (a failed step retried, rewriting its
    jsonl with a shorter batch). Suppressing that frame would strand every open
    console on the stale pre-retry records until the next REST poll, so it must
    be applied and relayed exactly as it was before the fix.
    """
    state = ServerState()
    hub = RecordingHub()

    async def scenario():
        await _register_flow(state, status="running", project_root="/repo")
        await state.apply_history_frame(
            "f1", protocol.HISTORY_MODE_FULL, ROUND_1_2, machine_id="m1"
        )
        hub.owned.clear()

        await _handle_message(
            protocol.make_history_data("f1", protocol.HISTORY_MODE_FULL, ROUND_1),
            state,
            "m1",
            hub,
        )

        pushes = _history_pushes(hub)
        assert len(pushes) == 1
        assert [r["line"] for r in pushes[0]["records"]] == [1]
        assert [r["line"] for r in (await state.get_history("f1"))["records"]] == [1]

    asyncio.run(scenario())


def test_growing_full_still_broadcasts_to_ui():
    """The self-heal path stays live: a full that ADDS records is relayed.

    Suppressing rejected fulls must not suppress the reconcile frame that
    carries the missing round — that delivery is the whole point of the widened
    worktree self-heal.
    """
    state = ServerState()
    hub = RecordingHub()

    async def scenario():
        await state.register_machine("m1", owner_id=None)
        await state.apply_history_frame(
            "f1", protocol.HISTORY_MODE_FULL, ROUND_1, machine_id="m1"
        )
        hub.owned.clear()

        await _handle_message(
            protocol.make_history_data("f1", protocol.HISTORY_MODE_FULL, ROUND_1_2),
            state,
            "m1",
            hub,
        )

        pushes = _history_pushes(hub)
        assert len(pushes) == 1
        assert pushes[0]["mode"] == protocol.HISTORY_MODE_FULL
        assert [r["line"] for r in pushes[0]["records"]] == [1, 2]
        bundle = await state.get_history("f1")
        assert [r["line"] for r in bundle["records"]] == [1, 2]

    asyncio.run(scenario())
