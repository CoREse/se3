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
            StepType.PLAN,
            StepType.CONFIRM,
            StepType.IMPLEMENT,
        ]

        # Add completed PROPOSE step
        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
            outputs={"plan": {"proposal": {"summary": "Test plan"}, "design": {}}, "task_groups": []},
        )
        flow.state.add_step(plan_step)

        # Add CONFIRM step that returns REVISION_NEEDED
        confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            step_id="confirm-001",
            inputs={
                "step_to_review_id": "plan-001",
                "step_to_review_type": "plan",
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
                    "step_to_review_id": "plan-001",
                    "step_to_review_type": "plan",
                }
                step.outputs["revision_feedback"] = "Please revise"
                return StepStatus.REVISION_NEEDED
            else:
                step.outputs["review_result"] = {
                    "approved": True,
                    "feedback": "Looks good now",
                    "step_to_review_id": "plan-001",
                    "step_to_review_type": "plan",
                }
                return StepStatus.COMPLETED

        # Mock the propose handler for when it gets re-executed
        propose_call_count = 0

        def mock_plan_handler(step, fl):
            nonlocal propose_call_count
            propose_call_count += 1
            step.outputs["plan"] = {"proposal": {"summary": "Revised plan"}, "design": {}}
            return StepStatus.COMPLETED

        self.sm.register_handler(StepType.CONFIRM, mock_confirm_handler)
        self.sm.register_handler(StepType.PLAN, mock_plan_handler)
        # Register a design handler so the flow can complete
        self.sm.register_handler(StepType.IMPLEMENT, lambda s, f: StepStatus.COMPLETED)

        flow.status = FlowStatus.RUNNING

        # Drive the flow manually using init_flow + run_step + transition_to_next
        self.sm.init_flow(flow)

        max_iterations = 100
        iterations = 0
        while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED) and iterations < max_iterations:
            iterations += 1
            current_step = flow.state.get_current_step()
            if not current_step:
                flow.status = FlowStatus.COMPLETED
                break
            step_status = self.sm.run_step(flow, current_step)
            if step_status == StepStatus.FAILED:
                flow.status = FlowStatus.FAILED
                break
            if step_status == StepStatus.PAUSED:
                flow.status = FlowStatus.PAUSED
                break
            if not self.sm.transition_to_next(flow):
                break

        # The propose step should have been re-executed (revision)
        assert propose_call_count >= 1, "PROPOSE step should have been re-run after revision"

        # Flow should not have hit max iterations (the old bug)
        assert flow.status != FlowStatus.FAILED, "Flow should not fail from infinite loop"

    def test_confirm_revision_calls_transition_to_next(self):
        """Verify that transition_to_next is called (not skipped) when
        CONFIRM returns REVISION_NEEDED."""
        flow = FlowInstance(
            task_description="Test",
            task_type="feature",
        )
        flow.state.selected_steps = [
            StepType.PLAN,
            StepType.CONFIRM,
            StepType.IMPLEMENT,
        ]

        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
        )
        flow.state.add_step(plan_step)

        confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.REVISION_NEEDED,
            step_id="confirm-001",
            outputs={
                "review_result": {
                    "approved": False,
                    "feedback": "Revise",
                    "step_to_review_id": "plan-001",
                    "step_to_review_type": "plan",
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
        assert result.step_id == "plan-001"
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
            StepType.PLAN,
            StepType.CONFIRM,
            StepType.IMPLEMENT,
            StepType.CONFIRM,
        ]

        # Add completed PROPOSE step
        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
            outputs={"plan": {"proposal": {"summary": "Test plan"}, "design": {}}, "task_groups": []},
        )
        flow.state.add_step(plan_step)

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
                    "step_to_review_id": "plan-001",
                    "step_to_review_type": "plan",
                },
            },
        )
        flow.state.add_step(confirm_step)

        # Add completed IMPLEMENT step
        implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            step_id="implement-001",
            outputs={"files_changed": ["test.py"]},
        )
        flow.state.add_step(implement_step)

        # Build inputs for the second CONFIRM step
        inputs = self.sm._build_step_inputs(flow, StepType.CONFIRM)

        # The second CONFIRM should review IMPLEMENT, not PLAN
        # (because PLAN is already confirmed)
        assert inputs.get("step_to_review_id") == "implement-001"
        assert inputs.get("step_to_review_type") == "implement"

    def test_rejected_confirm_does_not_mark_step_confirmed(self):
        """A CONFIRM that requested changes (approved=False) must NOT mark its
        reviewed step as confirmed.

        Regression for the plan-revision bug: when the LLM reviewer rejects
        plan (approved=False) and the plan re-runs via revision, the next
        CONFIRM must re-review the *same* plan step instead of treating it as
        already-confirmed and sliding forward to an unconfigured later step
        (which would trip the defensive human fallback in _build_step_inputs).
        """
        # Configure 'plan' for LLM review so resolve_confirm_inputs returns a
        # real entry (reviewer=None → defaults chain) rather than None, which
        # would force the human defensive fallback regardless of this fix.
        (self.project_root / "se3" / "specs").mkdir(parents=True, exist_ok=True)
        (self.project_root / "se3.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {}\n"
        )

        flow = FlowInstance(
            task_description="Test",
            task_type="feature",
        )
        # ANALYZE is an unconfigured later step the bug would slide forward to.
        flow.state.selected_steps = [
            StepType.PLAN,
            StepType.CONFIRM,
            StepType.ANALYZE,
        ]

        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
            outputs={"plan": {"proposal": {"summary": "Test plan"}, "design": {}}, "task_groups": []},
        )
        flow.state.add_step(plan_step)

        # A completed CONFIRM that REQUESTED CHANGES (approved=False) for plan.
        rejected_confirm = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.REVISION_NEEDED,
            step_id="confirm-001",
            outputs={
                "review_result": {
                    "approved": False,
                    "feedback": "Please revise the plan",
                    "step_to_review_id": "plan-001",
                    "step_to_review_type": "plan",
                },
            },
        )
        flow.state.add_step(rejected_confirm)

        inputs = self.sm._build_step_inputs(flow, StepType.CONFIRM)

        # The rejected confirm must NOT shield plan-001: the next CONFIRM still
        # selects plan-001 for re-review, not the unconfigured ANALYZE.
        assert inputs.get("step_to_review_id") == "plan-001"
        assert inputs.get("step_to_review_type") == "plan"
        # The defensive human fallback (state_machine.py ~1205) was not hit.
        assert inputs.get("reviewer") != "human"

    def test_not_yet_confirmed_step_is_found(self):
        """A step that has NOT been confirmed should be selected for review."""
        flow = FlowInstance(
            task_description="Test",
            task_type="feature",
        )
        flow.state.selected_steps = [
            StepType.PLAN,
            StepType.CONFIRM,
        ]

        # Add completed PROPOSE step (no prior CONFIRM for it)
        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
            outputs={"plan": {"proposal": {"summary": "Test plan"}, "design": {}}, "task_groups": []},
        )
        flow.state.add_step(plan_step)

        inputs = self.sm._build_step_inputs(flow, StepType.CONFIRM)

        assert inputs.get("step_to_review_id") == "plan-001"


