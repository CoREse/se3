"""Tests for the DAG scheduler module."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from se3.engine.dag_scheduler import (
    ConvergenceInfo,
    DAGScheduler,
    GroupResult,
    RelayContext,
    RelayPlan,
    _relay_plan_is_linear,
    classify_chains,
)


# ---------------------------------------------------------------------------
# GroupResult dataclass tests
# ---------------------------------------------------------------------------

class TestGroupResult:
    def test_defaults(self):
        r = GroupResult(group_id="G1", status="completed")
        assert r.group_id == "G1"
        assert r.status == "completed"
        assert r.files_changed == []
        assert r.tests_added == []
        assert r.test_mapping == {}
        assert r.summary == ""
        assert r.branch_name == ""
        assert r.worktree_path is None
        assert r.completion_status == "complete"
        assert r.incomplete_tasks == []
        assert r.restricted_edits == []
        assert r.error is None

    def test_skipped_factory(self):
        r = GroupResult.skipped("G2")
        assert r.group_id == "G2"
        assert r.status == "skipped"
        assert r.completion_status == "failed"
        assert r.error is not None
        assert "upstream" in r.error.lower()

    def test_failed_factory(self):
        r = GroupResult.failed("G3", "timeout")
        assert r.group_id == "G3"
        assert r.status == "failed"
        assert r.completion_status == "failed"
        assert r.error == "timeout"

    def test_mutable_defaults_not_shared(self):
        r1 = GroupResult(group_id="A", status="completed")
        r2 = GroupResult(group_id="B", status="completed")
        r1.files_changed.append("foo.py")
        assert r2.files_changed == []


# ---------------------------------------------------------------------------
# DAG construction tests
# ---------------------------------------------------------------------------

class TestDAGBuild:
    def test_linear_dependency(self):
        """A → B → C linear chain."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["B"]},
        ]
        s = DAGScheduler(groups)
        assert s._in_degree["A"] == 0
        assert s._in_degree["B"] == 1
        assert s._in_degree["C"] == 1
        assert "B" in s._adjacency["A"]
        assert "C" in s._adjacency["B"]
        assert s._topo_order == ["A", "B", "C"]

    def test_diamond_dependency(self):
        """A → {B, C} → D diamond."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["A"]},
            {"group_id": "D", "depends_on": ["B", "C"]},
        ]
        s = DAGScheduler(groups)
        assert s._in_degree["A"] == 0
        assert s._in_degree["B"] == 1
        assert s._in_degree["C"] == 1
        assert s._in_degree["D"] == 2
        # A must come before B, C; B and C before D
        order = s._topo_order
        assert order.index("A") < order.index("B")
        assert order.index("A") < order.index("C")
        assert order.index("B") < order.index("D")
        assert order.index("C") < order.index("D")

    def test_no_dependencies(self):
        """All groups independent — all in_degree 0."""
        groups = [
            {"group_id": "A"},
            {"group_id": "B", "depends_on": []},
            {"group_id": "C", "depends_on": None},
        ]
        s = DAGScheduler(groups)
        for gid in ["A", "B", "C"]:
            assert s._in_degree[gid] == 0

    def test_cycle_detection(self):
        groups = [
            {"group_id": "A", "depends_on": ["C"]},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["B"]},
        ]
        with pytest.raises(ValueError, match="[Cc]ycle"):
            DAGScheduler(groups)

    def test_self_cycle_detection(self):
        groups = [{"group_id": "A", "depends_on": ["A"]}]
        with pytest.raises(ValueError, match="[Cc]ycle"):
            DAGScheduler(groups)

    def test_unknown_dependency_is_skipped_with_warning(self, caplog):
        """A dangling depends_on edge is skipped (logged), not fatal.

        This is the DAG disaster-recovery defense: a completed/pre-merged
        group may have been dropped from the to-run set while a retained group
        still references it. Such edges are treated as already satisfied.
        """
        groups = [{"group_id": "A", "depends_on": ["Z"]}]
        with caplog.at_level(logging.WARNING):
            s = DAGScheduler(groups)
        # No exception raised; the dangling edge contributes no in-degree.
        assert s._in_degree["A"] == 0
        assert s._reverse_deps["A"] == []
        assert s._topo_order == ["A"]
        assert any("unknown group" in r.message for r in caplog.records)

    def test_duplicate_group_id(self):
        groups = [
            {"group_id": "A"},
            {"group_id": "A"},
        ]
        with pytest.raises(ValueError, match="[Dd]uplicate"):
            DAGScheduler(groups)

    def test_empty_groups(self):
        s = DAGScheduler([])
        assert s._topo_order == []

    def test_missing_group_id(self):
        with pytest.raises(ValueError, match="missing group_id"):
            DAGScheduler([{"depends_on": []}])


# ---------------------------------------------------------------------------
# Scheduling behavior tests
# ---------------------------------------------------------------------------

def _make_result(
    group_id: str, delay: float = 0,
    worktree_path: Path | None = None,
    branch_name: str | None = None,
) -> GroupResult:
    """Helper: create a completed GroupResult, optionally sleeping."""
    if delay:
        time.sleep(delay)
    return GroupResult(
        group_id=group_id,
        status="completed",
        summary=f"done-{group_id}",
        branch_name=branch_name if branch_name is not None else f"branch-{group_id}",
        worktree_path=worktree_path,
    )


class TestDAGRun:
    def test_no_deps_all_parallel(self):
        """Three independent groups should run concurrently."""
        groups = [
            {"group_id": "A"},
            {"group_id": "B"},
            {"group_id": "C"},
        ]
        thread_ids: dict[str, int] = {}
        start_times: dict[str, float] = {}
        barrier = threading.Barrier(3, timeout=5)

        def execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            thread_ids[gid] = threading.current_thread().ident
            start_times[gid] = time.monotonic()
            barrier.wait()  # all three must reach here concurrently
            return _make_result(gid)

        s = DAGScheduler(groups, max_workers=3)
        results = s.run(execute)

        assert len(results) == 3
        assert all(r.status == "completed" for r in results)
        # At least 2 different threads used (proves parallelism)
        assert len(set(thread_ids.values())) >= 2

    def test_linear_sequential(self):
        """A → B → C: must execute in strict order."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["B"]},
        ]
        call_order: list[str] = []
        lock = threading.Lock()

        def execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            with lock:
                call_order.append(gid)
            return _make_result(gid)

        s = DAGScheduler(groups, max_workers=4)
        results = s.run(execute)

        assert call_order == ["A", "B", "C"]
        assert [r.group_id for r in results] == ["A", "B", "C"]

    def test_diamond_ordering(self):
        """Diamond: D starts only after both B and C complete."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["A"]},
            {"group_id": "D", "depends_on": ["B", "C"]},
        ]
        completion_times: dict[str, float] = {}
        start_times: dict[str, float] = {}

        def execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            start_times[gid] = time.monotonic()
            time.sleep(0.05)
            completion_times[gid] = time.monotonic()
            return _make_result(gid)

        s = DAGScheduler(groups, max_workers=4)
        results = s.run(execute)

        # D must start after both B and C completed
        assert start_times["D"] >= completion_times["B"]
        assert start_times["D"] >= completion_times["C"]
        # A must complete before B and C start
        assert start_times["B"] >= completion_times["A"]
        assert start_times["C"] >= completion_times["A"]

    def test_immediate_trigger(self):
        """A and B are roots, C depends on A only. C should start as soon as
        A finishes without waiting for B."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": []},
            {"group_id": "C", "depends_on": ["A"]},
        ]

        # A finishes fast, B finishes slow
        def execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            if gid == "B":
                time.sleep(0.3)
            elif gid == "A":
                time.sleep(0.05)
            return _make_result(gid)

        start_times: dict[str, float] = {}
        orig_execute = execute

        def tracking_execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            start_times[gid] = time.monotonic()
            return orig_execute(group, deps_results)

        s = DAGScheduler(groups, max_workers=4)
        results = s.run(tracking_execute)

        # C should have started well before B finished
        assert all(r.status == "completed" for r in results)
        # C started at most ~0.15s after beginning (A took 0.05s),
        # while B takes 0.3s. So C should start before B ends.
        c_start = start_times["C"]
        a_start = start_times["A"]
        assert c_start - a_start < 0.2  # C starts soon after A, not waiting for B


