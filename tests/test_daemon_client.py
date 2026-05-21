"""Tests for the daemon-side WebSocket client and its server integration."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest

from se3.daemon import protocol
from se3.daemon.client import DaemonClient, _default_respond_handler, _normalize_ws_url
from se3.daemon.daemon import Daemon, DaemonConfig


# --------------------------------------------------------------------------
# URL normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        # Host already carries an explicit port -> kept verbatim.
        ("localhost:8080", "ws://localhost:8080/ws"),
        ("ws://host:9", "ws://host:9/ws"),
        ("http://host:8080", "ws://host:8080/ws"),
        ("ws://host:8080/ws", "ws://host:8080/ws"),
        ("host:8080/", "ws://host:8080/ws"),
        # A non-default explicit port is never overwritten.
        ("ws://host:9000", "ws://host:9000/ws"),
        ("ws://host:9000/ws", "ws://host:9000/ws"),
        # No port -> the shared DEFAULT_SERVER_PORT is filled in.
        ("ws://host", "ws://host:8080/ws"),
        ("wss://host", "wss://host:8080/ws"),
        ("https://host", "wss://host:8080/ws"),
        ("http://host", "ws://host:8080/ws"),
        ("host", "ws://host:8080/ws"),
        ("host/ws", "ws://host:8080/ws"),
        # A custom path is preserved alongside the filled-in port.
        ("ws://host/daemon", "ws://host:8080/daemon"),
        ("ws://host:9000/daemon", "ws://host:9000/daemon"),
        # IPv6 literals: brackets are not mistaken for a port separator.
        ("ws://[::1]", "ws://[::1]:8080/ws"),
        ("ws://[::1]:9000", "ws://[::1]:9000/ws"),
        ("ws://[::1]:9000/ws", "ws://[::1]:9000/ws"),
    ],
)
def test_normalize_ws_url(given, expected):
    assert _normalize_ws_url(given) == expected


def test_normalize_ws_url_default_port_is_shared_constant():
    """The filled-in port comes from protocol.DEFAULT_SERVER_PORT."""
    assert _normalize_ws_url("ws://host") == f"ws://host:{protocol.DEFAULT_SERVER_PORT}/ws"


# --------------------------------------------------------------------------
# response-file writer
# --------------------------------------------------------------------------


def test_default_respond_handler_writes_file(tmp_path):
    _default_respond_handler("call-7", str(tmp_path), {"answer": "go"})
    target = tmp_path / "se3" / "calls" / "call-7.response.json"
    assert target.is_file()
    payload = json.loads(target.read_text())
    assert payload["call_id"] == "call-7"
    assert payload["response"] == {"answer": "go"}
    assert payload["source"] == "daemon-client"


# --------------------------------------------------------------------------
# message dispatch (no real socket)
# --------------------------------------------------------------------------


class _FakeWS:
    """Minimal WebSocket stand-in capturing what the client sends."""

    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(protocol.decode(data))


def _make_client(**kw):
    return DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="6.4.0",
        snapshot_provider=kw.pop("snapshot_provider", lambda: {"machine_id": "m1"}),
        **kw,
    )


def test_dispatch_ping_replies_pong():
    client = _make_client()
    ws = _FakeWS()

    async def scenario():
        await client._dispatch(ws, protocol.make_ping(seq=11))

    asyncio.run(scenario())
    assert len(ws.sent) == 1
    assert ws.sent[0].type == protocol.MSG_PONG
    assert ws.sent[0].seq == 11


def test_dispatch_spawn_flow_routes_to_handler():
    received = []
    client = _make_client(
        spawn_handler=lambda t, p, ty, d: received.append((t, p, ty, d))
    )

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_spawn_flow("Implement Y", project_root="/p", task_type="bugfix"),
        )

    asyncio.run(scenario())
    assert received == [("Implement Y", "/p", "bugfix", False)]


def test_dispatch_spawn_flow_threads_discover_flag():
    received = []
    client = _make_client(
        spawn_handler=lambda t, p, ty, d: received.append((t, p, ty, d))
    )

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_spawn_flow("Explore Z", discover=True),
        )

    asyncio.run(scenario())
    assert received == [("Explore Z", "", "feature", True)]


def test_dispatch_spawn_flow_runs_ensure_handler_first():
    """ensure_handler is called before the spawn_handler with project_root."""
    ensure_calls = []
    spawn_calls = []

    class _Ensure:
        error = ""

    client = _make_client(
        ensure_handler=lambda root: (ensure_calls.append(root) or _Ensure()),
        spawn_handler=lambda t, p, ty, d: spawn_calls.append((t, p, ty, d)),
    )

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_spawn_flow("T", project_root="/some/path"),
        )

    asyncio.run(scenario())
    assert ensure_calls == ["/some/path"]
    assert spawn_calls == [("T", "/some/path", "feature", False)]


def test_dispatch_spawn_flow_aborts_when_ensure_returns_error():
    """A truthy ensure.error short-circuits — spawn_handler is not called."""
    spawn_calls = []

    class _Ensure:
        error = "se3 init exited non-zero"

    client = _make_client(
        ensure_handler=lambda root: _Ensure(),
        spawn_handler=lambda *a: spawn_calls.append(a),
    )

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_spawn_flow("T", project_root="/bad/path"),
        )

    asyncio.run(scenario())
    assert spawn_calls == []


def test_dispatch_spawn_flow_skips_ensure_when_no_project_root():
    """No project_root in payload means no ensure call — spawn still runs."""
    ensure_calls = []
    spawn_calls = []
    client = _make_client(
        ensure_handler=lambda root: ensure_calls.append(root),
        spawn_handler=lambda *a: spawn_calls.append(a),
    )

    async def scenario():
        await client._dispatch(_FakeWS(), protocol.make_spawn_flow("T"))

    asyncio.run(scenario())
    assert ensure_calls == []
    assert spawn_calls == [("T", "", "feature", False)]


def test_dispatch_spawn_flow_ignores_empty_task():
    received = []
    client = _make_client(spawn_handler=lambda *a: received.append(a))

    async def scenario():
        await client._dispatch(_FakeWS(), protocol.make_spawn_flow("   "))

    asyncio.run(scenario())
    assert received == []


def test_dispatch_respond_call_routes_to_handler(tmp_path):
    client = _make_client()
    ws = _FakeWS()

    async def scenario():
        await client._dispatch(
            ws,
            protocol.make_respond_call("c9", "the-answer", project_root=str(tmp_path)),
        )

    asyncio.run(scenario())
    target = tmp_path / "se3" / "calls" / "c9.response.json"
    assert target.is_file()


def test_push_status_sends_status_update():
    client = _make_client(snapshot_provider=lambda: {"machine_id": "m1", "flows": []})
    ws = _FakeWS()

    async def scenario():
        await client._push_status(ws)

    asyncio.run(scenario())
    assert ws.sent[0].type == protocol.MSG_STATUS_UPDATE
    assert ws.sent[0].payload["snapshot"]["machine_id"] == "m1"


# --------------------------------------------------------------------------
# history push: signature-gated trigger + cursor pruning
# --------------------------------------------------------------------------


class _FakeHistoryProvider:
    """Minimal history provider for exercising the client's push debounce."""

    def __init__(self):
        self.signature: dict = {}
        self.reads: list = []  # FlowRead-likes returned by read_active_flows

    def build_index(self):
        return []

    def active_flow_signature(self):
        return dict(self.signature)

    def read_active_flows(self, cursors):
        return list(self.reads)


