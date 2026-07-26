"""Tests for the confirmation/review mechanism in SE3 workflow engine.

Tests cover:
- Human approves confirmation - flow continues forward
- Human requests changes - flow returns to previous step
- Resume with existing response - uses response without waiting
- State machine transition logic for CONFIRM steps
- Run command handling of confirmation responses
"""

import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.persistence import PersistenceManager
from tianluo.engine.state_machine import StateMachine


class TestConfirmStepHandler:
    """Test the confirm step handler directly."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        # Create tianluo/calls directory
        calls_dir = self.project_root / "tianluo" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)

        # Create a flow with a CONFIRM step
        self.flow = FlowInstance(
            task_description="Test task",
            task_type="feature",
            change_name="test-change",
            change_path=self.project_root / "test-change",
        )
        self.flow.state.selected_steps = [StepType.PLAN, StepType.CONFIRM, StepType.IMPLEMENT]

        # Create the step being reviewed (e.g., PROPOSE)
        self.plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
        )
        self.plan_step.outputs["plan"] = {"proposal": {"summary": "Test plan"}, "design": {"overview": "Test design"}}
        self.flow.state.add_step(self.plan_step)

        # Create the CONFIRM step
        self.confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            step_id="confirm-001",
            inputs={
                "step_to_review_id": "plan-001",
                "step_to_review_type": "plan",
                "reviewer": "human",
            },
        )
        self.flow.state.add_step(self.confirm_step)
        self.flow.state.current_step_id = "confirm-001"

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_confirm_handler_no_response_returns_paused(self):
        """Test that confirm handler returns PAUSED when no response exists."""
        from tianluo.engine.steps.confirm import confirm_handler

        result = confirm_handler(self.confirm_step, self.flow)

        assert result == StepStatus.PAUSED
        # Should have created a call file and stored its path
        assert "call_file" in self.confirm_step.outputs

    def test_confirm_handler_paused_creates_call_file(self):
        """Test that PAUSED creates a call file for the run loop to find."""
        from tianluo.engine.steps.confirm import confirm_handler

        result = confirm_handler(self.confirm_step, self.flow)

        assert result == StepStatus.PAUSED
        call_file_path = self.confirm_step.outputs.get("call_file")
        assert call_file_path is not None
        assert Path(call_file_path).exists()

    def test_confirm_handler_existing_approval_response(self):
        """Test that existing approval response is used without waiting."""
        from tianluo.engine.steps.confirm import confirm_handler

        # First create a call file
        calls_dir = self.project_root / "tianluo" / "calls"
        call_file = calls_dir / f"confirm_{self.confirm_step.step_id}_12345.json"
        call_data = {
            "step": self.confirm_step.step_id,
            "change_id": self.flow.change_name,
            "timestamp": 12345,
            "type": "confirm",
        }
        with open(call_file, "w") as f:
            json.dump(call_data, f)

        # Create a response file indicating approval
        response_file = call_file.parent / f"{call_file.stem}.response"
        response_data = {
            "approved": True,
            "feedback": "Approved from existing response",
        }
        with open(response_file, "w") as f:
            json.dump(response_data, f)

        result = confirm_handler(self.confirm_step, self.flow)

        # Should return COMPLETED without waiting
        assert result == StepStatus.COMPLETED
        assert self.confirm_step.outputs["review_result"]["approved"] is True

    def test_confirm_handler_existing_revision_response(self):
        """Test that existing revision response is used without waiting."""
        from tianluo.engine.steps.confirm import confirm_handler

        # First create a call file
        calls_dir = self.project_root / "tianluo" / "calls"
        call_file = calls_dir / f"confirm_{self.confirm_step.step_id}_12345.json"
        call_data = {
            "step": self.confirm_step.step_id,
            "change_id": self.flow.change_name,
            "timestamp": 12345,
            "type": "confirm",
        }
        with open(call_file, "w") as f:
            json.dump(call_data, f)

        # Create a response file indicating changes requested
        response_file = call_file.parent / f"{call_file.stem}.response"
        response_data = {
            "approved": False,
            "feedback": "Please revise this section",
        }
        with open(response_file, "w") as f:
            json.dump(response_data, f)

        result = confirm_handler(self.confirm_step, self.flow)

        # Should return REVISION_NEEDED without waiting
        assert result == StepStatus.REVISION_NEEDED
        assert self.confirm_step.outputs["review_result"]["approved"] is False


class TestConfirmResponseChecker:
    """Test the _check_existing_response helper function."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.calls_dir = Path(self.tmpdir)
        self.calls_dir.mkdir(parents=True, exist_ok=True)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_check_existing_response_returns_none_when_no_response(self):
        """Should return None when response file doesn't exist."""
        from tianluo.engine.steps.confirm import _check_existing_response

        call_file = self.calls_dir / "confirm_test.json"
        call_file.write_text('{"step": "test"}')

        result = _check_existing_response(call_file)

        assert result is None

    def test_check_existing_response_approval(self):
        """Should return approved=True when response indicates approval."""
        from tianluo.engine.steps.confirm import _check_existing_response

        call_file = self.calls_dir / "confirm_test.json"
        call_file.write_text('{"step": "test"}')

        response_file = self.calls_dir / "confirm_test.response"
        response_file.write_text('{"approved": true, "feedback": "Great work!"}')

        result = _check_existing_response(call_file)

        assert result is not None
        assert result["approved"] is True
        assert result["feedback"] == "Great work!"

    def test_check_existing_response_changes_requested(self):
        """Should return approved=False when response indicates changes needed."""
        from tianluo.engine.steps.confirm import _check_existing_response

        call_file = self.calls_dir / "confirm_test.json"
        call_file.write_text('{"step": "test"}')

        response_file = self.calls_dir / "confirm_test.response"
        response_file.write_text('{"approved": false, "feedback": "Fix this issue"}')

        result = _check_existing_response(call_file)

        assert result is not None
        assert result["approved"] is False
        assert result["feedback"] == "Fix this issue"

    def test_check_existing_response_invalid_json(self):
        """Should return None when response file has invalid JSON."""
        from tianluo.engine.steps.confirm import _check_existing_response

        call_file = self.calls_dir / "confirm_test.json"
        call_file.write_text('{"step": "test"}')

        response_file = self.calls_dir / "confirm_test.response"
        response_file.write_text("not valid json")

        result = _check_existing_response(call_file)

        assert result is None


