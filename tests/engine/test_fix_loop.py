"""Tests for the fix loop mechanism.

This test module verifies the test-verify-fix loop that automatically
transitions back to the implement step when tests fail, with configurable
iteration limits.

Acceptance Criteria:
- Tests verify fix loop iterates up to max_fix_iterations
- Tests verify REVISION_NEEDED is returned when tests fail
- Tests verify fix loop exits after max iterations
- Tests verify fix_context is properly passed between steps
"""

from __future__ import annotations

import os
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


class TestFixLoopIterations:
    """Test cases for fix loop iteration behavior and max iteration enforcement."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine with mocked persistence."""
        with patch("se3.engine.state_machine.PersistenceManager"):
            return StateMachine(project_root=tmp_path)

    @pytest.fixture
    def flow_with_failed_tests(self, tmp_path):
        """Create a flow with failed tests in verify_spec step."""
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
            inputs={"task_groups": []},
            outputs={"files_changed": ["test.py"]},
        )
        flow.state.add_step(implement_step)

        # Create and add test step with failed tests
        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={
                "test_results": {
                    "passed": False,
                    "returncode": 1,
                    "stdout": "Test failed",
                    "stderr": "AssertionError in test_fix_loop",
                }
            },
        )
        flow.state.add_step(test_step)

        # Create and add verify_spec step with REVISION_NEEDED
        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Fix the assertion error in test_fix_loop",
                "fix_context": {"test_failed": True, "error": "AssertionError"},
            },
        )
        flow.state.add_step(verify_step)
        flow.state.current_step_id = verify_step.step_id

        return flow, implement_step, test_step, verify_step

    def test_fix_loop_iterates_up_to_max_fix_iterations(self, state_machine, flow_with_failed_tests):
        """Test that fix loop iterates up to max_fix_iterations.

        Acceptance Criteria: Tests verify fix loop iterates up to max_fix_iterations
        """
        flow, implement_step, _, verify_step = flow_with_failed_tests

        max_iterations = 3

        with patch.object(state_machine, '_get_max_fix_iterations', return_value=max_iterations):
            # Simulate multiple fix iterations
            for iteration in range(1, max_iterations + 1):
                # Reset verify step to REVISION_NEEDED
                verify_step.status = StepStatus.REVISION_NEEDED
                flow.state.current_step_id = verify_step.step_id

                if iteration == max_iterations:
                    # At max iterations, set fix_iterations to max so transition stops
                    flow.state.fix_iterations = max_iterations

                next_step = state_machine.transition_to_next(flow)

                if iteration < max_iterations:
                    # Should go back to implement
                    assert next_step is not None, f"Iteration {iteration}: Should return implement step"
                    assert next_step.step_type == StepType.IMPLEMENT, f"Iteration {iteration}: Should be IMPLEMENT"
                    assert flow.state.get_fix_iteration() == iteration, f"Iteration {iteration}: Fix iteration should be {iteration}"

                    # Simulate completing implement and test again
                    next_step.status = StepStatus.COMPLETED

                    # Add new test step for next iteration
                    test_step = Step(
                        step_type=StepType.TEST,
                        status=StepStatus.COMPLETED,
                        outputs={
                            "test_results": {
                                "passed": False,
                                "returncode": 1,
                                "stdout": f"Test failed iteration {iteration}",
                            }
                        },
                    )
                    flow.state.add_step(test_step)

                    # Add new verify step for next iteration
                    verify_step = Step(
                        step_type=StepType.VERIFY_SPEC,
                        status=StepStatus.REVISION_NEEDED,
                        outputs={
                            "fix_needed": True,
                            "fix_instructions": f"Fix attempt {iteration + 1}",
                            "fix_context": {"test_failed": True},
                        },
                    )
                    flow.state.add_step(verify_step)
                    flow.state.current_step_id = verify_step.step_id
                else:
                    # At max iterations, should continue to next step instead of fixing
                    assert next_step is not None, "Max iteration: Should return next step"
                    assert next_step.step_type == StepType.COMMIT, "Max iteration: Should continue to COMMIT"

    def test_fix_loop_exits_after_max_iterations(self, state_machine, flow_with_failed_tests):
        """Test that fix loop exits after max iterations reached.

        Acceptance Criteria: Tests verify fix loop exits after max iterations
        """
        flow, _, _, verify_step = flow_with_failed_tests

        # Set fix iterations at max
        flow.state.fix_iterations = 3

        with patch.object(state_machine, '_get_max_fix_iterations', return_value=3):
            next_step = state_machine.transition_to_next(flow)

            # Should continue to COMMIT, not back to IMPLEMENT
            assert next_step is not None
            assert next_step.step_type == StepType.COMMIT
            assert flow.state.get_fix_iteration() == 3  # Should not increment


