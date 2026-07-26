"""Tests for the daemon-channel identity resolution and owner-scoped views.

Covers group G6:

* ``MachineRecord.owner_id`` and the owner-filtered ``ServerState`` queries
  (one owner never sees another owner's machines / flows / history);
* ``handle_daemon_connection`` HELLO key → owner resolution and the fail-closed
  reject of a missing / invalid key;
* the owner-scoped ``/ws/ui`` push paths (``UiHub`` filtering, the ``_push_*``
  helpers, and the ``handle_ui_connection`` unauthenticated reject).

These exercise ``tianluo.server`` directly with lightweight fake WebSockets, so no
FastAPI test client (and no live event loop wiring) is needed.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from tianluo.daemon import protocol
from tianluo.server.crypto import generate_token
from tianluo.server.identity import IdentityService
from tianluo.server.persistence import Store
from tianluo.server.state import MachineRecord, ServerState
from tianluo.server.ws import (
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


def test_owner_takeover_on_machine_id_collision_discards_prior_state():
    """A forged HELLO reusing a victim's machine_id must not inherit its state.

    machine_id is derived from hostname + NIC MAC and supplied verbatim by the
    daemon, so any valid-key holder can connect under another owner's
    machine_id. When the resolved owner changes, register_machine must scrub the
    prior owner's flows and cached history before rebinding, so the new owner
    can never read the previous owner's trust-domain data.
    """

    async def scenario():
        state = ServerState()
        # Owner B's daemon connects and aggregates flows + history under m1.
        await state.register_machine("host-mac", "host", "6.4.0", owner_id="B")
        await state.update_status(
            "host-mac",
            {"flows": [{"flow_id": "fB", "status": "running",
                        "task_description": "secret B task"}]},
        )
        await state.update_history_index(
            "host-mac", [{"flow_id": "fB", "updated_at": "1"}]
        )
        await state.append_history(
            "fB", "full", [{"step_id": "s", "message": {}}], machine_id="host-mac"
        )

        # Owner A connects with the SAME machine_id and A's own valid key.
        await state.register_machine("host-mac", "host", "6.4.0", owner_id="A")

        # A must see no flows and no history from B's domain.
        assert await state.get_machine_flows("host-mac", owner="A") == []
        assert await state.get_history_index(owner="A") == []
        # The cached bundle for B's flow is gone too (no cross-owner pull).
        assert await state.get_history("fB") is None
        assert await state.find_machine_for_history_flow("fB", owner="A") is None

    asyncio.run(scenario())


def test_owner_reconnect_same_owner_retains_flows():
    """The scrub only fires on an owner *change* — a same-owner reconnect keeps
    its aggregated flows/history until the next STATUS_UPDATE."""

    async def scenario():
        state = ServerState()
        await state.register_machine("m1", "host", "6.4.0", owner_id="A")
        await state.update_status(
            "m1", {"flows": [{"flow_id": "fA", "status": "running"}]}
        )
        await state.update_history_index("m1", [{"flow_id": "fA", "updated_at": "1"}])
        # Same owner reconnects — flows and history survive.
        await state.register_machine("m1", "host", "6.4.0", owner_id="A")
        flows = await state.get_machine_flows("m1", owner="A")
        assert [f["flow_id"] for f in flows] == ["fA"]
        assert [e["flow_id"] for e in await state.get_history_index(owner="A")] == [
            "fA"
        ]

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
    import tianluo.server.crypto as crypto
    from tianluo.server.app import create_app
    from tianluo.server.auth.session import CookieConfig, SessionStore

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


def _next_daemon_frame(sock):
    """Read the next substantive server→daemon frame, skipping presence frames.

    Since protocol revision 4 the server sends ``MSG_VIEWERS`` levels/edges
    (right after the handshake and on UI-client 0↔non-0 transitions); tests
    that assert on a specific dispatched frame must not trip over them.
    """
    while True:
        msg = protocol.decode(sock.receive_text())
        if msg.type != protocol.MSG_VIEWERS:
            return msg


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
            spawn = _next_daemon_frame(da)
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
            respond = _next_daemon_frame(da)
            assert respond.type == protocol.MSG_RESPOND_CALL
            assert respond.payload["call_id"] == "cA"


def test_rest_resume_is_owner_isolated(authz_app):
    """POST /api/flows/{id}/resume is owner-gated: cross-owner returns 404."""
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
            # A has a paused flow, B has a paused flow.
            da.send_text(
                protocol.make_status_update(
                    {
                        "machine_id": "mA",
                        "flows": [
                            {
                                "flow_id": "fA",
                                "project_root": "/pa",
                                "status": "paused",
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
                            {
                                "flow_id": "fB",
                                "project_root": "/pb",
                                "status": "paused",
                            }
                        ],
                    }
                ).to_json()
            )
            _await_visible(ca, "mA")
            _await_visible(cb, "mB")

            # A can resume its own flow.
            ok = ca.post("/api/flows/fA/resume")
            assert ok.status_code == 202
            spawn = _next_daemon_frame(da)
            assert spawn.type == protocol.MSG_SPAWN_FLOW
            assert spawn.payload["resume_flow_id"] == "fA"

            # A cannot resume B's flow — cross-owner reads as absent (404).
            cross = ca.post("/api/flows/fB/resume")
            assert cross.status_code == 404


def test_rest_publish_from_issue_is_owner_isolated(authz_app):
    """POST /api/flows with from_issue_id is owner-gated: B's issue is 404 to A."""
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
                        "flows": [],
                        "issues": [
                            {"id": "001", "project_root": "/pa", "status": "open"}
                        ],
                    }
                ).to_json()
            )
            db.send_text(
                protocol.make_status_update(
                    {
                        "machine_id": "mB",
                        "flows": [],
                        "issues": [
                            {"id": "900", "project_root": "/pb", "status": "open"}
                        ],
                    }
                ).to_json()
            )
            _await_visible(ca, "mA")
            _await_visible(cb, "mB")

            # A may launch a flow from its OWN issue.
            ok = ca.post("/api/flows", json={"from_issue_id": "001"})
            assert ok.status_code == 202
            spawn = _next_daemon_frame(da)
            assert spawn.type == protocol.MSG_SPAWN_FLOW
            assert spawn.payload["from_issue_id"] == "001"

            # A may NOT launch from B's issue — cross-owner reads as absent (404).
            cross = ca.post("/api/flows", json={"from_issue_id": "900"})
            assert cross.status_code == 404


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
        assert anon.post("/api/flows/fA/resume").status_code == 401


