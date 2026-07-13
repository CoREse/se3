"""Regression tests pinning the read-side authoritative-``project_root`` path.

A worktree-mode flow runs its discovery step in the main repo root (writing one
``01_discovery`` history file there) and every later step under the worktree
root, so the same ``flow_id`` ends up with a ``se3/history/<flow_id>/``
directory under TWO distinct roots. The daemon's legacy ``_resolve_flow_dir``
heuristic — scan every registered root and take the FIRST that contains the
flow's history dir — then returns the main repo's discovery-only directory, and
the WebUI freezes after the first step.

The fix makes ``SessionMeta.project_root`` the single source of truth: the
server resolves the flow's authoritative run root via
:meth:`ServerState.get_history_flow_project_root` and threads it through
``request_history`` → ``make_history_request`` so the daemon reads the right
root rather than guessing. These tests nail down that contract so a later
refactor cannot silently revert to the first-match heuristic.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from _authsrv import authed_hello
from se3.daemon import protocol
from se3.server.state import ServerState


# --------------------------------------------------------------------------
# ServerState.get_history_flow_project_root — unit
# --------------------------------------------------------------------------


def test_project_root_from_history_index_is_authoritative():
    """A history-index hit returns the flow's authoritative SessionMeta root."""
    state = ServerState()

    async def scenario():
        await state.update_history_index(
            "m1",
            [
                {"flow_id": "f1", "project_root": "/repo/se3/worktrees/wt-a"},
                {"flow_id": "f2", "project_root": "/other/repo"},
            ],
        )
        root = await state.get_history_flow_project_root("f1")
        assert root == "/repo/se3/worktrees/wt-a"

    asyncio.run(scenario())


def test_project_root_not_first_match_but_authoritative_for_flow():
    """With several roots registered, the authoritative root for THE flow wins.

    Two machines each report their own root; resolving ``f-wt`` must return the
    worktree root recorded for *that* flow, not whatever root happens to be
    enumerated first.
    """
    state = ServerState()

    async def scenario():
        # m-main is registered first and carries the discovery-only root, but it
        # does NOT own f-wt — the lookup must not return its root by position.
        await state.update_history_index(
            "m-main", [{"flow_id": "f-other", "project_root": "/repo"}]
        )
        await state.update_history_index(
            "m-wt",
            [{"flow_id": "f-wt", "project_root": "/repo/se3/worktrees/wt-a"}],
        )
        root = await state.get_history_flow_project_root("f-wt")
        assert root == "/repo/se3/worktrees/wt-a"

    asyncio.run(scenario())


def test_project_root_falls_back_to_live_flow_set():
    """An active-but-not-yet-indexed flow resolves via the live flow set."""
    state = ServerState()

    async def scenario():
        await state.update_status(
            "m1",
            {
                "machine_id": "m1",
                "flows": [
                    {"flow_id": "f-live", "project_root": "/repo/se3/worktrees/wt-b"}
                ],
            },
        )
        root = await state.get_history_flow_project_root("f-live")
        assert root == "/repo/se3/worktrees/wt-b"

    asyncio.run(scenario())


def test_project_root_index_preferred_over_live_flow():
    """The authoritative history index takes precedence over the live flow set."""
    state = ServerState()

    async def scenario():
        await state.update_history_index(
            "m1", [{"flow_id": "f1", "project_root": "/repo/se3/worktrees/wt"}]
        )
        await state.update_status(
            "m1",
            {
                "machine_id": "m1",
                "flows": [{"flow_id": "f1", "project_root": "/some/other/root"}],
            },
        )
        root = await state.get_history_flow_project_root("f1")
        assert root == "/repo/se3/worktrees/wt"

    asyncio.run(scenario())


def test_project_root_unknown_flow_returns_none():
    state = ServerState()
    assert asyncio.run(state.get_history_flow_project_root("ghost")) is None


def test_project_root_empty_when_not_recorded():
    """A flow with no recorded root degrades to ``None`` (legacy empty behaviour)."""
    state = ServerState()

    async def scenario():
        await state.update_history_index("m1", [{"flow_id": "f1"}])
        assert await state.get_history_flow_project_root("f1") is None

    asyncio.run(scenario())


