"""Tests for the server-side browser-presence publication (G4).

Protocol revision 4 lets the server tell every daemon whether any browser is
watching, so an unwatched daemon can drop into a low-power cadence:

* **edge** — :class:`UiHub` fires the injected ``on_presence_edge`` callback
  only when the UI client count crosses the 0↔non-0 boundary, and
  :meth:`ConnectionManager.broadcast_viewers` fans the resulting
  ``MSG_VIEWERS`` frame to every connected daemon;
* **level** — the per-connection heartbeat PING piggybacks the live
  ``viewers`` count so a lost edge self-heals within one PING interval, and a
  freshly-handshaken v4 daemon receives one immediate level so a reconnect
  window cannot leave it in the wrong gear.

Everything runs against lightweight fake WebSockets — no live network.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tianluo.daemon import protocol
from tianluo.server import ws as ws_module
from tianluo.server.state import ServerState
from tianluo.server.ws import (
    ConnectionManager,
    UiHub,
    _serve_loop,
    handle_daemon_connection,
)
from tianluo.daemon.wire_metrics import WireMetrics


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class _Disconnect(Exception):
    """Signals the fake socket has no more frames (a client disconnect)."""


class _FakeUiWS:
    """A web-frontend socket stand-in: captures sent JSON payloads."""

    def __init__(self, broken: bool = False):
        self.sent = []
        self.broken = broken

    async def send_text(self, data):
        if self.broken:
            raise RuntimeError("socket dead")
        self.sent.append(json.loads(data))


class _FakeDaemonWS:
    """A server-side daemon socket stand-in driven by a queued frame list."""

    def __init__(self, frames=None, broken: bool = False):
        self._incoming = list(frames or [])
        self.sent = []
        self.accepted = False
        self.closed = False
        self.broken = broken

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if self._incoming:
            return self._incoming.pop(0)
        raise _Disconnect()

    async def send_text(self, data):
        if self.broken:
            raise RuntimeError("socket dead")
        self.sent.append(protocol.decode(data))

    async def close(self, code=1000):
        self.closed = True

    def frames_of_type(self, msg_type):
        return [m for m in self.sent if m.type == msg_type]


class _BlockingDaemonWS(_FakeDaemonWS):
    """A daemon socket whose receive blocks forever, for heartbeat tests.

    ``fail_next_send`` flips send_text into raising, which ends the heartbeat
    loop and lets ``_serve_loop`` return cleanly.
    """

    def __init__(self):
        super().__init__()
        self.fail_next_send = False

    async def receive_text(self):
        await asyncio.Event().wait()  # cancelled by _serve_loop teardown

    async def send_text(self, data):
        if self.fail_next_send:
            raise RuntimeError("socket closed")
        self.sent.append(protocol.decode(data))


class _EdgeRecorder:
    """Captures presence-edge callback invocations."""

    def __init__(self, raise_on_call: bool = False):
        self.calls = []
        self.raise_on_call = raise_on_call

    async def __call__(self, count: int) -> None:
        self.calls.append(count)
        if self.raise_on_call:
            raise RuntimeError("edge callback boom")


# --------------------------------------------------------------------------
# Task 6 — UiHub 0↔non-0 edge detection
# --------------------------------------------------------------------------


def test_first_connect_fires_edge_second_does_not():
    async def scenario():
        edge = _EdgeRecorder()
        hub = UiHub(on_presence_edge=edge)
        await hub.register(_FakeUiWS(), "A")
        assert edge.calls == [1]
        await hub.register(_FakeUiWS(), "B")
        assert edge.calls == [1]  # 1→2 is silent

    asyncio.run(scenario())


def test_last_disconnect_fires_edge_penultimate_does_not():
    async def scenario():
        edge = _EdgeRecorder()
        hub = UiHub(on_presence_edge=edge)
        ws1, ws2 = _FakeUiWS(), _FakeUiWS()
        await hub.register(ws1)
        await hub.register(ws2)
        edge.calls.clear()

        await hub.unregister(ws1)
        assert edge.calls == []  # 2→1 is silent
        await hub.unregister(ws2)
        assert edge.calls == [0]

    asyncio.run(scenario())


def test_unregister_unknown_socket_fires_no_edge():
    async def scenario():
        edge = _EdgeRecorder()
        hub = UiHub(on_presence_edge=edge)
        # Empty hub: removing a never-registered socket is not a transition.
        await hub.unregister(_FakeUiWS())
        assert edge.calls == []

        await hub.register(_FakeUiWS())
        edge.calls.clear()
        await hub.unregister(_FakeUiWS())  # unknown while one client remains
        assert edge.calls == []
        assert hub.client_count == 1

    asyncio.run(scenario())


def test_edge_callback_exception_does_not_break_registration():
    async def scenario():
        edge = _EdgeRecorder(raise_on_call=True)
        hub = UiHub(on_presence_edge=edge)
        ws = _FakeUiWS()
        await hub.register(ws)  # must not raise
        assert hub.client_count == 1
        assert edge.calls == [1]
        await hub.unregister(ws)  # must not raise either
        assert hub.client_count == 0
        assert edge.calls == [1, 0]

    asyncio.run(scenario())


def test_no_callback_injected_behaves_as_before():
    async def scenario():
        hub = UiHub()
        ws = _FakeUiWS()
        await hub.register(ws, "A")
        assert hub.client_count == 1
        assert hub.distinct_owners() == {"A"}
        await hub.unregister(ws)
        assert hub.client_count == 0

    asyncio.run(scenario())


def test_fan_out_prune_of_last_client_fires_zero_edge():
    """A client dropped by the fan-out prune is gone before its handler's
    unregister runs — the non0→0 edge must fire at the prune itself."""

    async def scenario():
        edge = _EdgeRecorder()
        hub = UiHub(on_presence_edge=edge)
        dead = _FakeUiWS(broken=True)
        await hub.register(dead)
        edge.calls.clear()

        await hub.broadcast({"type": "x"})
        assert hub.client_count == 0
        assert edge.calls == [0]

        # The handler's finally-unregister arrives late and must not re-fire.
        await hub.unregister(dead)
        assert edge.calls == [0]

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Task 6 — ConnectionManager.broadcast_viewers
# --------------------------------------------------------------------------


def test_broadcast_viewers_sends_to_all_daemons_and_records_metrics():
    async def scenario():
        metrics = WireMetrics()
        manager = ConnectionManager(metrics=metrics)
        d1, d2 = _FakeDaemonWS(), _FakeDaemonWS()
        await manager.connect("m1", d1)
        await manager.connect("m2", d2)

        await manager.broadcast_viewers(1)

        for sock in (d1, d2):
            frames = sock.frames_of_type(protocol.MSG_VIEWERS)
            assert len(frames) == 1
            assert frames[0].payload == {"count": 1}
        snap = metrics.snapshot()
        assert protocol.MSG_VIEWERS in snap
        assert snap[protocol.MSG_VIEWERS]["count"] == 2
        assert snap[protocol.MSG_VIEWERS]["bytes"] > 0

    asyncio.run(scenario())


def test_broadcast_viewers_single_send_failure_does_not_block_others():
    async def scenario():
        manager = ConnectionManager()
        broken, healthy = _FakeDaemonWS(broken=True), _FakeDaemonWS()
        await manager.connect("m-broken", broken)
        await manager.connect("m-ok", healthy)

        await manager.broadcast_viewers(0)  # must not raise

        frames = healthy.frames_of_type(protocol.MSG_VIEWERS)
        assert len(frames) == 1
        assert frames[0].payload == {"count": 0}

    asyncio.run(scenario())


def test_broadcast_viewers_with_no_daemons_is_a_noop():
    async def scenario():
        manager = ConnectionManager()
        await manager.broadcast_viewers(1)  # must not raise

    asyncio.run(scenario())


def test_app_assembly_wires_edge_to_daemon_broadcast():
    """create_app injects manager.broadcast_viewers as the hub's edge callback."""
    pytest.importorskip("fastapi")
    from _authsrv import authed_app

    async def scenario():
        app, _key = authed_app()
        manager = app.state.connection_manager
        hub = app.state.ui_hub

        daemon_ws = _FakeDaemonWS()
        await manager.connect("m1", daemon_ws)

        ui_ws = _FakeUiWS()
        await hub.register(ui_ws)
        frames = daemon_ws.frames_of_type(protocol.MSG_VIEWERS)
        assert len(frames) == 1 and frames[0].payload == {"count": 1}

        await hub.unregister(ui_ws)
        frames = daemon_ws.frames_of_type(protocol.MSG_VIEWERS)
        assert len(frames) == 2 and frames[1].payload == {"count": 0}

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Task 7 — heartbeat PING carries the live viewers level
# --------------------------------------------------------------------------


