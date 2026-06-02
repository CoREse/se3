"""Tests for the unified human-interaction channel (group G1).

Covers the protocol extensions (call-file kinds + ``MSG_INTERJECT_FLOW``),
the aggregator's enriched ``PendingCall`` parsing, the daemon client's
``INTERJECT_FLOW`` dispatch, and the server's ``/api/flows/{id}/interject``
endpoint.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from se3.daemon import protocol
from se3.daemon.aggregator import DaemonAggregator
from se3.daemon.client import DaemonClient
from se3.daemon.protocol import (
    CALL_KIND_CALL,
    CALL_KIND_CLI_CONFIRM,
    CALL_KIND_DISCOVERY_CONFIRM,
    CALL_KIND_INTERJECTION,
    CALL_KIND_RETRY_DECISION,
    CALL_KINDS,
    MSG_INTERJECT_FLOW,
    decode,
)


# --------------------------------------------------------------------------
# protocol: call-file kinds + MSG_INTERJECT_FLOW
# --------------------------------------------------------------------------


def test_call_kinds_set():
    assert CALL_KINDS == {
        CALL_KIND_CALL,
        CALL_KIND_INTERJECTION,
        CALL_KIND_RETRY_DECISION,
        CALL_KIND_CLI_CONFIRM,
        CALL_KIND_DISCOVERY_CONFIRM,
    }


def test_interject_flow_registered_server_to_daemon():
    assert MSG_INTERJECT_FLOW in protocol.SERVER_TO_DAEMON
    assert MSG_INTERJECT_FLOW in protocol.ALL_MESSAGE_TYPES
    assert MSG_INTERJECT_FLOW not in protocol.DAEMON_TO_SERVER


def test_make_interject_flow_payload():
    msg = protocol.make_interject_flow("flow-1", "also handle errors", project_root="/p")
    assert msg.type == MSG_INTERJECT_FLOW
    assert msg.payload == {
        "flow_id": "flow-1",
        "text": "also handle errors",
        "project_root": "/p",
    }


def test_make_interject_flow_round_trip_decode():
    msg = protocol.make_interject_flow("flow-2", "do X")
    decoded = decode(msg.to_json())
    assert decoded.type == MSG_INTERJECT_FLOW
    assert decoded.payload["flow_id"] == "flow-2"
    assert decoded.payload["text"] == "do X"


def test_unknown_message_type_still_rejected():
    raw = json.dumps({"type": "not_a_real_type", "payload": {}})
    with pytest.raises(protocol.ProtocolError):
        decode(raw)


# --------------------------------------------------------------------------
# aggregator: enriched PendingCall parsing
# --------------------------------------------------------------------------


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_enumerate_calls_parses_kind_and_display_fields(tmp_path):
    calls = tmp_path / "se3" / "calls"
    _write(
        calls / "retry_42.json",
        {
            "kind": "retry_decision",
            "prompt": "Step failed — retry?",
            "context": {"error": "boom"},
            "options": ["retry", "skip", "abort"],
            "step_id": "s5",
        },
    )

    [call] = DaemonAggregator()._enumerate_calls(tmp_path)
    assert call.kind == "retry_decision"
    assert call.prompt == "Step failed — retry?"
    assert call.context == {"error": "boom"}
    assert call.options == ["retry", "skip", "abort"]
    assert call.step_id == "s5"

    d = call.to_dict()
    assert d["kind"] == "retry_decision"
    assert d["prompt"] == "Step failed — retry?"
    assert d["context"] == {"error": "boom"}
    assert d["options"] == ["retry", "skip", "abort"]
    assert d["step_id"] == "s5"


def test_enumerate_calls_cli_confirm_kind(tmp_path):
    calls = tmp_path / "se3" / "calls"
    _write(calls / "cli_1.json", {"kind": "cli_confirm", "prompt": "Press 1 to confirm"})
    [call] = DaemonAggregator()._enumerate_calls(tmp_path)
    assert call.kind == CALL_KIND_CLI_CONFIRM
    assert call.prompt == "Press 1 to confirm"


def test_enumerate_calls_legacy_file_falls_back_to_call(tmp_path):
    """An old-style call file with no ``kind`` metadata reports kind=call."""
    calls = tmp_path / "se3" / "calls"
    _write(calls / "confirm_legacy.json", {"step_to_review_id": "s1"})

    [call] = DaemonAggregator()._enumerate_calls(tmp_path)
    assert call.kind == CALL_KIND_CALL
    assert call.prompt == ""
    assert call.context == {}
    assert call.options == []
    assert call.step_id is None


def test_enumerate_calls_unknown_kind_falls_back_to_call(tmp_path):
    calls = tmp_path / "se3" / "calls"
    _write(calls / "weird.json", {"kind": "totally_unknown", "prompt": "p"})
    [call] = DaemonAggregator()._enumerate_calls(tmp_path)
    assert call.kind == CALL_KIND_CALL


def test_enumerate_calls_non_json_file_does_not_error(tmp_path):
    calls = tmp_path / "se3" / "calls"
    calls.mkdir(parents=True)
    (calls / "broken.json").write_text("not json at all", encoding="utf-8")
    [call] = DaemonAggregator()._enumerate_calls(tmp_path)
    assert call.kind == CALL_KIND_CALL
    assert call.call_id == "broken"


def test_enumerate_calls_answered_still_skipped(tmp_path):
    """A call with a sibling .response file is still skipped regardless of kind."""
    calls = tmp_path / "se3" / "calls"
    _write(calls / "answered.json", {"kind": "interjection", "prompt": "p"})
    _write(calls / "answered.response", {"ok": True})
    assert DaemonAggregator()._enumerate_calls(tmp_path) == []


# --------------------------------------------------------------------------
# daemon client: MSG_INTERJECT_FLOW dispatch
# --------------------------------------------------------------------------


class _FakeWS:
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


def test_dispatch_interject_writes_request_file(tmp_path):
    client = _make_client()

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_interject_flow(
                "flow-9", "remember to add tests", project_root=str(tmp_path)
            ),
        )

    asyncio.run(scenario())
    requests_dir = tmp_path / "se3" / "calls"
    files = list(requests_dir.glob("interjection_*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["kind"] == "interjection"
    assert payload["text"] == "remember to add tests"
    assert payload["context"]["flow_id"] == "flow-9"


def test_dispatch_interject_resolves_root_from_snapshot(tmp_path):
    """When the payload carries no project_root, the snapshot is consulted."""
    client = _make_client(
        snapshot_provider=lambda: {
            "machine_id": "m1",
            "flows": [{"flow_id": "flow-7", "project_root": str(tmp_path)}],
        }
    )

    async def scenario():
        await client._dispatch(
            _FakeWS(), protocol.make_interject_flow("flow-7", "hello")
        )

    asyncio.run(scenario())
    files = list((tmp_path / "se3" / "calls").glob("interjection_*.json"))
    assert len(files) == 1


def test_dispatch_interject_unresolvable_root_does_not_crash(caplog):
    """An unresolvable flow logs a warning rather than crashing the client."""
    client = _make_client(snapshot_provider=lambda: {"machine_id": "m1", "flows": []})

    async def scenario():
        await client._dispatch(
            _FakeWS(), protocol.make_interject_flow("ghost-flow", "text")
        )

    asyncio.run(scenario())  # must not raise


def test_dispatch_interject_ignores_empty_text(tmp_path):
    client = _make_client()

    async def scenario():
        await client._dispatch(
            _FakeWS(),
            protocol.make_interject_flow("flow-9", "   ", project_root=str(tmp_path)),
        )

    asyncio.run(scenario())
    assert not list((tmp_path / "se3" / "calls").glob("interjection_*.json"))


# --------------------------------------------------------------------------
# server: POST /api/flows/{id}/interject
# --------------------------------------------------------------------------


@pytest.fixture()
def server_client():
    from fastapi.testclient import TestClient

    from _authsrv import authed_app, login

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client


def _snapshot(machine_id, flows):
    return {"machine_id": machine_id, "hostname": "h", "flows": flows}


def test_interject_endpoint_dispatches(server_client):
    with server_client.websocket_connect("/ws") as ws:
        ws.send_text(
            protocol.make_hello(
                "m1", "h", "6.4.0", key=server_client.app.state.test_daemon_key
            ).to_json()
        )
        protocol.decode(ws.receive_text())  # WELCOME
        ws.send_text(
            protocol.make_status_update(
                _snapshot(
                    "m1", [{"flow_id": "f1", "project_root": "/proj", "status": "running"}]
                )
            ).to_json()
        )
        for _ in range(50):
            if server_client.get("/api/flows/f1").status_code == 200:
                break

        resp = server_client.post(
            "/api/flows/f1/interject", json={"text": "also fix the typo"}
        )
        assert resp.status_code == 200
        assert resp.json()["flow_id"] == "f1"

        msg = protocol.decode(ws.receive_text())
        assert msg.type == MSG_INTERJECT_FLOW
        assert msg.payload["flow_id"] == "f1"
        assert msg.payload["text"] == "also fix the typo"
        assert msg.payload["project_root"] == "/proj"


def test_interject_endpoint_unknown_flow_404(server_client):
    resp = server_client.post("/api/flows/ghost/interject", json={"text": "x"})
    assert resp.status_code == 404


def test_interject_endpoint_rejects_empty_text(server_client):
    with server_client.websocket_connect("/ws") as ws:
        ws.send_text(
            protocol.make_hello(
                "m1", "h", "6.4.0", key=server_client.app.state.test_daemon_key
            ).to_json()
        )
        protocol.decode(ws.receive_text())
        ws.send_text(
            protocol.make_status_update(
                _snapshot("m1", [{"flow_id": "f1", "project_root": "/proj"}])
            ).to_json()
        )
        for _ in range(50):
            if server_client.get("/api/flows/f1").status_code == 200:
                break
        resp = server_client.post("/api/flows/f1/interject", json={"text": "   "})
        assert resp.status_code == 422


def test_interject_pending_call_fields_reach_snapshot(server_client):
    """A pending call's kind/prompt/options survive the machines snapshot."""
    with server_client.websocket_connect("/ws") as ws:
        ws.send_text(
            protocol.make_hello(
                "m1", "h", "6.4.0", key=server_client.app.state.test_daemon_key
            ).to_json()
        )
        protocol.decode(ws.receive_text())
        ws.send_text(
            protocol.make_status_update(
                _snapshot(
                    "m1",
                    [
                        {
                            "flow_id": "f1",
                            "pending_calls": [
                                {
                                    "call_id": "c1",
                                    "kind": "cli_confirm",
                                    "prompt": "Press 1",
                                    "options": ["1", "2"],
                                    "step_id": "s2",
                                }
                            ],
                        }
                    ],
                )
            ).to_json()
        )
        flow = None
        for _ in range(50):
            r = server_client.get("/api/flows/f1")
            if r.status_code == 200:
                flow = r.json()["flow"]
                break
        assert flow is not None
        call = flow["pending_calls"][0]
        assert call["kind"] == "cli_confirm"
        assert call["prompt"] == "Press 1"
        assert call["options"] == ["1", "2"]
        assert call["step_id"] == "s2"
