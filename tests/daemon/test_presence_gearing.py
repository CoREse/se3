"""Tests for the daemon's presence gearing (revision-4 viewers signalling).

These lock the daemon-side half of the presence contract:

* :class:`~se3.daemon.client.DaemonClient` consumes both presence channels —
  the ``MSG_VIEWERS`` 0↔non-0 edge frames and the ``viewers`` level riding on
  every ``MSG_PING`` heartbeat — into one belief (:attr:`viewer_count`), and a
  0→non-0 edge immediately wakes the push loop with a forced full
  STATUS_UPDATE + full HISTORY_INDEX (the just-opened browser holds no state);
* with ``viewers == 0`` **and** a revision >= 4 server, the push loop drops to
  the idle-gear cadence; a legacy (< v4) server, an unknown count, or any
  malformed presence input keeps today's full-speed behavior (fail-open);
* server-command wakeups (``_trigger_fast_push``) stay instant even in the
  idle gear — the low-power cadence never delays a spawn/interject/issue push;
* :meth:`Daemon._poll_loop` mirrors the gear off the client's ``viewer_count``
  (no client / disconnected / missing attribute ⇒ watched ⇒ full speed) and
  the stop event still interrupts an idle-gear wait immediately.

The async cases drive their own event loop via ``asyncio.run``: pytest-asyncio
is not a test dependency of this project, so the suite must run on bare pytest.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List

from se3.daemon import client as client_module
from se3.daemon import daemon as daemon_module
from se3.daemon import protocol
from se3.daemon.aggregator import MachineStatus
from se3.daemon.client import DaemonClient
from se3.daemon.daemon import Daemon, DaemonConfig


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class _FakeWS:
    """Minimal WebSocket stand-in capturing the frames the client sends."""

    def __init__(self) -> None:
        self.sent: List[protocol.Message] = []

    async def send(self, data: str) -> None:
        self.sent.append(protocol.decode(data))


class _FakeHistory:
    """The minimal history-provider surface the push loop exercises."""

    def __init__(self) -> None:
        self.invalidations = 0
        self.signature_calls = 0

    def build_index(self) -> list:
        return []

    def read_active_flows(self, cursors: Dict[str, Any]) -> list:
        return []

    def active_flow_signature(self) -> Dict[str, Any]:
        self.signature_calls += 1
        return {}

    def invalidate_index_cache(self) -> None:
        self.invalidations += 1

    def live_flow_ids(self) -> set:
        return set()


class _CountingCalls:
    """A calls-signature provider counting how often the fast tick scans."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Dict[str, Any]:
        self.calls += 1
        return {}


def _client(
    *,
    peer_version: Any = "4",
    history_provider: Any = None,
    calls_provider: Any = None,
    fast: float = 0.1,
    status: float = 0.5,
) -> DaemonClient:
    client = DaemonClient(
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
        history_provider=history_provider,
        calls_signature_provider=calls_provider,
        status_interval=status,
        history_poll_interval=fast,
    )
    # The gear gates on the WELCOME-learned peer version; set it directly so
    # the tests can model a v4 / legacy peer without driving the handshake.
    client._peer_protocol_version = peer_version
    return client


def _viewers(count: Any) -> protocol.Message:
    return protocol.Message(type=protocol.MSG_VIEWERS, payload={"count": count})


def _types(ws: _FakeWS) -> List[str]:
    return [m.type for m in ws.sent]


async def _run_push_loop_for(client: DaemonClient, ws: _FakeWS, body) -> None:
    """Run the client push loop around *body* (an async callable)."""
    stop = asyncio.Event()
    client._fast_push_event = asyncio.Event()
    task = asyncio.create_task(client._push_loop(ws, stop))
    try:
        await body()
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)


async def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------
# task 8 — receiving side: edges, PING level, legacy gating, garbage tolerance
# --------------------------------------------------------------------------


