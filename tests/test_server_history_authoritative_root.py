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
