"""Tests for the daemon↔server protocol, ServerState, and the FastAPI server."""

from __future__ import annotations

import asyncio

import pytest

from se3.daemon import protocol
from se3.server.state import FlowSnapshot, ServerState


# --------------------------------------------------------------------------
# protocol
# --------------------------------------------------------------------------


def test_message_round_trip():
    msg = protocol.Message(type=protocol.MSG_HELLO, payload={"a": 1}, seq=7)
    restored = protocol.Message.from_json(msg.to_json())
    assert restored.type == protocol.MSG_HELLO
    assert restored.payload == {"a": 1}
    assert restored.seq == 7
    assert restored.timestamp == pytest.approx(msg.timestamp)


def test_decode_rejects_unknown_type():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode('{"type": "bogus", "payload": {}}')


def test_decode_rejects_malformed_json():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode("not json")


def test_decode_rejects_non_object_payload():
    with pytest.raises(protocol.ProtocolError):
        protocol.decode('{"type": "hello", "payload": 5}')


def test_message_directions_partition():
    assert protocol.MSG_HELLO in protocol.DAEMON_TO_SERVER
    assert protocol.MSG_SPAWN_FLOW in protocol.SERVER_TO_DAEMON
    assert protocol.DAEMON_TO_SERVER.isdisjoint(protocol.SERVER_TO_DAEMON)


def test_typed_constructors():
    hello = protocol.make_hello("m1", "host", "6.4.0")
    assert hello.type == protocol.MSG_HELLO
    assert hello.payload["machine_id"] == "m1"
    assert hello.payload["protocol_version"] == protocol.PROTOCOL_VERSION

    spawn = protocol.make_spawn_flow("do it", project_root="/p", task_type="bugfix")
    assert spawn.payload == {
        "task_description": "do it",
        "project_root": "/p",
        "task_type": "bugfix",
        "discover": False,
    }

    respond = protocol.make_respond_call("c1", {"ans": True}, project_root="/p")
    assert respond.payload["call_id"] == "c1"
    assert respond.payload["response"] == {"ans": True}

    assert protocol.make_ping(seq=3).seq == 3
    assert protocol.make_pong(seq=3).seq == 3


def test_encode_decode_helpers():
    raw = protocol.encode(protocol.MSG_PONG, {}, seq=2)
    msg = protocol.decode(raw)
    assert msg.type == protocol.MSG_PONG and msg.seq == 2


# --------------------------------------------------------------------------
# ServerState
# --------------------------------------------------------------------------


def _snapshot(machine_id="m1", flows=None):
    return {
        "machine_id": machine_id,
        "hostname": "host-1",
        "flows": flows if flows is not None else [],
        "pending_calls": [],
    }


def test_state_register_and_list():
    state = ServerState()

    async def scenario():
        await state.register_machine("m1", "host-1", "6.4.0")
        machines = await state.get_machines()
        assert len(machines) == 1
        assert machines[0]["machine_id"] == "m1"
        assert machines[0]["online"] is True
        assert "flows" not in machines[0]  # summary view omits nested flows

    asyncio.run(scenario())


def test_state_update_status_replaces_flows():
    state = ServerState()

    async def scenario():
        await state.register_machine("m1", "host-1", "6.4.0")
        await state.update_status(
            "m1",
            _snapshot(flows=[{"flow_id": "f1", "status": "running", "task_description": "T"}]),
        )
        flows = await state.get_machine_flows("m1")
        assert len(flows) == 1 and flows[0]["flow_id"] == "f1"

        # A second update fully replaces the flow set.
        await state.update_status("m1", _snapshot(flows=[{"flow_id": "f2"}]))
        flows = await state.get_machine_flows("m1")
        assert [f["flow_id"] for f in flows] == ["f2"]

    asyncio.run(scenario())


def test_state_get_flow_across_machines():
    state = ServerState()

    async def scenario():
        await state.update_status("m1", _snapshot("m1", [{"flow_id": "f1"}]))
        await state.update_status("m2", _snapshot("m2", [{"flow_id": "f2"}]))
        found = await state.get_flow("f2")
        assert found is not None
        machine_id, flow = found
        assert machine_id == "m2" and flow["flow_id"] == "f2"
        assert await state.get_flow("missing") is None
        assert await state.find_machine_for_flow("f1") == "m1"

    asyncio.run(scenario())


def test_state_update_status_records_project_roots():
    """ServerState surfaces the daemon's project_roots in /api/machines responses.

    The frontend's New Task modal reads `state.machines[*].project_roots` to
    populate the Project select; the field must round-trip through update_status
    and survive the no-flows summary view of MachineRecord.to_dict.
    """
    state = ServerState()

    async def scenario():
        # No project_roots in payload → defaults to [].
        await state.update_status("m1", _snapshot())
        machines = await state.get_machines()
        assert machines[0]["project_roots"] == []

        # Now report two project roots.
        snap = _snapshot()
        snap["project_roots"] = ["/proj/a", "/proj/b"]
        await state.update_status("m1", snap)
        machines = await state.get_machines()
        assert machines[0]["project_roots"] == ["/proj/a", "/proj/b"]

        # A subsequent update with no roots clears them.
        await state.update_status("m1", _snapshot())
        machines = await state.get_machines()
        assert machines[0]["project_roots"] == []

    asyncio.run(scenario())


