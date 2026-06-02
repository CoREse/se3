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
