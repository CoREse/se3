"""Tests for the attachment-upload relay (``POST /api/uploads``).

The upload leg is a pure relay: the server never touches a byte of the file
beyond base64-ing it onto the daemon's socket, so everything worth asserting
here is about the *gates* it puts in front of that relay —

* the ownership gate (another owner's flow / machine reads as unknown);
* the size gate, which must fire on the declared ``Content-Length`` **before**
  the body is pulled into memory and again on the bytes actually received;
* the capability gate, which refuses a pre-revision-5 daemon up front rather
  than letting the browser's placeholder token hang until the ack times out;
* the ``request_id`` correlation, which is the only thing tying a daemon's
  ``UPLOAD_RESULT`` back to the parked REST call.

Every failure answers with a stable ``error_code`` at the body's *top level*:
that code (not the prose, and not the HTTP status alone) is what the web UI
localizes.
"""

from __future__ import annotations

import asyncio
import base64
import threading

import pytest

from _authsrv import authed_app, authed_hello, login, recv_daemon_frame
from tianluo.daemon import protocol
from tianluo.server import app as app_module
from tianluo.server.state import ServerState
from tianluo.server.ws import (
    ProjectCommandRegistry,
    UploadRequestRegistry,
    _handle_message,
)


PROJ = "/srv/projects/alpha"
FLOW = "f1"
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 10


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


def _connect_daemon(client, app, machine_id="m1", *, protocol_version=None):
    """Open an authenticated daemon socket; returns (ctx, sock).

    *protocol_version* overrides what the HELLO advertises so the capability
    gate can be exercised against a legacy daemon.
    """
    ctx = client.websocket_connect("/ws")
    sock = ctx.__enter__()
    hello = authed_hello(app, machine_id, "host", "6.4.0")
    if protocol_version is not None:
        message = protocol.decode(hello)
        message.payload["protocol_version"] = protocol_version
        hello = message.to_json()
    sock.send_text(hello)
    protocol.decode(sock.receive_text())  # WELCOME
    return ctx, sock


def _push_flow(client, sock, machine_id="m1", *, flows=None):
    """Push a STATUS_UPDATE and wait until the flow mirror reflects it."""
    sock.send_text(
        protocol.make_status_update(
            {
                "machine_id": machine_id,
                "hostname": "host",
                "flows": flows
                if flows is not None
                else [{"flow_id": FLOW, "status": "running", "project_root": PROJ}],
            }
        ).to_json()
    )
    for _ in range(200):
        if client.get(f"/api/flows/{FLOW}").status_code == 200:
            return
    raise AssertionError("flow mirror never reflected the pushed snapshot")


def _push_empty(client, sock, machine_id="m1"):
    """Push a flowless STATUS_UPDATE and wait until the machine is mirrored."""
    sock.send_text(
        protocol.make_status_update(
            {"machine_id": machine_id, "hostname": "host", "flows": []}
        ).to_json()
    )
    for _ in range(200):
        if client.get(f"/api/machines/{machine_id}/projects").status_code == 200:
            return
    raise AssertionError("machine never appeared in the mirror")


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


def _upload(client, content=PNG, **params):
    params.setdefault("filename", "shot.png")
    return client.post("/api/uploads", params=params, content=content)


def _round_trip(client, sock, call, *, reply):
    """Run *call* in a thread and answer its UPLOAD_COMMAND with *reply*."""
    thread, box = _call_in_thread(call)
    try:
        frame = recv_daemon_frame(sock)
        assert frame.type == protocol.MSG_UPLOAD_COMMAND
        sock.send_text(reply(frame).to_json())
    finally:
        thread.join(timeout=10)
    return frame, box["resp"]


# --------------------------------------------------------------------------
# UploadRequestRegistry / UPLOAD_RESULT routing
# --------------------------------------------------------------------------


def test_registry_resolves_registers_and_discards():
    async def scenario():
        registry = UploadRequestRegistry()
        fut = registry.register("r1")
        registry.resolve("r1", {"ok": True})
        assert await fut == {"ok": True}
        # A resolved id is forgotten, so a duplicate ack is a harmless no-op.
        registry.resolve("r1", {"ok": False})

        other = registry.register("r2")
        registry.discard("r2", other)
        registry.resolve("r2", {"ok": True})
        assert not other.done()
        assert registry._waiters == {}
        # Discarding an id that was never parked must not raise.
        registry.discard("never", other)

    asyncio.run(scenario())