def test_project_root_owner_scoped_out_returns_none():
    """A flow on another owner's machine is invisible to this owner's query."""
    state = ServerState()

    async def scenario():
        await state.register_machine("m1", owner_id="owner-a")
        await state.update_history_index(
            "m1", [{"flow_id": "f1", "project_root": "/a/root"}]
        )
        # owner-a sees it...
        assert (
            await state.get_history_flow_project_root("f1", owner="owner-a")
            == "/a/root"
        )
        # ...but owner-b must not.
        assert (
            await state.get_history_flow_project_root("f1", owner="owner-b") is None
        )

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# history_detail cache-miss pull carries the authoritative project_root
# --------------------------------------------------------------------------


@pytest.fixture()
def client_and_app(monkeypatch):
    from fastapi.testclient import TestClient

    import se3.server.app as app_module

    from _authsrv import authed_app, login

    monkeypatch.setattr(app_module, "HISTORY_INDEX_REFRESH_TIMEOUT", 0.3)

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


def _receive_until(daemon, msg_type):
    while True:
        frame = protocol.decode(daemon.receive_text())
        if frame.type == msg_type:
            return frame
        assert frame.type == protocol.MSG_HISTORY_INDEX_REQUEST


# --------------------------------------------------------------------------
# ServerState.is_active_worktree_flow — unit (self-heal gate)
# --------------------------------------------------------------------------


def test_is_active_worktree_flow_true_for_running_worktree():
    """A running flow under ``se3/worktrees/<name>`` is an active worktree flow."""
    state = ServerState()

    async def scenario():
        await state.update_status(
            "m1",
            {
                "machine_id": "m1",
                "flows": [
                    {
                        "flow_id": "f1",
                        "status": "running",
                        "project_root": "/repo/se3/worktrees/wt-a",
                    }
                ],
            },
        )
        assert await state.is_active_worktree_flow("f1") is True

    asyncio.run(scenario())


def test_is_active_worktree_flow_true_for_paused_worktree():
    """A paused worktree flow (discovery blocked on a human reply) is still
    active: round-2 chat records may be on the daemon but not yet in the server
    cache, so the self-heal gate must fire during the pending-reply window."""
    state = ServerState()

    async def scenario():
        await state.update_status(
            "m1",
            {
                "machine_id": "m1",
                "flows": [
                    {
                        "flow_id": "f1",
                        "status": "paused",
                        "project_root": "/repo/se3/worktrees/wt-a",
                    }
                ],
            },
        )
        assert await state.is_active_worktree_flow("f1") is True

    asyncio.run(scenario())


def test_is_active_worktree_flow_false_when_completed():
    """A completed worktree flow is no longer active — no reconcile pull."""
    state = ServerState()

    async def scenario():
        await state.update_status(
            "m1",
            {
                "machine_id": "m1",
                "flows": [
                    {
                        "flow_id": "f1",
                        "status": "completed",
                        "project_root": "/repo/se3/worktrees/wt-a",
                    }
                ],
            },
        )
        assert await state.is_active_worktree_flow("f1") is False

    asyncio.run(scenario())


def test_is_active_worktree_flow_false_for_non_worktree():
    """A running flow under an ordinary root is NOT a worktree flow (unchanged)."""
    state = ServerState()

    async def scenario():
        await state.update_status(
            "m1",
            {
                "machine_id": "m1",
                "flows": [
                    {
                        "flow_id": "f1",
                        "status": "running",
                        "project_root": "/repo",
                    }
                ],
            },
        )
        assert await state.is_active_worktree_flow("f1") is False

    asyncio.run(scenario())


def test_is_active_worktree_flow_false_for_unknown_flow():
    state = ServerState()
    assert asyncio.run(state.is_active_worktree_flow("ghost")) is False


def test_is_active_worktree_flow_owner_scoped():
    """One owner's active worktree flow is invisible to another owner's gate."""
    state = ServerState()

    async def scenario():
        await state.register_machine("m1", owner_id="owner-a")
        await state.update_status(
            "m1",
            {
                "machine_id": "m1",
                "flows": [
                    {
                        "flow_id": "f1",
                        "status": "running",
                        "project_root": "/repo/se3/worktrees/wt-a",
                    }
                ],
            },
        )
        assert await state.is_active_worktree_flow("f1", owner="owner-a") is True
        assert await state.is_active_worktree_flow("f1", owner="owner-b") is False

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# ServerState.append_history — idempotent full re-pull keeps the generation
# --------------------------------------------------------------------------


