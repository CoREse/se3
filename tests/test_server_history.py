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
def client_and_app(monkeypatch):
    from fastapi.testclient import TestClient

    import se3.server.app as app_module
    from se3.server.app import create_app

    # ``GET /api/history`` now broadcasts a forced index re-push to every
    # connected daemon and waits for the replies. Tests using a stand-in
    # daemon that does not answer would otherwise block the full 2 s timeout
    # on every call, so shorten the wait here.
    monkeypatch.setattr(app_module, "HISTORY_INDEX_REFRESH_TIMEOUT", 0.3)

    app = create_app()
    with TestClient(app) as client:
        yield client, app


def _receive_until(daemon, msg_type):
    """Read frames from *daemon*, skipping index-refresh broadcasts.

    ``GET /api/history`` queues a ``MSG_HISTORY_INDEX_REQUEST`` on every
    connected daemon; a test that next expects a different server→daemon frame
    must skip past those broadcasts. Returns the first frame of *msg_type*.
    """
    while True:
        frame = protocol.decode(daemon.receive_text())
        if frame.type == msg_type:
            return frame
        assert frame.type == protocol.MSG_HISTORY_INDEX_REQUEST


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
            # The server should route a pull request to the daemon (skipping
            # any index-refresh broadcast queued by the GET /api/history above).
            req = _receive_until(daemon, protocol.MSG_HISTORY_REQUEST)
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
            # Drain the request but deliberately never answer it (skipping any
            # index-refresh broadcast queued by the GET /api/history above).
            _receive_until(daemon, protocol.MSG_HISTORY_REQUEST)
        finally:
            worker.join(timeout=5)
        assert result["resp"].status_code == 504


def test_history_endpoints_empty(client_and_app):
    client, _ = client_and_app
    resp = client.get("/api/history")
    assert resp.status_code == 200
    assert resp.json() == {"sessions": [], "count": 0}


# --------------------------------------------------------------------------
# IndexRefreshRegistry + broadcast_index_refresh (unit)
# --------------------------------------------------------------------------


class _FakeServerWS:
    """A server-side WebSocket stand-in capturing what the server sends down."""

    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(protocol.decode(data))


def test_index_refresh_registry_resolve_and_discard():
    from se3.server.ws import IndexRefreshRegistry

    async def scenario():
        reg = IndexRefreshRegistry()
        fut = reg.register("m1")
        reg.resolve("m1")
        assert fut.result() is True
        # resolve with no parked waiter is a no-op (no error).
        reg.resolve("ghost")
        # discard removes a waiter without resolving it.
        fut2 = reg.register("m2")
        reg.discard("m2", fut2)
        assert not fut2.done()
        # discard on an already-cleared machine is harmless.
        reg.discard("m2", fut2)

    asyncio.run(scenario())


def test_broadcast_index_refresh_sends_to_connected_and_returns_waiters():
    from se3.server.ws import (
        ConnectionManager,
        IndexRefreshRegistry,
        broadcast_index_refresh,
    )

    async def scenario():
        mgr = ConnectionManager()
        ws1, ws2 = _FakeServerWS(), _FakeServerWS()
        await mgr.connect("m1", ws1)
        await mgr.connect("m2", ws2)
        reg = IndexRefreshRegistry()

        waiters = await broadcast_index_refresh(mgr, reg)

        assert set(waiters) == {"m1", "m2"}
        assert ws1.sent[0].type == protocol.MSG_HISTORY_INDEX_REQUEST
        assert ws2.sent[0].type == protocol.MSG_HISTORY_INDEX_REQUEST
        # A daemon's re-push resolves only its own waiter.
        reg.resolve("m1")
        assert waiters["m1"].result() is True
        assert not waiters["m2"].done()

    asyncio.run(scenario())


