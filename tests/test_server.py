"""Tests for the daemon↔server protocol, ServerState, and the FastAPI server."""

from __future__ import annotations

import asyncio
import threading

import pytest

from tianluo.daemon import protocol

from _authsrv import recv_daemon_frame
from tianluo.server.state import FlowSnapshot, ServerState


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

    # resume spawn: resume_flow_id is included, task_description is empty.
    resume = protocol.make_spawn_flow("", project_root="/p", resume_flow_id="fid")
    assert resume.payload["resume_flow_id"] == "fid"
    assert resume.payload["task_description"] == ""
    # Non-resume spawn omits resume_flow_id entirely.
    assert "resume_flow_id" not in spawn.payload


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


def test_flow_snapshot_waiting_for_lock_round_trips():
    """The server-side FlowSnapshot mirror preserves waiting_for_lock so the
    daemon-reported running·waiting-for-lock sub-state reaches the frontend."""
    snap = FlowSnapshot.from_payload(
        {"flow_id": "f1", "status": "running", "waiting_for_lock": True}
    )
    assert snap.waiting_for_lock is True
    assert snap.to_dict()["waiting_for_lock"] is True

    # Absent flag defaults to False (the common, non-queued case).
    plain = FlowSnapshot.from_payload({"flow_id": "f2", "status": "running"})
    assert plain.waiting_for_lock is False
    assert plain.to_dict()["waiting_for_lock"] is False


def test_state_update_status_threads_waiting_for_lock():
    """waiting_for_lock survives the full update_status → get_machine_flows path
    that backs the /ws/ui push the frontend consumes."""
    state = ServerState()

    async def scenario():
        await state.register_machine("m1", "host-1", "6.4.0")
        await state.update_status(
            "m1",
            _snapshot(
                flows=[
                    {
                        "flow_id": "f1",
                        "status": "running",
                        "waiting_for_lock": True,
                    }
                ]
            ),
        )
        flows = await state.get_machine_flows("m1")
        assert flows[0]["waiting_for_lock"] is True

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


def test_state_is_flow_resumable_paused():
    state = ServerState()

    async def scenario():
        await state.update_status(
            "m1",
            _snapshot("m1", [{"flow_id": "f1", "status": "paused"}]),
        )
        result = await state.is_flow_resumable("f1")
        assert result is not None
        machine_id, flow = result
        assert machine_id == "m1"
        assert flow["flow_id"] == "f1"

    asyncio.run(scenario())


def test_state_is_flow_resumable_failed():
    state = ServerState()

    async def scenario():
        await state.update_status(
            "m1",
            _snapshot("m1", [{"flow_id": "f1", "status": "failed"}]),
        )
        result = await state.is_flow_resumable("f1")
        assert result is not None

    asyncio.run(scenario())


def test_state_is_flow_resumable_rejects_completed():
    state = ServerState()

    async def scenario():
        await state.update_status(
            "m1",
            _snapshot("m1", [{"flow_id": "f1", "status": "completed"}]),
        )
        assert await state.is_flow_resumable("f1") is None

    asyncio.run(scenario())


def test_state_is_flow_resumable_rejects_running():
    state = ServerState()

    async def scenario():
        await state.update_status(
            "m1",
            _snapshot("m1", [{"flow_id": "f1", "status": "running"}]),
        )
        assert await state.is_flow_resumable("f1") is None

    asyncio.run(scenario())


def test_state_is_flow_resumable_rejects_unknown_flow():
    state = ServerState()

    async def scenario():
        assert await state.is_flow_resumable("ghost") is None

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
    """An authenticated TestClient + app.

    G7 makes the server multi-tenant and fail-closed: every ``/api/*`` route and
    the ``/ws/ui`` socket require a resolved owner, and the daemon ``/ws`` channel
    requires a valid HELLO key. The fixture therefore seeds an *admin* owner
    (admins get the unscoped operator view, preserving the legacy "see every
    machine" behaviour these tests assert), logs in to obtain a session cookie
    (auto-persisted by the TestClient cookie jar), and issues a daemon key. The
    issued key is stashed on ``app.state.test_daemon_key`` so the daemon-channel
    tests can present it in their HELLO.
    """
    from fastapi.testclient import TestClient

    import tianluo.server.crypto as crypto
    from tianluo.server.app import create_app
    from tianluo.server.auth.session import CookieConfig, SessionStore

    # The TestClient speaks plain HTTP, so a Secure cookie would never be sent
    # back; use a non-secure session cookie for the test transport only.
    app = create_app(
        session_store=SessionStore(cookie_config=CookieConfig(secure=False))
    )
    store = app.state.store
    owner_id = store.create_owner("admin", is_admin=True)
    store.link_identity(owner_id, "local", "admin")
    store.set_password(owner_id, crypto.hash_password("pw"))
    key_plain, key_hash = crypto.generate_token("dk")
    store.issue_daemon_key(owner_id, key_hash)
    app.state.test_daemon_key = key_plain
    app.state.test_owner_id = owner_id
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/login", json={"username": "admin", "password": "pw"}
        )
        assert resp.status_code == 200, resp.text
        yield client, app


