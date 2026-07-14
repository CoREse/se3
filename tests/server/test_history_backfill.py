"""Tests for the ``missing=`` backfill read on ``GET /api/history/{flow_id}`` (G2).

The progress token's ``offset`` is the server's self-signed claim of what it
SENT; it cannot witness what the client KEPT. A record dropped in flight leaves a
hole the token can never see, and ``not_modified`` then locks that hole in
forever (the live head-loss defect). The fix moves the completeness judgement to
the client, which checks its held records against the authoritative per-file
``cursor`` and names the numbers it is missing; this module covers the server
half: parsing that list, and serving exactly those records out of the SAME cached
bundle as ``delivery: "backfill"``.

The token's minting semantics are unchanged, which is asserted explicitly — a
backfill is an extra read of one bundle, not a new token dialect.
"""

from __future__ import annotations

import pytest

from _authsrv import authed_app, authed_hello, login
from se3.daemon import protocol
from se3.server.app import MISSING_MAX_ORDINALS, parse_missing_param


def _rec(step_id, ordinal, role, content):
    return {
        "step_id": step_id,
        "step_type": "discovery",
        "ordinal": ordinal,
        "message": {"role": role, "content": content},
    }


STEP = "01_discovery_9ed2a95c"
HEAD = _rec(STEP, 0, "user", "the head prompt nobody ever saw")
TAIL = _rec(STEP, 1, "assistant", "the tail the client does hold")


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


def _seed_bundle(client, app, flow_id, records):
    """Connect a daemon, push a full bundle, return (daemon ctx, sock, first reply)."""
    daemon = client.websocket_connect("/ws")
    sock = daemon.__enter__()
    sock.send_text(authed_hello(app, "m1", "host", "6.4.0"))
    protocol.decode(sock.receive_text())  # WELCOME
    sock.send_text(
        protocol.make_history_data(
            flow_id, protocol.HISTORY_MODE_FULL, records
        ).to_json()
    )
    for _ in range(50):
        resp = client.get(f"/api/history/{flow_id}")
        if resp.status_code == 200 and resp.json().get("cached"):
            return daemon, sock, resp
    daemon.__exit__(None, None, None)
    raise AssertionError("bundle never became cache-visible")


# --------------------------------------------------------------------------
# missing= parsing (pure function)
# --------------------------------------------------------------------------


def test_parse_missing_param_wire_forms():
    assert parse_missing_param("s1:0") == {"s1": [0]}
    assert parse_missing_param("s1:0,2;s2:1") == {"s1": [0, 2], "s2": [1]}
    # Repeated numbers collapse; whitespace around the tokens is tolerated.
    assert parse_missing_param(" s1 : 0 , 0 , 3 ") == {"s1": [0, 3]}
    # A ``.from-<branch>`` sidecar step id keeps its suffix (a distinct stream).
    assert parse_missing_param("01_d.from-main:0") == {"01_d.from-main": [0]}


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "s1",  # no ordinals
        "s1:",  # empty ordinal list
        "s1:x",  # non-numeric
        "s1:-1",  # negative
        "s1:1.5",  # non-integer
        ":0",  # no step id
        "s1:0;" + ";".join(f"s{i}:{i}" for i in range(MISSING_MAX_ORDINALS + 5)),
    ],
)
def test_parse_missing_param_degrades_to_none(raw):
    """Every malformed / over-long input degrades to "no missing list", never raises.

    The backfill is an optimisation over the existing full fallback, so a client
    on a different encoding must still get correct history — just not a slice.
    """
    assert parse_missing_param(raw) is None


def test_malformed_missing_is_not_a_500(client_and_app):
    client, app = client_and_app
    daemon, _sock, first = _seed_bundle(client, app, "f1", [HEAD, TAIL])
    try:
        body = first.json()
        got = client.get(
            f"/api/history/f1?after={body['progress']}&sig={body['signature']}"
            "&missing=%3B%3Bgarbage"
        )
        assert got.status_code == 200
        # Ignored ⇒ the ordinary token semantics apply: the client is in sync.
        assert got.json()["delivery"] == "not_modified"
    finally:
        daemon.__exit__(None, None, None)


# --------------------------------------------------------------------------
# backfill delivery
# --------------------------------------------------------------------------


def test_live_shape_backfill_returns_exactly_the_head(client_and_app):
    """The live defect's shape: bundle has 2 records, token says o=2, head is gone.

    The client holds only ``STEP#1`` but its receipt claims full sync — the
    absorbing state. Its cursor self-check names ordinal 0, and the server hands
    back exactly that record.
    """
    client, app = client_and_app
    daemon, _sock, first = _seed_bundle(client, app, "f1", [HEAD, TAIL])
    try:
        body = first.json()
        token, sig = body["progress"], body["signature"]
        # Sanity: without a missing list this is the frozen not_modified reply.
        frozen = client.get(f"/api/history/f1?after={token}&sig={sig}").json()
        assert frozen["delivery"] == "not_modified"
        assert frozen["records"] == []
        assert frozen["cursor"] == {}  # a full push carries no cursor

        got = client.get(
            f"/api/history/f1?after={token}&sig={sig}&missing={STEP}:0"
        ).json()
        assert got["delivery"] == "backfill"
        assert got["records"] == [HEAD]
        # Same key set as every other delivery — the client's merge path is one.
        for key in ("progress", "signature", "cursor", "machine_id", "mode"):
            assert key in got
    finally:
        daemon.__exit__(None, None, None)


