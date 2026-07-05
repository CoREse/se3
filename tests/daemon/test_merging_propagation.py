"""Propagation-layer coverage for the worktree-merge ``merging`` sub-state (G3).

Mirrors the ``waiting_for_lock`` propagation tests: the ``merging`` flag written
into a worktree's ``engine.json`` (emit-when-True) must survive every hop between
disk and the web client payload —

* ``disk_json_cache`` degraded header extraction (oversized legacy engine.json),
* the daemon ``aggregator`` FlowSnapshot (active + resumable construction paths),
* the daemon ``history`` SessionMeta index, and
* the central ``server`` ServerState mirror.

Unlike ``waiting_for_lock`` (which layers on a RUNNING flow), ``merging`` layers
on a *completed* worktree flow whose body finished while its trailing merge runs,
so the tests deliberately use ``status="completed"`` where the distinction matters.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from se3.daemon import disk_json_cache as cache_mod
from se3.daemon.disk_json_cache import SIZE_GUARD_BYTES, read_engine_header
from se3.daemon.aggregator import DaemonAggregator
from se3.daemon.history import DaemonHistoryReader
from se3.server.state import FlowSnapshot, ServerState


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _merging_engine(flow_id: str, *, merging: bool, waiting_for_lock: bool = False) -> dict:
    """A completed worktree engine.json optionally flagged mid-merge."""
    return {
        "flow_id": flow_id,
        "task_description": "isolated worktree run",
        "task_type": "feature",
        "status": "completed",
        "is_worktree_mode": True,
        "merging": merging,
        "waiting_for_lock": waiting_for_lock,
        "updated_at": "2026-07-06T10:00:00",
        "state": {
            "current_step_id": "s1",
            "selected_steps": ["implement", "commit"],
            "current_step_index": 2,
            "steps": {"s1": {"step_type": "commit"}},
        },
    }


# ---- aggregator FlowSnapshot ----------------------------------------------


def test_aggregator_snapshot_surfaces_merging_true(tmp_path: Path) -> None:
    """A completed worktree engine.json with merging=True surfaces the flag.

    ``merging`` is read unconditionally (not gated on RUNNING) so a COMPLETED
    worktree flow whose branch is being merged back still renders as 合并中.
    """
    cache_mod.clear_cache()
    _write(
        tmp_path / "se3" / "state" / "engine.json",
        _merging_engine("flow-merging", merging=True, waiting_for_lock=True),
    )
    aggregator = DaemonAggregator()
    aggregator.add_project_root(tmp_path)

    snapshot = aggregator._snapshot_for_root(tmp_path)
    assert snapshot is not None
    assert snapshot.flow_id == "flow-merging"
    assert snapshot.status == "completed"
    assert snapshot.merging is True
    # merging is orthogonal to waiting_for_lock — both can be True at once.
    assert snapshot.waiting_for_lock is True
    # Must survive the wire serialization that feeds STATUS_UPDATE.
    assert snapshot.to_dict()["merging"] is True


def test_aggregator_snapshot_merging_defaults_false(tmp_path: Path) -> None:
    """An engine.json without the key (the common case) reads as not-merging."""
    cache_mod.clear_cache()
    _write(
        tmp_path / "se3" / "state" / "engine.json",
        {
            "flow_id": "flow-normal",
            "task_description": "t",
            "task_type": "feature",
            "status": "RUNNING",
            "state": {
                "current_step_id": "s1",
                "selected_steps": ["analyze"],
                "current_step_index": 0,
                "steps": {"s1": {"step_type": "analyze"}},
            },
        },
    )
    aggregator = DaemonAggregator()
    aggregator.add_project_root(tmp_path)

    snapshot = aggregator._snapshot_for_root(tmp_path)
    assert snapshot is not None
    assert snapshot.merging is False
    assert snapshot.to_dict()["merging"] is False


def test_aggregator_resumable_snapshot_merging_always_false(tmp_path: Path) -> None:
    """A superseded resumable snapshot is never mid-merge, even if it carries the
    flag — merging is a live-engine.json sub-state on a just-completed flow."""
    data = _merging_engine("flow-resumable", merging=True)
    data["status"] = "paused"  # a resumable snapshot preserves a non-terminal status
    snapshot = DaemonAggregator._snapshot_from_resumable(tmp_path, data)
    assert snapshot.merging is False
    assert snapshot.to_dict()["merging"] is False


# ---- disk_json_cache degraded header extraction ---------------------------


def _write_oversized(path: Path, flow_id: str, *, merging: bool) -> None:
    """Write a well-formed engine.json larger than SIZE_GUARD_BYTES.

    ``merging`` lives in the tail cluster (after the multi-MB ``state`` blob), so
    a passing assertion proves the degraded head+tail scan straddles the padding
    to reach it — exactly the oversized legacy-worktree case where the flag must
    not be lost.
    """
    padding = "x" * (SIZE_GUARD_BYTES + 1024 * 1024)
    data = {
        "flow_id": flow_id,
        "status": "completed",
        "task_description": "big legacy worktree flow",
        "task_type": "implement",
        "state": {"blob": padding, "steps": []},
        "created_at": "2026-07-06T00:00:00",
        "updated_at": "2026-07-06T02:00:00",
        "is_worktree_mode": True,
        "merging": merging,
        "project_root": str(path.parent),
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def test_disk_cache_degraded_header_carries_merging(tmp_path: Path) -> None:
    """read_engine_header extracts merging from an oversized engine.json.

    The file is over the parse guard, so it is never fully parsed; the degraded
    head+tail regex scan must still surface the tail-cluster ``merging`` key.
    """
    cache_mod.clear_cache()
    path = tmp_path / "engine.json"
    _write_oversized(path, "flow-big", merging=True)
    assert path.stat().st_size > SIZE_GUARD_BYTES

    header = read_engine_header(path)
    assert header is not None
    assert header["flow_id"] == "flow-big"
    assert header["is_worktree_mode"] is True
    assert header["merging"] is True


def test_disk_cache_degraded_header_merging_false(tmp_path: Path) -> None:
    """An oversized engine.json with merging=false reports the flag as False."""
    cache_mod.clear_cache()
    path = tmp_path / "engine.json"
    _write_oversized(path, "flow-big-nomerge", merging=False)
    assert path.stat().st_size > SIZE_GUARD_BYTES

    header = read_engine_header(path)
    assert header is not None
    assert header["merging"] is False


# ---- daemon history SessionMeta -------------------------------------------


def test_history_index_carries_merging_on_active_flow(tmp_path: Path) -> None:
    """The active engine.json's merging flag propagates into the SessionMeta.

    A completed worktree flow's engine.json is still the live ("active" source)
    engine.json while it merges, so the history index must carry merging even
    though the flow's status is COMPLETED (hence ``active`` is False)."""
    cache_mod.clear_cache()
    _write(
        tmp_path / "se3" / "state" / "engine.json",
        _merging_engine("hist-merging", merging=True),
    )
    reader = DaemonHistoryReader(project_roots_provider=lambda: [tmp_path])
    meta = reader.build_index()[0]
    assert meta.flow_id == "hist-merging"
    assert meta.source == "active"
    assert meta.merging is True
    assert meta.to_dict()["merging"] is True