class TestRevisionNeededBehavior:
    """Test cases for REVISION_NEEDED status behavior."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine with mocked persistence."""
        with patch("se3.engine.state_machine.PersistenceManager"):
            return StateMachine(project_root=tmp_path)

    def test_revision_needed_returned_when_tests_fail(self, state_machine):
        """Test that REVISION_NEEDED is returned when tests fail.

        Acceptance Criteria: Tests verify REVISION_NEEDED is returned when tests fail
        """
        from se3.engine.steps.verify_spec import verify_spec_handler

        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )

        step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Test task",
                "spec_content": {},
                "changes_made": {},
                "test_results": {
                    "passed": False,
                    "returncode": 1,
                    "stdout": "Test failed",
                    "stderr": "Error",
                },
                "fix_iteration": 0,
            },
        )

        mock_response = """{
            "verified": false,
            "issues": [{"severity": "error", "message": "Tests failed"}],
            "summary": "Tests failed",
            "recommendations": ["Fix tests"],
            "test_analysis": {
                "tests_passed": false,
                "failure_summary": "Test failure",
                "root_cause": "Bug"
            },
            "fix_instructions": "Fix the bug"
        }"""

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            result = verify_spec_handler(step, flow)

            assert result == StepStatus.REVISION_NEEDED
            assert step.outputs["fix_needed"] is True
            assert step.outputs["fix_instructions"] == "Fix the bug"

    def test_completed_returned_when_tests_pass(self, state_machine):
        """Test that COMPLETED is returned when tests pass."""
        from se3.engine.steps.verify_spec import verify_spec_handler

        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )

        step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Test task",
                "spec_content": {},
                "changes_made": {},
                "test_results": {
                    "passed": True,
                    "returncode": 0,
                    "stdout": "All tests passed",
                    "stderr": "",
                },
            },
        )

        mock_response = """{
            "verified": true,
            "issues": [],
            "summary": "All tests passed",
            "recommendations": [],
            "test_analysis": {
                "tests_passed": true,
                "failure_summary": "",
                "root_cause": ""
            },
            "fix_instructions": ""
        }"""

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            result = verify_spec_handler(step, flow)

            assert result == StepStatus.COMPLETED
            assert step.outputs["verified"] is True