def test_backfill_unions_with_the_tail_in_bundle_order(client_and_app):
    """A behind-the-count token PLUS a missing head: both travel, once, in order."""
    client, app = client_and_app
    mid = _rec(STEP, 2, "user", "second round")
    daemon, sock, first = _seed_bundle(client, app, "f1", [HEAD, TAIL])
    try:
        token = first.json()["progress"]  # minted at offset 2
        sig = first.json()["signature"]
        sock.send_text(
            protocol.make_history_data(
                "f1", protocol.HISTORY_MODE_APPEND, [mid]
            ).to_json()
        )
        got = None
        for _ in range(50):
            resp = client.get(
                f"/api/history/f1?after={token}&sig={sig}&missing={STEP}:0"
            ).json()
            if len(resp["records"]) == 2:
                got = resp
                break
        assert got is not None, "the appended tail never surfaced"
        assert got["delivery"] == "backfill"
        # ordinal 0 (named) + ordinal 2 (the tail after the token's offset),
        # in bundle order; ordinal 1 — already held — does not travel.
        assert got["records"] == [HEAD, mid]
    finally:
        daemon.__exit__(None, None, None)


def test_missing_number_naming_a_record_in_the_tail_travels_once(client_and_app):
    """A named number that also lies in the token's tail is not duplicated."""
    client, app = client_and_app
    daemon, _sock, first = _seed_bundle(client, app, "f1", [HEAD, TAIL])
    try:
        sig = first.json()["signature"]
        # A token at offset 0 (no ``after``) would be full, so ask with the
        # in-sync token and name BOTH records: the union must still be 2 records.
        token = first.json()["progress"]
        got = client.get(
            f"/api/history/f1?after={token}&sig={sig}&missing={STEP}:0,1"
        ).json()
        assert got["delivery"] == "backfill"
        assert got["records"] == [HEAD, TAIL]
    finally:
        daemon.__exit__(None, None, None)


def test_unlocatable_number_is_declared_unfillable_not_rebuilt(client_and_app):
    """A number the bundle holds no record for is NAMED back, never escalated to full.

    The cursor counts physical lines, so a number under it need not name a record
    at all (a blank / unparseable line advances the cursor without emitting one).
    Rebuilding would hand the client the very same bundle — still without that
    number — so it would re-detect the identical hole on the next signal and ask
    again forever. Declaring the number unfillable lets the client retire it.
    """
    client, app = client_and_app
    daemon, _sock, first = _seed_bundle(client, app, "f1", [HEAD, TAIL])
    try:
        body = first.json()
        got = client.get(
            f"/api/history/f1?after={body['progress']}&sig={body['signature']}"
            f"&missing={STEP}:7"
        ).json()
        assert got["delivery"] == "backfill"
        assert got["records"] == []          # in sync; nothing else to send
        assert got["unfillable"] == {STEP: [7]}
    finally:
        daemon.__exit__(None, None, None)


def test_partially_locatable_missing_serves_what_exists(client_and_app):
    """The numbers that DO exist are served; the rest come back as ``unfillable``."""
    client, app = client_and_app
    daemon, _sock, first = _seed_bundle(client, app, "f1", [HEAD, TAIL])
    try:
        body = first.json()
        got = client.get(
            f"/api/history/f1?after={body['progress']}&sig={body['signature']}"
            f"&missing={STEP}:0,7"
        ).json()
        assert got["delivery"] == "backfill"
        assert got["records"] == [HEAD]
        assert got["unfillable"] == {STEP: [7]}
    finally:
        daemon.__exit__(None, None, None)


def test_legacy_records_without_ordinals_are_served_by_a_full_bundle(client_and_app):
    """A pre-ordinal record the bundle HOLDS must reach the client, not be disowned.

    An un-numbered record cannot be found by index, but "the index cannot find it"
    is not "the bundle does not have it". Declaring it unfillable would make the
    client retire the number and read clean forever, so a record the server holds
    — and a full delivery would render — would stay invisible for the life of the
    page: the head-loss defect, re-created. The only answer that carries an
    un-numbered record is the whole bundle.
    """
    client, app = client_and_app
    legacy = [
        {"step_id": STEP, "message": {"role": "user", "content": "head"}},
        {"step_id": STEP, "message": {"role": "assistant", "content": "tail"}},
    ]
    daemon, _sock, first = _seed_bundle(client, app, "f1", legacy)
    try:
        body = first.json()
        got = client.get(
            f"/api/history/f1?after={body['progress']}&sig={body['signature']}"
            f"&missing={STEP}:0"
        ).json()
        assert got["delivery"] == "full"
        assert got["records"] == legacy
        assert got["unfillable"] == {}
    finally:
        daemon.__exit__(None, None, None)