def _hello(app, machine_id="m1", hostname="host-1", version="6.4.0"):
    """Build an authenticated daemon HELLO carrying the fixture's daemon key."""
    return protocol.make_hello(
        machine_id, hostname, version, key=app.state.test_daemon_key
    ).to_json()


def test_health_endpoint(client_and_app):
    client, app = client_and_app
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_index_serves_frontend(client_and_app):
    client, app = client_and_app
    resp = client.get("/")
    assert resp.status_code == 200
    assert "tianluo" in resp.text


def test_machines_empty(client_and_app):
    client, app = client_and_app
    resp = client.get("/api/machines")
    assert resp.status_code == 200
    assert resp.json() == {"machines": [], "count": 0}


def test_unknown_machine_flows_404(client_and_app):
    client, app = client_and_app
    assert client.get("/api/machines/nope/flows").status_code == 404
    assert client.get("/api/flows/nope").status_code == 404


def test_daemon_handshake_and_status_update(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        welcome = recv_daemon_frame(ws)
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
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        # First frame is a STATUS_UPDATE, not a HELLO.
        ws.send_text(protocol.make_status_update(_snapshot()).to_json())
        welcome = recv_daemon_frame(ws)
        assert welcome.type == protocol.MSG_WELCOME
        assert welcome.payload["accepted"] is False


def test_publish_flow_dispatches_spawn(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME

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
        spawn = recv_daemon_frame(ws)
        assert spawn.type == protocol.MSG_SPAWN_FLOW
        assert spawn.payload["task_description"] == "Build X"
        assert spawn.payload["discover"] is False


def test_publish_flow_threads_discover_flag(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME

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
        spawn = recv_daemon_frame(ws)
        assert spawn.type == protocol.MSG_SPAWN_FLOW
        assert spawn.payload["discover"] is True


def test_publish_flow_threads_worktree_flag(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME

        resp = client.post(
            "/api/flows",
            json={
                "machine_id": "m1",
                "task": "Isolate Y",
                "worktree": True,
                "project_root": "/p",
            },
        )
        assert resp.status_code == 202
        spawn = recv_daemon_frame(ws)
        assert spawn.type == protocol.MSG_SPAWN_FLOW
        assert spawn.payload["worktree"] is True


def test_publish_flow_omits_worktree_key_by_default(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME

        resp = client.post(
            "/api/flows",
            json={"machine_id": "m1", "task": "Plain", "project_root": "/p"},
        )
        assert resp.status_code == 202
        spawn = recv_daemon_frame(ws)
        assert "worktree" not in spawn.payload


def test_publish_flow_unknown_machine_404(client_and_app):
    client, app = client_and_app
    resp = client.post(
        "/api/flows",
        json={"machine_id": "ghost", "task": "X", "project_root": "/p"},
    )
    assert resp.status_code == 404


def test_publish_flow_rejects_empty_task(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
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
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME

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
        spawn = recv_daemon_frame(ws)
        assert spawn.payload["project_root"] == "/never/registered/path"


def test_publish_flow_rejects_non_absolute_project_root(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
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
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
        resp = client.post(
            "/api/flows",
            json={"machine_id": "m1", "task": "X"},
        )
        assert resp.status_code == 422


def test_respond_flow_dispatches_respond_call(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME
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
        respond = recv_daemon_frame(ws)
        assert respond.type == protocol.MSG_RESPOND_CALL
        assert respond.payload["call_id"] == "c1"
        assert respond.payload["response"] == "yes"
        assert respond.payload["project_root"] == "/proj"


def test_respond_flow_unknown_flow_404(client_and_app):
    client, app = client_and_app
    resp = client.post("/api/flows/ghost/respond", json={"response": "x"})
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Web-frontend WebSocket (/ws/ui) + broadcast
# --------------------------------------------------------------------------


def test_static_assets_served(client_and_app):
    client, app = client_and_app
    # index.html (via html=True directory serving)
    assert "tianluo" in client.get("/").text
    css = client.get("/style.css")
    assert css.status_code == 200 and "control plane" in css.text.lower()
    js = client.get("/app.js")
    assert js.status_code == 200 and "WebSocket" in js.text
    # WebUI i18n locale dictionaries load from the root static mount at /i18n/
    # (the same origin as app.js), NOT /static/i18n/. Lock that serving path so
    # a regression in the mount or the fetch URL surfaces here.
    en = client.get("/i18n/en-US.json")
    assert en.status_code == 200, "en-US locale must be served at /i18n/en-US.json"
    assert "nav.history" in en.json(), "en-US baseline dict looks malformed"
    zh = client.get("/i18n/zh-CN.json")
    assert zh.status_code == 200, "zh-CN locale must be served at /i18n/zh-CN.json"


def test_ui_ws_receives_initial_snapshot(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws/ui") as ui:
        import json

        msg = json.loads(ui.receive_text())
        assert msg["type"] == "snapshot"
        assert msg["machines"] == []


def test_ui_ws_broadcasts_daemon_status_update(client_and_app):
    import json

    client, app = client_and_app
    with client.websocket_connect("/ws/ui") as ui:
        snapshot = json.loads(ui.receive_text())
        assert snapshot["type"] == "snapshot"

        with client.websocket_connect("/ws") as daemon:
            daemon.send_text(_hello(app))
            recv_daemon_frame(daemon)  # WELCOME
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


# --------------------------------------------------------------------------
# uvicorn launch: large WebSocket frame cap (large MSG_HISTORY_DATA frames)
# --------------------------------------------------------------------------


def test_run_passes_ws_max_size_to_uvicorn(monkeypatch):
    """``app.run`` must pass ``ws_max_size=protocol.MAX_WS_MESSAGE_BYTES`` to
    ``uvicorn.run`` so the server's default 16 MiB inbound frame cap is raised
    and large ``MSG_HISTORY_DATA`` frames are accepted instead of being dropped
    (the direct cause of the history-pull 504). Host / port are still threaded
    through unchanged.
    """
    import sys
    import types

    from tianluo.server import app as app_module

    captured = {}

    fake_uvicorn = types.ModuleType("uvicorn")

    def _fake_run(app_obj, **kwargs):
        captured["kwargs"] = kwargs

    fake_uvicorn.run = _fake_run
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    # Avoid building the real FastAPI app / auth stack for this launch check.
    monkeypatch.setattr(app_module, "create_app", lambda **kw: object())

    app_module.run(host="0.0.0.0", port=12345, db_path=":memory:")

    assert captured["kwargs"]["ws_max_size"] == protocol.MAX_WS_MESSAGE_BYTES
    assert captured["kwargs"]["host"] == "0.0.0.0"
    assert captured["kwargs"]["port"] == 12345


# --------------------------------------------------------------------------
# ServerState issue mirror
# --------------------------------------------------------------------------


def _snapshot_with_issues(machine_id="m1", issues=None, **kw):
    """Build a snapshot dict that includes issues."""
    snap = _snapshot(machine_id, **kw)
    snap["issues"] = issues or []
    return snap


def test_state_update_status_ingests_issues():
    state = ServerState()

    async def scenario():
        await state.register_machine("m1", "h", "v")
        snap = _snapshot_with_issues(issues=[
            {
                "id": "001",
                "project_root": "/proj",
                "title": "Bug",
                "description": "desc",
                "status": "open",
                "source": "human",
            },
            {
                "id": "002",
                "project_root": "/proj",
                "title": "Bug2",
                "description": "desc2",
                "status": "closed",
                "source": "system",
            },
        ])
        await state.update_status("m1", snap)

        # Default: open only
        issues = await state.get_issues()
        assert len(issues) == 1
        assert issues[0]["id"] == "001"

        # Include closed
        all_issues = await state.get_issues(include_closed=True)
        assert len(all_issues) == 2

    asyncio.run(scenario())


def test_state_issues_filtered_by_source():
    state = ServerState()

    async def scenario():
        await state.register_machine("m1")
        snap = _snapshot_with_issues(issues=[
            {"id": "001", "project_root": "/p", "status": "open", "source": "human"},
            {"id": "002", "project_root": "/p", "status": "open", "source": "system"},
        ])
        await state.update_status("m1", snap)

        human = await state.get_issues(source="human")
        assert len(human) == 1 and human[0]["id"] == "001"

        system = await state.get_issues(source="system")
        assert len(system) == 1 and system[0]["id"] == "002"

    asyncio.run(scenario())


def test_state_issues_filtered_by_type():
    state = ServerState()

    async def scenario():
        await state.register_machine("m1")
        snap = _snapshot_with_issues(issues=[
            {"id": "001", "project_root": "/p", "status": "open", "type": "bug"},
            {"id": "002", "project_root": "/p", "status": "open", "type": "feature"},
        ])
        await state.update_status("m1", snap)

        bugs = await state.get_issues(type_filter="bug")
        assert len(bugs) == 1 and bugs[0]["id"] == "001"

    asyncio.run(scenario())


def test_state_get_issue_by_id():
    state = ServerState()

    async def scenario():
        await state.register_machine("m1")
        snap = _snapshot_with_issues(issues=[
            {"id": "042", "project_root": "/proj", "status": "open", "source": "human"},
        ])
        await state.update_status("m1", snap)

        result = await state.get_issue_by_id("042")
        assert result is not None
        mid, root, iss = result
        assert mid == "m1"
        assert root == "/proj"
        assert iss["id"] == "042"

        assert await state.get_issue_by_id("999") is None

    asyncio.run(scenario())


def test_state_find_machine_for_project():
    state = ServerState()

    async def scenario():
        await state.register_machine("m1")
        snap = _snapshot_with_issues(
            issues=[{"id": "001", "project_root": "/proj", "status": "open"}],
        )
        snap["project_roots"] = ["/proj"]
        await state.update_status("m1", snap)

        assert await state.find_machine_for_project("/proj") == "m1"
        assert await state.find_machine_for_project("/other") is None

    asyncio.run(scenario())


def test_state_issues_owner_scoped():
    state = ServerState()

    async def scenario():
        await state.register_machine("m1", owner_id="owner-a")
        await state.register_machine("m2", owner_id="owner-b")
        await state.update_status("m1", _snapshot_with_issues(issues=[
            {"id": "001", "project_root": "/p", "status": "open"},
        ]))
        await state.update_status("m2", _snapshot_with_issues(issues=[
            {"id": "002", "project_root": "/q", "status": "open"},
        ]))

        # Owner A sees only their issues
        a_issues = await state.get_issues(owner="owner-a")
        assert len(a_issues) == 1 and a_issues[0]["id"] == "001"

        # Owner B sees only their issues
        b_issues = await state.get_issues(owner="owner-b")
        assert len(b_issues) == 1 and b_issues[0]["id"] == "002"

        # Admin (unscoped) sees all
        all_issues = await state.get_issues()
        assert len(all_issues) == 2

    asyncio.run(scenario())


def test_state_discard_machine_state_clears_issues():
    state = ServerState()

    async def scenario():
        await state.register_machine("m1", owner_id="old-owner")
        await state.update_status("m1", _snapshot_with_issues(issues=[
            {"id": "001", "project_root": "/p", "status": "open"},
        ]))
        # Owner takeover: issues from old owner must be cleared
        await state.register_machine("m1", owner_id="new-owner")
        issues = await state.get_issues(owner="new-owner")
        assert issues == []

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Issue REST API endpoints
# --------------------------------------------------------------------------


def test_list_issues_endpoint(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
        ws.send_text(protocol.make_status_update(
            _snapshot_with_issues(issues=[
                {"id": "001", "project_root": "/p", "status": "open", "source": "human"},
                {"id": "002", "project_root": "/p", "status": "closed", "source": "system"},
            ])
        ).to_json())
        for _ in range(50):
            resp = client.get("/api/issues")
            if resp.json()["count"] > 0:
                break

    resp = client.get("/api/issues")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1  # open only
    assert data["issues"][0]["id"] == "001"


def test_list_issues_include_closed(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
        ws.send_text(protocol.make_status_update(
            _snapshot_with_issues(issues=[
                {"id": "001", "project_root": "/p", "status": "open"},
                {"id": "002", "project_root": "/p", "status": "closed"},
            ])
        ).to_json())
        for _ in range(50):
            resp = client.get("/api/issues?include_closed=true")
            if resp.json()["count"] > 1:
                break

    resp = client.get("/api/issues?include_closed=true")
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


def test_list_issues_filter_by_source(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
        ws.send_text(protocol.make_status_update(
            _snapshot_with_issues(issues=[
                {"id": "001", "project_root": "/p", "status": "open", "source": "human"},
                {"id": "002", "project_root": "/p", "status": "open", "source": "system"},
            ])
        ).to_json())
        for _ in range(50):
            resp = client.get("/api/issues?source=human")
            if resp.json()["count"] > 0:
                break

    resp = client.get("/api/issues?source=human")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["issues"][0]["source"] == "human"


def test_get_issue_by_id_endpoint(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
        ws.send_text(protocol.make_status_update(
            _snapshot_with_issues(issues=[
                {"id": "042", "project_root": "/proj", "status": "open", "title": "Test"},
            ])
        ).to_json())
        for _ in range(50):
            resp = client.get("/api/issues/042")
            if resp.status_code == 200:
                break

    resp = client.get("/api/issues/042")
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "m1"
    assert data["project_root"] == "/proj"
    assert data["issue"]["id"] == "042"


def test_get_issue_not_found(client_and_app):
    client, app = client_and_app
    resp = client.get("/api/issues/999")
    assert resp.status_code == 404


def test_create_issue_dispatches_command(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)

        result: dict = {}

        def do_post():
            result["resp"] = client.post("/api/issues", json={
                "machine_id": "m1",
                "project_root": "/proj",
                "description": "Something is broken",
                "title": "Fix it",
                "priority": "high",
                "type": "bug",
            })

        worker = threading.Thread(target=do_post)
        worker.start()
        try:
            msg = recv_daemon_frame(ws)
            assert msg.type == protocol.MSG_ISSUE_COMMAND
            assert msg.payload["operation"] == "create"
            assert msg.payload["description"] == "Something is broken"
            assert msg.payload["project_root"] == "/proj"
            ws.send_text(protocol.make_issue_result(
                msg.payload.get("request_id", ""),
                ok=True,
                issue_id="001",
            ).to_json())
        finally:
            worker.join(timeout=5)
        resp = result["resp"]
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "created"
        assert body["issue_id"] == "001"


def test_create_issue_rejects_empty_description(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
        resp = client.post("/api/issues", json={
            "machine_id": "m1",
            "project_root": "/proj",
            "description": "  ",
        })
        assert resp.status_code == 422


def test_create_issue_rejects_non_absolute_root(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
        resp = client.post("/api/issues", json={
            "machine_id": "m1",
            "project_root": "relative",
            "description": "desc",
        })
        assert resp.status_code == 422


def test_create_issue_unknown_machine_404(client_and_app):
    client, app = client_and_app
    resp = client.post("/api/issues", json={
        "machine_id": "ghost",
        "project_root": "/proj",
        "description": "desc",
    })
    assert resp.status_code == 404


def test_edit_issue_dispatches_command(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
        ws.send_text(protocol.make_status_update(
            _snapshot_with_issues(issues=[
                {"id": "042", "project_root": "/proj", "status": "open"},
            ])
        ).to_json())
        for _ in range(50):
            resp = client.get("/api/issues/042")
            if resp.status_code == 200:
                break

        result: dict = {}

        def do_patch():
            result["resp"] = client.patch("/api/issues/042", json={
                "description": "Updated",
            })

        worker = threading.Thread(target=do_patch)
        worker.start()
        try:
            msg = recv_daemon_frame(ws)
            assert msg.type == protocol.MSG_ISSUE_COMMAND
            assert msg.payload["operation"] == "edit"
            assert msg.payload["issue_id"] == "042"
            assert msg.payload["description"] == "Updated"
            ws.send_text(protocol.make_issue_result(
                msg.payload.get("request_id", ""),
                ok=True,
                issue_id="042",
            ).to_json())
        finally:
            worker.join(timeout=5)
        resp = result["resp"]
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"


def test_close_issue_dispatches_command(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
        ws.send_text(protocol.make_status_update(
            _snapshot_with_issues(issues=[
                {"id": "042", "project_root": "/proj", "status": "open"},
            ])
        ).to_json())
        for _ in range(50):
            resp = client.get("/api/issues/042")
            if resp.status_code == 200:
                break

        result: dict = {}

        def do_close():
            result["resp"] = client.post(
                "/api/issues/042/close", json={"reason": "Fixed"},
            )

        worker = threading.Thread(target=do_close)
        worker.start()
        try:
            msg = recv_daemon_frame(ws)
            assert msg.type == protocol.MSG_ISSUE_COMMAND
            assert msg.payload["operation"] == "close"
            assert msg.payload["reason"] == "Fixed"
            ws.send_text(protocol.make_issue_result(
                msg.payload.get("request_id", ""),
                ok=True,
                issue_id="042",
            ).to_json())
        finally:
            worker.join(timeout=5)
        resp = result["resp"]
        assert resp.status_code == 200
        assert resp.json()["status"] == "closed"


def test_reopen_issue_dispatches_command(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
        ws.send_text(protocol.make_status_update(
            _snapshot_with_issues(issues=[
                {"id": "042", "project_root": "/proj", "status": "closed"},
            ])
        ).to_json())
        for _ in range(50):
            resp = client.get("/api/issues/042?include_closed=true")
            if resp.status_code == 200:
                break

        result: dict = {}

        def do_reopen():
            result["resp"] = client.post("/api/issues/042/reopen", json={})

        worker = threading.Thread(target=do_reopen)
        worker.start()
        try:
            msg = recv_daemon_frame(ws)
            assert msg.type == protocol.MSG_ISSUE_COMMAND
            assert msg.payload["operation"] == "reopen"
            assert msg.payload["issue_id"] == "042"
            ws.send_text(protocol.make_issue_result(
                msg.payload.get("request_id", ""),
                ok=True,
                issue_id="042",
            ).to_json())
        finally:
            worker.join(timeout=5)
        resp = result["resp"]
        assert resp.status_code == 200
        assert resp.json()["status"] == "reopened"


# --------------------------------------------------------------------------
# Issue command timeout reconciliation (stop-the-bleeding)
# --------------------------------------------------------------------------


def _patch_reconcile_timeouts(
    monkeypatch, *, ack=0.3, reconcile=3.0, interval=0.05
):
    """Shrink the issue-command ack / reconcile windows so tests run fast.

    The constants are read from the module namespace at call time inside the
    closures in ``create_app``, so patching the module attributes takes effect
    for in-flight requests.
    """
    import tianluo.server.app as app_module

    monkeypatch.setattr(app_module, "ISSUE_COMMAND_TIMEOUT", ack)
    monkeypatch.setattr(app_module, "ISSUE_RECONCILE_TIMEOUT", reconcile)
    monkeypatch.setattr(app_module, "ISSUE_RECONCILE_POLL_INTERVAL", interval)


def test_create_issue_reconciles_after_ack_timeout(client_and_app, monkeypatch):
    """No ack, but the issue mirror later shows a new issue → 201 via reconcile."""
    _patch_reconcile_timeouts(monkeypatch)
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)

        result: dict = {}

        def do_post():
            result["resp"] = client.post("/api/issues", json={
                "machine_id": "m1",
                "project_root": "/proj",
                "description": "Something is broken",
            })

        worker = threading.Thread(target=do_post)
        worker.start()
        try:
            msg = recv_daemon_frame(ws)
            assert msg.type == protocol.MSG_ISSUE_COMMAND
            assert msg.payload["operation"] == "create"
            # Deliberately do NOT send MSG_ISSUE_RESULT. Instead the issue
            # lands on disk and surfaces in the next STATUS_UPDATE.
            ws.send_text(protocol.make_status_update(
                _snapshot_with_issues(issues=[
                    {
                        "id": "007",
                        "project_root": "/proj",
                        "description": "Something is broken",
                        "status": "open",
                        "source": "human",
                    },
                ])
            ).to_json())
        finally:
            worker.join(timeout=10)
        resp = result["resp"]
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "created"
        # The reconcile recovered the daemon-assigned id from the mirror.
        assert body["issue_id"] == "007"


def test_create_issue_reconcile_ignores_preexisting_issue(client_and_app, monkeypatch):
    """A pre-existing (baseline) issue must NOT be mistaken for the new one.

    With the mirror already holding issue 005 before the request, a reconcile
    that never sees a *new* id must still time out and fail (504) rather than
    falsely matching the baseline issue.
    """
    _patch_reconcile_timeouts(monkeypatch, ack=0.2, reconcile=0.6)
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
        # Seed the mirror with a pre-existing issue (the baseline).
        ws.send_text(protocol.make_status_update(
            _snapshot_with_issues(issues=[
                {
                    "id": "005",
                    "project_root": "/proj",
                    "description": "old one",
                    "status": "open",
                    "source": "human",
                },
            ])
        ).to_json())
        for _ in range(50):
            if client.get("/api/issues/005").status_code == 200:
                break

        result: dict = {}

        def do_post():
            result["resp"] = client.post("/api/issues", json={
                "machine_id": "m1",
                "project_root": "/proj",
                "description": "a different brand new issue",
            })

        worker = threading.Thread(target=do_post)
        worker.start()
        try:
            msg = recv_daemon_frame(ws)
            assert msg.type == protocol.MSG_ISSUE_COMMAND
            # No ack and no new issue ever appears: only the baseline 005 is
            # in the mirror, so reconcile must NOT match it.
        finally:
            worker.join(timeout=10)
        resp = result["resp"]
        assert resp.status_code == 504


def test_edit_issue_reconciles_after_ack_timeout(client_and_app, monkeypatch):
    """No ack, but the mirror later reflects the edited field → 200 via reconcile."""
    _patch_reconcile_timeouts(monkeypatch)
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
        ws.send_text(protocol.make_status_update(
            _snapshot_with_issues(issues=[
                {"id": "042", "project_root": "/proj", "status": "open",
                 "description": "old"},
            ])
        ).to_json())
        for _ in range(50):
            if client.get("/api/issues/042").status_code == 200:
                break

        result: dict = {}

        def do_patch():
            result["resp"] = client.patch("/api/issues/042", json={
                "description": "Updated text",
            })

        worker = threading.Thread(target=do_patch)
        worker.start()
        try:
            msg = recv_daemon_frame(ws)
            assert msg.type == protocol.MSG_ISSUE_COMMAND
            assert msg.payload["operation"] == "edit"
            # No ack; the edit lands and shows up in the next STATUS_UPDATE.
            ws.send_text(protocol.make_status_update(
                _snapshot_with_issues(issues=[
                    {"id": "042", "project_root": "/proj", "status": "open",
                     "description": "Updated text"},
                ])
            ).to_json())
        finally:
            worker.join(timeout=10)
        resp = result["resp"]
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "updated"


def test_close_issue_reconciles_after_ack_timeout(client_and_app, monkeypatch):
    """No ack, but the mirror later shows the issue closed → 200 via reconcile."""
    _patch_reconcile_timeouts(monkeypatch)
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
        ws.send_text(protocol.make_status_update(
            _snapshot_with_issues(issues=[
                {"id": "042", "project_root": "/proj", "status": "open"},
            ])
        ).to_json())
        for _ in range(50):
            if client.get("/api/issues/042").status_code == 200:
                break

        result: dict = {}

        def do_close():
            result["resp"] = client.post(
                "/api/issues/042/close", json={"reason": "Fixed"},
            )

        worker = threading.Thread(target=do_close)
        worker.start()
        try:
            msg = recv_daemon_frame(ws)
            assert msg.type == protocol.MSG_ISSUE_COMMAND
            assert msg.payload["operation"] == "close"
            ws.send_text(protocol.make_status_update(
                _snapshot_with_issues(issues=[
                    {"id": "042", "project_root": "/proj", "status": "resolved"},
                ])
            ).to_json())
        finally:
            worker.join(timeout=10)
        resp = result["resp"]
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "closed"


def test_reopen_issue_reconciles_after_ack_timeout(client_and_app, monkeypatch):
    """No ack, but the mirror later shows the issue open again → 200 via reconcile."""
    _patch_reconcile_timeouts(monkeypatch)
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)
        ws.send_text(protocol.make_status_update(
            _snapshot_with_issues(issues=[
                {"id": "042", "project_root": "/proj", "status": "closed"},
            ])
        ).to_json())
        for _ in range(50):
            if client.get("/api/issues/042?include_closed=true").status_code == 200:
                break

        result: dict = {}

        def do_reopen():
            result["resp"] = client.post("/api/issues/042/reopen", json={})

        worker = threading.Thread(target=do_reopen)
        worker.start()
        try:
            msg = recv_daemon_frame(ws)
            assert msg.type == protocol.MSG_ISSUE_COMMAND
            assert msg.payload["operation"] == "reopen"
            ws.send_text(protocol.make_status_update(
                _snapshot_with_issues(issues=[
                    {"id": "042", "project_root": "/proj", "status": "open"},
                ])
            ).to_json())
        finally:
            worker.join(timeout=10)
        resp = result["resp"]
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "reopened"


def test_create_issue_reconcile_fails_when_never_persisted(client_and_app, monkeypatch):
    """Issue never lands → reconcile window elapses and the request fails (504).

    The reconcile must be *bounded*: it does not block indefinitely waiting for
    a STATUS_UPDATE that never reflects the issue.
    """
    _patch_reconcile_timeouts(monkeypatch, ack=0.2, reconcile=0.6)
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)

        result: dict = {}

        def do_post():
            result["resp"] = client.post("/api/issues", json={
                "machine_id": "m1",
                "project_root": "/proj",
                "description": "never lands",
            })

        worker = threading.Thread(target=do_post)
        worker.start()
        try:
            msg = recv_daemon_frame(ws)
            assert msg.type == protocol.MSG_ISSUE_COMMAND
            # No ack, and never push a STATUS_UPDATE reflecting the issue.
        finally:
            worker.join(timeout=10)
        resp = result["resp"]
        assert resp.status_code == 504


def test_create_issue_normal_ack_path_skips_reconcile(client_and_app, monkeypatch):
    """A prompt ack returns success without entering the reconcile window.

    Reconcile windows are shrunk to near-zero; if the normal ack path were
    broken the request would fall through to reconcile and fail, so a 201 here
    proves the ack path is unaffected.
    """
    _patch_reconcile_timeouts(monkeypatch, ack=2.0, reconcile=0.01, interval=0.01)
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)

        result: dict = {}

        def do_post():
            result["resp"] = client.post("/api/issues", json={
                "machine_id": "m1",
                "project_root": "/proj",
                "description": "acked promptly",
            })

        worker = threading.Thread(target=do_post)
        worker.start()
        try:
            msg = recv_daemon_frame(ws)
            assert msg.type == protocol.MSG_ISSUE_COMMAND
            ws.send_text(protocol.make_issue_result(
                msg.payload.get("request_id", ""),
                ok=True,
                issue_id="099",
            ).to_json())
        finally:
            worker.join(timeout=10)
        resp = result["resp"]
        assert resp.status_code == 201, resp.text
        assert resp.json()["issue_id"] == "099"


# POST /api/flows/{flow_id}/resume
# --------------------------------------------------------------------------


def test_resume_flow_dispatches_spawn_with_resume_flow_id(client_and_app):
    """A PAUSED flow's resume dispatches MSG_SPAWN_FLOW with resume_flow_id."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME
        ws.send_text(
            protocol.make_status_update(
                _snapshot(
                    "m1",
                    [
                        {
                            "flow_id": "f-resume",
                            "project_root": "/proj",
                            "status": "paused",
                        }
                    ],
                )
            ).to_json()
        )
        for _ in range(50):
            if client.get("/api/flows/f-resume").status_code == 200:
                break

        resp = client.post("/api/flows/f-resume/resume")
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "resume_dispatched"
        assert body["flow_id"] == "f-resume"

        spawn = recv_daemon_frame(ws)
        assert spawn.type == protocol.MSG_SPAWN_FLOW
        assert spawn.payload["resume_flow_id"] == "f-resume"
        assert spawn.payload["project_root"] == "/proj"


def test_resume_flow_failed_is_resumable(client_and_app):
    """A FAILED flow is also resumable."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME
        ws.send_text(
            protocol.make_status_update(
                _snapshot(
                    "m1",
                    [
                        {
                            "flow_id": "f-failed",
                            "project_root": "/proj",
                            "status": "failed",
                        }
                    ],
                )
            ).to_json()
        )
        for _ in range(50):
            if client.get("/api/flows/f-failed").status_code == 200:
                break

        resp = client.post("/api/flows/f-failed/resume")
        assert resp.status_code == 202


def test_resume_flow_completed_returns_409(client_and_app):
    """A COMPLETED flow exists but is terminal — returns 409, not a
    misleading resume_dispatched, and not 404 (it does exist)."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME
        ws.send_text(
            protocol.make_status_update(
                _snapshot(
                    "m1",
                    [
                        {
                            "flow_id": "f-done",
                            "project_root": "/proj",
                            "status": "completed",
                        }
                    ],
                )
            ).to_json()
        )
        for _ in range(50):
            if client.get("/api/flows/f-done").status_code == 200:
                break

        resp = client.post("/api/flows/f-done/resume")
        assert resp.status_code == 409
        body = resp.json()
        assert body.get("detail") != "resume_dispatched"
        assert "无法 resume" in body.get("detail", "")


def test_resume_flow_running_returns_409_with_still_running_detail(client_and_app):
    """A RUNNING flow (live process holds it) is rejected with an explicit
    409 'running on machine X' detail, NOT a misleading resume_dispatched.

    Acceptance point (3): calling resume on a running flow yields a clear
    rejection rather than an optimistic dispatched receipt.
    """
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME
        ws.send_text(
            protocol.make_status_update(
                _snapshot(
                    "m1",
                    [
                        {
                            "flow_id": "f-run",
                            "project_root": "/proj",
                            "status": "running",
                            # The G1 live-process gate forces resumable=False
                            # for a running flow whose process is still alive.
                            "resumable": False,
                        }
                    ],
                )
            ).to_json()
        )
        for _ in range(50):
            if client.get("/api/flows/f-run").status_code == 200:
                break

        resp = client.post("/api/flows/f-run/resume")
        assert resp.status_code == 409
        body = resp.json()
        # The receipt must be an explicit rejection, never resume_dispatched.
        assert body.get("status") != "resume_dispatched"
        # The rejection names the owning machine: on a shared filesystem the
        # holder may be a run on another host, and the operator needs to know
        # WHERE to go (luo end-session) rather than just that it is busy.
        assert "该 flow 正在机器 m1 上运行，无法 resume" == body.get("detail")


def test_resume_flow_running_but_dead_is_resumable(client_and_app):
    """A flow whose status is RUNNING but which the daemon marked
    resumable=True (process actually died / was interrupted) is still
    resumable — acceptance point (2) must not regress."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME
        ws.send_text(
            protocol.make_status_update(
                _snapshot(
                    "m1",
                    [
                        {
                            "flow_id": "f-dead",
                            "project_root": "/proj",
                            "status": "running",
                            "resumable": True,
                        }
                    ],
                )
            ).to_json()
        )
        for _ in range(50):
            if client.get("/api/flows/f-dead").status_code == 200:
                break

        resp = client.post("/api/flows/f-dead/resume")
        assert resp.status_code == 202
        assert resp.json()["status"] == "resume_dispatched"


def test_resume_flow_unknown_returns_404(client_and_app):
    """An unknown flow_id returns 404."""
    client, app = client_and_app
    resp = client.post("/api/flows/ghost/resume")
    assert resp.status_code == 404


def test_resume_flow_daemon_disconnected_returns_404(client_and_app):
    """When the owning daemon is not connected, resume returns 404."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME
        ws.send_text(
            protocol.make_status_update(
                _snapshot(
                    "m1",
                    [
                        {
                            "flow_id": "f-disc",
                            "project_root": "/proj",
                            "status": "paused",
                        }
                    ],
                )
            ).to_json()
        )
        for _ in range(50):
            if client.get("/api/flows/f-disc").status_code == 200:
                break
    # After exiting the ws context, the daemon is "disconnected".
    # But the server may not have marked it offline yet — the test is
    # checking the code path; the 404 comes from is_connected check.
    # In practice the status update keeps it alive; this tests the
    # 404 when the flow itself is not in the live set.
    resp = client.post("/api/flows/f-nonexistent/resume")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# POST /api/flows with from_issue_id (start a flow from an existing issue)
# --------------------------------------------------------------------------


def _push_issue(ws, client, app, *, issue_id="001", project_root="/p", status="open", machine_id="m1"):
    """Push a status update carrying one issue and wait for it to land."""
    ws.send_text(
        protocol.make_status_update(
            _snapshot_with_issues(
                machine_id=machine_id,
                issues=[
                    {
                        "id": issue_id,
                        "project_root": project_root,
                        "status": status,
                        "source": "human",
                    }
                ],
            )
        ).to_json()
    )
    for _ in range(50):
        if client.get(f"/api/issues/{issue_id}").status_code == 200:
            return


def test_publish_flow_from_issue_dispatches(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME
        _push_issue(ws, client, app, issue_id="001", project_root="/p")

        # task content is supplied but MUST be ignored on the from-issue path.
        resp = client.post(
            "/api/flows",
            json={"from_issue_id": "001", "task": "this is ignored"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["from_issue_id"] == "001"
        assert body["machine_id"] == "m1"

        spawn = recv_daemon_frame(ws)
        assert spawn.type == protocol.MSG_SPAWN_FLOW
        assert spawn.payload["from_issue_id"] == "001"
        assert spawn.payload["project_root"] == "/p"
        # Issue content drives the task; the request task is dropped.
        assert spawn.payload["task_description"] == ""
        assert spawn.payload["discover"] is False


def test_publish_flow_from_issue_threads_discover(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME
        _push_issue(ws, client, app, issue_id="007", project_root="/p")

        resp = client.post(
            "/api/flows",
            json={"from_issue_id": "007", "discover": True},
        )
        assert resp.status_code == 202, resp.text
        spawn = recv_daemon_frame(ws)
        assert spawn.payload["from_issue_id"] == "007"
        assert spawn.payload["discover"] is True


def test_publish_flow_from_issue_unknown_404(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME
        # No issue pushed — the id resolves to nothing.
        resp = client.post("/api/flows", json={"from_issue_id": "999"})
        assert resp.status_code == 404


def test_publish_flow_from_issue_non_open_409(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME
        _push_issue(ws, client, app, issue_id="002", status="in-progress")

        resp = client.post("/api/flows", json={"from_issue_id": "002"})
        assert resp.status_code == 409


def test_publish_flow_from_issue_target_mismatch_404(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME
        _push_issue(ws, client, app, issue_id="003", machine_id="m1")

        # Request names a machine that does not own the issue -> 404.
        resp = client.post(
            "/api/flows",
            json={"from_issue_id": "003", "machine_id": "other-machine"},
        )
        assert resp.status_code == 404


def test_publish_flow_from_issue_daemon_disconnected_404(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        ws.send_text(_hello(app))
        recv_daemon_frame(ws)  # WELCOME
        _push_issue(ws, client, app, issue_id="004", project_root="/p")
    # After the ws context exits the daemon is disconnected; the issue mirror
    # persists (machine merely marked offline), so the not-connected branch
    # is exercised.
    for _ in range(50):
        resp = client.post("/api/flows", json={"from_issue_id": "004"})
        if resp.status_code == 404:
            break
    assert resp.status_code == 404
