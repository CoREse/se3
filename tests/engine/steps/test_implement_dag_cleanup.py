"""Tests for the DAG cleanup safety net (v4.11.0).

Even if every merge-robustness layer in ``_merge_leaf_branch`` failed,
the cleanup loop must NOT force-delete a branch whose commits did not
land on ``original_branch``. And the step status must reflect any
remaining merge_failures so the flow stops instead of advancing into
test/self_check on an inconsistent main repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tianluo.engine.dag_scheduler import GroupResult, RelayPlan
from tianluo.engine.models import Step, StepStatus, StepType
from tianluo.engine.steps.implement import (
    _is_branch_reachable_from,
    _run_dag_parallel,
)


_IMP = "tianluo.engine.steps.implement"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "--initial-branch=main")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "T")
    _git(r, "commit", "--allow-empty", "-m", "init")
    return r


# ---------------------------------------------------------------------------
# _is_branch_reachable_from
# ---------------------------------------------------------------------------


class TestIsBranchReachable:
    def test_ancestor_returns_true(self, repo: Path):
        """A branch fully merged into main is an ancestor → True."""
        _git(repo, "checkout", "-b", "feat")
        _git(repo, "commit", "--allow-empty", "-m", "feat work")
        _git(repo, "checkout", "main")
        _git(repo, "merge", "feat", "--ff-only")
        assert _is_branch_reachable_from(repo, "feat", "main") is True

    def test_unmerged_branch_returns_false(self, repo: Path):
        """A branch with un-merged commits is NOT an ancestor → False.

        This is the protective case: an un-merged branch must NOT be
        deletable by the cleanup loop, otherwise commits are lost.
        """
        _git(repo, "checkout", "-b", "feat")
        _git(repo, "commit", "--allow-empty", "-m", "feat unmerged")
        _git(repo, "checkout", "main")
        # main is at init; feat has an extra commit not in main.
        assert _is_branch_reachable_from(repo, "feat", "main") is False

    def test_missing_branch_returns_false(self, repo: Path):
        """A non-existent branch ref → False (fail closed)."""
        assert _is_branch_reachable_from(repo, "no-such-branch", "main") is False


# ---------------------------------------------------------------------------
# Helpers to construct minimal Step / RelayPlan / scheduler scaffolding
# (parallels the patterns used in tests/engine/steps/test_implement_dag.py)
# ---------------------------------------------------------------------------


def _make_step():
    step = Step(
        step_id="impl_1",
        step_type=StepType.IMPLEMENT,
        inputs={"task_description": "t"},
    )
    return step


def _make_flow():
    flow = MagicMock()
    flow.flow_id = "20260101-test"
    return flow


def _make_groups(specs):
    """[(gid, order, deps, loc)] → list of group dicts."""
    return [
        {"group_id": gid, "order": order, "depends_on": list(deps), "loc": loc}
        for gid, order, deps, loc in specs
    ]


# ---------------------------------------------------------------------------
# Cleanup safe-delete: failed merge → branch preserved
# ---------------------------------------------------------------------------


class TestCleanupSafeDelete:
    """Branches whose commits did not land on original_branch are preserved."""

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._is_branch_reachable_from")
    @patch(f"{_IMP}._merge_leaf_branch")
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_failed_merge_branch_preserved(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch,
        mock_merge, mock_reachable, mock_del,
    ):
        """Branch where _merge_leaf_branch returned False is NOT deleted."""
        groups = _make_groups([("G1", 1, [], 200)])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None}, fork_from={},
            leaf_nodes={"G1"}, convergence_points={},
            root_nodes={"G1"},
        )

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name="impl/f/G1", worktree_path=Path("/wt")),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        mock_merge.return_value = False  # merge fails
        # Branch NOT ancestor since merge failed
        mock_reachable.return_value = False

        step = _make_step()
        flow = _make_flow()

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature",
            injection=None, retry_count=0,
        )

        # delete_branch must NOT have been called for impl/f/G1
        for call in mock_del.call_args_list:
            assert call.args[1] != "impl/f/G1", (
                f"branch impl/f/G1 should be preserved on merge failure, "
                f"but delete_branch was called with: {call.args}"
            )
        # Step output reports the preserved branch
        assert "impl/f/G1" in step.outputs.get("preserved_branches", [])
        # And step failed with merge_failures populated
        assert "G1" in step.outputs.get("merge_failures", [])
        assert step.outputs["completion_status"] == "failed"

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._is_branch_reachable_from", return_value=True)
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_successful_merge_branch_deleted(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch,
        mock_merge, mock_reachable, mock_del,
    ):
        """Successfully merged branch IS deleted (clean happy-path cleanup)."""
        groups = _make_groups([("G1", 1, [], 200)])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None}, fork_from={},
            leaf_nodes={"G1"}, convergence_points={},
            root_nodes={"G1"},
        )

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name="impl/f/G1", worktree_path=Path("/wt")),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature",
            injection=None, retry_count=0,
        )

        # delete_branch invoked with impl/f/G1
        delete_targets = [c.args[1] for c in mock_del.call_args_list]
        assert "impl/f/G1" in delete_targets
        # No preserved_branches output on the happy path
        assert "preserved_branches" not in step.outputs
        assert step.outputs.get("merge_failures", []) == []

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._is_branch_reachable_from")
    @patch(f"{_IMP}._merge_leaf_branch")
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_mixed_merge_outcomes(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch,
        mock_merge, mock_reachable, mock_del,
    ):
        """Out of 2 leaves: 1 merged, 1 failed → first deleted, second preserved."""
        groups = _make_groups([
            ("G1", 1, [], 200),
            ("G2", 1, [], 200),  # independent leaf
        ])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": None}, fork_from={},
            leaf_nodes={"G1", "G2"}, convergence_points={},
            root_nodes={"G1", "G2"},
        )

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(group_id="G1", status="completed",
                        branch_name="impl/f/G1", worktree_path=Path("/wt1")),
            GroupResult(group_id="G2", status="completed",
                        branch_name="impl/f/G2", worktree_path=Path("/wt2")),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        # G1 merges OK, G2 fails
        def merge_side_effect(_root, branch, *args, **kwargs):
            return branch == "impl/f/G1"
        mock_merge.side_effect = merge_side_effect

        # reachability mirrors merge outcome
        def reachable_side_effect(_root, branch, _target):
            return branch == "impl/f/G1"
        mock_reachable.side_effect = reachable_side_effect

        step = _make_step()
        flow = _make_flow()

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature",
            injection=None, retry_count=0,
        )

        delete_targets = [c.args[1] for c in mock_del.call_args_list]
        assert "impl/f/G1" in delete_targets
        assert "impl/f/G2" not in delete_targets
        assert "impl/f/G2" in step.outputs.get("preserved_branches", [])


# ---------------------------------------------------------------------------
# overall_status: merge_failures is an independent failure source
# ---------------------------------------------------------------------------


class TestOverallStatusReflectsMergeFailures:
    """merge_failures non-empty → status='failed' regardless of group statuses."""

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._is_branch_reachable_from", return_value=False)
    @patch(f"{_IMP}._merge_leaf_branch", return_value=False)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_all_groups_complete_but_merge_failed_returns_failed(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch,
        mock_merge, mock_reachable, mock_del,
    ):
        """All groups completed_status='complete' but merge failed → status=failed."""
        groups = _make_groups([("G1", 1, [], 200)])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None}, fork_from={},
            leaf_nodes={"G1"}, convergence_points={},
            root_nodes={"G1"},
        )

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(
                group_id="G1", status="completed",
                branch_name="impl/f/G1", worktree_path=Path("/wt"),
                completion_status="complete",
            ),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        result = _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature",
            injection=None, retry_count=0,
        )

        assert step.outputs["completion_status"] == "failed"
        assert step.outputs["merge_failures"] == ["G1"]
        assert result == StepStatus.FAILED

    @patch(f"{_IMP}.delete_branch")
    @patch(f"{_IMP}._is_branch_reachable_from", return_value=True)
    @patch(f"{_IMP}._merge_leaf_branch", return_value=True)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.force_cleanup_worktree")
    @patch(f"{_IMP}._salvage_history_from_worktree")
    @patch(f"{_IMP}.DAGScheduler")
    @patch(f"{_IMP}.classify_chains")
    @patch(f"{_IMP}.transitive_reduce")
    def test_merge_success_status_complete(
        self, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch,
        mock_merge, mock_reachable, mock_del,
    ):
        """Happy path: all groups completed + merge succeeded → status=complete."""
        groups = _make_groups([("G1", 1, [], 200)])
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None}, fork_from={},
            leaf_nodes={"G1"}, convergence_points={},
            root_nodes={"G1"},
        )

        mock_sched = MagicMock()
        mock_sched.run.return_value = [
            GroupResult(
                group_id="G1", status="completed",
                branch_name="impl/f/G1", worktree_path=Path("/wt"),
                completion_status="complete",
            ),
        ]
        mock_sched.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_sched

        step = _make_step()
        flow = _make_flow()

        result = _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature",
            injection=None, retry_count=0,
        )

        assert step.outputs["completion_status"] == "complete"
        assert step.outputs.get("merge_failures", []) == []
        assert result == StepStatus.COMPLETED