def test_identical_full_repull_keeps_generation():
    """A no-op reconcile full pull MUST NOT roll the bundle generation.

    The running-worktree self-heal re-pulls the whole bundle on a throttle even
    when the daemon has nothing new. If an identical replace rolled a fresh
    generation it would invalidate every outstanding progress token and force an
    in-sync client into a full re-fetch on the next poll — the churn the
    delta/not-modified path exists to avoid.
    """
    state = ServerState()

    async def scenario():
        recs = [
            {
                "step_id": "01_discovery_ab12",
                "step_type": "discovery",
                "ordinal": 0,
                "message": {"round": 1},
            }
        ]
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, recs, machine_id="m1"
        )
        gen1 = (await state.get_history("f1"))["generation"]
        # An identical full re-pull from the same machine keeps generation.
        applied = await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, list(recs), machine_id="m1"
        )
        assert applied is True
        snap2 = await state.get_history("f1")
        assert snap2["generation"] == gen1
        assert len(snap2["records"]) == 1
        # A full re-pull that actually GREW (a new round arrived) rolls a fresh
        # generation so the client is told to re-fetch the enlarged bundle.
        grown = recs + [
            {
                "step_id": "01_discovery_ab12",
                "step_type": "discovery",
                "ordinal": 1,
                "message": {"round": 2},
            }
        ]
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, grown, machine_id="m1"
        )
        snap3 = await state.get_history("f1")
        assert snap3["generation"] != gen1
        assert len(snap3["records"]) == 2

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# ServerState.append_history — reconcile may only add, never take away (#287)
# --------------------------------------------------------------------------


def _round(ordinal: int) -> dict:
    return {
        "step_id": "01_discovery_ab12",
        "step_type": "discovery",
        "ordinal": ordinal,
        "message": {"round": ordinal + 1},
    }


def test_empty_full_does_not_wipe_existing_bundle():
    """An empty full frame MUST NOT clear a non-empty bundle from the same machine.

    This is the #287 regression at its narrowest: the paused-worktree self-heal
    reconcile fires a cursorless pull, the daemon fails to resolve the flow's
    history dir and answers ``mode=full, records=[]``, and the wholesale replace
    left the browser with a ``full`` delivery of zero records — a blank chat.
    """
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [_round(0)], machine_id="m1"
        )
        gen1 = (await state.get_history("f1"))["generation"]

        applied = await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [], machine_id="m1"
        )
        # Reported as applied so a pull waiter blocked on this reply is released
        # rather than spinning until the REST handler times out.
        assert applied is True

        snap = await state.get_history("f1")
        assert len(snap["records"]) == 1
        assert snap["records"] == [_round(0)]
        # Generation kept ⇒ in-sync clients stay on the cheap not_modified path.
        assert snap["generation"] == gen1

    asyncio.run(scenario())


def test_non_empty_full_still_replaces_and_rolls_generation():
    """The guard must not blunt a genuine reconcile: a fuller frame still wins.

    This is the self-heal path that fixes the ORIGINAL multi-round loss — a
    reconcile that brings records the live push dropped must replace the bundle
    and roll the generation so the client re-fetches.
    """
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [_round(0)], machine_id="m1"
        )
        gen1 = (await state.get_history("f1"))["generation"]

        applied = await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [_round(0), _round(1), _round(2)],
            machine_id="m1",
        )
        assert applied is True

        snap = await state.get_history("f1")
        assert len(snap["records"]) == 3
        assert snap["generation"] != gen1

    asyncio.run(scenario())


def test_cross_machine_full_still_establishes_new_bundle():
    """A real machine switch still replaces the bundle wholesale.

    The no-rollback guard is scoped to the SAME machine: another daemon's full
    snapshot is authoritative for the flow it now owns, so it must take over the
    bundle even though that discards the previous machine's records.
    """
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [_round(0), _round(1)], machine_id="m1"
        )
        applied = await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [_round(0)], machine_id="m2"
        )
        assert applied is True

        snap = await state.get_history("f1")
        assert snap["machine_id"] == "m2"
        assert snap["records"] == [_round(0)]

    asyncio.run(scenario())


