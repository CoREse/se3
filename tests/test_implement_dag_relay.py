"""Tests for DAG relay execution integration in implement step.

Tests the integration of transitive reduction, relay plan, leaf-only merge,
LOC threshold, and enhanced conflict resolution in the implement step.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.dag_scheduler import GroupResult, RelayContext, RelayPlan
from se3.engine.steps.implement import (
    _compute_total_loc,
    _merge_leaf_branch,
    _resolve_leaf_merge_conflicts,
)


# ---------------------------------------------------------------------------
# _compute_total_loc
# ---------------------------------------------------------------------------


class TestComputeTotalLoc:
    def test_empty_groups(self):
        assert _compute_total_loc([]) == 0

    def test_groups_without_estimated_loc(self):
        groups = [
            {"group_id": "G1", "tasks": [{"id": 1}]},
            {"group_id": "G2", "tasks": [{"id": 2}]},
        ]
        assert _compute_total_loc(groups) == 0

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
        groups = [
            {"group_id": "G1", "tasks": [
                {"id": 1, "estimated_loc": 50},
                {"id": 2},  # no estimated_loc
            ]},
        ]
        assert _compute_total_loc(groups) == 50

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

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement._run_single_llm_call")
    @patch("se3.engine.steps.implement._run_dag_parallel")
    @patch("se3.engine.steps.implement.has_commits", return_value=True)
    def test_below_threshold_single_call(
        self, mock_has_commits, mock_dag, mock_single, mock_inj, tmp_path,
    ):
        """Total LOC below threshold routes to single LLM call, not DAG."""
        from se3.engine.models import FlowInstance, Step, StepStatus, StepType
        from se3.engine.steps.implement import implement_handler

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
            },
        )
        flow = FlowInstance(
            task_description="Test",
            change_path=tmp_path / "se3",
        )

        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_single.assert_called_once()
        mock_dag.assert_not_called()

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement._run_dag_parallel")
    @patch("se3.engine.steps.implement.has_commits", return_value=True)
    def test_above_threshold_uses_dag(
        self, mock_has_commits, mock_dag, mock_inj, tmp_path,
    ):
        """Total LOC above threshold routes to DAG parallel."""
        from se3.engine.models import FlowInstance, Step, StepStatus, StepType
        from se3.engine.steps.implement import implement_handler

        mock_dag.return_value = StepStatus.COMPLETED

        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [],
             "tasks": [{"id": 1, "estimated_loc": 200}]},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"],
             "tasks": [{"id": 2, "estimated_loc": 200}]},
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
            change_path=tmp_path / "se3",
        )

        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_called_once()

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement._should_use_dag", return_value=True)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_no_estimated_loc_falls_through(
        self, mock_parse, mock_caller, mock_dag, mock_inj, tmp_path,
    ):
        """Groups without estimated_loc skip LOC check entirely."""
        from se3.engine.models import FlowInstance, Step, StepStatus, StepType
        from se3.engine.steps.implement import implement_handler

        mock_parse.return_value = {
            "files_changed": [], "summary": "ok",
            "completion_status": "complete", "restricted_edits": [],
        }
        mock_caller.return_value.call.return_value = "{}"

        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [],
             "tasks": [{"id": 1}]},  # no estimated_loc
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"],
             "tasks": [{"id": 2}]},
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
            change_path=tmp_path / "se3",
        )

        # Should fall through LOC check and reach sequential path
        # (because _should_use_dag returns True but has_commits not mocked → may error)
        # We only care that _run_single_llm_call is NOT called from LOC path
        # Let it reach the sequential path
        result = implement_handler(step, flow)
        # If it reaches sequential path, LLMCaller is called per-group
        assert mock_caller.call_count == 2


# ---------------------------------------------------------------------------
# _merge_leaf_branch
# ---------------------------------------------------------------------------


class TestMergeLeafBranch:
    """Tests for _merge_leaf_branch function."""

    @patch("se3.engine.steps.implement.get_conflicting_files")
    @patch("se3.engine.steps.implement._resolve_leaf_merge_conflicts")
    @patch("se3.engine.steps.implement._run_git")
    @patch("se3.engine.steps.implement.get_current_branch")
    def test_clean_merge(self, mock_branch, mock_git, mock_resolve, mock_conflict):
        """Clean merge returns True without conflict resolution."""
        mock_branch.return_value = "main"
        mock_git.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = _merge_leaf_branch(
            Path("/repo"), "impl/flow/G3", "main",
            "task desc", [], "spec",
        )

        assert result is True
        mock_resolve.assert_not_called()

    @patch("se3.engine.steps.implement.get_conflicting_files")
    @patch("se3.engine.steps.implement._resolve_leaf_merge_conflicts")
    @patch("se3.engine.steps.implement._run_git")
    @patch("se3.engine.steps.implement.get_current_branch")
    def test_conflict_resolved_by_llm(self, mock_branch, mock_git, mock_resolve, mock_conflict):
        """Conflict resolved by LLM returns True."""
        mock_branch.return_value = "main"
        # First call: merge fails with conflict
        merge_result = MagicMock(returncode=1, stdout="CONFLICT", stderr="")
        # Second+: various git operations
        mock_git.side_effect = [merge_result]  # only the merge call
        mock_conflict.return_value = ["file.py"]
        mock_resolve.return_value = True

        result = _merge_leaf_branch(
            Path("/repo"), "impl/flow/G3", "main",
            "task desc", [{"group_id": "G1", "summary": "s", "files_changed": []}], "spec",
        )

        assert result is True
        mock_resolve.assert_called_once()

    @patch("se3.engine.steps.implement.get_conflicting_files")
    @patch("se3.engine.steps.implement._resolve_leaf_merge_conflicts")
    @patch("se3.engine.steps.implement._run_git")
    @patch("se3.engine.steps.implement.get_current_branch")
    def test_conflict_unresolved_aborts(self, mock_branch, mock_git, mock_resolve, mock_conflict):
        """Unresolved conflict aborts merge and returns False."""
        mock_branch.return_value = "main"
        merge_result = MagicMock(returncode=1, stdout="CONFLICT", stderr="")
        abort_result = MagicMock(returncode=0, stdout="", stderr="")
        mock_git.side_effect = [merge_result, abort_result]
        mock_conflict.return_value = ["file.py"]
        mock_resolve.return_value = False

        result = _merge_leaf_branch(
            Path("/repo"), "impl/flow/G3", "main",
            "task desc", [], "spec",
        )

        assert result is False

    @patch("se3.engine.steps.implement.get_conflicting_files")
    @patch("se3.engine.steps.implement._run_git")
    @patch("se3.engine.steps.implement.get_current_branch")
    def test_non_conflict_failure(self, mock_branch, mock_git, mock_conflict):
        """Non-conflict merge failure aborts and returns False."""
        mock_branch.return_value = "main"
        merge_result = MagicMock(returncode=1, stdout="fatal: error", stderr="not a conflict")
        abort_result = MagicMock(returncode=0, stdout="", stderr="")
        mock_git.side_effect = [merge_result, abort_result]

        result = _merge_leaf_branch(
            Path("/repo"), "impl/flow/G3", "main",
            "task desc", [], "spec",
        )

        assert result is False
        mock_conflict.assert_not_called()

    @patch("se3.engine.steps.implement._run_git")
    @patch("se3.engine.steps.implement.get_current_branch")
    def test_checkout_to_original_branch(self, mock_branch, mock_git):
        """Checks out original_branch if not already there."""
        mock_branch.return_value = "impl/flow/G3"  # Not on original branch
        checkout_result = MagicMock(returncode=0, stdout="", stderr="")
        merge_result = MagicMock(returncode=0, stdout="", stderr="")
        mock_git.side_effect = [checkout_result, merge_result]

        result = _merge_leaf_branch(
            Path("/repo"), "impl/flow/G3", "main",
            "task desc", [], "spec",
        )

        assert result is True
        # First git call should be checkout
        first_call = mock_git.call_args_list[0]
        assert "checkout" in first_call[0]


# ---------------------------------------------------------------------------
# _resolve_leaf_merge_conflicts
# ---------------------------------------------------------------------------


class TestResolveLeafMergeConflicts:
    """Tests for _resolve_leaf_merge_conflicts function."""

    @patch("se3.engine.steps.implement._run_git")
    @patch("se3.engine.steps.implement.LLMCaller")
    def test_successful_resolution(self, mock_caller_cls, mock_git, tmp_path):
        """LLM resolves all conflicts on first attempt."""
        conflict_file = tmp_path / "conflict.py"
        conflict_file.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>>\n")

        mock_caller = MagicMock()
        mock_caller.call.return_value = "resolved content"
        mock_caller_cls.return_value = mock_caller

        mock_git.return_value = MagicMock(returncode=0)

        result = _resolve_leaf_merge_conflicts(
            tmp_path, ["conflict.py"],
            "task desc",
            [{"group_id": "G1", "summary": "did stuff", "files_changed": ["a.py"]}],
            "spec",
        )

        assert result is True
        assert conflict_file.read_text() == "resolved content"

    @patch("se3.engine.steps.implement._run_git")
    @patch("se3.engine.steps.implement.LLMCaller")
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

        result = _resolve_leaf_merge_conflicts(
            tmp_path, ["conflict.py"],
            "task desc", [], "spec",
            max_retries=2,
        )

        assert result is True
        assert mock_caller.call.call_count == 2

    @patch("se3.engine.steps.implement._run_git")
    @patch("se3.engine.steps.implement.LLMCaller")
    def test_all_retries_fail_returns_false(self, mock_caller_cls, mock_git, tmp_path):
        """All retries failing returns False (no --theirs fallback)."""
        conflict_file = tmp_path / "conflict.py"
        conflict_file.write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>>\n")

        mock_caller = MagicMock()
        mock_caller.call.return_value = "<<<<<<< still broken >>>>>>>"
        mock_caller_cls.return_value = mock_caller

        mock_git.return_value = MagicMock(returncode=0)

        result = _resolve_leaf_merge_conflicts(
            tmp_path, ["conflict.py"],
            "task desc", [], "spec",
            max_retries=3,
        )

        assert result is False
        # Should have tried 3 times
        assert mock_caller.call.call_count == 3

    @patch("se3.engine.steps.implement._run_git")
    @patch("se3.engine.steps.implement.LLMCaller")
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

        result = _resolve_leaf_merge_conflicts(
            tmp_path, ["conflict.py"],
            "task desc", [], "spec",
            max_retries=2,
        )

        assert result is True

    @patch("se3.engine.steps.implement.LLMCaller")
    def test_missing_file_returns_false(self, mock_caller_cls, tmp_path):
        """Missing conflict file returns False."""
        result = _resolve_leaf_merge_conflicts(
            tmp_path, ["nonexistent.py"],
            "task desc", [], "spec",
        )

        assert result is False

    @patch("se3.engine.steps.implement._run_git")
    @patch("se3.engine.steps.implement.LLMCaller")
    def test_already_resolved_file_skipped(self, mock_caller_cls, mock_git, tmp_path):
        """Files without conflict markers are skipped."""
        clean_file = tmp_path / "clean.py"
        clean_file.write_text("already resolved content")

        mock_git.return_value = MagicMock(returncode=0)

        result = _resolve_leaf_merge_conflicts(
            tmp_path, ["clean.py"],
            "task desc", [], "spec",
        )

        assert result is True
        # LLM should not be called for already-resolved files
        mock_caller_cls.assert_not_called()

    @patch("se3.engine.steps.implement._run_git")
    @patch("se3.engine.steps.implement.LLMCaller")
    def test_rich_context_in_prompt(self, mock_caller_cls, mock_git, tmp_path):
        """Verify the LLM prompt includes task description and group summaries."""
        conflict_file = tmp_path / "file.py"
        conflict_file.write_text("<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>>\n")

        mock_caller = MagicMock()
        mock_caller.call.return_value = "resolved"
        mock_caller_cls.return_value = mock_caller

        mock_git.return_value = MagicMock(returncode=0)

        _resolve_leaf_merge_conflicts(
            tmp_path, ["file.py"],
            "Implement user auth",
            [
                {"group_id": "G1", "summary": "Added login", "files_changed": ["auth.py"]},
                {"group_id": "G2", "summary": "Added logout", "files_changed": ["auth.py"]},
            ],
            "Use bcrypt for passwords",
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

    @patch("se3.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("se3.engine.steps.implement.get_current_branch", return_value="main")
    @patch("se3.engine.steps.implement.force_cleanup_worktree")
    @patch("se3.engine.steps.implement._salvage_history_from_worktree")
    @patch("se3.engine.steps.implement.DAGScheduler")
    @patch("se3.engine.steps.implement.classify_chains")
    @patch("se3.engine.steps.implement.transitive_reduce")
    @patch("se3.engine.steps.implement.delete_branch")
    def test_transitive_reduce_called(
        self, mock_del, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge,
    ):
        """_run_dag_parallel calls transitive_reduce before classify_chains."""
        from se3.engine.models import FlowInstance, Step, StepStatus, StepType
        from se3.engine.steps.implement import _run_dag_parallel

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
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        mock_reduce.assert_called_once_with(groups)
        mock_classify.assert_called_once_with(reduced_groups)

    @patch("se3.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("se3.engine.steps.implement.get_current_branch", return_value="main")
    @patch("se3.engine.steps.implement.force_cleanup_worktree")
    @patch("se3.engine.steps.implement._salvage_history_from_worktree")
    @patch("se3.engine.steps.implement.DAGScheduler")
    @patch("se3.engine.steps.implement.classify_chains")
    @patch("se3.engine.steps.implement.transitive_reduce")
    @patch("se3.engine.steps.implement.delete_branch")
    def test_relay_plan_passed_to_scheduler(
        self, mock_del, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge,
    ):
        """DAGScheduler receives relay_plan from classify_chains."""
        from se3.engine.models import FlowInstance, Step, StepStatus, StepType
        from se3.engine.steps.implement import _run_dag_parallel

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
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        # DAGScheduler should be created with relay_plan
        mock_sched_cls.assert_called_once()
        call_kwargs = mock_sched_cls.call_args
        assert call_kwargs[1].get("relay_plan") is relay_plan or (
            len(call_kwargs[0]) >= 3 and call_kwargs[0][2] is relay_plan
        )

    @patch("se3.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("se3.engine.steps.implement.get_current_branch", return_value="main")
    @patch("se3.engine.steps.implement.force_cleanup_worktree")
    @patch("se3.engine.steps.implement._salvage_history_from_worktree")
    @patch("se3.engine.steps.implement.DAGScheduler")
    @patch("se3.engine.steps.implement.classify_chains")
    @patch("se3.engine.steps.implement.transitive_reduce")
    @patch("se3.engine.steps.implement.delete_branch")
    def test_only_leaf_branches_merged(
        self, mock_del, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge,
    ):
        """Only leaf node branches are merged back, not intermediate nodes."""
        from se3.engine.models import FlowInstance, Step, StepStatus, StepType
        from se3.engine.steps.implement import _run_dag_parallel

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
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        # Only one merge call (for the leaf G3's branch, which is shared)
        assert mock_merge.call_count == 1

    @patch("se3.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("se3.engine.steps.implement.get_current_branch", return_value="main")
    @patch("se3.engine.steps.implement.force_cleanup_worktree")
    @patch("se3.engine.steps.implement._salvage_history_from_worktree")
    @patch("se3.engine.steps.implement.DAGScheduler")
    @patch("se3.engine.steps.implement.classify_chains")
    @patch("se3.engine.steps.implement.transitive_reduce")
    @patch("se3.engine.steps.implement.delete_branch")
    def test_fallback_leaves_merged_on_failure(
        self, mock_del, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge,
    ):
        """Fallback leaves from partial failure are merged back."""
        from se3.engine.models import FlowInstance, Step, StepStatus, StepType
        from se3.engine.steps.implement import _run_dag_parallel

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
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        # G1 should be merged as a fallback leaf
        assert mock_merge.call_count == 1
        merge_call = mock_merge.call_args
        assert "impl/f/G1" in str(merge_call)

        # Status should be partial (some work preserved)
        assert step.outputs["completion_status"] == "partial"
        assert result == StepStatus.PARTIAL

    @patch("se3.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("se3.engine.steps.implement.get_current_branch", return_value="main")
    @patch("se3.engine.steps.implement.force_cleanup_worktree")
    @patch("se3.engine.steps.implement._salvage_history_from_worktree")
    @patch("se3.engine.steps.implement.DAGScheduler")
    @patch("se3.engine.steps.implement.classify_chains")
    @patch("se3.engine.steps.implement.transitive_reduce")
    @patch("se3.engine.steps.implement.delete_branch")
    def test_worktree_cleanup_deduplicated(
        self, mock_del, mock_reduce, mock_classify, mock_sched_cls,
        mock_salvage, mock_cleanup, mock_branch, mock_merge,
    ):
        """Relay chains sharing worktrees only clean up once."""
        from se3.engine.models import FlowInstance, Step, StepType
        from se3.engine.steps.implement import _run_dag_parallel

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
            task_description="t", task_type="feature", design_section="",
            spec_summary="", injection=None, retry_count=0,
        )

        # force_cleanup_worktree should only be called once for the shared branch
        assert mock_cleanup.call_count == 1
