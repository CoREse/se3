"""Tests for Bug B (CONFIRM REVISION_NEEDED loop), Bug C (already_confirmed detection),
and the _write_flow_meta feature."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.persistence import PersistenceManager
from se3.engine.state_machine import StateMachine


class TestBugB_ConfirmRevisionNoInfiniteLoop:
    """Bug B: CONFIRM + REVISION_NEEDED must not loop infinitely.

    Before the fix, the `continue` statement caused the run() loop to
    re-execute the same CONFIRM step instead of falling through to
    transition_to_next() which handles _transition_to_revision.
    """

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        self.persistence = PersistenceManager(self.project_root)
        self.sm = StateMachine(self.project_root, self.persistence)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_confirm_revision_needed_transitions_to_reviewed_step(self):
        """When CONFIRM returns REVISION_NEEDED, flow should transition back
        to the reviewed step via transition_to_next, not loop on CONFIRM."""
        flow = FlowInstance(
            task_description="Test",
            task_type="feature",
        )
        flow.state.selected_steps = [
            StepType.ANALYZE,
            StepType.PROPOSE,
            StepType.CONFIRM,
            StepType.DESIGN,
        ]

        # Add completed PROPOSE step
        propose_step = Step(
            step_type=StepType.PROPOSE,
            status=StepStatus.COMPLETED,
            step_id="propose-001",
            outputs={"proposal": "Test proposal"},
        )
        flow.state.add_step(propose_step)

        # Add CONFIRM step that returns REVISION_NEEDED
        confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            step_id="confirm-001",
            inputs={
                "step_to_review_id": "propose-001",
                "step_to_review_type": "propose",
                "reviewer": "human",
            },
        )
        flow.state.add_step(confirm_step)
        flow.state.current_step_id = "confirm-001"
        flow.state.current_step_index = 2  # At CONFIRM

        # Mock the confirm handler: first call rejects, second approves
        confirm_call_count = 0

        def mock_confirm_handler(step, fl):
            nonlocal confirm_call_count
            confirm_call_count += 1
            if confirm_call_count == 1:
                step.outputs["review_result"] = {
                    "approved": False,
                    "feedback": "Please revise",
                    "step_to_review_id": "propose-001",
                    "step_to_review_type": "propose",
                }
                step.outputs["revision_feedback"] = "Please revise"
                return StepStatus.REVISION_NEEDED
            else:
                step.outputs["review_result"] = {
                    "approved": True,
                    "feedback": "Looks good now",
                    "step_to_review_id": "propose-001",
                    "step_to_review_type": "propose",
                }
                return StepStatus.COMPLETED

        # Mock the propose handler for when it gets re-executed
        propose_call_count = 0

        def mock_propose_handler(step, fl):
            nonlocal propose_call_count
            propose_call_count += 1
            step.outputs["proposal"] = "Revised proposal"
            return StepStatus.COMPLETED

        self.sm.register_handler(StepType.CONFIRM, mock_confirm_handler)
        self.sm.register_handler(StepType.PROPOSE, mock_propose_handler)
        # Register a design handler so the flow can complete
        self.sm.register_handler(StepType.DESIGN, lambda s, f: StepStatus.COMPLETED)

        flow.status = FlowStatus.RUNNING

        # Run the flow — before the fix, this would infinite-loop on CONFIRM
        result = self.sm.run(flow)

        # The propose step should have been re-executed (revision)
        assert propose_call_count >= 1, "PROPOSE step should have been re-run after revision"

        # Flow should not have hit max iterations (the old bug)
        assert result != FlowStatus.FAILED, "Flow should not fail from infinite loop"

    def test_confirm_revision_calls_transition_to_next(self):
        """Verify that transition_to_next is called (not skipped) when
        CONFIRM returns REVISION_NEEDED."""
        flow = FlowInstance(
            task_description="Test",
            task_type="feature",
        )
        flow.state.selected_steps = [
            StepType.PROPOSE,
            StepType.CONFIRM,
            StepType.DESIGN,
        ]

        propose_step = Step(
            step_type=StepType.PROPOSE,
            status=StepStatus.COMPLETED,
            step_id="propose-001",
        )
        flow.state.add_step(propose_step)

        confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.REVISION_NEEDED,
            step_id="confirm-001",
            outputs={
                "review_result": {
                    "approved": False,
                    "feedback": "Revise",
                    "step_to_review_id": "propose-001",
                    "step_to_review_type": "propose",
                },
                "revision_feedback": "Revise",
            },
        )
        flow.state.add_step(confirm_step)
        flow.state.current_step_id = "confirm-001"
        flow.state.current_step_index = 1

        # Call transition_to_next directly
        result = self.sm.transition_to_next(flow)

        # Should transition back to propose step for revision
        assert result is not None
        assert result.step_id == "propose-001"
        assert result.status == StepStatus.PENDING


class TestBugC_AlreadyConfirmedDetection:
    """Bug C: already_confirmed detection must read from the nested
    review_result structure, not from top-level outputs."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        self.persistence = PersistenceManager(self.project_root)
        self.sm = StateMachine(self.project_root, self.persistence)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_already_confirmed_detected_with_nested_review_result(self):
        """_build_step_inputs should detect already_confirmed when
        review_result contains matching step_to_review_id."""
        flow = FlowInstance(
            task_description="Test",
            task_type="feature",
        )
        flow.state.selected_steps = [
            StepType.PROPOSE,
            StepType.CONFIRM,
            StepType.DESIGN,
            StepType.CONFIRM,
        ]

        # Add completed PROPOSE step
        propose_step = Step(
            step_type=StepType.PROPOSE,
            status=StepStatus.COMPLETED,
            step_id="propose-001",
            outputs={"proposal": "Test proposal"},
        )
        flow.state.add_step(propose_step)

        # Add completed CONFIRM step with nested review_result
        # (this is the actual structure written by confirm_handler)
        confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.COMPLETED,
            step_id="confirm-001",
            outputs={
                "review_result": {
                    "approved": True,
                    "feedback": "Looks good",
                    "step_to_review_id": "propose-001",
                    "step_to_review_type": "propose",
                },
            },
        )
        flow.state.add_step(confirm_step)

        # Add completed DESIGN step
        design_step = Step(
            step_type=StepType.DESIGN,
            status=StepStatus.COMPLETED,
            step_id="design-001",
            outputs={"design_doc": "Test design"},
        )
        flow.state.add_step(design_step)

        # Build inputs for the second CONFIRM step
        inputs = self.sm._build_step_inputs(flow, StepType.CONFIRM)

        # The second CONFIRM should review DESIGN, not PROPOSE
        # (because PROPOSE is already confirmed)
        assert inputs.get("step_to_review_id") == "design-001"
        assert inputs.get("step_to_review_type") == "design"

    def test_not_yet_confirmed_step_is_found(self):
        """A step that has NOT been confirmed should be selected for review."""
        flow = FlowInstance(
            task_description="Test",
            task_type="feature",
        )
        flow.state.selected_steps = [
            StepType.PROPOSE,
            StepType.CONFIRM,
        ]

        # Add completed PROPOSE step (no prior CONFIRM for it)
        propose_step = Step(
            step_type=StepType.PROPOSE,
            status=StepStatus.COMPLETED,
            step_id="propose-001",
            outputs={"proposal": "Test proposal"},
        )
        flow.state.add_step(propose_step)

        inputs = self.sm._build_step_inputs(flow, StepType.CONFIRM)

        assert inputs.get("step_to_review_id") == "propose-001"