def test_history_changed_detects_signature_delta():
    """_history_changed reports a change only when the signature actually moves."""
    provider = _FakeHistoryProvider()
    client = _make_client(history_provider=provider)

    provider.signature = {"f1": (1, 10)}
    assert client._history_changed() is True  # changed from the initial {}
    assert client._history_changed() is False  # unchanged -> debounced

    provider.signature = {"f1": (2, 20)}
    assert client._history_changed() is True  # engine.json / jsonl advanced


def test_history_changed_without_provider_is_false():
    client = _make_client()  # no history_provider
    assert client._history_changed() is False


def test_push_history_prunes_drained_terminal_flow_cursor():
    """The cursor map keeps active/flushed flows and drops drained ones."""
    from se3.daemon.history import FlowRead

    provider = _FakeHistoryProvider()
    client = _make_client(history_provider=provider)
    ws = _FakeWS()

    provider.reads = [
        FlowRead(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step_id": "s", "message": {"role": "user", "content": "x"}}],
            {"s.jsonl": 1},
        )
    ]
    asyncio.run(client._push_history(ws))
    assert client._history_cursors == {"f1": {"s.jsonl": 1}}
    data_frames = [m for m in ws.sent if m.type == protocol.MSG_HISTORY_DATA]
    assert len(data_frames) == 1

    # Next round: f1 is drained/terminal and no longer returned -> pruned.
    provider.reads = []
    asyncio.run(client._push_history(ws))
    assert client._history_cursors == {}


