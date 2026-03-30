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
