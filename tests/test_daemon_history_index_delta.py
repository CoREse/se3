"""Tests for the daemon's incremental HISTORY_INDEX delta push (group G3).

These lock the traffic-reduction contract on the daemon side: a full
``MSG_HISTORY_INDEX`` is a reconciliation baseline (connect / reconnect /
HISTORY_INDEX_REQUEST / a legacy peer), while the steady state ships only the
changed meta rows as a ``MSG_HISTORY_INDEX_DELTA`` keyed by ``flow_id``. An
updated_at-only "liveness tick" is throttled to the status heartbeat so an
active flow no longer re-pushes its whole meta row every few seconds.
"""

from __future__ import annotations

import asyncio

from se3.daemon import protocol
from se3.daemon.client import DaemonClient
from se3.daemon.history import (
    FlowRead,
    SessionMeta,
    meta_change_is_throttleable,
)


class _FakeWS:
    """Minimal WebSocket stand-in capturing decoded frames the client sends."""

    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(protocol.decode(data))


class _IndexProvider:
    """History provider whose index + active reads are set per test."""

    def __init__(self):
        self.metas: list = []
        self.reads: list = []

    def build_index(self):
        return list(self.metas)

    def read_active_flows(self, cursors):
        return list(self.reads)

    def active_flow_signature(self):
        return {}


def _make_client(provider, *, reduction=True):
    client = DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="6.4.0",
        snapshot_provider=lambda: {"machine_id": "m1"},
        history_provider=provider,
    )
    # A test drives _push_history directly, bypassing the WELCOME handshake that
    # normally flips this flag; set it explicitly to model a rev-3 peer.
    client._peer_supports_reduction = reduction
    return client


def _meta(flow_id, *, updated_at="t1", status="running", step_count=1):
    return SessionMeta(
        flow_id=flow_id,
        project_root="/p",
        status=status,
        updated_at=updated_at,
        step_count=step_count,
        active=True,
    )


def _frames(ws, msg_type):
    return [m for m in ws.sent if m.type == msg_type]


# --------------------------------------------------------------------------
# history.py: updated_at-only classification helper
# --------------------------------------------------------------------------


def test_meta_change_is_throttleable_updated_at_only():
    old = _meta("f1", updated_at="t1").to_dict()
    new = _meta("f1", updated_at="t2").to_dict()
    assert meta_change_is_throttleable(new, old) is True


def test_meta_change_is_throttleable_substantive_change_is_not():
    old = _meta("f1", updated_at="t1", status="running").to_dict()
    # status changed alongside updated_at -> substantive, must not be throttled.
    new = _meta("f1", updated_at="t2", status="done").to_dict()
    assert meta_change_is_throttleable(new, old) is False


def test_meta_change_is_throttleable_identical_is_false():
    same = _meta("f1", updated_at="t1").to_dict()
    # Nothing changed -> there is no delta at all, so nothing to throttle.
    assert meta_change_is_throttleable(dict(same), dict(same)) is False


# --------------------------------------------------------------------------
# baseline (full) vs delta selection
# --------------------------------------------------------------------------


def test_force_index_sends_full_baseline_and_primes():
    provider = _IndexProvider()
    provider.metas = [_meta("f1"), _meta("f2")]
    client = _make_client(provider)
    ws = _FakeWS()

    asyncio.run(client._push_history(ws, force_index=True))

    full = _frames(ws, protocol.MSG_HISTORY_INDEX)
    assert len(full) == 1
    assert {m["flow_id"] for m in full[0].payload["sessions"]} == {"f1", "f2"}
    assert client._index_primed is True
    # No delta frame is emitted on the baseline push.
    assert _frames(ws, protocol.MSG_HISTORY_INDEX_DELTA) == []