# ---------------------------------------------------------------------------
# execute_fn parameter validation
# ---------------------------------------------------------------------------

class TestExecuteFnParams:
    def test_root_group_gets_empty_deps(self):
        """Root groups receive empty deps_results."""
        groups = [{"group_id": "A"}]
        received_deps = {}

        def execute(group, deps_results, relay_context=None):
            received_deps[group["group_id"]] = deps_results
            return _make_result(group["group_id"])

        DAGScheduler(groups).run(execute)
        assert received_deps["A"] == {}

    def test_dependent_group_gets_only_its_deps(self):
        """A group receives only the results of its direct dependencies."""
        groups = [
            {"group_id": "A"},
            {"group_id": "B"},
            {"group_id": "C", "depends_on": ["A"]},  # only depends on A, not B
        ]
        received_deps: dict[str, dict] = {}

        def execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            received_deps[gid] = dict(deps_results)
            return _make_result(gid)

        DAGScheduler(groups, max_workers=1).run(execute)
        # C should have A's result but not B's
        assert "A" in received_deps["C"]
        assert "B" not in received_deps["C"]

    def test_diamond_d_gets_both_b_and_c(self):
        """In a diamond, D receives results from both B and C."""
        groups = [
            {"group_id": "A"},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["A"]},
            {"group_id": "D", "depends_on": ["B", "C"]},
        ]
        received_deps: dict[str, set] = {}

        def execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            received_deps[gid] = set(deps_results.keys())
            return _make_result(gid)

        DAGScheduler(groups, max_workers=2).run(execute)
        assert received_deps["D"] == {"B", "C"}
        assert received_deps["B"] == {"A"}
        assert received_deps["C"] == {"A"}
        assert received_deps["A"] == set()