async def _wait_for(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.005)


def test_heartbeat_ping_carries_live_viewers_count(monkeypatch):
    monkeypatch.setattr(ws_module, "PING_INTERVAL", 0.01)

    async def scenario():
        state = ServerState()
        manager = ConnectionManager()
        hub = UiHub()
        await hub.register(_FakeUiWS(), "A")

        sock = _BlockingDaemonWS()
        task = asyncio.create_task(
            _serve_loop(sock, manager, state, "m1", hub)
        )
        try:
            await _wait_for(lambda: sock.frames_of_type(protocol.MSG_PING))
            first = sock.frames_of_type(protocol.MSG_PING)[0]
            assert first.payload.get("viewers") == 1

            # The level is read at send time, so a mid-connection change in
            # client_count shows up in the very next heartbeat.
            await hub.register(_FakeUiWS(), "B")
            await _wait_for(
                lambda: any(
                    m.payload.get("viewers") == 2
                    for m in sock.frames_of_type(protocol.MSG_PING)
                )
            )
        finally:
            sock.fail_next_send = True
            await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(scenario())


def test_heartbeat_ping_without_hub_stays_revision3_compatible(monkeypatch):
    monkeypatch.setattr(ws_module, "PING_INTERVAL", 0.01)

    async def scenario():
        state = ServerState()
        manager = ConnectionManager()
        sock = _BlockingDaemonWS()
        task = asyncio.create_task(
            _serve_loop(sock, manager, state, "m1", None)
        )
        try:
            await _wait_for(lambda: sock.frames_of_type(protocol.MSG_PING))
            ping = sock.frames_of_type(protocol.MSG_PING)[0]
            assert "viewers" not in ping.payload
        finally:
            sock.fail_next_send = True
            await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Task 7 — immediate viewers level after the handshake
