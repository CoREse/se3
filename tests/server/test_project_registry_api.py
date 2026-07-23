"""Tests for the project-registry REST endpoints (G3).

``/api/machines/{id}/projects`` is a deliberately asymmetric trio:

* **GET** is a pure read of the STATUS_UPDATE mirror — it must never emit a
  downlink frame, so the dialog opens instantly and still renders the last
  known registry while the daemon is offline;
* **POST / DELETE** are downlink commands parked on a ``request_id`` future,
  so they own the delivery (503), timeout (504) and ``error_code``→status
  mapping behaviour.

Every verb shares one ownership gate: a machine owned by somebody else is
reported exactly like an unknown one, so these routes cannot be used to probe
the machine namespace.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from _authsrv import authed_app, authed_hello, login, recv_daemon_frame
from se3.daemon import protocol
from se3.server import app as app_module
from se3.server.state import MachineRecord, ServerState, _sanitize_registered_projects
from se3.server.ws import ProjectCommandRegistry, _handle_message


PROJ_A = "/srv/projects/alpha"
PROJ_B = "/srv/projects/beta-gone"


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


def _connect_daemon(client, app, machine_id="m1"):
    """Open an authenticated daemon socket; returns (ctx, sock)."""
    ctx = client.websocket_connect("/ws")
    sock = ctx.__enter__()
    sock.send_text(authed_hello(app, machine_id, "host", "6.4.0"))
    protocol.decode(sock.receive_text())  # WELCOME
    return ctx, sock


def _push_registry(client, sock, machine_id="m1", projects=None):
    """Push a STATUS_UPDATE carrying *projects* and wait until it is mirrored."""
    sock.send_text(
        protocol.make_status_update(
            {
                "machine_id": machine_id,
                "hostname": "host",
                "flows": [],
                "registered_projects": projects if projects is not None else [],
            }
        ).to_json()
    )
    for _ in range(200):
        resp = client.get(f"/api/machines/{machine_id}/projects")
        if resp.status_code == 200 and resp.json()["count"] == len(projects or []):
            return resp
    raise AssertionError("registry mirror never reflected the pushed snapshot")


def _spy_on_downlink(app):
    """Wrap the connection manager's send_to and record every dispatched frame."""
    manager = app.state.connection_manager
    sent = []
    original = manager.send_to

    async def spy(machine_id, message):
        sent.append((machine_id, message))
        return await original(machine_id, message)

    manager.send_to = spy  # type: ignore[method-assign]
    return sent


def _call_in_thread(fn):
    """Run a blocking REST call off the main thread; returns (thread, box)."""
    box: dict = {}

    def runner():
        box["resp"] = fn()

    thread = threading.Thread(target=runner)
    thread.start()
    return thread, box


# --------------------------------------------------------------------------
# ProjectCommandRegistry / PROJECT_RESULT routing
# --------------------------------------------------------------------------


def test_registry_resolves_registers_and_discards():
    async def scenario():
        registry = ProjectCommandRegistry()
        fut = registry.register("r1")
        registry.resolve("r1", {"ok": True})
        assert await fut == {"ok": True}
        # A resolved id is forgotten, so a duplicate ack is a harmless no-op
        # rather than a second set_result on a done future.
        registry.resolve("r1", {"ok": False})

        other = registry.register("r2")
        registry.discard("r2", other)
        registry.resolve("r2", {"ok": True})
        assert not other.done()
        assert registry._waiters == {}
        # Discarding an id that was never parked must not raise.
        registry.discard("never", other)

    asyncio.run(scenario())


def test_registry_wakes_every_waiter_on_one_id():
    async def scenario():
        registry = ProjectCommandRegistry()
        a = registry.register("r1")
        b = registry.register("r1")
        registry.resolve("r1", {"ok": True})
        assert await a == {"ok": True}
        assert await b == {"ok": True}

    asyncio.run(scenario())


def test_project_result_without_registry_is_ignored():
    """A bare harness with no registry wired must not blow up the receive loop."""

    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "6.4.0")
        message = protocol.make_project_result("r1", ok=True, project_root=PROJ_A)
        await _handle_message(message, state, "m1")

    asyncio.run(scenario())


