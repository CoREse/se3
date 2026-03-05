"""Tests for the state machine fix loop functionality.

These tests verify the test-verify-fix loop mechanism that automatically
transitions back to the implement step when tests fail.
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.state_machine import StateMachine


class TestFixIterationTracking:
    """Test cases for fix iteration tracking in State class."""

    def test_increment_fix_iteration_initial(self):
        """Test that increment_fix_iteration starts at 1."""
        state = State()
        assert state.get_fix_iteration() == 0

        iteration = state.increment_fix_iteration()

        assert iteration == 1
        assert state.get_fix_iteration() == 1
        assert state.context["fix_iterations"] == 1

    def test_increment_fix_iteration_multiple(self):
        """Test that increment_fix_iteration increments correctly."""
        state = State()

        assert state.increment_fix_iteration() == 1
        assert state.increment_fix_iteration() == 2
        assert state.increment_fix_iteration() == 3
        assert state.get_fix_iteration() == 3

    def test_increment_fix_iteration_tracks_history(self):
        """Test that increment_fix_iteration tracks history."""
        state = State()

        state.increment_fix_iteration(fix_context={"reason": "test_failure"})
        state.increment_fix_iteration(fix_context={"reason": "spec_compliance"})

        assert len(state.fix_history) == 2
        assert state.fix_history[0]["iteration"] == 1
        assert state.fix_history[0]["reason"] == "test_failure"
        assert state.fix_history[1]["iteration"] == 2
        assert state.fix_history[1]["reason"] == "spec_compliance"

    def test_fix_iteration_serialization(self):
        """Test that fix iteration data serializes correctly."""
        state = State()
        state.increment_fix_iteration(fix_context={"test": "data"})

        data = state.to_dict()

        assert data["fix_iterations"] == 1
        assert len(data["fix_history"]) == 1
        assert data["fix_history"][0]["iteration"] == 1

    def test_fix_iteration_deserialization(self):
        """Test that fix iteration data deserializes correctly."""
        data = {
            "current_step_id": None,
            "step_history": [],
            "steps": {},
            "context": {},
            "selected_steps": [],
            "current_step_index": 0,
            "review_iterations": {},
            "fix_iterations": 2,
            "fix_history": [
                {"iteration": 1, "reason": "test"},
                {"iteration": 2, "reason": "test2"},
            ],
        }

        state = State.from_dict(data)

        assert state.fix_iterations == 2
        assert state.get_fix_iteration() == 2
        assert len(state.fix_history) == 2


class TestTransitionToFix:
    """Test cases for the _transition_to_fix method."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine."""
        return StateMachine(project_root=tmp_path)

    @pytest.fixture
    def flow_with_implement(self, tmp_path):
        """Create a flow with an implement step in history."""
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [
            StepType.ANALYZE,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERIFY_SPEC,
        ]

        # Create and add implement step
        implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            inputs={"task_groups": []},
            outputs={"files_changed": ["test.py"]},
        )
        flow.state.add_step(implement_step)

        # Create and add test step
        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": False}},
        )
        flow.state.add_step(test_step)

        # Create and add verify_spec step
        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Fix the bug",
                "fix_context": {"test_failed": True},
            },
        )
        flow.state.add_step(verify_step)
        flow.state.current_step_id = verify_step.step_id

        return flow, implement_step, verify_step

    def test_transition_to_fix_finds_implement_step(self, state_machine, flow_with_implement):
        """Test that _transition_to_fix finds the implement step."""
        flow, implement_step, verify_step = flow_with_implement

        result = state_machine._transition_to_fix(flow, verify_step)

        assert result is not None
        assert result.step_id == implement_step.step_id

    def test_transition_to_fix_increments_iteration(self, state_machine, flow_with_implement):
        """Test that _transition_to_fix increments fix iteration."""
        flow, _, verify_step = flow_with_implement

        assert flow.state.get_fix_iteration() == 0

        state_machine._transition_to_fix(flow, verify_step)

        assert flow.state.get_fix_iteration() == 1

    def test_transition_to_fix_sets_inputs(self, state_machine, flow_with_implement):
        """Test that _transition_to_fix sets correct inputs on implement step."""
        flow, implement_step, verify_step = flow_with_implement

        state_machine._transition_to_fix(flow, verify_step)

        assert implement_step.inputs["fix_instructions"] == "Fix the bug"
        assert implement_step.inputs["fix_context"]["test_failed"] is True
        assert implement_step.inputs["is_fix_iteration"] is True
        assert implement_step.inputs["fix_iteration"] == 1

    def test_transition_to_fix_resets_step_status(self, state_machine, flow_with_implement):
        """Test that _transition_to_fix resets the implement step status."""
        flow, implement_step, verify_step = flow_with_implement

        assert implement_step.status == StepStatus.COMPLETED

        state_machine._transition_to_fix(flow, verify_step)

        assert implement_step.status == StepStatus.PENDING

    def test_transition_to_fix_updates_current_step(self, state_machine, flow_with_implement):
        """Test that _transition_to_fix updates flow state to point to implement."""
        flow, implement_step, verify_step = flow_with_implement

        state_machine._transition_to_fix(flow, verify_step)

        assert flow.state.current_step_id == implement_step.step_id

    def test_transition_to_fix_returns_none_when_no_fix_needed(self, state_machine, flow_with_implement):
        """Test that _transition_to_fix returns None when fix_needed is False."""
        flow, _, verify_step = flow_with_implement
        verify_step.outputs["fix_needed"] = False

        result = state_machine._transition_to_fix(flow, verify_step)

        assert result is None

    def test_transition_to_fix_returns_none_when_no_implement_step(self, state_machine, tmp_path):
        """Test that _transition_to_fix returns None when no implement step exists."""
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )

        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Fix it",
            },
        )

        result = state_machine._transition_to_fix(flow, verify_step)

        assert result is None