# --------------------------------------------------------------------------
# G8 task 1 — owner-self-managed daemon keys (create / list / revoke)
# --------------------------------------------------------------------------


def test_daemon_key_create_returns_plaintext_once_then_list_hides_it(authz_app):
    from fastapi.testclient import TestClient

    with TestClient(authz_app) as ca:
        login(ca, "A", "pw")
        created = ca.post("/api/daemon-keys", json={"label": "laptop"})
        assert created.status_code == 201
        body = created.json()
        plaintext = body["key"]
        key_id = body["key_id"]
        assert plaintext and body["label"] == "laptop"

        listing = ca.get("/api/daemon-keys")
        assert listing.status_code == 200
        keys = listing.json()["keys"]
        entry = next(k for k in keys if k["key_id"] == key_id)
        # Metadata only — the plaintext (and the stored hash) are never echoed.
        assert "key" not in entry
        assert "key_hash" not in entry
        assert entry["label"] == "laptop"
        assert entry["revoked"] is False
        # The one-time plaintext appears in no field of any list entry.
        assert all(plaintext not in str(v) for k in keys for v in k.values())


def test_daemon_keys_are_owner_isolated(authz_app):
    from fastapi.testclient import TestClient

    app = authz_app
    with TestClient(app) as ca, TestClient(app) as cb:
        login(ca, "A", "pw")
        login(cb, "B", "pw")
        a_key_id = ca.post("/api/daemon-keys", json={"label": "a-key"}).json()["key_id"]
        b_key_id = cb.post("/api/daemon-keys", json={"label": "b-key"}).json()["key_id"]

        # B never sees A's key in its own listing.
        b_ids = {k["key_id"] for k in cb.get("/api/daemon-keys").json()["keys"]}
        assert a_key_id not in b_ids
        assert b_key_id in b_ids

        # B cannot revoke A's key — it reads as absent (404, no existence leak).
        assert ca.delete(f"/api/daemon-keys/{a_key_id}").status_code == 200
        # (Re-login a fresh A client to confirm revoke landed.)
        revoked = next(
            k for k in ca.get("/api/daemon-keys").json()["keys"] if k["key_id"] == a_key_id
        )
        assert revoked["revoked"] is True
        assert cb.delete(f"/api/daemon-keys/{a_key_id}").status_code == 404


