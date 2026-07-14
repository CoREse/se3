"""Regression tests for the daemon's history-cursor commit rule (Bug B-1).

The reported bug: the first round of a ``--worktree`` discovery flow never
appeared in the web chat pane, and no refresh brought it back — the records were
on disk and the daemon had read them, but the server's authoritative bundle
started at round two.

The cause was in :meth:`DaemonClient._push_history`. It installed the whole
read-side cursor map (``self._history_cursors = new_cursors``) *before* the send
loop, and treated a failed ``MSG_HISTORY_DATA`` send as a bare ``log + return``.
The dropped batch was therefore already marked as delivered: the next round read
past it and those records never reached the server again for the lifetime of the
flow.

The fix makes the cursor a *delivery* water mark rather than a *read* water mark
— a flow's new cursor is committed only once its frame has actually left the
socket. Retaining the old cursor makes ``read_flow`` fall back to a full re-read
from that mark next round, so the dropped batch is re-sent.

These tests drive the real :class:`~se3.daemon.history.DaemonHistoryReader` over
real jsonl files (so the re-read semantics are the genuine ones, not a stub's
idea of them) behind a minimal provider, and feed the frames that actually
reached the socket into a real :class:`~se3.server.state.ServerState` to assert
the server ends up with the complete, duplicate-free history.

The async cases drive their own event loop via ``asyncio.run``: pytest-asyncio is
not a test dependency of this project, so the suite must run on a bare pytest.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from se3.daemon import protocol
from se3.daemon.client import DaemonClient
from se3.daemon.history import DaemonHistoryReader
from se3.daemon.protocol import HISTORY_MODE_APPEND, HISTORY_MODE_FULL


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _append_records(root, flow_id: str, step: str, messages: List[dict]) -> None:
    """Append *messages* as jsonl lines to ``<root>/se3/history/<flow>/<step>``."""
    path = root / "se3" / "history" / flow_id / step
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for message in messages:
            fh.write(json.dumps(message) + "\n")


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def _keys(records: List[dict]) -> List[tuple]:
    """The ``(step_id, ordinal)`` identity the frontend reconciles by."""
    return [(r["step_id"], r["ordinal"]) for r in records]


def _bodies(records: List[dict]) -> List[str]:
    return [r["message"]["content"] for r in records]


class _Provider:
    """A HistoryProvider over a real reader, scoped to a fixed set of flows.

    Only the three methods ``_push_history`` calls are implemented. The index is
    left empty so the test exercises the active-flow read/send path alone; the
    reads themselves go through the real ``read_flow``, which is what gives the
    "old cursor ⇒ re-read from that water mark" behaviour its teeth.
    """

    def __init__(self, reader: DaemonHistoryReader, root, flow_ids: List[str]) -> None:
        self._reader = reader
        self._root = str(root)
        self._flow_ids = flow_ids

    def build_index(self) -> List[Any]:
        return []

    def read_active_flows(
        self, cursors: Optional[Dict[str, Dict[str, int]]] = None
    ) -> List[Any]:
        cursors = cursors or {}
        return [
            self._reader.read_flow(
                flow_id, project_root=self._root, cursor=cursors.get(flow_id)
            )
            for flow_id in self._flow_ids
        ]

    def live_flow_ids(self) -> set:
        return set(self._flow_ids)


class _StubWS:
    """A socket stub that records sent frames and can fail HISTORY_DATA sends.

    ``fail_flows`` holds the flow ids whose ``MSG_HISTORY_DATA`` frames raise on
    send — i.e. the frames that never leave the process. Everything else (the
    index baseline) goes through, mirroring a socket that dies mid-push.
    """

    def __init__(self) -> None:
        self.fail_flows: set = set()
        self.frames: List[dict] = []

    async def send(self, data: str) -> None:
        payload = json.loads(data)
        if payload.get("type") == protocol.MSG_HISTORY_DATA:
            flow_id = payload["payload"]["flow_id"]
            if flow_id in self.fail_flows:
                raise ConnectionError(f"socket down while sending {flow_id}")
        self.frames.append(payload)

    def history_frames(self, flow_id: Optional[str] = None) -> List[dict]:
        out = [
            f["payload"]
            for f in self.frames
            if f.get("type") == protocol.MSG_HISTORY_DATA
        ]
        if flow_id is not None:
            out = [p for p in out if p["flow_id"] == flow_id]
        return out


def _client(provider: _Provider) -> DaemonClient:
    return DaemonClient(
        "ws://test.invalid",
        machine_id="m1",
        hostname="testhost",
        se3_version="0.0.0",
        snapshot_provider=lambda: {"machine_id": "m1", "flows": []},
        history_provider=provider,
    )


@pytest.fixture()
def flow_env(tmp_path):
    """A project root with a real reader/provider over one discovery flow."""
    flow_id = "20260714-000000_deadbeef"
    step = "01_discovery_aaaa1111.jsonl"
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
    provider = _Provider(reader, tmp_path, [flow_id])
    return tmp_path, flow_id, step, provider


# --------------------------------------------------------------------------
# the cursor is a delivery water mark, not a read water mark
# --------------------------------------------------------------------------


def test_failed_send_does_not_advance_cursor(flow_env):
    """A HISTORY_DATA send failure must leave the flow's cursor untouched."""

    async def scenario():
        root, flow_id, step, provider = flow_env
        _append_records(
            root, flow_id, step, [_msg("user", "round-1"), _msg("assistant", "r1")]
        )

        client = _client(provider)
        ws = _StubWS()
        ws.fail_flows = {flow_id}

        await client._push_history(ws)

        assert ws.history_frames(flow_id) == []
        # The batch never left the socket, so nothing may be recorded as
        # delivered. Before the fix this held {step: 2} and the two records were
        # lost forever.
        assert flow_id not in client._history_cursors

    asyncio.run(scenario())


