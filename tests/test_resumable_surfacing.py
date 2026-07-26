"""Tests for daemon surfacing of per-flow resumable snapshots (group G3).

A paused / interrupted / failed flow writes a snapshot under
``tianluo/state/resumable/<flow_id>.json`` (see ``PersistenceManager``); the next
``se3 run`` overwrites the single-slot ``tianluo/state/engine.json`` but leaves that
snapshot intact. These tests pin the behaviour that:

* :meth:`DaemonAggregator.get_snapshot` re-surfaces such an overwritten flow as
  a ``resumable=True`` :class:`FlowSnapshot` (original status preserved),
  de-duplicated against the live engine.json flow; and
* :meth:`DaemonHistoryReader.build_index` produces a ``resumable=True``
  :class:`SessionMeta` with source ``"resumable"`` for the same flow, winning
  over a history-only degradation.

The negative case — a normally COMPLETED flow has no snapshot and never gains
``resumable`` — is asserted too.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from tianluo.daemon.aggregator import DaemonAggregator
from tianluo.daemon.history import DaemonHistoryReader
from tianluo.server.state import FlowSnapshot, ServerState


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _engine_payload(flow_id: str, status: str) -> dict:
    """A minimal engine.json / resumable-snapshot shaped dict for *flow_id*."""
    return {
        "flow_id": flow_id,
        "status": status,
        "task_description": f"task for {flow_id}",
        "task_type": "feature",
        "state": {
            "selected_steps": ["analyze", "implement"],
            "current_step_index": 1,
            "current_step_id": "s1",
            "steps": {"s1": {"step_type": "implement", "status": "running"}},
        },
        "created_at": "2026-06-18T10:00:00",
        "updated_at": "2026-06-18T10:05:00",
    }


def _write_engine(root: Path, flow_id: str, status: str) -> None:
    _write_json(
        root / "tianluo" / "state" / "engine.json", _engine_payload(flow_id, status)
    )


def _write_resumable(root: Path, flow_id: str, status: str) -> None:
    _write_json(
        root / "tianluo" / "state" / "resumable" / f"{flow_id}.json",
        _engine_payload(flow_id, status),
    )


def _write_history_dir(root: Path, flow_id: str) -> None:
    """Create a non-empty tianluo/history/<flow_id>/ to exercise dedup ordering."""
    flow_dir = root / "tianluo" / "history" / flow_id
    flow_dir.mkdir(parents=True, exist_ok=True)
    (flow_dir / "01_analyze_abc.jsonl").write_text(
        json.dumps({"role": "user", "content": "hi"}) + "\n", encoding="utf-8"
    )


def _aggregator_for(root: Path) -> DaemonAggregator:
    agg = DaemonAggregator()
    agg.add_project_root(root)
    return agg


def _reader_for(root: Path) -> DaemonHistoryReader:
    return DaemonHistoryReader(project_roots_provider=lambda: [str(root)])


# --------------------------------------------------------------------------
# aggregator.get_snapshot
# --------------------------------------------------------------------------


def test_aggregator_resurfaces_overwritten_resumable_flows(tmp_path: Path) -> None:
    """A new active flow plus paused/interrupted/failed snapshots all surface."""
    # engine.json points at a brand-new running flow ...
    _write_engine(tmp_path, "flow_new", "running")
    # ... while three older flows survive only as resumable snapshots, each in a
    # different recoverable status.
    _write_resumable(tmp_path, "flow_paused", "paused")
    _write_resumable(tmp_path, "flow_running", "running")  # interrupted
    _write_resumable(tmp_path, "flow_failed", "failed")
    # The active flow's own snapshot is also on disk (save_flow writes one for
    # every non-completed flow) and must be de-duplicated away.
    _write_resumable(tmp_path, "flow_new", "running")

    status = _aggregator_for(tmp_path).get_snapshot()
    flows = {f.flow_id: f for f in status.flows}

    # No duplicates: each flow appears exactly once.
    assert len(status.flows) == len(flows)
    assert set(flows) == {"flow_new", "flow_paused", "flow_running", "flow_failed"}

    # The three superseded flows surface with their ORIGINAL status + resumable.
    assert flows["flow_paused"].status == "paused"
    assert flows["flow_paused"].resumable is True
    assert flows["flow_running"].status == "running"
    assert flows["flow_running"].resumable is True
    assert flows["flow_failed"].status == "failed"
    assert flows["flow_failed"].resumable is True

    # The live active flow is itself resumable (non-completed running).
    assert flows["flow_new"].status == "running"
    assert flows["flow_new"].resumable is True


def test_aggregator_completed_active_flow_not_resumable(tmp_path: Path) -> None:
    """A completed active flow (no snapshot) is reported with resumable=False."""
    _write_engine(tmp_path, "flow_done", "completed")
    # A normally completed flow has had its resumable snapshot cleared, so the
    # resumable/ dir is absent entirely.

    status = _aggregator_for(tmp_path).get_snapshot()
    flows = {f.flow_id: f for f in status.flows}

    assert "flow_done" in flows
    assert flows["flow_done"].status == "completed"
    assert flows["flow_done"].resumable is False
    # No phantom resumable supplement was emitted.
    assert len(status.flows) == 1


def test_aggregator_stale_completed_snapshot_not_resumable(tmp_path: Path) -> None:
    """A stale completed resumable snapshot is ignored, not surfaced resumable.

    If ``clear_resumable_snapshot`` failed (or an operator/test artifact
    remains), a ``completed`` snapshot can linger under ``tianluo/state/resumable/``.
    The aggregator must NOT advertise it as resumable — the daemon resume
    validator rejects a COMPLETED flow — so it is dropped entirely.
    """
    _write_engine(tmp_path, "flow_new", "running")
    _write_resumable(tmp_path, "flow_old_done", "completed")

    status = _aggregator_for(tmp_path).get_snapshot()
    flows = {f.flow_id: f for f in status.flows}

    # The stale completed snapshot produced no phantom resumable flow.
    assert "flow_old_done" not in flows
    assert set(flows) == {"flow_new"}


def test_aggregator_resumable_snapshot_to_dict_carries_flag(tmp_path: Path) -> None:
    """The resumable flag round-trips through FlowSnapshot.to_dict."""
    _write_engine(tmp_path, "flow_new", "running")
    _write_resumable(tmp_path, "flow_paused", "paused")

    status = _aggregator_for(tmp_path).get_snapshot()
    by_id = {f.flow_id: f.to_dict() for f in status.flows}

    assert by_id["flow_paused"]["resumable"] is True
    assert by_id["flow_paused"]["status"] == "paused"


# --------------------------------------------------------------------------
# history.build_index
# --------------------------------------------------------------------------


def test_history_index_marks_resumable_snapshot_flows(tmp_path: Path) -> None:
    """Overwritten resumable flows index as source='resumable', resumable=True."""
    _write_engine(tmp_path, "flow_new", "running")
    _write_resumable(tmp_path, "flow_paused", "paused")
    _write_resumable(tmp_path, "flow_running", "running")
    _write_resumable(tmp_path, "flow_failed", "failed")
    _write_resumable(tmp_path, "flow_new", "running")  # deduped vs active

    metas = _reader_for(tmp_path).build_index()
    by_id = {m.flow_id: m for m in metas}

    # No duplicates; the active flow's snapshot is deduped away.
    assert len(metas) == len(by_id)
    assert set(by_id) == {"flow_new", "flow_paused", "flow_running", "flow_failed"}

    for fid, expected_status in (
        ("flow_paused", "paused"),
        ("flow_running", "running"),
        ("flow_failed", "failed"),
    ):
        meta = by_id[fid]
        assert meta.source == "resumable", fid
        assert meta.resumable is True, fid
        assert meta.status == expected_status, fid
        # A snapshot-only flow is not the live active flow.
        assert meta.active is False, fid

    # The live active flow stays source='active' and resumable (non-completed).
    assert by_id["flow_new"].source == "active"
    assert by_id["flow_new"].active is True
    assert by_id["flow_new"].resumable is True


def test_history_index_resumable_wins_over_history_only(tmp_path: Path) -> None:
    """A flow with both a snapshot and a history dir indexes as resumable."""
    _write_engine(tmp_path, "flow_new", "running")
    _write_resumable(tmp_path, "flow_paused", "paused")
    # Same flow also has a degraded history-only directory on disk.
    _write_history_dir(tmp_path, "flow_paused")

    metas = _reader_for(tmp_path).build_index()
    by_id = {m.flow_id: m for m in metas}

    assert by_id["flow_paused"].source == "resumable"
    assert by_id["flow_paused"].resumable is True
    assert by_id["flow_paused"].status == "paused"
    # Appears exactly once despite the two on-disk sources.
    assert sum(1 for m in metas if m.flow_id == "flow_paused") == 1


def test_history_index_completed_not_resumable(tmp_path: Path) -> None:
    """A completed flow (no snapshot) indexes with resumable=False."""
    _write_engine(tmp_path, "flow_done", "completed")

    metas = _reader_for(tmp_path).build_index()
    by_id = {m.flow_id: m for m in metas}

    assert by_id["flow_done"].resumable is False
    assert by_id["flow_done"].source == "active"


def test_history_index_stale_completed_snapshot_not_resumable(
    tmp_path: Path,
) -> None:
    """A stale completed snapshot does not index as a resumable source.

    With only a completed snapshot and a history dir, the flow degrades to a
    plain non-resumable history-only row rather than a phantom resumable one.
    """
    _write_resumable(tmp_path, "flow_old_done", "completed")
    _write_history_dir(tmp_path, "flow_old_done")

    metas = _reader_for(tmp_path).build_index()
    by_id = {m.flow_id: m for m in metas}

    assert "flow_old_done" in by_id
    assert by_id["flow_old_done"].source == "history"
    assert by_id["flow_old_done"].resumable is False


def test_history_index_active_wins_over_resumable_snapshot(tmp_path: Path) -> None:
    """When engine.json still points at the flow, the active meta wins (deduped)."""
    _write_engine(tmp_path, "flow_a", "paused")
    _write_resumable(tmp_path, "flow_a", "paused")

    metas = _reader_for(tmp_path).build_index()
    matching = [m for m in metas if m.flow_id == "flow_a"]

    assert len(matching) == 1
    assert matching[0].source == "active"
    # An active paused flow is still resumable via the status-derived default.
    assert matching[0].resumable is True


def test_session_meta_to_dict_includes_resumable(tmp_path: Path) -> None:
    """SessionMeta.to_dict carries the new resumable field."""
    _write_engine(tmp_path, "flow_new", "running")
    _write_resumable(tmp_path, "flow_paused", "paused")

    metas = _reader_for(tmp_path).build_index()
    dicts = {m.flow_id: m.to_dict() for m in metas}

    assert dicts["flow_paused"]["resumable"] is True
    assert dicts["flow_paused"]["source"] == "resumable"


# --------------------------------------------------------------------------
# server.state.FlowSnapshot / ServerState.is_flow_resumable (group G4)
# --------------------------------------------------------------------------


def _server_snapshot(flows: list) -> dict:
    """Minimal MachineStatus-shaped dict for ServerState.update_status."""
    return {"hostname": "h", "flows": flows, "issues": []}


def test_server_flow_snapshot_resumable_round_trips() -> None:
    """The resumable flag survives from_payload -> to_dict, defaulting False."""
    on = FlowSnapshot.from_payload({"flow_id": "f1", "resumable": True})
    assert on.resumable is True
    assert on.to_dict()["resumable"] is True

    # Absent flag (older daemon payload) defaults to False.
    off = FlowSnapshot.from_payload({"flow_id": "f2"})
    assert off.resumable is False
    assert off.to_dict()["resumable"] is False


def test_server_is_flow_resumable_via_flag_overrides_status() -> None:
    """resumable=True passes even when raw status is running (interrupted flow)."""
    state = ServerState()

    async def scenario():
        await state.update_status(
            "m1",
            _server_snapshot(
                [{"flow_id": "f1", "status": "running", "resumable": True}]
            ),
        )
        result = await state.is_flow_resumable("f1")
        assert result is not None
        machine_id, flow = result
        assert machine_id == "m1"
        assert flow["flow_id"] == "f1"

    asyncio.run(scenario())


def test_server_is_flow_resumable_flag_completed_not_resumable() -> None:
    """A completed flow stays non-resumable even though the flag defaults False."""
    state = ServerState()

    async def scenario():
        await state.update_status(
            "m1",
            _server_snapshot([{"flow_id": "f1", "status": "completed"}]),
        )
        assert await state.is_flow_resumable("f1") is None

    asyncio.run(scenario())


def test_server_is_flow_resumable_completed_flag_ignored() -> None:
    """A completed flow is non-resumable even if a stale resumable=True leaks in.

    The completed guard takes precedence over the authoritative flag, mirroring
    the daemon resume validator which rejects a COMPLETED flow.
    """
    state = ServerState()

    async def scenario():
        await state.update_status(
            "m1",
            _server_snapshot(
                [{"flow_id": "f1", "status": "completed", "resumable": True}]
            ),
        )
        assert await state.is_flow_resumable("f1") is None

    asyncio.run(scenario())


def test_server_is_flow_resumable_legacy_status_fallback() -> None:
    """Without the flag, the legacy status∈{failed,paused} fallback still works."""
    state = ServerState()

    async def scenario():
        # paused without resumable flag -> resumable via fallback
        await state.update_status(
            "m1",
            _server_snapshot([{"flow_id": "f1", "status": "paused"}]),
        )
        assert await state.is_flow_resumable("f1") is not None

        # running without flag -> NOT resumable
        await state.update_status(
            "m1",
            _server_snapshot([{"flow_id": "f2", "status": "running"}]),
        )
        assert await state.is_flow_resumable("f2") is None

    asyncio.run(scenario())
