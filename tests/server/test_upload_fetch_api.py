"""Tests for the attachment read-back leg (``GET /api/uploads/file``).

The read-back endpoint is the inverse relay of ``POST /api/uploads``: the file
lives on the daemon's machine and the server never touches the filesystem, so
what is worth asserting here — as on the upload leg — is the *gates* in front of
the relay, plus the two things unique to this direction:

* the response headers, because the endpoint's whole performance story rests on
  the ``immutable`` cache directive being safe (the stored name carries a
  content-hash prefix) and on the type whitelist refusing to label
  operator-supplied bytes as anything a browser would render; and
* the byte fidelity, because these bytes make a round trip through base64 and
  must come back out identical.

Every failure answers with a stable ``error_code`` at the body's top level. On
this leg the browser turns *any* failure into "no thumbnail, plain path text",
so the codes exist for the operator-facing diagnostics path rather than for a
visible error — but they must still be there and still be stable.
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
from tianluo.server.ws import UploadRequestRegistry, _handle_message


PROJ = "/srv/projects/alpha"
FLOW = "f1"
REL = "tianluo/uploads/0123456789ab_shot.png"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


def _connect_daemon(client, app, machine_id="m1", *, protocol_version=None):
    """Open an authenticated daemon socket; returns (ctx, sock).

    *protocol_version* overrides what the HELLO advertises so the revision-6
    capability gate can be exercised against a legacy daemon.
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


def _fetch(client, **params):
    params.setdefault("path", REL)
    return client.get("/api/uploads/file", params=params)


def _ok_reply(frame, content=PNG):
    return protocol.make_fetch_result(
        frame.payload["request_id"],
        ok=True,
        content_b64=base64.b64encode(content).decode("ascii"),
        size=len(content),
        name=REL.rsplit("/", 1)[-1],
    )


def _round_trip(client, sock, call, *, reply):
    """Run *call* in a thread and answer its FETCH_COMMAND with *reply*."""
    thread, box = _call_in_thread(call)
    try:
        frame = recv_daemon_frame(sock)
        assert frame.type == protocol.MSG_FETCH_COMMAND
        sock.send_text(reply(frame).to_json())
    finally:
        thread.join(timeout=10)
    return frame, box["resp"]


# --------------------------------------------------------------------------
# FETCH_RESULT routing
# --------------------------------------------------------------------------


def test_fetch_result_resolves_the_parked_waiter():
    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "6.4.0")
        registry = UploadRequestRegistry()
        fut = registry.register("r1")
        await _handle_message(
            protocol.make_fetch_result("r1", ok=True, content_b64="QQ==", size=1),
            state,
            "m1",
            fetch_registry=registry,
        )
        assert (await fut)["content_b64"] == "QQ=="

    asyncio.run(scenario())


def test_fetch_and_upload_registries_do_not_share_a_keyspace():
    """The reason the fetch leg gets its own registry instance.

    Both legs mint ids independently, so the same string can legitimately be in
    flight on each. An upload ack resolving a fetch waiter would hand the
    browser an upload receipt in place of file bytes.
    """

    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "6.4.0")
        uploads = UploadRequestRegistry()
        fetches = UploadRequestRegistry()
        upload_fut = uploads.register("same-id")
        fetch_fut = fetches.register("same-id")

        await _handle_message(
            protocol.make_upload_result("same-id", ok=True, path=REL, size=3),
            state,
            "m1",
            upload_registry=uploads,
            fetch_registry=fetches,
        )
        assert upload_fut.done()
        assert not fetch_fut.done()

        await _handle_message(
            protocol.make_fetch_result("same-id", ok=True, content_b64="Qg==", size=1),
            state,
            "m1",
            upload_registry=uploads,
            fetch_registry=fetches,
        )
        assert (await fetch_fut)["content_b64"] == "Qg=="

    asyncio.run(scenario())


def test_fetch_result_without_registry_or_request_id_is_ignored():
    """A bare harness (no registry) and an id-less reply must both be no-ops."""

    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "6.4.0")
        await _handle_message(
            protocol.make_fetch_result("r1", ok=True, content_b64="", size=0),
            state,
            "m1",
        )
        registry = UploadRequestRegistry()
        fut = registry.register("")
        await _handle_message(
            protocol.make_fetch_result("", ok=True, content_b64="", size=0),
            state,
            "m1",
            fetch_registry=registry,
        )
        assert not fut.done()

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# auth / ownership gates
# --------------------------------------------------------------------------


