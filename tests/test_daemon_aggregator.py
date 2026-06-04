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
import os
from pathlib import Path

from se3.daemon import protocol
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


def test_get_snapshot_copies_root_set_before_iterating(tmp_path: Path) -> None:
    """get_snapshot must iterate a copy of _project_roots, not the live set.

    The snapshot build is offloaded to a worker thread (``_push_status`` /
    ``_resolve_interject_root`` / the poll loop), while the event loop can call
    ``add_project_root`` (e.g. a webui SPAWN_FLOW for a not-yet-tracked root).
    If get_snapshot iterated the live set, a concurrent add could raise
    ``RuntimeError: Set changed size during iteration`` and silently drop the
    STATUS_UPDATE during task creation. This asserts the loop tolerates the set
    being mutated while the snapshot build is in progress (here driven from the
    per-root callback, standing in for the concurrent add) without raising.
    """
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    proj_a.mkdir()
    proj_b.mkdir()
    agg = DaemonAggregator()
    agg.add_project_root(proj_a)
    agg.add_project_root(proj_b)

    extra = tmp_path / "proj-c"
    extra.mkdir()
    original = agg._snapshot_for_root
    state = {"added": False}

    def _mutate_then_snapshot(root: Path):
        # Mutate the underlying set while the snapshot build is mid-flight; a
        # build that iterated the live set rather than a copy would be at risk.
        if not state["added"]:
            agg.add_project_root(extra)
            state["added"] = True
        return original(root)

    agg._snapshot_for_root = _mutate_then_snapshot  # type: ignore[assignment]

    status = agg.get_snapshot()  # must not raise
    assert extra.resolve() in {Path(p) for p in status.project_roots}


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


# ---- _filter_stale_calls: FAILED-as-stale exemption by call kind ----------


def _stale_call(
    call_id: str,
    *,
    kind: str = protocol.CALL_KIND_CALL,
    step_id: str = "s1",
) -> PendingCall:
    return PendingCall(
        call_id=call_id,
        path=f"/tmp/{call_id}.json",
        project_root="/tmp",
        kind=kind,
        context={"step_id": step_id, "flow_id": "flow-1"},
    )


def _state_with_step_status(step_id: str, status: str) -> dict:
    return {
        "current_step_id": step_id,
        "steps": {step_id: {"step_type": "implement", "status": status}},
    }


def test_filter_stale_keeps_retry_decision_on_failed_current_step() -> None:
    """A retry_decision call on the FAILED current step stays pending.

    The retry_decision chip exists *because* the step failed; without the
    FAILED-as-stale exemption the daemon would filter it out the instant
    the flow paused, hiding the very interaction the human needs to answer.
    """
    state = _state_with_step_status("s1", "failed")
    calls = [_stale_call("rd1", kind=protocol.CALL_KIND_RETRY_DECISION)]
    result = DaemonAggregator._filter_stale_calls(calls, state)
    assert [c.call_id for c in result] == ["rd1"]


def test_filter_stale_drops_plain_call_on_failed_step() -> None:
    """Non-exempt kinds (kind=call) keep the original FAILED-as-stale rule."""
    state = _state_with_step_status("s1", "failed")
    calls = [_stale_call("c1", kind=protocol.CALL_KIND_CALL)]
    result = DaemonAggregator._filter_stale_calls(calls, state)
    assert result == []


def test_filter_stale_drops_retry_decision_on_non_failed_processed_status() -> None:
    """The exemption ONLY removes ``failed`` — completed / partial /
    revision_needed still count as processed for retry_decision."""
    for status in ("completed", "partial", "revision_needed"):
        state = _state_with_step_status("s1", status)
        calls = [_stale_call("rd1", kind=protocol.CALL_KIND_RETRY_DECISION)]
        result = DaemonAggregator._filter_stale_calls(calls, state)
        assert result == [], (
            f"retry_decision should be stale on status={status}"
        )


def test_filter_stale_drops_retry_decision_when_flow_moved_past_step() -> None:
    """Exemption is scoped to ``step_id == current_step_id``."""
    state = {
        "current_step_id": "s2",
        "steps": {
            "s1": {"step_type": "implement", "status": "failed"},
            "s2": {"step_type": "verify_spec", "status": "running"},
        },
    }
    calls = [
        _stale_call("rd1", kind=protocol.CALL_KIND_RETRY_DECISION, step_id="s1"),
    ]
    result = DaemonAggregator._filter_stale_calls(calls, state)
    assert result == []


def test_filter_stale_mixed_kinds_on_failed_step() -> None:
    """Mixed batch: retry_decision survives, plain call is filtered out."""
    state = _state_with_step_status("s1", "failed")
    calls = [
        _stale_call("rd1", kind=protocol.CALL_KIND_RETRY_DECISION),
        _stale_call("c1", kind=protocol.CALL_KIND_CALL),
    ]
    result = DaemonAggregator._filter_stale_calls(calls, state)
    assert [c.call_id for c in result] == ["rd1"]


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