def test_upload_and_project_registries_do_not_share_a_keyspace():
    """The reason uploads get their own registry: no cross-leg id collision.

    Both legs mint ids independently, so the same string can legitimately be
    in flight on each. A project ack must never resolve an upload waiter — that
    would hand the browser a "success" carrying no stored path at all.
    """

    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "6.4.0")
        uploads = UploadRequestRegistry()
        projects = ProjectCommandRegistry()
        upload_fut = uploads.register("same-id")
        project_fut = projects.register("same-id")

        await _handle_message(
            protocol.make_project_result("same-id", ok=True, project_root=PROJ),
            state,
            "m1",
            project_registry=projects,
            upload_registry=uploads,
        )
        assert project_fut.done()
        assert not upload_fut.done()

        await _handle_message(
            protocol.make_upload_result("same-id", ok=True, path="x/y.png", size=3),
            state,
            "m1",
            project_registry=projects,
            upload_registry=uploads,
        )
        assert (await upload_fut)["path"] == "x/y.png"

    asyncio.run(scenario())


def test_upload_result_resolves_and_touches_machine():
    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "6.4.0")
        state._machines["m1"].last_seen = 0.0
        registry = UploadRequestRegistry()
        fut = registry.register("r1")
        await _handle_message(
            protocol.make_upload_result(
                "r1", ok=True, path="tianluo/uploads/ab_shot.png", size=7
            ),
            state,
            "m1",
            upload_registry=registry,
        )
        assert (await fut)["path"] == "tianluo/uploads/ab_shot.png"
        # An upload changes no snapshot state, so this ack is the only liveness
        # evidence the frame produces — it must still refresh last-seen.
        assert state._machines["m1"].last_seen > 0.0

    asyncio.run(scenario())


def test_upload_result_without_registry_is_ignored():
    """A bare harness with no registry wired must not blow up the receive loop."""

    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "6.4.0")
        await _handle_message(
            protocol.make_upload_result("r1", ok=True, path="p", size=1), state, "m1"
        )

    asyncio.run(scenario())


def test_upload_result_without_request_id_is_ignored():
    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "6.4.0")
        registry = UploadRequestRegistry()
        fut = registry.register("")
        await _handle_message(
            protocol.make_upload_result("", ok=True, path="p", size=1),
            state,
            "m1",
            upload_registry=registry,
        )
        assert not fut.done()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# auth / ownership gates
# --------------------------------------------------------------------------


def test_upload_requires_auth():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as anon:
        resp = anon.post(
            "/api/uploads",
            params={"filename": "a.png", "machine_id": "m1", "project_root": PROJ},
            content=PNG,
        )
        assert resp.status_code == 401


def test_upload_404s_for_unknown_flow_and_machine(client_and_app):
    client, _app = client_and_app
    assert _upload(client, flow_id="nope").status_code == 404
    assert _upload(client, machine_id="nope", project_root=PROJ).status_code == 404


def test_upload_404s_across_owners(client_and_app):
    """Another owner's machine and flow read exactly like unknown ones."""
    from fastapi.testclient import TestClient

    import tianluo.server.crypto as crypto

    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_flow(client, sock)
        store = app.state.store
        eve = store.create_owner("eve", is_admin=False)
        store.link_identity(eve, "local", "eve")
        store.set_password(eve, crypto.hash_password("eve-pw"))
        with TestClient(app) as other:
            login(other, "eve", "eve-pw")
            assert _upload(other, flow_id=FLOW).status_code == 404
            assert (
                _upload(other, machine_id="m1", project_root=PROJ).status_code == 404
            )
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------
# request validation — rejected before any daemon contact
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "   "])
def test_upload_rejects_empty_filename(client_and_app, name):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        sent = _spy_on_downlink(app)
        resp = _upload(client, filename=name, machine_id="m1", project_root=PROJ)
        assert resp.status_code == 422
        assert sent == []
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"machine_id": "m1"},
        {"project_root": PROJ},
    ],
)
def test_upload_without_a_resolvable_target_is_422(client_and_app, params):
    client, _app = client_and_app
    resp = _upload(client, **params)
    assert resp.status_code == 422
    assert resp.json()["error_code"] == app_module.UPLOAD_ERR_NO_TARGET


@pytest.mark.parametrize("bad_root", ["relative/path", "./x", "~/proj"])
def test_upload_rejects_non_absolute_project_root(client_and_app, bad_root):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        sent = _spy_on_downlink(app)
        assert (
            _upload(client, machine_id="m1", project_root=bad_root).status_code == 422
        )
        assert sent == []
    finally:
        ctx.__exit__(None, None, None)


