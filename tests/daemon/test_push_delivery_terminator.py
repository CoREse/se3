"""A push delivery the daemon opened must always be closed by the daemon.

``MSG_HISTORY_DATA`` carries a ``final`` bit: ``False`` means "more of this
delivery is coming", and the server keeps the flow's bundle UNSETTLED while one
is outstanding (``ServerState._OpenDelivery`` → ``incomplete: true`` on every
snapshot, and a repair pull once the delivery is provably no longer arriving).
That machinery is what makes a drain cut in the middle detectable at all.

The hole this pins: the reader caps a read AFTER appending the record that
crosses the bound (``MAX_RECORDS_PER_REPORT`` / ``MAX_BYTES_PER_REPORT``), so a
flow whose LAST record lands exactly ON the cap is reported ``truncated`` with
nothing behind it. The next read confirms EOF with an empty record list — and the
push path shipped no frame for an empty read, so the closing declaration was
never sent. A complete bundle then reported itself ``incomplete`` for the rest of
the server's uptime, and fired a pointless repair pull once the stall grace
expired. The two shapes it happens in are covered here: the flow is still active
(the EOF read comes back empty), and the flow has gone terminal (it drops out of
``read_active_flows`` altogether).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from tianluo.daemon import history as history_module
from tianluo.daemon import protocol
from tianluo.daemon.client import DaemonClient
from tianluo.daemon.history import DaemonHistoryReader
from tianluo.server.state import ServerState

MACHINE = "m1"


def _append_records(root, flow_id: str, step: str, messages: List[dict]) -> None:
    path = root / "tianluo" / "history" / flow_id / step
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for message in messages:
            fh.write(json.dumps(message) + "\n")


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


class _Provider:
    """A HistoryProvider over a real reader; ``live`` controls the active set."""

    def __init__(self, reader: DaemonHistoryReader, root, flow_ids: List[str]):
        self._reader = reader
        self._root = str(root)
        self._flow_ids = list(flow_ids)
        #: Flows still ACTIVE. A flow removed here has gone terminal: it is
        #: surfaced by the final-flush pass only while it still has records.
        self.live = set(flow_ids)

    def build_index(self) -> List[Any]:
        return []

    def read_active_flows(
        self, cursors: Optional[Dict[str, Dict[str, int]]] = None
    ) -> List[Any]:
        cursors = cursors or {}
        reads = []
        for flow_id in self._flow_ids:
            read = self._reader.read_flow(
                flow_id, project_root=self._root, cursor=cursors.get(flow_id)
            )
            if flow_id in self.live or read.records:
                reads.append(read)
        return reads

    def live_flow_ids(self) -> set:
        return set(self.live)


class _StubWS:
    def __init__(self) -> None:
        self.frames: List[dict] = []

    async def send(self, data: str) -> None:
        self.frames.append(json.loads(data))

    def history_frames(self, flow_id: str) -> List[dict]:
        return [
            f["payload"]
            for f in self.frames
            if f.get("type") == protocol.MSG_HISTORY_DATA
            and f["payload"]["flow_id"] == flow_id
        ]


def _client(provider: _Provider) -> DaemonClient:
    return DaemonClient(
        "ws://test.invalid",
        machine_id=MACHINE,
        hostname="testhost",
        se3_version="0.0.0",
        snapshot_provider=lambda: {"machine_id": MACHINE, "flows": []},
        history_provider=provider,
    )


async def _apply(state: ServerState, payload: dict) -> None:
    """Feed one wire frame into the server exactly as ``ws.py`` does."""
    outcome = await state.apply_history_frame(
        payload["flow_id"],
        payload["mode"],
        payload.get("records") or [],
        cursor=payload.get("cursor") or {},
        cursor_base=payload.get("cursor_base") or {},
        machine_id=MACHINE,
    )
    await state.take_history_replay_verdict(
        payload["flow_id"],
        mode_full=payload["mode"] == protocol.HISTORY_MODE_FULL,
        chunk_bounded=False,
        cursor_base=payload.get("cursor_base") or {},
        machine_id=MACHINE,
        final=(
            bool(payload["final"])
            if isinstance(payload.get("final"), bool)
            else None
        ),
        # Same pairing ws.py uses: what the daemon DECLARED plus whether the
        # cache actually took it. A refused frame may not settle a delivery.
        applied=outcome.resolves_pull and not outcome.rejected_full,
    )


@pytest.fixture()
def capped_reads(monkeypatch):
    """Cap a read at two records, so a 2-record flow ends exactly ON the bound."""
    monkeypatch.setattr(history_module, "MAX_RECORDS_PER_REPORT", 2)
    return 2


@pytest.fixture()
def flow_env(tmp_path):
    flow_id = "20260714-000000_deadbeef"
    step = "01_discovery_aaaa1111.jsonl"
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(tmp_path)])
    provider = _Provider(reader, tmp_path, [flow_id])
    return tmp_path, flow_id, step, provider


def test_eof_on_the_bound_still_declares_the_delivery_finished(
    flow_env, capped_reads
):
    """An ACTIVE flow: the EOF-confirming empty read ships as the terminator."""

    async def scenario():
        root, flow_id, step, provider = flow_env
        _append_records(
            root, flow_id, step, [_msg("user", "one"), _msg("assistant", "two")]
        )
        client = _client(provider)
        ws = _StubWS()
        state = ServerState()

        await client._push_history(ws)
        frames = ws.history_frames(flow_id)
        assert len(frames) == 1
        # The read stopped exactly on the bound, so the daemon cannot know it is
        # at EOF and truthfully says the delivery has more to come.
        assert frames[0]["final"] is False
        await _apply(state, frames[0])
        assert await state.history_delivery_incomplete(flow_id) is True

        # Next tick: nothing new on disk. The empty read is the EOF confirmation,
        # and it must reach the server as a closing declaration.
        await client._push_history(ws)
        frames = ws.history_frames(flow_id)
        assert len(frames) == 2, (
            "the EOF-confirming read must ship a terminator; without it the "
            "server holds a COMPLETE bundle open forever"
        )
        assert frames[1]["records"] == []
        assert frames[1]["final"] is True
        await _apply(state, frames[1])
        assert await state.history_delivery_incomplete(flow_id) is False

        # …and it happens exactly once: a settled delivery ships nothing more.
        await client._push_history(ws)
        assert len(ws.history_frames(flow_id)) == 2

        snapshot = await state.get_history_snapshot(flow_id)
        assert snapshot["incomplete"] is False
        assert len(snapshot["records"]) == 2

    asyncio.run(scenario())


def test_a_flow_that_went_terminal_on_the_bound_is_still_closed(
    flow_env, capped_reads
):
    """A COMPLETED flow leaves the read set entirely; the terminator is synthesized.

    This is the shape the live defect took: opening a big archived session is
    what drives a multi-frame delivery in the first place, and a completed flow
    gets no further reads — so if its last chunk landed on the bound there was
    nothing left to close the delivery with.
    """

    async def scenario():
        root, flow_id, step, provider = flow_env
        _append_records(
            root, flow_id, step, [_msg("user", "one"), _msg("assistant", "two")]
        )
        client = _client(provider)
        ws = _StubWS()
        state = ServerState()

        await client._push_history(ws)
        await _apply(state, ws.history_frames(flow_id)[0])
        assert await state.history_delivery_incomplete(flow_id) is True

        # The flow finishes: it is no longer active, and its final flush has no
        # records, so it is absent from read_active_flows from here on.
        provider.live = set()
        await client._push_history(ws)
        frames = ws.history_frames(flow_id)
        assert len(frames) == 2, (
            "a delivery this daemon opened must be closed by this daemon, even "
            "once the flow has left the active set"
        )
        assert frames[1]["final"] is True
        await _apply(state, frames[1])
        assert await state.history_delivery_incomplete(flow_id) is False

        await client._push_history(ws)
        assert len(ws.history_frames(flow_id)) == 2, "and it is not repeated"

    asyncio.run(scenario())


def test_an_unbounded_read_still_settles_without_a_terminator(flow_env):
    """The common path is untouched: a read under the bound closes its own delivery."""

    async def scenario():
        root, flow_id, step, provider = flow_env
        _append_records(root, flow_id, step, [_msg("user", "one")])
        client = _client(provider)
        ws = _StubWS()
        state = ServerState()

        await client._push_history(ws)
        frames = ws.history_frames(flow_id)
        assert len(frames) == 1 and frames[0]["final"] is True
        await _apply(state, frames[0])
        assert await state.history_delivery_incomplete(flow_id) is False

        # An idle tick ships nothing at all — no terminator storm on every flow.
        await client._push_history(ws)
        assert len(ws.history_frames(flow_id)) == 1

    asyncio.run(scenario())
