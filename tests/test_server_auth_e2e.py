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

from _authsrv import authed_app, login, recv_daemon_frame

from tianluo.daemon import protocol
from tianluo.server.auth.ratelimit import LoginRateLimiter, RateLimitConfig


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
    import tianluo.server.crypto as crypto

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
    from tianluo.server.auth.registry import AuthNotConfigured

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

    import tianluo.server.crypto as crypto
    from tianluo.server.app import create_app

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


# -- admin user provisioning (G8 task 2) ------------------------------------


def _seed_local_user(app, username, password, *, is_admin=False) -> str:
    """Directly seed a local user (owner + binding + password) in the store."""
    import tianluo.server.crypto as crypto

    store = app.state.store
    oid = store.create_owner(username, is_admin=is_admin)
    store.link_identity(oid, "local", username)
    store.set_password(oid, crypto.hash_password(password))
    return oid


def test_admin_creates_user_who_can_then_login(client_and_app):
    client, app = client_and_app
    login(client)  # the seeded admin
    resp = client.post(
        "/api/users",
        json={"username": "bob", "password": "bob-pw", "display_name": "Bob"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "bob"
    assert body["display_name"] == "Bob"
    assert body["is_admin"] is False
    new_owner_id = body["owner_id"]

    # The owner + binding + password hash all landed: the new user can log in.
    from tianluo.server import crypto

    store = app.state.store
    assert store.resolve_owner_by_identity("local", "bob") == new_owner_id
    assert crypto.verify_password("bob-pw", store.get_password_hash(new_owner_id))

    from fastapi.testclient import TestClient

    with TestClient(app) as fresh:
        ok = fresh.post("/api/auth/login", json={"username": "bob", "password": "bob-pw"})
        assert ok.status_code == 200
        assert ok.json()["owner_id"] == new_owner_id


def test_admin_can_create_another_admin(client_and_app):
    client, app = client_and_app
    login(client)
    resp = client.post(
        "/api/users",
        json={"username": "carol", "password": "pw2", "is_admin": True},
    )
    assert resp.status_code == 201
    assert resp.json()["is_admin"] is True
    assert app.state.store.get_owner(resp.json()["owner_id"]).is_admin is True


def test_non_admin_cannot_create_user(client_and_app):
    client, app = client_and_app
    _seed_local_user(app, "eve", "eve-pw", is_admin=False)
    from fastapi.testclient import TestClient

    with TestClient(app) as eve:
        eve.post("/api/auth/login", json={"username": "eve", "password": "eve-pw"})
        forbidden = eve.post(
            "/api/users", json={"username": "mallory", "password": "x"}
        )
        assert forbidden.status_code == 403
        # No owner was created for the rejected request.
        assert app.state.store.resolve_owner_by_identity("local", "mallory") is None


def test_create_user_requires_authentication(client_and_app):
    client, _ = client_and_app
    # No public self-registration: an unauthenticated POST is 401, never 201.
    assert (
        client.post("/api/users", json={"username": "x", "password": "y"}).status_code
        == 401
    )


def test_create_duplicate_user_is_409(client_and_app):
    client, app = client_and_app
    login(client)
    first = client.post("/api/users", json={"username": "dup", "password": "pw"})
    assert first.status_code == 201
    again = client.post("/api/users", json={"username": "dup", "password": "other"})
    assert again.status_code == 409
    # The duplicate attempt left no orphan: exactly one owner bound to "dup".
    owner_id = app.state.store.resolve_owner_by_identity("local", "dup")
    assert owner_id == first.json()["owner_id"]


def test_create_user_validates_empty_fields(client_and_app):
    client, _ = client_and_app
    login(client)
    assert (
        client.post("/api/users", json={"username": "  ", "password": "pw"}).status_code
        == 422
    )
    assert (
        client.post("/api/users", json={"username": "ok", "password": ""}).status_code
        == 422
    )


def test_no_public_registration_endpoint(client_and_app):
    """v1 exposes no self-service registration route (design non-goal)."""
    client, _ = client_and_app
    for path in ("/api/auth/register", "/api/register", "/api/signup"):
        # No POST registration handler exists: the path is either unrouted
        # (404) or only served by the static mount for GET (405). Crucially it
        # is never a successful account creation.
        assert client.post(
            path, json={"username": "x", "password": "y"}
        ).status_code in (404, 405)


def test_credentials_never_logged(client_and_app, caplog):
    client, app = client_and_app
    secret_pw = "S3cr3t-PASSWORD-do-not-log"
    # Re-seed the admin password to a recognizable value.
    import tianluo.server.crypto as crypto

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


# --------------------------------------------------------------------------
# G10 task 1 — the full multi-tenant chain, end to end
#
# bootstrap-token  ->  break-glass admin login  ->  admin creates owner
#   ->  owner mints a daemon key in the UI  ->  daemon HELLOs with that key and
#   binds its machine to the owner  ->  the owner dispatches a flow to its OWN
#   daemon  ->  a second owner can neither see nor dispatch to the first's
#   daemon. Exercised over a live FastAPI TestClient with a real daemon /ws
#   socket, against the real persistence + identity wiring.
# --------------------------------------------------------------------------


def _shared_app():
    """A multi-tenant app over a shared real Store (in-memory sqlite)."""
    from tianluo.server.app import create_app
    from tianluo.server.auth.session import CookieConfig, SessionStore
    from tianluo.server.persistence import Store

    store = Store(":memory:")
    app = create_app(
        store=store,
        session_store=SessionStore(cookie_config=CookieConfig(secure=False)),
    )
    return app, store


def _await_visible(client, machine_id, tries=200):
    for _ in range(tries):
        machines = client.get("/api/machines").json().get("machines", [])
        if any(m["machine_id"] == machine_id for m in machines):
            return
    raise AssertionError(f"machine {machine_id} never became visible")


def test_full_chain_bootstrap_owner_key_daemon_dispatch():
    from fastapi.testclient import TestClient

    from tianluo.server import bootstrap

    app, store = _shared_app()

    # 1. bootstrap: mint a break-glass token exactly as `se3-server
    #    bootstrap-token` does (hash at rest; plaintext returned once).
    bg_plain, _tid = bootstrap.issue_breakglass_token(store)

    # 2. break-glass login -> a stable single admin subject.
    with TestClient(app) as admin:
        bg = admin.post("/api/auth/breakglass", json={"token": bg_plain})
        assert bg.status_code == 200 and bg.json()["is_admin"] is True

        # 3. the admin provisions two distinct owners (no public self-signup).
        alice = admin.post(
            "/api/users", json={"username": "alice", "password": "alice-pw"}
        )
        bob = admin.post(
            "/api/users", json={"username": "bob", "password": "bob-pw"}
        )
        assert alice.status_code == 201 and bob.status_code == 201
        alice_id = alice.json()["owner_id"]
        bob_id = bob.json()["owner_id"]
        assert alice_id != bob_id and not alice.json()["is_admin"]

    with TestClient(app) as ca, TestClient(app) as cb:
        login(ca, "alice", "alice-pw")
        login(cb, "bob", "bob-pw")

        # 4. alice mints a daemon key in the UI (plaintext returned once).
        created = ca.post("/api/daemon-keys", json={"label": "alice-node"})
        assert created.status_code == 201
        dkey = created.json()["key"]
        assert dkey  # the one-time plaintext

        # 5. the daemon dials /ws and authenticates via the HELLO key, binding
        #    its machine to alice's owner_id.
        with ca.websocket_connect("/ws") as daemon:
            daemon.send_text(
                protocol.make_hello("mAlice", "h", "6.4.0", key=dkey).to_json()
            )
            welcome = recv_daemon_frame(daemon)
            assert welcome.type == protocol.MSG_WELCOME
            assert welcome.payload["accepted"] is True
            # The secret key is never echoed back in the WELCOME.
            assert dkey not in welcome.to_json()

            daemon.send_text(
                protocol.make_status_update(
                    {
                        "machine_id": "mAlice",
                        "flows": [
                            {"flow_id": "fA", "project_root": "/pa", "status": "running"}
                        ],
                    }
                ).to_json()
            )
            _await_visible(ca, "mAlice")

            # alice sees ONLY her machine; bob sees nothing of hers.
            assert {m["machine_id"] for m in ca.get("/api/machines").json()["machines"]} == {
                "mAlice"
            }
            assert cb.get("/api/machines").json()["machines"] == []
            assert cb.get("/api/machines/mAlice/flows").status_code == 404
            assert cb.get("/api/flows/fA").status_code == 404

            # 6. alice dispatches a flow to her OWN daemon -> SPAWN_FLOW lands.
            ok = ca.post(
                "/api/flows",
                json={"machine_id": "mAlice", "task": "do", "project_root": "/pa"},
            )
            assert ok.status_code == 202
            spawn = recv_daemon_frame(daemon)
            assert spawn.type == protocol.MSG_SPAWN_FLOW

            # 7. bob CANNOT dispatch to alice's daemon — it reads as absent (404),
            #    so the remote-command-execution hole the bare server had is shut.
            cross = cb.post(
                "/api/flows",
                json={"machine_id": "mAlice", "task": "pwn", "project_root": "/pa"},
            )
            assert cross.status_code == 404


def test_full_chain_fail_closed_without_identity():
    """No valid identity anywhere on the chain ⇒ fail-closed, never bare."""
    from fastapi.testclient import TestClient

    app, _store = _shared_app()
    with TestClient(app) as anon:
        # REST: unauthenticated reads/writes are refused (401), never anonymous.
        assert anon.get("/api/machines").status_code == 401
        assert (
            anon.post(
                "/api/flows",
                json={"machine_id": "m", "task": "x", "project_root": "/p"},
            ).status_code
            == 401
        )
        # daemon /ws: a HELLO with no key is rejected (WELCOME accepted=false).
        with anon.websocket_connect("/ws") as ws:
            ws.send_text(protocol.make_hello("mGhost", "h", "6.4.0").to_json())
            welcome = recv_daemon_frame(ws)
            assert welcome.type == protocol.MSG_WELCOME
            assert welcome.payload["accepted"] is False
        # The rejected daemon registered nothing the operator could later see.
        from tianluo.server import bootstrap

        bg_plain, _ = bootstrap.issue_breakglass_token(_store)
        admin = anon  # reuse the client; log in via break-glass
        admin.post("/api/auth/breakglass", json={"token": bg_plain})
        assert all(
            m["machine_id"] != "mGhost"
            for m in admin.get("/api/machines").json()["machines"]
        )


# -- config wiring (se3-server reads server.auth.* / server.db_path) ---------


def test_create_app_kwargs_from_server_config_translates_auth():
    """``_create_app_kwargs_from_server_config`` maps the structured
    ``ServerConfig`` onto the surfaces ``create_app`` consumes, so an operator's
    ``server.auth.*`` / ``server.db_path`` values actually drive the server
    instead of being silently dropped (regression for the medium self-check)."""
    from tianluo.config import (
        AuthConfig,
        LocalAuthConfig,
        ProxyHeaderConfig,
        ServerConfig,
        SessionConfig,
    )
    from tianluo.server.app import _create_app_kwargs_from_server_config

    cfg = ServerConfig(
        db_path="/tmp/custom-server.db",
        auth=AuthConfig(
            providers=["local", "proxy_header"],
            session=SessionConfig(
                cookie_name="my_sess",
                cookie_secure=False,
                cookie_samesite="strict",
                max_age_seconds=3600,
            ),
            local=LocalAuthConfig(
                max_failed_attempts=3,
                lockout_seconds=120,
                ratelimit_window_seconds=45,
            ),
            proxy_header=ProxyHeaderConfig(
                enabled=True, trust_proxy=True, header="X-Auth-Email"
            ),
        ),
    )

    kwargs = _create_app_kwargs_from_server_config(cfg)

    assert kwargs["db_path"] == "/tmp/custom-server.db"
    # providers expanded into full entries carrying each provider's options,
    # including trust_proxy so the proxy-header provider is actually enableable
    # purely through se3.yaml.
    entries = kwargs["auth_config"]["providers"]
    assert entries[0] == "local"
    assert entries[1] == {
        "type": "proxy_header",
        "enabled": True,
        "trust_proxy": True,
        "header": "X-Auth-Email",
    }
    # session cookie attributes flow into the SessionStore.
    cookie = kwargs["session_store"].cookie_config
    assert cookie.name == "my_sess"
    assert cookie.secure is False
    assert cookie.same_site == "strict"
    assert cookie.max_age == 3600
    # local lockout thresholds flow into the LoginRateLimiter.
    rl_cfg = kwargs["rate_limiter"]._config
    assert rl_cfg.max_failures == 3
    assert rl_cfg.lockout_seconds == 120.0
    assert rl_cfg.window_seconds == 45.0


def test_create_app_kwargs_default_config_builds_local_chain():
    """The default ServerConfig still yields a usable local-only chain when fed
    through the translation + create_app (no fail-closed regression)."""
    from tianluo.config import ServerConfig
    from tianluo.server.app import _create_app_kwargs_from_server_config, create_app

    cfg = ServerConfig()  # defaults: providers=['local']
    kwargs = _create_app_kwargs_from_server_config(cfg)
    # Use an in-memory store rather than the configured ~/.se3 default.
    kwargs["db_path"] = None
    app = create_app(**kwargs)
    assert app.state.auth_chain is not None
