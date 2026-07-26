"""build_index invalidation convergence (idle-CPU issue, group G2 task 4).

The old ``BUILD_INDEX_TTL = 3.0`` sat *below* the 5 s status heartbeat, so every
idle status tick expired the cache and paid a cold rebuild (~17.5k stats across
the history tree) with zero disk changes. These tests lock the new contract:

* freshness is driven by change signals — the push loop invalidates when
  ``active_flow_signature`` moves, and every explicit state-changing command
  (spawn / resume / END_SESSION / ISSUE_COMMAND / HISTORY_INDEX_REQUEST)
  invalidates on arrival;
* the TTL is a pure 60 s backstop for changes no signal can see (direct disk
  edits), so consecutive no-change status ticks cost ZERO cold rebuilds;
* HISTORY_INDEX_REQUEST keeps its force-rebuild + full-frame semantics.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import tianluo.daemon.disk_json_cache as djc
from tianluo.daemon import protocol
from tianluo.daemon.client import DaemonClient
from tianluo.daemon.history import BUILD_INDEX_TTL, DaemonHistoryReader


@pytest.fixture(autouse=True)
def _clean_cache():
    djc.clear_cache()
    yield
    djc.clear_cache()


class _FakeWS:
    """Minimal WebSocket stand-in capturing what the client sends."""

    def __init__(self) -> None:
        self.sent = []

    async def send(self, data) -> None:
        self.sent.append(protocol.decode(data))


def _make_client(**kw) -> DaemonClient:
    return DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="1.2.3",
        snapshot_provider=kw.pop("snapshot_provider", lambda: {"machine_id": "m1"}),
        **kw,
    )


def _write_active_flow(root: Path, flow_id: str) -> None:
    state = root / "se3" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "engine.json").write_text(
        json.dumps(
            {
                "flow_id": flow_id,
                "status": "RUNNING",
                "task_description": "live task",
                "task_type": "feature",
                "created_at": "2026-07-17T00:00:00",
                "updated_at": "2026-07-17T00:00:01",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    hist = root / "se3" / "history" / flow_id
    hist.mkdir(parents=True, exist_ok=True)
    (hist / "01_discovery_ab12.jsonl").write_text(
        json.dumps({"type": "message", "content": "hello"}) + "\n",
        encoding="utf-8",
    )


def _count_fresh_builds(monkeypatch, reader: DaemonHistoryReader) -> dict:
    counter = {"n": 0}
    original = reader._build_index_fresh

    def counting():
        counter["n"] += 1
        return original()

    monkeypatch.setattr(reader, "_build_index_fresh", counting)
    return counter


# --------------------------------------------------------------------------- #
# reader-level: TTL is a long backstop, signals drive freshness
# --------------------------------------------------------------------------- #


def test_ttl_is_a_60s_backstop() -> None:
    # Must exceed the 5 s status heartbeat by a wide margin, or every idle
    # status tick degenerates back into a cold rebuild (the stat storm).
    assert BUILD_INDEX_TTL == 60.0


def test_consecutive_status_ticks_no_change_zero_cold_rebuilds(
    tmp_path, monkeypatch
) -> None:
    """Ten 5s-spaced status ticks with no disk change: exactly one build."""
    _write_active_flow(tmp_path, "flow-a")
    reader = DaemonHistoryReader(lambda: [str(tmp_path)])
    counter = _count_fresh_builds(monkeypatch, reader)

    reader.build_index()
    for _ in range(10):
        # Simulate the 5 s status-heartbeat spacing by aging the cache stamp;
        # with the old 3 s TTL every one of these was a cold rebuild.
        reader._index_cache_at -= 5.0
        reader.build_index()
    assert counter["n"] == 1, (
        "no-change status ticks must be served from the cached index"
    )


def test_ttl_backstop_still_rebuilds(tmp_path, monkeypatch) -> None:
    """Past the 60 s backstop the index does rebuild (direct-disk-edit safety)."""
    _write_active_flow(tmp_path, "flow-a")
    reader = DaemonHistoryReader(lambda: [str(tmp_path)])
    counter = _count_fresh_builds(monkeypatch, reader)

    reader.build_index()
    reader._index_cache_at -= BUILD_INDEX_TTL + 1
    reader.build_index()
    assert counter["n"] == 2


def test_invalidate_forces_immediate_rebuild(tmp_path, monkeypatch) -> None:
    _write_active_flow(tmp_path, "flow-a")
    reader = DaemonHistoryReader(lambda: [str(tmp_path)])
    counter = _count_fresh_builds(monkeypatch, reader)

    reader.build_index()
    reader.build_index()
    assert counter["n"] == 1
    reader.invalidate_index_cache()
    reader.build_index()
    assert counter["n"] == 2


def test_new_history_only_flow_rebuilds_without_explicit_invalidate(
    tmp_path, monkeypatch
) -> None:
    """A new history-only flow dir (no engine.json, no signal) still surfaces.

    ``active_flow_signature`` never moves for a history-only flow and no
    command fires, so without the per-root source stat token the flow would
    wait out the full 60 s TTL — breaking the real-daemon e2e acceptance
    (records polled over HTTP within ~18 s).
    """
    _write_active_flow(tmp_path, "flow-a")
    reader = DaemonHistoryReader(lambda: [str(tmp_path)])
    counter = _count_fresh_builds(monkeypatch, reader)

    assert {m.flow_id for m in reader.build_index()} == {"flow-a"}
    reader.build_index()
    assert counter["n"] == 1

    hist = tmp_path / "se3" / "history" / "flow-hist-only"
    hist.mkdir(parents=True)
    (hist / "01_discovery_ab12.jsonl").write_text(
        json.dumps({"type": "message", "content": "hi"}) + "\n",
        encoding="utf-8",
    )

    # No invalidate_index_cache call: the source token alone must trigger it.
    metas = reader.build_index()
    assert counter["n"] == 2
    assert {m.flow_id for m in metas} == {"flow-a", "flow-hist-only"}
    reader.build_index()
    assert counter["n"] == 2  # settled again


def test_archived_engine_json_rebuilds_via_source_token(
    tmp_path, monkeypatch
) -> None:
    """An end-session-style archival (engine.json → archive/) shifts the token."""
    _write_active_flow(tmp_path, "flow-a")
    reader = DaemonHistoryReader(lambda: [str(tmp_path)])
    counter = _count_fresh_builds(monkeypatch, reader)

    assert reader.build_index()[0].source == "active"
    assert counter["n"] == 1

    state = tmp_path / "se3" / "state"
    archive = state / "archive"
    archive.mkdir(parents=True)
    (state / "engine.json").rename(archive / "engine_20260717.json")

    metas = reader.build_index()
    assert counter["n"] == 2
    assert [m.source for m in metas if m.flow_id == "flow-a"] == ["archived"]


# --------------------------------------------------------------------------- #
# push-loop integration: signal-driven invalidation end to end
# --------------------------------------------------------------------------- #


def _run_push_ticks(client: DaemonClient, ws: _FakeWS, n_ticks: int) -> None:
    """Drive ``_push_loop`` for *n_ticks* deterministic status ticks."""

    async def scenario() -> None:
        ticks = {"n": 0}

        async def fake_wait(stop_event: asyncio.Event) -> bool:
            ticks["n"] += 1
            if ticks["n"] > n_ticks:
                stop_event.set()
            return False

        client._wait_next_tick = fake_wait  # type: ignore[method-assign]
        # Make every tick a status tick (status_due always true), matching the
        # worst-case cadence the old TTL turned into a rebuild storm.
        client.status_interval = 0.0
        await client._push_loop(ws, asyncio.Event())

    asyncio.run(scenario())


def test_push_loop_idle_status_ticks_do_not_cold_rebuild(
    tmp_path, monkeypatch
) -> None:
    """No disk change → consecutive status ticks reuse the cached index."""
    _write_active_flow(tmp_path, "flow-a")
    reader = DaemonHistoryReader(lambda: [str(tmp_path)])
    counter = _count_fresh_builds(monkeypatch, reader)
    client = _make_client(history_provider=reader)

    _run_push_ticks(client, _FakeWS(), 5)
    assert counter["n"] == 1, (
        "five idle status ticks must not cold-rebuild the index"
    )


def test_push_loop_disk_change_rebuilds_next_tick(tmp_path, monkeypatch) -> None:
    """An active-flow signature change invalidates and rebuilds immediately."""
    _write_active_flow(tmp_path, "flow-a")
    reader = DaemonHistoryReader(lambda: [str(tmp_path)])
    counter = _count_fresh_builds(monkeypatch, reader)
    client = _make_client(history_provider=reader)

    _run_push_ticks(client, _FakeWS(), 3)
    assert counter["n"] == 1

    # A jsonl append moves active_flow_signature → the next tick must
    # invalidate + rebuild rather than serve the (well within TTL) cache.
    jsonl = tmp_path / "se3" / "history" / "flow-a" / "01_discovery_ab12.jsonl"
    with open(jsonl, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "message", "content": "more"}) + "\n")

    _run_push_ticks(client, _FakeWS(), 2)
    assert counter["n"] == 2, "a signalled disk change must rebuild at once"


# --------------------------------------------------------------------------- #
# explicit-command invalidation
# --------------------------------------------------------------------------- #


class _StubProvider:
    """History-provider stub recording invalidations and builds."""

    def __init__(self) -> None:
        self.invalidations = 0
        self.builds = 0

    def invalidate_index_cache(self) -> None:
        self.invalidations += 1

    def build_index(self) -> list:
        self.builds += 1
        return []

    def read_active_flows(self, cursors) -> list:
        return []


def test_end_session_invalidates_index_cache() -> None:
    provider = _StubProvider()
    calls = []
    client = _make_client(
        history_provider=provider,
        end_session_handler=lambda fid, root, reason: calls.append(fid),
    )
    asyncio.run(
        client._handle_end_session(
            {"flow_id": "flow-x", "project_root": "/tmp/p", "reason": "done"}
        )
    )
    assert calls == ["flow-x"]
    assert provider.invalidations == 1


def test_end_session_failure_does_not_invalidate() -> None:
    """A failed end-session leaves the index alone — nothing changed on disk."""
    provider = _StubProvider()

    def boom(fid, root, reason):
        raise RuntimeError("no such flow")

    client = _make_client(history_provider=provider, end_session_handler=boom)
    asyncio.run(
        client._handle_end_session(
            {"flow_id": "flow-x", "project_root": "/tmp/p"}
        )
    )
    assert provider.invalidations == 0


def test_issue_command_invalidates_index_cache(tmp_path, monkeypatch) -> None:
    provider = _StubProvider()
    client = _make_client(history_provider=provider)
    resolved = str(Path(tmp_path).resolve())
    client._last_known_project_roots = {resolved}
    monkeypatch.setattr(
        client, "_execute_issue_operation", lambda op, root, payload: "001"
    )
    ws = _FakeWS()
    asyncio.run(
        client._handle_issue_command(
            ws,
            {
                "operation": "create",
                "project_root": str(tmp_path),
                "request_id": "r1",
                "description": "d",
            },
        )
    )
    assert provider.invalidations == 1
    results = [m for m in ws.sent if m.type == protocol.MSG_ISSUE_RESULT]
    assert results and results[0].payload.get("ok") is True


def test_history_index_request_forces_invalidate_and_full_frame() -> None:
    """HISTORY_INDEX_REQUEST keeps its force-rebuild + full-index semantics."""
    provider = _StubProvider()
    client = _make_client(history_provider=provider)
    ws = _FakeWS()
    asyncio.run(client._handle_history_index_request(ws))
    assert provider.invalidations == 1
    assert provider.builds >= 1
    index_frames = [m for m in ws.sent if m.type == protocol.MSG_HISTORY_INDEX]
    assert len(index_frames) == 1