def test_upload_over_the_limit_is_413_without_a_downlink_frame(client_and_app):
    """The size gate fires on the declared length, before the body is read."""
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        sent = _spy_on_downlink(app)
        oversized = protocol.MAX_UPLOAD_BYTES + 1
        resp = client.post(
            "/api/uploads",
            params={"filename": "big.bin", "machine_id": "m1", "project_root": PROJ},
            content=b"\0" * oversized,
        )
        assert resp.status_code == 413
        assert resp.json()["error_code"] == protocol.UPLOAD_ERR_TOO_LARGE
        # Nothing may reach the daemon: refusing here is the whole point.
        assert sent == []
    finally:
        ctx.__exit__(None, None, None)


def test_upload_at_exactly_the_limit_is_dispatched(client_and_app):
    """The boundary is inclusive — MAX_UPLOAD_BYTES itself must go through."""
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        payload = b"\0" * protocol.MAX_UPLOAD_BYTES
        frame, resp = _round_trip(
            client,
            sock,
            lambda: client.post(
                "/api/uploads",
                params={
                    "filename": "big.bin",
                    "machine_id": "m1",
                    "project_root": PROJ,
                },
                content=payload,
            ),
            reply=lambda f: protocol.make_upload_result(
                f.payload["request_id"],
                ok=True,
                path="tianluo/uploads/ab_big.bin",
                size=protocol.MAX_UPLOAD_BYTES,
            ),
        )
        assert frame.payload["size"] == protocol.MAX_UPLOAD_BYTES
        assert resp.status_code == 201
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------
# connectivity / capability gates
# --------------------------------------------------------------------------


