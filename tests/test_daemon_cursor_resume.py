"""Regression tests for daemon history-cursor retention across a fail→resume.

These cover the G3 fix: when a flow goes terminal (e.g. FAILED) the daemon
does one final flush, and previously rebuilt its cursor map purely from the
flows returned that round — so the round *after* the flush (no new records)
dropped the flow's cursor entirely. A subsequent ``se3 run --resume`` then
flipped the flow back to RUNNING with no cursor on record, forcing a full
re-read and freezing the web console on the failure snapshot.

The fix retains a final-flushed terminal flow's cursor as long as it is still
the live ``engine.json`` flow (i.e. resumable), while a fully-drained *and*
archived terminal flow is still pruned so the cursor map stays bounded.
"""

from __future__ import annotations

import asyncio

from tianluo.daemon import protocol
from tianluo.daemon.client import DaemonClient
from tianluo.daemon.history import DaemonHistoryReader, FlowRead


def _make_client(provider):
    return DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="6.4.0",
        snapshot_provider=lambda: {"machine_id": "m1"},
        history_provider=provider,
    )


class _FakeWS:
    """Minimal WebSocket stand-in capturing what the client sends."""

    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(protocol.decode(data))


class _ScriptedHistoryProvider:
    """History provider whose reads/live-set can be scripted per round.

    ``read_active_flows`` records the cursor map it was handed so a test can
    assert the daemon continued from the *retained* cursor (append) rather than
    re-reading from scratch (full).
    """

    def __init__(self):
        self.reads: list = []
        self.live: set = set()
        self.seen_cursors: list = []

    def build_index(self):
        return []

    def active_flow_signature(self):
        return {}

    def live_flow_ids(self):
        return set(self.live)

    def read_active_flows(self, cursors):
        # Snapshot what the client passed so the test can verify append-continuation.
        self.seen_cursors.append({k: dict(v) for k, v in (cursors or {}).items()})
        return list(self.reads)


def _data_frames(ws):
    return [m for m in ws.sent if m.type == protocol.MSG_HISTORY_DATA]


def test_terminal_flush_retains_cursor_for_live_flow():
    """A FAILED flow still in engine.json keeps its cursor after the final flush."""
    provider = _ScriptedHistoryProvider()
    client = _make_client(provider)
    ws = _FakeWS()

    # Round 1: f1 is active and produces records.
    provider.live = {"f1"}
    provider.reads = [
        FlowRead(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step_id": "s", "message": {"role": "user", "content": "a"}}],
            {"s.jsonl": 2},
        )
    ]
    asyncio.run(client._push_history(ws))
    assert client._history_cursors == {"f1": {"s.jsonl": 2}}

    # Round 2: f1 just FAILED; the final flush ships its last records.
    provider.reads = [
        FlowRead(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step_id": "s", "message": {"role": "system", "content": "failed"}}],
            {"s.jsonl": 3},
        )
    ]
    asyncio.run(client._push_history(ws))
    assert client._history_cursors == {"f1": {"s.jsonl": 3}}

    # Round 3: f1 is drained but still the live engine.json flow (FAILED, awaiting
    # resume). It produces no records this round -> previously its cursor would be
    # pruned; the fix retains it because it is resumable.
    provider.reads = []
    asyncio.run(client._push_history(ws))
    assert client._history_cursors == {"f1": {"s.jsonl": 3}}