def test_first_empty_full_still_creates_empty_bundle():
    """With no cached bundle there is nothing to protect: an empty full writes through.

    A flow that genuinely has no records yet must still get an authoritative
    (empty) bundle, otherwise the REST read stays a cache miss and re-pulls the
    daemon on every poll.
    """
    state = ServerState()

    async def scenario():
        applied = await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [], machine_id="m1"
        )
        assert applied is True

        snap = await state.get_history("f1")
        assert snap is not None
        assert snap["records"] == []
        assert snap["machine_id"] == "m1"

        # And the empty bundle is a normal anchor: a live append extends it.
        assert await state.append_history(
            "f1", protocol.HISTORY_MODE_APPEND, [_round(0)], machine_id="m1"
        )
        assert len((await state.get_history("f1"))["records"]) == 1

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# history_detail running-worktree self-heal reconcile (integration)
# --------------------------------------------------------------------------


def _sync_live_flow(client, flow_id="f1"):
    """Block until a STATUS_UPDATE-reported live flow is visible over REST."""
    for _ in range(200):
        if client.get(f"/api/flows/{flow_id}").status_code == 200:
            return
    raise AssertionError(f"flow {flow_id} never became live")


def _report_running_flow(daemon, project_root, flow_id="f1"):
    daemon.send_text(
        protocol.make_status_update(
            {
                "machine_id": "m1",
                "flows": [
                    {
                        "flow_id": flow_id,
                        "status": "running",
                        "project_root": project_root,
                    }
                ],
            }
        ).to_json()
    )


def _first_full_pull(client, daemon, records, flow_id="f1"):
    """Run the cache-miss GET, answer the daemon pull, return the JSON body."""
    result: dict = {}

    def do_get():
        result["resp"] = client.get(f"/api/history/{flow_id}")

    worker = threading.Thread(target=do_get)
    worker.start()
    try:
        _receive_until(daemon, protocol.MSG_HISTORY_REQUEST)
        daemon.send_text(
            protocol.make_history_data(
                flow_id, protocol.HISTORY_MODE_FULL, records
            ).to_json()
        )
    finally:
        worker.join(timeout=5)
    return result["resp"].json()


def test_running_worktree_selfheal_reconciles_missing_round(
    client_and_app, monkeypatch
):
    """A running worktree flow whose cache froze at round 1 self-heals: the
    ``not_modified`` poll reconciles against the daemon and the missing round 2
    lands in the response, identity fields intact."""
    from se3.server.state import ServerState as _SS

    client, app = client_and_app
    # Drop the reconcile throttle so the self-heal fires on the very next poll
    # (the throttle itself is asserted separately below).
    monkeypatch.setattr(_SS, "_HISTORY_FULL_PULL_MIN_INTERVAL", 0.0)
    wt = "/repo/se3/worktrees/wt-a"
    round1 = {
        "step_id": "01_discovery_ab12",
        "step_type": "discovery",
        "ordinal": 0,
        "message": {"round": 1},
    }
    round2 = {
        "step_id": "01_discovery_ab12.from-wt__b",
        "step_type": "discovery",
        "ordinal": 0,
        "message": {"round": 2},
    }
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        _report_running_flow(daemon, wt)
        _sync_live_flow(client)

        # Cache miss → daemon returns only round 1 (the "stuck at round 1" cache).
        body1 = _first_full_pull(client, daemon, [round1])
        assert body1["delivery"] == "full"
        assert len(body1["records"]) == 1

        # The client is now provably in sync with the (incomplete) cache, so a
        # bare poll would answer not_modified forever. The self-heal reconcile
        # re-pulls the daemon, which now has round 2 as well.
        result: dict = {}

        def do_poll():
            result["resp"] = client.get(
                "/api/history/f1",
                params={"after": body1["progress"], "sig": body1["signature"]},
            )

        worker = threading.Thread(target=do_poll)
        worker.start()
        try:
            _receive_until(daemon, protocol.MSG_HISTORY_REQUEST)
            daemon.send_text(
                protocol.make_history_data(
                    "f1", protocol.HISTORY_MODE_FULL, [round1, round2]
                ).to_json()
            )
        finally:
            worker.join(timeout=5)

        body2 = result["resp"].json()
        assert body2["delivery"] == "full"
        steps = [(r["step_id"], r["ordinal"]) for r in body2["records"]]
        # Both rounds present, and G1's distinct per-file identity is preserved
        # verbatim through the relay (round 2 not dropped as a duplicate ordinal).
        assert ("01_discovery_ab12", 0) in steps
        assert ("01_discovery_ab12.from-wt__b", 0) in steps


