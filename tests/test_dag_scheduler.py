"""Tests for the DAG scheduler module."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from se3.engine.dag_scheduler import DAGScheduler, GroupResult


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

    def test_unknown_dependency(self):
        groups = [{"group_id": "A", "depends_on": ["Z"]}]
        with pytest.raises(ValueError, match="unknown group"):
            DAGScheduler(groups)

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

def _make_result(group_id: str, delay: float = 0) -> GroupResult:
    """Helper: create a completed GroupResult, optionally sleeping."""
    if delay:
        time.sleep(delay)
    return GroupResult(
        group_id=group_id,
        status="completed",
        summary=f"done-{group_id}",
        branch_name=f"branch-{group_id}",
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

        def execute(group, deps_results):
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

        def execute(group, deps_results):
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

        def execute(group, deps_results):
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
        def execute(group, deps_results):
            gid = group["group_id"]
            if gid == "B":
                time.sleep(0.3)
            elif gid == "A":
                time.sleep(0.05)
            return _make_result(gid)

        start_times: dict[str, float] = {}
        orig_execute = execute

        def tracking_execute(group, deps_results):
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

        def execute(group, deps_results):
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

        def execute(group, deps_results):
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

        def execute(group, deps_results):
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

        def execute(group, deps_results):
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

        def execute(group, deps_results):
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

        def execute(group, deps_results):
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

        def execute(group, deps_results):
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

        def execute(group, deps_results):
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

        def execute(group, deps_results):
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

        def execute(group, deps_results):
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

        def execute(group, deps_results):
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

        def execute(group, deps_results):
            return _make_result(group["group_id"])

        results = DAGScheduler(groups).run(execute)
        assert len(results) == 1
        assert results[0].group_id == "X"
        assert results[0].status == "completed"

    def test_empty_groups_run(self):
        """Empty group list returns empty results."""
        results = DAGScheduler([]).run(lambda g, d: _make_result(g["group_id"]))
        assert results == []