def test_fetch_requires_auth():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as anon:
        resp = anon.get(
            "/api/uploads/file",
            params={"path": REL, "machine_id": "m1", "project_root": PROJ},
        )
        assert resp.status_code == 401


def test_fetch_404s_for_unknown_flow_and_machine(client_and_app):
    client, _app = client_and_app
    assert _fetch(client, flow_id="nope").status_code == 404
    assert _fetch(client, machine_id="nope", project_root=PROJ).status_code == 404


def test_fetch_404s_across_owners(client_and_app):
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
            sent = _spy_on_downlink(app)
            assert _fetch(other, flow_id=FLOW).status_code == 404
            assert (
                _fetch(other, machine_id="m1", project_root=PROJ).status_code == 404
            )
            # The ownership gate must refuse before anything reaches the daemon:
            # a dispatched frame would read the other owner's file even though
            # the bytes never make it back to this caller.
            assert sent == []
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------
# request validation — rejected before any daemon contact
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"machine_id": "m1"},
        {"project_root": PROJ},
    ],
)
def test_fetch_without_a_resolvable_target_is_422(client_and_app, params):
    client, _app = client_and_app
    resp = _fetch(client, **params)
    assert resp.status_code == 422
    assert resp.json()["error_code"] == app_module.UPLOAD_ERR_NO_TARGET


@pytest.mark.parametrize("bad_path", ["", "   "])
def test_fetch_without_a_path_is_422(client_and_app, bad_path):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        sent = _spy_on_downlink(app)
        resp = _fetch(client, path=bad_path, machine_id="m1", project_root=PROJ)
        assert resp.status_code == 422
        assert resp.json()["error_code"] == protocol.FETCH_ERR_INVALID_PATH
        assert sent == []
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize(
    "bad_path",
    ["/etc/passwd", "tianluo/uploads/../../etc/passwd", "../secrets.png"],
)
def test_fetch_rejects_malformed_paths_before_the_wire(client_and_app, bad_path):
    """The cheap early reject: a frame the daemon would refuse never goes out.

    This is NOT the containment boundary — that lives in the daemon, on the
    resolved path — but an absolute or ``..``-bearing path is refusable here for
    free, and doing so keeps the obviously-hostile shapes off the socket.
    """
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        sent = _spy_on_downlink(app)
        resp = _fetch(client, path=bad_path, machine_id="m1", project_root=PROJ)
        assert resp.status_code == 422
        assert resp.json()["error_code"] == protocol.FETCH_ERR_INVALID_PATH
        assert sent == []
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize("bad_root", ["relative/path", "./x", "~/proj"])
def test_fetch_rejects_non_absolute_project_root(client_and_app, bad_root):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        sent = _spy_on_downlink(app)
        assert _fetch(client, machine_id="m1", project_root=bad_root).status_code == 422
        assert sent == []
    finally:
        ctx.__exit__(None, None, None)