def test_resume_continues_with_append_not_full_reread():
    """After resume, the daemon reads from the retained cursor (append continuation)."""
    provider = _ScriptedHistoryProvider()
    client = _make_client(provider)
    ws = _FakeWS()

    # Seed the post-failure steady state: f1 terminal, drained, cursor retained.
    client._history_cursors = {"f1": {"s.jsonl": 3}}
    provider.live = {"f1"}

    # Resume: f1 flips back to RUNNING and appends one new record. Because the
    # retained cursor was handed to read_active_flows, the read is an APPEND
    # delta carrying only the new line (not a full re-read of s.jsonl[:3]).
    provider.reads = [
        FlowRead(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step_id": "s", "message": {"role": "assistant", "content": "resumed"}}],
            {"s.jsonl": 4},
        )
    ]
    asyncio.run(client._push_history(ws))

    # The retained cursor was passed through, so the next read continues from it.
    assert provider.seen_cursors[-1] == {"f1": {"s.jsonl": 3}}
    assert client._history_cursors == {"f1": {"s.jsonl": 4}}

    frames = _data_frames(ws)
    assert len(frames) == 1
    frame = frames[0]
    assert frame.payload["mode"] == protocol.HISTORY_MODE_APPEND
    assert frame.payload["cursor"] == {"s.jsonl": 4}
    # Exactly the one new record, no duplication of the pre-failure tail.
    assert len(frame.payload["records"]) == 1
    assert frame.payload["records"][0]["message"]["content"] == "resumed"


def test_drained_archived_flow_cursor_is_pruned():
    """A drained terminal flow no longer in engine.json is pruned (bounded growth)."""
    provider = _ScriptedHistoryProvider()
    client = _make_client(provider)
    ws = _FakeWS()

    # f1 is the live (resumable) flow; f2 is a finished+archived flow still
    # lingering in the cursor map from a prior tick.
    client._history_cursors = {"f1": {"s.jsonl": 3}, "f2": {"s.jsonl": 9}}
    provider.live = {"f1"}
    provider.reads = []  # nothing active or flushing this round

    asyncio.run(client._push_history(ws))

    # f1 retained (still resumable), f2 pruned (no longer the engine.json flow).
    assert client._history_cursors == {"f1": {"s.jsonl": 3}}


def test_provider_without_live_flow_ids_falls_back_to_prune():
    """A provider lacking live_flow_ids keeps the prior prune-on-drain behavior."""

    class _LegacyProvider:
        reads: list = []

        def build_index(self):
            return []

        def active_flow_signature(self):
            return {}

        def read_active_flows(self, cursors):
            return list(self.reads)

    provider = _LegacyProvider()
    client = _make_client(provider)
    ws = _FakeWS()

    client._history_cursors = {"f1": {"s.jsonl": 3}}
    provider.reads = []
    asyncio.run(client._push_history(ws))
    # No live_flow_ids -> resumable set is empty -> drained flow pruned.
    assert client._history_cursors == {}


# --------------------------------------------------------------------------
# reader-level: live_flow_ids reads engine.json regardless of status
# --------------------------------------------------------------------------


def _write_engine_json(root, flow_id, status):
    state_dir = root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "engine.json").write_text(
        '{"flow_id": "%s", "status": "%s"}' % (flow_id, status),
        encoding="utf-8",
    )


def test_live_flow_ids_includes_failed_engine_flow(tmp_path):
    """A FAILED engine.json flow counts as live (resumable) for cursor retention."""
    root = tmp_path / "proj"
    _write_engine_json(root, "f-failed", "FAILED")
    reader = DaemonHistoryReader(lambda: [str(root)])
    assert reader.live_flow_ids() == {"f-failed"}


def test_live_flow_ids_bounded_to_one_per_root(tmp_path):
    """Each root contributes at most its single current engine.json flow."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _write_engine_json(root_a, "fa", "RUNNING")
    _write_engine_json(root_b, "fb", "COMPLETED")
    reader = DaemonHistoryReader(lambda: [str(root_a), str(root_b)])
    assert reader.live_flow_ids() == {"fa", "fb"}


def test_live_flow_ids_empty_without_engine_json(tmp_path):
    """A root with no engine.json contributes nothing (no spurious retention)."""
    root = tmp_path / "empty"
    (root / "se3" / "state").mkdir(parents=True, exist_ok=True)
    reader = DaemonHistoryReader(lambda: [str(root)])
    assert reader.live_flow_ids() == set()