# ---------------------------------------------------------------------------
# Failure propagation tests
# ---------------------------------------------------------------------------

class TestFailurePropagation:
    def test_failure_skips_downstream(self):
        """A fails → B (depends on A) is skipped, C (independent) succeeds."""
        groups = [
            {"group_id": "A"},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C"},
        ]
        executed: list[str] = []

        def execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            executed.append(gid)
            if gid == "A":
                raise RuntimeError("A failed")
            return _make_result(gid)

        s = DAGScheduler(groups, max_workers=4)
        results = s.run(execute)

        result_map = {r.group_id: r for r in results}
        assert result_map["A"].status == "failed"
        assert result_map["B"].status == "skipped"
        assert result_map["C"].status == "completed"
        # B's execute_fn should never have been called
        assert "B" not in executed

    def test_multi_level_propagation(self):
        """A fails → B skipped → D skipped (D depends on B)."""
        groups = [
            {"group_id": "A"},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C"},
            {"group_id": "D", "depends_on": ["B"]},
        ]
        executed: list[str] = []

        def execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            executed.append(gid)
            if gid == "A":
                raise RuntimeError("A failed")
            return _make_result(gid)

        results = DAGScheduler(groups, max_workers=4).run(execute)
        result_map = {r.group_id: r for r in results}

        assert result_map["A"].status == "failed"
        assert result_map["B"].status == "skipped"
        assert result_map["D"].status == "skipped"
        assert result_map["C"].status == "completed"
        assert "B" not in executed
        assert "D" not in executed

    def test_returned_failed_status_propagates(self):
        """execute_fn returns GroupResult with status='failed' (no exception)."""
        groups = [
            {"group_id": "A"},
            {"group_id": "B", "depends_on": ["A"]},
        ]

        def execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            if gid == "A":
                return GroupResult.failed(gid, "internal error")
            return _make_result(gid)

        results = DAGScheduler(groups).run(execute)
        result_map = {r.group_id: r for r in results}
        assert result_map["A"].status == "failed"
        assert result_map["B"].status == "skipped"

    def test_partial_failure_unrelated_branch_unaffected(self):
        """In a diamond, if B fails, C (sibling) still runs, D is skipped."""
        groups = [
            {"group_id": "A"},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["A"]},
            {"group_id": "D", "depends_on": ["B", "C"]},
        ]

        def execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            if gid == "B":
                raise RuntimeError("B failed")
            return _make_result(gid)

        results = DAGScheduler(groups, max_workers=4).run(execute)
        result_map = {r.group_id: r for r in results}

        assert result_map["A"].status == "completed"
        assert result_map["B"].status == "failed"
        assert result_map["C"].status == "completed"
        # D depends on B (failed), so skipped
        assert result_map["D"].status == "skipped"


# ---------------------------------------------------------------------------
# Topological merge order tests
# ---------------------------------------------------------------------------

class TestTopologicalMergeOrder:
    def test_linear(self):
        groups = [
            {"group_id": "A"},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["B"]},
        ]
        s = DAGScheduler(groups)
        assert s.topological_merge_order() == ["A", "B", "C"]

    def test_diamond(self):
        groups = [
            {"group_id": "A"},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["A"]},
            {"group_id": "D", "depends_on": ["B", "C"]},
        ]
        s = DAGScheduler(groups)
        order = s.topological_merge_order()
        assert order.index("A") < order.index("B")
        assert order.index("A") < order.index("C")
        assert order.index("B") < order.index("D")
        assert order.index("C") < order.index("D")

    def test_empty(self):
        s = DAGScheduler([])
        assert s.topological_merge_order() == []

    def test_merge_order_contains_all_groups(self):
        """topological_merge_order() returns all group_ids; caller filters by status."""
        groups = [
            {"group_id": "A"},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C"},
        ]
        s = DAGScheduler(groups)
        order = s.topological_merge_order()
        assert set(order) == {"A", "B", "C"}
        assert order.index("A") < order.index("B")

    def test_merge_order_caller_filters_completed_only(self):
        """Demonstrate that the caller can filter merge order to completed groups."""
        groups = [
            {"group_id": "A"},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["B"]},
            {"group_id": "D"},
        ]

        def execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            if gid == "A":
                raise RuntimeError("fail")
            return _make_result(gid)

        s = DAGScheduler(groups, max_workers=4)
        results = s.run(execute)
        result_map = {r.group_id: r for r in results}

        # topological_merge_order returns all groups
        merge_order = s.topological_merge_order()
        assert len(merge_order) == 4

        # Caller filters to completed only (as implement.py does)
        completed_order = [
            gid for gid in merge_order if result_map[gid].status == "completed"
        ]
        assert "A" not in completed_order  # failed
        assert "B" not in completed_order  # skipped
        assert "C" not in completed_order  # skipped
        assert "D" in completed_order  # completed (independent)


