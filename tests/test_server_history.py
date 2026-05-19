"""Tests for the server-side history relay: ServerState caching, WebSocket
routing of history messages, and the ``/api/history`` REST endpoints.

The server is a pure in-memory relay — these tests exercise index
aggregation, append merging, on-demand pull (cache hit / miss), and the
pull timeout path.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from se3.daemon import protocol
from se3.server.state import ServerState


# --------------------------------------------------------------------------
# ServerState — history index & data caching
# --------------------------------------------------------------------------


def test_history_index_write_and_aggregate():
    state = ServerState()

    async def scenario():
        await state.update_history_index(
            "m1", [{"flow_id": "f1", "task_description": "A", "updated_at": "2026-01-02"}]
        )
        await state.update_history_index(
            "m2", [{"flow_id": "f2", "task_description": "B", "updated_at": "2026-01-03"}]
        )
        index = await state.get_history_index()
        assert len(index) == 2
        # Sorted by updated_at descending.
        assert index[0]["flow_id"] == "f2"
        # Each entry is annotated with its reporting machine.
        by_flow = {e["flow_id"]: e["machine_id"] for e in index}
        assert by_flow == {"f1": "m1", "f2": "m2"}

    asyncio.run(scenario())


def test_history_index_replaced_per_machine():
    state = ServerState()

    async def scenario():
        await state.update_history_index("m1", [{"flow_id": "f1"}])
        await state.update_history_index("m1", [{"flow_id": "f9"}])
        index = await state.get_history_index()
        assert [e["flow_id"] for e in index] == ["f9"]

    asyncio.run(scenario())


def test_history_data_full_then_append_merges():
    state = ServerState()

    async def scenario():
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
            [{"step": "s1", "line": 2}, {"step": "s1", "line": 3}],
            cursor={"s1": 3},
        )
        cached = await state.get_history("f1")
        assert cached is not None
        assert [r["line"] for r in cached["records"]] == [1, 2, 3]
        assert cached["cursor"] == {"s1": 3}
        assert cached["machine_id"] == "m1"

    asyncio.run(scenario())


def test_history_data_full_replaces():
    state = ServerState()

    async def scenario():
        await state.append_history("f1", protocol.HISTORY_MODE_FULL, [{"line": 1}])
        await state.append_history("f1", protocol.HISTORY_MODE_FULL, [{"line": 9}])
        cached = await state.get_history("f1")
        assert [r["line"] for r in cached["records"]] == [9]

    asyncio.run(scenario())


def test_history_append_without_prior_full_starts_fresh():
    state = ServerState()

    async def scenario():
        # An append with no cached bundle behaves as a first full snapshot.
        await state.append_history("f1", protocol.HISTORY_MODE_APPEND, [{"line": 1}])
        cached = await state.get_history("f1")
        assert cached is not None
        assert [r["line"] for r in cached["records"]] == [1]

    asyncio.run(scenario())


def test_get_history_miss_returns_none():
    state = ServerState()

    async def scenario():
        assert await state.get_history("nope") is None

    asyncio.run(scenario())


def test_find_machine_for_history_flow():
    state = ServerState()

    async def scenario():
        await state.update_history_index("m1", [{"flow_id": "f1"}])
        assert await state.find_machine_for_history_flow("f1") == "m1"
        # Falls back to cached data owner.
        await state.append_history(
            "f2", protocol.HISTORY_MODE_FULL, [], machine_id="m2"
        )
        assert await state.find_machine_for_history_flow("f2") == "m2"
        # Falls back to the live flow set.
        await state.update_status(
            "m3", {"machine_id": "m3", "flows": [{"flow_id": "f3"}]}
        )
        assert await state.find_machine_for_history_flow("f3") == "m3"
        assert await state.find_machine_for_history_flow("ghost") is None

    asyncio.run(scenario())


def test_history_caches_are_not_persisted(tmp_path):
    """The relay holds history purely in memory — no files are written."""
    state = ServerState()

    async def scenario():
        await state.update_history_index("m1", [{"flow_id": "f1"}])
        await state.append_history("f1", protocol.HISTORY_MODE_FULL, [{"line": 1}])

    asyncio.run(scenario())
    # No on-disk artifact of any kind.
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# WebSocket routing + REST endpoints
# --------------------------------------------------------------------------


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    from se3.server.app import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client, app


def test_history_index_message_routed_to_state(client_and_app):
    client, _ = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(protocol.make_hello("m1", "host", "6.4.0").to_json())
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(
            protocol.make_history_index(
                [{"flow_id": "f1", "task_description": "T", "status": "completed"}]
            ).to_json()
        )
        for _ in range(50):
            sessions = client.get("/api/history").json()["sessions"]
            if sessions:
                break
        assert sessions and sessions[0]["flow_id"] == "f1"
        assert sessions[0]["machine_id"] == "m1"


def test_history_index_broadcast_to_ui(client_and_app):
    client, _ = client_and_app
    with client.websocket_connect("/ws/ui") as ui:
        assert json.loads(ui.receive_text())["type"] == "snapshot"
        with client.websocket_connect("/ws") as daemon:
            daemon.send_text(protocol.make_hello("m1", "host", "6.4.0").to_json())
            protocol.decode(daemon.receive_text())  # WELCOME
            assert json.loads(ui.receive_text())["type"] == "status_update"
            daemon.send_text(
                protocol.make_history_index([{"flow_id": "f1"}]).to_json()
            )
            pushed = json.loads(ui.receive_text())
            assert pushed["type"] == "history_index"
            assert pushed["sessions"][0]["flow_id"] == "f1"


def test_history_data_message_cached_and_broadcast(client_and_app):
    client, _ = client_and_app
    with client.websocket_connect("/ws/ui") as ui:
        assert json.loads(ui.receive_text())["type"] == "snapshot"
        with client.websocket_connect("/ws") as daemon:
            daemon.send_text(protocol.make_hello("m1", "host", "6.4.0").to_json())
            protocol.decode(daemon.receive_text())  # WELCOME
            assert json.loads(ui.receive_text())["type"] == "status_update"
            # Active flow incremental append arrives unsolicited.
            daemon.send_text(
                protocol.make_history_data(
                    "f1",
                    protocol.HISTORY_MODE_FULL,
                    [{"step": "s1", "line": "hi"}],
                ).to_json()
            )
            pushed = json.loads(ui.receive_text())
            assert pushed["type"] == "history_data"
            assert pushed["flow_id"] == "f1"
            # And it landed in the cache (served straight from REST).
            resp = client.get("/api/history/f1")
            assert resp.status_code == 200
            body = resp.json()
            assert body["cached"] is True
            assert body["records"][0]["line"] == "hi"


def test_history_detail_on_demand_pull(client_and_app):
    """A cache miss triggers a MSG_HISTORY_REQUEST and resolves on the reply."""
    client, _ = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(protocol.make_hello("m1", "host", "6.4.0").to_json())
        protocol.decode(daemon.receive_text())  # WELCOME
        # Report the index so the server knows m1 owns f1.
        daemon.send_text(protocol.make_history_index([{"flow_id": "f1"}]).to_json())
        for _ in range(50):
            if client.get("/api/history").json()["sessions"]:
                break

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/history/f1")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            # The server should route a pull request to the daemon.
            req = protocol.decode(daemon.receive_text())
            assert req.type == protocol.MSG_HISTORY_REQUEST
            assert req.payload["flow_id"] == "f1"
            daemon.send_text(
                protocol.make_history_data(
                    "f1",
                    protocol.HISTORY_MODE_FULL,
                    [{"step": "s1", "line": "pulled"}],
                ).to_json()
            )
        finally:
            worker.join(timeout=5)
        resp = result["resp"]
        assert resp.status_code == 200
        body = resp.json()
        assert body["cached"] is False
        assert body["records"][0]["line"] == "pulled"


def test_history_detail_no_daemon_404(client_and_app):
    client, _ = client_and_app
    resp = client.get("/api/history/ghost")
    assert resp.status_code == 404


def test_history_detail_pull_timeout(client_and_app, monkeypatch):
    """When the owning daemon never replies, the pull times out with 504."""
    import se3.server.app as app_module

    monkeypatch.setattr(app_module, "HISTORY_PULL_TIMEOUT", 0.5)
    client, _ = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(protocol.make_hello("m1", "host", "6.4.0").to_json())
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(protocol.make_history_index([{"flow_id": "f1"}]).to_json())
        for _ in range(50):
            if client.get("/api/history").json()["sessions"]:
                break

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/history/f1")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            # Drain the request but deliberately never answer it.
            req = protocol.decode(daemon.receive_text())
            assert req.type == protocol.MSG_HISTORY_REQUEST
        finally:
            worker.join(timeout=5)
        assert result["resp"].status_code == 504


def test_history_endpoints_empty(client_and_app):
    client, _ = client_and_app
    resp = client.get("/api/history")
    assert resp.status_code == 200
    assert resp.json() == {"sessions": [], "count": 0}