class TestStateMachineConfirmTransitions:
    """Test state machine transitions for CONFIRM steps."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        # Create persistence manager
        self.persistence = PersistenceManager(self.project_root)

        # Create state machine
        self.state_machine = StateMachine(self.project_root, self.persistence)

        # Create a flow with confirmation steps
        self.flow = self.state_machine.create_flow(
            task_description="Test task with confirmation",
            task_type="feature",
            change_name="test-change",
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_transition_to_next_after_confirm_approval(self):
        """Test that flow continues forward when confirmation is approved."""
        # Set up a CONFIRM step that has been approved
        current_step = self.flow.state.get_current_step()
        current_step.status = StepStatus.COMPLETED
        current_step.outputs["review_result"] = {
            "approved": True,
            "feedback": "Approved",
            "step_to_review_id": "some-step-id",
            "step_to_review_type": "plan",
        }

        # Transition should go to next step
        next_step = self.state_machine.transition_to_next(self.flow)

        # Should have transitioned forward (not backward)
        assert next_step is not None
        # The next step should be after the CONFIRM step
        assert self.flow.state.current_step_index > 0

    def test_transition_to_next_after_confirm_revision(self):
        """Test that flow goes back when confirmation requests changes."""
        # Create a step that needs review (e.g., PROPOSE)
        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
        )
        self.flow.state.add_step(plan_step)

        # Create a CONFIRM step after it
        confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.COMPLETED,
            step_id="confirm-001",
            inputs={
                "step_to_review_id": "plan-001",
                "step_to_review_type": "plan",
            },
            outputs={
                "review_result": {
                    "approved": False,
                    "feedback": "Please revise",
                    "step_to_review_id": "plan-001",
                    "step_to_review_type": "plan",
                },
            },
        )
        self.flow.state.add_step(confirm_step)
        self.flow.state.current_step_id = "confirm-001"

        # Update selected steps to include both
        self.flow.state.selected_steps = [
            StepType.ANALYZE,
            StepType.PLAN,
            StepType.CONFIRM,
            StepType.IMPLEMENT,
        ]
        self.flow.state.current_step_index = 2  # At CONFIRM

        # Transition should go back to PROPOSE
        next_step = self.state_machine.transition_to_next(self.flow)

        # Should have transitioned to the step being reviewed
        assert next_step is not None
        assert next_step.step_id == "plan-001"
        assert next_step.status == StepStatus.PENDING  # Reset for revision


class TestRunCommandConfirmHandling:
    """Test the run command's handling of confirmation responses."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        # Create se3 directory structure
        (self.project_root / "tianluo" / "calls").mkdir(parents=True, exist_ok=True)
        (self.project_root / "tianluo" / "state").mkdir(parents=True, exist_ok=True)

        # Create a flow with a CONFIRM step
        self.flow = FlowInstance(
            task_description="Test task",
            task_type="feature",
            change_name="test-change",
            change_path=self.project_root,
        )
        self.flow.state.selected_steps = [
            StepType.ANALYZE,
            StepType.PLAN,
            StepType.CONFIRM,
            StepType.IMPLEMENT,
        ]

        # Create PROPOSE step
        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
        )
        self.flow.state.add_step(plan_step)

        # Create CONFIRM step
        self.confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PAUSED,
            step_id="confirm-001",
            inputs={
                "step_to_review_id": "plan-001",
                "step_to_review_type": "plan",
            },
        )
        self.flow.state.add_step(self.confirm_step)
        self.flow.state.current_step_id = "confirm-001"
        self.flow.status = FlowStatus.PAUSED

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_check_confirm_response_with_approval(self):
        """Test that _check_confirm_response returns COMPLETED when approved."""
        from tianluo.commands.run import _check_confirm_response

        # Create call file
        calls_dir = self.project_root / "tianluo" / "calls"
        call_file = calls_dir / "confirm_test.json"
        call_file.write_text(json.dumps({
            "step": self.confirm_step.step_id,
            "change_id": self.flow.change_name,
        }))

        # Create response file with approval
        response_file = calls_dir / "confirm_test.response"
        response_file.write_text(json.dumps({
            "approved": True,
            "feedback": "Approved!",
        }))

        result = _check_confirm_response(self.flow, self.confirm_step, self.project_root)

        assert result == StepStatus.COMPLETED
        assert self.confirm_step.outputs["review_result"]["approved"] is True

    def test_check_confirm_response_with_changes(self):
        """Test that _check_confirm_response returns REVISION_NEEDED when changes requested."""
        from tianluo.commands.run import _check_confirm_response

        # Create call file
        calls_dir = self.project_root / "tianluo" / "calls"
        call_file = calls_dir / "confirm_test.json"
        call_file.write_text(json.dumps({
            "step": self.confirm_step.step_id,
            "change_id": self.flow.change_name,
        }))

        # Create response file with changes requested
        response_file = calls_dir / "confirm_test.response"
        response_file.write_text(json.dumps({
            "approved": False,
            "feedback": "Please fix",
        }))

        result = _check_confirm_response(self.flow, self.confirm_step, self.project_root)

        assert result == StepStatus.REVISION_NEEDED
        assert self.confirm_step.outputs["review_result"]["approved"] is False

    def test_check_confirm_response_no_response_file(self):
        """Test that _check_confirm_response returns None when no response exists."""
        from tianluo.commands.run import _check_confirm_response

        # Create call file but no response
        calls_dir = self.project_root / "tianluo" / "calls"
        call_file = calls_dir / "confirm_test.json"
        call_file.write_text(json.dumps({
            "step": self.confirm_step.step_id,
            "change_id": self.flow.change_name,
        }))

        result = _check_confirm_response(self.flow, self.confirm_step, self.project_root)

        assert result is None