# ---------------------------------------------------------------------------
# Results ordering
# ---------------------------------------------------------------------------

class TestResultsOrdering:
    def test_results_in_topo_order(self):
        """Results list follows topological order regardless of completion time."""
        groups = [
            {"group_id": "A"},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["B"]},
        ]

        def execute(group, deps_results, relay_context=None):
            return _make_result(group["group_id"])

        results = DAGScheduler(groups).run(execute)
        assert [r.group_id for r in results] == ["A", "B", "C"]

    def test_mixed_statuses_in_topo_order(self):
        """Results include failed/skipped groups in topo order."""
        groups = [
            {"group_id": "A"},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["B"]},
        ]

        def execute(group, deps_results, relay_context=None):
            if group["group_id"] == "A":
                raise RuntimeError("fail")
            return _make_result(group["group_id"])

        results = DAGScheduler(groups).run(execute)
        assert [r.group_id for r in results] == ["A", "B", "C"]
        assert results[0].status == "failed"
        assert results[1].status == "skipped"
        assert results[2].status == "skipped"

    def test_partial_failure_all_statuses_present(self):
        """Complex DAG with mix of completed, failed, and skipped — all in results."""
        #   A (root, ok)
        #   ├─ B (depends A, fails)
        #   │  └─ D (depends B, skipped)
        #   ├─ C (depends A, ok)
        #   │  └─ E (depends C, ok)
        #   └─ F (root, ok)
        groups = [
            {"group_id": "A"},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["A"]},
            {"group_id": "D", "depends_on": ["B"]},
            {"group_id": "E", "depends_on": ["C"]},
            {"group_id": "F"},
        ]

        def execute(group, deps_results, relay_context=None):
            gid = group["group_id"]
            if gid == "B":
                raise RuntimeError("B exploded")
            return _make_result(gid)

        results = DAGScheduler(groups, max_workers=4).run(execute)
        result_map = {r.group_id: r for r in results}

        # All 6 groups must be present
        assert len(results) == 6
        assert set(result_map.keys()) == {"A", "B", "C", "D", "E", "F"}

        # Verify statuses
        assert result_map["A"].status == "completed"
        assert result_map["B"].status == "failed"
        assert result_map["C"].status == "completed"
        assert result_map["D"].status == "skipped"
        assert result_map["E"].status == "completed"
        assert result_map["F"].status == "completed"

        # Verify error info
        assert result_map["B"].error is not None
        assert "B exploded" in result_map["B"].error
        assert result_map["D"].error is not None
        assert "upstream" in result_map["D"].error.lower()

    def test_single_group_run(self):
        """Single group with no deps runs and returns one result."""
        groups = [{"group_id": "X"}]

        def execute(group, deps_results, relay_context=None):
            return _make_result(group["group_id"])

        results = DAGScheduler(groups).run(execute)
        assert len(results) == 1
        assert results[0].group_id == "X"
        assert results[0].status == "completed"

    def test_empty_groups_run(self):
        """Empty group list returns empty results."""
        results = DAGScheduler([]).run(lambda g, d, rc=None: _make_result(g["group_id"]))
        assert results == []


# ---------------------------------------------------------------------------
# Relay dataclass tests
# ---------------------------------------------------------------------------


class TestRelayDataclasses:
    def test_convergence_info(self):
        ci = ConvergenceInfo(primary_predecessor="G2", secondary_predecessors=["G3"])
        assert ci.primary_predecessor == "G2"
        assert ci.secondary_predecessors == ["G3"]

    def test_relay_context_defaults(self):
        rc = RelayContext()
        assert rc.worktree_path is None
        assert rc.branch_name is None
        assert rc.is_fork is False
        assert rc.fork_source_branch is None
        assert rc.convergence_merges == []

    def test_relay_context_mutable_defaults_not_shared(self):
        rc1 = RelayContext()
        rc2 = RelayContext()
        rc1.convergence_merges.append("branch-X")
        assert rc2.convergence_merges == []

    def test_relay_plan_fields(self):
        plan = RelayPlan(
            relay_map={"G1": None},
            fork_from={},
            leaf_nodes={"G1"},
            convergence_points={},
            root_nodes={"G1"},
        )
        assert plan.relay_map == {"G1": None}
        assert plan.leaf_nodes == {"G1"}
        assert plan.root_nodes == {"G1"}


# ---------------------------------------------------------------------------
# classify_chains tests
# ---------------------------------------------------------------------------