def test_state_mark_offline():
    state = ServerState()

    async def scenario():
        await state.register_machine("m1")
        await state.mark_offline("m1")
        machines = await state.get_machines()
        assert machines[0]["online"] is False

    asyncio.run(scenario())


def test_state_unknown_machine_returns_none():
    state = ServerState()

    async def scenario():
        assert await state.get_machine("nope") is None
        assert await state.get_machine_flows("nope") is None

    asyncio.run(scenario())


def test_flow_snapshot_from_payload_defaults():
    flow = FlowSnapshot.from_payload({"project_root": "/p"})
    # flow_id falls back to project_root when absent.
    assert flow.flow_id == "/p"
    assert flow.status == "unknown"
    assert flow.pending_calls == []


# --------------------------------------------------------------------------
# FastAPI app + WebSocket endpoint
# --------------------------------------------------------------------------


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    from se3.server.app import create_app

    app = create_app()
    with TestClient(app) as client:
        yield client, app


def test_health_endpoint(client_and_app):
    client, _ = client_and_app
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_index_serves_frontend(client_and_app):
    client, _ = client_and_app
    resp = client.get("/")
    assert resp.status_code == 200
    assert "SE3" in resp.text


def test_machines_empty(client_and_app):
    client, _ = client_and_app
    resp = client.get("/api/machines")
    assert resp.status_code == 200
    assert resp.json() == {"machines": [], "count": 0}


def test_unknown_machine_flows_404(client_and_app):
    client, _ = client_and_app
    assert client.get("/api/machines/nope/flows").status_code == 404
    assert client.get("/api/flows/nope").status_code == 404


def test_daemon_handshake_and_status_update(client_and_app):
    client, _ = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(protocol.make_hello("m1", "host-1", "6.4.0").to_json())
        welcome = protocol.decode(ws.receive_text())
        assert welcome.type == protocol.MSG_WELCOME
        assert welcome.payload["accepted"] is True

        ws.send_text(
            protocol.make_status_update(
                _snapshot("m1", [{"flow_id": "f1", "status": "running"}])
            ).to_json()
        )
        # Poll the REST view until the update lands.
        for _ in range(50):
            machines = client.get("/api/machines").json()["machines"]
            if machines:
                break
        assert machines and machines[0]["machine_id"] == "m1"
        flow = client.get("/api/flows/f1").json()
        assert flow["machine_id"] == "m1"
        assert flow["flow"]["flow_id"] == "f1"


def test_bad_hello_is_rejected(client_and_app):
    client, _ = client_and_app
    with client.websocket_connect("/ws") as ws:
        # First frame is a STATUS_UPDATE, not a HELLO.
        ws.send_text(protocol.make_status_update(_snapshot()).to_json())
        welcome = protocol.decode(ws.receive_text())
        assert welcome.type == protocol.MSG_WELCOME
        assert welcome.payload["accepted"] is False


def test_publish_flow_dispatches_spawn(client_and_app):
    client, _ = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(protocol.make_hello("m1", "host-1", "6.4.0").to_json())
        protocol.decode(ws.receive_text())  # WELCOME

        resp = client.post(
            "/api/flows",
            json={
                "machine_id": "m1",
                "task": "Build X",
                "task_type": "feature",
                "project_root": "/p",
            },
        )
        assert resp.status_code == 202
        spawn = protocol.decode(ws.receive_text())
        assert spawn.type == protocol.MSG_SPAWN_FLOW
        assert spawn.payload["task_description"] == "Build X"
        assert spawn.payload["discover"] is False


def test_publish_flow_threads_discover_flag(client_and_app):
    client, _ = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(protocol.make_hello("m1", "host-1", "6.4.0").to_json())
        protocol.decode(ws.receive_text())  # WELCOME

        resp = client.post(
            "/api/flows",
            json={
                "machine_id": "m1",
                "task": "Explore Y",
                "discover": True,
                "project_root": "/p",
            },
        )
        assert resp.status_code == 202
        spawn = protocol.decode(ws.receive_text())
        assert spawn.type == protocol.MSG_SPAWN_FLOW
        assert spawn.payload["discover"] is True


def test_publish_flow_unknown_machine_404(client_and_app):
    client, _ = client_and_app
    resp = client.post(
        "/api/flows",
        json={"machine_id": "ghost", "task": "X", "project_root": "/p"},
    )
    assert resp.status_code == 404


