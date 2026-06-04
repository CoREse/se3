"""End-to-end tests for the admin user-management REST surface (G2 task 6).

Covers the four ``/api/users`` routes added in group G2 over a real FastAPI
``TestClient``:

* ``GET    /api/users``                  — list manageable owners
* ``DELETE /api/users/{owner_id}``       — delete a user
* ``POST   /api/users/{owner_id}/password`` — reset a local user's password
* ``POST   /api/users/{owner_id}/admin``    — toggle the admin flag

Every security boundary the design mandates is asserted with at least one
negative case: non-admin / unauthenticated rejection, the field whitelist +
break-glass filtering of the list view, the self / last-admin / break-glass /
local-only protections, and owner-existence validation. A password-reset
end-to-end check confirms the reset user can subsequently log in.
"""

from __future__ import annotations

import pytest

from _authsrv import authed_app, login
from se3.server.app import BREAKGLASS_EXTERNAL_ID, BREAKGLASS_PROVIDER


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as client:
        yield client, app


# --------------------------------------------------------------------------
# store-seeding helpers
# --------------------------------------------------------------------------


def _seed_local(app, username, password="pw", *, is_admin=False) -> str:
    """Seed a local user (owner + (local, username) binding + password hash)."""
    import se3.server.crypto as crypto

    store = app.state.store
    oid = store.create_owner(username, is_admin=is_admin)
    store.link_identity(oid, "local", username)
    store.set_password(oid, crypto.hash_password(password))
    return oid


def _seed_oidc(app, username, *, is_admin=False) -> str:
    """Seed a non-local (OIDC) owner — it carries no local credential."""
    store = app.state.store
    oid = store.create_owner(username, is_admin=is_admin)
    store.link_identity(oid, "oidc", f"issuer|{username}")
    return oid


def _seed_breakglass(app) -> str:
    """Directly create the reserved break-glass admin owner in the store."""
    store = app.state.store
    oid = store.create_owner("break-glass admin", is_admin=True)
    store.link_identity(oid, BREAKGLASS_PROVIDER, BREAKGLASS_EXTERNAL_ID)
    return oid


def _login_breakglass(client, app) -> str:
    """Mint + consume a break-glass token so ``client`` holds a bg admin session.

    Logging in as break-glass (rather than the seeded local admin) is the only
    way to act on *another* owner as "an admin that is not counted as a real
    admin" — required to exercise the last-real-admin delete/demote guards
    without the self-protection firing first.
    """
    import se3.server.crypto as crypto

    plain, key_hash = crypto.generate_token("bg")
    app.state.store.put_breakglass(key_hash)
    resp = client.post("/api/auth/breakglass", json={"token": plain})
    assert resp.status_code == 200, resp.text
    return resp.json()["owner_id"]


# --------------------------------------------------------------------------
# auth gate: non-admin -> 403, unauthenticated -> 401 (all four routes)
# --------------------------------------------------------------------------


def test_user_routes_reject_non_admin_403(client_and_app):
    _client, app = client_and_app
    _seed_local(app, "eve", "eve-pw", is_admin=False)
    from fastapi.testclient import TestClient

    with TestClient(app) as eve:
        eve.post("/api/auth/login", json={"username": "eve", "password": "eve-pw"})
        assert eve.get("/api/users").status_code == 403
        # The admin check runs before existence — a non-admin is 403 even for a
        # made-up owner_id (no information about whether it exists leaks).
        assert eve.delete("/api/users/whatever").status_code == 403
        assert (
            eve.post("/api/users/whatever/password", json={"password": "x"}).status_code
            == 403
        )
        assert (
            eve.post("/api/users/whatever/admin", json={"is_admin": True}).status_code
            == 403
        )


def test_user_routes_require_authentication_401(client_and_app):
    _client, app = client_and_app
    from fastapi.testclient import TestClient

    with TestClient(app) as anon:
        assert anon.get("/api/users").status_code == 401
        assert anon.delete("/api/users/whatever").status_code == 401
        assert (
            anon.post("/api/users/whatever/password", json={"password": "x"}).status_code
            == 401
        )
        assert (
            anon.post("/api/users/whatever/admin", json={"is_admin": True}).status_code
            == 401
        )