class TestClassifyChains:
    """Tests for classify_chains() relay execution planner."""

    def test_empty_groups(self):
        """Empty input returns empty plan."""
        plan = classify_chains([])
        assert plan.relay_map == {}
        assert plan.fork_from == {}
        assert plan.leaf_nodes == set()
        assert plan.convergence_points == {}
        assert plan.root_nodes == set()

    def test_single_group(self):
        """Single group is both root and leaf."""
        groups = [{"group_id": "G1", "group_order": 1, "depends_on": []}]
        plan = classify_chains(groups)

        assert plan.relay_map == {"G1": None}
        assert plan.fork_from == {}
        assert plan.leaf_nodes == {"G1"}
        assert plan.root_nodes == {"G1"}
        assert plan.convergence_points == {}

    def test_linear_chain(self):
        """G1 → G2 → G3: all relay, only G3 is leaf."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G2"]},
        ]
        plan = classify_chains(groups)

        assert plan.relay_map == {"G1": None, "G2": "G1", "G3": "G2"}
        assert plan.fork_from == {}
        assert plan.leaf_nodes == {"G3"}
        assert plan.root_nodes == {"G1"}
        assert plan.convergence_points == {}

    def test_fork(self):
        """G1 → {G2, G3}: G2 relays (smaller order), G3 forks. Both are leaves."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1"]},
        ]
        plan = classify_chains(groups)

        # G2 relays from G1 (smaller group_order), G3 forks
        assert plan.relay_map["G1"] is None
        assert plan.relay_map["G2"] == "G1"
        assert plan.relay_map["G3"] is None
        assert plan.fork_from == {"G3": "G1"}
        assert plan.leaf_nodes == {"G2", "G3"}
        assert plan.root_nodes == {"G1"}
        assert plan.convergence_points == {}

    def test_diamond(self):
        """G1 → {G2, G3} → G4: G4 is convergence point with primary=G2."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1"]},
            {"group_id": "G4", "group_order": 4, "depends_on": ["G2", "G3"]},
        ]
        plan = classify_chains(groups)

        # G1: root
        assert plan.root_nodes == {"G1"}
        # G4: only leaf
        assert plan.leaf_nodes == {"G4"}

        # Relay: G1→G2 (relay), G3 forks from G1, G4 relays from G2
        assert plan.relay_map["G1"] is None
        assert plan.relay_map["G2"] == "G1"
        assert plan.relay_map["G3"] is None
        assert plan.relay_map["G4"] == "G2"

        # G3 forks from G1
        assert plan.fork_from == {"G3": "G1"}

        # G4 convergence: primary=G2, secondary=[G3]
        assert "G4" in plan.convergence_points
        ci = plan.convergence_points["G4"]
        assert ci.primary_predecessor == "G2"
        assert ci.secondary_predecessors == ["G3"]

    def test_two_independent_groups(self):
        """G1, G2 independent: two roots, two leaves, no relay/fork."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": []},
        ]
        plan = classify_chains(groups)

        assert plan.relay_map == {"G1": None, "G2": None}
        assert plan.fork_from == {}
        assert plan.leaf_nodes == {"G1", "G2"}
        assert plan.root_nodes == {"G1", "G2"}
        assert plan.convergence_points == {}

    def test_fork_three_children(self):
        """G1 → {G2, G3, G4}: G2 relays, G3 and G4 fork."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1"]},
            {"group_id": "G4", "group_order": 4, "depends_on": ["G1"]},
        ]
        plan = classify_chains(groups)

        assert plan.relay_map["G2"] == "G1"  # heir relays
        assert plan.relay_map["G3"] is None
        assert plan.relay_map["G4"] is None
        assert "G3" in plan.fork_from
        assert "G4" in plan.fork_from
        assert plan.fork_from["G3"] == "G1"
        assert plan.fork_from["G4"] == "G1"
        assert plan.leaf_nodes == {"G2", "G3", "G4"}

    def test_complex_mixed_dag(self):
        """Complex DAG: G1→G2, G1→G3, G2→G4, G3→G4, G4→G5.

        After structure:
        - G1: root
        - G2: relays from G1 (heir)
        - G3: forks from G1
        - G4: convergence (G2 primary, G3 secondary), relays from G2
        - G5: relays from G4, leaf
        """
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1"]},
            {"group_id": "G4", "group_order": 4, "depends_on": ["G2", "G3"]},
            {"group_id": "G5", "group_order": 5, "depends_on": ["G4"]},
        ]
        plan = classify_chains(groups)

        assert plan.root_nodes == {"G1"}
        assert plan.leaf_nodes == {"G5"}

        assert plan.relay_map["G1"] is None
        assert plan.relay_map["G2"] == "G1"
        assert plan.relay_map["G3"] is None
        assert plan.relay_map["G4"] == "G2"
        assert plan.relay_map["G5"] == "G4"

        assert plan.fork_from == {"G3": "G1"}

        assert "G4" in plan.convergence_points
        ci = plan.convergence_points["G4"]
        assert ci.primary_predecessor == "G2"
        assert ci.secondary_predecessors == ["G3"]

    def test_group_order_determines_heir(self):
        """When group_order is reversed, the heir changes.

        G1 → {G2(order=3), G3(order=2)}: G3 relays (smaller order), G2 forks.
        """
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 3, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 2, "depends_on": ["G1"]},
        ]
        plan = classify_chains(groups)

        assert plan.relay_map["G3"] == "G1"  # G3 has smaller order → relays
        assert plan.relay_map["G2"] is None  # G2 forks
        assert plan.fork_from == {"G2": "G1"}

    def test_missing_group_order_defaults(self):
        """Groups without group_order field default to 0 for ordering."""
        groups = [
            {"group_id": "G1", "depends_on": []},
            {"group_id": "G2", "depends_on": ["G1"]},
        ]
        plan = classify_chains(groups)

        # Should still work — both default to order 0
        assert plan.relay_map["G1"] is None
        assert plan.relay_map["G2"] == "G1"

    def test_convergence_primary_selected_by_group_order(self):
        """Convergence point picks the predecessor with smallest group_order as primary.

        G1(order=1) → G3, G2(order=2) → G3: primary=G1.
        """
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": []},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1", "G2"]},
        ]
        plan = classify_chains(groups)

        assert plan.root_nodes == {"G1", "G2"}
        assert plan.leaf_nodes == {"G3"}

        # G3 has predecessors G1 and G2; both have G3 as their only child (heir)
        # Both are relaying predecessors; primary = G1 (smaller order)
        assert "G3" in plan.convergence_points
        ci = plan.convergence_points["G3"]
        assert ci.primary_predecessor == "G1"
        assert ci.secondary_predecessors == ["G2"]
        assert plan.relay_map["G3"] == "G1"

    def test_parallel_chains(self):
        """Two independent chains: G1→G2 and G3→G4."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": []},
            {"group_id": "G4", "group_order": 4, "depends_on": ["G3"]},
        ]
        plan = classify_chains(groups)

        assert plan.root_nodes == {"G1", "G3"}
        assert plan.leaf_nodes == {"G2", "G4"}
        assert plan.relay_map == {"G1": None, "G2": "G1", "G3": None, "G4": "G3"}
        assert plan.fork_from == {}
        assert plan.convergence_points == {}