class TestBoundedCrossRevisionReview:
    """The LLM review<->revise loop must be bounded by max_iterations across
    revisions.

    Each revision creates a brand-new CONFIRM step, so the cap can only work
    if it reads the persisted, cross-revision counter
    (flow.state.review_iterations), which _transition_to_revision increments
    once per revision. Before the fix, the per-confirm counter reset to 0
    every cycle and the safety net never engaged → unbounded re-review.
    """

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        (self.project_root / "se3" / "state").mkdir(parents=True, exist_ok=True)
        (self.project_root / "se3" / "specs").mkdir(parents=True, exist_ok=True)
        self.persistence = PersistenceManager(self.project_root)
        self.sm = StateMachine(self.project_root, self.persistence)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_flow(self):
        flow = FlowInstance(
            task_description="Test bounded review",
            task_type="feature",
            change_name="test-change",
            change_path=self.project_root / "test-change",
        )
        return flow

    def test_unit_auto_approve_when_persisted_count_at_cap(self):
        """When flow.state.review_iterations reaches max_iterations, the
        confirm handler auto-approves without calling the LLM."""
        from se3.engine.steps import confirm as confirm_mod

        flow = self._make_flow()
        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
            outputs={"plan": {"proposal": {"summary": "p"}, "design": {}}},
        )
        flow.state.add_step(plan_step)

        confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            step_id="confirm-cap",
            inputs={
                "step_to_review_id": "plan-001",
                "step_to_review_type": "plan",
                "reviewer": None,
                "agents": [{"name": "a", "type": "claude-code", "cmd": "claude", "priority": 0}],
                "max_iterations": 3,
            },
        )
        flow.state.add_step(confirm_step)

        # Drive the persisted counter to the cap (as repeated revisions would).
        flow.state.review_iterations["plan-001"] = 3

        with patch.object(confirm_mod, "LLMCaller") as MockLLMCaller:
            result = confirm_mod.confirm_handler(confirm_step, flow)
            # The LLM must NOT be constructed / called on the capped path.
            MockLLMCaller.assert_not_called()

        assert result == StepStatus.COMPLETED
        review_result = confirm_step.outputs["review_result"]
        assert review_result["approved"] is True
        assert "max review iterations" in review_result["feedback"]

    def test_integration_revision_loop_is_bounded(self):
        """End-to-end: with the LLM always rejecting, the review<->revise loop
        is bounded by max_iterations and then auto-approves, advancing the
        flow rather than looping forever."""
        from se3.engine.steps import confirm as confirm_mod
        from se3.engine.steps.confirm import confirm_handler

        max_iters = 2
        (self.project_root / "se3.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            f"    plan: {{max_iterations: {max_iters}}}\n"
        )

        flow = self._make_flow()
        flow.state.selected_steps = [
            StepType.PLAN,
            StepType.CONFIRM,
            StepType.IMPLEMENT,
        ]

        # Start at a PENDING plan step.
        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.PENDING,
            step_id="plan-001",
        )
        flow.state.add_step(plan_step)
        flow.state.current_step_id = "plan-001"
        flow.state.current_step_index = 0

        plan_runs = 0

        def plan_handler(step, fl):
            nonlocal plan_runs
            plan_runs += 1
            step.outputs["plan"] = {"proposal": {"summary": f"plan v{plan_runs}"}, "design": {}}
            step.outputs["task_groups"] = []
            return StepStatus.COMPLETED

        implement_runs = 0

        def implement_handler(step, fl):
            nonlocal implement_runs
            implement_runs += 1
            step.outputs["files_changed"] = []
            return StepStatus.COMPLETED

        self.sm.register_handler(StepType.PLAN, plan_handler)
        self.sm.register_handler(StepType.CONFIRM, confirm_handler)
        self.sm.register_handler(StepType.IMPLEMENT, implement_handler)

        flow.status = FlowStatus.RUNNING

        # LLM reviewer always rejects → would loop forever without a bound.
        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps(
            {"approved": False, "feedback": "still not good enough"}
        )

        guard = 0
        with patch.object(confirm_mod, "LLMCaller", return_value=mock_caller):
            while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED):
                guard += 1
                assert guard < 50, "review loop did not terminate — likely unbounded"
                current_step = flow.state.get_current_step()
                if not current_step:
                    break
                status = self.sm.run_step(flow, current_step)
                assert status != StepStatus.FAILED, f"step failed: {current_step.error_message}"
                if self.sm.transition_to_next(flow) is None:
                    break

        # The LLM was consulted exactly max_iterations times (counts 0..N-1);
        # the (N+1)-th confirm auto-approved without an LLM call.
        assert mock_caller.call.call_count == max_iters
        # Persisted counter ended at the cap.
        assert flow.state.get_review_iteration("plan-001") == max_iters
        # Plan re-ran once per rejection (initial run + max_iters revisions).
        assert plan_runs == max_iters + 1
        # Flow advanced past the gate: IMPLEMENT ran and the flow completed.
        assert implement_runs == 1
        assert flow.status == FlowStatus.COMPLETED


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

    def test_meta_json_written_by_init_flow(self):
        """init_flow() should call _write_flow_meta, writing _meta.json."""
        flow = FlowInstance(
            task_description="Test",
            task_type="feature",
        )

        self.sm.init_flow(flow)

        from se3.engine.chat_history import _history_dir
        meta_path = _history_dir(self.project_root, flow.flow_id) / "_meta.json"

        assert meta_path.exists()