def test_project_result_resolves_and_touches_machine():
    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "6.4.0")
        state._machines["m1"].last_seen = 0.0
        registry = ProjectCommandRegistry()
        fut = registry.register("r1")
        message = protocol.make_project_result("r1", ok=True, project_root=PROJ_A)
        await _handle_message(message, state, "m1", project_registry=registry)
        assert (await fut)["project_root"] == PROJ_A
        assert state._machines["m1"].last_seen > 0.0

    asyncio.run(scenario())


def test_project_result_without_request_id_is_ignored():
    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "6.4.0")
        registry = ProjectCommandRegistry()
        fut = registry.register("")
        await _handle_message(
            protocol.make_project_result("", ok=True),
            state,
            "m1",
            project_registry=registry,
        )
        assert not fut.done()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# snapshot mirroring / defensive normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [None, "not-a-list", 42, {"path": PROJ_A}],
)
def test_sanitize_rejects_non_list_payloads(raw):
    assert _sanitize_registered_projects(raw) == []


def test_sanitize_clips_entries_to_the_three_rendered_keys():
    out = _sanitize_registered_projects(
        [
            {"path": PROJ_A, "exists": True, "active": True, "secret": "leak"},
            {"path": PROJ_B, "exists": 0, "active": None},
            "not-a-dict",
            {"exists": True},          # no path — unrenderable
            {"path": "", "exists": True},
        ]
    )
    assert out == [
        {"path": PROJ_A, "exists": True, "active": True},
        {"path": PROJ_B, "exists": False, "active": False},
    ]


def test_machine_record_serializes_registered_projects():
    record = MachineRecord(machine_id="m1")
    assert record.to_dict()["registered_projects"] == []
    record.registered_projects = [{"path": PROJ_A, "exists": True, "active": False}]
    data = record.to_dict()
    assert data["registered_projects"] == [
        {"path": PROJ_A, "exists": True, "active": False}
    ]
    # A copy, not the live list — a caller mutating the response must not edit
    # the mirror.
    data["registered_projects"][0]["exists"] = False
    assert record.registered_projects[0]["exists"] is True


def test_status_update_replaces_then_clears_registered_projects():
    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "6.4.0")
        await state.update_status(
            "m1",
            {
                "machine_id": "m1",
                "registered_projects": [
                    {"path": PROJ_A, "exists": True, "active": True}
                ],
            },
        )
        assert state._machines["m1"].registered_projects == [
            {"path": PROJ_A, "exists": True, "active": True}
        ]
        # A daemon that has emptied its registry (or was downgraded) must clear
        # the mirror rather than leave a stale row behind.
        await state.update_status("m1", {"machine_id": "m1"})
        assert state._machines["m1"].registered_projects == []

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# GET — mirror read
# --------------------------------------------------------------------------