def test_daemon_key_endpoints_require_auth(authz_app):
    from fastapi.testclient import TestClient

    with TestClient(authz_app) as anon:
        assert anon.post("/api/daemon-keys", json={"label": "x"}).status_code == 401
        assert anon.get("/api/daemon-keys").status_code == 401
        assert anon.delete("/api/daemon-keys/whatever").status_code == 401


def test_revoked_daemon_key_blocks_daemon_hello(authz_app):
    """A key minted via the API authenticates a daemon HELLO until it is revoked."""
    from fastapi.testclient import TestClient

    app = authz_app
    owner_a = app.state.owners["A"]["owner_id"]
    with TestClient(app) as ca:
        login(ca, "A", "pw")
        created = ca.post("/api/daemon-keys", json={"label": "node"}).json()
        key, key_id = created["key"], created["key_id"]

    identity = app.state.identity
    # Before revocation the key resolves to owner A (a daemon could connect).
    assert identity.resolve_owner_for_key(key) == owner_a

    with TestClient(app) as ca:
        login(ca, "A", "pw")
        assert ca.delete(f"/api/daemon-keys/{key_id}").status_code == 200

    # After revocation the key resolves to nothing, so the daemon HELLO is
    # rejected fail-closed (WELCOME accepted=false + close), exactly as a
    # bogus key is.
    assert identity.resolve_owner_for_key(key) is None

    async def hello_is_rejected():
        state = app.state.server_state
        manager = ConnectionManager()
        ws = _FakeDaemonWS(
            [protocol.make_hello("mRevoked", "h", "6.4.0", key=key).to_json()]
        )
        await handle_daemon_connection(ws, manager, state, identity=identity)
        welcomes = ws.welcomes()
        assert welcomes and welcomes[0].payload["accepted"] is False
        assert ws.closed is True
        assert await state.get_machine("mRevoked") is None

    asyncio.run(hello_is_rejected())


# --------------------------------------------------------------------------
# Issue REST endpoints — owner isolation
# --------------------------------------------------------------------------


def test_issue_endpoints_are_owner_isolated(authz_app):
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
                protocol.make_status_update({
                    "machine_id": "mA",
                    "flows": [],
                    "issues": [
                        {"id": "001", "project_root": "/pa", "status": "open", "source": "human"},
                    ],
                }).to_json()
            )
            db.send_text(
                protocol.make_status_update({
                    "machine_id": "mB",
                    "flows": [],
                    "issues": [
                        {"id": "002", "project_root": "/pb", "status": "open", "source": "system"},
                    ],
                }).to_json()
            )
            _await_visible(ca, "mA")
            _await_visible(cb, "mB")

            # A sees only its own issues
            a_issues = ca.get("/api/issues").json()["issues"]
            assert len(a_issues) == 1 and a_issues[0]["id"] == "001"

            # B sees only its own issues
            b_issues = cb.get("/api/issues").json()["issues"]
            assert len(b_issues) == 1 and b_issues[0]["id"] == "002"

            # Cross-owner issue read is 404
            assert ca.get("/api/issues/002").status_code == 404
            assert cb.get("/api/issues/001").status_code == 404

            # A can create on its own machine
            create_result: dict = {}

            def do_create():
                create_result["resp"] = ca.post("/api/issues", json={
                    "machine_id": "mA",
                    "project_root": "/pa",
                    "description": "New issue",
                })

            worker = threading.Thread(target=do_create)
            worker.start()
            try:
                msg = _next_daemon_frame(da)
                assert msg.type == protocol.MSG_ISSUE_COMMAND
                da.send_text(protocol.make_issue_result(
                    msg.payload.get("request_id", ""),
                    ok=True,
                    issue_id="003",
                ).to_json())
            finally:
                worker.join(timeout=5)
            resp = create_result["resp"]
            assert resp.status_code == 201

            # A cannot create on B's machine (404)
            cross = ca.post("/api/issues", json={
                "machine_id": "mB",
                "project_root": "/pb",
                "description": "Sneaky",
            })
            assert cross.status_code == 404