class TestTransitionToNextWithFixLoop:
    """Test cases for transition_to_next handling REVISION_NEEDED from VERIFY_SPEC."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine."""
        return StateMachine(project_root=tmp_path)

    @pytest.fixture
    def flow_with_verify_revision(self, tmp_path):
        """Create a flow with VERIFY_SPEC in REVISION_NEEDED state."""
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERIFY_SPEC,
            StepType.COMMIT,
        ]

        # Create and add implement step
        implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": []},
        )
        flow.state.add_step(implement_step)

        # Create and add test step
        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": False}},
        )
        flow.state.add_step(test_step)

        # Create and add verify_spec step with REVISION_NEEDED
        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Fix the bug",
                "fix_context": {"test_failed": True},
            },
        )
        flow.state.add_step(verify_step)
        flow.state.current_step_id = verify_step.step_id

        return flow, implement_step, verify_step

    def test_transition_to_next_calls_transition_to_fix(self, state_machine, flow_with_verify_revision):
        """Test that transition_to_next calls _transition_to_fix for VERIFY_SPEC REVISION_NEEDED."""
        flow, implement_step, _ = flow_with_verify_revision

        next_step = state_machine.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_id == implement_step.step_id
        assert next_step.step_type == StepType.IMPLEMENT

    def test_transition_to_next_respects_max_iterations(self, state_machine, flow_with_verify_revision):
        """Test that transition_to_next respects max fix iterations."""
        flow, _, verify_step = flow_with_verify_revision

        # Set iteration at max
        flow.state.fix_iterations = 3

        with patch.object(state_machine, '_get_max_fix_iterations', return_value=3):
            next_step = state_machine.transition_to_next(flow)

        # Should continue to next step (COMMIT) instead of going back to IMPLEMENT
        assert next_step is not None
        assert next_step.step_type == StepType.COMMIT

    def test_transition_to_next_increments_iteration_on_fix(self, state_machine, flow_with_verify_revision):
        """Test that transition_to_next increments fix iteration when transitioning to fix."""
        flow, _, _ = flow_with_verify_revision

        assert flow.state.get_fix_iteration() == 0

        state_machine.transition_to_next(flow)

        assert flow.state.get_fix_iteration() == 1


