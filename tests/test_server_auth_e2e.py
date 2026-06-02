"""End-to-end tests for the server's auth endpoints (G7 task 1).

Covers ``POST /api/auth/login`` / ``logout`` / ``GET /api/auth/me`` /
``POST /api/auth/breakglass`` over a real FastAPI ``TestClient``: session
cookie issuance + secure attributes, login rate-limiting / lockout, break-glass
one-time consumption, fail-closed assembly, and the hard rule that no
credential (password / token / session id) is ever written to the logs.
"""

from __future__ import annotations

import logging

import pytest

from _authsrv import authed_app, login

from se3.server.auth.ratelimit import LoginRateLimiter, RateLimitConfig


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as client:
        yield client, app


# -- login / me / logout ----------------------------------------------------


def test_login_sets_session_cookie_and_me(client_and_app):
    client, _ = client_and_app
    resp = login(client)
    body = resp.json()
    assert body["is_admin"] is True
    assert body["provider"] == "local"
    # A session cookie was set with the secure attributes the SessionStore
    # declares (HttpOnly + SameSite). ``Secure`` is off only because the test
    # transport is plain HTTP (see _authsrv); the attributes are still applied.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "se3_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["owner_id"] == client_and_app[1].state.test_owner_id


def test_me_requires_authentication(client_and_app):
    client, _ = client_and_app
    # No login -> 401, never anonymous.
    assert client.get("/api/auth/me").status_code == 401


def test_login_bad_password_is_401(client_and_app):
    client, _ = client_and_app
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "wrong"}
    )
    assert resp.status_code == 401
    # No session cookie handed out on a failed login.
    assert "se3_session=" not in resp.headers.get("set-cookie", "")


def test_login_unknown_user_is_401(client_and_app):
    client, _ = client_and_app
    resp = client.post(
        "/api/auth/login", json={"username": "nobody", "password": "pw"}
    )
    assert resp.status_code == 401


def test_logout_destroys_session(client_and_app):
    client, _ = client_and_app
    login(client)
    assert client.get("/api/auth/me").status_code == 200
    out = client.post("/api/auth/logout")
    assert out.status_code == 200
    # The session is gone; subsequent authed calls fail closed.
    assert client.get("/api/auth/me").status_code == 401


def test_logout_is_idempotent_without_session(client_and_app):
    client, _ = client_and_app
    # Logout with no active session still succeeds (idempotent).
    assert client.post("/api/auth/logout").status_code == 200


# -- rate limiting / lockout ------------------------------------------------


def test_login_rate_limit_locks_out():
    from fastapi.testclient import TestClient

    limiter = LoginRateLimiter(
        RateLimitConfig(max_failures=2, lockout_seconds=300, window_seconds=900)
    )
    app, _key = authed_app(rate_limiter=limiter)
    with TestClient(app) as client:
        # Two failures trip the lockout...
        for _ in range(2):
            assert (
                client.post(
                    "/api/auth/login", json={"username": "admin", "password": "no"}
                ).status_code
                == 401
            )
        # ...the next attempt is refused with 429 before the store is consulted,
        # and carries a Retry-After header.
        locked = client.post(
            "/api/auth/login", json={"username": "admin", "password": "no"}
        )
        assert locked.status_code == 429
        assert int(locked.headers["retry-after"]) > 0
        # Even the correct password is refused while locked out.
        assert (
            client.post(
                "/api/auth/login", json={"username": "admin", "password": "pw"}
            ).status_code
            == 429
        )


# -- break-glass ------------------------------------------------------------


def _put_breakglass(app) -> str:
    """Issue a break-glass token directly via the store; return the plaintext."""
    import se3.server.crypto as crypto

    plaintext, token_hash = crypto.generate_token("bg")
    app.state.store.put_breakglass(token_hash)
    return plaintext


def test_breakglass_mints_admin_session(client_and_app):
    client, app = client_and_app
    token = _put_breakglass(app)
    resp = client.post("/api/auth/breakglass", json={"token": token})
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is True
    # The minted session authenticates as an admin (sees all machines).
    me = client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["is_admin"] is True


def test_breakglass_is_single_admin_subject(client_and_app):
    """Two break-glass consumptions resolve to the SAME stable admin owner."""
    client, app = client_and_app
    t1 = _put_breakglass(app)
    t2 = _put_breakglass(app)
    o1 = client.post("/api/auth/breakglass", json={"token": t1}).json()["owner_id"]
    client.post("/api/auth/logout")
    o2 = client.post("/api/auth/breakglass", json={"token": t2}).json()["owner_id"]
    assert o1 == o2  # never one-owner-per-token impersonation


def test_breakglass_token_is_one_time(client_and_app):
    client, app = client_and_app
    token = _put_breakglass(app)
    assert client.post("/api/auth/breakglass", json={"token": token}).status_code == 200
    # A consumed token cannot be replayed.
    assert client.post("/api/auth/breakglass", json={"token": token}).status_code == 401


def test_breakglass_invalid_token_is_401(client_and_app):
    client, _ = client_and_app
    assert (
        client.post("/api/auth/breakglass", json={"token": "not-a-real-token"}).status_code
        == 401
    )


def test_breakglass_empty_token_is_422(client_and_app):
    client, _ = client_and_app
    assert client.post("/api/auth/breakglass", json={"token": "  "}).status_code == 422


# -- fail-closed ------------------------------------------------------------


def test_fail_closed_when_no_provider_configured():
    """With local disabled and nothing else enabled, create_app refuses to build."""
    from se3.server.auth.registry import AuthNotConfigured

    with pytest.raises(AuthNotConfigured):
        authed_app(auth_config={"providers": [{"type": "local", "enabled": False}]})


def test_unauthenticated_reads_are_401():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as client:
        assert client.get("/api/machines").status_code == 401
        assert client.get("/api/history").status_code == 401
        assert client.get("/api/flows/whatever").status_code == 401


def test_default_session_cookie_is_secure():
    """A default (non-test) app issues a Secure session cookie."""
    from fastapi.testclient import TestClient

    import se3.server.crypto as crypto
    from se3.server.app import create_app

    app = create_app()  # default SessionStore -> Secure cookie
    store = app.state.store
    oid = store.create_owner("u", is_admin=False)
    store.link_identity(oid, "local", "u")
    store.set_password(oid, crypto.hash_password("pw"))
    with TestClient(app) as client:
        resp = client.post(
            "/api/auth/login", json={"username": "u", "password": "pw"}
        )
        assert resp.status_code == 200
        set_cookie = resp.headers.get("set-cookie", "")
        assert "Secure" in set_cookie
        assert "HttpOnly" in set_cookie


# -- credential hygiene: nothing secret reaches the logs --------------------


def test_credentials_never_logged(client_and_app, caplog):
    client, app = client_and_app
    secret_pw = "S3cr3t-PASSWORD-do-not-log"
    # Re-seed the admin password to a recognizable value.
    import se3.server.crypto as crypto

    app.state.store.set_password(
        app.state.test_owner_id, crypto.hash_password(secret_pw)
    )
    bg_token = _put_breakglass(app)

    with caplog.at_level(logging.DEBUG):
        # Successful + failed login, and a break-glass consume.
        client.post("/api/auth/login", json={"username": "admin", "password": secret_pw})
        client.post("/api/auth/login", json={"username": "admin", "password": "wrong-one"})
        session_cookie = client.cookies.get("se3_session")
        client.post("/api/auth/breakglass", json={"token": bg_token})

    blob = caplog.text
    assert secret_pw not in blob
    assert "wrong-one" not in blob
    assert bg_token not in blob
    if session_cookie:
        assert session_cookie not in blob
