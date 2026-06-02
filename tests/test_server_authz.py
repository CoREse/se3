"""Tests for the daemon-channel identity resolution and owner-scoped views.

Covers group G6:

* ``MachineRecord.owner_id`` and the owner-filtered ``ServerState`` queries
  (one owner never sees another owner's machines / flows / history);
* ``handle_daemon_connection`` HELLO key → owner resolution and the fail-closed
  reject of a missing / invalid key;
* the owner-scoped ``/ws/ui`` push paths (``UiHub`` filtering, the ``_push_*``
  helpers, and the ``handle_ui_connection`` unauthenticated reject).

These exercise ``se3.server`` directly with lightweight fake WebSockets, so no
FastAPI test client (and no live event loop wiring) is needed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from se3.daemon import protocol
from se3.server.crypto import generate_token
from se3.server.identity import IdentityService
from se3.server.persistence import Store
from se3.server.state import MachineRecord, ServerState
from se3.server.ws import (
    ConnectionManager,
    UiHub,
    _push_history_data,
    _push_history_index,
    _push_state,
    handle_daemon_connection,
    handle_ui_connection,
)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class _Disconnect(Exception):
    """Signals the fake socket has no more frames (a client disconnect)."""


class _FakeDaemonWS:
    """A server-side daemon socket stand-in driven by a queued frame list."""

    def __init__(self, frames):
        self._incoming = list(frames)
        self.sent = []
        self.accepted = False
        self.closed = False

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if self._incoming:
            return self._incoming.pop(0)
        raise _Disconnect()

    async def send_text(self, data):
        self.sent.append(data)

    async def close(self, code=1000):
        self.closed = True

    def welcomes(self):
        """Decoded WELCOME messages the server sent down this socket."""
        out = []
        for raw in self.sent:
            msg = protocol.decode(raw)
            if msg.type == protocol.MSG_WELCOME:
                out.append(msg)
        return out


class _FakeUiWS:
    """A web-frontend socket stand-in: captures sent JSON payloads."""

    def __init__(self, frames=None):
        self._incoming = list(frames or [])
        self.sent = []
        self.accepted = False
        self.closed = False
        self.close_code = None

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if self._incoming:
            return self._incoming.pop(0)
        raise _Disconnect()

    async def send_text(self, data):
        self.sent.append(json.loads(data))

    async def close(self, code=1000):
        self.closed = True
        self.close_code = code


def _make_identity_with_key(label="alice"):
    """Return ``(identity, owner_id, key_plaintext)`` for a fresh issued key."""
    store = Store(":memory:")
    owner_id = store.create_owner(label)
    plaintext, key_hash = generate_token("dk")
    store.issue_daemon_key(owner_id, key_hash)
    return IdentityService(store), owner_id, plaintext


# --------------------------------------------------------------------------
# Task 1 — MachineRecord.owner_id + owner-scoped ServerState queries
# --------------------------------------------------------------------------


def test_machine_record_owner_id_in_to_dict():
    rec = MachineRecord(machine_id="m1", owner_id="owner-A")
    assert rec.owner_id == "owner-A"
    assert rec.to_dict()["owner_id"] == "owner-A"
    assert rec.to_dict(include_flows=False)["owner_id"] == "owner-A"
    # Default (unbound) record exposes None, not a missing key.
    assert MachineRecord(machine_id="m2").to_dict()["owner_id"] is None


def test_register_machine_records_owner_id():
    async def scenario():
        state = ServerState()
        rec = await state.register_machine("m1", "host", "6.4.0", owner_id="A")
        assert rec.owner_id == "A"
        # A reconnect re-asserts the owner.
        rec2 = await state.register_machine("m1", "host", "6.4.0", owner_id="A")
        assert rec2.owner_id == "A"
        assert await state.get_machine_owner("m1") == "A"
        assert await state.get_machine_owner("ghost") is None

    asyncio.run(scenario())


def test_owner_scoped_machine_queries_isolate_owners():
    async def scenario():
        state = ServerState()
        await state.register_machine("mA", "hA", owner_id="A")
        await state.register_machine("mB", "hB", owner_id="B")
        await state.update_status(
            "mA", {"flows": [{"flow_id": "fA", "status": "running"}]}
        )
        await state.update_status(
            "mB", {"flows": [{"flow_id": "fB", "status": "running"}]}
        )

        # get_machines
        a_ids = {m["machine_id"] for m in await state.get_machines(owner="A")}
        assert a_ids == {"mA"}
        b_ids = {m["machine_id"] for m in await state.get_machines(owner="B")}
        assert b_ids == {"mB"}
        all_ids = {m["machine_id"] for m in await state.get_machines()}
        assert all_ids == {"mA", "mB"}

        # get_machines_full
        a_full = await state.get_machines_full(owner="A")
        assert [m["machine_id"] for m in a_full] == ["mA"]

        # get_machine — cross-owner reads as absent (no existence leak)
        assert (await state.get_machine("mA", owner="A"))["machine_id"] == "mA"
        assert await state.get_machine("mA", owner="B") is None
        assert await state.get_machine("mB", owner="A") is None

        # get_machine_flows
        assert await state.get_machine_flows("mB", owner="A") is None
        flows_b = await state.get_machine_flows("mB", owner="B")
        assert [f["flow_id"] for f in flows_b] == ["fB"]

        # get_flow / find_machine_for_flow
        assert await state.get_flow("fA", owner="B") is None
        owned = await state.get_flow("fA", owner="A")
        assert owned is not None and owned[0] == "mA"
        assert await state.find_machine_for_flow("fB", owner="A") is None
        assert await state.find_machine_for_flow("fB", owner="B") == "mB"
        # Unscoped still finds everything.
        assert (await state.get_flow("fB"))[0] == "mB"

    asyncio.run(scenario())


def test_unbound_machine_invisible_to_scoped_view():
    """A machine with no owner (owner_id None) is fail-closed out of scoped views."""

    async def scenario():
        state = ServerState()
        await state.register_machine("m0", "h", owner_id=None)
        assert await state.get_machines(owner="A") == []
        assert await state.get_machine("m0", owner="A") is None
        # The admin / unscoped view still sees it.
        assert len(await state.get_machines()) == 1

    asyncio.run(scenario())


def test_owner_scoped_history_index_and_lookup():
    async def scenario():
        state = ServerState()
        await state.register_machine("mA", owner_id="A")
        await state.register_machine("mB", owner_id="B")
        await state.update_history_index("mA", [{"flow_id": "fA", "updated_at": "2"}])
        await state.update_history_index("mB", [{"flow_id": "fB", "updated_at": "1"}])

        a_sessions = await state.get_history_index(owner="A")
        assert [s["flow_id"] for s in a_sessions] == ["fA"]
        b_sessions = await state.get_history_index(owner="B")
        assert [s["flow_id"] for s in b_sessions] == ["fB"]
        # Unscoped aggregates both (sorted by updated_at desc).
        assert [s["flow_id"] for s in await state.get_history_index()] == ["fA", "fB"]

        # find_machine_for_history_flow honours owner scoping.
        assert await state.find_machine_for_history_flow("fB", owner="A") is None
        assert await state.find_machine_for_history_flow("fB", owner="B") == "mB"
        assert await state.find_machine_for_history_flow("fA") == "mA"

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Task 2 — daemon HELLO key → owner resolution / fail-closed reject
# --------------------------------------------------------------------------


def test_daemon_hello_valid_key_binds_owner_and_accepts():
    identity, owner_id, key = _make_identity_with_key()

    async def scenario():
        state = ServerState()
        manager = ConnectionManager()
        ws = _FakeDaemonWS(
            [protocol.make_hello("m1", "host", "6.4.0", key=key).to_json()]
        )
        await handle_daemon_connection(ws, manager, state, identity=identity)

        welcomes = ws.welcomes()
        assert welcomes and welcomes[0].payload["accepted"] is True
        # The machine is bound to the resolved owner in both the state record
        # and the identity live index.
        assert await state.get_machine_owner("m1") == owner_id
        assert identity.owner_of_machine("m1") == owner_id
        rec = await state.get_machine("m1", owner=owner_id)
        assert rec is not None and rec["owner_id"] == owner_id
        # The secret key is never echoed back in any sent frame.
        assert all(key not in raw for raw in ws.sent)

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["bogus-key", ""])
def test_daemon_hello_invalid_or_missing_key_rejected_fail_closed(key):
    identity, _owner_id, _good = _make_identity_with_key()

    async def scenario():
        state = ServerState()
        manager = ConnectionManager()
        # Frame 2 (a STATUS_UPDATE) must NOT be processed — a rejected daemon
        # never enters the receive loop.
        frames = [
            protocol.make_hello("m1", "host", "6.4.0", key=key).to_json(),
            protocol.make_status_update(
                {"flows": [{"flow_id": "leak", "status": "running"}]}
            ).to_json(),
        ]
        ws = _FakeDaemonWS(frames)
        await handle_daemon_connection(ws, manager, state, identity=identity)

        welcomes = ws.welcomes()
        assert welcomes and welcomes[0].payload["accepted"] is False
        assert ws.closed is True
        # Not registered, not connected, not bound — and no leaked flow.
        assert await state.get_machine("m1") is None
        assert manager.is_connected("m1") is False
        assert identity.owner_of_machine("m1") is None
        assert await state.get_flow("leak") is None

    asyncio.run(scenario())


def test_daemon_hello_no_identity_is_unauthenticated_passthrough():
    """Without an identity service the channel stays unauthenticated (owner None)."""

    async def scenario():
        state = ServerState()
        manager = ConnectionManager()
        ws = _FakeDaemonWS(
            [protocol.make_hello("m1", "host", "6.4.0").to_json()]
        )
        await handle_daemon_connection(ws, manager, state)
        welcomes = ws.welcomes()
        assert welcomes and welcomes[0].payload["accepted"] is True
        assert await state.get_machine_owner("m1") is None

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Task 3 — owner-scoped /ws/ui pushes
# --------------------------------------------------------------------------


def test_uihub_broadcast_scoped_routes_per_owner():
    async def scenario():
        hub = UiHub()
        wsA, wsB, wsAdmin = _FakeUiWS(), _FakeUiWS(), _FakeUiWS()
        await hub.register(wsA, "A")
        await hub.register(wsB, "B")
        await hub.register(wsAdmin, None)
        assert hub.distinct_owners() == {"A", "B", None}

        await hub.broadcast_scoped(
            {
                "A": {"type": "x", "v": "a"},
                "B": {"type": "x", "v": "b"},
                None: {"type": "x", "v": "all"},
            }
        )
        assert wsA.sent[-1]["v"] == "a"
        assert wsB.sent[-1]["v"] == "b"
        assert wsAdmin.sent[-1]["v"] == "all"

        # A None payload for an owner sends that owner nothing.
        await hub.broadcast_scoped({"A": {"type": "y"}, "B": None, None: None})
        assert wsA.sent[-1]["type"] == "y"
        assert len(wsB.sent) == 1  # unchanged
        assert len(wsAdmin.sent) == 1  # unchanged

    asyncio.run(scenario())


def test_uihub_broadcast_owned_only_owner_and_admin():
    async def scenario():
        hub = UiHub()
        wsA, wsB, wsAdmin = _FakeUiWS(), _FakeUiWS(), _FakeUiWS()
        await hub.register(wsA, "A")
        await hub.register(wsB, "B")
        await hub.register(wsAdmin, None)

        await hub.broadcast_owned({"type": "evt", "for": "A"}, "A")
        assert wsA.sent and wsA.sent[-1]["for"] == "A"
        assert wsAdmin.sent and wsAdmin.sent[-1]["for"] == "A"
        assert wsB.sent == []  # owner B never sees owner A's data

        # Data with no owner is visible only to the admin view.
        await hub.broadcast_owned({"type": "evt", "for": "none"}, None)
        assert wsAdmin.sent[-1]["for"] == "none"
        assert all(p.get("for") != "none" for p in wsA.sent)
        assert wsB.sent == []

    asyncio.run(scenario())


def test_push_state_is_owner_scoped():
    async def scenario():
        state = ServerState()
        await state.register_machine("mA", owner_id="A")
        await state.register_machine("mB", owner_id="B")
        hub = UiHub()
        wsA, wsB, wsAdmin = _FakeUiWS(), _FakeUiWS(), _FakeUiWS()
        await hub.register(wsA, "A")
        await hub.register(wsB, "B")
        await hub.register(wsAdmin, None)

        await _push_state(hub, state, "status_update")

        a_machines = {m["machine_id"] for m in wsA.sent[-1]["machines"]}
        b_machines = {m["machine_id"] for m in wsB.sent[-1]["machines"]}
        admin_machines = {m["machine_id"] for m in wsAdmin.sent[-1]["machines"]}
        assert a_machines == {"mA"}
        assert b_machines == {"mB"}
        assert admin_machines == {"mA", "mB"}

    asyncio.run(scenario())


def test_push_history_index_is_owner_scoped():
    async def scenario():
        state = ServerState()
        await state.register_machine("mA", owner_id="A")
        await state.register_machine("mB", owner_id="B")
        await state.update_history_index("mA", [{"flow_id": "fA", "updated_at": "1"}])
        await state.update_history_index("mB", [{"flow_id": "fB", "updated_at": "1"}])
        hub = UiHub()
        wsA, wsAdmin = _FakeUiWS(), _FakeUiWS()
        await hub.register(wsA, "A")
        await hub.register(wsAdmin, None)

        await _push_history_index(hub, state)

        assert {s["flow_id"] for s in wsA.sent[-1]["sessions"]} == {"fA"}
        assert {s["flow_id"] for s in wsAdmin.sent[-1]["sessions"]} == {"fA", "fB"}

    asyncio.run(scenario())


def test_push_history_data_scoped_to_machine_owner():
    async def scenario():
        state = ServerState()
        await state.register_machine("mA", owner_id="A")
        hub = UiHub()
        wsA, wsB, wsAdmin = _FakeUiWS(), _FakeUiWS(), _FakeUiWS()
        await hub.register(wsA, "A")
        await hub.register(wsB, "B")
        await hub.register(wsAdmin, None)

        await _push_history_data(hub, state, "mA", "fA", "full", [{"x": 1}])

        assert wsA.sent and wsA.sent[-1]["flow_id"] == "fA"
        assert wsAdmin.sent and wsAdmin.sent[-1]["flow_id"] == "fA"
        assert wsB.sent == []  # other owner gets nothing

    asyncio.run(scenario())


def test_handle_ui_connection_rejects_unauthenticated_fail_closed():
    async def scenario():
        state = ServerState()
        await state.register_machine("mA", owner_id="A")
        hub = UiHub()
        ws = _FakeUiWS()
        await handle_ui_connection(
            ws, hub, state, owner=None, require_owner=True
        )
        # Accepted then immediately closed; never registered, no snapshot sent.
        assert ws.accepted is True
        assert ws.closed is True
        assert ws.sent == []
        assert hub.client_count == 0

    asyncio.run(scenario())


def test_handle_ui_connection_owner_snapshot_is_scoped():
    async def scenario():
        state = ServerState()
        await state.register_machine("mA", owner_id="A")
        await state.register_machine("mB", owner_id="B")
        hub = UiHub()
        ws = _FakeUiWS()  # no extra frames -> disconnects right after snapshot
        await handle_ui_connection(ws, hub, state, owner="A")
        assert ws.sent and ws.sent[0]["type"] == "snapshot"
        assert {m["machine_id"] for m in ws.sent[0]["machines"]} == {"mA"}
        # Connection ended -> client unregistered.
        assert hub.client_count == 0

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# G7 tasks 2 & 3 — REST owner authorization filtering over a live TestClient
#
# Two *non-admin* owners A and B each have their own daemon (bound via a daemon
# key in HELLO). The REST surface must isolate them: A can see and control only
# A's daemon, B only B's, and an unauthenticated caller sees nothing (401).
# --------------------------------------------------------------------------

from _authsrv import login  # noqa: E402  (kept beside the tests that use it)


@pytest.fixture()
def authz_app():
    """A server app seeded with two non-admin owners (A, B), each with a key."""
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
        owners[name] = {"owner_id": oid, "key": key_plain}
    app.state.owners = owners
    return app


def _owner_hello(app, owner_name, machine_id):
    key = app.state.owners[owner_name]["key"]
    return protocol.make_hello(machine_id, "h", "6.4.0", key=key).to_json()


def _await_visible(client, machine_id, tries=100):
    for _ in range(tries):
        machines = client.get("/api/machines").json().get("machines", [])
        if any(m["machine_id"] == machine_id for m in machines):
            return
    raise AssertionError(f"machine {machine_id} never became visible")


def test_rest_reads_are_owner_isolated(authz_app):
    from fastapi.testclient import TestClient

    app = authz_app
    with TestClient(app) as ca, TestClient(app) as cb:
        login(ca, "A", "pw")
        login(cb, "B", "pw")
        with ca.websocket_connect("/ws") as da, cb.websocket_connect("/ws") as db:
            da.send_text(_owner_hello(app, "A", "mA"))
            protocol.decode(da.receive_text())  # WELCOME
            db.send_text(_owner_hello(app, "B", "mB"))
            protocol.decode(db.receive_text())  # WELCOME
            da.send_text(
                protocol.make_status_update(
                    {"machine_id": "mA", "flows": [{"flow_id": "fA", "status": "running"}]}
                ).to_json()
            )
            db.send_text(
                protocol.make_status_update(
                    {"machine_id": "mB", "flows": [{"flow_id": "fB", "status": "running"}]}
                ).to_json()
            )
            _await_visible(ca, "mA")
            _await_visible(cb, "mB")

            # /api/machines is scoped to each owner.
            a_machines = {m["machine_id"] for m in ca.get("/api/machines").json()["machines"]}
            b_machines = {m["machine_id"] for m in cb.get("/api/machines").json()["machines"]}
            assert a_machines == {"mA"}
            assert b_machines == {"mB"}

            # Cross-owner reads are 404 (no existence leak), own reads are 200.
            assert ca.get("/api/flows/fA").status_code == 200
            assert ca.get("/api/flows/fB").status_code == 404
            assert cb.get("/api/flows/fA").status_code == 404
            assert ca.get("/api/machines/mB/flows").status_code == 404
            assert ca.get("/api/machines/mA/flows").status_code == 200
            # Cross-owner history detail is 404 (resolved via the live flow set).
            assert ca.get("/api/history/fB").status_code == 404


def test_rest_unauthenticated_reads_are_401(authz_app):
    from fastapi.testclient import TestClient

    with TestClient(authz_app) as anon:
        assert anon.get("/api/machines").status_code == 401
        assert anon.get("/api/machines/mA/flows").status_code == 401
        assert anon.get("/api/flows/fA").status_code == 401
        assert anon.get("/api/history").status_code == 401
        assert anon.get("/api/history/fA").status_code == 401


def test_rest_writes_are_owner_isolated(authz_app):
    from fastapi.testclient import TestClient

    app = authz_app
    with TestClient(app) as ca, TestClient(app) as cb:
        login(ca, "A", "pw")
        login(cb, "B", "pw")
        with ca.websocket_connect("/ws") as da, cb.websocket_connect("/ws") as db:
            da.send_text(_owner_hello(app, "A", "mA"))
            protocol.decode(da.receive_text())
            db.send_text(_owner_hello(app, "B", "mB"))
            protocol.decode(db.receive_text())
            da.send_text(
                protocol.make_status_update(
                    {
                        "machine_id": "mA",
                        "flows": [
                            {
                                "flow_id": "fA",
                                "project_root": "/pa",
                                "status": "running",
                                "pending_calls": [{"call_id": "cA"}],
                            }
                        ],
                    }
                ).to_json()
            )
            db.send_text(
                protocol.make_status_update(
                    {
                        "machine_id": "mB",
                        "flows": [
                            {"flow_id": "fB", "project_root": "/pb", "status": "running"}
                        ],
                    }
                ).to_json()
            )
            _await_visible(ca, "mA")
            _await_visible(cb, "mB")

            # A may dispatch to its OWN daemon.
            ok = ca.post(
                "/api/flows",
                json={"machine_id": "mA", "task": "do", "project_root": "/pa"},
            )
            assert ok.status_code == 202
            spawn = protocol.decode(da.receive_text())
            assert spawn.type == protocol.MSG_SPAWN_FLOW

            # A may NOT dispatch to B's daemon — cross-owner reads as absent (404).
            cross = ca.post(
                "/api/flows",
                json={"machine_id": "mB", "task": "pwn", "project_root": "/pb"},
            )
            assert cross.status_code == 404

            # The absolute-path constraint is preserved (422 before ownership).
            rel = ca.post(
                "/api/flows",
                json={"machine_id": "mA", "task": "do", "project_root": "relative"},
            )
            assert rel.status_code == 422

            # respond / interject are owner-gated: B's flow reads as absent to A.
            assert (
                ca.post("/api/flows/fB/respond", json={"response": "x"}).status_code
                == 404
            )
            assert (
                ca.post("/api/flows/fB/interject", json={"text": "x"}).status_code
                == 404
            )
            # A may respond to its own flow's pending call.
            own = ca.post("/api/flows/fA/respond", json={"response": "yes"})
            assert own.status_code == 200
            respond = protocol.decode(da.receive_text())
            assert respond.type == protocol.MSG_RESPOND_CALL
            assert respond.payload["call_id"] == "cA"


def test_rest_unauthenticated_writes_are_401(authz_app):
    from fastapi.testclient import TestClient

    with TestClient(authz_app) as anon:
        assert (
            anon.post(
                "/api/flows",
                json={"machine_id": "mA", "task": "x", "project_root": "/p"},
            ).status_code
            == 401
        )
        assert (
            anon.post("/api/flows/fA/respond", json={"response": "x"}).status_code == 401
        )
        assert (
            anon.post("/api/flows/fA/interject", json={"text": "x"}).status_code == 401
        )