def test_upload_is_503_when_machine_is_disconnected(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    _push_flow(client, sock)
    ctx.__exit__(None, None, None)
    # The record survives the disconnect (it is the offline mirror), so the
    # ownership gate passes and the request fails on delivery, not on 404.
    for _ in range(200):
        resp = _upload(client, machine_id="m1", project_root=PROJ)
        if resp.status_code == 503:
            break
    assert resp.status_code == 503
    assert resp.json()["error_code"] == app_module.UPLOAD_ERR_NOT_CONNECTED


def test_upload_to_a_pre_revision_5_daemon_is_501(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app, protocol_version="4")
    try:
        _push_empty(client, sock)
        sent = _spy_on_downlink(app)
        resp = _upload(client, machine_id="m1", project_root=PROJ)
        assert resp.status_code == 501
        assert resp.json()["error_code"] == app_module.UPLOAD_ERR_UNSUPPORTED_DAEMON
        # A legacy daemon would drop the unknown frame silently; the whole point
        # of the gate is that the frame is never sent.
        assert sent == []
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------
# success round trips
# --------------------------------------------------------------------------


def test_upload_dispatches_recoverable_bytes_and_returns_201(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        frame, resp = _round_trip(
            client,
            sock,
            lambda: _upload(client, machine_id="m1", project_root=f"  {PROJ}  "),
            reply=lambda f: protocol.make_upload_result(
                f.payload["request_id"],
                ok=True,
                path="tianluo/uploads/0123456789ab_shot.png",
                size=len(PNG),
            ),
        )
        assert frame.payload["project_root"] == PROJ
        assert frame.payload["filename"] == "shot.png"
        assert frame.payload["size"] == len(PNG)
        assert frame.payload["request_id"]
        # The bytes survive the base64 hop unchanged — this endpoint must never
        # transcode or normalize a file's content.
        assert base64.b64decode(frame.payload["content_b64"]) == PNG

        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "stored"
        assert body["path"] == "tianluo/uploads/0123456789ab_shot.png"
        assert body["filename"] == "shot.png"
        assert body["size"] == len(PNG)
        assert body["machine_id"] == "m1"
        assert body["project_root"] == PROJ
        assert body["deduplicated"] is False
    finally:
        ctx.__exit__(None, None, None)


def test_upload_by_flow_id_resolves_machine_and_project_root(client_and_app):
    """The docked reply box names only the flow; the server supplies the rest."""
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_flow(client, sock)
        frame, resp = _round_trip(
            client,
            sock,
            lambda: _upload(client, flow_id=FLOW),
            reply=lambda f: protocol.make_upload_result(
                f.payload["request_id"],
                ok=True,
                path="tianluo/uploads/abc_shot.png",
                size=len(PNG),
                deduplicated=True,
            ),
        )
        assert frame.payload["project_root"] == PROJ
        assert resp.status_code == 201
        body = resp.json()
        assert body["machine_id"] == "m1"
        assert body["project_root"] == PROJ
        # A repeat paste of the same content is a success, not an error — the
        # flag is passed through so the UI can stay silent about it.
        assert body["deduplicated"] is True
    finally:
        ctx.__exit__(None, None, None)


def test_upload_by_flow_id_without_a_project_root_is_422(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_flow(
            client, sock, flows=[{"flow_id": FLOW, "status": "running"}]
        )
        sent = _spy_on_downlink(app)
        resp = _upload(client, flow_id=FLOW)
        assert resp.status_code == 422
        assert resp.json()["error_code"] == protocol.UPLOAD_ERR_INVALID_PATH
        assert sent == []
    finally:
        ctx.__exit__(None, None, None)


def test_empty_file_is_a_legal_upload(client_and_app):
    """A 0-byte file is a real answer, not an absent one."""
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        frame, resp = _round_trip(
            client,
            sock,
            lambda: _upload(client, b"", filename="empty.txt", machine_id="m1",
                            project_root=PROJ),
            reply=lambda f: protocol.make_upload_result(
                f.payload["request_id"],
                ok=True,
                path="tianluo/uploads/e3b0c44298fc_empty.txt",
                size=0,
            ),
        )
        assert frame.payload["size"] == 0
        assert resp.status_code == 201
        assert resp.json()["size"] == 0
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------
# correlation, failure mapping, timeout
# --------------------------------------------------------------------------


def test_mismatched_request_id_does_not_wake_the_waiter(client_and_app, monkeypatch):
    client, app = client_and_app
    monkeypatch.setattr(app_module, "UPLOAD_COMMAND_TIMEOUT", 0.5)
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        thread, box = _call_in_thread(
            lambda: _upload(client, machine_id="m1", project_root=PROJ)
        )
        try:
            frame = recv_daemon_frame(sock)
            assert frame.type == protocol.MSG_UPLOAD_COMMAND
            # An ack for a *different* upload must not resolve this one.
            sock.send_text(
                protocol.make_upload_result(
                    "some-other-id", ok=True, path="wrong.png", size=1
                ).to_json()
            )
        finally:
            thread.join(timeout=10)
        assert box["resp"].status_code == 504
    finally:
        ctx.__exit__(None, None, None)


def test_upload_times_out_to_504_and_leaves_no_waiter(client_and_app, monkeypatch):
    client, app = client_and_app
    monkeypatch.setattr(app_module, "UPLOAD_COMMAND_TIMEOUT", 0.2)
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        thread, box = _call_in_thread(
            lambda: _upload(client, machine_id="m1", project_root=PROJ)
        )
        try:
            frame = recv_daemon_frame(sock)
            assert frame.type == protocol.MSG_UPLOAD_COMMAND
        finally:
            thread.join(timeout=10)
        assert box["resp"].status_code == 504
        assert box["resp"].json()["error_code"] == app_module.UPLOAD_ERR_TIMEOUT
        # The parked future must be discarded on timeout, or a silent daemon
        # would leak one waiter per retry.
        assert app.state.upload_command_registry._waiters == {}
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize(
    "error_code,status",
    [
        (protocol.UPLOAD_ERR_TOO_LARGE, 413),
        (protocol.UPLOAD_ERR_NOT_REGISTERED, 409),
        (protocol.UPLOAD_ERR_INVALID_PATH, 422),
        (protocol.UPLOAD_ERR_INVALID_FILENAME, 422),
        (protocol.UPLOAD_ERR_INVALID_PAYLOAD, 422),
        (protocol.UPLOAD_ERR_UNSUPPORTED, 501),
        (protocol.UPLOAD_ERR_WRITE_FAILED, 500),
        # An unknown / absent code is an upstream fault, not a client error.
        ("", 502),
    ],
)
def test_daemon_error_codes_map_to_http_status(client_and_app, error_code, status):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        _frame, resp = _round_trip(
            client,
            sock,
            lambda: _upload(client, machine_id="m1", project_root=PROJ),
            reply=lambda f: protocol.make_upload_result(
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


def test_failure_body_falls_back_when_daemon_sends_no_prose(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        _frame, resp = _round_trip(
            client,
            sock,
            lambda: _upload(client, machine_id="m1", project_root=PROJ),
            reply=lambda f: protocol.make_upload_result(
                f.payload["request_id"],
                ok=False,
                error_code=protocol.UPLOAD_ERR_WRITE_FAILED,
            ),
        )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "upload failed on daemon"
    finally:
        ctx.__exit__(None, None, None)