def test_failed_batch_is_re_read_and_re_sent_next_round(flow_env):
    """The batch dropped by a failed send is re-read and re-sent next round."""

    async def scenario():
        root, flow_id, step, provider = flow_env
        _append_records(
            root, flow_id, step, [_msg("user", "round-1"), _msg("assistant", "r1")]
        )

        client = _client(provider)
        ws = _StubWS()
        ws.fail_flows = {flow_id}
        await client._push_history(ws)
        assert ws.history_frames(flow_id) == []

        # Socket recovers; the same records must be read again from the retained
        # water mark and shipped as the flow's first (full) frame.
        ws.fail_flows = set()
        await client._push_history(ws)

        frames = ws.history_frames(flow_id)
        assert len(frames) == 1
        assert frames[0]["mode"] == HISTORY_MODE_FULL
        assert _bodies(frames[0]["records"]) == ["round-1", "r1"]
        assert client._history_cursors[flow_id] == {step: 2}

    asyncio.run(scenario())


def test_mid_flow_send_failure_re_sends_only_the_dropped_delta(flow_env):
    """A failure on a later append re-ships exactly that delta, not the whole flow."""

    async def scenario():
        root, flow_id, step, provider = flow_env
        _append_records(
            root, flow_id, step, [_msg("user", "round-1"), _msg("assistant", "r1")]
        )

        client = _client(provider)
        ws = _StubWS()
        await client._push_history(ws)
        assert client._history_cursors[flow_id] == {step: 2}

        # Round 2 lands on disk but the socket dies while it is being shipped.
        _append_records(
            root, flow_id, step, [_msg("user", "round-2"), _msg("assistant", "r2")]
        )
        ws.fail_flows = {flow_id}
        await client._push_history(ws)
        assert client._history_cursors[flow_id] == {step: 2}
        assert len(ws.history_frames(flow_id)) == 1

        ws.fail_flows = set()
        await client._push_history(ws)

        frames = ws.history_frames(flow_id)
        assert len(frames) == 2
        assert frames[1]["mode"] == HISTORY_MODE_APPEND
        assert _bodies(frames[1]["records"]) == ["round-2", "r2"]
        assert client._history_cursors[flow_id] == {step: 4}

    asyncio.run(scenario())