class TestConfirmationStepInsertion:
    """Test that CONFIRM steps are properly inserted into workflows."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        # Create tianluo.yaml using the per-step confirmation schema.
        se3_yaml = self.project_root / "tianluo.yaml"
        se3_yaml.write_text("""
confirmation:
  steps:
    plan: {reviewer: human}
""")

        self.persistence = PersistenceManager(self.project_root)
        self.state_machine = StateMachine(self.project_root, self.persistence)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_confirm_steps_inserted_after_configured_steps(self):
        """Test that CONFIRM step is inserted after plan."""
        flow = self.state_machine.create_flow(
            task_description="Test task",
            task_type="feature",
        )

        selected_steps = flow.state.selected_steps

        # Find positions of plan and confirm
        plan_idx = None
        confirm_after_plan_idx = None

        for i, step in enumerate(selected_steps):
            if step == StepType.PLAN:
                plan_idx = i
            elif step == StepType.CONFIRM and plan_idx is not None:
                confirm_after_plan_idx = i
                break

        # Should have CONFIRM after PLAN
        assert plan_idx is not None
        assert confirm_after_plan_idx is not None
        assert confirm_after_plan_idx == plan_idx + 1

    def test_confirm_steps_not_inserted_when_steps_omitted(self):
        """Empty confirmation.steps confirms only plan (always-on), nothing else."""
        # Empty confirmation.steps means no NON-plan step gets confirmed;
        # plan-confirm is always-on and is therefore unaffected.
        se3_yaml = self.project_root / "tianluo.yaml"
        se3_yaml.write_text("""
