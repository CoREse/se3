"""Defect C — expired/rotated signed cursor recovery, and the 401 forensics (G4).

Field symptom (node007 …-7c8ae1d30d71): under a ~40 s daemon-reconnect storm,
``GET /api/history/{flow_id}?after=&sig=`` and ``GET /api/flows/{flow_id}``
*intermittently* returned 401 while other requests in the SAME browser session
were fine, and the chat panel froze re-presenting a cursor that never advanced.

DISCOVERY CONCLUSION (established empirically by the tests below, recorded here
as the truth-source note the task asks for — no source file records it):

1. ``require_owner`` (``Depends(require_owner)`` on both routes, app.py ~1959)
   resolves identity from the SESSION COOKIE ALONE — ``ProviderChain
   .resolve_owner`` → ``SessionAuthProvider.resolve_owner`` (auth/session.py:219)
   reads only the session cookie; it NEVER looks at ``after``/``sig``. It raises
   401 iff ``chain.resolve_owner()`` returns ``None`` (auth/registry.py:157),
   i.e. the browser session cookie is missing / expired past TTL / swept / the
   owner record was removed.

2. The signed cursor (``after``, HMAC-signed by ``encode_progress``) and content
   ``sig`` are examined ONLY AFTER the owner gate, inside
   ``ServerState.get_history_snapshot``. Every stale / expired / tampered /
   generation-rotated value fail-closes to ``delivery: "full"`` — a 200
   recoverable snapshot carrying a FRESH authoritative ``progress`` /
   ``signature`` / ``generation`` / ``cursor``. So a stale signed cursor can
   NEVER, by itself, produce a 401 (proved by
   ``test_stale_signed_cursor_never_401_recovers_with_resync``).

3. Therefore the field's 401↔reconnect-storm correlation is SPURIOUS at the auth
   layer: the daemon ``/ws`` channel authenticates with a HELLO daemon key and
   never touches the browser ``SessionStore``; nothing in a daemon reconnect
   sweeps or rotates browser sessions. A genuine 401 is a genuine session-cookie
   failure and MUST stay fail-closed.

4. The real recoverability gap (now fixed): a stale-cursor ``full`` is 200 but
   was INDISTINGUISHABLE from a first-ever load, so a client could not tell "your
   cursor was rejected — resync to the authoritative one" from a routine rebuild
   and could loop re-presenting the dead cursor. ``get_history_snapshot`` now
   flags that reply ``resync: True`` (only when a non-empty ``after`` was offered
   yet did not bind the current bundle), so the client resynchronises to the
   reply's authoritative token instead of bare-retrying. ``decode_progress``'s
   existing full-fallback (the "过期 sig → full" path) is what this marker
   annotates — it is not itself a 401 source.

The tests drive the REAL REST endpoint through an authenticated ``TestClient``
with a connected daemon seeding the cache, matching test_history_endpoint_signature.
"""

from __future__ import annotations

import pytest

from _authsrv import authed_app, authed_hello, login
from tianluo.daemon import protocol


def _make_records(n):
    return [
        {"step_id": f"s{i}", "message": {"role": "user", "content": f"m{i}"}}
        for i in range(n)
    ]


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


def _seed_bundle(client, app, flow_id, records, machine_id="m1"):
    """Connect a daemon, push a full bundle, return (daemon_ctx, sock, first_body).

    The daemon stays connected so the cache-hit path the pushed bundle
    establishes is exercised end to end.
    """
    daemon = client.websocket_connect("/ws")
    sock = daemon.__enter__()
    sock.send_text(authed_hello(app, machine_id, "host", "6.4.0"))
    protocol.decode(sock.receive_text())  # WELCOME
    sock.send_text(
        protocol.make_history_data(
            flow_id, protocol.HISTORY_MODE_FULL, records
        ).to_json()
    )
    for _ in range(50):
        resp = client.get(f"/api/history/{flow_id}")
        if resp.status_code == 200 and resp.json().get("cached"):
            return daemon, sock, resp.json()
    daemon.__exit__(None, None, None)
    raise AssertionError("bundle never became cache-visible")