def test_running_worktree_selfheal_respects_throttle(client_and_app):
    """Within the throttle window the self-heal does NOT re-pull the daemon: an
    in-sync poll answers ``not_modified`` cheaply, so the 3 s self-heal cannot
    fan out one回源 pull per tick."""
    client, app = client_and_app  # default throttle (>0) in effect
    wt = "/repo/se3/worktrees/wt-a"
    round1 = {"step_id": "01_discovery", "ordinal": 0, "message": {"round": 1}}
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        _report_running_flow(daemon, wt)
        _sync_live_flow(client)

        body1 = _first_full_pull(client, daemon, [round1])
        # Immediately re-poll: the cache-miss pull above just stamped a full pull,
        # so the reconcile is throttled and no MSG_HISTORY_REQUEST is emitted —
        # the poll returns straight from cache. (If it wrongly re-pulled, this GET
        # would block on a daemon reply we never send.)
        resp2 = client.get(
            "/api/history/f1",
            params={"after": body1["progress"], "sig": body1["signature"]},
        )
        body2 = resp2.json()
        assert body2["delivery"] == "not_modified"
        assert body2["records"] == []


def test_non_worktree_flow_never_reconciles(client_and_app, monkeypatch):
    """An ordinary (non-worktree) running flow is served straight from cache even
    with the throttle disabled — the self-heal reconcile is worktree-only, so
    normal sessions are unaffected."""
    from se3.server.state import ServerState as _SS

    client, app = client_and_app
    monkeypatch.setattr(_SS, "_HISTORY_FULL_PULL_MIN_INTERVAL", 0.0)
    round1 = {"step_id": "01_discovery", "ordinal": 0, "message": {"round": 1}}
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        _report_running_flow(daemon, "/repo")  # ordinary root, not a worktree
        _sync_live_flow(client)

        body1 = _first_full_pull(client, daemon, [round1])
        # Even with the throttle off, a non-worktree flow's in-sync poll answers
        # not_modified without re-pulling the daemon.
        resp2 = client.get(
            "/api/history/f1",
            params={"after": body1["progress"], "sig": body1["signature"]},
        )
        body2 = resp2.json()
        assert body2["delivery"] == "not_modified"
        assert body2["records"] == []


def test_cache_miss_pull_sends_authoritative_project_root(client_and_app):
    """``GET /api/history/{flow_id}`` cache miss must tell the daemon the flow's
    authoritative ``SessionMeta.project_root`` (the worktree root), not an empty
    string that lets the daemon fall back to first-match guessing."""
    client, app = client_and_app
    worktree_root = "/repo/se3/worktrees/wt-a"
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        # Report the index carrying the flow's authoritative run root.
        daemon.send_text(
            protocol.make_history_index(
                [{"flow_id": "f1", "project_root": worktree_root}]
            ).to_json()
        )
        for _ in range(50):
            if client.get("/api/history").json()["sessions"]:
                break

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/history/f1")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            req = _receive_until(daemon, protocol.MSG_HISTORY_REQUEST)
            assert req.payload["flow_id"] == "f1"
            # The single source of truth: the daemon is told exactly which root
            # to read, so it can merge the main + worktree history dirs rather
            # than returning the first-match discovery-only directory.
            assert req.payload["project_root"] == worktree_root
            daemon.send_text(
                protocol.make_history_data(
                    "f1",
                    protocol.HISTORY_MODE_FULL,
                    [{"step": "s1", "line": "pulled"}],
                ).to_json()
            )
        finally:
            worker.join(timeout=5)
        assert result["resp"].status_code == 200


def test_cache_miss_pull_empty_root_when_unrecorded(client_and_app):
    """When the flow has no recorded root the pull degrades to an empty
    ``project_root`` — the legacy behaviour, so non-worktree flows are
    unaffected by the authoritative-root wiring."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(
            protocol.make_history_index([{"flow_id": "f1"}]).to_json()
        )
        for _ in range(50):
            if client.get("/api/history").json()["sessions"]:
                break

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/history/f1")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            req = _receive_until(daemon, protocol.MSG_HISTORY_REQUEST)
            assert req.payload["flow_id"] == "f1"
            assert req.payload["project_root"] == ""
            daemon.send_text(
                protocol.make_history_data(
                    "f1",
                    protocol.HISTORY_MODE_FULL,
                    [{"step": "s1", "line": "pulled"}],
                ).to_json()
            )
        finally:
            worker.join(timeout=5)
        assert result["resp"].status_code == 200