def test_publish_flow_rejects_empty_task(client_and_app):
    client, _ = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(protocol.make_hello("m1", "host-1", "6.4.0").to_json())
        protocol.decode(ws.receive_text())
        resp = client.post(
            "/api/flows",
            json={"machine_id": "m1", "task": "  ", "project_root": "/p"},
        )
        assert resp.status_code == 422


def test_publish_flow_accepts_unknown_absolute_project_root(client_and_app):
    """A brand-new absolute path (not in machine.project_roots) is allowed.

    The owning daemon auto-runs `se3 init` on first use, so users can target
    a freshly typed directory directly from the web New Task form.
    """
    client, _ = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(protocol.make_hello("m1", "host-1", "6.4.0").to_json())
        protocol.decode(ws.receive_text())  # WELCOME

        # Note: '/never/registered/path' is not in any STATUS_UPDATE.
        resp = client.post(
            "/api/flows",
            json={
                "machine_id": "m1",
                "task": "Bootstrap a fresh project",
                "project_root": "/never/registered/path",
            },
        )
        assert resp.status_code == 202
        spawn = protocol.decode(ws.receive_text())
        assert spawn.payload["project_root"] == "/never/registered/path"


def test_publish_flow_rejects_non_absolute_project_root(client_and_app):
    client, _ = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(protocol.make_hello("m1", "host-1", "6.4.0").to_json())
        protocol.decode(ws.receive_text())
        resp = client.post(
            "/api/flows",
            json={
                "machine_id": "m1",
                "task": "X",
                "project_root": "relative/path",
            },
        )
        assert resp.status_code == 422


def test_publish_flow_rejects_empty_project_root(client_and_app):
    client, _ = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(protocol.make_hello("m1", "host-1", "6.4.0").to_json())
        protocol.decode(ws.receive_text())
        resp = client.post(
            "/api/flows",
            json={"machine_id": "m1", "task": "X"},
        )
        assert resp.status_code == 422


def test_respond_flow_dispatches_respond_call(client_and_app):
    client, _ = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(protocol.make_hello("m1", "host-1", "6.4.0").to_json())
        protocol.decode(ws.receive_text())  # WELCOME
        ws.send_text(
            protocol.make_status_update(
                _snapshot(
                    "m1",
                    [
                        {
                            "flow_id": "f1",
                            "project_root": "/proj",
                            "pending_calls": [{"call_id": "c1"}],
                        }
                    ],
                )
            ).to_json()
        )
        for _ in range(50):
            if client.get("/api/flows/f1").status_code == 200:
                break

        resp = client.post("/api/flows/f1/respond", json={"response": "yes"})
        assert resp.status_code == 200
        respond = protocol.decode(ws.receive_text())
        assert respond.type == protocol.MSG_RESPOND_CALL
        assert respond.payload["call_id"] == "c1"
        assert respond.payload["response"] == "yes"
        assert respond.payload["project_root"] == "/proj"


def test_respond_flow_unknown_flow_404(client_and_app):
    client, _ = client_and_app
    resp = client.post("/api/flows/ghost/respond", json={"response": "x"})
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Web-frontend WebSocket (/ws/ui) + broadcast
# --------------------------------------------------------------------------


def test_static_assets_served(client_and_app):
    client, _ = client_and_app
    # index.html (via html=True directory serving)
    assert "SE3" in client.get("/").text
    css = client.get("/style.css")
    assert css.status_code == 200 and "control plane" in css.text.lower()
    js = client.get("/app.js")
    assert js.status_code == 200 and "WebSocket" in js.text


def test_ui_ws_receives_initial_snapshot(client_and_app):
    client, _ = client_and_app
    with client.websocket_connect("/ws/ui") as ui:
        import json

        msg = json.loads(ui.receive_text())
        assert msg["type"] == "snapshot"
        assert msg["machines"] == []


def test_ui_ws_broadcasts_daemon_status_update(client_and_app):
    import json

    client, _ = client_and_app
    with client.websocket_connect("/ws/ui") as ui:
        snapshot = json.loads(ui.receive_text())
        assert snapshot["type"] == "snapshot"

        with client.websocket_connect("/ws") as daemon:
            daemon.send_text(protocol.make_hello("m1", "host-1", "6.4.0").to_json())
            protocol.decode(daemon.receive_text())  # WELCOME
            # Daemon connect triggers a broadcast to the UI client.
            on_connect = json.loads(ui.receive_text())
            assert on_connect["type"] == "status_update"

            daemon.send_text(
                protocol.make_status_update(
                    _snapshot("m1", [{"flow_id": "f1", "status": "running"}])
                ).to_json()
            )
            update = json.loads(ui.receive_text())
            assert update["type"] == "status_update"
            machines = update["machines"]
            assert machines and machines[0]["machine_id"] == "m1"
            assert machines[0]["flows"][0]["flow_id"] == "f1"