# --------------------------------------------------------------------------
# Project-registry REST endpoints — owner isolation
# --------------------------------------------------------------------------


def test_project_endpoints_are_owner_isolated(authz_app):
    """One owner can neither read nor mutate another owner's project registry.

    The registry names real filesystem paths on the daemon host, so a
    cross-owner read is a disclosure and a cross-owner write is remote control
    of somebody else's machine — both must be indistinguishable from "no such
    machine", never a 403 that confirms the id exists.
    """
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
                protocol.make_status_update({
                    "machine_id": "mA",
                    "flows": [],
                    "registered_projects": [
                        {"path": "/pa", "exists": True, "active": True},
                    ],
                }).to_json()
            )
            db.send_text(
                protocol.make_status_update({
                    "machine_id": "mB",
                    "flows": [],
                    "registered_projects": [
                        {"path": "/pb", "exists": False, "active": False},
                    ],
                }).to_json()
            )
            _await_visible(ca, "mA")
            _await_visible(cb, "mB")

            # Each owner reads only its own machine's registry.
            a_projects = ca.get("/api/machines/mA/projects").json()["projects"]
            assert [p["path"] for p in a_projects] == ["/pa"]
            b_projects = cb.get("/api/machines/mB/projects").json()["projects"]
            assert [p["path"] for p in b_projects] == ["/pb"]

            # Cross-owner read is 404, not 403 — no existence leak.
            assert ca.get("/api/machines/mB/projects").status_code == 404
            assert cb.get("/api/machines/mA/projects").status_code == 404

            # Cross-owner writes are 404 too, and must not reach the daemon.
            assert ca.post(
                "/api/machines/mB/projects", json={"project_root": "/pb"}
            ).status_code == 404
            assert ca.request(
                "DELETE",
                "/api/machines/mB/projects",
                params={"project_root": "/pb"},
            ).status_code == 404

            # A can still register on its OWN machine (proving the 404s above
            # are the ownership gate, not a blanket rejection).
            add_result: dict = {}

            def do_add():
                add_result["resp"] = ca.post(
                    "/api/machines/mA/projects", json={"project_root": "/pa/new"}
                )

            worker = threading.Thread(target=do_add)
            worker.start()
            try:
                msg = _next_daemon_frame(da)
                assert msg.type == protocol.MSG_PROJECT_COMMAND
                da.send_text(protocol.make_project_result(
                    msg.payload.get("request_id", ""),
                    ok=True,
                    project_root="/pa/new",
                ).to_json())
            finally:
                worker.join(timeout=5)
            assert add_result["resp"].status_code == 201


def test_project_endpoints_require_auth(authz_app):
    from fastapi.testclient import TestClient

    with TestClient(authz_app) as anon:
        assert anon.get("/api/machines/mA/projects").status_code == 401
        assert anon.post(
            "/api/machines/mA/projects", json={"project_root": "/p"}
        ).status_code == 401
        assert anon.request(
            "DELETE", "/api/machines/mA/projects", params={"project_root": "/p"}
        ).status_code == 401


def test_issue_endpoints_require_auth(authz_app):
    from fastapi.testclient import TestClient

    with TestClient(authz_app) as anon:
        assert anon.get("/api/issues").status_code == 401
        assert anon.get("/api/issues/001").status_code == 401
        assert anon.post(
            "/api/issues",
            json={"machine_id": "m", "project_root": "/p", "description": "d"},
        ).status_code == 401
        assert anon.patch("/api/issues/001", json={"title": "x"}).status_code == 401
        assert anon.post("/api/issues/001/close", json={}).status_code == 401
        assert anon.post("/api/issues/001/reopen", json={}).status_code == 401