def test_viewers_edge_updates_count_and_wakes_push():
    """MSG_VIEWERS{0} records idle; a 0→1 edge arms the forced full refresh."""
    client = _client()
    ws = _FakeWS()

    async def scenario():
        client._fast_push_event = asyncio.Event()
        await client._dispatch(ws, _viewers(0))
        assert client.viewer_count == 0
        assert client._presence_wake_force is False
        assert not client._fast_push_event.is_set()

        await client._dispatch(ws, _viewers(1))
        assert client.viewer_count == 1
        assert client._presence_wake_force is True
        assert client._fast_push_event.is_set()

    asyncio.run(scenario())
    # The VIEWERS frames themselves produce no outbound traffic.
    assert ws.sent == []


def test_first_nonzero_report_of_a_session_also_wakes():
    """None→non-0 (the session's first report) arms the wake, per the 0→1 race fix."""
    client = _client()
    ws = _FakeWS()

    async def scenario():
        client._fast_push_event = asyncio.Event()
        await client._dispatch(ws, _viewers(2))
        assert client.viewer_count == 2
        assert client._presence_wake_force is True
        # 1→2 / 2→1 are not edges: no further wake once watched.
        client._presence_wake_force = False
        client._fast_push_event.clear()
        await client._dispatch(ws, _viewers(1))
        assert client._presence_wake_force is False
        assert not client._fast_push_event.is_set()

    asyncio.run(scenario())


def test_ping_viewers_level_updates_count_and_pong_is_unchanged():
    """The PING-borne level heals the belief; the PONG reply is unaffected."""
    client = _client()
    ws = _FakeWS()

    async def scenario():
        client._fast_push_event = asyncio.Event()
        await client._dispatch(ws, protocol.make_ping(seq=7, viewers=0))
        assert client.viewer_count == 0
        await client._dispatch(ws, protocol.make_ping(seq=8, viewers=3))
        assert client.viewer_count == 3
        assert client._presence_wake_force is True

    asyncio.run(scenario())
    assert _types(ws) == [protocol.MSG_PONG, protocol.MSG_PONG]
    assert [m.seq for m in ws.sent] == [7, 8]


def test_v3_server_never_reports_an_effective_zero():
    """Against a < v4 peer no viewers input may downshift the client."""
    client = _client(peer_version="3", fast=0.1, status=0.5)
    ws = _FakeWS()

    async def scenario():
        client._fast_push_event = asyncio.Event()
        await client._dispatch(ws, _viewers(0))
        await client._dispatch(ws, protocol.make_ping(seq=1, viewers=0))

    asyncio.run(scenario())
    # Effective count stays unknown → assume watched → full-speed cadence.
    assert client.viewer_count is None
    assert client._effective_intervals() == (0.1, 0.5)


def test_missing_or_malformed_viewers_keeps_current_gear():
    """A PING without viewers — or with garbage — never moves the gear."""
    client = _client()
    ws = _FakeWS()

    async def scenario():
        client._fast_push_event = asyncio.Event()
        await client._dispatch(ws, _viewers(0))
        assert client.viewer_count == 0
        # Level-less PING (a v3-shaped heartbeat): belief unchanged.
        await client._dispatch(ws, protocol.make_ping(seq=2))
        assert client.viewer_count == 0
        # Garbage counts: ignored, and no spurious wake is armed.
        for bogus in ("3", -1, 1.5, True, None, {}):
            await client._dispatch(
                ws,
                protocol.Message(
                    type=protocol.MSG_PING, payload={"viewers": bogus}, seq=3
                ),
            )
            await client._dispatch(ws, _viewers(bogus))
        assert client.viewer_count == 0
        assert client._presence_wake_force is False

    asyncio.run(scenario())


def test_unknown_message_type_is_silently_ignored():
    """An unrecognised inbound frame is dropped without error or reply."""
    client = _client()
    ws = _FakeWS()
    asyncio.run(
        client._dispatch(
            ws, protocol.Message(type="mystery-frame", payload={"x": 1})
        )
    )
    assert ws.sent == []