def _push_full(client, sock, flow_id, records, prev_generation):
    """Push a replacing full frame and wait until its new generation is visible."""
    sock.send_text(
        protocol.make_history_data(
            flow_id, protocol.HISTORY_MODE_FULL, records
        ).to_json()
    )
    for _ in range(50):
        body = client.get(f"/api/history/{flow_id}").json()
        if body["generation"] != prev_generation:
            return body
    raise AssertionError("generation never rotated")


def _add_owner(app, username, *, is_admin=False):
    """Seed a second local owner so cross-owner scoping can be exercised."""
    import tianluo.server.crypto as crypto

    store = app.state.store
    owner_id = store.create_owner(username, is_admin=is_admin)
    store.link_identity(owner_id, "local", username)
    store.set_password(owner_id, crypto.hash_password("pw"))
    return owner_id


# --------------------------------------------------------------------------- #
# Recoverable-response semantics (Defect C fix)                                #
# --------------------------------------------------------------------------- #


def test_first_load_full_is_not_a_resync(client_and_app):
    """A no-``after`` full is a routine load, NOT a stale-cursor resync."""
    client, app = client_and_app
    daemon, _sock, first = _seed_bundle(client, app, "f1", _make_records(1))
    try:
        assert first["delivery"] == "full"
        assert first["resync"] is False
        # A recoverable reply always carries the authoritative resync inputs.
        assert first["progress"] and first["signature"]
        assert isinstance(first["generation"], int) and first["generation"] > 0
    finally:
        daemon.__exit__(None, None, None)


def test_stale_signed_cursor_never_401_recovers_with_resync(client_and_app):
    """An expired/garbage signed cursor → 200 recoverable full flagged resync.

    This is the core Defect-C assertion: the expired sig path is NOT a 401 (it
    never reaches ``require_owner``), and the reply now positively tells the
    client to resync rather than looking like a plain rebuild.
    """
    client, app = client_and_app
    daemon, _sock, first = _seed_bundle(client, app, "f1", _make_records(2))
    try:
        # Garbage token + garbage content signature, with a VALID owner cookie.
        resp = client.get("/api/history/f1?after=GARBAGE-TOKEN&sig=STALE-SIG")
        assert resp.status_code == 200  # emphatically NOT 401
        body = resp.json()
        assert body["delivery"] == "full"
        assert body["resync"] is True
        # Carries everything the client needs to resynchronise its cursor.
        assert body["progress"] and body["signature"]
        assert body["generation"] == first["generation"]
        assert body["records"] == _make_records(2)
    finally:
        daemon.__exit__(None, None, None)