def test_single_flow_change_emits_only_that_flow_upsert():
    provider = _IndexProvider()
    provider.metas = [_meta("f1"), _meta("f2")]
    client = _make_client(provider)
    ws = _FakeWS()

    asyncio.run(client._push_history(ws, force_index=True))  # baseline
    ws.sent.clear()

    # Only f1 changes substantively; f2 is untouched.
    provider.metas = [_meta("f1", status="done"), _meta("f2")]
    asyncio.run(client._push_history(ws))

    assert _frames(ws, protocol.MSG_HISTORY_INDEX) == []  # no full re-push
    deltas = _frames(ws, protocol.MSG_HISTORY_INDEX_DELTA)
    assert len(deltas) == 1
    upserts = deltas[0].payload["upserts"]
    assert [m["flow_id"] for m in upserts] == ["f1"]
    assert upserts[0]["status"] == "done"
    assert deltas[0].payload["removed"] == []


def test_vanished_flow_emits_removal():
    provider = _IndexProvider()
    provider.metas = [_meta("f1"), _meta("f2")]
    client = _make_client(provider)
    ws = _FakeWS()

    asyncio.run(client._push_history(ws, force_index=True))  # baseline
    ws.sent.clear()

    provider.metas = [_meta("f2")]  # f1 disappeared
    asyncio.run(client._push_history(ws))

    deltas = _frames(ws, protocol.MSG_HISTORY_INDEX_DELTA)
    assert len(deltas) == 1
    assert deltas[0].payload["upserts"] == []
    assert deltas[0].payload["removed"] == ["f1"]


def test_unchanged_index_emits_no_frame():
    provider = _IndexProvider()
    provider.metas = [_meta("f1")]
    client = _make_client(provider)
    ws = _FakeWS()

    asyncio.run(client._push_history(ws, force_index=True))  # baseline
    ws.sent.clear()

    # Nothing changed since the baseline -> no index/delta frame at all.
    asyncio.run(client._push_history(ws))
    assert _frames(ws, protocol.MSG_HISTORY_INDEX) == []
    assert _frames(ws, protocol.MSG_HISTORY_INDEX_DELTA) == []


def test_history_index_request_resends_full_baseline():
    provider = _IndexProvider()
    provider.metas = [_meta("f1")]
    client = _make_client(provider)
    ws = _FakeWS()

    asyncio.run(client._push_history(ws, force_index=True))  # initial baseline
    ws.sent.clear()

    async def scenario():
        await client._dispatch(ws, protocol.make_history_index_request())

    asyncio.run(scenario())
    # A HISTORY_INDEX_REQUEST forces a full baseline even for a rev-3 peer, so
    # the server can reconcile a fresh view (never a delta).
    assert len(_frames(ws, protocol.MSG_HISTORY_INDEX)) == 1
    assert _frames(ws, protocol.MSG_HISTORY_INDEX_DELTA) == []


# --------------------------------------------------------------------------
# updated_at-only throttle (task 2)
# --------------------------------------------------------------------------


def test_updated_at_only_change_is_throttled_off_heartbeat():
    provider = _IndexProvider()
    provider.metas = [_meta("f1", updated_at="t1")]
    client = _make_client(provider)
    ws = _FakeWS()

    asyncio.run(client._push_history(ws, force_index=True))  # baseline @t1
    ws.sent.clear()

    # Only updated_at moved (a jsonl append) and this is a fast tick, not the
    # heartbeat -> the meta row is held back.
    provider.metas = [_meta("f1", updated_at="t2")]
    asyncio.run(client._push_history(ws, status_tick=False))
    assert _frames(ws, protocol.MSG_HISTORY_INDEX_DELTA) == []

    # The status heartbeat flushes the pending updated_at-only change.
    asyncio.run(client._push_history(ws, status_tick=True))
    deltas = _frames(ws, protocol.MSG_HISTORY_INDEX_DELTA)
    assert len(deltas) == 1
    upserts = deltas[0].payload["upserts"]
    assert [m["flow_id"] for m in upserts] == ["f1"]
    assert upserts[0]["updated_at"] == "t2"