# --------------------------------------------------------------------------
# GET /api/users — field whitelist, break-glass filtering, provider flags
# --------------------------------------------------------------------------


def test_list_users_whitelists_fields_and_filters_breakglass(client_and_app):
    client, app = client_and_app
    login(client)  # seeded admin
    _seed_local(app, "bob", "bob-pw")
    _seed_oidc(app, "olive")
    bg_id = _seed_breakglass(app)

    resp = client.get("/api/users")
    assert resp.status_code == 200
    users = resp.json()["users"]
    by_id = {u["owner_id"]: u for u in users}

    # Break-glass is filtered out of the manageable list.
    assert bg_id not in by_id

    # The local admin + bob + olive are all present.
    assert app.state.test_owner_id in by_id
    bob = next(u for u in users if u["display_name"] == "bob")
    olive = next(u for u in users if u["display_name"] == "olive")

    # Exactly the whitelisted fields — no password / key hashes or other leaks.
    allowed = {
        "owner_id",
        "display_name",
        "is_admin",
        "created_at",
        "provider",
        "can_reset_password",
    }
    for u in users:
        assert set(u.keys()) == allowed

    # provider + can_reset_password reflect the binding origin.
    assert bob["provider"] == "local" and bob["can_reset_password"] is True
    assert olive["provider"] == "oidc" and olive["can_reset_password"] is False

    # No sensitive material anywhere in the serialized body.
    body = resp.text
    for owner_id in (app.state.test_owner_id, bob["owner_id"]):
        h = app.state.store.get_password_hash(owner_id)
        if h:
            assert h not in body


# --------------------------------------------------------------------------
# DELETE /api/users/{owner_id}
# --------------------------------------------------------------------------


def test_delete_self_is_forbidden_403(client_and_app):
    client, app = client_and_app
    login(client)
    resp = client.delete(f"/api/users/{app.state.test_owner_id}")
    assert resp.status_code == 403
    # The admin still exists — nothing was deleted.
    assert app.state.store.get_owner(app.state.test_owner_id) is not None


def test_delete_unknown_owner_is_404(client_and_app):
    client, _app = client_and_app
    login(client)
    assert client.delete("/api/users/does-not-exist").status_code == 404


def test_delete_breakglass_owner_is_404(client_and_app):
    client, app = client_and_app
    login(client)
    bg_id = _seed_breakglass(app)
    assert client.delete(f"/api/users/{bg_id}").status_code == 404
    # The break-glass owner survives the rejected delete.
    assert app.state.store.get_owner(bg_id) is not None


def test_delete_last_real_admin_is_409(client_and_app):
    client, app = client_and_app
    # Act as break-glass (not counted as a real admin) so the seeded local admin
    # is the *only* real admin and the last-admin guard fires (not self-protect).
    bg_client = client
    _login_breakglass(bg_client, app)
    seeded_admin = app.state.test_owner_id
    resp = bg_client.delete(f"/api/users/{seeded_admin}")
    assert resp.status_code == 409
    assert app.state.store.get_owner(seeded_admin) is not None


def test_delete_user_succeeds_and_drops_from_list(client_and_app):
    client, app = client_and_app
    login(client)
    created = client.post("/api/users", json={"username": "bob", "password": "pw"})
    assert created.status_code == 201
    bob_id = created.json()["owner_id"]

    assert client.delete(f"/api/users/{bob_id}").status_code == 200
    # Cascade removed the owner + its binding.
    assert app.state.store.get_owner(bob_id) is None
    listed = {u["owner_id"] for u in client.get("/api/users").json()["users"]}
    assert bob_id not in listed


# --------------------------------------------------------------------------
# POST /api/users/{owner_id}/password
# --------------------------------------------------------------------------


def test_reset_password_non_local_owner_is_409(client_and_app):
    client, app = client_and_app
    login(client)
    olive_id = _seed_oidc(app, "olive")
    resp = client.post(
        f"/api/users/{olive_id}/password", json={"password": "whatever"}
    )
    assert resp.status_code == 409


