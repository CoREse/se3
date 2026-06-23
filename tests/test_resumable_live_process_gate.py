"""Tests for the live-process gate on the daemon's resumable determination (G1).

These pin the fix for "a genuinely-running flow still shows a clickable Resume
button". :class:`DaemonAggregator` now accepts a ``live_roots_provider`` (the
daemon wires it to ``supervisor.flows`` + ``is_alive``, the same source as
``request_resume``'s double-spawn guard). The aggregator gates the ``resumable``
flag of a flow:

* a ``RUNNING`` flow whose ``project_root`` has a live process → ``resumable``
  becomes ``False`` (no Resume button);
* a ``RUNNING`` flow whose process has died (root absent from the live set) →
  ``resumable`` stays ``True`` (the interrupted-but-recoverable case);
* ``PAUSED`` / ``FAILED`` flows are never gated by live processes (their Resume
  entry must not regress);
* with no provider injected (``None``) the legacy status-only behavior stands.

Both the active ``engine.json`` path and the per-flow resumable-snapshot path
are covered. The provider is a stub — no real process is probed, so the tests
never touch ``~/.se3``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional, Set

from se3.daemon.aggregator import (
    DaemonAggregator,
    _resumable_with_live_gate,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _engine_payload(flow_id: str, status: str) -> dict:
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
        root / "se3" / "state" / "engine.json", _engine_payload(flow_id, status)
    )


def _write_resumable(root: Path, flow_id: str, status: str) -> None:
    _write_json(
        root / "se3" / "state" / "resumable" / f"{flow_id}.json",
        _engine_payload(flow_id, status),
    )


def _aggregator_for(
    root: Path, live_roots: Optional[Iterable[str]] = None
) -> DaemonAggregator:
    """Build an aggregator whose live-root provider yields *live_roots*.

    ``live_roots is None`` means "no provider injected" (legacy behavior).
    Otherwise the provider returns the given roots verbatim — the aggregator
    normalizes them itself, so passing ``str(root)`` matches a snapshot's
    realpath-normalized ``project_root``.
    """
    provider = None
    if live_roots is not None:
        snapshot = set(live_roots)
        provider = lambda: snapshot  # noqa: E731 - tiny stub
    agg = DaemonAggregator(live_roots_provider=provider)
    agg.add_project_root(root)
    return agg


def _flows_by_id(root: Path, live_roots: Optional[Iterable[str]]) -> dict:
    status = _aggregator_for(root, live_roots).get_snapshot()
    return {f.flow_id: f for f in status.flows}


# --------------------------------------------------------------------------
# pure helper
# --------------------------------------------------------------------------


def test_gate_running_with_live_root_is_not_resumable() -> None:
    live = {os.path.realpath("/proj/a")}
    assert _resumable_with_live_gate("running", "/proj/a", live) is False
    # case-insensitive on status
    assert _resumable_with_live_gate("RUNNING", "/proj/a", live) is False


def test_gate_running_without_live_root_is_resumable() -> None:
    assert _resumable_with_live_gate("running", "/proj/a", set()) is True
    assert (
        _resumable_with_live_gate("running", "/proj/a", {os.path.realpath("/proj/b")})
        is True
    )


def test_gate_paused_failed_ignore_live_roots() -> None:
    live = {os.path.realpath("/proj/a")}
    assert _resumable_with_live_gate("paused", "/proj/a", live) is True
    assert _resumable_with_live_gate("failed", "/proj/a", live) is True
    # transient states are likewise un-gated
    assert _resumable_with_live_gate("init", "/proj/a", live) is True
    assert _resumable_with_live_gate("recovering", "/proj/a", live) is True


def test_gate_completed_never_resumable() -> None:
    live = {os.path.realpath("/proj/a")}
    assert _resumable_with_live_gate("completed", "/proj/a", live) is False
    assert _resumable_with_live_gate("completed", "/proj/a", set()) is False


def test_gate_none_provider_is_legacy_status_only() -> None:
    # live_roots=None => no gate, bare status decision.
    assert _resumable_with_live_gate("running", "/proj/a", None) is True
    assert _resumable_with_live_gate("completed", "/proj/a", None) is False


# --------------------------------------------------------------------------
# active engine.json path
# --------------------------------------------------------------------------


def test_active_running_with_live_process_not_resumable(tmp_path: Path) -> None:
    """Acceptance (1): a truly-running RUNNING flow shows no Resume button."""
    _write_engine(tmp_path, "flow_live", "running")
    flows = _flows_by_id(tmp_path, live_roots={str(tmp_path)})
    assert flows["flow_live"].status == "running"
    assert flows["flow_live"].resumable is False


def test_active_running_dead_process_still_resumable(tmp_path: Path) -> None:
    """Acceptance (2): RUNNING but process dead (no live root) stays resumable."""
    _write_engine(tmp_path, "flow_dead", "running")
    flows = _flows_by_id(tmp_path, live_roots=set())  # no live process
    assert flows["flow_dead"].status == "running"
    assert flows["flow_dead"].resumable is True


def test_active_paused_failed_not_gated_by_live_process(tmp_path: Path) -> None:
    """Acceptance (4): PAUSED / FAILED resume entry never regresses."""
    paused_root = tmp_path / "paused"
    failed_root = tmp_path / "failed"
    _write_engine(paused_root, "flow_paused", "paused")
    _write_engine(failed_root, "flow_failed", "failed")

    paused_flows = _flows_by_id(paused_root, live_roots={str(paused_root)})
    failed_flows = _flows_by_id(failed_root, live_roots={str(failed_root)})

    assert paused_flows["flow_paused"].resumable is True
    assert failed_flows["flow_failed"].resumable is True


def test_active_legacy_no_provider_running_resumable(tmp_path: Path) -> None:
    """Backward compat: with no provider a RUNNING flow stays resumable."""
    _write_engine(tmp_path, "flow_legacy", "running")
    flows = _flows_by_id(tmp_path, live_roots=None)
    assert flows["flow_legacy"].resumable is True


# --------------------------------------------------------------------------
# resumable-snapshot path
# --------------------------------------------------------------------------


def test_snapshot_running_with_live_process_not_resumable(tmp_path: Path) -> None:
    """A superseded RUNNING snapshot is surfaced but gated when root is live."""
    # A different, completed flow currently owns engine.json ...
    _write_engine(tmp_path, "flow_active", "completed")
    # ... while the interrupted flow survives only as a resumable snapshot.
    _write_resumable(tmp_path, "flow_running", "running")
    _write_resumable(tmp_path, "flow_paused", "paused")
    _write_resumable(tmp_path, "flow_failed", "failed")

    flows = _flows_by_id(tmp_path, live_roots={str(tmp_path)})

    # The RUNNING snapshot is still surfaced (a card / 409, not a 404) but with
    # the Resume button gated off.
    assert "flow_running" in flows
    assert flows["flow_running"].status == "running"
    assert flows["flow_running"].resumable is False
    # PAUSED / FAILED snapshots are never gated by a live process.
    assert flows["flow_paused"].resumable is True
    assert flows["flow_failed"].resumable is True


def test_snapshot_running_dead_process_still_resumable(tmp_path: Path) -> None:
    """A superseded RUNNING snapshot with no live root stays resumable."""
    _write_engine(tmp_path, "flow_active", "completed")
    _write_resumable(tmp_path, "flow_running", "running")

    flows = _flows_by_id(tmp_path, live_roots=set())
    assert flows["flow_running"].status == "running"
    assert flows["flow_running"].resumable is True


def test_snapshot_legacy_no_provider_running_resumable(tmp_path: Path) -> None:
    """Backward compat on the snapshot path: no provider => resumable=True."""
    _write_engine(tmp_path, "flow_active", "completed")
    _write_resumable(tmp_path, "flow_running", "running")

    flows = _flows_by_id(tmp_path, live_roots=None)
    assert flows["flow_running"].resumable is True