def test_substantive_change_is_not_throttled_off_heartbeat():
    provider = _IndexProvider()
    provider.metas = [_meta("f1", updated_at="t1", status="running")]
    client = _make_client(provider)
    ws = _FakeWS()

    asyncio.run(client._push_history(ws, force_index=True))  # baseline
    ws.sent.clear()

    # A real state change (status) must be delivered immediately even on a fast
    # (non-heartbeat) tick.
    provider.metas = [_meta("f1", updated_at="t2", status="done")]
    asyncio.run(client._push_history(ws, status_tick=False))
    deltas = _frames(ws, protocol.MSG_HISTORY_INDEX_DELTA)
    assert len(deltas) == 1
    assert deltas[0].payload["upserts"][0]["status"] == "done"


# --------------------------------------------------------------------------
# legacy-peer fallback + coexistence with active-flow cursor pushes
# --------------------------------------------------------------------------


def test_legacy_peer_always_gets_full_index_not_delta():
    provider = _IndexProvider()
    provider.metas = [_meta("f1")]
    client = _make_client(provider, reduction=False)  # a v2 server
    ws = _FakeWS()

    asyncio.run(client._push_history(ws, force_index=True))
    ws.sent.clear()

    provider.metas = [_meta("f1", status="done")]
    asyncio.run(client._push_history(ws))
    # A legacy peer would reject a delta frame, so the change re-pushes a full
    # HISTORY_INDEX instead.
    assert len(_frames(ws, protocol.MSG_HISTORY_INDEX)) == 1
    assert _frames(ws, protocol.MSG_HISTORY_INDEX_DELTA) == []


def test_index_push_frames_are_metered_by_type():
    """Every index frame that leaves the socket is counted in ``metrics`` by type.

    The per-type wire accounting is the verification handle for the whole
    traffic-reduction pass: a full baseline and an incremental delta must land
    under distinct message-type keys so "where are the bytes going?" is
    answerable at runtime.
    """
    provider = _IndexProvider()
    provider.metas = [_meta("f1"), _meta("f2")]
    client = _make_client(provider)
    ws = _FakeWS()

    asyncio.run(client._push_history(ws, force_index=True))  # full baseline
    provider.metas = [_meta("f1", status="done"), _meta("f2")]
    asyncio.run(client._push_history(ws))  # delta

    snap = client.metrics.snapshot()
    assert snap[protocol.MSG_HISTORY_INDEX]["count"] == 1
    assert snap[protocol.MSG_HISTORY_INDEX_DELTA]["count"] == 1
    assert snap[protocol.MSG_HISTORY_INDEX]["bytes"] > 0
    assert snap[protocol.MSG_HISTORY_INDEX_DELTA]["bytes"] > 0
    # The single-flow delta is smaller than the two-flow full baseline it replaces.
    assert (
        snap[protocol.MSG_HISTORY_INDEX_DELTA]["bytes"]
        < snap[protocol.MSG_HISTORY_INDEX]["bytes"]
    )


def test_active_flow_cursor_push_coexists_with_index_delta():
    provider = _IndexProvider()
    provider.metas = [_meta("f1")]
    client = _make_client(provider)
    ws = _FakeWS()

    asyncio.run(client._push_history(ws, force_index=True))  # baseline
    ws.sent.clear()

    # f1 advances: its meta changes AND it emits new conversation records. Both
    # an index delta and the incremental HISTORY_DATA frame must go out.
    provider.metas = [_meta("f1", updated_at="t2", status="done")]
    provider.reads = [
        FlowRead(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step_id": "s", "message": {"role": "user", "content": "x"}}],
            {"s.jsonl": 2},
        )
    ]
    asyncio.run(client._push_history(ws))

    assert len(_frames(ws, protocol.MSG_HISTORY_INDEX_DELTA)) == 1
    assert len(_frames(ws, protocol.MSG_HISTORY_DATA)) == 1
    assert client._history_cursors == {"f1": {"s.jsonl": 2}}