def test_push_history_keeps_empty_active_flow_cursor():
    """An active flow with an empty delta keeps its cursor (no re-snapshot)."""
    from se3.daemon.history import FlowRead

    provider = _FakeHistoryProvider()
    client = _make_client(history_provider=provider)
    ws = _FakeWS()

    provider.reads = [
        FlowRead("f1", protocol.HISTORY_MODE_APPEND, [], {"s.jsonl": 3})
    ]
    asyncio.run(client._push_history(ws))
    assert client._history_cursors == {"f1": {"s.jsonl": 3}}
    # Empty delta -> no HISTORY_DATA frame emitted.
    assert not [m for m in ws.sent if m.type == protocol.MSG_HISTORY_DATA]


# --------------------------------------------------------------------------
# Daemon integration
# --------------------------------------------------------------------------


def test_daemon_without_server_url_has_no_client():
    daemon = Daemon(DaemonConfig(server_url=None))
    assert daemon._start_server_client() is None


# --------------------------------------------------------------------------
# end-to-end: DaemonClient <-> live FastAPI server
# --------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(port: int):
    """Start a fresh uvicorn-hosted FastAPI server on *port* in a thread."""
    import uvicorn

    from se3.server.app import create_app

    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 8
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn server did not start"
    return server, thread


def _stop_server(server, thread) -> None:
    server.should_exit = True
    thread.join(timeout=8)


async def _wait_connected(client: DaemonClient, *, want: bool = True, tries: int = 300):
    for _ in range(tries):
        if client.connected is want:
            return
        await asyncio.sleep(0.05)


def test_client_connects_reports_and_receives():
    """End-to-end: a DaemonClient connects to a live server, reports state,
    and the server's REST API can route a SPAWN_FLOW back to it."""
    import httpx

    port = _free_port()
    server, thread = _start_server(port)
    snapshot = {
        "machine_id": "m-e2e",
        "hostname": "e2e-host",
        "flows": [{"flow_id": "f-e2e", "status": "running"}],
    }
    spawned = []
    client = DaemonClient(
        f"ws://127.0.0.1:{port}",
        machine_id="m-e2e",
        hostname="e2e-host",
        se3_version="6.4.0",
        snapshot_provider=lambda: snapshot,
        spawn_handler=lambda t, p, ty, d: spawned.append((t, p, ty, d)),
        status_interval=0.2,
    )
    base = f"http://127.0.0.1:{port}"

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await _wait_connected(client)
        assert client.connected, f"client never connected: {client.last_error}"

        async with httpx.AsyncClient(base_url=base) as http:
            # The server should learn our machine + flow from the pushed snapshot.
            for _ in range(300):
                resp = await http.get("/api/flows/f-e2e")
                if resp.status_code == 200:
                    break
                await asyncio.sleep(0.05)
            assert resp.status_code == 200
            assert resp.json()["machine_id"] == "m-e2e"

            # Downlink: publish a task -> server -> daemon SPAWN_FLOW.
            pub = await http.post(
                "/api/flows",
                json={
                    "machine_id": "m-e2e",
                    "task": "e2e task",
                    "task_type": "bugfix",
                    "project_root": "/p-e2e",
                },
            )
            assert pub.status_code == 202

        for _ in range(300):
            if spawned:
                break
            await asyncio.sleep(0.05)
        assert spawned == [("e2e task", "/p-e2e", "bugfix", False)]

        stop.set()
        await asyncio.wait_for(task, timeout=8)

    try:
        asyncio.run(scenario())
    finally:
        _stop_server(server, thread)


def test_client_reconnects_after_server_drop():
    """The client must re-establish the connection (with backoff) after the
    server goes away and comes back."""
    port = _free_port()
    server, thread = _start_server(port)
    client = DaemonClient(
        f"ws://127.0.0.1:{port}",
        machine_id="m-recon",
        hostname="h",
        se3_version="6.4.0",
        snapshot_provider=lambda: {"machine_id": "m-recon", "flows": []},
        status_interval=0.2,
    )

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await _wait_connected(client)
        assert client.connected

        # Drop the server; the client should notice and start retrying.
        await asyncio.get_event_loop().run_in_executor(
            None, _stop_server, server, thread
        )
        await _wait_connected(client, want=False)
        assert not client.connected

        # Bring a fresh server back up on the same port.
        server2, thread2 = await asyncio.get_event_loop().run_in_executor(
            None, _start_server, port
        )
        try:
            await _wait_connected(client, tries=400)
            assert client.connected, f"client did not reconnect: {client.last_error}"
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=8)
            _stop_server(server2, thread2)

    asyncio.run(scenario())