# ---------------------------------------------------------------------------
# _relay_plan_is_linear tests
# ---------------------------------------------------------------------------


class TestRelayPlanIsLinear:
    """Tests for _relay_plan_is_linear() linear-chain detector."""

    def test_linear_chain_returns_true(self):
        """A → B → C: single root, no forks → linear."""
        groups = [
            {"group_id": "A", "group_order": 1, "depends_on": []},
            {"group_id": "B", "group_order": 2, "depends_on": ["A"]},
            {"group_id": "C", "group_order": 3, "depends_on": ["B"]},
        ]
        assert _relay_plan_is_linear(classify_chains(groups)) is True

    def test_single_node_returns_true(self):
        """A single-node DAG is trivially linear."""
        groups = [{"group_id": "A", "group_order": 1, "depends_on": []}]
        assert _relay_plan_is_linear(classify_chains(groups)) is True

    def test_fork_returns_false(self):
        """A → B, A → C: fork present → not linear."""
        groups = [
            {"group_id": "A", "group_order": 1, "depends_on": []},
            {"group_id": "B", "group_order": 2, "depends_on": ["A"]},
            {"group_id": "C", "group_order": 3, "depends_on": ["A"]},
        ]
        assert _relay_plan_is_linear(classify_chains(groups)) is False

    def test_multiple_roots_returns_false(self):
        """Two independent roots → not linear even without forks."""
        groups = [
            {"group_id": "A", "group_order": 1, "depends_on": []},
            {"group_id": "B", "group_order": 2, "depends_on": []},
        ]
        assert _relay_plan_is_linear(classify_chains(groups)) is False

    def test_diamond_returns_false(self):
        """Diamond (convergence point) contains a fork → not linear."""
        groups = [
            {"group_id": "A", "group_order": 1, "depends_on": []},
            {"group_id": "B", "group_order": 2, "depends_on": ["A"]},
            {"group_id": "C", "group_order": 3, "depends_on": ["A"]},
            {"group_id": "D", "group_order": 4, "depends_on": ["B", "C"]},
        ]
        assert _relay_plan_is_linear(classify_chains(groups)) is False

    def test_empty_plan_returns_false(self):
        """Empty plan has zero roots → not linear."""
        assert _relay_plan_is_linear(classify_chains([])) is False


# ---------------------------------------------------------------------------
# DAGScheduler relay_plan integration tests
# ---------------------------------------------------------------------------