def test_fetch_by_flow_id_without_a_project_root_is_422(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_flow(client, sock, flows=[{"flow_id": FLOW, "status": "running"}])
        sent = _spy_on_downlink(app)
        resp = _fetch(client, flow_id=FLOW)
        assert resp.status_code == 422
        assert resp.json()["error_code"] == protocol.UPLOAD_ERR_INVALID_PATH
        assert sent == []
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------
# connectivity / capability gates
# --------------------------------------------------------------------------


def test_fetch_is_503_when_machine_is_disconnected(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    _push_flow(client, sock)
    ctx.__exit__(None, None, None)
    # The record survives the disconnect (it is the offline mirror), so the
    # ownership gate passes and the request fails on delivery, not on 404.
    for _ in range(200):
        resp = _fetch(client, machine_id="m1", project_root=PROJ)
        if resp.status_code == 503:
            break
    assert resp.status_code == 503
    assert resp.json()["error_code"] == app_module.UPLOAD_ERR_NOT_CONNECTED


def test_fetch_from_a_pre_revision_6_daemon_is_501(client_and_app):
    """A daemon speaking revision 5 has uploads but no read-back channel."""
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app, protocol_version="5")
    try:
        _push_empty(client, sock)
        sent = _spy_on_downlink(app)
        resp = _fetch(client, machine_id="m1", project_root=PROJ)
        assert resp.status_code == 501
        assert resp.json()["error_code"] == app_module.UPLOAD_ERR_UNSUPPORTED_DAEMON
        # A legacy daemon drops the unknown frame silently, so every thumbnail
        # would otherwise hold a connection open for the full ack window. The
        # gate's whole purpose is that the frame is never sent.
        assert sent == []
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------
# success round trips
# --------------------------------------------------------------------------


def test_fetch_returns_the_exact_bytes_with_cache_and_type_headers(client_and_app):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        frame, resp = _round_trip(
            client,
            sock,
            lambda: _fetch(client, machine_id="m1", project_root=f"  {PROJ}  "),
            reply=_ok_reply,
        )
        assert frame.payload["project_root"] == PROJ
        assert frame.payload["path"] == REL
        assert frame.payload["request_id"]

        assert resp.status_code == 200
        # Byte fidelity across the base64 hop is the entire contract of this
        # endpoint — a transcoded image is a corrupted one.
        assert resp.content == PNG
        assert resp.headers["content-type"] == "image/png"
        assert "immutable" in resp.headers["cache-control"]
        assert "max-age=31536000" in resp.headers["cache-control"]
        # The bytes are one owner's private attachment behind an owner gate, so
        # the long life must be scoped to the requesting browser: a shared cache
        # allowed to store this would replay it to unauthenticated requests for
        # the same URL and the owner check would never run.
        assert "private" in resp.headers["cache-control"]
        assert "public" not in resp.headers["cache-control"]
        assert "Cookie" in resp.headers["vary"]
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["content-disposition"] == "inline"
    finally:
        ctx.__exit__(None, None, None)


def test_both_target_forms_reach_the_same_file(client_and_app):
    """flow_id and machine_id+project_root must resolve to one identical fetch."""
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_flow(client, sock)
        by_flow, first = _round_trip(
            client, sock, lambda: _fetch(client, flow_id=FLOW), reply=_ok_reply
        )
        by_pair, second = _round_trip(
            client,
            sock,
            lambda: _fetch(client, machine_id="m1", project_root=PROJ),
            reply=_ok_reply,
        )
        assert by_flow.payload["project_root"] == by_pair.payload["project_root"] == PROJ
        assert by_flow.payload["path"] == by_pair.payload["path"] == REL
        assert first.content == second.content == PNG
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize(
    "rel,expected",
    [
        ("tianluo/uploads/ab_a.png", "image/png"),
        ("tianluo/uploads/ab_a.JPG", "image/jpeg"),
        ("tianluo/uploads/ab_a.jpeg", "image/jpeg"),
        ("tianluo/uploads/ab_a.gif", "image/gif"),
        ("tianluo/uploads/ab_a.webp", "image/webp"),
        # Not whitelisted: served as an opaque download, never as a document.
        # SVG in particular is script-bearing and must not render same-origin.
        ("tianluo/uploads/ab_a.svg", "application/octet-stream"),
        ("tianluo/uploads/ab_a.html", "application/octet-stream"),
        ("tianluo/uploads/ab_a.txt", "application/octet-stream"),
        ("se3/uploads/ab_noext", "application/octet-stream"),
    ],
)
def test_content_type_comes_from_the_extension_whitelist(client_and_app, rel, expected):
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        _frame, resp = _round_trip(
            client,
            sock,
            lambda: _fetch(client, path=rel, machine_id="m1", project_root=PROJ),
            reply=lambda f: _ok_reply(f, b"<svg/>"),
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == expected
        assert resp.headers["x-content-type-options"] == "nosniff"
    finally:
        ctx.__exit__(None, None, None)


def test_empty_file_is_a_legal_fetch(client_and_app):
    """A 0-byte stored file is a real answer, not a missing one."""
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        _frame, resp = _round_trip(
            client,
            sock,
            lambda: _fetch(client, machine_id="m1", project_root=PROJ),
            reply=lambda f: _ok_reply(f, b""),
        )
        assert resp.status_code == 200
        assert resp.content == b""
    finally:
        ctx.__exit__(None, None, None)


# --------------------------------------------------------------------------
# correlation, failure mapping, timeout
# --------------------------------------------------------------------------


def test_mismatched_request_id_does_not_wake_the_waiter(client_and_app, monkeypatch):
    client, app = client_and_app
    monkeypatch.setattr(app_module, "FETCH_COMMAND_TIMEOUT", 0.5)
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        thread, box = _call_in_thread(
            lambda: _fetch(client, machine_id="m1", project_root=PROJ)
        )
        try:
            frame = recv_daemon_frame(sock)
            assert frame.type == protocol.MSG_FETCH_COMMAND
            # A reply for a *different* fetch must not resolve this one.
            sock.send_text(
                protocol.make_fetch_result(
                    "some-other-id", ok=True, content_b64="QQ==", size=1
                ).to_json()
            )
        finally:
            thread.join(timeout=10)
        assert box["resp"].status_code == 504
    finally:
        ctx.__exit__(None, None, None)


def test_fetch_times_out_to_504_and_leaves_no_waiter(client_and_app, monkeypatch):
    client, app = client_and_app
    monkeypatch.setattr(app_module, "FETCH_COMMAND_TIMEOUT", 0.2)
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        thread, box = _call_in_thread(
            lambda: _fetch(client, machine_id="m1", project_root=PROJ)
        )
        try:
            frame = recv_daemon_frame(sock)
            assert frame.type == protocol.MSG_FETCH_COMMAND
        finally:
            thread.join(timeout=10)
        assert box["resp"].status_code == 504
        assert box["resp"].json()["error_code"] == app_module.UPLOAD_ERR_TIMEOUT
        # A fetch fires once per thumbnail per render, so a leaked waiter here
        # would grow the registry for as long as a conversation stays open.
        assert app.state.fetch_command_registry._waiters == {}
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize(
    "error_code,status",
    [
        (protocol.FETCH_ERR_INVALID_PATH, 422),
        (protocol.FETCH_ERR_NOT_REGISTERED, 409),
        (protocol.FETCH_ERR_NOT_FOUND, 404),
        (protocol.FETCH_ERR_TOO_LARGE, 413),
        (protocol.FETCH_ERR_UNSUPPORTED, 501),
        (protocol.FETCH_ERR_READ_FAILED, 500),
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
            lambda: _fetch(client, machine_id="m1", project_root=PROJ),
            reply=lambda f: protocol.make_fetch_result(
                f.payload["request_id"],
                ok=False,
                error="daemon says no",
                error_code=error_code,
            ),
        )
        assert resp.status_code == status
        body = resp.json()
        # The code rides at the top level: it is the frontend's i18n key.
        assert body["error_code"] == error_code
        assert body["detail"] == "daemon says no"
        # A failed read must never be cached — the file may appear later.
        assert "immutable" not in resp.headers.get("cache-control", "")
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
            lambda: _fetch(client, machine_id="m1", project_root=PROJ),
            reply=lambda f: protocol.make_fetch_result(
                f.payload["request_id"],
                ok=False,
                error_code=protocol.FETCH_ERR_NOT_FOUND,
            ),
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "fetch failed on daemon"
    finally:
        ctx.__exit__(None, None, None)


def test_undecodable_payload_from_daemon_is_502(client_and_app):
    """An ok=true reply whose body is not base64 is an upstream fault."""
    client, app = client_and_app
    ctx, sock = _connect_daemon(client, app)
    try:
        _push_empty(client, sock)
        _frame, resp = _round_trip(
            client,
            sock,
            lambda: _fetch(client, machine_id="m1", project_root=PROJ),
            reply=lambda f: protocol.make_fetch_result(
                f.payload["request_id"], ok=True, content_b64="not base64!!", size=3
            ),
        )
        assert resp.status_code == 502
        assert resp.json()["error_code"] == protocol.FETCH_ERR_READ_FAILED
    finally:
        ctx.__exit__(None, None, None)