def test_rotated_generation_token_recovers_with_resync(client_and_app):
    """A once-valid token whose bundle generation rotated → resync, not 401.

    Models the reconnect-storm scenario named in the task: the daemon re-pushes
    a fresh bundle (new generation) while an in-flight poll still carries the old
    signed cursor. The old token was legitimately server-issued and correctly
    HMAC-signed — only its generation is stale — yet it must recover gracefully.
    """
    client, app = client_and_app
    daemon, sock, first = _seed_bundle(client, app, "f1", _make_records(2))
    try:
        old_token, old_sig = first["progress"], first["signature"]
        # Fresh valid cursor is honoured cheaply — resync stays False.
        nm = client.get(f"/api/history/f1?after={old_token}&sig={old_sig}").json()
        assert nm["delivery"] == "not_modified"
        assert nm["resync"] is False

        # Daemon reconnect re-pushes a larger bundle: generation rotates.
        rotated = _push_full(client, sock, "f1", _make_records(4), first["generation"])
        assert rotated["generation"] != first["generation"]

        # The now-stale-generation token recovers as a resync full (never 401).
        resp = client.get(f"/api/history/f1?after={old_token}&sig={old_sig}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["delivery"] == "full"
        assert body["resync"] is True
        assert body["generation"] == rotated["generation"]
        assert body["records"] == _make_records(4)
        # Re-adopting the fresh token settles back to the cheap path — proving
        # the resync is a real recovery, not an endless full loop.
        settled = client.get(
            f"/api/history/f1?after={body['progress']}&sig={body['signature']}"
        ).json()
        assert settled["delivery"] == "not_modified"
        assert settled["resync"] is False
    finally:
        daemon.__exit__(None, None, None)


def test_owner_polling_through_rotation_storm_never_401(client_and_app):
    """A logged-in owner polling across repeated bundle rotations never 401s.

    Simulates the ~40 s reconnect storm: the daemon keeps re-pushing fuller
    bundles (rolling the generation) while the browser keeps polling with the
    token it last held. Every response stays 200; the poll self-heals to the
    fresh cursor each round instead of wedging on 401.
    """
    client, app = client_and_app
    daemon, sock, body = _seed_bundle(client, app, "f1", _make_records(1))
    try:
        token, sig = body["progress"], body["signature"]
        gen = body["generation"]
        for n in range(2, 8):
            gen_before = gen
            rotated = _push_full(client, sock, "f1", _make_records(n), gen_before)
            gen = rotated["generation"]
            # The in-flight poll still carries the PRE-rotation cursor.
            resp = client.get(f"/api/history/f1?after={token}&sig={sig}")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["delivery"] == "full"
            assert body["resync"] is True
            # Resync to the authoritative cursor for the next round.
            token, sig = body["progress"], body["signature"]
        # After the storm settles, the adopted cursor is cheap again.
        final = client.get(f"/api/history/f1?after={token}&sig={sig}").json()
        assert final["delivery"] == "not_modified"
        assert final["resync"] is False
    finally:
        daemon.__exit__(None, None, None)


def test_flow_detail_owner_polling_never_401(client_and_app):
    """``GET /api/flows/{id}`` for a logged-in owner is 200 (or 404), never 401.

    The other route named in the field report carries no signed cursor at all,
    so it can only 404 (unknown/cross-owner flow) or 200 — never 401 for an
    authenticated owner. Asserted so a regression that couples auth to flow
    lookup would be caught.
    """
    client, app = client_and_app
    # Unknown flow for a logged-in owner: 404, not 401.
    assert client.get("/api/flows/does-not-exist").status_code == 404


# --------------------------------------------------------------------------- #
# Fail-closed boundaries preserved                                            #
# --------------------------------------------------------------------------- #


def test_unauthenticated_still_401(client_and_app):
    """Genuinely unauthenticated requests still 401 on BOTH routes.

    The 401 is a real session failure — the only thing that produces it — and it
    must stay fail-closed even when a (would-be) signed cursor is present.
    """
    from fastapi.testclient import TestClient

    _client, app = client_and_app
    anon = TestClient(app)  # fresh cookie jar, never logs in
    assert anon.get("/api/history/f1?after=GARBAGE&sig=STALE").status_code == 401
    assert anon.get("/api/flows/f1").status_code == 401


def test_cross_owner_still_404(client_and_app):
    """A different owner reading another owner's flow reads as absent (404).

    Owner scoping is not weakened by the resync path: the flow's owning machine
    (m1, bound to the seeding admin) is not owned by ``bob``, so ``bob`` gets 404
    — even presenting a signed cursor — never a leak and never a 401.
    """
    from fastapi.testclient import TestClient

    client, app = client_and_app
    daemon, _sock, _first = _seed_bundle(client, app, "f1", _make_records(2))
    try:
        _add_owner(app, "bob", is_admin=False)
        bob = TestClient(app)
        login(bob, "bob", "pw")
        # Cross-owner: reads as absent (404), with OR without a signed cursor.
        assert bob.get("/api/history/f1").status_code == 404
        assert (
            bob.get("/api/history/f1?after=GARBAGE&sig=STALE").status_code == 404
        )
        assert bob.get("/api/flows/f1").status_code == 404
    finally:
        daemon.__exit__(None, None, None)
