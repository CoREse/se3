"""Tests for ``DaemonAggregator`` flow-scoped pending-call filtering.

Covers the regression where call files written by other flows/sessions in the
same project root leaked into a flow's view because :class:`FlowSnapshot`
emitted *every* call file as pending. After this change,
``FlowSnapshot.pending_calls`` only carries calls whose ``context.flow_id``
matches the current flow (or is unattributed), while
``MachineStatus.pending_calls`` retains the unfiltered machine-wide aggregate.
"""

from __future__ import annotations

import json
from pathlib import Path

from se3.daemon.aggregator import DaemonAggregator, PendingCall


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---- _filter_calls_for_flow ------------------------------------------------


def _call(call_id: str, flow_id: object = "__missing__") -> PendingCall:
    context: dict = {}
    if flow_id != "__missing__":
        context["flow_id"] = flow_id
    return PendingCall(
        call_id=call_id,
        path=f"/tmp/{call_id}.json",
        project_root="/tmp",
        context=context,
    )


def test_filter_keeps_matching_flow_id() -> None:
    calls = [_call("a", flow_id="flow-1"), _call("b", flow_id="flow-1")]
    result = DaemonAggregator._filter_calls_for_flow(calls, "flow-1")
    assert {c.call_id for c in result} == {"a", "b"}


def test_filter_drops_non_matching_flow_id() -> None:
    calls = [
        _call("a", flow_id="flow-1"),
        _call("b", flow_id="flow-2"),
        _call("c", flow_id="flow-1"),
    ]
    result = DaemonAggregator._filter_calls_for_flow(calls, "flow-1")
    assert {c.call_id for c in result} == {"a", "c"}


def test_filter_keeps_missing_flow_id() -> None:
    """Unattributed calls (no context.flow_id) belong to the current flow."""
    calls = [
        _call("a"),  # no flow_id field at all
        _call("b", flow_id=""),  # empty string
        _call("c", flow_id="flow-1"),
        _call("d", flow_id="other"),
    ]
    result = DaemonAggregator._filter_calls_for_flow(calls, "flow-1")
    assert {c.call_id for c in result} == {"a", "b", "c"}


def test_filter_passthrough_when_flow_id_unknown() -> None:
    calls = [
        _call("a", flow_id="flow-1"),
        _call("b", flow_id="flow-2"),
        _call("c"),
    ]
    assert (
        DaemonAggregator._filter_calls_for_flow(calls, None)
        == calls
    )
    assert (
        DaemonAggregator._filter_calls_for_flow(calls, "")
        == calls
    )


# ---- _snapshot_for_root end-to-end ----------------------------------------


def _make_root(
    tmp_path: Path,
    *,
    engine_flow_id: str,
    call_specs: list[tuple[str, dict | None]],
) -> Path:
    """Create a project root with ``engine.json`` and a set of call files.

    ``call_specs`` is a list of ``(call_id, context)`` pairs; pass ``None`` for
    ``context`` to write a call file with no ``context`` field at all (legacy
    untagged call).
    """
    _write(
        tmp_path / "se3" / "state" / "engine.json",
        {
            "flow_id": engine_flow_id,
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

    for call_id, context in call_specs:
        body: dict = {"prompt": "p"}
        if context is not None:
            body["context"] = context
        _write(tmp_path / "se3" / "calls" / f"{call_id}.json", body)

    return tmp_path


def test_snapshot_filters_pending_calls_by_flow_id(tmp_path: Path) -> None:
    root = _make_root(
        tmp_path,
        engine_flow_id="flow-current",
        call_specs=[
            ("matching_01", {"flow_id": "flow-current"}),
            ("other_02", {"flow_id": "flow-other"}),
            ("unattributed_03", {}),
            ("legacy_04", None),
        ],
    )
    aggregator = DaemonAggregator()
    aggregator.add_project_root(root)

    snapshot = aggregator._snapshot_for_root(root)
    assert snapshot is not None
    assert snapshot.flow_id == "flow-current"
    assert {c.call_id for c in snapshot.pending_calls} == {
        "matching_01",
        "unattributed_03",
        "legacy_04",
    }


def test_snapshot_no_engine_json_passthrough(tmp_path: Path) -> None:
    """When engine.json is missing, pending calls pass through unfiltered."""
    _write(
        tmp_path / "se3" / "calls" / "alpha.json",
        {"prompt": "p", "context": {"flow_id": "flow-x"}},
    )
    _write(
        tmp_path / "se3" / "calls" / "beta.json",
        {"prompt": "p"},
    )
    aggregator = DaemonAggregator()
    aggregator.add_project_root(tmp_path)

    snapshot = aggregator._snapshot_for_root(tmp_path)
    assert snapshot is not None
    assert snapshot.flow_id is None
    assert {c.call_id for c in snapshot.pending_calls} == {"alpha", "beta"}


def test_machine_status_pending_calls_unfiltered(tmp_path: Path) -> None:
    """MachineStatus.pending_calls aggregates *all* calls regardless of flow."""
    root = _make_root(
        tmp_path,
        engine_flow_id="flow-current",
        call_specs=[
            ("matching_01", {"flow_id": "flow-current"}),
            ("other_02", {"flow_id": "flow-other"}),
            ("unattributed_03", {}),
        ],
    )
    aggregator = DaemonAggregator()
    aggregator.add_project_root(root)

    status = aggregator.get_snapshot()
    assert len(status.flows) == 1
    # Flow-scoped: filtered to current flow + unattributed.
    assert {c.call_id for c in status.flows[0].pending_calls} == {
        "matching_01",
        "unattributed_03",
    }
    # Machine-wide: unfiltered aggregate, includes the other-flow call.
    assert {c.call_id for c in status.pending_calls} == {
        "matching_01",
        "other_02",
        "unattributed_03",
    }