def test_mixed_bundle_serves_the_unnumbered_head_via_full(client_and_app):
    """The live mixed shape: the head predates ordinals, the tail carries one.

    The client holds only the numbered tail and asks for ordinal 0 — which names
    a record the bundle really holds, just un-numbered. Answering with a numbered
    slice would omit it and (via ``unfillable``) tell the client to stop asking.
    The step is partially numbered, so a failed lookup there is ambiguous and the
    request is answered with the complete bundle, head included.
    """
    client, app = client_and_app
    legacy_head = {"step_id": STEP, "message": {"role": "user", "content": "head"}}
    daemon, _sock, first = _seed_bundle(client, app, "f1", [legacy_head, TAIL])
    try:
        body = first.json()
        got = client.get(
            f"/api/history/f1?after={body['progress']}&sig={body['signature']}"
            f"&missing={STEP}:0"
        ).json()
        assert got["delivery"] == "full"
        assert got["records"] == [legacy_head, TAIL]
        assert got["unfillable"] == {}
    finally:
        daemon.__exit__(None, None, None)


def test_every_delivery_names_its_bundle_generation(client_and_app):
    """The reply carries the bundle's generation on every delivery.

    The client scopes its repair budget and its retired-unfillable numbers to ONE
    bundle: both are void when the daemon replaces it (a number legitimately
    unfillable in the old bundle can be a real record in the new one). The
    signature cannot key that state — it is re-minted on every append — and the
    token is opaque, so the generation is the only stable per-bundle identity the
    client can see.
    """
    client, app = client_and_app
    daemon, _sock, first = _seed_bundle(client, app, "f1", [HEAD, TAIL])
    try:
        body = first.json()
        gen = body["generation"]
        assert isinstance(gen, int) and gen > 0
        token, sig = body["progress"], body["signature"]
        for url in (
            f"/api/history/f1?after={token}&sig={sig}",                  # not_modified
            f"/api/history/f1?after={token}&sig={sig}&missing={STEP}:0",  # backfill
            "/api/history/f1",                                           # full
        ):
            assert client.get(url).json()["generation"] == gen
    finally:
        daemon.__exit__(None, None, None)


def test_stale_token_ignores_missing_and_serves_full(client_and_app):
    """Without a valid token the numbering is not anchored to this bundle → full."""
    client, app = client_and_app
    daemon, _sock, _first = _seed_bundle(client, app, "f1", [HEAD, TAIL])
    try:
        got = client.get(
            f"/api/history/f1?after=forged&sig=nope&missing={STEP}:0"
        ).json()
        assert got["delivery"] == "full"
        assert got["records"] == [HEAD, TAIL]
        # No token at all: same fallback.
        bare = client.get(f"/api/history/f1?missing={STEP}:0").json()
        assert bare["delivery"] == "full"
        assert bare["records"] == [HEAD, TAIL]
    finally:
        daemon.__exit__(None, None, None)


# --------------------------------------------------------------------------
# token semantics are unchanged
# --------------------------------------------------------------------------


def test_token_and_signature_minting_is_unchanged_by_missing(client_and_app):
    """The same bundle mints the SAME progress token + signature, with or without
    ``missing`` — and the not_modified / delta / full state machine is untouched.
    """
    client, app = client_and_app
    daemon, sock, first = _seed_bundle(client, app, "f1", [HEAD, TAIL])
    try:
        body = first.json()
        token, sig = body["progress"], body["signature"]

        plain = client.get(f"/api/history/f1?after={token}&sig={sig}").json()
        backfilled = client.get(
            f"/api/history/f1?after={token}&sig={sig}&missing={STEP}:0"
        ).json()
        # ``o`` still means "records in this bundle" — a backfill does not
        # rewind, advance, or otherwise reinterpret the receipt.
        assert backfilled["progress"] == plain["progress"] == token
        assert backfilled["signature"] == plain["signature"] == sig

        # The three-state machine still behaves exactly as before: an append moves
        # the old token behind the count, so it reads as a delta carrying the tail.
        mid = _rec(STEP, 2, "user", "next round")
        sock.send_text(
            protocol.make_history_data(
                "f1", protocol.HISTORY_MODE_APPEND, [mid]
            ).to_json()
        )
        delta = None
        for _ in range(50):
            got = client.get(f"/api/history/f1?after={token}&sig={sig}").json()
            if got["delivery"] == "delta":
                delta = got
                break
        assert delta is not None
        assert delta["records"] == [mid]
        assert delta["signature"] != sig
        # And echoing the freshly minted token is in sync again.
        nm = client.get(
            f"/api/history/f1?after={delta['progress']}&sig={delta['signature']}"
        ).json()
        assert nm["delivery"] == "not_modified"
        assert nm["records"] == []
    finally:
        daemon.__exit__(None, None, None)
