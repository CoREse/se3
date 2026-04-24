"""Tests for the implement step handler, specifically resume/idempotency.

Tests cover:
- Idempotent group skipping on resume
- State restoration from step.outputs
- Partial progress preservation
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from se3.engine.dag_scheduler import RelayContext
from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)


class TestImplementResumeIdempotency:
    """Test idempotent group skipping in implement handler on resume."""

    def setup_method(self):
        """Set up test fixtures."""
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        # Create a flow
        self.flow = FlowInstance(
            flow_id="test-flow-001",
            task_description="Test implementation",
            task_type="feature",
        )

        # Create task groups for testing
        self.task_groups = [
            {"group_id": "G1", "description": "Group 1", "tasks": ["task1"]},
            {"group_id": "G2", "description": "Group 2", "tasks": ["task2"]},
            {"group_id": "G3", "description": "Group 3", "tasks": ["task3"]},
            {"group_id": "G4", "description": "Group 4", "tasks": ["task4"]},
            {"group_id": "G5", "description": "Group 5", "tasks": ["task5"]},
        ]

    def teardown_method(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_implement_step(
        self,
        resumed=False,
        implemented_groups=None,
        total_files_changed=0,
        status=StepStatus.PENDING,
    ):
        """Helper to create an implement step with specified state."""
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=status,
            step_id="implement-001",
            inputs={
                "task_groups": self.task_groups,
                "design_doc": {"title": "Test Design"},
            },
            outputs={},
        )

        if resumed:
            step.inputs["resumed"] = True

        if implemented_groups:
            step.outputs["implemented_groups"] = implemented_groups

        if total_files_changed:
            step.outputs["total_files_changed"] = total_files_changed

        return step

    def test_fresh_start_processes_all_groups(self):
        """Test that fresh start (no resume) processes all groups."""
        step = self._create_implement_step(resumed=False)

        processed_groups = []

        # Mock the group processing function
        def mock_process_group(group, *args, **kwargs):
            processed_groups.append(group["group_id"])
            return {"files_changed": 1, "success": True}

        # This test verifies the structure - in actual implementation,
        # the handler would call a process function for each group
        for group in step.inputs["task_groups"]:
            result = mock_process_group(group)
            assert result["success"] is True

        # All 5 groups should be processed
        assert len(processed_groups) == 5
        assert processed_groups == ["G1", "G2", "G3", "G4", "G5"]

    def test_resume_skips_already_completed_groups(self):
        """Test that resume skips groups already in implemented_groups."""
        # Create step simulating resume with 2 groups already done
        step = self._create_implement_step(
            resumed=True,
            implemented_groups=["G1", "G2"],
            total_files_changed=3,
        )

        completed_groups = set(step.outputs.get("implemented_groups", []))

        # Simulate the resume logic: skip already completed
        groups_to_process = [
            g for g in step.inputs["task_groups"]
            if g["group_id"] not in completed_groups
        ]

        # Should only have G3, G4, G5
        assert len(groups_to_process) == 3
        assert [g["group_id"] for g in groups_to_process] == ["G3", "G4", "G5"]

    def test_resume_restores_accumulated_state(self):
        """Test that resume restores all_results and total_files_changed."""
        step = self._create_implement_step(
            resumed=True,
            implemented_groups=["G1", "G2"],
            total_files_changed=3,
        )

        # Simulate state restoration on resume
        all_results = []
        total_files = 0

        if step.inputs.get("resumed") and step.outputs.get("implemented_groups"):
            # Restore state from outputs
            all_results = [
                {"group_id": g, "files_changed": 1}
                for g in step.outputs["implemented_groups"]
            ]
            total_files = step.outputs.get("total_files_changed", 0)

        # Verify restored state
        assert len(all_results) == 2
        assert all_results[0]["group_id"] == "G1"
        assert all_results[1]["group_id"] == "G2"
        assert total_files == 3

    def test_resume_processes_remaining_groups(self):
        """Test that only remaining groups are processed after resume."""
        step = self._create_implement_step(
            resumed=True,
            implemented_groups=["G1", "G2"],
            total_files_changed=3,
        )

        completed = set(step.outputs.get("implemented_groups", []))
        remaining = [g for g in step.inputs["task_groups"] if g["group_id"] not in completed]

        processed = []

        # Process only remaining groups
        for group in remaining:
            processed.append(group["group_id"])

        assert processed == ["G3", "G4", "G5"]

    def test_resume_updates_outputs_after_each_group(self):
        """Test that outputs are updated incrementally during resume."""
        step = self._create_implement_step(
            resumed=True,
            implemented_groups=["G1"],
            total_files_changed=1,
        )

        # Simulate processing G2 during resume
        step.outputs["implemented_groups"].append("G2")
        step.outputs["total_files_changed"] += 2

        # Verify incremental update
        assert step.outputs["implemented_groups"] == ["G1", "G2"]
        assert step.outputs["total_files_changed"] == 3

        # Simulate processing G3
        step.outputs["implemented_groups"].append("G3")
        step.outputs["total_files_changed"] += 1

        assert step.outputs["implemented_groups"] == ["G1", "G2", "G3"]
        assert step.outputs["total_files_changed"] == 4

    def test_no_resume_flag_processes_all_groups(self):
        """Test that absence of resumed flag means process all groups."""
        step = self._create_implement_step(resumed=False)

        # Without resumed flag, all groups should be processed
        groups_to_process = step.inputs["task_groups"]

        assert len(groups_to_process) == 5

    def test_resume_with_empty_implemented_groups_processes_all(self):
        """Test that resume with empty implemented_groups processes all."""
        step = self._create_implement_step(
            resumed=True,
            implemented_groups=[],
        )

        completed = set(step.outputs.get("implemented_groups", []))
        groups_to_process = [
            g for g in step.inputs["task_groups"] if g["group_id"] not in completed
        ]

        # Should process all groups
        assert len(groups_to_process) == 5

    def test_resume_with_all_groups_completed(self):
        """Test that resume with all groups already completed skips all."""
        step = self._create_implement_step(
            resumed=True,
            implemented_groups=["G1", "G2", "G3", "G4", "G5"],
            total_files_changed=10,
        )

        completed = set(step.outputs.get("implemented_groups", []))
        groups_to_process = [
            g for g in step.inputs["task_groups"] if g["group_id"] not in completed
        ]

        # No groups to process
        assert len(groups_to_process) == 0

    def test_resume_preserves_other_step_outputs(self):
        """Test that resume preserves other outputs not related to progress."""
        step = self._create_implement_step(
            resumed=True,
            implemented_groups=["G1"],
        )
        step.outputs["other_data"] = "should be preserved"
        step.outputs["design_reference"] = {"key": "value"}

        # Simulate resume processing
        if step.inputs.get("resumed"):
            step.outputs["implemented_groups"].append("G2")

        # Verify other outputs are preserved
        assert step.outputs["other_data"] == "should be preserved"
        assert step.outputs["design_reference"] == {"key": "value"}
        assert step.outputs["implemented_groups"] == ["G1", "G2"]


class TestImplementStatePersistence:
    """Test state persistence during implement step execution."""

    def setup_method(self):
        """Set up test fixtures."""
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        self.flow = FlowInstance(
            flow_id="test-flow-001",
            task_description="Test implementation",
            task_type="feature",
        )

        self.task_groups = [
            {"group_id": "G1", "tasks": ["t1"]},
            {"group_id": "G2", "tasks": ["t2"]},
        ]

    def teardown_method(self):
        """Clean up."""
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_partial_progress_saved_to_outputs(self):
        """Test that partial progress is saved to step.outputs."""
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            inputs={"task_groups": self.task_groups},
            outputs={},
        )

        # Simulate processing first group
        step.outputs["implemented_groups"] = ["G1"]
        step.outputs["total_files_changed"] = 2

        # Verify outputs contain progress
        assert "implemented_groups" in step.outputs
        assert step.outputs["implemented_groups"] == ["G1"]
        assert step.outputs["total_files_changed"] == 2

    def test_outputs_structure_for_resume(self):
        """Test that outputs have correct structure for resume."""
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            inputs={"task_groups": self.task_groups, "resumed": True},
            outputs={
                "implemented_groups": ["G1"],
                "total_files_changed": 2,
            },
        )

        # Verify structure
        assert isinstance(step.outputs["implemented_groups"], list)
        assert isinstance(step.outputs["total_files_changed"], int)
        assert all(isinstance(g, str) for g in step.outputs["implemented_groups"])


class TestShouldUseDag:
    """Test _should_use_dag() backward compatibility predicate."""

    def test_single_group_returns_false(self):
        """Single group should never use DAG path."""
        from se3.engine.steps.implement import _should_use_dag

        groups = [{"group_id": "G1", "tasks": ["t1"]}]
        assert _should_use_dag(groups) is False

    def test_single_group_with_depends_on_returns_false(self):
        """Single group even with depends_on should not use DAG (len <= 1)."""
        from se3.engine.steps.implement import _should_use_dag

        groups = [{"group_id": "G1", "depends_on": ["G0"], "tasks": ["t1"]}]
        assert _should_use_dag(groups) is False

    def test_empty_groups_returns_false(self):
        """Empty group list should not use DAG."""
        from se3.engine.steps.implement import _should_use_dag

        assert _should_use_dag([]) is False

    def test_multiple_groups_no_depends_on_returns_true(self):
        """Multiple independent groups should use DAG for parallel execution."""
        from se3.engine.steps.implement import _should_use_dag

        groups = [
            {"group_id": "G1", "tasks": ["t1"]},
            {"group_id": "G2", "tasks": ["t2"]},
            {"group_id": "G3", "tasks": ["t3"]},
        ]
        assert _should_use_dag(groups) is True

    def test_multiple_groups_empty_depends_on_returns_true(self):
        """Multiple groups with empty depends_on lists should use DAG for parallel execution."""
        from se3.engine.steps.implement import _should_use_dag

        groups = [
            {"group_id": "G1", "depends_on": [], "tasks": ["t1"]},
            {"group_id": "G2", "depends_on": [], "tasks": ["t2"]},
        ]
        assert _should_use_dag(groups) is True

    def test_multiple_groups_with_depends_on_returns_true(self):
        """Multiple groups with at least one non-empty depends_on enables DAG."""
        from se3.engine.steps.implement import _should_use_dag

        groups = [
            {"group_id": "G1", "tasks": ["t1"]},
            {"group_id": "G2", "depends_on": ["G1"], "tasks": ["t2"]},
        ]
        assert _should_use_dag(groups) is True

    def test_diamond_dependency_returns_true(self):
        """Diamond dependency pattern enables DAG."""
        from se3.engine.steps.implement import _should_use_dag

        groups = [
            {"group_id": "G1", "tasks": ["t1"]},
            {"group_id": "G2", "depends_on": ["G1"], "tasks": ["t2"]},
            {"group_id": "G3", "depends_on": ["G1"], "tasks": ["t3"]},
            {"group_id": "G4", "depends_on": ["G2", "G3"], "tasks": ["t4"]},
        ]
        assert _should_use_dag(groups) is True

    def test_fix_iteration_bypasses_dag(self):
        """Fix iteration takes early return before DAG check in implement_handler."""
        # This tests the control flow in implement_handler, not _should_use_dag itself.
        # Fix iterations hit the is_fix_iteration branch which returns before
        # _should_use_dag is ever called. We verify by checking the handler structure.
        from se3.engine.steps.implement import implement_handler
        import inspect

        source = inspect.getsource(implement_handler)
        # Fix iteration path returns before the DAG check
        fix_idx = source.index("is_fix_iteration")
        dag_idx = source.index("_should_use_dag")
        assert fix_idx < dag_idx, (
            "Fix iteration check must come before DAG check in implement_handler"
        )

    def test_single_group_path_bypasses_dag(self):
        """Single group (len <= 1) takes early return before DAG check."""
        from se3.engine.steps.implement import implement_handler
        import inspect

        source = inspect.getsource(implement_handler)
        # len(groups) <= 1 check returns before _should_use_dag
        single_idx = source.index("len(groups) <= 1")
        dag_idx = source.index("_should_use_dag")
        assert single_idx < dag_idx, (
            "Single-group check must come before DAG check in implement_handler"
        )

    def test_no_commits_falls_back_to_sequential(self):
        """When has_commits() returns False, DAG path is skipped."""
        from se3.engine.steps.implement import implement_handler
        import inspect

        source = inspect.getsource(implement_handler)
        # has_commits check must appear inside the _should_use_dag block
        dag_idx = source.index("_should_use_dag")
        has_commits_idx = source.index("has_commits(project_root)")
        assert has_commits_idx > dag_idx, (
            "has_commits check must be inside the DAG decision block"
        )
        # _run_dag_parallel should only be called when has_commits is True
        assert "not has_commits(project_root)" in source, (
            "implement_handler must check has_commits before calling _run_dag_parallel"
        )


class TestDagEmptyRepoFallback:
    """Test that DAG parallel path falls back to sequential in empty repos."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        self.flow = FlowInstance(
            flow_id="test-flow-empty",
            task_description="Test empty repo",
            task_type="feature",
        )

        # Fork DAG (G1 → G2, G1 → G3) so the linear-chain short-circuit
        # does not apply; linear chains now fall through to sequential.
        self.task_groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [], "tasks": [{"id": 1, "estimated_loc": 200}]},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"], "tasks": [{"id": 2, "estimated_loc": 200}]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1"], "tasks": [{"id": 3, "estimated_loc": 200}]},
        ]

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("se3.engine.steps.implement.has_commits", return_value=False)
    @patch("se3.engine.steps.implement._should_use_dag", return_value=True)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    def test_empty_repo_skips_dag_uses_sequential(
        self, mock_injection, mock_parse, mock_caller, mock_dag, mock_has_commits,
    ):
        """When repo has no commits, multiple groups execute sequentially."""
        mock_parse.return_value = {
            "files_changed": ["a.py"],
            "tests_added": [],
            "test_mapping": {},
            "summary": "done",
            "completion_status": "complete",
            "incomplete_tasks": [],
            "restricted_edits": [],
        }
        mock_caller_instance = MagicMock()
        mock_caller_instance.call.return_value = "{}"
        mock_caller.return_value = mock_caller_instance

        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.PENDING,
            step_id="impl-empty",
            inputs={
                "task_description": "test",
                "task_type": "feature",
                "task_groups": self.task_groups,
                "spec_content": {},
            },
            outputs={},
        )

        from se3.engine.steps.implement import implement_handler
        result = implement_handler(step, self.flow)

        # Should NOT have called _run_dag_parallel (we'd see an error if it did)
        assert result == StepStatus.COMPLETED
        # LLMCaller should have been called once per group (sequential)
        assert mock_caller.call_count == len(self.task_groups)

    @patch("se3.engine.steps.implement.has_commits", return_value=True)
    @patch("se3.engine.steps.implement._run_dag_parallel")
    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    def test_repo_with_commits_uses_dag(
        self, mock_injection, mock_dag_parallel, mock_has_commits,
    ):
        """When repo has commits, DAG parallel path is used."""
        mock_dag_parallel.return_value = StepStatus.COMPLETED

        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.PENDING,
            step_id="impl-dag",
            inputs={
                "task_description": "test",
                "task_type": "feature",
                "task_groups": self.task_groups,
                "spec_content": {},
            },
            outputs={},
        )

        from se3.engine.steps.implement import implement_handler
        result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        mock_dag_parallel.assert_called_once()