class TestFixContextPassing:
    """Test cases for fix_context passing between steps."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine with mocked persistence."""
        with patch("se3.engine.state_machine.PersistenceManager"):
            return StateMachine(project_root=tmp_path)

    @pytest.fixture
    def flow_with_fix_context(self, tmp_path):
        """Create a flow with fix context to be passed."""
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
            outputs={
                "test_results": {
                    "passed": False,
                    "returncode": 1,
                    "stdout": "Test error output",
                    "stderr": "AssertionError",
                }
            },
        )
        flow.state.add_step(test_step)

        # Create verify_spec step with fix context
        fix_context = {
            "test_results": {"passed": False, "returncode": 1},
            "test_analysis": {"tests_passed": False, "root_cause": "Missing import"},
            "fix_instructions": "Add the missing import statement",
            "iteration": 1,
        }

        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Add the missing import statement",
                "fix_context": fix_context,
            },
        )
        flow.state.add_step(verify_step)
        flow.state.current_step_id = verify_step.step_id

        return flow, implement_step, verify_step, fix_context

    def test_fix_context_passed_to_implement_step(self, state_machine, flow_with_fix_context):
        """Test that fix_context is properly passed to implement step.

        Acceptance Criteria: Tests verify fix_context is properly passed between steps
        """
        flow, implement_step, verify_step, original_fix_context = flow_with_fix_context

        # Transition back to implement
        result = state_machine._transition_to_fix(flow, verify_step)

        assert result is not None
        assert result.step_id == implement_step.step_id

        # Verify fix context is passed
        assert "fix_context" in implement_step.inputs
        assert implement_step.inputs["fix_context"] == original_fix_context

    def test_fix_instructions_passed_to_implement_step(self, state_machine, flow_with_fix_context):
        """Test that fix_instructions is properly passed to implement step."""
        flow, implement_step, verify_step, _ = flow_with_fix_context

        # Transition back to implement
        state_machine._transition_to_fix(flow, verify_step)

        # Verify fix instructions are passed
        assert "fix_instructions" in implement_step.inputs
        assert implement_step.inputs["fix_instructions"] == "Add the missing import statement"

    def test_is_fix_iteration_flag_set(self, state_machine, flow_with_fix_context):
        """Test that is_fix_iteration flag is set on implement step."""
        flow, implement_step, verify_step, _ = flow_with_fix_context

        # Transition back to implement
        state_machine._transition_to_fix(flow, verify_step)

        # Verify is_fix_iteration flag is set
        assert "is_fix_iteration" in implement_step.inputs
        assert implement_step.inputs["is_fix_iteration"] is True

    def test_fix_iteration_count_passed(self, state_machine, flow_with_fix_context):
        """Test that fix_iteration count is passed to implement step."""
        flow, implement_step, verify_step, _ = flow_with_fix_context

        # Transition back to implement
        state_machine._transition_to_fix(flow, verify_step)

        # Verify fix iteration is passed and incremented
        assert "fix_iteration" in implement_step.inputs
        assert implement_step.inputs["fix_iteration"] == 1


class TestTestStepFailureHandling:
    """Test cases for test step behavior when tests fail."""

    def test_test_step_returns_completed_on_failure(self):
        """Test that test step returns COMPLETED (not FAILED) when tests fail.

        The test step should return COMPLETED so the flow continues to verify_spec,
        which then decides whether to trigger the fix loop.
        """
        from se3.engine.steps.test import test_handler

        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )

        step = Step(
            step_type=StepType.TEST,
            status=StepStatus.PENDING,
            inputs={},
        )

        # Must remove SE3_TEST_RUNNING so the recursion guard doesn't
        # short-circuit before our Popen mock gets called.
        env_without_guard = {k: v for k, v in os.environ.items() if k != "SE3_TEST_RUNNING"}
        with patch.dict("os.environ", env_without_guard, clear=True):
            with patch("se3.engine.steps.test.subprocess.Popen") as mock_popen:
                mock_process = Mock()
                mock_process.returncode = 1
                mock_process.communicate.return_value = ("Test output", "Test failed")
                mock_popen.return_value = mock_process

                result = test_handler(step, flow)

                assert result == StepStatus.COMPLETED
                assert step.outputs["test_results"]["passed"] is False
                assert step.outputs["test_results"]["returncode"] == 1

    def test_test_step_stores_detailed_results(self):
        """Test that test step stores detailed test results."""
        from se3.engine.steps.test import test_handler

        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )

        step = Step(
            step_type=StepType.TEST,
            status=StepStatus.PENDING,
            inputs={},
        )

        env_without_guard = {k: v for k, v in os.environ.items() if k != "SE3_TEST_RUNNING"}
        with patch.dict("os.environ", env_without_guard, clear=True):
            with patch("se3.engine.steps.test.subprocess.Popen") as mock_popen:
                mock_process = Mock()
                mock_process.returncode = 1
                mock_process.communicate.return_value = ("Test stdout content", "Test stderr content")
                mock_popen.return_value = mock_process

                result = test_handler(step, flow)

                assert result == StepStatus.COMPLETED
                test_results = step.outputs["test_results"]
                assert test_results["passed"] is False
                assert test_results["returncode"] == 1
                assert test_results["stdout"] == "Test stdout content"
                assert test_results["stderr"] == "Test stderr content"


