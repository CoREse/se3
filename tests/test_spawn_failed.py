"""Tests for the SPAWN_FAILED feedback channel (G3).

A server-dispatched ``MSG_SPAWN_FLOW`` is answered ``202 dispatched`` by the
REST API immediately; the daemon spawns asynchronously. If that spawn — or the
``ensure_se3_project`` init that precedes it, or a resume — fails, the daemon
must no longer return silently: it reports the real error back to the server via
``MSG_SPAWN_FAILED`` so the web UI can surface it instead of leaving the task
stuck on the "published" pseudo-success state.

This module covers the three layers:

* ``protocol`` — the new message type + constructor;
* ``client._handle_spawn`` — the three failure paths emit SPAWN_FAILED;
* ``server.ws`` — an inbound SPAWN_FAILED is routed to the owner's UI clients.
"""

from __future__ import annotations

import asyncio

from tianluo.daemon import protocol
from tianluo.daemon.client import DaemonClient


# --------------------------------------------------------------------------
# protocol
# --------------------------------------------------------------------------


def test_make_spawn_failed_payload_and_registration():
    msg = protocol.make_spawn_failed(
        "/proj",
        "boom",
        task_description="Build X",
        from_issue_id="007",
        resume_flow_id="flow-1",
    )
    assert msg.type == protocol.MSG_SPAWN_FAILED
    assert msg.payload == {
        "project_root": "/proj",
        "error": "boom",
        "task_description": "Build X",
        "from_issue_id": "007",
        "resume_flow_id": "flow-1",
    }
    # Registered as a daemon -> server message and a known type, so decode()
    # accepts it and mixed-version peers can interoperate.
    assert protocol.MSG_SPAWN_FAILED in protocol.DAEMON_TO_SERVER
    assert protocol.MSG_SPAWN_FAILED in protocol.ALL_MESSAGE_TYPES
    # Round-trips through the wire.
    assert protocol.decode(msg.to_json()).type == protocol.MSG_SPAWN_FAILED


def test_make_spawn_failed_omits_empty_optionals():
    msg = protocol.make_spawn_failed("/proj", "boom")
    assert msg.payload == {"project_root": "/proj", "error": "boom"}
    assert "task_description" not in msg.payload
    assert "from_issue_id" not in msg.payload
    assert "resume_flow_id" not in msg.payload


# --------------------------------------------------------------------------
# client._handle_spawn — failure reporting
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


def _spawn_failed_frames(ws):
    return [m for m in ws.sent if m.type == protocol.MSG_SPAWN_FAILED]


def test_fresh_spawn_failure_reports_spawn_failed():
    def boom(*a, **k):
        raise RuntimeError("subprocess blew up")

    client = _make_client(spawn_handler=boom)
    ws = _FakeWS()

    asyncio.run(
        client._dispatch(
            ws, protocol.make_spawn_flow("Build X", project_root="/proj")
        )
    )

    frames = _spawn_failed_frames(ws)
    assert len(frames) == 1
    assert frames[0].payload["project_root"] == "/proj"
    assert "subprocess blew up" in frames[0].payload["error"]
    assert frames[0].payload["task_description"] == "Build X"


def test_ensure_error_reports_spawn_failed_and_skips_spawn():
    spawn_calls = []

    class _Ensure:
        error = "se3 init exited non-zero"

    client = _make_client(
        ensure_handler=lambda root: _Ensure(),
        spawn_handler=lambda *a, **k: spawn_calls.append(a),
    )
    ws = _FakeWS()

    asyncio.run(
        client._dispatch(
            ws, protocol.make_spawn_flow("T", project_root="/bad/path")
        )
    )

    assert spawn_calls == []  # spawn never runs on ensure error
    frames = _spawn_failed_frames(ws)
    assert len(frames) == 1
    assert frames[0].payload["project_root"] == "/bad/path"
    assert "se3 init exited non-zero" in frames[0].payload["error"]


def test_ensure_exception_reports_spawn_failed():
    def ensure_boom(root):
        raise OSError("disk full")

    client = _make_client(
        ensure_handler=ensure_boom,
        spawn_handler=lambda *a, **k: None,
    )
    ws = _FakeWS()

    asyncio.run(
        client._dispatch(
            ws, protocol.make_spawn_flow("T", project_root="/x")
        )
    )

    frames = _spawn_failed_frames(ws)
    assert len(frames) == 1
    assert "disk full" in frames[0].payload["error"]


def test_resume_failure_reports_spawn_failed():
    def resume_boom(flow_id, root):
        raise RuntimeError("no engine.json")

    client = _make_client(resume_handler=resume_boom)
    ws = _FakeWS()

    asyncio.run(
        client._dispatch(
            ws,
            protocol.make_spawn_flow(
                "", project_root="/proj", resume_flow_id="flow-9"
            ),
        )
    )

    frames = _spawn_failed_frames(ws)
    assert len(frames) == 1
    assert frames[0].payload["resume_flow_id"] == "flow-9"
    assert "no engine.json" in frames[0].payload["error"]


def test_successful_spawn_emits_no_spawn_failed():
    client = _make_client(spawn_handler=lambda *a, **k: None)
    ws = _FakeWS()

    asyncio.run(
        client._dispatch(
            ws, protocol.make_spawn_flow("Build X", project_root="/proj")
        )
    )

    assert _spawn_failed_frames(ws) == []


def test_empty_task_does_not_report_spawn_failed():
    """A pure input-validation drop never launched anything — no failure frame."""
    client = _make_client(spawn_handler=lambda *a, **k: None)
    ws = _FakeWS()

    asyncio.run(client._dispatch(ws, protocol.make_spawn_flow("   ")))

    assert _spawn_failed_frames(ws) == []


# --------------------------------------------------------------------------
# server.ws — routing an inbound SPAWN_FAILED to the owner's UI clients
# --------------------------------------------------------------------------


def test_handle_message_routes_spawn_failed_to_owner_ui():
    from tianluo.server.state import ServerState
    from tianluo.server.ws import UiHub, _handle_message

    class _UiWS:
        def __init__(self):
            self.sent = []

        async def send_text(self, data):
            import json

            self.sent.append(json.loads(data))

    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "6.4.0", owner_id="owner-A")
        hub = UiHub()
        mine = _UiWS()
        other = _UiWS()
        await hub.register(mine, "owner-A")
        await hub.register(other, "owner-B")

        msg = protocol.make_spawn_failed(
            "/proj", "could not launch", task_description="Build X"
        )
        await _handle_message(msg, state, "m1", hub)
        return mine, other

    mine, other = asyncio.run(scenario())

    # The owner of the reporting machine sees the failure; a different owner
    # never does.
    events = [m for m in mine.sent if m.get("type") == "spawn_failed"]
    assert len(events) == 1
    assert events[0]["machine_id"] == "m1"
    assert events[0]["project_root"] == "/proj"
    assert events[0]["error"] == "could not launch"
    assert events[0]["task_description"] == "Build X"
    assert not any(m.get("type") == "spawn_failed" for m in other.sent)
