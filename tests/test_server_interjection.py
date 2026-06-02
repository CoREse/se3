"""Tests for the mid-flow interjection server endpoint and the no-TTY
failure path of ``se3 run``.

Two surfaces are covered:

* ``POST /api/flows/{id}/interject`` — pushes a fresh instruction into a
  running flow (hit / 404 flow-not-found / 503 machine-offline), and the
  existing ``POST /api/flows/{id}/respond`` resolving a *non-call* kind of
  pending call (a ``retry_decision``).
* ``se3 run``'s ``_resolve_step_failure_action`` — the failure handler that
  branches on whether stdin is an interactive terminal: with a TTY it defers
  to the operator prompt, without one it externalises the decision as a
  ``retry_decision`` call file.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from se3.daemon import protocol
from se3.engine import interaction_calls


# --------------------------------------------------------------------------
# POST /api/flows/{id}/interject  +  /respond on a non-call kind
# --------------------------------------------------------------------------


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    from _authsrv import authed_app, login

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


def _snapshot(machine_id="m1", flows=None):
    return {
        "machine_id": machine_id,
        "hostname": "host-1",
        "flows": flows if flows is not None else [],
        "pending_calls": [],
    }


def _register(client, app, ws, flows):
    """Send HELLO + STATUS_UPDATE on *ws* and wait for *flows* to land."""
    from _authsrv import authed_hello

    ws.send_text(authed_hello(app, "m1", "host-1", "6.4.0"))
    protocol.decode(ws.receive_text())  # WELCOME
    ws.send_text(protocol.make_status_update(_snapshot("m1", flows)).to_json())
    for _ in range(50):
        if client.get(f"/api/flows/{flows[0]['flow_id']}").status_code == 200:
            break


def test_interject_endpoint_dispatches(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        _register(
            client, app, ws,
            [{"flow_id": "f1", "status": "running", "project_root": "/p"}],
        )
        resp = client.post("/api/flows/f1/interject", json={"text": "add logging"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "dispatched"
        assert body["flow_id"] == "f1"
        # The daemon receives an INTERJECT_FLOW frame carrying the text.
        msg = protocol.decode(ws.receive_text())
        assert msg.type == protocol.MSG_INTERJECT_FLOW
        assert msg.payload["text"] == "add logging"
        assert msg.payload["flow_id"] == "f1"


def test_interject_unknown_flow_returns_404(client_and_app):
    client, _ = client_and_app
    resp = client.post("/api/flows/ghost/interject", json={"text": "do X"})
    assert resp.status_code == 404


def test_interject_empty_text_returns_422(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        _register(client, app, ws, [{"flow_id": "f1", "status": "running"}])
        resp = client.post("/api/flows/f1/interject", json={"text": "   "})
        assert resp.status_code == 422


def test_interject_offline_machine_returns_503(client_and_app):
    client, app = client_and_app
    # Register the flow, then drop the daemon connection. The flow stays in
    # ServerState but its owning machine is no longer connected.
    with client.websocket_connect("/ws") as ws:
        _register(client, app, ws, [{"flow_id": "f1", "status": "running"}])
    # The flow is still visible…
    assert client.get("/api/flows/f1").status_code == 200
    # …but interject cannot be delivered to an offline machine.
    resp = client.post("/api/flows/f1/interject", json={"text": "do X"})
    assert resp.status_code == 503


def test_respond_locates_non_call_kind_pending_call(client_and_app):
    """`/respond` must resolve a pending call of any kind, e.g. retry_decision."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        _register(
            client, app, ws,
            [
                {
                    "flow_id": "f1",
                    "status": "paused",
                    "project_root": "/p",
                    "pending_calls": [
                        {"call_id": "retry_decision_s1", "kind": "retry_decision"},
                    ],
                }
            ],
        )
        # No call_id supplied: the endpoint defaults to the first pending
        # call — here a retry_decision, not a plain `call`.
        resp = client.post(
            "/api/flows/f1/respond", json={"response": {"decision": "retry"}}
        )
        assert resp.status_code == 200
        assert resp.json()["call_id"] == "retry_decision_s1"
        msg = protocol.decode(ws.receive_text())
        assert msg.type == protocol.MSG_RESPOND_CALL
        assert msg.payload["call_id"] == "retry_decision_s1"
        assert msg.payload["response"] == {"decision": "retry"}