# --------------------------------------------------------------------------


def test_handshake_sends_immediate_viewers_level():
    async def scenario():
        state = ServerState()
        manager = ConnectionManager()
        hub = UiHub()
        await hub.register(_FakeUiWS(), "A")
        await hub.register(_FakeUiWS(), "B")

        sock = _FakeDaemonWS([protocol.make_hello("m1", "host", "6.4.0").to_json()])
        await handle_daemon_connection(sock, manager, state, hub)

        welcomes = sock.frames_of_type(protocol.MSG_WELCOME)
        assert welcomes and welcomes[0].payload["accepted"] is True
        frames = sock.frames_of_type(protocol.MSG_VIEWERS)
        assert len(frames) == 1
        assert frames[0].payload == {"count": 2}
        # The level follows the WELCOME (the daemon must be handshaken first).
        assert sock.sent.index(welcomes[0]) < sock.sent.index(frames[0])

    asyncio.run(scenario())


def test_handshake_skips_viewers_for_pre_v4_daemon():
    """A revision-3 daemon would reject MSG_VIEWERS as an unknown type, so the
    handshake level is gated on the peer's advertised protocol_version."""

    async def scenario():
        state = ServerState()
        manager = ConnectionManager()
        hub = UiHub()
        await hub.register(_FakeUiWS(), "A")

        hello_v3 = protocol.Message(
            type=protocol.MSG_HELLO,
            payload={
                "machine_id": "m-old",
                "hostname": "host",
                "se3_version": "6.0.0",
                "protocol_version": "3",
            },
        ).to_json()
        sock = _FakeDaemonWS([hello_v3])
        await handle_daemon_connection(sock, manager, state, hub)

        welcomes = sock.frames_of_type(protocol.MSG_WELCOME)
        assert welcomes and welcomes[0].payload["accepted"] is True
        assert sock.frames_of_type(protocol.MSG_VIEWERS) == []

    asyncio.run(scenario())