confirmation:
  steps: {}
""")

        flow = self.state_machine.create_flow(
            task_description="Test task",
            task_type="feature",
        )

        selected_steps = flow.state.selected_steps

        # The only CONFIRM is the always-on plan-confirm directly after PLAN.
        plan_idx = selected_steps.index(StepType.PLAN)
        assert selected_steps[plan_idx + 1] == StepType.CONFIRM
        assert selected_steps.count(StepType.CONFIRM) == 1


class TestEndToEndConfirmationFlow:
    """End-to-end tests for the confirmation mechanism."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        # Create directory structure
        (self.project_root / "tianluo" / "calls").mkdir(parents=True, exist_ok=True)
        (self.project_root / "tianluo" / "state").mkdir(parents=True, exist_ok=True)

        # Create tianluo.yaml using the per-step confirmation schema.
        se3_yaml = self.project_root / "tianluo.yaml"
        se3_yaml.write_text("""
confirmation:
  steps:
    plan: {reviewer: human}
""")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_flow_approval_resumes_forward(self):
        """Full flow: human approves, flow continues forward."""
        from tianluo.engine.steps.confirm import confirm_handler

        # change_path must be a child of project_root so that
        # change_path.parent resolves back to project_root
        change_path = self.project_root / "test-change"

        # Create flow with PROPOSE and CONFIRM
        flow = FlowInstance(
            task_description="Test feature",
            task_type="feature",
            change_name="test-change",
            change_path=change_path,
        )

        # Add PROPOSE step
        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
            outputs={"plan": {"proposal": {"summary": "Test plan"}, "design": {"overview": "Test design"}}, "task_groups": []},
        )
        flow.state.add_step(plan_step)

        # Add CONFIRM step
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

        # Simulate human response file already exists (approval)
        calls_dir = self.project_root / "tianluo" / "calls"
        call_file = calls_dir / f"confirm_{confirm_step.step_id}_12345.json"
        call_file.write_text(json.dumps({
            "step": confirm_step.step_id,
            "change_id": flow.change_name,
        }))

        response_file = call_file.parent / f"{call_file.stem}.response"
        response_file.write_text(json.dumps({
            "approved": True,
            "feedback": "Approved - proceed!",
        }))

        # Execute confirm handler
        result = confirm_handler(confirm_step, flow)

        # Should be COMPLETED
        assert result == StepStatus.COMPLETED

        # Check outputs are set correctly
        review_result = confirm_step.outputs.get("review_result", {})
        assert review_result["approved"] is True
        assert review_result["feedback"] == "Approved - proceed!"

    def test_full_flow_changes_requested_returns_to_previous(self):
        """Full flow: human requests changes, flow returns to previous step."""
        from tianluo.engine.steps.confirm import confirm_handler

        # change_path must be a child of project_root so that
        # change_path.parent resolves back to project_root
        change_path = self.project_root / "test-change"

        # Create flow with PROPOSE and CONFIRM
        flow = FlowInstance(
            task_description="Test feature",
            task_type="feature",
            change_name="test-change",
            change_path=change_path,
        )

        # Add PROPOSE step
        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
            outputs={"plan": {"proposal": {"summary": "Test plan"}, "design": {"overview": "Test design"}}, "task_groups": []},
        )
        flow.state.add_step(plan_step)

        # Add CONFIRM step
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

        # Simulate human response file (changes requested)
        calls_dir = self.project_root / "tianluo" / "calls"
        call_file = calls_dir / f"confirm_{confirm_step.step_id}_12345.json"
        call_file.write_text(json.dumps({
            "step": confirm_step.step_id,
            "change_id": flow.change_name,
        }))

        response_file = call_file.parent / f"{call_file.stem}.response"
        response_file.write_text(json.dumps({
            "approved": False,
            "feedback": "Please add more details",
        }))

        # Execute confirm handler
        result = confirm_handler(confirm_step, flow)

        # Should be REVISION_NEEDED
        assert result == StepStatus.REVISION_NEEDED

        # Check outputs are set correctly
        review_result = confirm_step.outputs.get("review_result", {})
        assert review_result["approved"] is False
        assert review_result["feedback"] == "Please add more details"