def test_reset_password_unknown_or_breakglass_owner_is_404(client_and_app):
    client, app = client_and_app
    login(client)
    assert (
        client.post("/api/users/ghost/password", json={"password": "x"}).status_code
        == 404
    )
    bg_id = _seed_breakglass(app)
    assert (
        client.post(f"/api/users/{bg_id}/password", json={"password": "x"}).status_code
        == 404
    )


def test_reset_password_empty_is_422(client_and_app):
    client, app = client_and_app
    login(client)
    bob_id = _seed_local(app, "bob", "bob-pw")
    resp = client.post(f"/api/users/{bob_id}/password", json={"password": ""})
    assert resp.status_code == 422


def test_reset_password_lets_user_login_with_new_password(client_and_app):
    client, app = client_and_app
    login(client)
    created = client.post("/api/users", json={"username": "bob", "password": "old-pw"})
    bob_id = created.json()["owner_id"]

    resp = client.post(
        f"/api/users/{bob_id}/password", json={"password": "fresh-pw"}
    )
    assert resp.status_code == 200

    from fastapi.testclient import TestClient

    with TestClient(app) as fresh:
        # The new password works...
        ok = fresh.post(
            "/api/auth/login", json={"username": "bob", "password": "fresh-pw"}
        )
        assert ok.status_code == 200 and ok.json()["owner_id"] == bob_id
        # ...and the old one no longer does.
        bad = fresh.post(
            "/api/auth/login", json={"username": "bob", "password": "old-pw"}
        )
        assert bad.status_code == 401


def test_reset_password_plaintext_not_logged(client_and_app, caplog):
    import logging

    client, app = client_and_app
    login(client)
    bob_id = _seed_local(app, "bob", "bob-pw")
    secret = "sup3r-s3cret-value"
    with caplog.at_level(logging.DEBUG):
        resp = client.post(f"/api/users/{bob_id}/password", json={"password": secret})
    assert resp.status_code == 200
    assert all(secret not in rec.getMessage() for rec in caplog.records)


# --------------------------------------------------------------------------
# POST /api/users/{owner_id}/admin
# --------------------------------------------------------------------------


def test_demote_self_is_forbidden_403(client_and_app):
    client, app = client_and_app
    login(client)
    resp = client.post(
        f"/api/users/{app.state.test_owner_id}/admin", json={"is_admin": False}
    )
    assert resp.status_code == 403
    assert app.state.store.get_owner(app.state.test_owner_id).is_admin is True


def test_demote_last_real_admin_is_409(client_and_app):
    client, app = client_and_app
    _login_breakglass(client, app)
    seeded_admin = app.state.test_owner_id
    resp = client.post(f"/api/users/{seeded_admin}/admin", json={"is_admin": False})
    assert resp.status_code == 409
    assert app.state.store.get_owner(seeded_admin).is_admin is True


def test_toggle_admin_on_breakglass_owner_is_404(client_and_app):
    client, app = client_and_app
    login(client)
    bg_id = _seed_breakglass(app)
    assert (
        client.post(f"/api/users/{bg_id}/admin", json={"is_admin": False}).status_code
        == 404
    )


def test_toggle_admin_unknown_owner_is_404(client_and_app):
    client, _app = client_and_app
    login(client)
    assert (
        client.post("/api/users/ghost/admin", json={"is_admin": True}).status_code
        == 404
    )


def test_promote_then_demote_reflected_in_list(client_and_app):
    client, app = client_and_app
    login(client)
    bob_id = _seed_local(app, "bob", "bob-pw", is_admin=False)

    # Promote -> GET reflects is_admin true.
    up = client.post(f"/api/users/{bob_id}/admin", json={"is_admin": True})
    assert up.status_code == 200 and up.json()["is_admin"] is True
    listed = {u["owner_id"]: u for u in client.get("/api/users").json()["users"]}
    assert listed[bob_id]["is_admin"] is True

    # With two real admins now, demoting bob is allowed (not the last admin).
    down = client.post(f"/api/users/{bob_id}/admin", json={"is_admin": False})
    assert down.status_code == 200 and down.json()["is_admin"] is False
    assert app.state.store.get_owner(bob_id).is_admin is False