def test_handshake_without_hub_sends_no_viewers():
    async def scenario():
        state = ServerState()
        manager = ConnectionManager()
        sock = _FakeDaemonWS([protocol.make_hello("m1", "host", "6.4.0").to_json()])
        await handle_daemon_connection(sock, manager, state, None)
        assert sock.frames_of_type(protocol.MSG_VIEWERS) == []

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# Task 7 — a viewers-carrying PING still gets a PONG (v3-daemon contract)
# --------------------------------------------------------------------------


def test_daemon_ping_with_viewers_still_replies_pong():
    """The daemon replies PONG to a PING regardless of extra payload fields —
    the contract that makes the level field safe to send to every daemon."""
    from tianluo.daemon.client import DaemonClient

    class _FakeClientWS:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(protocol.decode(data))

    client = DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="6.4.0",
        snapshot_provider=lambda: {"machine_id": "m1"},
    )
    ws = _FakeClientWS()

    async def scenario():
        await client._dispatch(ws, protocol.make_ping(seq=7, viewers=3))

    asyncio.run(scenario())
    assert len(ws.sent) == 1
    assert ws.sent[0].type == protocol.MSG_PONG
    assert ws.sent[0].seq == 7


# --------------------------------------------------------------------------
# task 13 (b)/(c) — full-flow integration: a real UiHub + ConnectionManager on
# the server side wired, in memory, to a real DaemonClient push loop on the
# daemon side. No network, no threads — the bridge socket dispatches every
# server→daemon frame straight into the client.
# --------------------------------------------------------------------------

from tianluo.daemon import client as client_module
from tianluo.daemon.client import DaemonClient


class _ClientOutWS:
    """The daemon client's outbound socket: captures daemon→server frames."""

    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(protocol.decode(data))

    def types(self):
        return [m.type for m in self.sent]


class _PresenceBridgeWS:
    """Server-side daemon socket piping frames into a live DaemonClient.

    ``send_text`` (the server's downlink) decodes and dispatches directly into
    the client — the in-memory stand-in for the WebSocket hop — so presence
    edges, WELCOMEs and heartbeats reach the client exactly as encoded on the
    wire. ``receive_text`` blocks forever (the daemon's uplink is captured on
    :class:`_ClientOutWS` instead); ``fail_next_send`` ends a server heartbeat
    loop for teardown, mirroring :class:`_BlockingDaemonWS`.
    """

    def __init__(self, client, client_out):
        self._client = client
        self._out = client_out
        self.fail_next_send = False

    async def accept(self):
        pass

    async def receive_text(self):
        await asyncio.Event().wait()  # cancelled at teardown

    async def send_text(self, data):
        if self.fail_next_send:
            raise RuntimeError("socket closed")
        await self._client._dispatch(self._out, protocol.decode(data))

    async def close(self, code=1000):
        pass


class _IndexedHistory:
    """Minimal history provider whose index push is observable."""

    def build_index(self):
        return []

    def read_active_flows(self, cursors):
        return []

    def active_flow_signature(self):
        return {}

    def invalidate_index_cache(self):
        pass

    def live_flow_ids(self):
        return set()


def _make_client(*, fast: float = 0.1, status: float = 0.5) -> DaemonClient:
    return DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="11.0.0",
        snapshot_provider=lambda: {
            "machine_id": "m1",
            "flows": [],
            "issues": [],
            "pending_calls": [],
            "project_roots": [],
        },
        history_provider=_IndexedHistory(),
        status_interval=status,
        history_poll_interval=fast,
    )