# --------------------------------------------------------------------------
# task 9 — gear application: intervals, idle slowdown, 0→1 wake, fast-push
# --------------------------------------------------------------------------


def test_effective_intervals_switch_with_viewer_count():
    """viewers==0 (v4 peer) selects the idle constants; >0 / unknown the config."""
    client = _client(fast=0.1, status=0.5)
    assert client._effective_intervals() == (0.1, 0.5)  # unknown → full speed
    client._viewer_count = 0
    assert client._effective_intervals() == (
        client_module._IDLE_FAST_INTERVAL,
        client_module._IDLE_STATUS_INTERVAL,
    )
    client._viewer_count = 1
    assert client._effective_intervals() == (0.1, 0.5)  # recovery to full speed


def test_idle_gear_slows_the_scan_cadence(monkeypatch):
    """With zero viewers the fast tick (and its disk scans) all but stops."""
    monkeypatch.setattr(client_module, "_IDLE_FAST_INTERVAL", 5.0)
    monkeypatch.setattr(client_module, "_IDLE_STATUS_INTERVAL", 10.0)

    def _scan_count(viewer_count):
        calls = _CountingCalls()
        history = _FakeHistory()
        client = _client(
            history_provider=history, calls_provider=calls, fast=0.1, status=0.5
        )
        client._viewer_count = viewer_count
        ws = _FakeWS()

        async def body():
            await asyncio.sleep(0.55)

        asyncio.run(_run_push_loop_for(client, ws, body))
        return calls.calls + history.signature_calls

    idle_scans = _scan_count(0)
    active_scans = _scan_count(2)
    # Idle: the first tick is 5 s away, so the loop never scans in the window.
    assert idle_scans == 0
    # Active: the 0.02 s cadence scans many times over the same window.
    assert active_scans > idle_scans + 2


def test_presence_wake_forces_full_status_and_full_index(monkeypatch):
    """A 0→1 edge mid-idle forces STATUS_UPDATE + HISTORY_INDEX within a tick."""
    monkeypatch.setattr(client_module, "_IDLE_FAST_INTERVAL", 5.0)
    monkeypatch.setattr(client_module, "_IDLE_STATUS_INTERVAL", 10.0)
    history = _FakeHistory()
    client = _client(history_provider=history, fast=0.1, status=0.5)
    client._viewer_count = 0
    ws = _FakeWS()

    async def body():
        # Prime the status baseline so a non-forced push would collapse to a
        # keepalive — proving the wake genuinely bypasses the content gate.
        await client._push_status(ws)
        assert _types(ws) == [protocol.MSG_STATUS_UPDATE]
        before = history.invalidations
        await asyncio.sleep(0.1)
        assert len(ws.sent) == 1  # idle gear: nothing pushed on its own
        await client._dispatch(ws, _viewers(1))
        assert await _wait_until(lambda: len(ws.sent) >= 3)
        types = _types(ws)[1:]
        assert protocol.MSG_STATUS_UPDATE in types  # full, not keepalive
        assert protocol.MSG_HISTORY_INDEX in types  # force_index re-baseline
        assert protocol.MSG_KEEPALIVE not in types
        assert history.invalidations > before  # wake refuses a stale TTL index

    asyncio.run(_run_push_loop_for(client, ws, body))


def test_trigger_fast_push_stays_instant_in_idle_gear(monkeypatch):
    """A server-command wake (interject/issue/spawn) bypasses the idle cadence."""
    monkeypatch.setattr(client_module, "_IDLE_FAST_INTERVAL", 5.0)
    monkeypatch.setattr(client_module, "_IDLE_STATUS_INTERVAL", 10.0)
    client = _client(fast=0.1, status=0.5)
    client._viewer_count = 0
    ws = _FakeWS()

    async def body():
        await asyncio.sleep(0.05)
        assert ws.sent == []
        client._trigger_fast_push()
        assert await _wait_until(lambda: len(ws.sent) >= 1)
        assert ws.sent[0].type == protocol.MSG_STATUS_UPDATE

    asyncio.run(_run_push_loop_for(client, ws, body))


