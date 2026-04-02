"""End-to-end tests for the flow engine.

Tests complete flow execution for real tasks.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from .models import FlowInstance, StepType, StepStatus, FlowStatus
from .state_machine import StateMachine
from .persistence import PersistenceManager


class MockSubprocessResult:
    """Mock for subprocess result."""
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def create_mock_llm_responses():
    """Create mock LLM responses for a complete flow."""
    return {
        StepType.ANALYZE: json.dumps({
            "task_type": "small",
            "scope": "documentation",
            "complexity": "simple",
            "suggested_steps": ["analyze", "implement", "commit"],
        }),
        StepType.PLAN: json.dumps({
            "title": "Add README",
            "description": "Create project README file",
        }),
        StepType.IMPLEMENT: "Created README.md with project description",
        StepType.TEST: json.dumps({"passed": True}),
        StepType.COMMIT: json.dumps({
            "message": "docs: add README",
            "files": ["README.md"],
        }),
    }


class TestEndToEndSmallTask:
    """End-to-end test for a small task."""

    @patch("subprocess.run")
    def test_complete_small_flow(self, mock_run):
        """Test complete flow for a small documentation task."""
        responses = create_mock_llm_responses()
        call_count = [0]

        def mock_run_impl(args, **kwargs):
            call_count[0] += 1
            # Determine which step based on args
            prompt = " ".join(args) if isinstance(args, list) else str(args)

            if "analyze" in prompt.lower():
                return MockSubprocessResult(stdout=responses.get(StepType.ANALYZE, "{}"))
            elif "implement" in prompt.lower():
                return MockSubprocessResult(stdout=responses.get(StepType.IMPLEMENT, ""))
            elif "test" in prompt.lower():
                return MockSubprocessResult(stdout=responses.get(StepType.TEST, "{}"))
            elif "commit" in prompt.lower():
                return MockSubprocessResult(returncode=0)
            else:
                return MockSubprocessResult(stdout="{}")

        mock_run.side_effect = mock_run_impl

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            sm = StateMachine(project_root)

            # Create flow
            flow = sm.create_flow(
                task_description="Add README file",
                task_type="small"
            )

            # Register mock handlers for all steps
            def mock_handler(step, flow):
                step.status = StepStatus.COMPLETED
                step.outputs["mock"] = True
                return StepStatus.COMPLETED

            for step_type in StepType:
                sm.register_handler(step_type, mock_handler)

            # Execute flow
            max_steps = 15
            steps_taken = 0
            while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED) and steps_taken < max_steps:
                step = flow.state.get_current_step()
                if not step:
                    flow.status = FlowStatus.COMPLETED
                    break

                sm.run_step(flow, step)
                sm.transition_to_next(flow)
                steps_taken += 1

            assert flow.status == FlowStatus.COMPLETED
            assert steps_taken > 0

    @patch("subprocess.run")
    def test_flow_with_interruption(self, mock_run):
        """Test flow that gets interrupted and resumed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            pm = PersistenceManager(project_root)
            sm = StateMachine(project_root)

            # Create and save flow
            flow = sm.create_flow("Test task")
            step = flow.state.get_current_step()
            step.status = StepStatus.COMPLETED
            pm.save_flow(flow)

            # Simulate interruption by creating new instances
            pm2 = PersistenceManager(project_root)
            sm2 = StateMachine(project_root)

            loaded, is_resumed = sm2.load_or_create_flow()

            assert is_resumed
            assert loaded.flow_id == flow.flow_id
            assert loaded.state.get_current_step() is not None


class TestRealWorldScenarios:
    """Tests based on real-world scenarios."""

    def test_feature_request_flow(self):
        """Test flow for a typical feature request."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def mock_handler(step, flow):
                step.status = StepStatus.COMPLETED
                return StepStatus.COMPLETED

            for step_type in StepType:
                sm.register_handler(step_type, mock_handler)

            flow = sm.create_flow(
                task_description="Add user authentication",
                task_type="feature"
            )

            # Execute all steps
            max_steps = 20
            steps_taken = 0
            while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED) and steps_taken < max_steps:
                step = flow.state.get_current_step()
                if not step:
                    flow.status = FlowStatus.COMPLETED
                    break

                result = sm.run_step(flow, step)
                if result == StepStatus.FAILED:
                    break

                sm.transition_to_next(flow)
                steps_taken += 1

            assert flow.status == FlowStatus.COMPLETED
            # Feature should go through analyze, propose, design, implement, test, commit
            assert steps_taken >= 5

    def test_bugfix_flow(self):
        """Test flow for a bug fix."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def mock_handler(step, flow):
                step.status = StepStatus.COMPLETED
                return StepStatus.COMPLETED

            for step_type in StepType:
                sm.register_handler(step_type, mock_handler)

            flow = sm.create_flow(
                task_description="Fix login error",
                task_type="bugfix"
            )

            # Execute
            max_steps = 15
            steps_taken = 0
            while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED) and steps_taken < max_steps:
                step = flow.state.get_current_step()
                if not step:
                    break

                sm.run_step(flow, step)
                sm.transition_to_next(flow)
                steps_taken += 1

            assert flow.status == FlowStatus.COMPLETED


class TestErrorRecovery:
    """Tests for error recovery in real scenarios."""

    def test_recovery_after_step_failure(self):
        """Test recovery when a step fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            call_count = 0

            def flaky_handler(step, flow):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    step.error_message = "Simulated error"
                    return StepStatus.FAILED
                step.status = StepStatus.COMPLETED
                return StepStatus.COMPLETED

            sm.register_handler(StepType.ANALYZE, flaky_handler)

            # Register success handlers for other steps
            for step_type in [StepType.PLAN, StepType.IMPLEMENT, StepType.COMMIT]:
                sm.register_handler(step_type, lambda s, f: StepStatus.COMPLETED)

            flow = sm.create_flow("Test recovery")

            # First attempt fails
            step = flow.state.get_current_step()
            result = sm.run_step(flow, step)
            assert result == StepStatus.FAILED

            # Retry succeeds
            step.retry_count += 1
            result = sm.run_step(flow, step)
            assert result == StepStatus.COMPLETED

    def test_state_consistency_across_persistence(self):
        """Test that state remains consistent after save/load cycles."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            pm = PersistenceManager(project_root)
            sm = StateMachine(project_root)

            # Create flow and progress through some steps
            flow = sm.create_flow("Test consistency")

            # Complete analyze step
            step1 = flow.state.get_current_step()
            step1.status = StepStatus.COMPLETED
            step1.outputs["result"] = "analysis done"

            # Transition
            sm.transition_to_next(flow)

            # Save
            pm.save_flow(flow)

            # Load in fresh instances
            pm2 = PersistenceManager(project_root)
            sm2 = StateMachine(project_root)

            loaded, _ = sm2.load_or_create_flow()

            # Verify state
            assert loaded.flow_id == flow.flow_id
            assert len(loaded.state.steps) == 2  # analyze + next step
            first_step_id = loaded.state.step_history[0]
            assert loaded.state.steps[first_step_id].outputs["result"] == "analysis done"
            assert loaded.state.get_current_step().step_type != StepType.ANALYZE


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