def test_broadcast_index_refresh_no_daemon_returns_empty():
    from se3.server.ws import (
        ConnectionManager,
        IndexRefreshRegistry,
        broadcast_index_refresh,
    )

    async def scenario():
        waiters = await broadcast_index_refresh(
            ConnectionManager(), IndexRefreshRegistry()
        )
        assert waiters == {}

    asyncio.run(scenario())


def test_broadcast_index_refresh_discards_waiter_on_send_failure():
    from se3.server.ws import (
        ConnectionManager,
        IndexRefreshRegistry,
        broadcast_index_refresh,
    )

    class _BadWS:
        async def send_text(self, data):
            raise RuntimeError("boom")

    async def scenario():
        mgr = ConnectionManager()
        await mgr.connect("m1", _BadWS())
        reg = IndexRefreshRegistry()
        waiters = await broadcast_index_refresh(mgr, reg)
        # Send failed -> the machine is not returned as a waiter ...
        assert waiters == {}
        # ... and no dangling waiter was left behind in the registry.
        reg.resolve("m1")  # no-op, must not raise

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# GET /api/history actively refreshes the index on entry
# --------------------------------------------------------------------------


def test_history_list_broadcasts_index_refresh_request(client_and_app):
    """Entering the history view asks every connected daemon to re-push."""
    client, _ = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(protocol.make_hello("m1", "host", "6.4.0").to_json())
        protocol.decode(daemon.receive_text())  # WELCOME

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/history")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            req = _receive_until(daemon, protocol.MSG_HISTORY_INDEX_REQUEST)
            assert req.type == protocol.MSG_HISTORY_INDEX_REQUEST
        finally:
            worker.join(timeout=5)
        assert result["resp"].status_code == 200


def test_history_list_returns_latest_after_forced_repush(client_and_app):
    """A stale cached index (5/14) is replaced by the daemon's forced
    re-push (5/21) before GET /api/history aggregates and returns."""
    client, _ = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(protocol.make_hello("m1", "host", "6.4.0").to_json())
        protocol.decode(daemon.receive_text())  # WELCOME
        # Stale index the server caches up front (only an old 5/14 entry).
        daemon.send_text(
            protocol.make_history_index(
                [{"flow_id": "f1", "updated_at": "2026-05-14"}]
            ).to_json()
        )

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/history")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            # The GET broadcasts a forced index-refresh; the daemon answers
            # with a fresh index carrying the latest 5/21 session.
            req = _receive_until(daemon, protocol.MSG_HISTORY_INDEX_REQUEST)
            assert req.type == protocol.MSG_HISTORY_INDEX_REQUEST
            daemon.send_text(
                protocol.make_history_index(
                    [{"flow_id": "f1", "updated_at": "2026-05-21"}]
                ).to_json()
            )
        finally:
            worker.join(timeout=5)

        resp = result["resp"]
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert sessions and sessions[0]["flow_id"] == "f1"
        # The forced re-push won: the response carries the latest date.
        assert sessions[0]["updated_at"] == "2026-05-21"


def test_history_list_degrades_to_cache_on_timeout(client_and_app):
    """A connected daemon that never answers the refresh request still yields
    a prompt 200 with the currently cached index (no 5xx, no hang)."""
    client, _ = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(protocol.make_hello("m1", "host", "6.4.0").to_json())
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(
            protocol.make_history_index(
                [{"flow_id": "f1", "updated_at": "2026-05-14"}]
            ).to_json()
        )

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/history")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            # Drain the refresh request but never answer it -> forced timeout.
            _receive_until(daemon, protocol.MSG_HISTORY_INDEX_REQUEST)
        finally:
            worker.join(timeout=5)

        resp = result["resp"]
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert sessions and sessions[0]["flow_id"] == "f1"


def test_history_list_no_daemon_returns_200_without_blocking(client_and_app):
    """With no connected daemon the endpoint returns the cached index
    immediately (no waiter to await) and never errors."""
    client, _ = client_and_app
    resp = client.get("/api/history")
    assert resp.status_code == 200
    assert resp.json() == {"sessions": [], "count": 0}