class TestLLMReviewerNoCallFile:
    """Verify LLM reviewer path does NOT create call files and does NOT return PAUSED."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        calls_dir = self.project_root / "tianluo" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)

        self.flow = FlowInstance(
            task_description="Test task",
            task_type="feature",
            change_name="test-change",
            change_path=self.project_root / "test-change",
        )
        self.flow.state.selected_steps = [StepType.PLAN, StepType.CONFIRM, StepType.IMPLEMENT]

        self.plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
        )
        self.plan_step.outputs["plan"] = {"proposal": {"summary": "Test plan"}, "design": {"overview": "Test design"}}
        self.flow.state.add_step(self.plan_step)

        self.confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            step_id="confirm-001",
            inputs={
                "step_to_review_id": "plan-001",
                "step_to_review_type": "plan",
                "reviewer": "llm",
                "llm_reviewer": {"model": None, "max_iterations": 3},
            },
        )
        self.flow.state.add_step(self.confirm_step)
        self.flow.state.current_step_id = "confirm-001"

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("tianluo.engine.steps.confirm.LLMCaller")
    def test_llm_reviewer_does_not_create_call_file(self, MockLLMCaller):
        """LLM reviewer path must not create any call files."""
        from tianluo.engine.steps.confirm import confirm_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = '{"approved": true, "feedback": "Looks good"}'
        MockLLMCaller.return_value = mock_caller

        confirm_handler(self.confirm_step, self.flow)

        calls_dir = self.project_root / "tianluo" / "calls"
        call_files = list(calls_dir.glob("confirm_*.json"))
        assert len(call_files) == 0, f"Expected no call files but found: {call_files}"

    @patch("tianluo.engine.steps.confirm.LLMCaller")
    def test_llm_reviewer_never_returns_paused(self, MockLLMCaller):
        """LLM reviewer path must never return PAUSED."""
        from tianluo.engine.steps.confirm import confirm_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = '{"approved": true, "feedback": "OK"}'
        MockLLMCaller.return_value = mock_caller

        result = confirm_handler(self.confirm_step, self.flow)

        assert result != StepStatus.PAUSED
        assert result in (StepStatus.COMPLETED, StepStatus.REVISION_NEEDED)
