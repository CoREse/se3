"""Tests for the ``GET /api/history/{flow_id}`` signature-check three-state (G6).

The self-heal poll no longer搬 the whole bundle every 3 s; it echoes the
progress token it holds plus the bundle's content ``sig``. The endpoint answers
one of three ways from the server-side cache:

* ``delivery: "not_modified"`` — the token is in sync AND the signature matches,
  so nothing changed: an extra-small idle-poll reply carrying no records;
* ``delivery: "delta"`` — a valid token behind the record count: only the tail
  travels;
* ``delivery: "full"`` — every fallback (no / stale token): the complete bundle,
  which ``GZipMiddleware`` compresses once it clears the size floor.

A repeated cache-miss full rebuild for the same flow is additionally rate-limited
by :meth:`ServerState.full_pull_throttled` so a client stuck presenting a diverged
token cannot force a multi-MB回源 pull on every poll.

The endpoint is exercised through a real ``TestClient`` with an authenticated
owner and a connected daemon that seeds the cache by pushing a ``MSG_HISTORY_DATA``
full frame, so the owner-scoping, cache-hit and gzip paths are covered end to end.
The full-pull throttle is asserted at the ``ServerState`` unit level where the
monotonic floor is deterministic.
"""

from __future__ import annotations

import asyncio

import pytest

from _authsrv import authed_app, authed_hello, login
from se3.daemon import protocol
from se3.server.state import ServerState, bundle_signature, encode_progress


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


def _seed_bundle(client, app, flow_id, records):
    """Connect a daemon, push a full history bundle, return the daemon socket ctx.

    The daemon stays connected (the returned context manager is still open) so a
    later cache-*miss* would have a live owner to pull from — but every assertion
    here targets the cache-*hit* path the pushed bundle establishes.
    """
    daemon = client.websocket_connect("/ws")
    sock = daemon.__enter__()
    sock.send_text(authed_hello(app, "m1", "host", "6.4.0"))
    protocol.decode(sock.receive_text())  # WELCOME
    sock.send_text(
        protocol.make_history_data(
            flow_id, protocol.HISTORY_MODE_FULL, records
        ).to_json()
    )
    # Wait until the pushed bundle is visible via the REST cache-hit path.
    for _ in range(50):
        resp = client.get(f"/api/history/{flow_id}")
        if resp.status_code == 200 and resp.json().get("cached"):
            return daemon, sock, resp
    daemon.__exit__(None, None, None)
    raise AssertionError("bundle never became cache-visible")


def test_full_then_not_modified_then_delta(client_and_app):
    """The three delivery states, driven through the real REST endpoint.

    A first pull with no token is ``full`` and hands back a ``progress`` token
    plus a ``signature``. Echoing both yields ``not_modified``; after the daemon
    appends a record, echoing the *old* token yields ``delta`` carrying only the
    new tail.
    """
    client, app = client_and_app
    records = [{"step_id": "s1", "message": {"role": "user", "content": "hello"}}]
    daemon, sock, first = _seed_bundle(client, app, "f1", records)
    try:
        body = first.json()
        assert body["delivery"] == "full"
        assert body["records"] == records
        token = body["progress"]
        sig = body["signature"]
        assert token and sig

        # 2) Echo token + signature → not_modified (the idle-poll win).
        nm = client.get(f"/api/history/f1?after={token}&sig={sig}").json()
        assert nm["delivery"] == "not_modified"
        assert nm["records"] == []
        # The signature is stable while the bundle is unchanged.
        assert nm["signature"] == sig

        # 3) Daemon appends a record; the OLD token is now behind → delta tail.
        tail = {"step_id": "s2", "message": {"role": "assistant", "content": "hi"}}
        sock.send_text(
            protocol.make_history_data(
                "f1", protocol.HISTORY_MODE_APPEND, [tail]
            ).to_json()
        )
        delta = None
        for _ in range(50):
            got = client.get(f"/api/history/f1?after={token}&sig={sig}").json()
            if got["delivery"] == "delta":
                delta = got
                break
        assert delta is not None, "append never surfaced as a delta"
        # Only the appended tail travels — not the whole bundle.
        assert delta["records"] == [tail]
        # The signature moved because the bundle content changed.
        assert delta["signature"] != sig
    finally:
        daemon.__exit__(None, None, None)


def test_stale_token_falls_back_to_full(client_and_app):
    """A token that does not validate (wrong signature/generation) → full rebuild."""
    client, app = client_and_app
    records = [{"step_id": "s1", "message": {"role": "user", "content": "x"}}]
    daemon, sock, first = _seed_bundle(client, app, "f1", records)
    try:
        # A garbage token + garbage signature cannot validate → full fallback
        # carrying the complete bundle (never a slice).
        got = client.get("/api/history/f1?after=not-a-real-token&sig=nope").json()
        assert got["delivery"] == "full"
        assert got["records"] == records
    finally:
        daemon.__exit__(None, None, None)


