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


def test_filter_drops_unattributed_calls_when_flow_id_known() -> None:
    """Unattributed calls (no/empty context.flow_id) are NOT current-flow-scoped.

    Legacy / cross-scenario artifacts like ``merge_<branch>_*`` and
    ``sync_conflicts_*`` write call files without a ``context.flow_id``; the
    filter must drop them so they do not bleed into an unrelated flow's
    pending-intervention list.
    """
    calls = [
        _call("a"),  # no flow_id field at all
        _call("b", flow_id=""),  # empty string
        _call("c", flow_id="flow-1"),
        _call("d", flow_id="other"),
    ]
    result = DaemonAggregator._filter_calls_for_flow(calls, "flow-1")
    assert {c.call_id for c in result} == {"c"}


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
    extra_call_payloads: dict[str, dict] | None = None,
) -> Path:
    """Create a project root with ``engine.json`` and a set of call files.

    ``call_specs`` is a list of ``(call_id, context)`` pairs; pass ``None`` for
    ``context`` to write a call file with no ``context`` field at all (legacy
    untagged call).

    ``extra_call_payloads`` is an optional mapping of ``call_id -> payload``
    used to write call files whose on-disk shape doesn't fit the
    ``(call_id, context)`` shorthand — e.g. legacy producers that record
    ``flow_id`` at the top level of the payload (mirroring
    ``_write_discovery_call`` in ``src/se3/commands/run.py``).
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

    if extra_call_payloads:
        for call_id, payload in extra_call_payloads.items():
            _write(tmp_path / "se3" / "calls" / f"{call_id}.json", payload)

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
    # Strict scoping: only calls whose context.flow_id matches the current
    # engine.json flow_id are surfaced. Unattributed and legacy untagged
    # call files (typical of ``merge_*`` and ``sync_conflicts_*`` artifacts
    # left behind by other flows) are dropped.
    assert {c.call_id for c in snapshot.pending_calls} == {"matching_01"}


def test_snapshot_folds_legacy_top_level_flow_id(tmp_path: Path) -> None:
    """Call files with top-level ``flow_id`` (no ``context``) are attributed.

    Producers that predate the ``context.flow_id`` convention — notably
    ``_write_discovery_call`` in ``src/se3/commands/run.py``, which writes
    ``{"flow_id": flow.flow_id, "prompt": ..., ...}`` and never adds a
    ``context`` field — must still be folded into ``context["flow_id"]`` by
    :meth:`DaemonAggregator._parse_call_file` so the per-flow filter keeps
    them visible. Without this fold-up, on-disk discovery call files would
    silently become unattributed and disappear from
    :class:`FlowSnapshot.pending_calls`.
    """
    root = _make_root(
        tmp_path,
        engine_flow_id="flow-current",
        call_specs=[
            ("ctx_match", {"flow_id": "flow-current"}),
        ],
        extra_call_payloads={
            # Mirrors `_write_discovery_call` exactly: top-level flow_id,
            # top-level prompt, NO context field.
            "discovery_legacy": {
                "flow_id": "flow-current",
                "prompt": "Please clarify",
                "step_id": "discovery-1",
            },
            # Same shape but for a different flow_id — must be filtered out
            # for the current flow.
            "discovery_other_flow": {
                "flow_id": "flow-other",
                "prompt": "Other flow",
                "step_id": "discovery-2",
            },
        },
    )
    aggregator = DaemonAggregator()
    aggregator.add_project_root(root)

    snapshot = aggregator._snapshot_for_root(root)
    assert snapshot is not None
    assert snapshot.flow_id == "flow-current"
    # Both the conventional `context.flow_id` call and the legacy
    # top-level-`flow_id` discovery call must survive the per-flow filter;
    # the other-flow legacy call must be dropped.
    assert {c.call_id for c in snapshot.pending_calls} == {
        "ctx_match",
        "discovery_legacy",
    }
    # Verify the fold-up actually populated context.flow_id (not just that
    # the call survived for some other reason).
    legacy = next(
        c for c in snapshot.pending_calls if c.call_id == "discovery_legacy"
    )
    assert legacy.context.get("flow_id") == "flow-current"


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


def test_machine_status_project_roots(tmp_path: Path) -> None:
    """Aggregator surfaces its registered project roots in MachineStatus.

    Older daemons did not include this list; the new frontend's New Task modal
    relies on it to populate the Project select, so the field MUST be present
    and stable. With no registered roots it serializes as an empty list.
    """
    agg_empty = DaemonAggregator()
    status_empty = agg_empty.get_snapshot()
    assert status_empty.project_roots == []
    assert status_empty.to_dict()["project_roots"] == []

    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    proj_a.mkdir()
    proj_b.mkdir()
    agg = DaemonAggregator()
    agg.add_project_root(proj_a)
    agg.add_project_root(proj_b)
    status = agg.get_snapshot()
    assert sorted(status.project_roots) == sorted(
        [str(proj_a.resolve()), str(proj_b.resolve())]
    )
    payload = status.to_dict()
    assert "project_roots" in payload
    assert sorted(payload["project_roots"]) == sorted(status.project_roots)


def test_machine_status_project_roots_includes_historical(tmp_path: Path) -> None:
    """project_roots merges active registrations with historical project roots.

    A root that contains SE3 history artifacts (an archive or a history/
    directory) is surfaced even when it was not added through the normal
    supervisor / spawner path, so the New Task dropdown can list projects
    that ran before the daemon was restarted.
    """
    active_root = tmp_path / "active-proj"
    active_root.mkdir()

    history_root = tmp_path / "history-proj"
    archive_dir = history_root / "se3" / "state" / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "engine_20260101_000000.json").write_text(
        json.dumps({"flow_id": "f1", "status": "completed"}), encoding="utf-8"
    )

    aggregator = DaemonAggregator()
    aggregator.add_project_root(active_root)
    # The historical root is registered too — the daemon would typically
    # learn about it via `config.project_roots` or a prior spawn — but it
    # is not currently spawning a flow.
    aggregator.add_project_root(history_root)

    status = aggregator.get_snapshot()
    assert str(active_root.resolve()) in status.project_roots
    assert str(history_root.resolve()) in status.project_roots
    # No duplicates after a spawn re-registers a historical root.
    aggregator.add_project_root(history_root)
    status2 = aggregator.get_snapshot()
    assert status2.project_roots.count(str(history_root.resolve())) == 1


def test_machine_status_project_roots_sorted_and_unique(tmp_path: Path) -> None:
    """Merged project_roots is sorted and contains no duplicates."""
    proj_a = tmp_path / "a-proj"
    proj_b = tmp_path / "b-proj"
    proj_a.mkdir()
    proj_b.mkdir()
    # Give proj_a some history so it would also be picked up via the
    # historical enumeration path, exercising the dedupe.
    (proj_a / "se3" / "state" / "archive").mkdir(parents=True)
    (proj_a / "se3" / "state" / "archive" / "engine_x.json").write_text(
        json.dumps({"flow_id": "f1"}), encoding="utf-8"
    )

    aggregator = DaemonAggregator()
    aggregator.add_project_root(proj_a)
    aggregator.add_project_root(proj_b)
    status = aggregator.get_snapshot()

    assert status.project_roots == sorted(set(status.project_roots))
    assert status.project_roots.count(str(proj_a.resolve())) == 1


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
    # Flow-scoped: strict — only calls whose context.flow_id matches.
    assert {c.call_id for c in status.flows[0].pending_calls} == {
        "matching_01",
    }
    # Machine-wide: unfiltered aggregate, includes the other-flow call.
    assert {c.call_id for c in status.pending_calls} == {
        "matching_01",
        "other_02",
        "unattributed_03",
    }