# --------------------------------------------------------------------------
# task 10 — Daemon._poll_loop gearing
# --------------------------------------------------------------------------


def _daemon(tmp_path, *, poll_interval: float = 0.02) -> Daemon:
    return Daemon(
        DaemonConfig(
            pid_dir=tmp_path / "daemon-home",
            poll_interval=poll_interval,
            gc_interval=0,  # GC off: these tests time the poll cadence alone
        )
    )


def _count_polls(daemon: Daemon, duration: float) -> int:
    counter = {"n": 0}

    async def fake_poll_once():
        counter["n"] += 1

    daemon._poll_once = fake_poll_once  # type: ignore[method-assign]

    async def scenario():
        daemon._stop_event = asyncio.Event()
        task = asyncio.create_task(daemon._poll_loop())
        await asyncio.sleep(duration)
        daemon._stop_event.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    return counter["n"]


def test_poll_loop_downshifts_when_no_viewers(monkeypatch, tmp_path):
    """viewers==0 on a connected client stretches the poll wait to the idle gear."""
    monkeypatch.setattr(daemon_module, "_IDLE_POLL_INTERVAL", 5.0)
    idle_daemon = _daemon(tmp_path)
    idle_daemon._client = SimpleNamespace(connected=True, viewer_count=0)
    active_daemon = _daemon(tmp_path)
    active_daemon._client = SimpleNamespace(connected=True, viewer_count=2)

    idle_polls = _count_polls(idle_daemon, 0.3)
    active_polls = _count_polls(active_daemon, 0.3)
    # Idle: one immediate poll, then a 5 s wait the window never outlasts.
    assert idle_polls == 1
    assert active_polls >= 3


def test_poll_loop_without_client_keeps_full_speed(monkeypatch, tmp_path):
    """A local-only daemon (no server client) never downshifts."""
    monkeypatch.setattr(daemon_module, "_IDLE_POLL_INTERVAL", 5.0)
    daemon = _daemon(tmp_path)
    assert daemon._client is None
    assert _count_polls(daemon, 0.3) >= 3


def test_degraded_client_shapes_count_as_watched(tmp_path):
    """Disconnected / attribute-less / malformed clients all mean "watched"."""
    daemon = _daemon(tmp_path)
    # Disconnected: the last-known count is stale the moment the link drops.
    daemon._client = SimpleNamespace(connected=False, viewer_count=0)
    assert daemon._client_viewer_count() is None
    assert daemon._presence_idle() is False
    # An older client object without the property.
    daemon._client = SimpleNamespace(connected=True)
    assert daemon._client_viewer_count() is None
    # Malformed counts.
    for bogus in ("0", -1, 0.5, True, None):
        daemon._client = SimpleNamespace(connected=True, viewer_count=bogus)
        assert daemon._client_viewer_count() is None
    # The one shape that may downshift: a live connection reporting zero.
    daemon._client = SimpleNamespace(connected=True, viewer_count=0)
    assert daemon._presence_idle() is True


def test_stop_event_interrupts_an_idle_gear_wait(tmp_path):
    """Shutdown must not wait out the (default 30 s) idle poll interval."""
    daemon = _daemon(tmp_path)
    daemon._client = SimpleNamespace(connected=True, viewer_count=0)

    async def fake_poll_once():
        pass

    daemon._poll_once = fake_poll_once  # type: ignore[method-assign]

    async def scenario():
        daemon._stop_event = asyncio.Event()
        task = asyncio.create_task(daemon._poll_loop())
        await asyncio.sleep(0.1)  # the loop is now deep in its 30 s idle wait
        daemon._stop_event.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