def test_presence_full_flow_open_upshift_close_downshift(monkeypatch):
    """UI connect → edge → daemon upshifts with a forced full first screen;
    UI disconnect → edge → daemon drops back into the idle gear."""
    monkeypatch.setattr(client_module, "_IDLE_FAST_INTERVAL", 5.0)
    monkeypatch.setattr(client_module, "_IDLE_STATUS_INTERVAL", 10.0)

    async def scenario():
        manager = ConnectionManager()
        # The exact assembly app.py wires: the hub's 0↔non-0 edge fans a
        # MSG_VIEWERS to every connected daemon.
        hub = UiHub(on_presence_edge=manager.broadcast_viewers)

        # (The client clamps to its 0.1 s fast / 0.5 s status floors.)
        client = _make_client(fast=0.1, status=0.5)
        out = _ClientOutWS()
        client._fast_push_event = asyncio.Event()
        bridge = _PresenceBridgeWS(client, out)
        await manager.connect("m1", bridge)

        # The server-side handshake tail for a v4 daemon: WELCOME (which is
        # how the client learns the peer speaks revision 4) followed by the
        # immediate viewers level — here zero, nobody watching yet.
        await manager.send_to("m1", protocol.make_welcome("test-server"))
        await manager.send_to("m1", protocol.make_viewers(hub.client_count))
        assert client.viewer_count == 0

        # Prime the status baseline so a later unforced push would collapse to
        # a keepalive — proving the presence wake genuinely forces a full one.
        await client._push_status(out)
        pushed_baseline = len(out.sent)

        stop = asyncio.Event()
        push_task = asyncio.create_task(client._push_loop(out, stop))
        try:
            # Idle gear: the loop's first tick is 5 s away — silence.
            await asyncio.sleep(0.3)
            assert len(out.sent) == pushed_baseline

            # Browser opens: hub edge → broadcast → upshift + forced refresh.
            ui = _FakeUiWS()
            await hub.register(ui, "A")
            assert client.viewer_count == 1
            assert client._effective_intervals() == (0.1, 0.5)
            await _wait_for(
                lambda: protocol.MSG_HISTORY_INDEX in out.types()[pushed_baseline:]
                and protocol.MSG_STATUS_UPDATE in out.types()[pushed_baseline:]
            )
            woke = out.types()[pushed_baseline:]
            # The first screen must be real content, never a keepalive.
            assert protocol.MSG_KEEPALIVE not in woke

            # Browser closes: hub edge → broadcast → back to the idle gear.
            await hub.unregister(ui)
            assert client.viewer_count == 0
            assert client._effective_intervals() == (5.0, 10.0)

            # Let any in-flight full-speed tick drain, then require silence.
            await asyncio.sleep(0.15)
            settled = len(out.sent)
            await asyncio.sleep(0.4)
            assert len(out.sent) == settled
        finally:
            stop.set()
            await asyncio.wait_for(push_task, timeout=5)

    asyncio.run(scenario())