def test_full_bundle_response_is_gzipped(client_and_app):
    """A real ``full`` bundle over the gzip size floor is Content-Encoding: gzip.

    gzip is the second, orthogonal止血 layer: even after差量化 a genuine full
    rebuild can be multi-MB JSON, which ``GZipMiddleware`` compresses ~5–10x.
    """
    client, app = client_and_app
    # A bundle comfortably over GZIP_MIN_SIZE (1 KiB) so the middleware engages.
    big = [
        {"step_id": f"s{i}", "message": {"role": "user", "content": "z" * 200}}
        for i in range(40)
    ]
    daemon, sock, _first = _seed_bundle(client, app, "f1", big)
    try:
        # httpx transparently decodes gzip but leaves the response header intact,
        # so a full-bundle pull must advertise gzip when the client accepts it.
        resp = client.get(
            "/api/history/f1", headers={"accept-encoding": "gzip"}
        )
        assert resp.status_code == 200
        assert resp.json()["delivery"] == "full"
        assert resp.headers.get("content-encoding") == "gzip"
    finally:
        daemon.__exit__(None, None, None)


def test_small_reply_is_not_gzipped(client_and_app):
    """A tiny not_modified reply stays below the size floor → no gzip overhead."""
    client, app = client_and_app
    records = [{"step_id": "s1", "message": {"role": "user", "content": "x"}}]
    daemon, sock, first = _seed_bundle(client, app, "f1", records)
    try:
        body = first.json()
        token, sig = body["progress"], body["signature"]
        resp = client.get(
            f"/api/history/f1?after={token}&sig={sig}",
            headers={"accept-encoding": "gzip"},
        )
        assert resp.json()["delivery"] == "not_modified"
        # The tiny not_modified body is under GZIP_MIN_SIZE, so the middleware
        # leaves it uncompressed — gzip is reserved for the big full bundles.
        assert resp.headers.get("content-encoding") != "gzip"
    finally:
        daemon.__exit__(None, None, None)


# --------------------------------------------------------------------------
# full-pull throttle — unit level (deterministic monotonic floor)
# --------------------------------------------------------------------------


def test_full_pull_throttle_rate_limits_repeated_misses():
    """A full pull marked within the floor window reports throttled; outside, not."""
    state = ServerState()

    async def scenario():
        assert await state.full_pull_throttled("f1") is False  # never pulled
        await state.mark_full_pull("f1")
        # Immediately after marking, a repeat miss is throttled.
        assert await state.full_pull_throttled("f1") is True
        # A generous window still counts it as recent...
        assert await state.full_pull_throttled("f1", min_interval=1000) is True
        # ...but a zero window means "nothing is ever within the floor".
        assert await state.full_pull_throttled("f1", min_interval=0.0) is False
        # A different flow is unaffected.
        assert await state.full_pull_throttled("other") is False

    asyncio.run(scenario())


def test_signature_moves_with_record_count():
    """``bundle_signature`` is a content-version stamp: it changes as records grow.

    This is the value the endpoint hands the client as ``sig`` and compares on
    the next poll; an unchanged bundle must reproduce it (→ not_modified) while a
    new record must break it (→ delta/full).
    """
    sig_a = bundle_signature(1, 3, "m1")
    assert sig_a == bundle_signature(1, 3, "m1")  # stable for identical state
    assert sig_a != bundle_signature(1, 4, "m1")  # one more record → new sig
    assert sig_a != bundle_signature(2, 3, "m1")  # new generation → new sig


def test_not_modified_requires_a_signature():
    """State-level: an echoed token without a signature degrades to empty delta.

    ``not_modified`` is opt-in — a legacy client that echoes only a token (no
    ``sig``) must keep getting the records-empty ``delta`` it already handles,
    so the new state never reaches a consumer that cannot interpret it.
    """
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}], machine_id="m1"
        )
        full = await state.get_history_snapshot("f1")
        token = full["progress"]
        # Token echoed WITH matching signature → not_modified.
        nm = await state.get_history_snapshot(
            "f1", after=token, known_signature=full["signature"]
        )
        assert nm["delivery"] == "not_modified"
        # Same token, NO signature → records-empty delta (backward compatible).
        legacy = await state.get_history_snapshot("f1", after=token)
        assert legacy["delivery"] == "delta"
        assert legacy["records"] == []

    asyncio.run(scenario())