def test_status_file_reports_the_poll_gear(tmp_path):
    """daemon_status.json carries viewer_count + poll_gear for operators."""
    daemon = _daemon(tmp_path)
    snapshot = MachineStatus(machine_id="m1", hostname="host")

    daemon._client = SimpleNamespace(
        connected=True, viewer_count=0, last_error=None, metrics=None
    )
    daemon._write_status(snapshot, [])
    payload = json.loads(daemon.config.status_file.read_text(encoding="utf-8"))
    assert payload["poll_gear"] == "idle"
    assert payload["viewer_count"] == 0

    daemon._client = SimpleNamespace(
        connected=True, viewer_count=3, last_error=None, metrics=None
    )
    daemon._write_status(snapshot, [])
    payload = json.loads(daemon.config.status_file.read_text(encoding="utf-8"))
    assert payload["poll_gear"] == "full"
    assert payload["viewer_count"] == 3


# --------------------------------------------------------------------------
# task 13 (c) — compat matrix, daemon side: a v4 daemon on a v3 server runs a
# full revision-3 session — full speed, no downshift, no new frame types.
# --------------------------------------------------------------------------


def test_v4_daemon_on_v3_server_stays_full_speed_revision3():
    """v4 daemon × v3 server: the WELCOME pins the peer at revision 3, so no
    presence input may ever downshift, the push loop keeps the configured
    cadence, and every outbound frame is one a v3 server understands."""
    history = _FakeHistory()
    client = _client(
        peer_version=None, history_provider=history, fast=0.1, status=0.5
    )
    ws = _FakeWS()

    async def body():
        # The v3 server's real WELCOME (accepted, protocol_version "3").
        await client._dispatch(
            ws,
            protocol.Message(
                type=protocol.MSG_WELCOME,
                payload={
                    "server_version": "old",
                    "protocol_version": "3",
                    "accepted": True,
                },
            ),
        )
        # Its heartbeats carry no viewers level; PONGs must flow regardless.
        await client._dispatch(ws, protocol.make_ping(seq=1))
        # Even a stray zero (misrouted frame) cannot downshift against v3.
        await client._dispatch(ws, _viewers(0))
        assert client.viewer_count is None
        assert client._effective_intervals() == (0.1, 0.5)
        # The push loop runs at the configured full-speed cadence: the 0.5 s
        # status heartbeat fires repeatedly inside this window.
        await _wait_until(
            lambda: len(
                [
                    m
                    for m in ws.sent
                    if m.type
                    in (protocol.MSG_STATUS_UPDATE, protocol.MSG_KEEPALIVE)
                ]
            )
            >= 2,
            timeout=3.0,
        )

    asyncio.run(_run_push_loop_for(client, ws, body))
    sent_types = set(_types(ws))
    # Nothing outside the daemon→server vocabulary a v3 server already knows
    # (revision 4 added no daemon→server frame; MSG_VIEWERS is downlink-only).
    assert sent_types <= set(protocol.DAEMON_TO_SERVER)
    assert protocol.MSG_VIEWERS not in sent_types
    assert protocol.MSG_PONG in sent_types


def test_unknown_frame_mid_stream_does_not_kill_the_receive_loop():
    """The receive loop drops an unknown-typed frame and keeps serving — the
    exact behavior a v3 daemon shows a v4 server's MSG_VIEWERS broadcast (its
    decode() rejects the type), so the ungated broadcast is safe fleet-wide."""

    class _IterWS:
        def __init__(self, frames):
            self._frames = list(frames)
            self.sent = []

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._frames:
                raise StopAsyncIteration
            return self._frames.pop(0)

        async def send(self, data):
            self.sent.append(protocol.decode(data))

    client = _client()
    # A frame whose type this endpoint does not know — to a v3 daemon,
    # MSG_VIEWERS is exactly such a frame — followed by a normal PING.
    unknown = json.dumps({"type": "viewers/next", "seq": 1, "payload": {}})
    ws = _IterWS([unknown, protocol.make_ping(seq=2).to_json()])

    asyncio.run(client._receive_loop(ws, asyncio.Event()))

    # The unknown frame was dropped, the session survived, the PING after it
    # was still answered.
    assert [m.type for m in ws.sent] == [protocol.MSG_PONG]
    assert ws.sent[0].seq == 2