class TestDAGSchedulerRelayPlan:
    """Tests for DAGScheduler with relay_plan integration."""

    def test_relay_plan_none_gives_default_context(self):
        """When relay_plan is None, execute_fn receives a default RelayContext."""
        groups = [{"group_id": "A"}]
        received_contexts: dict[str, RelayContext] = {}

        def execute(group, deps_results, relay_context):
            gid = group["group_id"]
            received_contexts[gid] = relay_context
            return _make_result(gid)

        DAGScheduler(groups, relay_plan=None).run(execute)

        ctx = received_contexts["A"]
        assert ctx.worktree_path is None
        assert ctx.branch_name is None
        assert ctx.is_fork is False
        assert ctx.fork_source_branch is None
        assert ctx.convergence_merges == []

    def test_root_node_gets_empty_relay_context(self):
        """Root node with relay_plan gets worktree_path=None (must create new)."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
        ]
        plan = classify_chains(groups)
        received: dict[str, RelayContext] = {}

        def execute(group, deps_results, relay_context):
            gid = group["group_id"]
            received[gid] = relay_context
            return _make_result(gid, worktree_path=Path(f"/wt/{gid}"), branch_name=f"br-{gid}")

        DAGScheduler(groups, relay_plan=plan).run(execute)

        # G1 is root → no predecessor worktree
        assert received["G1"].worktree_path is None
        assert received["G1"].is_fork is False

    def test_relay_node_gets_predecessor_worktree(self):
        """Relay node receives predecessor's worktree_path and branch_name."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
        ]
        plan = classify_chains(groups)
        received: dict[str, RelayContext] = {}

        def execute(group, deps_results, relay_context):
            gid = group["group_id"]
            received[gid] = relay_context
            return _make_result(gid, worktree_path=Path("/wt/G1"), branch_name="br-G1")

        DAGScheduler(groups, relay_plan=plan).run(execute)

        # G2 relays from G1 → gets G1's worktree
        assert received["G2"].worktree_path == Path("/wt/G1")
        assert received["G2"].branch_name == "br-G1"
        assert received["G2"].is_fork is False

    def test_fork_node_gets_fork_context(self):
        """Fork node gets is_fork=True with fork_source_branch from predecessor."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1"]},
        ]
        plan = classify_chains(groups)
        received: dict[str, RelayContext] = {}

        def execute(group, deps_results, relay_context):
            gid = group["group_id"]
            received[gid] = relay_context
            return _make_result(gid, worktree_path=Path(f"/wt/{gid}"), branch_name="br-G1")

        DAGScheduler(groups, relay_plan=plan).run(execute)

        # G2 relays (heir), G3 forks
        assert received["G2"].worktree_path == Path("/wt/G1")
        assert received["G2"].is_fork is False

        assert received["G3"].is_fork is True
        assert received["G3"].fork_source_branch == "br-G1"
        assert received["G3"].worktree_path is None

    def test_convergence_node_gets_merge_list(self):
        """Convergence node receives secondary predecessor branches in convergence_merges."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1"]},
            {"group_id": "G4", "group_order": 4, "depends_on": ["G2", "G3"]},
        ]
        plan = classify_chains(groups)
        received: dict[str, RelayContext] = {}

        def execute(group, deps_results, relay_context):
            gid = group["group_id"]
            received[gid] = relay_context
            return _make_result(gid, worktree_path=Path(f"/wt/{gid}"), branch_name=f"br-{gid}")

        DAGScheduler(groups, relay_plan=plan).run(execute)

        # G4 is convergence: primary=G2, secondary=[G3]
        ctx = received["G4"]
        assert ctx.worktree_path == Path("/wt/G2")
        assert ctx.branch_name == "br-G2"
        assert ctx.convergence_merges == ["br-G3"]
        assert ctx.is_fork is False

    def test_linear_chain_relay_propagation(self):
        """G1→G2→G3: relay context propagates through the chain."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G2"]},
        ]
        plan = classify_chains(groups)
        received: dict[str, RelayContext] = {}

        def execute(group, deps_results, relay_context):
            gid = group["group_id"]
            received[gid] = relay_context
            # All groups share the same worktree/branch in a linear relay
            return _make_result(gid, worktree_path=Path("/wt/shared"), branch_name="br-shared")

        DAGScheduler(groups, relay_plan=plan).run(execute)

        assert received["G1"].worktree_path is None  # root
        assert received["G2"].worktree_path == Path("/wt/shared")
        assert received["G2"].branch_name == "br-shared"
        assert received["G3"].worktree_path == Path("/wt/shared")
        assert received["G3"].branch_name == "br-shared"

    def test_backward_compatible_without_relay_plan(self):
        """DAGScheduler without relay_plan still works with 3-arg execute_fn."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
        ]
        call_order: list[str] = []

        def execute(group, deps_results, relay_context):
            gid = group["group_id"]
            call_order.append(gid)
            assert relay_context.worktree_path is None  # default context
            return _make_result(gid)

        results = DAGScheduler(groups).run(execute)
        assert call_order == ["A", "B"]
        assert all(r.status == "completed" for r in results)


# ---------------------------------------------------------------------------
# get_fallback_leaves tests
# ---------------------------------------------------------------------------