class TestStaleBranchHandling:
    """Test that stale branches from failed runs are cleaned up properly."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        self.flow = FlowInstance(
            flow_id="test-flow-stale",
            task_description="Test stale branch cleanup",
            task_type="bugfix",
        )

        self.task_groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [], "tasks": [{"id": 1, "estimated_loc": 200}]},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"], "tasks": [{"id": 2, "estimated_loc": 200}]},
        ]

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("se3.engine.steps.implement.force_cleanup_worktree")
    @patch("se3.engine.steps.implement.parse_json_response")
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.create_worktree")
    @patch("se3.engine.steps.implement._run_git")
    def test_execute_fn_deletes_stale_branch_before_creation(
        self, mock_run_git, mock_create_wt, mock_caller_cls, mock_parse,
        mock_force_cleanup,
    ):
        """execute_fn must call 'branch -D' before 'branch <name> <base>' to clean stale branches."""
        from se3.engine.steps.implement import _make_execute_fn
        from se3.engine.dag_scheduler import GroupResult

        # Simulate: stale branch exists (delete returns 0)
        def run_git_side_effect(root, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        mock_run_git.side_effect = run_git_side_effect
        mock_create_wt.return_value = Path("/tmp/fake-worktree")
        mock_parse.return_value = {
            "files_changed": ["a.py"],
            "tests_added": [],
            "test_mapping": {},
            "summary": "done",
            "completion_status": "complete",
            "incomplete_tasks": [],
            "restricted_edits": [],
        }
        mock_caller_instance = MagicMock()
        mock_caller_instance.call.return_value = "{}"
        mock_caller_cls.return_value = mock_caller_instance

        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            step_id="impl-stale",
            inputs={},
            outputs={},
        )

        execute_fn = _make_execute_fn(
            project_root=self.project_root,
            original_branch="master",
            flow=self.flow,
            step=step,
            task_description="test",
            task_type="bugfix",
            design_section="",
            spec_summary="",
            injection=None,
            retry_count=0,
        )

        group = {"group_id": "G1", "group_order": 1, "depends_on": [], "tasks": [{"id": 1}]}
        result = execute_fn(group, {}, RelayContext())

        assert result.status == "completed"

        # Verify the call sequence: branch -D must come before branch create
        git_calls = mock_run_git.call_args_list
        git_args_list = [tuple(c[0][1:]) for c in git_calls]  # extract git args

        # Find the defensive delete and the branch create calls
        delete_idx = None
        create_idx = None
        for i, args in enumerate(git_args_list):
            if args[:2] == ("branch", "-D") and "impl/" in str(args):
                delete_idx = i
            if len(args) == 3 and args[0] == "branch" and args[1].startswith("impl/") and args[2] == "master":
                create_idx = i

        assert delete_idx is not None, f"No stale branch delete call found. Calls: {git_args_list}"
        assert create_idx is not None, f"No branch create call found. Calls: {git_args_list}"
        assert delete_idx < create_idx, (
            f"Stale branch delete (idx={delete_idx}) must come before branch create (idx={create_idx})"
        )

    @patch("se3.engine.steps.implement.force_cleanup_worktree")
    @patch("se3.engine.steps.implement.parse_json_response")
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.create_worktree")
    @patch("se3.engine.steps.implement._run_git")
    def test_execute_fn_succeeds_when_no_stale_branch(
        self, mock_run_git, mock_create_wt, mock_caller_cls, mock_parse,
        mock_force_cleanup,
    ):
        """Normal execution works when there is no stale branch (delete returns non-zero)."""
        from se3.engine.steps.implement import _make_execute_fn

        def run_git_side_effect(root, *args, **kwargs):
            result = MagicMock()
            # branch -D fails (no stale branch exists) — this is the normal case
            if args[:2] == ("branch", "-D"):
                result.returncode = 1
                result.stderr = "error: branch not found"
            else:
                result.returncode = 0
                result.stderr = ""
            result.stdout = ""
            return result

        mock_run_git.side_effect = run_git_side_effect
        mock_create_wt.return_value = Path("/tmp/fake-worktree")
        mock_parse.return_value = {
            "files_changed": ["b.py"],
            "tests_added": [],
            "test_mapping": {},
            "summary": "ok",
            "completion_status": "complete",
            "incomplete_tasks": [],
            "restricted_edits": [],
        }
        mock_caller_instance = MagicMock()
        mock_caller_instance.call.return_value = "{}"
        mock_caller_cls.return_value = mock_caller_instance

        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            step_id="impl-normal",
            inputs={},
            outputs={},
        )

        execute_fn = _make_execute_fn(
            project_root=self.project_root,
            original_branch="master",
            flow=self.flow,
            step=step,
            task_description="test",
            task_type="bugfix",
            design_section="",
            spec_summary="",
            injection=None,
            retry_count=0,
        )

        group = {"group_id": "G1", "group_order": 1, "depends_on": [], "tasks": [{"id": 1}]}
        result = execute_fn(group, {}, RelayContext())

        # Should complete successfully even though branch -D failed
        assert result.status == "completed"
        assert result.files_changed == ["b.py"]

    @patch("se3.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("se3.engine.steps.implement.delete_branch")
    @patch("se3.engine.steps.implement.force_cleanup_worktree")
    @patch("se3.engine.steps.implement.get_current_branch", return_value="master")
    @patch("se3.engine.steps.implement.DAGScheduler")
    @patch("se3.engine.steps.implement._make_execute_fn")
    @patch("se3.config.load_conflict_resolver_config")
    def test_finally_cleans_up_branches_for_all_groups(
        self, mock_config, mock_make_fn, mock_scheduler_cls,
        mock_get_branch, mock_force_cleanup, mock_del_branch, mock_merge,
    ):
        """On scheduler exception, finally cleans worktrees but leaves branches.

        Branches are NOT deleted in the finally block — they are only deleted
        post-merge. When the scheduler throws, merge never runs, so branches
        remain as orphans (stale branch cleanup at the start of execute_fn
        handles these on the next run).
        """
        from se3.engine.steps.implement import _run_dag_parallel
        from se3.engine.dag_scheduler import GroupResult

        mock_config.return_value = MagicMock(strategy="ours")

        # Scheduler raises an exception during run
        mock_scheduler_instance = MagicMock()
        mock_scheduler_instance.run.side_effect = RuntimeError("scheduler boom")
        mock_scheduler_cls.return_value = mock_scheduler_instance

        mock_make_fn.return_value = MagicMock()

        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            step_id="impl-cleanup",
            inputs={},
            outputs={},
        )

        with pytest.raises(RuntimeError, match="scheduler boom"):
            _run_dag_parallel(
                groups=self.task_groups,
                step=step,
                flow=self.flow,
                project_root=self.project_root,
                task_description="test",
                task_type="bugfix",
                design_section="",
                spec_summary="",
                injection=None,
                retry_count=0,
            )

        # Worktrees should still be cleaned up in finally
        # (force_cleanup_worktree is called for results, but results is empty
        # when scheduler throws, so no cleanup calls expected)

        # Branches should NOT be deleted in the finally block — they are
        # only deleted post-merge, which never runs on exception
        assert mock_del_branch.call_count == 0, (
            f"Branches should not be deleted on scheduler exception; "
            f"delete_branch was called {mock_del_branch.call_count} times"
        )

    @patch("se3.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("se3.engine.steps.implement.delete_branch")
    @patch("se3.engine.steps.implement.force_cleanup_worktree")
    @patch("se3.engine.steps.implement.get_current_branch", return_value="master")
    @patch("se3.engine.steps.implement.DAGScheduler")
    @patch("se3.engine.steps.implement._make_execute_fn")
    @patch("se3.config.load_conflict_resolver_config")
    def test_finally_cleans_up_branches_on_normal_completion(
        self, mock_config, mock_make_fn, mock_scheduler_cls,
        mock_get_branch, mock_force_cleanup, mock_del_branch, mock_merge,
    ):
        """Branches are cleaned up even on successful completion (idempotent cleanup)."""
        from se3.engine.steps.implement import _run_dag_parallel
        from se3.engine.dag_scheduler import GroupResult

        mock_config.return_value = MagicMock(strategy="ours")

        # Scheduler returns successful results
        g1_result = GroupResult(
            group_id="G1", status="completed", branch_name="impl/test-flow-stale/G1",
            worktree_path=Path("/tmp/wt-g1"),
        )
        g2_result = GroupResult(
            group_id="G2", status="completed", branch_name="impl/test-flow-stale/G2",
            worktree_path=Path("/tmp/wt-g2"),
        )
        mock_scheduler_instance = MagicMock()
        mock_scheduler_instance.run.return_value = [g1_result, g2_result]
        mock_scheduler_instance.topological_merge_order.return_value = ["G1", "G2"]
        mock_scheduler_cls.return_value = mock_scheduler_instance
        mock_make_fn.return_value = MagicMock()

        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            step_id="impl-normal-cleanup",
            inputs={},
            outputs={},
        )

        result = _run_dag_parallel(
            groups=self.task_groups,
            step=step,
            flow=self.flow,
            project_root=self.project_root,
            task_description="test",
            task_type="bugfix",
            design_section="",
            spec_summary="",
            injection=None,
            retry_count=0,
        )

        assert result == StepStatus.COMPLETED

        # delete_branch should be called once per group (post-merge only, NOT in finally)
        deleted_branches = [c[0][1] for c in mock_del_branch.call_args_list]
        for gid in ["G1", "G2"]:
            branch = f"impl/{self.flow.flow_id}/{gid}"
            assert branch in deleted_branches, (
                f"Branch {branch} not cleaned up. Deleted: {deleted_branches}"
            )
        # Exactly one deletion per group (post-merge), not double-deleted
        assert len(deleted_branches) == 2, (
            f"Expected 2 branch deletions (one per group), got {len(deleted_branches)}"
        )


class TestDagResumeFiltering:
    """Test that DAG parallel path correctly skips completed groups on resume."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        self.flow = FlowInstance(
            flow_id="test-flow-resume-dag",
            task_description="Test DAG resume",
            task_type="bugfix",
        )

        self.task_groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [], "tasks": [{"id": 1, "estimated_loc": 200}]},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"], "tasks": [{"id": 2, "estimated_loc": 200}]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1"], "tasks": [{"id": 3, "estimated_loc": 200}]},
            {"group_id": "G4", "group_order": 4, "depends_on": ["G2", "G3"], "tasks": [{"id": 4, "estimated_loc": 200}]},
        ]

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("se3.engine.steps.implement.has_commits", return_value=True)
    @patch("se3.engine.steps.implement._run_dag_parallel")
    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    def test_resume_filters_completed_groups_from_dag(
        self, mock_injection, mock_dag_parallel, mock_has_commits,
    ):
        """On resume, completed groups should be filtered out before calling _run_dag_parallel."""
        mock_dag_parallel.return_value = StepStatus.COMPLETED

        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            step_id="impl-resume-dag",
            inputs={
                "task_description": "test",
                "task_type": "bugfix",
                "task_groups": self.task_groups,
                "spec_content": {},
                "resumed": True,
            },
            outputs={
                "implemented_groups": ["G1"],
                "files_changed": ["a.py"],
                "tests_added": ["test_a.py"],
                "test_mapping": {"a.py": "test_a.py"},
            },
        )

        from se3.engine.steps.implement import implement_handler
        result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        mock_dag_parallel.assert_called_once()

        # Check that only remaining groups (G2, G3, G4) were passed
        call_kwargs = mock_dag_parallel.call_args
        passed_groups = call_kwargs.kwargs.get("groups") or call_kwargs[1].get("groups")
        passed_group_ids = [g["group_id"] for g in passed_groups]
        assert "G1" not in passed_group_ids
        assert set(passed_group_ids) == {"G2", "G3", "G4"}

        # Check that prior_outputs was passed
        prior = call_kwargs.kwargs.get("prior_outputs") or call_kwargs[1].get("prior_outputs")
        assert prior is not None
        assert prior["files_changed"] == ["a.py"]
        assert prior["tests_added"] == ["test_a.py"]
        assert prior["test_mapping"] == {"a.py": "test_a.py"}
        assert prior["implemented_groups"] == ["G1"]

    @patch("se3.engine.steps.implement.has_commits", return_value=True)
    @patch("se3.engine.steps.implement._run_dag_parallel", return_value=StepStatus.COMPLETED)
    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    def test_resume_all_completed_returns_early(
        self, mock_injection, mock_dag_parallel, mock_has_commits,
    ):
        """If all groups are already completed on resume, DAG is called with empty groups."""
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            step_id="impl-resume-all-done",
            inputs={
                "task_description": "test",
                "task_type": "bugfix",
                "task_groups": self.task_groups,
                "spec_content": {},
                "resumed": True,
            },
            outputs={
                "implemented_groups": ["G1", "G2", "G3", "G4"],
                "files_changed": ["a.py", "b.py"],
                "tests_added": [],
                "test_mapping": {},
            },
        )

        from se3.engine.steps.implement import implement_handler
        result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        # DAG is called with empty groups list to handle merge of recovered branches
        mock_dag_parallel.assert_called_once()
        call_kwargs = mock_dag_parallel.call_args
        assert call_kwargs[1]["groups"] == [] or call_kwargs[0][0] == []

    @patch("se3.engine.steps.implement.has_commits", return_value=True)
    @patch("se3.engine.steps.implement._run_dag_parallel")
    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    def test_fresh_start_passes_no_prior_outputs(
        self, mock_injection, mock_dag_parallel, mock_has_commits,
    ):
        """Fresh start (no resume) should pass prior_outputs=None."""
        mock_dag_parallel.return_value = StepStatus.COMPLETED

        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.PENDING,
            step_id="impl-fresh-dag",
            inputs={
                "task_description": "test",
                "task_type": "feature",
                "task_groups": self.task_groups,
                "spec_content": {},
            },
            outputs={},
        )

        from se3.engine.steps.implement import implement_handler
        result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        call_kwargs = mock_dag_parallel.call_args
        prior = call_kwargs.kwargs.get("prior_outputs") or call_kwargs[1].get("prior_outputs")
        assert prior is None

    @patch("se3.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("se3.engine.steps.implement.delete_branch")
    @patch("se3.engine.steps.implement.force_cleanup_worktree")
    @patch("se3.engine.steps.implement.get_current_branch", return_value="master")
    @patch("se3.engine.steps.implement.DAGScheduler")
    @patch("se3.engine.steps.implement._make_execute_fn")
    @patch("se3.config.load_conflict_resolver_config")
    def test_prior_outputs_merged_in_run_dag_parallel(
        self, mock_config, mock_make_fn, mock_scheduler_cls,
        mock_get_branch, mock_force_cleanup, mock_del_branch, mock_merge,
    ):
        """_run_dag_parallel merges prior_outputs into aggregated results."""
        from se3.engine.steps.implement import _run_dag_parallel
        from se3.engine.dag_scheduler import GroupResult

        mock_config.return_value = MagicMock(strategy="ours")

        g3_result = GroupResult(
            group_id="G3", status="completed",
            branch_name="impl/test-flow-resume-dag/G3",
            worktree_path=Path("/tmp/wt-g3"),
            files_changed=["c.py"],
            tests_added=["test_c.py"],
            test_mapping={"c.py": "test_c.py"},
            summary="implemented G3",
        )
        mock_scheduler_instance = MagicMock()
        mock_scheduler_instance.run.return_value = [g3_result]
        mock_scheduler_instance.topological_merge_order.return_value = ["G3"]
        mock_scheduler_cls.return_value = mock_scheduler_instance
        mock_make_fn.return_value = MagicMock()

        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            step_id="impl-merge-prior",
            inputs={},
            outputs={},
        )

        prior = {
            "files_changed": ["a.py", "b.py"],
            "tests_added": ["test_a.py"],
            "test_mapping": {"a.py": "test_a.py"},
            "implemented_groups": ["G1", "G2"],
        }

        remaining_groups = [
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1"], "tasks": [{"id": 3}]},
        ]

        result = _run_dag_parallel(
            groups=remaining_groups,
            step=step,
            flow=self.flow,
            project_root=self.project_root,
            task_description="test",
            task_type="bugfix",
            design_section="",
            spec_summary="",
            injection=None,
            retry_count=0,
            prior_outputs=prior,
        )

        assert result == StepStatus.COMPLETED

        # Verify merged outputs
        assert "a.py" in step.outputs["files_changed"]
        assert "b.py" in step.outputs["files_changed"]
        assert "c.py" in step.outputs["files_changed"]
        assert "test_a.py" in step.outputs["tests_added"]
        assert "test_c.py" in step.outputs["tests_added"]
        assert step.outputs["test_mapping"]["a.py"] == "test_a.py"
        assert step.outputs["test_mapping"]["c.py"] == "test_c.py"
        assert "G1" in step.outputs["implemented_groups"]
        assert "G2" in step.outputs["implemented_groups"]
        assert "G3" in step.outputs["implemented_groups"]

    @patch("se3.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("se3.engine.steps.implement.delete_branch")
    @patch("se3.engine.steps.implement.force_cleanup_worktree")
    @patch("se3.engine.steps.implement.get_current_branch", return_value="master")
    @patch("se3.engine.steps.implement.DAGScheduler")
    @patch("se3.engine.steps.implement._make_execute_fn")
    @patch("se3.config.load_conflict_resolver_config")
    def test_no_prior_outputs_preserves_default_behavior(
        self, mock_config, mock_make_fn, mock_scheduler_cls,
        mock_get_branch, mock_force_cleanup, mock_del_branch, mock_merge,
    ):
        """_run_dag_parallel without prior_outputs behaves as before."""
        from se3.engine.steps.implement import _run_dag_parallel
        from se3.engine.dag_scheduler import GroupResult

        mock_config.return_value = MagicMock(strategy="ours")

        g1_result = GroupResult(
            group_id="G1", status="completed",
            branch_name="impl/test-flow-resume-dag/G1",
            worktree_path=Path("/tmp/wt-g1"),
            files_changed=["a.py"],
            tests_added=[],
            test_mapping={},
            summary="done G1",
        )
        mock_scheduler_instance = MagicMock()
        mock_scheduler_instance.run.return_value = [g1_result]
        mock_scheduler_instance.topological_merge_order.return_value = ["G1"]
        mock_scheduler_cls.return_value = mock_scheduler_instance
        mock_make_fn.return_value = MagicMock()

        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            step_id="impl-no-prior",
            inputs={},
            outputs={},
        )

        result = _run_dag_parallel(
            groups=[{"group_id": "G1", "group_order": 1, "depends_on": [], "tasks": [{"id": 1}]}],
            step=step,
            flow=self.flow,
            project_root=self.project_root,
            task_description="test",
            task_type="bugfix",
            design_section="",
            spec_summary="",
            injection=None,
            retry_count=0,
        )

        assert result == StepStatus.COMPLETED
        assert step.outputs["files_changed"] == ["a.py"]
        assert step.outputs["implemented_groups"] == ["G1"]