# --------------------------------------------------------------------------
# run.py: _resolve_step_failure_action — TTY vs no-TTY failure routing
# --------------------------------------------------------------------------


def _stub_failed_step(step_id="step-1", step_type="implement", retry_count=0):
    return SimpleNamespace(
        step_id=step_id,
        step_type=SimpleNamespace(value=step_type),
        retry_count=retry_count,
    )


def test_failure_with_tty_races_and_writes_retry_decision_call(tmp_path: Path):
    """With an interactive terminal the handler returns a ``race`` action and
    STILL writes a retry_decision call file, so a webui bystander sees the
    failure as a Retry/Skip/Abort chip while the CLI prompt is raced against
    the webui response."""
    from se3.commands import run

    flow = SimpleNamespace(flow_id="flow-1")
    step = _stub_failed_step()

    action, info = run._resolve_step_failure_action(
        tmp_path, flow, step, "kaboom", interactive=True
    )
    assert action == "race"
    call_path = Path(info)
    # The dual-channel pause writes the retry_decision call even on a TTY.
    assert call_path.exists()
    data = interaction_calls.read_call(call_path)
    assert data is not None
    assert data["kind"] == protocol.CALL_KIND_RETRY_DECISION


def test_failure_without_tty_writes_retry_decision_call(tmp_path: Path):
    """Off a terminal the handler externalises the decision as a call file."""
    from se3.commands import run

    flow = SimpleNamespace(flow_id="flow-1")
    step = _stub_failed_step(step_id="step-7", step_type="verify_spec")

    action, info = run._resolve_step_failure_action(
        tmp_path, flow, step, "kaboom", interactive=False
    )
    # With no response yet, the flow must pause for an out-of-band answer.
    assert action == "pause"
    call_path = Path(info)
    assert call_path.exists()
    assert call_path.name == "retry_decision_step-7.json"

    data = interaction_calls.read_call(call_path)
    assert data is not None
    assert data["kind"] == protocol.CALL_KIND_RETRY_DECISION
    assert data["context"]["flow_id"] == "flow-1"
    assert "kaboom" in data["prompt"]


def test_failure_without_tty_applies_existing_response(tmp_path: Path):
    """A retry_decision answered out-of-band resolves to that decision."""
    from se3.commands import run

    flow = SimpleNamespace(flow_id="flow-1")
    step = _stub_failed_step(step_id="step-7")

    # First pass: no response → pause and a call file is written.
    action, info = run._resolve_step_failure_action(
        tmp_path, flow, step, "kaboom", interactive=False
    )
    assert action == "pause"

    # An external responder answers the retry_decision call.
    interaction_calls.write_response(Path(info), {"decision": "skip"})

    # Second pass: the same (deterministic) call file now carries a response.
    action, decision = run._resolve_step_failure_action(
        tmp_path, flow, step, "kaboom", interactive=False
    )
    assert action == "decision"
    assert decision == "skip"


def test_stdin_is_interactive_reflects_isatty(monkeypatch):
    from se3.commands import run

    class _FakeStdin(io.StringIO):
        def __init__(self, tty):
            super().__init__()
            self._tty = tty

        def isatty(self):
            return self._tty

    monkeypatch.setattr(run.sys, "stdin", _FakeStdin(True))
    assert run._stdin_is_interactive() is True

    monkeypatch.setattr(run.sys, "stdin", _FakeStdin(False))
    assert run._stdin_is_interactive() is False

    monkeypatch.setattr(run.sys, "stdin", None)
    assert run._stdin_is_interactive() is False
