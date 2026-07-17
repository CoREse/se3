"""Tests for the end-session REST endpoint ``POST /api/flows/{id}/end``.

Covers the four honest-receipt branches the endpoint mirrors from the resume
gate — 404 (unknown / cross-owner), 409 (already completed), 503 (owning
daemon not connected / delivery failure), and 202 (dispatched) — and asserts
the 202 path actually dispatches an ``MSG_END_SESSION`` carrying the flow's
``project_root`` and ``reason``.
"""

from __future__ import annotations

import pytest

from se3.daemon import protocol

from _authsrv import authed_app, authed_hello, login, recv_daemon_frame  # noqa: E402


@pytest.fixture()
def client_and_app():
    """An authenticated (admin-owner) TestClient + app, like test_server.py."""
    from fastapi.testclient import TestClient

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


def _report_flow(ws, client, app, flow_id, status, project_root="/proj"):
    """Send a HELLO + STATUS_UPDATE for one flow and wait until it is visible."""
    ws.send_text(authed_hello(app))
    recv_daemon_frame(ws)  # WELCOME
    ws.send_text(
        protocol.make_status_update(
            _snapshot(
                "m1",
                [
                    {
                        "flow_id": flow_id,
                        "project_root": project_root,
                        "status": status,
                    }
                ],
            )
        ).to_json()
    )
    for _ in range(100):
        if client.get(f"/api/flows/{flow_id}").status_code == 200:
            return
    raise AssertionError(f"flow {flow_id} never became visible")


# --------------------------------------------------------------------------
# 404 — unknown flow
# --------------------------------------------------------------------------


def test_end_flow_unknown_returns_404(client_and_app):
    """An unknown flow_id returns 404 (leak nothing)."""
    client, _app = client_and_app
    resp = client.post("/api/flows/ghost/end")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# 404 — cross-owner isolation
# --------------------------------------------------------------------------


def test_end_flow_cross_owner_returns_404():
    """A flow owned by another owner reads as absent (404), never controllable."""
    from fastapi.testclient import TestClient

    import se3.server.crypto as crypto
    from se3.server.app import create_app
    from se3.server.auth.session import CookieConfig, SessionStore

    app = create_app(
        session_store=SessionStore(cookie_config=CookieConfig(secure=False))
    )
    store = app.state.store
    owners = {}
    for name in ("A", "B"):
        oid = store.create_owner(name, is_admin=False)
        store.link_identity(oid, "local", name)
        store.set_password(oid, crypto.hash_password("pw"))
        key_plain, key_hash = crypto.generate_token("dk")
        store.issue_daemon_key(oid, key_hash)
        owners[name] = key_plain

    def _hello(owner_name, machine_id):
        return protocol.make_hello(
            machine_id, "h", "6.4.0", key=owners[owner_name]
        ).to_json()

    with TestClient(app) as ca, TestClient(app) as cb:
        login(ca, "A", "pw")
        login(cb, "B", "pw")
        with ca.websocket_connect("/ws") as da, cb.websocket_connect("/ws") as db:
            da.send_text(_hello("A", "mA"))
            recv_daemon_frame(da)
            db.send_text(_hello("B", "mB"))
            recv_daemon_frame(db)
            db.send_text(
                protocol.make_status_update(
                    {
                        "machine_id": "mB",
                        "flows": [
                            {
                                "flow_id": "fB",
                                "project_root": "/pb",
                                "status": "running",
                            }
                        ],
                    }
                ).to_json()
            )
            for _ in range(100):
                if cb.get("/api/flows/fB").status_code == 200:
                    break
            # B owns fB and may end it (its daemon is connected → 202).
            assert cb.post("/api/flows/fB/end").status_code == 202
            recv_daemon_frame(db)  # END_SESSION drained
            # A cannot — cross-owner reads as absent (404), never 409/503.
            cross = ca.post("/api/flows/fB/end")
            assert cross.status_code == 404


# --------------------------------------------------------------------------
# 409 — already completed
# --------------------------------------------------------------------------


def test_end_flow_completed_returns_409(client_and_app):
    """A COMPLETED flow exists but is already done — 409, not a misleading
    end_dispatched, and not 404 (it does exist)."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        _report_flow(ws, client, app, "f-done", "completed")
        resp = client.post("/api/flows/f-done/end")
        assert resp.status_code == 409
        body = resp.json()
        assert body.get("status") != "end_dispatched"
        assert "无法 end" in body.get("detail", "")


def test_end_flow_completed_worktree_dispatches(client_and_app):
    """A COMPLETED *worktree* session whose follow-up cleanup failed leaves a
    dangling worktree on disk; the daemon still reports it live under its
    ``<main>/se3/worktrees/<name>`` root. It MUST stay endable so the orphan can
    be archived — 202, not the 409 an ordinary completed flow gets."""
    client, app = client_and_app
    wt_root = "/proj/se3/worktrees/wt_dangling"
    with client.websocket_connect("/ws") as ws:
        _report_flow(ws, client, app, "f-wt", "completed", project_root=wt_root)
        resp = client.post("/api/flows/f-wt/end", json={"reason": "cleanup"})
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "end_dispatched"
        assert body["flow_id"] == "f-wt"

        dispatched = recv_daemon_frame(ws)
        assert dispatched.type == protocol.MSG_END_SESSION
        assert dispatched.payload["flow_id"] == "f-wt"
        # The server forwards the worktree sandbox path; the daemon folds it back
        # to <main> before spawning ``se3 end-session``.
        assert dispatched.payload["project_root"] == wt_root


# --------------------------------------------------------------------------
# 503 — owning daemon not connected
# --------------------------------------------------------------------------


def test_end_flow_daemon_disconnected_returns_503(client_and_app):
    """A flow that is still visible but whose daemon has disconnected → 503."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        _report_flow(ws, client, app, "f-disc", "paused")
    # The ws context has exited; the daemon for m1 is now disconnected, but the
    # flow snapshot is retained (the machine is only marked offline). End must
    # therefore report 503 (machine not connected), not 202.
    for _ in range(100):
        resp = client.post("/api/flows/f-disc/end")
        if resp.status_code == 503:
            break
    assert resp.status_code == 503


# --------------------------------------------------------------------------
# 202 — dispatched (the happy path) for several non-completed statuses
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["running", "paused", "failed", "recovering", "init"])
def test_end_flow_dispatches_end_session(client_and_app, status):
    """A non-completed flow on a connected daemon dispatches MSG_END_SESSION
    carrying the flow's project_root and the request reason, returning 202."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        _report_flow(ws, client, app, "f-end", status, project_root="/proj")
        resp = client.post("/api/flows/f-end/end", json={"reason": "cleanup"})
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "end_dispatched"
        assert body["flow_id"] == "f-end"
        assert body["machine_id"] == "m1"
        assert body["reason"] == "cleanup"

        dispatched = recv_daemon_frame(ws)
        assert dispatched.type == protocol.MSG_END_SESSION
        assert dispatched.payload["flow_id"] == "f-end"
        assert dispatched.payload["project_root"] == "/proj"
        assert dispatched.payload["reason"] == "cleanup"


def test_end_flow_default_reason_when_body_omitted(client_and_app):
    """Omitting the request body still dispatches with a default reason."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as ws:
        _report_flow(ws, client, app, "f-noreason", "running")
        resp = client.post("/api/flows/f-noreason/end")
        assert resp.status_code == 202, resp.text
        assert resp.json()["reason"] == "user terminated"
        dispatched = recv_daemon_frame(ws)
        assert dispatched.type == protocol.MSG_END_SESSION
        assert dispatched.payload["reason"] == "user terminated"