class TestWriteFlowMeta:
    """Test the _write_flow_meta feature that records version info."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        self.persistence = PersistenceManager(self.project_root)
        self.sm = StateMachine(self.project_root, self.persistence)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_meta_json_created_with_correct_fields(self):
        """_write_flow_meta should create _meta.json with the three required fields."""
        flow = FlowInstance(
            task_description="Test",
            task_type="feature",
        )

        self.sm._write_flow_meta(flow)

        from se3.engine.chat_history import _history_dir
        meta_path = _history_dir(self.project_root, flow.flow_id) / "_meta.json"

        assert meta_path.exists()

        with open(meta_path) as f:
            meta = json.load(f)

        assert "se3_version" in meta
        assert "python_version" in meta
        assert "created_at" in meta

        # Validate types
        assert isinstance(meta["se3_version"], str)
        assert isinstance(meta["python_version"], str)
        assert isinstance(meta["created_at"], str)

        # Validate created_at is valid ISO format
        datetime.fromisoformat(meta["created_at"])

        # Validate python_version matches current runtime
        assert meta["python_version"] == sys.version

    def test_meta_json_not_overwritten_on_resume(self):
        """_write_flow_meta should skip if _meta.json already exists (resume scenario)."""
        flow = FlowInstance(
            task_description="Test",
            task_type="feature",
        )

        # First write
        self.sm._write_flow_meta(flow)

        from se3.engine.chat_history import _history_dir
        meta_path = _history_dir(self.project_root, flow.flow_id) / "_meta.json"

        # Read original content
        with open(meta_path) as f:
            original_meta = json.load(f)

        # Second write (simulating resume)
        self.sm._write_flow_meta(flow)

        # Content should be unchanged
        with open(meta_path) as f:
            resumed_meta = json.load(f)

        assert original_meta == resumed_meta

    def test_meta_json_written_during_run(self):
        """run() should call _write_flow_meta before the main loop."""
        flow = FlowInstance(
            task_description="Test",
            task_type="feature",
        )
        flow.state.selected_steps = [StepType.ANALYZE]

        first_step = Step(
            step_type=StepType.ANALYZE,
            status=StepStatus.PENDING,
        )
        flow.state.add_step(first_step)
        flow.state.current_step_id = first_step.step_id
        flow.status = FlowStatus.RUNNING

        # Register a handler that completes immediately
        self.sm.register_handler(StepType.ANALYZE, lambda s, f: StepStatus.COMPLETED)

        self.sm.run(flow)

        from se3.engine.chat_history import _history_dir
        meta_path = _history_dir(self.project_root, flow.flow_id) / "_meta.json"

        assert meta_path.exists()
