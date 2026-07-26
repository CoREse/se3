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

from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.state_machine import StateMachine


class TestFixLoopIterations:
    """Test cases for fix loop iteration behavior and max iteration enforcement."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine with mocked persistence."""
        with patch("tianluo.engine.state_machine.PersistenceManager"):
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
                    # At max iterations, flow should be FAILED and return None
                    assert next_step is None, "Max iteration: Should return None (flow failed)"
                    assert flow.status == FlowStatus.FAILED, "Max iteration: Flow should be FAILED"

    def test_fix_loop_exits_after_max_iterations(self, state_machine, flow_with_failed_tests):
        """Test that fix loop exits after max iterations reached.

        Acceptance Criteria: Tests verify fix loop exits after max iterations
        """
        flow, _, _, verify_step = flow_with_failed_tests

        # Set fix iterations at max
        flow.state.fix_iterations = 3

        with patch.object(state_machine, '_get_max_fix_iterations', return_value=3):
            next_step = state_machine.transition_to_next(flow)

            # Should return None and set flow to FAILED
            assert next_step is None
            assert flow.status == FlowStatus.FAILED
            assert flow.state.get_fix_iteration() == 3  # Should not increment


class TestFixContextPassing:
    """Test cases for fix_context passing between steps."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine with mocked persistence."""
        with patch("tianluo.engine.state_machine.PersistenceManager"):
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
    """Test cases for test step behavior when tests fail.

    The TEST step now triggers the fix loop directly when tests fail,
    skipping verify_spec for faster iteration. These exercise the handler
    against the shared ``_run_command`` core (mocked at that boundary) so we
    avoid spawning a real subprocess.
    """

    # A failed pytest run whose single failure is NOT in the baseline →
    # classified as an introduced regression → triggers the fix loop.
    _STDOUT_REGRESSION_FAIL = (
        "tests/test_a.py::test_one PASSED\n"
        "tests/test_a.py::test_two FAILED\n"
    )

    @staticmethod
    def _run_result(stdout: str, stderr: str = "Test failed") -> dict:
        return {
            "command": "python -m pytest -v",
            "returncode": 1,
            "stdout": stdout,
            "stderr": stderr,
            "passed": False,
        }

    def _make_step(self) -> Step:
        return Step(
            step_type=StepType.TEST,
            status=StepStatus.PENDING,
            inputs={"baseline_failures": [], "tests_added": []},
        )

    def test_test_step_returns_revision_needed_on_failure(self):
        """Test that test step returns REVISION_NEEDED when tests fail."""
        from tianluo.engine.steps.test import test_handler

        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        step = self._make_step()

        with patch("tianluo.config.TestConfig") as mock_config, \
             patch("tianluo.engine.steps.test._run_command") as mock_run, \
             patch("tianluo.engine.steps.test._record_test_history"), \
             patch("tianluo.engine.steps.test._report_pre_existing_issues"):
            mock_config.load.return_value = MagicMock(
                command="python -m pytest -v", timeout=60,
                get_phases_for_run=MagicMock(return_value=[]),
            )
            mock_run.return_value = self._run_result(self._STDOUT_REGRESSION_FAIL)

            result = test_handler(step, flow)

            assert result == StepStatus.REVISION_NEEDED
            assert step.outputs["test_results"]["passed"] is False
            assert step.outputs["test_results"]["returncode"] == 1
            assert step.outputs["fix_needed"] is True
            assert "fix_instructions" in step.outputs
            assert "fix_context" in step.outputs

    def test_test_step_stores_detailed_results_and_fix_context(self):
        """Test that test step stores detailed test results and fix context when failing."""
        from tianluo.engine.steps.test import test_handler

        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        step = self._make_step()

        with patch("tianluo.config.TestConfig") as mock_config, \
             patch("tianluo.engine.steps.test._run_command") as mock_run, \
             patch("tianluo.engine.steps.test._record_test_history"), \
             patch("tianluo.engine.steps.test._report_pre_existing_issues"):
            mock_config.load.return_value = MagicMock(
                command="python -m pytest -v", timeout=60,
                get_phases_for_run=MagicMock(return_value=[]),
            )
            mock_run.return_value = self._run_result(
                self._STDOUT_REGRESSION_FAIL, stderr="Test stderr content"
            )

            result = test_handler(step, flow)

            assert result == StepStatus.REVISION_NEEDED
            test_results = step.outputs["test_results"]
            assert test_results["passed"] is False
            assert test_results["returncode"] == 1
            assert test_results["stderr"] == "Test stderr content"
            # Verify fix loop context is stored
            assert step.outputs["fix_needed"] is True
            assert "fix_instructions" in step.outputs
            fix_context = step.outputs["fix_context"]
            assert fix_context["test_failed"] is True
            assert fix_context["reason"] == "test_failure"


class TestFixLoopIntegration:
    """Integration tests for the complete fix loop mechanism."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine with mocked persistence."""
        with patch("tianluo.engine.state_machine.PersistenceManager"):
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
        """Test that max_fix_iterations can be configured via tianluo.yaml."""
        from tianluo.config import get_max_fix_iterations

        # Create tianluo.yaml with custom max_fix_iterations
        config_content = """
workflow:
  max_fix_iterations: 5
"""
        (tmp_path / "tianluo.yaml").write_text(config_content)

        result = get_max_fix_iterations(tmp_path)

        assert result == 5

    def test_max_fix_iterations_default(self, tmp_path):
        """Test default max_fix_iterations when not configured."""
        from tianluo.config import DEFAULT_MAX_FIX_ITERATIONS, get_max_fix_iterations

        result = get_max_fix_iterations(tmp_path)

        assert result == DEFAULT_MAX_FIX_ITERATIONS == 100

    def test_max_fix_iterations_no_config_file(self, tmp_path):
        """Test default when tianluo.yaml doesn't exist."""
        from tianluo.config import DEFAULT_MAX_FIX_ITERATIONS, get_max_fix_iterations

        non_existent_path = tmp_path / "non_existent"
        result = get_max_fix_iterations(non_existent_path)

        assert result == DEFAULT_MAX_FIX_ITERATIONS == 100