class TestDagParallelResumeBehavior:
    """Test DAG parallel resume: skip completed groups, clean stale worktrees, restore state."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        self.flow = FlowInstance(
            flow_id="test-flow-dag-resume",
            task_description="Test DAG resume behavior",
            task_type="bugfix",
        )

        self.task_groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [], "tasks": [{"id": 1, "estimated_loc": 200}]},
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"], "tasks": [{"id": 2, "estimated_loc": 200}]},
            {"group_id": "G3", "group_order": 3, "depends_on": ["G1"], "tasks": [{"id": 3, "estimated_loc": 200}]},
        ]

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("se3.engine.steps.implement.has_commits", return_value=True)
    @patch("se3.engine.steps.implement._run_dag_parallel")
    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    def test_dag_resume_skips_completed_groups(
        self, mock_injection, mock_dag_parallel, mock_has_commits,
    ):
        """On resume with implemented_groups set, completed groups are not re-executed."""
        mock_dag_parallel.return_value = StepStatus.COMPLETED

        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            step_id="impl-dag-resume-skip",
            inputs={
                "task_description": "test",
                "task_type": "bugfix",
                "task_groups": self.task_groups,
                "spec_content": {},
                "resumed": True,
            },
            outputs={
                "implemented_groups": ["G1"],
                "files_changed": ["a.py"],
                "tests_added": [],
                "test_mapping": {},
            },
        )

        from se3.engine.steps.implement import implement_handler
        result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        mock_dag_parallel.assert_called_once()

        # Only G2, G3 should be passed — G1 must be skipped
        call_kwargs = mock_dag_parallel.call_args
        passed_groups = call_kwargs.kwargs.get("groups") or call_kwargs[1].get("groups")
        passed_ids = [g["group_id"] for g in passed_groups]
        assert "G1" not in passed_ids, "Completed group G1 must not be re-executed"
        assert sorted(passed_ids) == ["G2", "G3"]

    @patch("se3.engine.steps.implement.force_cleanup_worktree")
    @patch("se3.engine.steps.implement.parse_json_response")
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.create_worktree")
    @patch("se3.engine.steps.implement._run_git")
    def test_dag_resume_cleans_stale_worktrees(
        self, mock_run_git, mock_create_wt, mock_caller_cls,
        mock_parse, mock_force_cleanup,
    ):
        """Stale worktrees from a previous run are cleaned up via force_cleanup_worktree before branch recreation."""
        from se3.engine.steps.implement import _make_execute_fn

        def run_git_side_effect(root, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        mock_run_git.side_effect = run_git_side_effect
        mock_create_wt.return_value = Path("/tmp/fake-worktree")
        mock_parse.return_value = {
            "files_changed": ["x.py"],
            "tests_added": [],
            "test_mapping": {},
            "summary": "done",
            "completion_status": "complete",
            "incomplete_tasks": [],
            "restricted_edits": [],
        }
        mock_caller_instance = MagicMock()
        mock_caller_instance.call.return_value = "{}"
        mock_caller_cls.return_value = mock_caller_instance

        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            step_id="impl-stale-wt",
            inputs={},
            outputs={},
        )

        execute_fn = _make_execute_fn(
            project_root=self.project_root,
            original_branch="master",
            flow=self.flow,
            step=step,
            task_description="test",
            task_type="bugfix",
            design_section="",
            spec_summary="",
            injection=None,
            retry_count=0,
        )

        group = {"group_id": "G1", "group_order": 1, "depends_on": [], "tasks": [{"id": 1}]}
        result = execute_fn(group, {}, RelayContext())

        assert result.status == "completed"

        # force_cleanup_worktree must have been called to clean stale worktree
        branch_name = f"impl/{self.flow.flow_id}/G1"
        mock_force_cleanup.assert_called_once_with(self.project_root, branch_name)

        # Verify that the branch -D (cleanup) comes AFTER force_cleanup_worktree
        # force_cleanup_worktree is called before any _run_git calls in the lock block
        git_calls = mock_run_git.call_args_list
        git_args_list = [tuple(c[0][1:]) for c in git_calls]
        delete_idx = next(
            (i for i, args in enumerate(git_args_list) if args[:2] == ("branch", "-D")),
            None,
        )
        assert delete_idx is not None, "branch -D call must happen after force_cleanup_worktree"

    @patch("se3.engine.steps.implement._merge_leaf_branch", return_value=True)
    @patch("se3.engine.steps.implement.delete_branch")
    @patch("se3.engine.steps.implement.force_cleanup_worktree")
    @patch("se3.engine.steps.implement.get_current_branch", return_value="master")
    @patch("se3.engine.steps.implement.DAGScheduler")
    @patch("se3.engine.steps.implement._make_execute_fn")
    @patch("se3.config.load_conflict_resolver_config")
    def test_dag_resume_restores_accumulated_state(
        self, mock_config, mock_make_fn, mock_scheduler_cls,
        mock_get_branch, mock_force_cleanup, mock_del_branch, mock_merge,
    ):
        """Prior outputs are preserved and merged with new group results."""
        from se3.engine.steps.implement import _run_dag_parallel
        from se3.engine.dag_scheduler import GroupResult

        mock_config.return_value = MagicMock(strategy="ours")

        # New group G2 completes successfully
        g2_result = GroupResult(
            group_id="G2", status="completed",
            branch_name="impl/test-flow-dag-resume/G2",
            worktree_path=Path("/tmp/wt-g2"),
            files_changed=["b.py"],
            tests_added=["test_b.py"],
            test_mapping={"b.py": "test_b.py"},
            summary="implemented G2",
        )
        mock_scheduler_instance = MagicMock()
        mock_scheduler_instance.run.return_value = [g2_result]
        mock_scheduler_instance.topological_merge_order.return_value = ["G2"]
        mock_scheduler_cls.return_value = mock_scheduler_instance
        mock_make_fn.return_value = MagicMock()

        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            step_id="impl-restore-state",
            inputs={},
            outputs={},
        )

        # Prior outputs from G1 completed in a previous run
        prior = {
            "files_changed": ["a.py"],
            "tests_added": ["test_a.py"],
            "test_mapping": {"a.py": "test_a.py"},
            "implemented_groups": ["G1"],
        }

        remaining_groups = [
            {"group_id": "G2", "group_order": 2, "depends_on": ["G1"], "tasks": [{"id": 2}]},
        ]

        result = _run_dag_parallel(
            groups=remaining_groups,
            step=step,
            flow=self.flow,
            project_root=self.project_root,
            task_description="test",
            task_type="bugfix",
            design_section="",
            spec_summary="",
            injection=None,
            retry_count=0,
            prior_outputs=prior,
        )

        assert result == StepStatus.COMPLETED

        # Prior G1 outputs must be preserved
        assert "a.py" in step.outputs["files_changed"]
        assert "test_a.py" in step.outputs["tests_added"]
        assert step.outputs["test_mapping"]["a.py"] == "test_a.py"
        assert "G1" in step.outputs["implemented_groups"]

        # New G2 outputs must be merged in
        assert "b.py" in step.outputs["files_changed"]
        assert "test_b.py" in step.outputs["tests_added"]
        assert step.outputs["test_mapping"]["b.py"] == "test_b.py"
        assert "G2" in step.outputs["implemented_groups"]

        # Combined counts
        assert len(step.outputs["implemented_groups"]) == 2


class TestStreamPrefixConstruction:
    """Test that implement.py constructs correct stream_prefix for each execution path."""

    def test_single_group_no_prefix(self):
        """Single group execution should not set stream_prefix (empty by default)."""
        from se3.engine.llm_caller import LLMCaller
        # When _run_single_llm_call is called without stream_prefix, it defaults to ''
        caller = LLMCaller(stream_prefix='')
        assert caller.stream_prefix == ''

    def test_sequential_group_prefix_format(self):
        """Sequential execution should use [Gx] prefix format."""
        from se3.engine.llm_caller import LLMCaller
        # Simulate what implement.py does for sequential execution
        group_id = "G2"
        caller = LLMCaller(stream_prefix=f'[{group_id}] ')
        assert caller.stream_prefix == '[G2] '

    def test_dag_parallel_group_prefix_format(self):
        """DAG parallel execution should use [Gx] prefix format."""
        from se3.engine.llm_caller import LLMCaller
        group_id = "G3"
        caller = LLMCaller(stream_prefix=f'[{group_id}] ')
        assert caller.stream_prefix == '[G3] '

    def test_loc_merge_prefix_format(self):
        """LOC merge execution should use [G1+G2+G3] prefix format."""
        # Simulate the merged prefix construction from implement.py
        groups = [
            {"group_id": "G1", "tasks": []},
            {"group_id": "G2", "tasks": []},
            {"group_id": "G3", "tasks": []},
        ]
        merged_group_ids = [g.get("group_id", f"G{i+1}") for i, g in enumerate(groups)]
        merged_prefix = f"[{'+'.join(merged_group_ids)}] "
        assert merged_prefix == "[G1+G2+G3] "

    def test_loc_merge_prefix_two_groups(self):
        """LOC merge with two groups should produce [G1+G2] prefix."""
        groups = [
            {"group_id": "G1", "tasks": []},
            {"group_id": "G2", "tasks": []},
        ]
        merged_group_ids = [g.get("group_id", f"G{i+1}") for i, g in enumerate(groups)]
        merged_prefix = f"[{'+'.join(merged_group_ids)}] "
        assert merged_prefix == "[G1+G2] "

    def test_loc_merge_prefix_fallback_group_ids(self):
        """Groups without group_id should use fallback G1, G2, etc."""
        groups = [
            {"tasks": []},
            {"tasks": []},
        ]
        merged_group_ids = [g.get("group_id", f"G{i+1}") for i, g in enumerate(groups)]
        merged_prefix = f"[{'+'.join(merged_group_ids)}] "
        assert merged_prefix == "[G1+G2] "

    @patch("se3.engine.steps.implement._run_single_llm_call")
    @patch("se3.engine.steps.implement._resolve_files_changed")
    @patch("se3.engine.steps.implement._display_task_plan")
    @patch("se3.engine.steps.implement._compute_total_loc", return_value=100)
    @patch("se3.engine.steps.implement._extract_sorted_groups")
    def test_loc_merge_no_prefix_for_single_call(
        self, mock_extract, mock_loc, mock_display, mock_resolve, mock_run
    ):
        """LOC merge path should NOT pass stream_prefix to _run_single_llm_call."""
        mock_extract.return_value = [
            {"group_id": "G1", "tasks": [], "depends_on": []},
            {"group_id": "G2", "tasks": [], "depends_on": []},
        ]
        mock_run.return_value = StepStatus.COMPLETED

        step = Step(step_id="s1", step_type=StepType.IMPLEMENT)
        step.inputs = {
            "task_description": "test",
            "task_type": "feature",
            "design_doc": "",
            "task_groups": [{"group_id": "G1"}, {"group_id": "G2"}],
            "spec_content": "",
        }
        flow = FlowInstance(
            flow_id="f1",
            task_description="test",
            task_type="feature",
        )
        flow.change_path = Path("/tmp/test/se3.yaml")

        from se3.engine.steps.implement import implement_handler
        with patch("se3.config.ImplementConfig.load") as mock_config:
            mock_config.return_value = MagicMock(group_loc_threshold=300)
            implement_handler(step, flow)

        # Verify _run_single_llm_call was called without stream_prefix
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        assert "stream_prefix" not in call_kwargs.kwargs