def test_ping_level_self_heals_lost_edges_in_both_directions(monkeypatch):
    """A lost MSG_VIEWERS edge is repaired by the heartbeat level within one
    PING interval — in both the 0→1 and the 1→0 direction."""
    monkeypatch.setattr(ws_module, "PING_INTERVAL", 0.02)

    async def scenario():
        state = ServerState()
        manager = ConnectionManager()
        # No edge callback wired: every edge is "lost" — only the PING level
        # can carry presence, which is exactly the self-heal path under test.
        hub = UiHub()

        client = _make_client()
        out = _ClientOutWS()
        client._fast_push_event = asyncio.Event()
        client._peer_protocol_version = "4"
        client._viewer_count = 0  # stale belief: idle
        bridge = _PresenceBridgeWS(client, out)

        ui = _FakeUiWS()
        await hub.register(ui)  # someone IS watching; the edge never arrived

        beat_task = asyncio.create_task(
            _serve_loop(bridge, manager, state, "m1", hub)
        )
        try:
            await _wait_for(lambda: client.viewer_count == 1)
            # The healed 0→1 also arms the forced-refresh wake, so the
            # just-opened browser still gets a fresh first screen.
            assert client._presence_wake_force is True
            assert client._fast_push_event.is_set()

            # Now lose the closing edge too: the level must heal 1→0.
            await hub.unregister(ui)
            await _wait_for(lambda: client.viewer_count == 0)
            # Every heartbeat kept its PONG (liveness never traded away).
            assert protocol.MSG_PONG in out.types()
        finally:
            bridge.fail_next_send = True
            await asyncio.wait_for(beat_task, timeout=5)

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# task 13 (c) — compat matrix, server side: a revision-3 daemon on a v4 server
# keeps its revision-3 session shape and nothing anomalous kills it.
# --------------------------------------------------------------------------


class _SessionDaemonWS(_FakeDaemonWS):
    """A daemon socket that stays connected after draining its frames.

    ``drained`` fires once the queued frames (the HELLO) are consumed;
    ``release`` then lets ``receive_text`` raise the disconnect, ending the
    session — so a test can act mid-session, unlike :class:`_FakeDaemonWS`
    whose empty queue disconnects immediately.
    """

    def __init__(self, frames):
        super().__init__(frames)
        self.drained = asyncio.Event()
        self.release = asyncio.Event()

    async def receive_text(self):
        if self._incoming:
            return self._incoming.pop(0)
        self.drained.set()
        await self.release.wait()
        raise _Disconnect()


def test_v3_daemon_session_survives_ui_churn_on_a_v4_server(monkeypatch):
    """v3 daemon × v4 server: the handshake stays revision-3 (no viewers
    level), UI churn mid-session broadcasts MSG_VIEWERS without disturbing the
    session, and heartbeats keep flowing throughout."""
    monkeypatch.setattr(ws_module, "PING_INTERVAL", 0.02)

    async def scenario():
        state = ServerState()
        manager = ConnectionManager()
        hub = UiHub(on_presence_edge=manager.broadcast_viewers)

        hello_v3 = protocol.Message(
            type=protocol.MSG_HELLO,
            payload={
                "machine_id": "m-old",
                "hostname": "host",
                "se3_version": "6.0.0",
                "protocol_version": "3",
            },
        ).to_json()
        sock = _SessionDaemonWS([hello_v3])
        session = asyncio.create_task(
            handle_daemon_connection(sock, manager, state, hub)
        )
        try:
            await asyncio.wait_for(sock.drained.wait(), timeout=5)
            # Revision-3 handshake shape: WELCOME accepted, no viewers level.
            welcomes = sock.frames_of_type(protocol.MSG_WELCOME)
            assert welcomes and welcomes[0].payload["accepted"] is True
            assert sock.frames_of_type(protocol.MSG_VIEWERS) == []

            # UI churn mid-session. The broadcast is deliberately ungated (a
            # real v3 daemon's decode() rejects the unknown type and drops the
            # frame — the documented fail-open no-op), so the frames DO go out
            # and the session must shrug them off.
            ui = _FakeUiWS()
            await hub.register(ui)
            await hub.unregister(ui)
            viewers = sock.frames_of_type(protocol.MSG_VIEWERS)
            assert [m.payload["count"] for m in viewers] == [1, 0]
            assert not session.done()  # nothing tore the session down

            # Heartbeats keep flowing after the churn.
            pings_before = len(sock.frames_of_type(protocol.MSG_PING))
            await _wait_for(
                lambda: len(sock.frames_of_type(protocol.MSG_PING))
                > pings_before
            )
        finally:
            sock.release.set()
            await asyncio.wait_for(session, timeout=5)

        # The daemon went offline cleanly — no unhandled exception escaped.
        assert session.exception() is None

    asyncio.run(scenario())
