"""Integration tests for the DAG parallel execution pipeline.

Validates the complete new DAG execution engine including LOC threshold
routing, transitive reduction, relay chain worktree reuse, fork/convergence
handling, mid-chain failure recovery, and LLM-based conflict resolution
(no --theirs fallback).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from tianluo.engine.dag_scheduler import (
    ConvergenceInfo,
    GroupResult,
    RelayContext,
    RelayPlan,
)
from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.steps.implement import _run_dag_parallel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_step(**kwargs):
    """Create a Step with sensible defaults for implement tests."""
    defaults = {
        "step_type": StepType.IMPLEMENT,
        "step_id": "test-impl",
        "inputs": {
            "task_description": "Test task",
            "task_type": "feature",
            "task_groups": [],
            "spec_content": {},
        },
    }
    defaults.update(kwargs)
    return Step(**defaults)


def _make_flow(tmp_path=None, **kwargs):
    """Create a FlowInstance with sensible defaults."""
    defaults = {
        "task_description": "Test task",
        "flow_id": "test-flow",
    }
    if tmp_path:
        defaults["change_path"] = tmp_path / "tianluo"
    defaults.update(kwargs)
    return FlowInstance(**defaults)


def _make_groups(specs):
    """Build group dicts from compact specs.

    Args:
        specs: list of (group_id, group_order, depends_on, estimated_loc) tuples.
    """
    groups = []
    for gid, order, deps, loc in specs:
        groups.append({
            "group_id": gid,
            "group_order": order,
            "depends_on": deps,
            "tasks": [{"id": f"task-{gid}", "estimated_loc": loc}],
        })
    return groups


# Common patch paths
_IMP = "tianluo.engine.steps.implement"


# ---------------------------------------------------------------------------
# Test A: LOC threshold routing
# ---------------------------------------------------------------------------


class TestLocThresholdRouting:
    """Total estimated_loc determines single-call vs DAG path."""

    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch(f"{_IMP}._run_single_llm_call")
    @patch(f"{_IMP}._run_dag_parallel")
    def test_below_threshold_routes_to_single_call(
        self, mock_dag, mock_single, mock_inj, mock_commits, tmp_path,
    ):
        """total_loc ≤ 300 → single LLM call, not DAG."""
        from tianluo.engine.steps.implement import implement_handler

        mock_single.return_value = StepStatus.COMPLETED

        groups = _make_groups([
            ("G1", 1, [], 100),
            ("G2", 2, ["G1"], 100),
        ])
        step = _make_step(inputs={
            "task_description": "Test",
            "task_type": "feature",
            "task_groups": groups,
            "spec_content": {},
            # The LOC gate is granular / legacy scheduling by definition: only
            # that doctrine emits the per-task estimated_loc it reads. Coarse
            # capability groups carry none, so they bypass it.
            "plan_decomposition": "granular",
        })
        flow = _make_flow(tmp_path)

        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_single.assert_called_once()
        mock_dag.assert_not_called()

    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch(f"{_IMP}._run_dag_parallel")
    def test_above_threshold_routes_to_dag(
        self, mock_dag, mock_inj, mock_commits, tmp_path,
    ):
        """total_loc > 300 → DAG parallel path."""
        from tianluo.engine.steps.implement import implement_handler

        mock_dag.return_value = StepStatus.COMPLETED

        # Fork DAG (G1 → G2, G1 → G3) so the linear-chain short-circuit
        # does not apply; linear chains fall through to sequential.
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
            ("G3", 3, ["G1"], 200),
        ])
        step = _make_step(inputs={
            "task_description": "Test",
            "task_type": "feature",
            "task_groups": groups,
            "spec_content": {},
        })
        flow = _make_flow(tmp_path)

        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_called_once()


# ---------------------------------------------------------------------------
# Test B: Transitive reduction integration
# ---------------------------------------------------------------------------


class TestTransitiveReductionIntegration:
    """Verify reduced DAG is passed to classify_chains and DAGScheduler."""

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_reduced_dag_reaches_scheduler(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        """transitive_reduce output feeds into classify_chains then DAGScheduler."""
        from tianluo.engine.steps.implement import _run_dag_parallel

        # G3 depends_on [G1, G2], G2 depends_on [G1] → G3→G1 is redundant
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
            ("G3", 3, ["G1", "G2"], 200),
        ])
        reduced = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
            ("G3", 3, ["G2"], 200),  # G1 removed
        ])
        mock_reduce.return_value = reduced
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1", "G3": "G2"},
            fork_from={},
            leaf_nodes={"G3"},
            convergence_points={},
            root_nodes={"G1"},
        )

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed", branch_name="impl/f/G1",
                        worktree_path=Path("/wt")),
            GroupResult(group_id="G2", status="completed", branch_name="impl/f/G1",
                        worktree_path=Path("/wt")),
            GroupResult(group_id="G3", status="completed", branch_name="impl/f/G1",
                        worktree_path=Path("/wt")),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        # transitive_reduce called with original groups
        mock_reduce.assert_called_once_with(groups)
        # classify_chains called with reduced output
        mock_classify.assert_called_once_with(reduced)
        # DAGScheduler created with reduced groups and relay_plan
        mock_sched_cls.assert_called_once()
        sched_args = mock_sched_cls.call_args
        assert sched_args[0][0] is reduced  # first positional arg
        assert sched_args[1].get("relay_plan") is mock_classify.return_value


# ---------------------------------------------------------------------------
# Test C: Linear relay chain — single worktree reused, 1 merge
# ---------------------------------------------------------------------------


class TestLinearRelayChain:
    """G1→G2→G3 linear chain: all share one worktree, one final merge."""

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_single_worktree_reuse_one_merge(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
            ("G3", 3, ["G2"], 200),
        ])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1", "G3": "G2"},
            fork_from={},
            leaf_nodes={"G3"},
            convergence_points={},
            root_nodes={"G1"},
        )

        wt = Path("/wt-A")
        branch = "impl/test-flow/G1"
        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name=branch, worktree_path=wt),
            GroupResult(group_id="G2", status="completed",
                        branch_name=branch, worktree_path=wt),
            GroupResult(group_id="G3", status="completed",
                        branch_name=branch, worktree_path=wt),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        result = _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        assert result == StepStatus.COMPLETED
        # Only 1 merge (leaf G3 — but all share the same branch, so 1 merge)
        assert mock_merge.call_count == 1
        merge_args = mock_merge.call_args
        assert merge_args[0][1] == branch  # branch to merge

        # Worktree cleanup should be deduplicated (1 unique branch)
        assert mock_cleanup.call_count == 1

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_relay_context_passes_worktree(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        """Verify DAGScheduler.run receives execute_fn and relay contexts are built."""
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
        ])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1"},
            fork_from={},
            leaf_nodes={"G2"},
            convergence_points={},
            root_nodes={"G1"},
        )

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name="impl/f/G1", worktree_path=Path("/wt")),
            GroupResult(group_id="G2", status="completed",
                        branch_name="impl/f/G1", worktree_path=Path("/wt")),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        # DAGScheduler.run must be called with execute_fn
        mock_sched.run.assert_called_once()
        execute_fn = mock_sched.run.call_args[0][0]
        assert callable(execute_fn)


# ---------------------------------------------------------------------------
# Test D: Fork relay G1→{G2,G3} — fork_worktree for second, 2 leaf merges
# ---------------------------------------------------------------------------


class TestForkRelay:
    """G1→{G2,G3} fork: G2 inherits G1, G3 forks, 2 leaf merges."""

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_fork_produces_two_leaf_merges(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
            ("G3", 3, ["G1"], 200),
        ])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1", "G3": None},
            fork_from={"G3": "G1"},
            leaf_nodes={"G2", "G3"},
            convergence_points={},
            root_nodes={"G1"},
        )

        wt_a = Path("/wt-A")
        wt_b = Path("/wt-B")
        branch_a = "impl/f/G1"
        branch_b = "impl/f/G3"
        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name=branch_a, worktree_path=wt_a),
            GroupResult(group_id="G2", status="completed",
                        branch_name=branch_a, worktree_path=wt_a),
            GroupResult(group_id="G3", status="completed",
                        branch_name=branch_b, worktree_path=wt_b),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        result = _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        assert result == StepStatus.COMPLETED
        # 2 leaf merges (G2 on branch_a, G3 on branch_b — different branches)
        assert mock_merge.call_count == 2
        merged_branches = {c[0][1] for c in mock_merge.call_args_list}
        assert merged_branches == {branch_a, branch_b}

        # 2 worktree cleanups (2 unique branches)
        assert mock_cleanup.call_count == 2

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_fork_from_in_relay_plan(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        """Fork group appears in relay_plan.fork_from."""
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
            ("G3", 3, ["G1"], 200),
        ])
        mock_reduce.return_value = groups
        plan = RelayPlan(
            relay_map={"G1": None, "G2": "G1", "G3": None},
            fork_from={"G3": "G1"},
            leaf_nodes={"G2", "G3"},
            convergence_points={},
            root_nodes={"G1"},
        )
        mock_classify.return_value = plan

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name="b1", worktree_path=Path("/wt")),
            GroupResult(group_id="G2", status="completed",
                        branch_name="b1", worktree_path=Path("/wt")),
            GroupResult(group_id="G3", status="completed",
                        branch_name="b3", worktree_path=Path("/wt2")),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        # DAGScheduler received relay_plan with fork_from
        sched_kwargs = mock_sched_cls.call_args[1]
        assert "G3" in sched_kwargs["relay_plan"].fork_from


# ---------------------------------------------------------------------------
# Test E: Diamond DAG G1→{G2,G3}→G4 — convergence merge, single leaf
# ---------------------------------------------------------------------------


class TestDiamondConvergence:
    """G1→{G2,G3}→G4: G4 is convergence point, only G4 merges back."""

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_diamond_single_leaf_merge(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        """Diamond DAG produces a single leaf merge (G4)."""
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
            ("G3", 3, ["G1"], 200),
            ("G4", 4, ["G2", "G3"], 200),
        ])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1", "G3": None, "G4": "G2"},
            fork_from={"G3": "G1"},
            leaf_nodes={"G4"},
            convergence_points={
                "G4": ConvergenceInfo(
                    primary_predecessor="G2",
                    secondary_predecessors=["G3"],
                ),
            },
            root_nodes={"G1"},
        )

        branch_a = "impl/f/G1"  # G1→G2→G4 relay chain
        branch_b = "impl/f/G3"  # G3 forked
        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name=branch_a, worktree_path=Path("/wt-A")),
            GroupResult(group_id="G2", status="completed",
                        branch_name=branch_a, worktree_path=Path("/wt-A")),
            GroupResult(group_id="G3", status="completed",
                        branch_name=branch_b, worktree_path=Path("/wt-B")),
            GroupResult(group_id="G4", status="completed",
                        branch_name=branch_a, worktree_path=Path("/wt-A")),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        result = _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        assert result == StepStatus.COMPLETED
        # Only 1 merge (G4 is the only leaf, on branch_a)
        assert mock_merge.call_count == 1
        assert mock_merge.call_args[0][1] == branch_a

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_convergence_info_in_relay_plan(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        """Convergence point G4 has correct primary/secondary predecessors."""
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
            ("G3", 3, ["G1"], 200),
            ("G4", 4, ["G2", "G3"], 200),
        ])
        mock_reduce.return_value = groups
        conv_info = ConvergenceInfo(
            primary_predecessor="G2",
            secondary_predecessors=["G3"],
        )
        plan = RelayPlan(
            relay_map={"G1": None, "G2": "G1", "G3": None, "G4": "G2"},
            fork_from={"G3": "G1"},
            leaf_nodes={"G4"},
            convergence_points={"G4": conv_info},
            root_nodes={"G1"},
        )
        mock_classify.return_value = plan

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name="b1", worktree_path=Path("/wt")),
            GroupResult(group_id="G2", status="completed",
                        branch_name="b1", worktree_path=Path("/wt")),
            GroupResult(group_id="G3", status="completed",
                        branch_name="b3", worktree_path=Path("/wt2")),
            GroupResult(group_id="G4", status="completed",
                        branch_name="b1", worktree_path=Path("/wt")),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        # Verify relay_plan passed to scheduler has convergence info
        sched_kwargs = mock_sched_cls.call_args[1]
        passed_plan = sched_kwargs["relay_plan"]
        assert "G4" in passed_plan.convergence_points
        assert passed_plan.convergence_points["G4"].primary_predecessor == "G2"
        assert passed_plan.convergence_points["G4"].secondary_predecessors == ["G3"]


# ---------------------------------------------------------------------------
# Test F: Mid-chain failure — fallback leaf identification and merge
# ---------------------------------------------------------------------------


class TestMidChainFailure:
    """G1→G2→G3 with G2 failing: G1 becomes fallback leaf."""

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_fallback_leaf_merged_on_midchain_failure(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
            ("G3", 3, ["G2"], 200),
        ])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1", "G3": "G2"},
            fork_from={},
            leaf_nodes={"G3"},
            convergence_points={},
            root_nodes={"G1"},
        )

        branch = "impl/f/G1"
        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name=branch, worktree_path=Path("/wt")),
            GroupResult(group_id="G2", status="failed",
                        error="LLM error", worktree_path=Path("/wt")),
            GroupResult.skipped("G3"),
        ]
        # G1 completed but G2 (its downstream) failed → G1 is fallback leaf
        mock_sched.get_fallback_leaves.return_value = ["G1"]
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        result = _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        # G1 merged as fallback leaf
        assert mock_merge.call_count == 1
        assert branch in str(mock_merge.call_args)

        # Status is partial (some work preserved)
        assert result == StepStatus.PARTIAL
        assert step.outputs["completion_status"] == "partial"

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_skipped_groups_recorded_in_incomplete_tasks(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
            ("G3", 3, ["G2"], 200),
        ])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1", "G3": "G2"},
            fork_from={},
            leaf_nodes={"G3"},
            convergence_points={},
            root_nodes={"G1"},
        )

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name="b1", worktree_path=Path("/wt")),
            GroupResult(group_id="G2", status="failed",
                        error="LLM timeout"),
            GroupResult.skipped("G3"),
        ]
        mock_sched.get_fallback_leaves.return_value = ["G1"]
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        incomplete = step.outputs["incomplete_tasks"]
        # Should mention both failed G2 and skipped G3
        assert any("G2" in t for t in incomplete)
        assert any("G3" in t and "skipped" in t.lower() for t in incomplete)

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_all_groups_fail_returns_failed(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        """When all groups fail and no fallback leaves, status is FAILED."""
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
        ])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1"},
            fork_from={},
            leaf_nodes={"G2"},
            convergence_points={},
            root_nodes={"G1"},
        )

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="failed", error="error1"),
            GroupResult.skipped("G2"),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        result = _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        assert result == StepStatus.FAILED
        assert step.outputs["completion_status"] == "failed"
        mock_merge.assert_not_called()


# ---------------------------------------------------------------------------
# Test G: Conflict resolution — LLM resolves, no --theirs fallback
# ---------------------------------------------------------------------------


class TestConflictResolution:
    """Leaf merge uses LLM conflict resolution + take-theirs fallback."""

    @patch(f"{_IMP}._is_branch_reachable_from", return_value=True)
    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    @patch(f"{_IMP}.resolve_merge_conflicts_with_context")
    @patch(f"{_IMP}.get_conflicting_files")
    @patch(f"{_IMP}._run_git")
    def test_conflict_resolved_by_llm(
        self, mock_git, mock_conflict_files, mock_resolve,
        mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_del,
        mock_reachable,
    ):
        """Merge conflict resolved by LLM returns COMPLETED."""
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
        ])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1"},
            fork_from={},
            leaf_nodes={"G2"},
            convergence_points={},
            root_nodes={"G1"},
        )

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name="impl/f/G1", worktree_path=Path("/wt")),
            GroupResult(group_id="G2", status="completed",
                        branch_name="impl/f/G1", worktree_path=Path("/wt")),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        # Sequence: stash push (no local changes), merge → conflict. LLM
        # resolves; no take-theirs needed; no pop (no stash).
        no_stash = MagicMock(
            returncode=0, stdout="No local changes to save", stderr="",
        )
        ref_ok = MagicMock(returncode=0, stdout="abc123", stderr="")
        conflict_merge = MagicMock(
            returncode=1, stdout="CONFLICT in file.py", stderr="",
        )
        mock_git.side_effect = [no_stash, ref_ok, conflict_merge]

        mock_conflict_files.return_value = ["file.py"]
        mock_resolve.return_value = True  # LLM resolves it

        step = _make_step()
        flow = _make_flow()

        # Use the real _merge_leaf_branch (not mocked)
        result = _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        assert result == StepStatus.COMPLETED
        mock_resolve.assert_called_once()
        # Verify it was called with task description and group summaries
        call_args = mock_resolve.call_args
        assert call_args[0][0] == Path("/repo")  # project_root
        assert "file.py" in call_args[0][1]  # conflict_files

    @patch(f"{_IMP}._is_branch_reachable_from", return_value=True)
    @patch(f"{_IMP}._record_take_theirs_event")
    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    @patch(f"{_IMP}.resolve_merge_conflicts_with_context")
    @patch(f"{_IMP}.get_conflicting_files")
    @patch(f"{_IMP}._run_git")
    def test_conflict_unresolved_falls_back_to_take_theirs(
        self, mock_git, mock_conflict_files, mock_resolve,
        mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_del,
        mock_audit, mock_reachable,
    ):
        """LLM exhausted → take-theirs fallback completes the merge.

        With the merge-robustness changes, an LLM that cannot resolve no
        longer surfaces as a merge_failure. Instead, ``_take_theirs_fallback``
        deterministically accepts the leaf branch's version for every
        conflict file and commits, surfacing an audit issue. Merge succeeds
        end-to-end; merge_failures stays empty.
        """
        groups = _make_groups([
            ("G1", 1, [], 200),
        ])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None},
            fork_from={},
            leaf_nodes={"G1"},
            convergence_points={},
            root_nodes={"G1"},
        )

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name="impl/f/G1", worktree_path=Path("/wt")),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        # Sequence: stash push (no-op), merge (conflict), take-theirs
        # (checkout --theirs, add, commit).
        no_stash = MagicMock(
            returncode=0, stdout="No local changes", stderr="",
        )
        ref_ok = MagicMock(returncode=0, stdout="abc123", stderr="")
        conflict_merge = MagicMock(
            returncode=1, stdout="CONFLICT in file.py", stderr="",
        )
        ok = MagicMock(returncode=0, stdout="", stderr="")
        mock_git.side_effect = [no_stash, ref_ok, conflict_merge, ok, ok, ok]

        mock_conflict_files.return_value = ["file.py"]
        mock_resolve.return_value = False  # LLM cannot resolve

        step = _make_step()
        flow = _make_flow()

        result = _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        # take-theirs fallback succeeded → no merge_failures
        assert step.outputs.get("merge_failures", []) == []
        mock_audit.assert_called_once()

    def test_take_theirs_fallback_helpers_present(self):
        """The merge-robustness fallback helpers ARE present.

        Replaces the old contract that asserted no --theirs path existed.
        Since 4.11.0, `_take_theirs_fallback` is the deterministic safety
        net invoked when LLM conflict resolution exhausts retries; it
        preserves the leaf branch's commits by accepting their version
        of every conflicting file. The legacy `_force_resolve_conflicts_theirs`
        (an indiscriminate --theirs that ran instead of LLM resolution) is
        still gone.
        """
        import tianluo.engine.steps.implement as impl_module

        assert hasattr(impl_module, "_take_theirs_fallback"), (
            "_take_theirs_fallback must exist as the merge-robustness "
            "deterministic fallback"
        )
        assert hasattr(impl_module, "_take_ours_for_stashpop"), (
            "_take_ours_for_stashpop must exist for stash-pop conflict cleanup"
        )
        # Legacy unguarded --theirs path stays removed
        assert not hasattr(impl_module, "_force_resolve_conflicts_theirs"), (
            "_force_resolve_conflicts_theirs should not return"
        )


# ---------------------------------------------------------------------------
# Test: Output aggregation
# ---------------------------------------------------------------------------


class TestOutputAggregation:
    """Verify outputs are correctly aggregated from all group results."""

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_files_changed_aggregated(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
        ])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1"},
            fork_from={},
            leaf_nodes={"G2"},
            convergence_points={},
            root_nodes={"G1"},
        )

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(
                group_id="G1", status="completed",
                branch_name="b", worktree_path=Path("/wt"),
                files_changed=["a.py", "b.py"],
                tests_added=["test_a.py"],
                test_mapping={"test_a.py::test_x": "spec::scenario1"},
                summary="Added A",
            ),
            GroupResult(
                group_id="G2", status="completed",
                branch_name="b", worktree_path=Path("/wt"),
                files_changed=["c.py"],
                tests_added=["test_c.py"],
                test_mapping={"test_c.py::test_y": "spec::scenario2"},
                summary="Added C",
            ),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        result = _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        assert result == StepStatus.COMPLETED
        assert set(step.outputs["files_changed"]) == {"a.py", "b.py", "c.py"}
        assert set(step.outputs["tests_added"]) == {"test_a.py", "test_c.py"}
        assert "test_a.py::test_x" in step.outputs["test_mapping"]
        assert "test_c.py::test_y" in step.outputs["test_mapping"]
        assert "G1" in step.outputs["implemented_groups"]
        assert "G2" in step.outputs["implemented_groups"]
        assert step.outputs["completion_status"] == "complete"
        assert step.outputs["group_summaries"] == [
            {"group_id": "G1", "summary": "Added A"},
            {"group_id": "G2", "summary": "Added C"},
        ]

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_resume_carries_pre_resume_group_summaries(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        """A resumed DAG run must not drop the summaries earned before the
        interruption — from the structured list nor from the joined string.

        Regression: ``summaries`` started empty on resume while
        ``implemented_groups`` was seeded from prior_outputs, so the aggregate
        summary described only the groups that happened to re-run.
        """
        groups = _make_groups([("G2", 2, [], 200)])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G2": None},
            fork_from={},
            leaf_nodes={"G2"},
            convergence_points={},
            root_nodes={"G2"},
        )

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(
                group_id="G2", status="completed",
                branch_name="b2", worktree_path=Path("/wt2"),
                files_changed=["c.py"], tests_added=[], test_mapping={},
                summary="Added C; covered C",
            ),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        result = _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
            prior_outputs={
                "files_changed": ["a.py"],
                "tests_added": [],
                "test_mapping": {},
                "implemented_groups": ["G1"],
                "group_summaries": [{"group_id": "G1", "summary": "Added A; wired A"}],
            },
        )

        assert result == StepStatus.COMPLETED
        assert step.outputs["group_summaries"] == [
            {"group_id": "G1", "summary": "Added A; wired A"},
            {"group_id": "G2", "summary": "Added C; covered C"},
        ]
        assert step.outputs["implemented_groups"] == ["G1", "G2"]
        # The joined string keeps its "; " semantics but must now also carry
        # the pre-resume group.
        assert step.outputs["summary"] == "Added A; wired A; Added C; covered C"

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}._is_branch_reachable_from", return_value=True)
    @patch(f"{_IMP}.has_new_commits", return_value=False)
    @patch(f"{_IMP}.recover_stale_unmerged_paths", return_value=([], []))
    @patch(f"{_IMP}.merge_in_progress", return_value=False)
    def test_all_groups_recovered_keeps_prior_group_summaries(
        self, mock_inprog, mock_recover, mock_newc, mock_reach,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        """The all-recovered early return exposes the prior summaries both
        structurally and in the aggregate ``summary`` string."""
        step = _make_step()
        flow = _make_flow()

        with patch(f"{_IMP}._run_git") as mock_git:
            mock_git.return_value = MagicMock(returncode=1)
            result = _run_dag_parallel(
                groups=[], step=step, flow=flow, project_root=Path("/repo"),
                task_description="t", task_type="feature", design_section="",
                spec_summary="", injection=None, retry_count=0,
                prior_outputs={
                    "files_changed": ["a.py"],
                    "tests_added": [],
                    "test_mapping": {},
                    "implemented_groups": ["G1", "G2"],
                    "group_summaries": [
                        {"group_id": "G1", "summary": "Added A"},
                        {"group_id": "G2", "summary": "Added C"},
                    ],
                },
            )

        assert result == StepStatus.COMPLETED
        assert step.outputs["group_summaries"] == [
            {"group_id": "G1", "summary": "Added A"},
            {"group_id": "G2", "summary": "Added C"},
        ]
        assert step.outputs["summary"] == "Added A; Added C"

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}._is_branch_reachable_from", return_value=True)
    @patch(f"{_IMP}.has_new_commits", return_value=False)
    @patch(f"{_IMP}.recover_stale_unmerged_paths", return_value=([], []))
    @patch(f"{_IMP}.merge_in_progress", return_value=False)
    def test_all_groups_recovered_without_summaries_uses_placeholder(
        self, mock_inprog, mock_recover, mock_newc, mock_reach,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        """An older flow with no persisted per-group summaries still gets a
        non-empty ``summary`` for downstream string consumers."""
        step = _make_step()
        flow = _make_flow()

        with patch(f"{_IMP}._run_git") as mock_git:
            mock_git.return_value = MagicMock(returncode=1)
            result = _run_dag_parallel(
                groups=[], step=step, flow=flow, project_root=Path("/repo"),
                task_description="t", task_type="feature", design_section="",
                spec_summary="", injection=None, retry_count=0,
                prior_outputs={
                    "files_changed": ["a.py"],
                    "tests_added": [],
                    "test_mapping": {},
                    "implemented_groups": ["G1"],
                },
            )

        assert result == StepStatus.COMPLETED
        assert step.outputs["group_summaries"] == []
        assert step.outputs["summary"] == "Recovered from previous run"


# ---------------------------------------------------------------------------
# Test: classify_chains produces correct plans for various topologies
# ---------------------------------------------------------------------------


class TestClassifyChainsUnit:
    """Unit tests for classify_chains across different DAG shapes."""

    def test_linear_chain(self):
        """G1→G2→G3: all relay, G3 is leaf."""
        from tianluo.engine.dag_scheduler import classify_chains

        groups = _make_groups([
            ("G1", 1, [], 100),
            ("G2", 2, ["G1"], 100),
            ("G3", 3, ["G2"], 100),
        ])
        plan = classify_chains(groups)

        assert plan.relay_map == {"G1": None, "G2": "G1", "G3": "G2"}
        assert plan.fork_from == {}
        assert plan.leaf_nodes == {"G3"}
        assert plan.root_nodes == {"G1"}
        assert plan.convergence_points == {}

    def test_fork(self):
        """G1→{G2,G3}: G2 relays (smaller order), G3 forks."""
        from tianluo.engine.dag_scheduler import classify_chains

        groups = _make_groups([
            ("G1", 1, [], 100),
            ("G2", 2, ["G1"], 100),
            ("G3", 3, ["G1"], 100),
        ])
        plan = classify_chains(groups)

        assert plan.relay_map["G1"] is None
        assert plan.relay_map["G2"] == "G1"  # relay (smallest order)
        assert plan.relay_map["G3"] is None  # fork
        assert "G3" in plan.fork_from
        assert plan.fork_from["G3"] == "G1"
        assert plan.leaf_nodes == {"G2", "G3"}

    def test_diamond(self):
        """G1→{G2,G3}→G4: G4 is convergence point."""
        from tianluo.engine.dag_scheduler import classify_chains

        groups = _make_groups([
            ("G1", 1, [], 100),
            ("G2", 2, ["G1"], 100),
            ("G3", 3, ["G1"], 100),
            ("G4", 4, ["G2", "G3"], 100),
        ])
        plan = classify_chains(groups)

        assert plan.leaf_nodes == {"G4"}
        assert "G4" in plan.convergence_points
        conv = plan.convergence_points["G4"]
        assert conv.primary_predecessor == "G2"
        assert "G3" in conv.secondary_predecessors

    def test_independent_groups(self):
        """G1, G2 independent: both roots, both leaves."""
        from tianluo.engine.dag_scheduler import classify_chains

        groups = _make_groups([
            ("G1", 1, [], 100),
            ("G2", 2, [], 100),
        ])
        plan = classify_chains(groups)

        assert plan.root_nodes == {"G1", "G2"}
        assert plan.leaf_nodes == {"G1", "G2"}
        assert plan.relay_map == {"G1": None, "G2": None}
        assert plan.fork_from == {}

    def test_empty_groups(self):
        """Empty input → empty plan."""
        from tianluo.engine.dag_scheduler import classify_chains

        plan = classify_chains([])

        assert plan.relay_map == {}
        assert plan.leaf_nodes == set()


# ---------------------------------------------------------------------------
# Test: DAGScheduler get_fallback_leaves
# ---------------------------------------------------------------------------


class TestGetFallbackLeaves:
    """Unit tests for DAGScheduler.get_fallback_leaves."""

    def test_no_failure_no_fallback(self):
        """All completed → no fallback leaves."""
        from tianluo.engine.dag_scheduler import DAGScheduler

        groups = _make_groups([
            ("G1", 1, [], 100),
            ("G2", 2, ["G1"], 100),
        ])
        plan = RelayPlan(
            relay_map={"G1": None, "G2": "G1"},
            fork_from={},
            leaf_nodes={"G2"},
            convergence_points={},
            root_nodes={"G1"},
        )
        scheduler = DAGScheduler(groups, relay_plan=plan)

        # Simulate run results
        def mock_execute(group, deps, ctx):
            return GroupResult(
                group_id=group["group_id"],
                status="completed",
                branch_name=f"b-{group['group_id']}",
            )

        scheduler.run(mock_execute)
        assert scheduler.get_fallback_leaves() == []

    def test_downstream_failure_creates_fallback(self):
        """G1 completed, G2 fails → G1 is fallback leaf."""
        from tianluo.engine.dag_scheduler import DAGScheduler

        groups = _make_groups([
            ("G1", 1, [], 100),
            ("G2", 2, ["G1"], 100),
            ("G3", 3, ["G2"], 100),
        ])
        plan = RelayPlan(
            relay_map={"G1": None, "G2": "G1", "G3": "G2"},
            fork_from={},
            leaf_nodes={"G3"},
            convergence_points={},
            root_nodes={"G1"},
        )
        scheduler = DAGScheduler(groups, relay_plan=plan)

        call_count = {"n": 0}

        def mock_execute(group, deps, ctx):
            call_count["n"] += 1
            gid = group["group_id"]
            if gid == "G2":
                return GroupResult.failed("G2", "LLM error")
            return GroupResult(
                group_id=gid,
                status="completed",
                branch_name=f"b-{gid}",
            )

        scheduler.run(mock_execute)
        fallback = scheduler.get_fallback_leaves()
        assert "G1" in fallback
        # G3 should NOT be in fallback (it was skipped, not completed)
        assert "G3" not in fallback


# ---------------------------------------------------------------------------
# Test: Worktree cleanup deduplication
# ---------------------------------------------------------------------------


class TestWorktreeCleanup:
    """Verify worktree cleanup is deduplicated for relay chains."""

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_shared_branch_cleaned_once(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        """Three groups sharing one branch → one cleanup call."""
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
            ("G3", 3, ["G2"], 200),
        ])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1", "G3": "G2"},
            fork_from={},
            leaf_nodes={"G3"},
            convergence_points={},
            root_nodes={"G1"},
        )

        shared_branch = "impl/f/G1"
        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name=shared_branch, worktree_path=Path("/wt")),
            GroupResult(group_id="G2", status="completed",
                        branch_name=shared_branch, worktree_path=Path("/wt")),
            GroupResult(group_id="G3", status="completed",
                        branch_name=shared_branch, worktree_path=Path("/wt")),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        assert mock_cleanup.call_count == 1
        mock_cleanup.assert_called_once_with(Path("/repo"), shared_branch)

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_forked_branches_cleaned_separately(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge, mock_del,
    ):
        """Fork creates 2 branches → 2 cleanup calls."""
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
            ("G3", 3, ["G1"], 200),
        ])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1", "G3": None},
            fork_from={"G3": "G1"},
            leaf_nodes={"G2", "G3"},
            convergence_points={},
            root_nodes={"G1"},
        )

        branch_a = "impl/f/G1"
        branch_b = "impl/f/G3"
        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name=branch_a, worktree_path=Path("/wt-A")),
            GroupResult(group_id="G2", status="completed",
                        branch_name=branch_a, worktree_path=Path("/wt-A")),
            GroupResult(group_id="G3", status="completed",
                        branch_name=branch_b, worktree_path=Path("/wt-B")),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        assert mock_cleanup.call_count == 2
        cleaned_branches = {c[0][1] for c in mock_cleanup.call_args_list}
        assert cleaned_branches == {branch_a, branch_b}


# ---------------------------------------------------------------------------
# Test: Branch deletion
# ---------------------------------------------------------------------------


class TestBranchDeletion:
    """Verify all impl branches are deleted after DAG execution."""

    @patch(f"{_IMP}._is_branch_reachable_from", return_value=True)
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    @patch(f"{_IMP}.delete_branch")
    def test_all_impl_branches_deleted(
        self, mock_del, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge,
        mock_reachable,
    ):
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 2, ["G1"], 200),
        ])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1"},
            fork_from={},
            leaf_nodes={"G2"},
            convergence_points={},
            root_nodes={"G1"},
        )

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name="impl/f/G1", worktree_path=Path("/wt")),
            GroupResult(group_id="G2", status="completed",
                        branch_name="impl/f/G1", worktree_path=Path("/wt")),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow(flow_id="f")

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        # Only actual branch names should be deleted (G2 reuses G1's branch)
        deleted = {c[0][1] for c in mock_del.call_args_list}
        assert "impl/f/G1" in deleted
        assert "impl/f/G2" not in deleted  # G2 has no separate branch