# ---- all_project_roots: TTL cache for historical enumeration ---------------


def test_all_project_roots_caches_historical_enumeration(monkeypatch) -> None:
    """Within the TTL window, the disk history walk runs at most once."""
    import se3.daemon.aggregator as agg_mod

    calls: list = []

    def spy(base):
        calls.append(set(base))
        return ["/hist/root"]

    monkeypatch.setattr(agg_mod, "enumerate_historical_project_roots", spy)

    aggregator = DaemonAggregator()
    aggregator.set_project_roots(["/p/one"])

    first = aggregator.all_project_roots()
    second = aggregator.all_project_roots()
    third = aggregator.all_project_roots()

    # Only one disk enumeration despite three calls.
    assert len(calls) == 1
    # The cached historical root is still merged into every result.
    assert first == second == third
    assert "/hist/root" in first


def test_all_project_roots_active_root_visible_immediately(monkeypatch) -> None:
    """A newly added active root appears at once, not after the TTL."""
    import se3.daemon.aggregator as agg_mod

    monkeypatch.setattr(
        agg_mod, "enumerate_historical_project_roots", lambda base: []
    )

    aggregator = DaemonAggregator()
    aggregator.set_project_roots(["/p/one"])
    first = aggregator.all_project_roots()
    assert os.path.realpath("/p/one") in first

    # Adding a root mid-window must surface it immediately (cache invalidated).
    aggregator.add_project_root("/p/two")
    second = aggregator.all_project_roots()
    assert os.path.realpath("/p/two") in second


def test_readd_existing_root_keeps_history_cache_warm(monkeypatch) -> None:
    """Re-adding an already-tracked root must NOT bust the historical cache.

    The daemon poll loop re-adds every active flow's already-known root on
    every ~2s tick. If that idempotent re-add invalidated the cache, the full
    ``se3/history`` walk would re-run every tick — the exact high-frequency
    disk scan the cache exists to eliminate.
    """
    import se3.daemon.aggregator as agg_mod

    calls: list = []
    monkeypatch.setattr(
        agg_mod,
        "enumerate_historical_project_roots",
        lambda base: calls.append(set(base)) or [],
    )

    aggregator = DaemonAggregator()
    aggregator.add_project_root("/p/one")
    aggregator.all_project_roots()
    assert len(calls) == 1

    # Re-adding the same root (poll-loop rediscovery) must reuse the cache.
    aggregator.add_project_root("/p/one")
    aggregator.add_project_root("/p/one")
    aggregator.all_project_roots()
    assert len(calls) == 1  # cache stayed warm — no extra disk walk

    # But a genuinely new root still invalidates and re-enumerates immediately.
    aggregator.add_project_root("/p/two")
    aggregator.all_project_roots()
    assert len(calls) == 2
    assert os.path.realpath("/p/two") in calls[1]


def test_all_project_roots_reenumerates_after_ttl(monkeypatch) -> None:
    """Once the TTL elapses the disk history walk runs again."""
    import se3.daemon.aggregator as agg_mod

    calls: list = []
    monkeypatch.setattr(
        agg_mod,
        "enumerate_historical_project_roots",
        lambda base: calls.append(1) or [],
    )

    clock = {"now": 1000.0}
    monkeypatch.setattr(agg_mod.time, "monotonic", lambda: clock["now"])

    aggregator = DaemonAggregator()
    aggregator.set_project_roots(["/p/one"])

    aggregator.all_project_roots()
    aggregator.all_project_roots()
    assert len(calls) == 1  # still within TTL

    # Advance past the TTL -> a fresh enumeration.
    clock["now"] += agg_mod.HISTORICAL_ROOTS_TTL + 1
    aggregator.all_project_roots()
    assert len(calls) == 2


def test_all_project_roots_reenumerates_on_base_change(monkeypatch) -> None:
    """A changed base fingerprint forces re-enumeration even within the TTL."""
    import se3.daemon.aggregator as agg_mod

    calls: list = []
    monkeypatch.setattr(
        agg_mod,
        "enumerate_historical_project_roots",
        lambda base: calls.append(set(base)) or [],
    )

    aggregator = DaemonAggregator()
    aggregator.set_project_roots(["/p/one"])
    aggregator.all_project_roots()
    assert len(calls) == 1

    # set_project_roots invalidates the cache, so the next call re-enumerates
    # with the new base.
    aggregator.set_project_roots(["/p/one", "/p/two"])
    aggregator.all_project_roots()
    assert len(calls) == 2
    assert os.path.realpath("/p/two") in calls[1]
