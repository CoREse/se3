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

        # Should fail the flow instead of continuing
        assert next_step is None
        assert flow.status == FlowStatus.FAILED

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


class TestBuildStepInputsVerifySpec:
    """Test _build_step_inputs propagation for VERIFY_SPEC in fix loop."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        return StateMachine(project_root=tmp_path)

    @pytest.fixture
    def flow_in_fix_loop(self):
        flow = FlowInstance(
            flow_id="test-flow-vs",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        flow.state.increment_fix_iteration(fix_context={"reason": "spec_compliance"})

        impl_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": ["a.py"]},
        )
        flow.state.add_step(impl_step)

        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )
        flow.state.add_step(test_step)

        prev_verify = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "verification_result": {"issues": [{"message": "Missing check", "scope": "in_scope"}]},
                "fix_instructions": "Add boundary check in handler",
                "issues": [{"message": "Missing check", "scope": "in_scope", "priority": "high"}],
            },
        )
        flow.state.add_step(prev_verify)

        return flow

    def test_propagates_fix_iteration(self, state_machine, flow_in_fix_loop):
        inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.VERIFY_SPEC)
        assert inputs["fix_iteration"] == 1

    def test_propagates_fix_history(self, state_machine, flow_in_fix_loop):
        inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.VERIFY_SPEC)
        assert len(inputs["fix_history"]) == 1
        assert inputs["fix_history"][0]["reason"] == "spec_compliance"

    def test_propagates_max_fix_iterations(self, state_machine, flow_in_fix_loop):
        inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.VERIFY_SPEC)
        assert "max_fix_iterations" in inputs
        assert inputs["max_fix_iterations"] >= 1

    def test_propagates_prev_issues(self, state_machine, flow_in_fix_loop):
        inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.VERIFY_SPEC)
        assert len(inputs["prev_issues"]) == 1
        assert inputs["prev_issues"][0]["message"] == "Missing check"

    def test_propagates_prev_verification_result(self, state_machine, flow_in_fix_loop):
        inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.VERIFY_SPEC)
        assert inputs["prev_verification_result"]["issues"][0]["message"] == "Missing check"

    def test_propagates_prev_fix_instructions(self, state_machine, flow_in_fix_loop):
        inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.VERIFY_SPEC)
        assert inputs["prev_fix_instructions"] == "Add boundary check in handler"

    def test_no_prev_data_when_not_in_fix_loop(self, state_machine):
        flow = FlowInstance(
            flow_id="test-flow-vs2",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        inputs = state_machine._build_step_inputs(flow, StepType.VERIFY_SPEC)
        assert "prev_issues" not in inputs
        assert "fix_iteration" not in inputs


class TestBuildStepInputsSelfCheck:
    """Test _build_step_inputs propagation for SELF_CHECK in fix loop."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        return StateMachine(project_root=tmp_path)

    @pytest.fixture
    def flow_in_fix_loop(self):
        flow = FlowInstance(
            flow_id="test-flow-sc",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        flow.state.increment_fix_iteration(fix_context={"reason": "self_check"})

        impl_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": ["b.py"]},
        )
        flow.state.add_step(impl_step)

        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )
        flow.state.add_step(test_step)

        prev_sc = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "issues": [{"severity": "high", "description": "Missing null check", "location": "handler.py:42"}],
            },
        )
        flow.state.add_step(prev_sc)

        return flow

    def test_propagates_fix_iteration(self, state_machine, flow_in_fix_loop):
        inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.SELF_CHECK)
        assert inputs["fix_iteration"] == 1

    def test_propagates_fix_history(self, state_machine, flow_in_fix_loop):
        inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.SELF_CHECK)
        assert len(inputs["fix_history"]) == 1
        assert inputs["fix_history"][0]["reason"] == "self_check"

    def test_propagates_max_fix_iterations(self, state_machine, flow_in_fix_loop):
        inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.SELF_CHECK)
        assert "max_fix_iterations" in inputs

    def test_propagates_prev_self_check_issues(self, state_machine, flow_in_fix_loop):
        inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.SELF_CHECK)
        assert len(inputs["prev_self_check_issues"]) == 1
        assert inputs["prev_self_check_issues"][0]["description"] == "Missing null check"

    def test_no_prev_data_when_not_in_fix_loop(self, state_machine):
        flow = FlowInstance(
            flow_id="test-flow-sc2",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        inputs = state_machine._build_step_inputs(flow, StepType.SELF_CHECK)
        assert "prev_self_check_issues" not in inputs
        assert "fix_iteration" not in inputs


class TestPrevInputsDeepCopy:
    """Previous-iteration data passed into inputs must be deep-copied so that
    later mutations on step.outputs cannot corrupt the snapshot the next step sees."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        return StateMachine(project_root=tmp_path)

    def _make_flow_with_prev_verify(self):
        flow = FlowInstance(
            flow_id="test-deepcopy-vs",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        flow.state.increment_fix_iteration(fix_context={"reason": "spec_compliance"})

        impl_step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED, outputs={})
        flow.state.add_step(impl_step)

        prev_verify = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "verification_result": {"issues": [{"message": "A", "scope": "in_scope"}]},
                "fix_instructions": "do X",
                "issues": [{"message": "A", "scope": "in_scope", "priority": "high"}],
            },
        )
        flow.state.add_step(prev_verify)
        return flow, prev_verify

    def test_verify_spec_prev_issues_is_deep_copied(self, state_machine):
        flow, prev_verify = self._make_flow_with_prev_verify()
        inputs = state_machine._build_step_inputs(flow, StepType.VERIFY_SPEC)

        # Mutate the snapshot inside inputs; originals on the step must be untouched.
        inputs["prev_issues"][0]["message"] = "MUTATED"
        inputs["prev_verification_result"]["issues"][0]["message"] = "MUTATED"

        assert prev_verify.outputs["issues"][0]["message"] == "A"
        assert prev_verify.outputs["verification_result"]["issues"][0]["message"] == "A"

    def test_self_check_prev_issues_is_deep_copied(self, state_machine):
        flow = FlowInstance(
            flow_id="test-deepcopy-sc",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        flow.state.increment_fix_iteration(fix_context={"reason": "self_check"})
        flow.state.add_step(Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED, outputs={}))
        prev_sc = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.REVISION_NEEDED,
            outputs={"issues": [{"severity": "high", "description": "D", "location": "f.py:1"}]},
        )
        flow.state.add_step(prev_sc)

        inputs = state_machine._build_step_inputs(flow, StepType.SELF_CHECK)
        inputs["prev_self_check_issues"][0]["description"] = "MUTATED"
        assert prev_sc.outputs["issues"][0]["description"] == "D"

    def test_implement_test_and_verify_results_deep_copied(self, state_machine):
        flow = FlowInstance(
            flow_id="test-deepcopy-impl",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        flow.state.increment_fix_iteration(fix_context={"reason": "test_failure"})
        flow.state.add_step(Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED, outputs={}))
        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": False, "failures": [{"name": "t1"}]}},
        )
        flow.state.add_step(test_step)
        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "verification_result": {"issues": [{"message": "V"}]},
                "fix_instructions": "do Y",
                "fix_context": {"spec_issues": [{"priority": "high", "message": "V"}]},
            },
        )
        flow.state.add_step(verify_step)

        inputs = state_machine._build_step_inputs(flow, StepType.IMPLEMENT)

        inputs["test_results"]["failures"][0]["name"] = "MUTATED"
        inputs["verification_result"]["issues"][0]["message"] = "MUTATED"
        inputs["fix_context"]["spec_issues"][0]["message"] = "MUTATED"

        assert test_step.outputs["test_results"]["failures"][0]["name"] == "t1"
        assert verify_step.outputs["verification_result"]["issues"][0]["message"] == "V"
        assert verify_step.outputs["fix_context"]["spec_issues"][0]["message"] == "V"


class TestMultiIterationAccumulation:
    """Verify that fix_history and previous_output behave correctly across
    multiple fix iterations — the scenario that was never tested before."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        return StateMachine(project_root=tmp_path)

    def test_fix_history_accumulates_across_iterations(self, state_machine):
        flow = FlowInstance(
            flow_id="test-accum-history",
            task_description="T",
            status=FlowStatus.RUNNING,
        )
        flow.state.increment_fix_iteration(fix_context={"reason": "test_failure"})
        flow.state.increment_fix_iteration(fix_context={"reason": "self_check"})
        flow.state.increment_fix_iteration(fix_context={"reason": "spec_compliance"})
        flow.state.add_step(Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED, outputs={}))

        inputs = state_machine._build_step_inputs(flow, StepType.IMPLEMENT)

        assert len(inputs["fix_history"]) == 3
        reasons = [e["reason"] for e in inputs["fix_history"]]
        assert reasons == ["test_failure", "self_check", "spec_compliance"]
        iterations = [e["iteration"] for e in inputs["fix_history"]]
        assert iterations == [1, 2, 3]

    def test_fix_history_snapshot_is_independent_of_state(self, state_machine):
        flow = FlowInstance(
            flow_id="test-accum-snapshot",
            task_description="T",
            status=FlowStatus.RUNNING,
        )
        flow.state.increment_fix_iteration(fix_context={"reason": "test_failure"})
        flow.state.add_step(Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED, outputs={}))

        inputs = state_machine._build_step_inputs(flow, StepType.IMPLEMENT)
        # Mutating the snapshot must not pollute state.fix_history.
        inputs["fix_history"].append({"iteration": 99, "reason": "fake"})
        assert len(flow.state.fix_history) == 1

    def test_transition_to_fix_caps_previous_output_size(self, tmp_path):
        from se3.engine.state_machine import _PREVIOUS_OUTPUT_MAX_BYTES

        sm = StateMachine(project_root=tmp_path)
        flow = FlowInstance(
            flow_id="test-truncate-prevout",
            task_description="T",
            status=FlowStatus.RUNNING,
        )
        huge = "x" * (_PREVIOUS_OUTPUT_MAX_BYTES * 2)
        impl_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"big_blob": huge, "files_changed": ["a.py"]},
        )
        flow.state.add_step(impl_step)

        trigger = Step(
            step_type=StepType.TEST,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "re-run",
                "fix_context": {"reason": "test_failure"},
            },
        )
        flow.state.add_step(trigger)

        with patch.object(sm, "persistence") as mock_pers:
            mock_pers.save_flow = Mock()
            result = sm._transition_to_fix(flow, trigger)

        assert result is impl_step
        prev = impl_step.inputs["previous_output"]
        assert prev.get("_truncated") is True
        assert prev["_original_size"] > _PREVIOUS_OUTPUT_MAX_BYTES
        assert len(prev["preview"]) <= _PREVIOUS_OUTPUT_MAX_BYTES

    def test_transition_to_fix_excludes_nested_previous_output(self, tmp_path):
        sm = StateMachine(project_root=tmp_path)
        flow = FlowInstance(
            flow_id="test-no-nest-prevout",
            task_description="T",
            status=FlowStatus.RUNNING,
        )
        # Simulate an LLM that echoed previous_output back into outputs.
        impl_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={
                "files_changed": ["a.py"],
                "previous_output": {"stale": "data"},
            },
        )
        flow.state.add_step(impl_step)
        trigger = Step(
            step_type=StepType.TEST,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "re-run",
                "fix_context": {"reason": "test_failure"},
            },
        )
        flow.state.add_step(trigger)

        with patch.object(sm, "persistence") as mock_pers:
            mock_pers.save_flow = Mock()
            sm._transition_to_fix(flow, trigger)

        prev = impl_step.inputs["previous_output"]
        # The key must not have re-nested a "previous_output" child.
        assert "previous_output" not in prev
        assert "files_changed" in prev


class TestInferFixReason:
    def test_known_trigger_types(self):
        from se3.engine.state_machine import _infer_fix_reason
        assert _infer_fix_reason("test") == "test_failure"
        assert _infer_fix_reason("self_check") == "self_check"
        assert _infer_fix_reason("verify_spec") == "spec_compliance"

    def test_unknown_type_returns_trigger_itself(self):
        from se3.engine.state_machine import _infer_fix_reason
        # Not silently labeled as "spec_compliance" anymore — returns the input.
        assert _infer_fix_reason("lint") == "lint"
        assert _infer_fix_reason("") == "unknown"


class TestMaxFixIterations:
    """Test cases for max fix iterations configuration."""

    def test_get_max_fix_iterations_default(self, tmp_path):
        """Test that default max fix iterations is 20."""
        state_machine = StateMachine(project_root=tmp_path)

        result = state_machine._get_max_fix_iterations()

        assert result == 20

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