def test_get_projects_returns_mirror_with_exists_flags(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        sent = _spy_on_downlink(app)
        resp = _push_registry(
            client,
            sock,
            projects=[
                {"path": PROJ_A, "exists": True, "active": True},
                {"path": PROJ_B, "exists": False, "active": False},
            ],
        )
        body = resp.json()
        assert body["machine_id"] == "m1"
        assert body["count"] == 2
        assert body["projects"] == [
            {"path": PROJ_A, "exists": True, "active": True},
            {"path": PROJ_B, "exists": False, "active": False},
        ]
        # The whole point of serving from the mirror: reading the list must not
        # cost a daemon round trip.
        assert sent == []
    finally:
        ctx.__exit__(None, None, None)


def test_get_projects_empty_for_daemon_without_the_field(client_and_app):
    """A pre-field daemon's snapshot degrades to an empty list, not a 500."""
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        sock.send_text(
            protocol.make_status_update(
                {"machine_id": "m1", "hostname": "host", "flows": []}
            ).to_json()
        )
        for _ in range(200):
            resp = client.get("/api/machines/m1/projects")
            if resp.status_code == 200:
                break
        assert resp.status_code == 200
        assert resp.json()["projects"] == []
    finally:
        ctx.__exit__(None, None, None)


def test_projects_endpoints_404_for_unknown_machine(client_and_app):
    client, _app = client_and_app
    assert client.get("/api/machines/nope/projects").status_code == 404
    assert client.post(
        "/api/machines/nope/projects", json={"project_root": PROJ_A}
    ).status_code == 404
    assert client.request(
        "DELETE", "/api/machines/nope/projects", params={"project_root": PROJ_A}
    ).status_code == 404


def test_projects_endpoints_require_auth():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as anon:
        assert anon.get("/api/machines/m1/projects").status_code == 401
        assert anon.post(
            "/api/machines/m1/projects", json={"project_root": PROJ_A}
        ).status_code == 401
        assert anon.request(
            "DELETE", "/api/machines/m1/projects", params={"project_root": PROJ_A}
        ).status_code == 401


# --------------------------------------------------------------------------
# write validation — rejected before any daemon contact
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_root", ["", "   ", "relative/path", "./x", "~/proj"])
def test_write_rejects_non_absolute_root_without_contacting_daemon(
    client_and_app, bad_root
):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_registry(client, sock, projects=[])
        sent = _spy_on_downlink(app)
        assert (
            client.post(
                "/api/machines/m1/projects", json={"project_root": bad_root}
            ).status_code
            == 422
        )
        assert (
            client.request(
                "DELETE", "/api/machines/m1/projects", params={"project_root": bad_root}
            ).status_code
            == 422
        )
        assert sent == []
    finally:
        ctx.__exit__(None, None, None)


def test_delete_without_project_root_param_is_422(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_registry(client, sock, projects=[])
        assert client.delete("/api/machines/m1/projects").status_code == 422
    finally:
        ctx.__exit__(None, None, None)


def test_write_is_503_when_machine_is_disconnected(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    _push_registry(client, sock, projects=[{"path": PROJ_A, "exists": True, "active": True}])
    ctx.__exit__(None, None, None)
    # The record survives the disconnect (it is the offline mirror), so the
    # ownership gate passes and the request fails on delivery, not on 404.
    for _ in range(200):
        resp = client.post("/api/machines/m1/projects", json={"project_root": PROJ_A})
        if resp.status_code == 503:
            break
    assert resp.status_code == 503
    resp = client.request(
        "DELETE", "/api/machines/m1/projects", params={"project_root": PROJ_A}
    )
    assert resp.status_code == 503


def test_write_times_out_to_504_and_leaves_no_waiter(client_and_app, monkeypatch):
    client, app = client_and_app
    monkeypatch.setattr(app_module, "PROJECT_COMMAND_TIMEOUT", 0.2)
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_registry(client, sock, projects=[])
        thread, box = _call_in_thread(
            lambda: client.post(
                "/api/machines/m1/projects", json={"project_root": PROJ_A}
            )
        )
        try:
            frame = recv_daemon_frame(sock)
            assert frame.type == protocol.MSG_PROJECT_COMMAND
        finally:
            thread.join(timeout=10)
        assert box["resp"].status_code == 504
        # The parked future must be discarded on timeout, or a silent daemon
        # would leak one waiter per retry.
        assert app.state.project_command_registry._waiters == {}
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------
# write success / failure round trips
# --------------------------------------------------------------------------


def _round_trip(client, sock, call, *, reply):
    """Run *call* in a thread, answer its PROJECT_COMMAND with *reply*.

    *reply* takes the decoded downlink frame and returns the PROJECT_RESULT
    message to send back. Returns ``(downlink_frame, response)``.
    """
    thread, box = _call_in_thread(call)
    try:
        frame = recv_daemon_frame(sock)
        assert frame.type == protocol.MSG_PROJECT_COMMAND
        sock.send_text(reply(frame).to_json())
    finally:
        thread.join(timeout=10)
    return frame, box["resp"]


def test_post_registers_project_and_returns_normalized_root(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_registry(client, sock, projects=[])
        frame, resp = _round_trip(
            client,
            sock,
            lambda: client.post(
                "/api/machines/m1/projects",
                json={"project_root": f"{PROJ_A}/wt/feature  "},
            ),
            reply=lambda f: protocol.make_project_result(
                f.payload["request_id"], ok=True, project_root=PROJ_A
            ),
        )
        assert frame.payload["operation"] == protocol.PROJECT_OP_ADD
        # Whitespace is stripped server-side before the path leaves the box.
        assert frame.payload["project_root"] == f"{PROJ_A}/wt/feature"
        assert frame.payload["request_id"]
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "registered"
        assert body["machine_id"] == "m1"
        # The daemon's normalized (worktree-folded) root wins over what was typed.
        assert body["project_root"] == PROJ_A
    finally:
        ctx.__exit__(None, None, None)


def test_post_falls_back_to_requested_root_when_daemon_omits_it(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_registry(client, sock, projects=[])
        _frame, resp = _round_trip(
            client,
            sock,
            lambda: client.post(
                "/api/machines/m1/projects", json={"project_root": PROJ_A}
            ),
            reply=lambda f: protocol.make_project_result(
                f.payload["request_id"], ok=True
            ),
        )
        assert resp.status_code == 201
        assert resp.json()["project_root"] == PROJ_A
    finally:
        ctx.__exit__(None, None, None)


def test_delete_deregisters_project(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_registry(
            client, sock, projects=[{"path": PROJ_A, "exists": True, "active": False}]
        )
        frame, resp = _round_trip(
            client,
            sock,
            lambda: client.request(
                "DELETE",
                "/api/machines/m1/projects",
                params={"project_root": PROJ_A},
            ),
            reply=lambda f: protocol.make_project_result(
                f.payload["request_id"], ok=True, project_root=PROJ_A
            ),
        )
        assert frame.payload["operation"] == protocol.PROJECT_OP_REMOVE
        assert frame.payload["project_root"] == PROJ_A
        assert frame.payload["request_id"]
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "removed",
            "machine_id": "m1",
            "project_root": PROJ_A,
        }
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize(
    "error_code,status",
    [
        ("not_found", 404),
        ("not_registered", 404),
        ("not_a_directory", 422),
        ("invalid_path", 422),
        ("live_flow", 409),
        # A registry rewrite the daemon could not perform is a machine fault,
        # deliberately NOT a 404 "not registered" (the entry is still there).
        ("registry_error", 500),
        ("unsupported", 400),
        ("some_future_code", 400),
        ("", 400),
    ],
)
def test_daemon_error_codes_map_to_http_status(client_and_app, error_code, status):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_registry(client, sock, projects=[])
        _frame, resp = _round_trip(
            client,
            sock,
            lambda: client.post(
                "/api/machines/m1/projects", json={"project_root": PROJ_A}
            ),
            reply=lambda f: protocol.make_project_result(
                f.payload["request_id"],
                ok=False,
                error="daemon says no",
                error_code=error_code,
            ),
        )
        assert resp.status_code == status
        body = resp.json()
        # The code rides at the top level: it is the frontend's i18n key, and
        # burying it under ``detail`` would force the UI onto daemon English.
        assert body["error_code"] == error_code
        assert body["detail"] == "daemon says no"
    finally:
        ctx.__exit__(None, None, None)


def test_delete_live_flow_refusal_is_409(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_registry(
            client, sock, projects=[{"path": PROJ_A, "exists": True, "active": True}]
        )
        _frame, resp = _round_trip(
            client,
            sock,
            lambda: client.request(
                "DELETE",
                "/api/machines/m1/projects",
                params={"project_root": PROJ_A},
            ),
            reply=lambda f: protocol.make_project_result(
                f.payload["request_id"],
                ok=False,
                error="project has a live flow",
                error_code="live_flow",
            ),
        )
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "live_flow"
    finally:
        ctx.__exit__(None, None, None)


def test_failure_body_falls_back_when_daemon_sends_no_prose(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_registry(client, sock, projects=[])
        _frame, resp = _round_trip(
            client,
            sock,
            lambda: client.request(
                "DELETE",
                "/api/machines/m1/projects",
                params={"project_root": PROJ_A},
            ),
            reply=lambda f: protocol.make_project_result(
                f.payload["request_id"], ok=False, error_code="not_registered"
            ),
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "project removal failed on daemon"
    finally:
        ctx.__exit__(None, None, None)