class TestGetFallbackLeaves:
    """Tests for DAGScheduler.get_fallback_leaves()."""

    def test_all_succeed_no_fallback(self):
        """When all groups complete, no fallback leaves are needed."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G2"]},
        ]
        plan = classify_chains(groups)

        def execute(group, deps_results, relay_context):
            return _make_result(group["group_id"])

        s = DAGScheduler(groups, relay_plan=plan)
        s.run(execute)
        assert s.get_fallback_leaves() == []

    def test_midchain_failure_creates_fallback_leaf(self):
        """G1→G2→G3, G2 fails: G1 becomes a fallback leaf."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G2"]},
        ]
        plan = classify_chains(groups)

        def execute(group, deps_results, relay_context):
            gid = group["group_id"]
            if gid == "G2":
                raise RuntimeError("G2 failed")
            return _make_result(gid)

        s = DAGScheduler(groups, relay_plan=plan)
        s.run(execute)

        fallback = s.get_fallback_leaves()
        assert fallback == ["G1"]

    def test_leaf_failure_no_fallback(self):
        """G1→G2, G2 (leaf) fails: G1 becomes fallback leaf."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
        ]
        plan = classify_chains(groups)

        def execute(group, deps_results, relay_context):
            gid = group["group_id"]
            if gid == "G2":
                raise RuntimeError("G2 failed")
            return _make_result(gid)

        s = DAGScheduler(groups, relay_plan=plan)
        s.run(execute)

        # G2 is the leaf but it failed; G1 completed but all downstream failed
        fallback = s.get_fallback_leaves()
        assert fallback == ["G1"]

    def test_fork_partial_failure(self):
        """G1→{G2,G3}, G2 completes, G3 fails: no fallback (G2 is leaf, carries G1's work)."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1"]},
        ]
        plan = classify_chains(groups)

        def execute(group, deps_results, relay_context):
            gid = group["group_id"]
            if gid == "G3":
                raise RuntimeError("G3 failed")
            return _make_result(gid)

        s = DAGScheduler(groups, relay_plan=plan)
        s.run(execute)

        # G2 is a leaf that completed, G3 is a leaf that failed
        # G1 has G2 as completed downstream → NOT a fallback leaf
        assert s.get_fallback_leaves() == []

    def test_fork_all_downstream_fail(self):
        """G1→{G2,G3}, both G2 and G3 fail: G1 becomes fallback leaf."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1"]},
        ]
        plan = classify_chains(groups)

        def execute(group, deps_results, relay_context):
            gid = group["group_id"]
            if gid in ("G2", "G3"):
                raise RuntimeError(f"{gid} failed")
            return _make_result(gid)

        s = DAGScheduler(groups, relay_plan=plan)
        s.run(execute)

        assert s.get_fallback_leaves() == ["G1"]

    def test_diamond_convergence_failure(self):
        """G1→{G2,G3}→G4: G4 fails but G2 and G3 completed.

        G2 is not a normal leaf (G4 is). G3 is not a normal leaf either.
        Both G2 and G3 have G4 as their only downstream and G4 failed,
        so both become fallback leaves.
        """
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1"]},
            {"group_id": "G4", "group_order": 4, "depends_on": ["G2", "G3"]},
        ]
        plan = classify_chains(groups)

        def execute(group, deps_results, relay_context):
            gid = group["group_id"]
            if gid == "G4":
                raise RuntimeError("G4 failed")
            return _make_result(gid)

        s = DAGScheduler(groups, relay_plan=plan)
        s.run(execute)

        fallback = s.get_fallback_leaves()
        # G2 and G3 are not leaf_nodes (G4 is), and their only downstream (G4) failed
        assert sorted(fallback) == ["G2", "G3"]

    def test_furthest_completed_is_fallback(self):
        """G1→G2→G3, G3 fails: G2 is fallback leaf (not G1, because G1 has G2 as completed downstream)."""
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G2"]},
        ]
        plan = classify_chains(groups)

        def execute(group, deps_results, relay_context):
            gid = group["group_id"]
            if gid == "G3":
                raise RuntimeError("G3 failed")
            return _make_result(gid)

        s = DAGScheduler(groups, relay_plan=plan)
        s.run(execute)

        # G1 has completed downstream G2, so G1 is NOT a fallback leaf
        # G2 has all downstream (G3) failed, and is NOT a normal leaf → fallback leaf
        assert s.get_fallback_leaves() == ["G2"]

    def test_no_relay_plan_fallback_leaves(self):
        """Without relay_plan, get_fallback_leaves still works (all nodes are potential fallbacks)."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
        ]

        def execute(group, deps_results, relay_context):
            gid = group["group_id"]
            if gid == "B":
                raise RuntimeError("B failed")
            return _make_result(gid)

        s = DAGScheduler(groups)
        s.run(execute)

        # A completed, B failed; no relay_plan → leaf_nodes is empty set
        # A has downstream B which failed → fallback leaf
        assert s.get_fallback_leaves() == ["A"]

    def test_before_run_returns_empty(self):
        """get_fallback_leaves before run() returns empty."""
        groups = [{"group_id": "A"}]
        s = DAGScheduler(groups)
        assert s.get_fallback_leaves() == []