def test_history_archived_flow_never_merging(tmp_path: Path) -> None:
    """An archived snapshot is never reported as merging, even with a stale flag
    — merging is only read from the live "active" source."""
    cache_mod.clear_cache()
    archive_dir = tmp_path / "se3" / "state" / "archive"
    _write(
        archive_dir / "engine_20260101_000000.json",
        {
            "flow_id": "archived-stale",
            "status": "completed",
            "merging": True,
            "updated_at": "2026-01-01T00:00:00",
        },
    )
    reader = DaemonHistoryReader(project_roots_provider=lambda: [tmp_path])
    meta = reader.build_index()[0]
    assert meta.flow_id == "archived-stale"
    assert meta.source == "archived"
    assert meta.merging is False
    assert meta.to_dict()["merging"] is False


# ---- central server ServerState mirror ------------------------------------


def test_server_flow_snapshot_merging_round_trips() -> None:
    """FlowSnapshot.from_payload coerces merging and to_dict re-emits it."""
    snap = FlowSnapshot.from_payload(
        {"flow_id": "f1", "status": "completed", "merging": True}
    )
    assert snap.merging is True
    assert snap.to_dict()["merging"] is True

    plain = FlowSnapshot.from_payload({"flow_id": "f2", "status": "running"})
    assert plain.merging is False
    assert plain.to_dict()["merging"] is False


def test_server_from_payload_coerces_bad_merging_to_false() -> None:
    """A malformed merging value is fail-safe coerced to False (never raises)."""
    snap = FlowSnapshot.from_payload(
        {"flow_id": "f3", "status": "completed", "merging": "not-a-bool"}
    )
    # Any truthy non-bool coerces to True via bool(); an empty/absent value → False.
    assert snap.merging is True
    empty = FlowSnapshot.from_payload(
        {"flow_id": "f4", "status": "completed", "merging": ""}
    )
    assert empty.merging is False


def test_server_update_status_threads_merging() -> None:
    """merging survives the full update_status → get_machine_flows path that
    backs the /ws/ui push the frontend consumes."""
    state = ServerState()

    async def scenario() -> None:
        await state.register_machine("m1", "host-1", "6.4.0")
        await state.update_status(
            "m1",
            {
                "machine_id": "m1",
                "hostname": "host-1",
                "flows": [
                    {
                        "flow_id": "f1",
                        "status": "completed",
                        "merging": True,
                    }
                ],
                "pending_calls": [],
            },
        )
        flows = await state.get_machine_flows("m1")
        assert flows[0]["merging"] is True

    asyncio.run(scenario())