class TestBuildStepInputsWithFixContext:
    """Test cases for _build_step_inputs including fix context."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine."""
        return StateMachine(project_root=tmp_path)

    @pytest.fixture
    def flow_with_fix_history(self, tmp_path):
        """Create a flow with fix history."""
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )

        # Add fix history
        flow.state.increment_fix_iteration(fix_context={"reason": "test_failure"})

        # Create and add test step
        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": False, "stdout": "Error"}},
        )
        flow.state.add_step(test_step)

        # Create and add verify_spec step
        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.COMPLETED,
            outputs={
                "verification_result": {"issues": []},
                "fix_instructions": "Fix it",
                "fix_context": {"test_failed": True},
            },
        )
        flow.state.add_step(verify_step)

        return flow

    def test_build_step_inputs_includes_fix_iteration_for_implement(self, state_machine, flow_with_fix_history):
        """Test that _build_step_inputs includes fix_iteration when building IMPLEMENT inputs."""
        inputs = state_machine._build_step_inputs(flow_with_fix_history, StepType.IMPLEMENT)

        assert inputs["fix_iteration"] == 1
        assert len(inputs["fix_history"]) == 1

    def test_build_step_inputs_includes_test_results_for_implement(self, state_machine, flow_with_fix_history):
        """Test that _build_step_inputs includes test_results when in fix loop."""
        inputs = state_machine._build_step_inputs(flow_with_fix_history, StepType.IMPLEMENT)

        assert "test_results" in inputs
        assert inputs["test_results"]["passed"] is False

    def test_build_step_inputs_includes_verification_result_for_implement(self, state_machine, flow_with_fix_history):
        """Test that _build_step_inputs includes verification_result when in fix loop."""
        inputs = state_machine._build_step_inputs(flow_with_fix_history, StepType.IMPLEMENT)

        assert "verification_result" in inputs
        assert "fix_instructions" in inputs
        assert "fix_context" in inputs

    def test_build_step_inputs_no_fix_context_when_iteration_zero(self, state_machine, tmp_path):
        """Test that _build_step_inputs doesn't include fix context when iteration is 0."""
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )

        inputs = state_machine._build_step_inputs(flow, StepType.IMPLEMENT)

        # Should not have fix-specific keys when not in a fix loop
        assert "fix_iteration" not in inputs
        assert "fix_history" not in inputs


class TestMaxFixIterations:
    """Test cases for max fix iterations configuration."""

    def test_get_max_fix_iterations_default(self, tmp_path):
        """Test that default max fix iterations is 3."""
        state_machine = StateMachine(project_root=tmp_path)

        result = state_machine._get_max_fix_iterations()

        assert result == 3

    def test_get_max_fix_iterations_from_config(self, tmp_path):
        """Test that max fix iterations can be loaded from config."""
        # Create se3.yaml with custom max_fix_iterations
        config_content = """
workflow:
  max_fix_iterations: 5
"""
        (tmp_path / "se3.yaml").write_text(config_content)

        state_machine = StateMachine(project_root=tmp_path)

        result = state_machine._get_max_fix_iterations()

        assert result == 5


class TestFixLoopIntegration:
    """Integration tests for the complete fix loop."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine with mocked persistence."""
        with patch("se3.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=tmp_path)
            return sm

    def test_complete_fix_loop_cycle(self, state_machine):
        """Test a complete cycle through the fix loop."""
        # Create flow
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERIFY_SPEC,
        ]

        # Add implement step
        implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": []},
        )
        flow.state.add_step(implement_step)

        # Add test step
        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": False}},
        )
        flow.state.add_step(test_step)

        # Add verify_spec step with REVISION_NEEDED
        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Fix the bug",
                "fix_context": {"test_failed": True},
            },
        )
        flow.state.add_step(verify_step)
        flow.state.current_step_id = verify_step.step_id

        # Transition should go back to implement
        next_step = state_machine.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.IMPLEMENT
        assert flow.state.get_fix_iteration() == 1
        assert implement_step.inputs["is_fix_iteration"] is True
