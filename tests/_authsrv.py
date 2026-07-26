"""Shared helpers for authenticating against the multi-tenant se3 server in tests.

Since G7 the central server is multi-tenant and fail-closed: every ``/api/*``
route and the ``/ws/ui`` socket require a resolved owner, and the daemon ``/ws``
channel requires a valid HELLO key. These helpers seed an admin owner, issue a
daemon key, and configure a non-secure session cookie (the plain-HTTP TestClient
would never transmit a ``Secure`` cookie), so legacy server tests can keep
exercising the REST/WS surface with minimal change.

Admins get the unscoped operator view (they see every machine), which preserves
the pre-multi-tenant "see everything" behaviour the older tests assert.
"""

from __future__ import annotations

from typing import Tuple

from tianluo.daemon import protocol


def authed_app(**create_app_kwargs) -> Tuple[object, str]:
    """Build a server app pre-seeded with an admin owner and a daemon key.

    Returns ``(app, daemon_key)``. The app uses a non-secure session cookie so
    the plain-HTTP TestClient transmits it. ``app.state.test_daemon_key`` and
    ``app.state.test_owner_id`` are also set for convenience.
    """
    import tianluo.server.crypto as crypto
    from tianluo.server.app import create_app
    from tianluo.server.auth.session import CookieConfig, SessionStore

    app = create_app(
        session_store=SessionStore(cookie_config=CookieConfig(secure=False)),
        **create_app_kwargs,
    )
    store = app.state.store
    owner_id = store.create_owner("admin", is_admin=True)
    store.link_identity(owner_id, "local", "admin")
    store.set_password(owner_id, crypto.hash_password("pw"))
    key_plain, key_hash = crypto.generate_token("dk")
    store.issue_daemon_key(owner_id, key_hash)
    app.state.test_daemon_key = key_plain
    app.state.test_owner_id = owner_id
    return app, key_plain


def login(client, username: str = "admin", password: str = "pw"):
    """Log a TestClient in as the seeded admin; the cookie persists in the jar."""
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp


def authed_hello(
    app, machine_id: str = "m1", hostname: str = "host", version: str = "6.4.0"
) -> str:
    """Build an authenticated daemon HELLO carrying the app's issued daemon key."""
    return protocol.make_hello(
        machine_id, hostname, version, key=app.state.test_daemon_key
    ).to_json()


def recv_daemon_frame(sock):
    """Decode the next substantive server→daemon frame from a TestClient socket.

    Skips ``MSG_VIEWERS`` presence frames: since protocol revision 4 the server
    sends one right after every accepted v4 handshake, plus one on each UI
    0↔non-0 client-count edge, so tests asserting on a specific dispatched
    frame (SPAWN/RESPOND/INTERJECT/...) must read past them.
    """
    while True:
        msg = protocol.decode(sock.receive_text())
        if msg.type != protocol.MSG_VIEWERS:
            return msg