class TestMaxIterationEnforcement:
    """Test cases for max iteration enforcement in verify_spec."""

    def test_max_iterations_enforced_by_verify_spec(self):
        """Test that verify_spec enforces max iterations and returns COMPLETED at max.

        Acceptance Criteria: Tests verify fix loop exits after max iterations
        """
        from se3.engine.steps.verify_spec import verify_spec_handler

        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        flow.state.context["max_fix_iterations"] = 3

        step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Test task",
                "spec_content": {},
                "changes_made": {},
                "test_results": {
                    "passed": False,
                    "returncode": 1,
                    "stdout": "Failed",
                    "stderr": "Error",
                },
                "fix_iteration": 3,  # At max iterations
            },
        )

        mock_response = """{
            "verified": false,
            "issues": [{"severity": "error", "message": "Tests still failing"}],
            "summary": "Tests failed after max iterations",
            "recommendations": [],
            "test_analysis": {
                "tests_passed": false,
                "failure_summary": "Still failing",
                "root_cause": "Unknown"
            },
            "fix_instructions": "Manual fix needed"
        }"""

        with patch("se3.engine.steps.verify_spec.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller

            result = verify_spec_handler(step, flow)

            # Should return COMPLETED when max iterations reached, not REVISION_NEEDED
            assert result == StepStatus.COMPLETED
            assert step.outputs.get("max_iterations_reached") is True


class TestFixLoopIntegration:
    """Integration tests for the complete fix loop mechanism."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine with mocked persistence."""
        with patch("se3.engine.state_machine.PersistenceManager"):
            return StateMachine(project_root=tmp_path)

    def test_complete_fix_loop_workflow(self, state_machine):
        """Test a complete fix loop workflow from test failure to fix."""
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERIFY_SPEC,
            StepType.COMMIT,
        ]

        # Initial implement step
        implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": ["feature.py"]},
        )
        flow.state.add_step(implement_step)

        # Test step with failure
        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={
                "test_results": {
                    "passed": False,
                    "returncode": 1,
                    "stdout": "FAILED test_feature.py::test_something",
                    "stderr": "AssertionError: expected 42 but got None",
                }
            },
        )
        flow.state.add_step(test_step)

        # Verify step requesting revision
        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Fix test_feature.py - function should return 42",
                "fix_context": {
                    "test_failed": True,
                    "test_results": test_step.outputs["test_results"],
                },
            },
        )
        flow.state.add_step(verify_step)
        flow.state.current_step_id = verify_step.step_id

        # Transition to fix
        next_step = state_machine.transition_to_next(flow)

        # Should go back to implement
        assert next_step is not None
        assert next_step.step_type == StepType.IMPLEMENT
        assert next_step.step_id == implement_step.step_id

        # Verify fix context was passed
        assert implement_step.inputs["is_fix_iteration"] is True
        assert implement_step.inputs["fix_iteration"] == 1
        assert "fix the assertion" in implement_step.inputs["fix_instructions"].lower() or \
               "fix test_feature" in implement_step.inputs["fix_instructions"].lower()

        # Verify flow iteration tracking
        assert flow.state.get_fix_iteration() == 1
        assert len(flow.state.fix_history) == 1


class TestConfigMaxIterations:
    """Test cases for max_fix_iterations configuration."""

    def test_max_fix_iterations_from_se3_yaml(self, tmp_path):
        """Test that max_fix_iterations can be configured via se3.yaml."""
        from se3.config import get_max_fix_iterations

        # Create se3.yaml with custom max_fix_iterations
        config_content = """
workflow:
  max_fix_iterations: 5
"""
        (tmp_path / "se3.yaml").write_text(config_content)

        result = get_max_fix_iterations(tmp_path)

        assert result == 5

    def test_max_fix_iterations_default(self, tmp_path):
        """Test default max_fix_iterations when not configured."""
        from se3.config import get_max_fix_iterations

        result = get_max_fix_iterations(tmp_path)

        assert result == 3

    def test_max_fix_iterations_no_config_file(self, tmp_path):
        """Test default when se3.yaml doesn't exist."""
        from se3.config import get_max_fix_iterations

        non_existent_path = tmp_path / "non_existent"
        result = get_max_fix_iterations(non_existent_path)

        assert result == 3
