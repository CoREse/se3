"""Tests for DAG relay execution integration in implement step.

Tests the integration of transitive reduction, relay plan, leaf-only merge,
LOC threshold, and enhanced conflict resolution in the implement step.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tianluo.engine.dag_scheduler import GroupResult, RelayContext, RelayPlan
from tianluo.engine.steps.implement import (
    _compute_total_loc,
    _merge_leaf_branch,
)
from tianluo.engine.worktree import resolve_merge_conflicts_with_context


# ---------------------------------------------------------------------------
# _compute_total_loc
# ---------------------------------------------------------------------------


class TestComputeTotalLoc:
    def test_empty_groups(self):
        assert _compute_total_loc([]) == 0

    def test_groups_without_estimated_loc(self):
        """Tasks missing estimated_loc default to 50 each."""
        groups = [
            {"group_id": "G1", "tasks": [{"id": 1}]},
            {"group_id": "G2", "tasks": [{"id": 2}]},
        ]
        assert _compute_total_loc(groups) == 100  # 2 tasks × 50 default

    def test_groups_with_estimated_loc(self):
        groups = [
            {"group_id": "G1", "tasks": [{"id": 1, "estimated_loc": 50}]},
            {"group_id": "G2", "tasks": [
                {"id": 2, "estimated_loc": 100},
                {"id": 3, "estimated_loc": 75},
            ]},
        ]
        assert _compute_total_loc(groups) == 225

    def test_mixed_tasks_some_without_loc(self):
        """Tasks without estimated_loc default to 50."""
        groups = [
            {"group_id": "G1", "tasks": [
                {"id": 1, "estimated_loc": 50},
                {"id": 2},  # no estimated_loc → defaults to 50
            ]},
        ]
        assert _compute_total_loc(groups) == 100  # 50 + 50 default

    def test_string_tasks_ignored(self):
        """Tasks that are plain strings (legacy format) should be skipped."""
        groups = [
            {"group_id": "G1", "tasks": ["task1", "task2"]},
        ]
        assert _compute_total_loc(groups) == 0

    def test_no_tasks_key(self):
        groups = [{"group_id": "G1"}]
        assert _compute_total_loc(groups) == 0

    def test_single_group_single_task(self):
        groups = [
            {"group_id": "G1", "tasks": [{"id": 1, "estimated_loc": 300}]},
        ]
        assert _compute_total_loc(groups) == 300


# ---------------------------------------------------------------------------
# LOC threshold routing in implement_handler
# ---------------------------------------------------------------------------


class TestLocThresholdRouting:
    """Verify LOC threshold routes small multi-group tasks to single LLM call."""

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("tianluo.engine.steps.implement._run_single_llm_call")
    @patch("tianluo.engine.steps.implement._run_dag_parallel")
    @patch("tianluo.engine.steps.implement.has_commits", return_value=True)
    def test_below_threshold_single_call(
        self, mock_has_commits, mock_dag, mock_single, mock_inj, tmp_path,
    ):
        """Total LOC below threshold routes to single LLM call, not DAG."""
        from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
        from tianluo.engine.steps.implement import implement_handler

        mock_single.return_value = StepStatus.COMPLETED

        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [],
             "tasks": [{"id": 1, "estimated_loc": 50}]},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"],
             "tasks": [{"id": 2, "estimated_loc": 50}]},
        ]
        step = Step(
            step_type=StepType.IMPLEMENT,
            step_id="test-loc",
            inputs={
                "task_description": "Test",
                "task_type": "feature",
                "task_groups": groups,
                "spec_content": {},
                # The LOC gate is granular / legacy scheduling by definition:
                # only that doctrine emits the per-task estimated_loc it reads.
                "plan_decomposition": "granular",
            },
        )
        flow = FlowInstance(
            task_description="Test",
            change_path=tmp_path / "tianluo",
        )

        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_single.assert_called_once()
        mock_dag.assert_not_called()

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("tianluo.engine.steps.implement._run_dag_parallel")
    @patch("tianluo.engine.steps.implement.has_commits", return_value=True)
    def test_above_threshold_uses_dag(
        self, mock_has_commits, mock_dag, mock_inj, tmp_path,
    ):
        """Total LOC above threshold routes to DAG parallel."""
        from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
        from tianluo.engine.steps.implement import implement_handler

        mock_dag.return_value = StepStatus.COMPLETED

        # Fork DAG (G1 → G2, G1 → G3) so the linear-chain short-circuit
        # does not apply; linear chains now fall through to sequential.
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [],
             "tasks": [{"id": 1, "estimated_loc": 200}]},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"],
             "tasks": [{"id": 2, "estimated_loc": 200}]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1"],
             "tasks": [{"id": 3, "estimated_loc": 200}]},
        ]
        step = Step(
            step_type=StepType.IMPLEMENT,
            step_id="test-loc",
            inputs={
                "task_description": "Test",
                "task_type": "feature",
                "task_groups": groups,
                "spec_content": {},
            },
        )
        flow = FlowInstance(
            task_description="Test",
            change_path=tmp_path / "tianluo",
        )

        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_called_once()

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("tianluo.engine.steps.implement._run_single_llm_call")
    @patch("tianluo.engine.steps.implement._run_dag_parallel")
    @patch("tianluo.engine.steps.implement.has_commits", return_value=True)
    def test_no_estimated_loc_defaults_to_50(
        self, mock_has_commits, mock_dag, mock_single, mock_inj, tmp_path,
    ):
        """Tasks without estimated_loc default to 50 LOC each, routing via threshold."""
        from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
        from tianluo.engine.steps.implement import implement_handler

        mock_single.return_value = StepStatus.COMPLETED

        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [],
             "tasks": [{"id": 1}]},  # no estimated_loc → defaults to 50
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"],
             "tasks": [{"id": 2}]},  # no estimated_loc → defaults to 50
        ]
        # total_loc = 100 (2 tasks × 50 default) ≤ 300 threshold → single call
        step = Step(
            step_type=StepType.IMPLEMENT,
            step_id="test-loc",
            inputs={
                "task_description": "Test",
                "task_type": "feature",
                "task_groups": groups,
                "spec_content": {},
                # The LOC gate is granular / legacy scheduling by definition:
                # only that doctrine emits the per-task estimated_loc it reads.
                "plan_decomposition": "granular",
            },
        )
        flow = FlowInstance(
            task_description="Test",
            change_path=tmp_path / "tianluo",
        )

        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_single.assert_called_once()
        mock_dag.assert_not_called()


# ---------------------------------------------------------------------------
# _merge_leaf_branch
# ---------------------------------------------------------------------------


class TestMergeLeafBranch:
    """Tests for _merge_leaf_branch function."""

    @patch("tianluo.engine.steps.implement.get_conflicting_files")
    @patch("tianluo.engine.steps.implement.resolve_merge_conflicts_with_context")
    @patch("tianluo.engine.steps.implement._run_git")
    @patch("tianluo.engine.steps.implement.get_current_branch")
    def test_clean_merge(self, mock_branch, mock_git, mock_resolve, mock_conflict):
        """Clean merge (no stashable changes, no conflict) returns True."""
        mock_branch.return_value = "main"
        # Stash returns "No local changes" so stashed=False; merge succeeds; no pop.
        mock_git.return_value = MagicMock(
            returncode=0, stdout="No local changes to save", stderr="",
        )

        result = _merge_leaf_branch(
            Path("/repo"), "impl/flow/G3", "main",
            "task desc", [],
        )

        assert result is True
        mock_resolve.assert_not_called()

    @patch("tianluo.engine.steps.implement.get_conflicting_files")
    @patch("tianluo.engine.steps.implement.resolve_merge_conflicts_with_context")
    @patch("tianluo.engine.steps.implement._run_git")
    @patch("tianluo.engine.steps.implement.get_current_branch")
    def test_conflict_resolved_by_llm(self, mock_branch, mock_git, mock_resolve, mock_conflict):
        """Conflict resolved by LLM returns True; take-theirs fallback not invoked."""
        mock_branch.return_value = "main"
        no_stash = MagicMock(returncode=0, stdout="No local changes", stderr="")
        ref_ok = MagicMock(returncode=0, stdout="abc123", stderr="")
        merge_result = MagicMock(returncode=1, stdout="CONFLICT", stderr="")
        mock_git.side_effect = [no_stash, ref_ok, merge_result]
        mock_conflict.return_value = ["file.py"]
        mock_resolve.return_value = True

        result = _merge_leaf_branch(
            Path("/repo"), "impl/flow/G3", "main",
            "task desc", [{"group_id": "G1", "summary": "s", "files_changed": []}],
        )

        assert result is True
        mock_resolve.assert_called_once()

    @patch("tianluo.engine.steps.implement._record_take_theirs_event")
    @patch("tianluo.engine.steps.implement.get_conflicting_files")
    @patch("tianluo.engine.steps.implement.resolve_merge_conflicts_with_context")
    @patch("tianluo.engine.steps.implement._run_git")
    @patch("tianluo.engine.steps.implement.get_current_branch")
    def test_llm_exhausted_falls_back_to_take_theirs(
        self, mock_branch, mock_git, mock_resolve, mock_conflict, mock_audit,
    ):
        """When LLM cannot resolve, take-theirs fallback completes the merge.

        Sequence: stash (no-op), merge (conflict), checkout --theirs <file>,
        add <file>, commit. Audit issue recorded.
        """
        mock_branch.return_value = "main"
        no_stash = MagicMock(returncode=0, stdout="No local changes", stderr="")
        ref_ok = MagicMock(returncode=0, stdout="abc123", stderr="")
        merge_result = MagicMock(returncode=1, stdout="CONFLICT", stderr="")
        ok = MagicMock(returncode=0, stdout="", stderr="")
        mock_git.side_effect = [no_stash, ref_ok, merge_result, ok, ok, ok]
        mock_conflict.return_value = ["file.py"]
        mock_resolve.return_value = False  # LLM exhausted

        result = _merge_leaf_branch(
            Path("/repo"), "impl/flow/G3", "main",
            "task desc", [],
        )

        assert result is True
        # Verify take-theirs sequence: checkout --theirs, add, commit
        args_list = [tuple(call.args) for call in mock_git.call_args_list]
        checkout_call = [a for a in args_list if "checkout" in a and "--theirs" in a]
        assert checkout_call, f"expected --theirs checkout, got {args_list}"
        commit_call = [a for a in args_list if "commit" in a]
        assert commit_call, f"expected commit, got {args_list}"
        mock_audit.assert_called_once()

    @patch("tianluo.engine.steps.implement._record_take_theirs_event")
    @patch("tianluo.engine.steps.implement.get_conflicting_files")
    @patch("tianluo.engine.steps.implement.resolve_merge_conflicts_with_context")
    @patch("tianluo.engine.steps.implement._run_git")
    @patch("tianluo.engine.steps.implement.get_current_branch")
    def test_take_theirs_commit_failure_aborts(
        self, mock_branch, mock_git, mock_resolve, mock_conflict, mock_audit,
    ):
        """If take-theirs commit itself fails (extremely rare), abort + False."""
        mock_branch.return_value = "main"
        no_stash = MagicMock(returncode=0, stdout="No local changes", stderr="")
        ref_ok = MagicMock(returncode=0, stdout="abc123", stderr="")
        merge_result = MagicMock(returncode=1, stdout="CONFLICT", stderr="")
        ok = MagicMock(returncode=0, stdout="", stderr="")
        commit_fail = MagicMock(returncode=1, stdout="", stderr="commit blocked")
        abort_ok = MagicMock(returncode=0, stdout="", stderr="")
        mock_git.side_effect = [
            no_stash, ref_ok, merge_result, ok, ok, commit_fail, abort_ok,
        ]
        mock_conflict.return_value = ["file.py"]
        mock_resolve.return_value = False

        result = _merge_leaf_branch(
            Path("/repo"), "impl/flow/G3", "main",
            "task desc", [],
        )

        assert result is False
        # Audit issue NOT recorded when commit fails
        mock_audit.assert_not_called()

    @patch("tianluo.engine.steps.implement.get_conflicting_files")
    @patch("tianluo.engine.steps.implement._run_git")
    @patch("tianluo.engine.steps.implement.get_current_branch")
    def test_non_conflict_failure(self, mock_branch, mock_git, mock_conflict):
        """Non-conflict merge failure aborts and returns False."""
        mock_branch.return_value = "main"
        no_stash = MagicMock(returncode=0, stdout="No local changes", stderr="")
        ref_ok = MagicMock(returncode=0, stdout="abc123", stderr="")
        merge_fail = MagicMock(
            returncode=1, stdout="fatal: error", stderr="not a conflict",
        )
        abort_result = MagicMock(returncode=0, stdout="", stderr="")
        mock_git.side_effect = [no_stash, ref_ok, merge_fail, abort_result]

        result = _merge_leaf_branch(
            Path("/repo"), "impl/flow/G3", "main",
            "task desc", [],
        )

        assert result is False
        mock_conflict.assert_not_called()

    @patch("tianluo.engine.steps.implement._run_git")
    @patch("tianluo.engine.steps.implement.get_current_branch")
    def test_checkout_to_original_branch(self, mock_branch, mock_git):
        """Checks out original_branch if not already there.

        Sequence: checkout main, stash push (no-op), merge (success). No pop
        because stash was no-op.
        """
        mock_branch.return_value = "impl/flow/G3"  # Not on original branch
        checkout_result = MagicMock(returncode=0, stdout="", stderr="")
        no_stash = MagicMock(returncode=0, stdout="No local changes", stderr="")
        ref_ok = MagicMock(returncode=0, stdout="abc123", stderr="")
        merge_result = MagicMock(returncode=0, stdout="", stderr="")
        mock_git.side_effect = [checkout_result, no_stash, ref_ok, merge_result]

        result = _merge_leaf_branch(
            Path("/repo"), "impl/flow/G3", "main",
            "task desc", [],
        )

        assert result is True
        # First git call should be checkout
        first_call = mock_git.call_args_list[0]
        assert "checkout" in first_call[0]


# ---------------------------------------------------------------------------
# resolve_merge_conflicts_with_context
# ---------------------------------------------------------------------------


class TestResolveLeafMergeConflicts:
    """Tests for resolve_merge_conflicts_with_context function (worktree.py)."""

    @patch("tianluo.engine.worktree._run_git")
    @patch("tianluo.engine.llm_caller.LLMCaller")
    def test_successful_resolution(self, mock_caller_cls, mock_git, tmp_path):
        """LLM resolves all conflicts on first attempt."""
        conflict_file = tmp_path / "conflict.py"
        conflict_file.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>>\n")

        mock_caller = MagicMock()
        mock_caller.call.return_value = "resolved content"
        mock_caller_cls.return_value = mock_caller

        mock_git.return_value = MagicMock(returncode=0)

        result = resolve_merge_conflicts_with_context(
            tmp_path, ["conflict.py"],
            "task desc",
            [{"group_id": "G1", "summary": "did stuff", "files_changed": ["a.py"]}],
        )

        assert result is True
        assert conflict_file.read_text() == "resolved content"

    @patch("tianluo.engine.worktree._run_git")
    @patch("tianluo.engine.llm_caller.LLMCaller")
    def test_markers_in_output_triggers_retry(self, mock_caller_cls, mock_git, tmp_path):
        """LLM output with markers triggers retry."""
        conflict_file = tmp_path / "conflict.py"
        conflict_content = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>>\n"
        conflict_file.write_text(conflict_content)

        mock_caller = MagicMock()
        # First attempt: still has markers. Second: clean.
        mock_caller.call.side_effect = [
            "<<<<<<< HEAD\nstill broken\n>>>>>>>",
            "properly resolved",
        ]
        mock_caller_cls.return_value = mock_caller

        mock_git.return_value = MagicMock(returncode=0)

        result = resolve_merge_conflicts_with_context(
            tmp_path, ["conflict.py"],
            "task desc", [],
            max_retries=2,
        )

        assert result is True
        assert mock_caller.call.call_count == 2

    @patch("tianluo.engine.worktree._run_git")
    @patch("tianluo.engine.llm_caller.LLMCaller")
    def test_all_retries_fail_returns_false(self, mock_caller_cls, mock_git, tmp_path):
        """All retries failing returns False (no --theirs fallback)."""
        conflict_file = tmp_path / "conflict.py"
        conflict_file.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>>\n")

        mock_caller = MagicMock()
        mock_caller.call.return_value = "<<<<<<< still broken >>>>>>>"
        mock_caller_cls.return_value = mock_caller

        mock_git.return_value = MagicMock(returncode=0)

        result = resolve_merge_conflicts_with_context(
            tmp_path, ["conflict.py"],
            "task desc", [],
            max_retries=3,
        )

        assert result is False
        # Should have tried 3 times
        assert mock_caller.call.call_count == 3

    @patch("tianluo.engine.worktree._run_git")
    @patch("tianluo.engine.llm_caller.LLMCaller")
    def test_llm_exception_triggers_retry(self, mock_caller_cls, mock_git, tmp_path):
        """LLM exception triggers retry."""
        conflict_file = tmp_path / "conflict.py"
        conflict_file.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>>\n")

        mock_caller = MagicMock()
        mock_caller.call.side_effect = [
            RuntimeError("LLM error"),
            "resolved content",
        ]
        mock_caller_cls.return_value = mock_caller

        mock_git.return_value = MagicMock(returncode=0)

        result = resolve_merge_conflicts_with_context(
            tmp_path, ["conflict.py"],
            "task desc", [],
            max_retries=2,
        )

        assert result is True

    @patch("tianluo.engine.llm_caller.LLMCaller")
    def test_missing_file_returns_false(self, mock_caller_cls, tmp_path):
        """Missing conflict file returns False."""
        result = resolve_merge_conflicts_with_context(
            tmp_path, ["nonexistent.py"],
            "task desc", [],
        )

        assert result is False

    @patch("tianluo.engine.worktree._run_git")
    @patch("tianluo.engine.llm_caller.LLMCaller")
    def test_already_resolved_file_skipped(self, mock_caller_cls, mock_git, tmp_path):
        """Files without conflict markers are skipped."""
        clean_file = tmp_path / "clean.py"
        clean_file.write_text("already resolved content")

        mock_git.return_value = MagicMock(returncode=0)

        result = resolve_merge_conflicts_with_context(
            tmp_path, ["clean.py"],
            "task desc", [],
        )

        assert result is True
        # LLM should not be called for already-resolved files
        mock_caller_cls.assert_not_called()

    @patch("tianluo.engine.worktree._run_git")
    @patch("tianluo.engine.llm_caller.LLMCaller")
    def test_rich_context_in_prompt(self, mock_caller_cls, mock_git, tmp_path):
        """Verify the LLM prompt includes task description and group summaries."""
        conflict_file = tmp_path / "file.py"
        conflict_file.write_text("<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>>\n")

        mock_caller = MagicMock()
        mock_caller.call.return_value = "resolved"
        mock_caller_cls.return_value = mock_caller

        mock_git.return_value = MagicMock(returncode=0)

        resolve_merge_conflicts_with_context(
            tmp_path, ["file.py"],
            "Implement user auth",
            [
                {"group_id": "G1", "summary": "Added login", "files_changed": ["auth.py"]},
                {"group_id": "G2", "summary": "Added logout", "files_changed": ["auth.py"]},
            ],
        )

        prompt = mock_caller.call.call_args[1].get("prompt", mock_caller.call.call_args[0][0] if mock_caller.call.call_args[0] else "")
        # Try kwargs first, then positional
        if not prompt:
            prompt = str(mock_caller.call.call_args)

        assert "Implement user auth" in prompt or "user auth" in str(mock_caller.call.call_args)


# ---------------------------------------------------------------------------
# Integration: transitive reduction in _run_dag_parallel
# ---------------------------------------------------------------------------


class TestDagParallelRelayIntegration:
    """Test that _run_dag_parallel integrates transitive reduction and relay plan."""

    @patch("tianluo.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("tianluo.engine.steps.implement.get_current_branch", return_value="main")
    @patch("tianluo.engine.steps.implement.force_cleanup_worktree")
    @patch("tianluo.engine.steps.implement._salvage_history_from_worktree")
    @patch("tianluo.engine.steps.implement.DAGScheduler")
    @patch("tianluo.engine.steps.implement.classify_chains")
    @patch("tianluo.engine.steps.implement.transitive_reduce")
    @patch("tianluo.engine.steps.implement.delete_branch")
    def test_transitive_reduce_called(
        self, mock_del, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge,
    ):
        """_run_dag_parallel calls transitive_reduce before classify_chains."""
        from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
        from tianluo.engine.steps.implement import _run_dag_parallel

        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [],
             "tasks": [{"id": 1, "estimated_loc": 200}]},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"],
             "tasks": [{"id": 2, "estimated_loc": 200}]},
        ]
        reduced_groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [],
             "tasks": [{"id": 1, "estimated_loc": 200}]},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"],
             "tasks": [{"id": 2, "estimated_loc": 200}]},
        ]
        mock_reduce.return_value = reduced_groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1"},
            fork_from={},
            leaf_nodes={"G2"},
            convergence_points={},
            root_nodes={"G1"},
        )

        mock_scheduler = MagicMock()
        mock_scheduler.run.return_value = [
            GroupResult(group_id="G1", status="completed", branch_name="impl/f/G1",
                        worktree_path=Path("/wt")),
            GroupResult(group_id="G2", status="completed", branch_name="impl/f/G1",
                        worktree_path=Path("/wt")),
        ]
        mock_scheduler.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_scheduler

        step = Step(step_type=StepType.IMPLEMENT, step_id="s", inputs={}, outputs={})
        flow = FlowInstance(task_description="t", flow_id="f")

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature",
            injection=None, retry_count=0,
        )

        mock_reduce.assert_called_once_with(groups)
        mock_classify.assert_called_once_with(reduced_groups)

    @patch("tianluo.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("tianluo.engine.steps.implement.get_current_branch", return_value="main")
    @patch("tianluo.engine.steps.implement.force_cleanup_worktree")
    @patch("tianluo.engine.steps.implement._salvage_history_from_worktree")
    @patch("tianluo.engine.steps.implement.DAGScheduler")
    @patch("tianluo.engine.steps.implement.classify_chains")
    @patch("tianluo.engine.steps.implement.transitive_reduce")
    @patch("tianluo.engine.steps.implement.delete_branch")
    def test_relay_plan_passed_to_scheduler(
        self, mock_del, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge,
    ):
        """DAGScheduler receives relay_plan from classify_chains."""
        from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
        from tianluo.engine.steps.implement import _run_dag_parallel

        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [],
             "tasks": [{"id": 1, "estimated_loc": 200}]},
        ]
        mock_reduce.return_value = groups
        relay_plan = RelayPlan(
            relay_map={"G1": None}, fork_from={},
            leaf_nodes={"G1"}, convergence_points={}, root_nodes={"G1"},
        )
        mock_classify.return_value = relay_plan

        mock_scheduler = MagicMock()
        mock_scheduler.run.return_value = [
            GroupResult(group_id="G1", status="completed", branch_name="impl/f/G1",
                        worktree_path=Path("/wt")),
        ]
        mock_scheduler.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_scheduler

        step = Step(step_type=StepType.IMPLEMENT, step_id="s", inputs={}, outputs={})
        flow = FlowInstance(task_description="t", flow_id="f")

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature",
            injection=None, retry_count=0,
        )

        # DAGScheduler should be created with relay_plan
        mock_sched_cls.assert_called_once()
        call_kwargs = mock_sched_cls.call_args
        assert call_kwargs[1].get("relay_plan") is relay_plan or (
            len(call_kwargs[0]) >= 3 and call_kwargs[0][2] is relay_plan
        )

    @patch("tianluo.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("tianluo.engine.steps.implement.get_current_branch", return_value="main")
    @patch("tianluo.engine.steps.implement.force_cleanup_worktree")
    @patch("tianluo.engine.steps.implement._salvage_history_from_worktree")
    @patch("tianluo.engine.steps.implement.DAGScheduler")
    @patch("tianluo.engine.steps.implement.classify_chains")
    @patch("tianluo.engine.steps.implement.transitive_reduce")
    @patch("tianluo.engine.steps.implement.delete_branch")
    def test_only_leaf_branches_merged(
        self, mock_del, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge,
    ):
        """Only leaf node branches are merged back, not intermediate nodes."""
        from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
        from tianluo.engine.steps.implement import _run_dag_parallel

        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [],
             "tasks": [{"id": 1, "estimated_loc": 200}]},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"],
             "tasks": [{"id": 2, "estimated_loc": 200}]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G2"],
             "tasks": [{"id": 3, "estimated_loc": 200}]},
        ]
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1", "G3": "G2"},
            fork_from={},
            leaf_nodes={"G3"},  # Only G3 is a leaf
            convergence_points={},
            root_nodes={"G1"},
        )

        # All share the same branch (relay chain)
        mock_scheduler = MagicMock()
        mock_scheduler.run.return_value = [
            GroupResult(group_id="G1", status="completed", branch_name="impl/f/G1",
                        worktree_path=Path("/wt")),
            GroupResult(group_id="G2", status="completed", branch_name="impl/f/G1",
                        worktree_path=Path("/wt")),
            GroupResult(group_id="G3", status="completed", branch_name="impl/f/G1",
                        worktree_path=Path("/wt")),
        ]
        mock_scheduler.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_scheduler

        step = Step(step_type=StepType.IMPLEMENT, step_id="s", inputs={}, outputs={})
        flow = FlowInstance(task_description="t", flow_id="f")

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature",
            injection=None, retry_count=0,
        )

        # Only one merge call (for the leaf G3's branch, which is shared)
        assert mock_merge.call_count == 1

    @patch("tianluo.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("tianluo.engine.steps.implement.get_current_branch", return_value="main")
    @patch("tianluo.engine.steps.implement.force_cleanup_worktree")
    @patch("tianluo.engine.steps.implement._salvage_history_from_worktree")
    @patch("tianluo.engine.steps.implement.DAGScheduler")
    @patch("tianluo.engine.steps.implement.classify_chains")
    @patch("tianluo.engine.steps.implement.transitive_reduce")
    @patch("tianluo.engine.steps.implement.delete_branch")
    def test_fallback_leaves_merged_on_failure(
        self, mock_del, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge,
    ):
        """Fallback leaves from partial failure are merged back."""
        from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
        from tianluo.engine.steps.implement import _run_dag_parallel

        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [],
             "tasks": [{"id": 1, "estimated_loc": 200}]},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"],
             "tasks": [{"id": 2, "estimated_loc": 200}]},
        ]
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1"},
            fork_from={},
            leaf_nodes={"G2"},
            convergence_points={},
            root_nodes={"G1"},
        )

        # G1 completed, G2 failed
        mock_scheduler = MagicMock()
        mock_scheduler.run.return_value = [
            GroupResult(group_id="G1", status="completed", branch_name="impl/f/G1",
                        worktree_path=Path("/wt")),
            GroupResult(group_id="G2", status="failed", error="LLM error",
                        worktree_path=Path("/wt")),
        ]
        mock_scheduler.get_fallback_leaves.return_value = ["G1"]
        mock_sched_cls.return_value = mock_scheduler

        step = Step(step_type=StepType.IMPLEMENT, step_id="s", inputs={}, outputs={})
        flow = FlowInstance(task_description="t", flow_id="f")

        result = _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature",
            injection=None, retry_count=0,
        )

        # G1 should be merged as a fallback leaf
        assert mock_merge.call_count == 1
        merge_call = mock_merge.call_args
        assert "impl/f/G1" in str(merge_call)

        # Status should be partial (some work preserved)
        assert step.outputs["completion_status"] == "partial"
        assert result == StepStatus.PARTIAL

    @patch("tianluo.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("tianluo.engine.steps.implement.get_current_branch", return_value="main")
    @patch("tianluo.engine.steps.implement.force_cleanup_worktree")
    @patch("tianluo.engine.steps.implement._salvage_history_from_worktree")
    @patch("tianluo.engine.steps.implement.DAGScheduler")
    @patch("tianluo.engine.steps.implement.classify_chains")
    @patch("tianluo.engine.steps.implement.transitive_reduce")
    @patch("tianluo.engine.steps.implement.delete_branch")
    def test_worktree_cleanup_deduplicated(
        self, mock_del, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge,
    ):
        """Relay chains sharing worktrees only clean up once."""
        from tianluo.engine.models import FlowInstance, Step, StepType
        from tianluo.engine.steps.implement import _run_dag_parallel

        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [],
             "tasks": [{"id": 1, "estimated_loc": 200}]},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"],
             "tasks": [{"id": 2, "estimated_loc": 200}]},
        ]
        mock_reduce.return_value = groups
        mock_classify.return_value = RelayPlan(
            relay_map={"G1": None, "G2": "G1"},
            fork_from={},
            leaf_nodes={"G2"},
            convergence_points={},
            root_nodes={"G1"},
        )

        # Both use the same branch (relay)
        mock_scheduler = MagicMock()
        mock_scheduler.run.return_value = [
            GroupResult(group_id="G1", status="completed", branch_name="impl/f/G1",
                        worktree_path=Path("/wt")),
            GroupResult(group_id="G2", status="completed", branch_name="impl/f/G1",
                        worktree_path=Path("/wt")),
        ]
        mock_scheduler.get_fallback_leaves.return_value = []
        mock_sched_cls.return_value = mock_scheduler

        step = Step(step_type=StepType.IMPLEMENT, step_id="s", inputs={}, outputs={})
        flow = FlowInstance(task_description="t", flow_id="f")

        _run_dag_parallel(
            groups=groups, step=step, flow=flow, project_root=Path("/repo"),
            task_description="t", task_type="feature",
            injection=None, retry_count=0,
        )

        # force_cleanup_worktree should only be called once for the shared branch
        assert mock_cleanup.call_count == 1