def test_empty_delta_commits_and_send_is_skipped(flow_env):
    """A flow with nothing new ships no frame and still keeps its cursor."""

    async def scenario():
        root, flow_id, step, provider = flow_env
        _append_records(root, flow_id, step, [_msg("user", "round-1")])

        client = _client(provider)
        ws = _StubWS()
        await client._push_history(ws)
        assert client._history_cursors[flow_id] == {step: 1}

        # No new records: the read is an empty append, so there is no delivery
        # that could fail and the cursor is committed unconditionally.
        ws.fail_flows = {flow_id}
        await client._push_history(ws)
        assert client._history_cursors[flow_id] == {step: 1}
        assert len(ws.history_frames(flow_id)) == 1

    asyncio.run(scenario())


def test_successful_flow_cursor_survives_another_flows_failure(tmp_path):
    """A flow already shipped this round is not rolled back by a later failure."""

    async def scenario():
        good, bad = "20260714-000000_aaaa", "20260714-000001_bbbb"
        step = "01_discovery_1111.jsonl"
        reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
        provider = _Provider(reader, tmp_path, [good, bad])
        _append_records(tmp_path, good, step, [_msg("user", "good-1")])
        _append_records(tmp_path, bad, step, [_msg("user", "bad-1")])

        client = _client(provider)
        ws = _StubWS()
        ws.fail_flows = {bad}

        await client._push_history(ws)

        # ``good`` was shipped before the socket died on ``bad``; rolling it back
        # would only manufacture duplicate traffic on the next round.
        assert client._history_cursors[good] == {step: 1}
        assert bad not in client._history_cursors
        assert _bodies(ws.history_frames(good)[0]["records"]) == ["good-1"]
        assert ws.history_frames(bad) == []

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# end to end: what the server ends up holding
# --------------------------------------------------------------------------


def test_server_bundle_is_complete_and_duplicate_free_after_a_drop(flow_env):
    """Every frame that reached the socket, replayed into the real ServerState.

    This is the invariant the whole fix exists for: a mid-flight send failure
    must not cost the server any records, and the re-send must not duplicate
    any either.
    """
    from se3.server.state import ServerState

    async def scenario():
        root, flow_id, step, provider = flow_env
        client = _client(provider)
        ws = _StubWS()
        state = ServerState()

        # Round 1 is written and dropped on the floor by a dying socket.
        _append_records(
            root, flow_id, step, [_msg("user", "round-1"), _msg("assistant", "r1")]
        )
        ws.fail_flows = {flow_id}
        await client._push_history(ws)

        # Round 2 lands while the socket is back up. The round-1 records are
        # re-read from the retained water mark, so this frame carries BOTH rounds.
        _append_records(
            root, flow_id, step, [_msg("user", "round-2"), _msg("assistant", "r2")]
        )
        ws.fail_flows = set()
        await client._push_history(ws)

        # Round 3 is an ordinary incremental append on top.
        _append_records(root, flow_id, step, [_msg("user", "round-3")])
        await client._push_history(ws)

        for frame in ws.history_frames(flow_id):
            await state.apply_history_frame(
                frame["flow_id"],
                frame["mode"],
                frame["records"],
                cursor=frame.get("cursor"),
                machine_id="m1",
            )

        cached = await state.get_history(flow_id)
        assert cached is not None
        assert _bodies(cached["records"]) == [
            "round-1",
            "r1",
            "round-2",
            "r2",
            "round-3",
        ]
        # No record was delivered twice: the physical identity the frontend
        # reconciles by is unique across the whole bundle.
        assert len(set(_keys(cached["records"]))) == len(cached["records"])
        assert cached["cursor"] == {step: 5}

    asyncio.run(scenario())
