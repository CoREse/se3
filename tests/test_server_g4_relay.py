"""Tests for the G4 server relay: history-index delta merge + differential
broadcast, the history bundle content signature and not-modified snapshot,
the full-rebuild throttle, keepalive liveness (no re-broadcast), the on-demand
issue/call detail endpoints, GZip on large JSON, and server wire-byte metrics.

The ``_handle_message`` unit tests drive the daemon receive-loop dispatcher
directly with a recording hub so a broadcast (or its absence) can be asserted
deterministically without a live socket.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from _authsrv import authed_hello
from fastapi.middleware.gzip import GZipMiddleware

from se3.daemon import protocol
from se3.server.state import ServerState, bundle_signature
from se3.server.ws import DetailRequestRegistry, _handle_message


# --------------------------------------------------------------------------
# ServerState — history-index delta merge
# --------------------------------------------------------------------------


def test_merge_history_index_delta_upsert_add_remove():
    state = ServerState()

    async def scenario():
        await state.update_history_index(
            "m1",
            [
                {"flow_id": "f1", "task_description": "A", "updated_at": "1"},
                {"flow_id": "f2", "task_description": "B", "updated_at": "2"},
            ],
        )
        # Upsert f1 (changed), add f3 (new), remove f2 (gone).
        await state.merge_history_index_delta(
            "m1",
            [
                {"flow_id": "f1", "task_description": "A2", "updated_at": "9"},
                {"flow_id": "f3", "task_description": "C", "updated_at": "3"},
            ],
            ["f2"],
        )
        index = await state.get_history_index()
        by_flow = {e["flow_id"]: e for e in index}
        assert set(by_flow) == {"f1", "f3"}
        assert by_flow["f1"]["task_description"] == "A2"
        assert by_flow["f3"]["task_description"] == "C"

    asyncio.run(scenario())


def test_merge_history_index_delta_ignores_rows_without_flow_id():
    state = ServerState()

    async def scenario():
        await state.merge_history_index_delta(
            "m1", [{"task_description": "no id"}, {"flow_id": "f1"}], []
        )
        index = await state.get_history_index()
        assert [e["flow_id"] for e in index] == ["f1"]

    asyncio.run(scenario())


def test_merge_delta_matches_full_reindex():
    """The delta path and a full re-push converge on the same index set."""
    delta_state = ServerState()
    full_state = ServerState()

    async def scenario():
        base = [{"flow_id": "f1", "updated_at": "1"}, {"flow_id": "f2", "updated_at": "2"}]
        await delta_state.update_history_index("m1", base)
        await full_state.update_history_index("m1", base)
        # Apply the same change two ways: as a delta, and as a full re-push.
        await delta_state.merge_history_index_delta(
            "m1", [{"flow_id": "f2", "updated_at": "9"}], []
        )
        await full_state.update_history_index(
            "m1", [{"flow_id": "f1", "updated_at": "1"}, {"flow_id": "f2", "updated_at": "9"}]
        )
        d = {e["flow_id"]: e["updated_at"] for e in await delta_state.get_history_index()}
        f = {e["flow_id"]: e["updated_at"] for e in await full_state.get_history_index()}
        assert d == f == {"f1": "1", "f2": "9"}

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# ServerState — bundle signature + not-modified snapshot
# --------------------------------------------------------------------------


def test_snapshot_exposes_signature_and_changes_with_records():
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}], machine_id="m1"
        )
        snap = await state.get_history_snapshot("f1")
        assert isinstance(snap["signature"], str) and snap["signature"]
        sig1 = snap["signature"]
        # New records → the signature must change (distinguishes 有新增).
        await state.append_history(
            "f1", protocol.HISTORY_MODE_APPEND, [{"line": 2}], machine_id="m1"
        )
        snap2 = await state.get_history_snapshot("f1")
        assert snap2["signature"] != sig1

    asyncio.run(scenario())


def test_snapshot_not_modified_when_signature_matches_and_no_new_records():
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}], machine_id="m1"
        )
        snap = await state.get_history_snapshot("f1")
        # Echo BOTH the progress token and the signature → not_modified (极小).
        again = await state.get_history_snapshot(
            "f1", after=snap["progress"], known_signature=snap["signature"]
        )
        assert again["delivery"] == "not_modified"
        assert again["records"] == []

    asyncio.run(scenario())


def test_snapshot_delta_when_new_records_even_with_signature():
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}], machine_id="m1"
        )
        snap = await state.get_history_snapshot("f1")
        await state.append_history(
            "f1", protocol.HISTORY_MODE_APPEND, [{"line": 2}], machine_id="m1"
        )
        again = await state.get_history_snapshot(
            "f1", after=snap["progress"], known_signature=snap["signature"]
        )
        # New records accrued → a delta tail, never not_modified.
        assert again["delivery"] == "delta"
        assert [r["line"] for r in again["records"]] == [2]

    asyncio.run(scenario())


def test_snapshot_legacy_client_without_signature_keeps_empty_delta():
    """A client that echoes only a token (no signature) must NOT get the new
    not_modified state — it keeps the records-empty ``delta`` it already
    handles, so the fast path is opt-in and backward compatible."""
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}], machine_id="m1"
        )
        snap = await state.get_history_snapshot("f1")
        again = await state.get_history_snapshot("f1", after=snap["progress"])
        assert again["delivery"] == "delta"
        assert again["records"] == []

    asyncio.run(scenario())


def test_snapshot_stale_signature_still_not_not_modified():
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}], machine_id="m1"
        )
        snap = await state.get_history_snapshot("f1")
        # A wrong signature (but valid token, offset==total) must NOT be
        # not_modified — it falls back to the records-empty delta.
        again = await state.get_history_snapshot(
            "f1", after=snap["progress"], known_signature="deadbeef"
        )
        assert again["delivery"] == "delta"

    asyncio.run(scenario())


def test_bundle_signature_is_cheap_and_stable():
    a = bundle_signature(3, 10, "m1")
    assert a == bundle_signature(3, 10, "m1")
    assert a != bundle_signature(3, 11, "m1")  # more records
    assert a != bundle_signature(4, 10, "m1")  # new generation
    assert a != bundle_signature(3, 10, "m2")  # different machine


# --------------------------------------------------------------------------
# ServerState — full-pull throttle
# --------------------------------------------------------------------------


def test_full_pull_throttle_window():
    state = ServerState()

    async def scenario():
        assert await state.full_pull_throttled("f1") is False
        await state.mark_full_pull("f1")
        # Immediately after marking, a repeat is throttled.
        assert await state.full_pull_throttled("f1") is True
        # A large window still throttles; a zero window never does.
        assert await state.full_pull_throttled("f1", min_interval=0.0) is False

    asyncio.run(scenario())


def test_find_call_owner_scans_pending_calls():
    state = ServerState()

    async def scenario():
        await state.update_status(
            "m1",
            {
                "flows": [
                    {
                        "flow_id": "f1",
                        "project_root": "/proj",
                        "pending_calls": [{"call_id": "c9", "prompt": "clip"}],
                    }
                ]
            },
        )
        found = await state.find_call_owner("c9")
        assert found == ("m1", "/proj")
        assert await state.find_call_owner("nope") is None

    asyncio.run(scenario())


def test_find_call_owner_disambiguates_by_project_root():
    # A local call_id is only unique within one project; two projects can each
    # hold the same id. When project_root is supplied resolution must pin to it
    # so the detail request is not misrouted to the earlier-scanned project.
    state = ServerState()

    async def scenario():
        await state.update_status(
            "m1",
            {
                "flows": [
                    {
                        "flow_id": "fa",
                        "project_root": "/proj/a",
                        "pending_calls": [{"call_id": "dup", "prompt": "a"}],
                    }
                ]
            },
        )
        await state.update_status(
            "m2",
            {
                "flows": [
                    {
                        "flow_id": "fb",
                        "project_root": "/proj/b",
                        "pending_calls": [{"call_id": "dup", "prompt": "b"}],
                    }
                ]
            },
        )
        found = await state.find_call_owner("dup", project_root="/proj/b")
        assert found == ("m2", "/proj/b")
        # Unknown project_root for that call yields no owner (not a misroute).
        assert await state.find_call_owner("dup", project_root="/proj/z") is None

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# _handle_message — keepalive / index-delta / detail-data dispatch
# --------------------------------------------------------------------------


class RecordingHub:
    """Minimal UiHub stand-in that records every broadcast for assertions."""

    def __init__(self, clients: int = 1):
        self._n = clients
        self.scoped = []
        self.owned = []

    @property
    def client_count(self) -> int:
        return self._n

    def distinct_owners(self):
        return {None}

    async def broadcast_scoped(self, payload_by_owner):
        self.scoped.append(payload_by_owner)

    async def broadcast_owned(self, payload, owner):
        self.owned.append((payload, owner))


def test_keepalive_touches_liveness_without_broadcast():
    state = ServerState()
    hub = RecordingHub()

    async def scenario():
        await state.register_machine("m1", owner_id=None)
        rec = state._machines["m1"]
        rec.last_seen = 0.0
        await _handle_message(
            protocol.make_keepalive("sig"), state, "m1", hub
        )
        # Liveness refreshed…
        assert state._machines["m1"].last_seen > 0.0
        # …but NO fan-out to browsers (the whole point of a keepalive).
        assert hub.scoped == [] and hub.owned == []

    asyncio.run(scenario())


def test_history_index_delta_merges_and_broadcasts_delta():
    state = ServerState()
    hub = RecordingHub()

    async def scenario():
        await state.register_machine("m1", owner_id=None)
        await state.update_history_index("m1", [{"flow_id": "f1", "updated_at": "1"}])
        msg = protocol.make_history_index_delta(
            [{"flow_id": "f1", "updated_at": "9"}, {"flow_id": "f2", "updated_at": "2"}],
            [],
        )
        await _handle_message(msg, state, "m1", hub)
        # Merged into the in-memory full index…
        index = await state.get_history_index()
        assert {e["flow_id"] for e in index} == {"f1", "f2"}
        # …and relayed to /ws/ui as a DELTA frame, not a full index re-fan.
        assert len(hub.owned) == 1
        payload, _owner = hub.owned[0]
        assert payload["type"] == "history_index_delta"
        assert {r["flow_id"] for r in payload["upserts"]} == {"f1", "f2"}
        assert all(r["machine_id"] == "m1" for r in payload["upserts"])

    asyncio.run(scenario())


def test_detail_data_resolves_registry_waiter():
    state = ServerState()
    reg = DetailRequestRegistry()

    async def scenario():
        fut, is_leader, active = reg.begin(
            "req1", protocol.DETAIL_KIND_ISSUE, "I1", "m1", "/p"
        )
        assert is_leader and active == "req1"
        msg = protocol.make_detail_data(
            "req1", protocol.DETAIL_KIND_ISSUE, detail={"id": "I1", "description": "full"}
        )
        await _handle_message(msg, state, "m1", detail_registry=reg)
        result = await asyncio.wait_for(fut, timeout=1.0)
        assert result["detail"]["description"] == "full"

    asyncio.run(scenario())


def test_detail_registry_followers_share_one_pull():
    reg = DetailRequestRegistry()

    async def scenario():
        # Same physical target (same machine + root) → follower joins the leader.
        f1, lead1, a1 = reg.begin("r1", protocol.DETAIL_KIND_CALL, "C1", "m1", "/p")
        f2, lead2, a2 = reg.begin("r2", protocol.DETAIL_KIND_CALL, "C1", "m1", "/p")
        assert lead1 is True and lead2 is False
        # The follower parks under the leader's request id, so ONE reply wakes both.
        assert a2 == "r1"
        reg.resolve("r1", {"ok": True, "detail": {"id": "C1"}})
        assert (await asyncio.wait_for(f1, 1.0))["detail"]["id"] == "C1"
        assert (await asyncio.wait_for(f2, 1.0))["detail"]["id"] == "C1"

    asyncio.run(scenario())


def test_detail_registry_scopes_coalescing_to_machine_and_root():
    """A same local id on a different machine/project must NOT share a pull.

    The local issue/call id (e.g. ``C1``) is only unique within one project on
    one machine. If coalescing keyed on ``(kind, id)`` alone, owner B opening
    ``C1`` on a different machine/project while owner A's ``C1`` pull is still in
    flight would join A's pull and receive A's body — a cross-owner leak. Keying
    on the full ``(kind, id, machine, root)`` tuple keeps each physical target's
    pull independent.
    """
    reg = DetailRequestRegistry()

    async def scenario():
        # Owner A: C1 on machine m1 / project /a.
        fa, leada, aa = reg.begin("ra", protocol.DETAIL_KIND_CALL, "C1", "m1", "/a")
        # Owner B: SAME local id C1 but a different machine + project.
        fb, leadb, ab = reg.begin("rb", protocol.DETAIL_KIND_CALL, "C1", "m2", "/b")
        # Both must lead their own pull — neither joins the other.
        assert leada is True and leadb is True
        assert aa == "ra" and ab == "rb"
        # A's reply resolves ONLY A's waiter; B keeps waiting for its own.
        reg.resolve("ra", {"ok": True, "detail": {"id": "C1", "body": "A-secret"}})
        assert (await asyncio.wait_for(fa, 1.0))["detail"]["body"] == "A-secret"
        assert not fb.done()
        reg.resolve("rb", {"ok": True, "detail": {"id": "C1", "body": "B-secret"}})
        assert (await asyncio.wait_for(fb, 1.0))["detail"]["body"] == "B-secret"

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Endpoint tests (TestClient with a stand-in daemon)
# --------------------------------------------------------------------------


@pytest.fixture()
def client_and_app(monkeypatch):
    from fastapi.testclient import TestClient
    import se3.server.app as app_module
    from _authsrv import authed_app, login

    monkeypatch.setattr(app_module, "HISTORY_INDEX_REFRESH_TIMEOUT", 0.3)
    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


def _drain_index_requests(daemon, want_type):
    while True:
        frame = protocol.decode(daemon.receive_text())
        if frame.type == want_type:
            return frame
        assert frame.type == protocol.MSG_HISTORY_INDEX_REQUEST


def test_gzip_middleware_registered():
    from _authsrv import authed_app

    app, _ = authed_app()
    assert any(mw.cls is GZipMiddleware for mw in app.user_middleware)


def test_run_enables_ws_per_message_deflate(monkeypatch):
    """``run()`` must pass ``ws_per_message_deflate=True`` to uvicorn.

    This is one of the two WS-level compression legs the traffic-reduction work
    installed (the daemon leg is locked in test_daemon_traffic_reduction). A
    later refactor that drops the kwarg would silently multiply steady-state WS
    traffic on full-frame sends, so pin it here.
    """
    import sys
    import types

    from se3.server import app as app_mod

    captured = {}

    def _fake_run(application, **kwargs):
        captured["kwargs"] = kwargs

    fake_uvicorn = types.SimpleNamespace(run=_fake_run)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    # Skip the (heavy) real app build — we only assert the run() kwargs.
    monkeypatch.setattr(app_mod, "create_app", lambda **kw: object())

    app_mod.run(host="127.0.0.1", port=0)

    assert captured["kwargs"].get("ws_per_message_deflate") is True
    assert captured["kwargs"].get("ws_max_size") == protocol.MAX_WS_MESSAGE_BYTES


def test_run_falls_back_when_uvicorn_lacks_deflate_kwarg(monkeypatch):
    """On an older uvicorn (no ``ws_per_message_deflate``), ``run()`` retries
    without the kwarg rather than crashing — the websockets protocol still
    negotiates deflate by default, so the fallback is safe."""
    import sys
    import types

    from se3.server import app as app_mod

    calls = []

    def _fake_run(application, **kwargs):
        calls.append(kwargs)
        if "ws_per_message_deflate" in kwargs:
            raise TypeError("run() got an unexpected keyword argument")

    fake_uvicorn = types.SimpleNamespace(run=_fake_run)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(app_mod, "create_app", lambda **kw: object())

    app_mod.run(host="127.0.0.1", port=0)

    assert len(calls) == 2
    assert "ws_per_message_deflate" in calls[0]
    assert "ws_per_message_deflate" not in calls[1]


def test_history_bundle_large_json_served_intact(client_and_app):
    """A large full bundle survives the GZip middleware end to end."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        big = [{"step": "s1", "line": "x" * 2000, "ordinal": i} for i in range(50)]
        daemon.send_text(
            protocol.make_history_data("f1", protocol.HISTORY_MODE_FULL, big).to_json()
        )
        resp = client.get("/api/history/f1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["delivery"] == "full"
        assert len(body["records"]) == 50
        assert "signature" in body


def test_history_not_modified_via_rest(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(protocol.make_history_index([{"flow_id": "f1"}]).to_json())
        for _ in range(50):
            if client.get("/api/history").json()["sessions"]:
                break
        daemon.send_text(
            protocol.make_history_data(
                "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}]
            ).to_json()
        )
        first = None
        for _ in range(50):
            r = client.get("/api/history/f1")
            if r.json().get("cached"):
                first = r.json()
                break
        assert first is not None and first["delivery"] == "full"
        again = client.get(
            "/api/history/f1",
            params={"after": first["progress"], "sig": first["signature"]},
        ).json()
        assert again["delivery"] == "not_modified"
        assert again["records"] == []


def test_issue_detail_endpoint_pulls_full_text(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        # Report an issue (truncated) via STATUS_UPDATE so the server can resolve
        # its owning machine / root.
        daemon.send_text(
            protocol.make_status_update(
                {
                    "issues": [
                        {"id": "I1", "project_root": "/proj", "description": "clip"}
                    ]
                }
            ).to_json()
        )
        for _ in range(50):
            if client.get("/api/issues", params={"include_closed": True}).json()["count"]:
                break

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/issues/I1/detail")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            req = _drain_index_requests(daemon, protocol.MSG_DETAIL_REQUEST)
            assert req.payload["kind"] == protocol.DETAIL_KIND_ISSUE
            assert req.payload["target_id"] == "I1"
            daemon.send_text(
                protocol.make_detail_data(
                    req.payload["request_id"],
                    protocol.DETAIL_KIND_ISSUE,
                    detail={"id": "I1", "description": "the full untruncated body"},
                ).to_json()
            )
        finally:
            worker.join(timeout=5)
        resp = result["resp"]
        assert resp.status_code == 200
        assert resp.json()["issue"]["description"] == "the full untruncated body"


def test_call_detail_endpoint_resolves_owner_and_pulls(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(
            protocol.make_status_update(
                {
                    "flows": [
                        {
                            "flow_id": "f1",
                            "project_root": "/proj",
                            "pending_calls": [{"call_id": "c9", "prompt": "clip"}],
                        }
                    ]
                }
            ).to_json()
        )
        for _ in range(50):
            flows = client.get("/api/machines/m1/flows").json().get("flows")
            if flows:
                break

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/calls/c9/detail")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            req = _drain_index_requests(daemon, protocol.MSG_DETAIL_REQUEST)
            assert req.payload["kind"] == protocol.DETAIL_KIND_CALL
            assert req.payload["target_id"] == "c9"
            daemon.send_text(
                protocol.make_detail_data(
                    req.payload["request_id"],
                    protocol.DETAIL_KIND_CALL,
                    detail={"call_id": "c9", "prompt": "full prompt body"},
                ).to_json()
            )
        finally:
            worker.join(timeout=5)
        resp = result["resp"]
        assert resp.status_code == 200
        assert resp.json()["call"]["prompt"] == "full prompt body"


def test_call_detail_no_daemon_404(client_and_app):
    client, _ = client_and_app
    assert client.get("/api/calls/ghost/detail").status_code == 404


def test_wire_metrics_endpoint_counts_downlink(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        # A GET /api/history broadcasts a HISTORY_INDEX_REQUEST downlink; drain it
        # so the send is accounted.
        client.get("/api/history")
        _drain_index_requests_soft(daemon)
        snap = client.get("/api/wire-metrics").json()["metrics"]
        assert "__total__" in snap
        assert snap["__total__"]["bytes"] > 0


def _drain_index_requests_soft(daemon):
    # Best-effort: read one frame (the index-refresh request) if present.
    try:
        protocol.decode(daemon.receive_text())
    except Exception:
        pass


def _authed_hello_v2(app, machine_id="m1"):
    """An authenticated HELLO advertising the pre-detail (revision-2) protocol.

    Such a daemon never learned MSG_DETAIL_REQUEST, so the server must serve the
    STATUS_UPDATE mirror on the detail leg rather than routing a doomed pull.
    """
    msg = protocol.make_hello(
        machine_id, "host", "6.4.0", key=app.state.test_daemon_key
    )
    msg.payload["protocol_version"] = "2"
    return msg.to_json()


def test_wire_metrics_records_welcome_and_ping(client_and_app):
    # The handshake WELCOME (and the periodic PING) are written straight to the
    # daemon socket, bypassing ConnectionManager routing; they must still show up
    # in the per-type server→daemon metrics so idle traffic is not under-reported.
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        snap = client.get("/api/wire-metrics").json()["metrics"]
        assert snap.get(protocol.MSG_WELCOME, {}).get("bytes", 0) > 0
        assert snap.get(protocol.MSG_WELCOME, {}).get("count", 0) >= 1


def test_issue_detail_pre_v3_daemon_serves_mirror(client_and_app):
    # A pre-v3 daemon would silently drop a MSG_DETAIL_REQUEST, so opening an
    # issue whose (un-clipped) description exceeds DESC_CLIP must NOT park a
    # doomed waiter (→ 10 s / 504). The mirror already holds the full body; serve
    # it directly. No DETAIL_REQUEST is expected on the wire.
    client, app = client_and_app
    full_desc = "x" * 500  # >200 chars: would look "truncated" to the frontend
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(_authed_hello_v2(app, "m1"))
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(
            protocol.make_status_update(
                {
                    "issues": [
                        {"id": "I1", "project_root": "/proj", "description": full_desc}
                    ]
                }
            ).to_json()
        )
        for _ in range(50):
            if client.get("/api/issues", params={"include_closed": True}).json()["count"]:
                break
        # Must return immediately with the full body. A synchronous GET here
        # would block for DETAIL_PULL_TIMEOUT (→ 504) if the server had parked a
        # doomed detail pull, so a fast 200 with the mirror body proves the
        # pre-v3 fall-back short-circuited the downlink.
        resp = client.get("/api/issues/I1/detail")
        assert resp.status_code == 200
        assert resp.json()["issue"]["description"] == full_desc


def test_call_detail_pre_v3_daemon_serves_mirror(client_and_app):
    client, app = client_and_app
    full_prompt = "p" * 500
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(_authed_hello_v2(app, "m1"))
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(
            protocol.make_status_update(
                {
                    "flows": [
                        {
                            "flow_id": "f1",
                            "project_root": "/proj",
                            "pending_calls": [{"call_id": "c9", "prompt": full_prompt}],
                        }
                    ]
                }
            ).to_json()
        )
        for _ in range(50):
            flows = client.get("/api/machines/m1/flows").json().get("flows")
            if flows:
                break
        # A fast 200 (rather than a 10 s / 504 park) proves the pre-v3 fall-back
        # served the mirror instead of routing a doomed DETAIL_REQUEST.
        resp = client.get("/api/calls/c9/detail")
        assert resp.status_code == 200
        assert resp.json()["call"]["prompt"] == full_prompt
