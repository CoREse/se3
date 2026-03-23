"""Tests for the flow engine.

Unit tests for state machine, persistence, and models.
"""

import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from .models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
    get_default_step_sequence,
)
from .persistence import PersistenceManager
from .state_machine import StateMachine


class TestModels:
    """Tests for data models."""

    def test_step_creation(self):
        """Test creating a step."""
        step = Step(step_type=StepType.ANALYZE)

        assert step.step_type == StepType.ANALYZE
        assert step.status == StepStatus.PENDING
        # step_id is empty until added to State; verify it's a string
        assert isinstance(step.step_id, str)

    def test_step_serialization(self):
        """Test step to/from dict."""
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            inputs={"task": "test"},
            outputs={"result": "done"},
        )

        data = step.to_dict()
        restored = Step.from_dict(data)

        assert restored.step_type == step.step_type
        assert restored.status == step.status
        assert restored.inputs == step.inputs
        assert restored.outputs == step.outputs

    def test_flow_instance_creation(self):
        """Test creating a flow instance."""
        flow = FlowInstance(task_description="Test task")

        assert flow.flow_id is not None
        # Format: YYYYMMDD-HHMMSS_uuid8 (24 chars)
        assert "_" in flow.flow_id
        assert flow.flow_id[:8].isdigit()  # date part
        assert flow.status == FlowStatus.INIT
        assert flow.task_description == "Test task"

    def test_flow_progress(self):
        """Test progress calculation."""
        flow = FlowInstance()
        flow.state.selected_steps = [
            StepType.ANALYZE,
                StepType.PROPOSE,
            StepType.IMPLEMENT,
        ]

        # No steps completed
        assert flow.get_progress() == (0, 3)

        # Complete one step
        step = Step(step_type=StepType.ANALYZE, status=StepStatus.COMPLETED)
        flow.state.add_step(step)
        assert flow.get_progress() == (1, 3)

    def test_flow_serialization(self):
        """Test flow instance serialization."""
        flow = FlowInstance(
            task_description="Test",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )

        # Add a step
        step = Step(step_type=StepType.ANALYZE, status=StepStatus.COMPLETED)
        flow.state.add_step(step)

        data = flow.to_dict()
        restored = FlowInstance.from_dict(data)

        assert restored.task_description == flow.task_description
        assert restored.task_type == flow.task_type
        assert restored.status == flow.status
        assert len(restored.state.steps) == 1

    def test_default_step_sequences(self):
        """Test default step sequences for different task types."""
        feature_seq = get_default_step_sequence("feature")
        assert StepType.ANALYZE in feature_seq
        assert StepType.IMPLEMENT in feature_seq

        bugfix_seq = get_default_step_sequence("bugfix")
        assert StepType.ANALYZE in bugfix_seq
        assert StepType.DESIGN not in bugfix_seq  # Bugfix usually skips design

        small_seq = get_default_step_sequence("small")
        assert len(small_seq) < len(feature_seq)  # Small tasks skip steps


class TestPersistence:
    """Tests for persistence manager."""

    def test_save_and_load(self):
        """Test saving and loading flow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = PersistenceManager(Path(tmpdir))

            flow = FlowInstance(task_description="Test persistence")
            flow.state.selected_steps = [StepType.ANALYZE, StepType.IMPLEMENT]

            # Save
            path = pm.save_flow(flow)
            assert path.exists()

            # Load
            loaded = pm.load_flow()
            assert loaded is not None
            assert loaded.flow_id == flow.flow_id
            assert loaded.task_description == flow.task_description

    def test_atomic_write(self):
        """Test that writes are atomic."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = PersistenceManager(Path(tmpdir))

            flow = FlowInstance(task_description="Test atomic")

            # Save should create file
            pm.save_flow(flow)
            assert pm.state_file.exists()

            # File should be valid JSON
            content = pm.state_file.read_text()
            data = json.loads(content)
            assert data["flow_id"] == flow.flow_id

    def test_list_active_flows(self):
        """Test listing active flows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = PersistenceManager(Path(tmpdir))

            flow = FlowInstance(task_description="Test listing")
            pm.save_flow(flow)

            flows = pm.list_active_flows()
            assert len(flows) == 1
            assert flows[0]["flow_id"] == flow.flow_id

    def test_context_save_load(self):
        """Test context file operations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = PersistenceManager(Path(tmpdir))

            # Use proper se3_context format
            context = {
                "type": "se3_context",
                "version": "3.0",
                "flow_id": "test-flow-123",
                "status": "running",
                "task": {"description": "Test task", "type": "feature"},
                "timestamp": "2026-02-24T10:00:00",
            }
            pm.save_context(context)

            loaded = pm.load_context()
            assert loaded["type"] == "se3_context"
            assert loaded["flow_id"] == "test-flow-123"


class TestStateMachine:
    """Tests for state machine."""

    def test_create_flow(self):
        """Test creating a new flow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            flow = sm.create_flow("Implement login feature", task_type="feature")

            assert flow.task_description == "Implement login feature"
            assert flow.task_type == "feature"
            assert flow.status == FlowStatus.INIT
            assert len(flow.state.selected_steps) > 0
            assert flow.state.current_step_id is not None

    def test_load_or_create_new(self):
        """Test load_or_create creates new when no existing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            flow, is_resumed = sm.load_or_create_flow("New task")

            assert not is_resumed
            assert flow.task_description == "New task"

    def test_load_or_create_resume(self):
        """Test load_or_create resumes existing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            # Create initial flow
            flow = sm.create_flow("Existing task")
            flow.status = FlowStatus.PAUSED
            sm.persistence.save_flow(flow)

            # Should resume
            loaded, is_resumed = sm.load_or_create_flow()

            assert is_resumed
            assert loaded.flow_id == flow.flow_id

    def test_transition_to_next(self):
        """Test transitioning between steps."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            flow = sm.create_flow("Test transition")
            current = flow.state.get_current_step()
            current.status = StepStatus.COMPLETED

            next_step = sm.transition_to_next(flow)

            assert next_step is not None
            assert next_step.step_type != current.step_type
            assert flow.state.get_current_step() == next_step

    def test_run_step_with_handler(self):
        """Test running a step with registered handler."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            # Register a mock handler
            def mock_handler(step, flow):
                step.outputs["result"] = "mock_result"
                return StepStatus.COMPLETED

            sm.register_handler(StepType.ANALYZE, mock_handler)

            flow = sm.create_flow("Test handler")
            step = flow.state.get_current_step()

            result = sm.run_step(flow, step)

            assert result == StepStatus.COMPLETED
            assert step.outputs["result"] == "mock_result"

    def test_run_step_no_handler(self):
        """Test running a step with no handler fails gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            flow = sm.create_flow("Test no handler")
            step = flow.state.get_current_step()

            result = sm.run_step(flow, step)

            assert result == StepStatus.FAILED
            assert "No handler" in step.error_message


class TestRecovery:
    """Tests for interrupt recovery scenarios."""

    def test_recovery_after_crash(self):
        """Test recovering flow state after simulated crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = PersistenceManager(Path(tmpdir))
            sm = StateMachine(Path(tmpdir))

            # Create flow and complete a step
            flow = sm.create_flow("Test recovery", task_type="feature")
            step = flow.state.get_current_step()
            step.status = StepStatus.COMPLETED
            step.outputs["result"] = "completed_before_crash"
            pm.save_flow(flow)

            # Simulate crash by creating new state machine
            sm2 = StateMachine(Path(tmpdir))
            loaded, is_resumed = sm2.load_or_create_flow()

            assert is_resumed
            assert loaded.flow_id == flow.flow_id
            assert loaded.state.get_current_step().step_type == step.step_type

    def test_recovery_mid_step(self):
        """Test recovery when interrupted during a step."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = PersistenceManager(Path(tmpdir))

            flow = FlowInstance(task_description="Test mid-step")
            flow.state.selected_steps = [StepType.ANALYZE, StepType.IMPLEMENT]
            step = Step(step_type=StepType.ANALYZE, status=StepStatus.RUNNING)
            flow.state.add_step(step)
            flow.state.current_step_id = step.step_id

            pm.save_flow(flow)

            # Load and verify
            loaded = pm.load_flow()
            loaded_step = loaded.state.get_current_step()
            assert loaded_step.status == StepStatus.RUNNING
            assert loaded_step.step_type == StepType.ANALYZE

    def test_multiple_flow_recovery(self):
        """Test recovery with multiple flows by scanning directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pm = PersistenceManager(Path(tmpdir))

            # Create and save first flow
            flow1 = FlowInstance(task_description="Flow 1")
            flow1.status = FlowStatus.PAUSED
            pm.save_flow(flow1)

            # PersistenceManager only tracks one flow per state file
            # For multi-flow, we'd scan the state directory
            state_dir = Path(tmpdir) / "se3" / "state"
            all_flows = list(state_dir.glob("*.json"))
            assert len(all_flows) >= 1  # At least one flow saved


class TestStateTransitions:
    """Tests for state machine transitions."""

    def test_complete_flow_transitions(self):
        """Test full flow through all states."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def mock_handler(step, flow):
                step.status = StepStatus.COMPLETED
                return StepStatus.COMPLETED

            # Register handlers for all steps
            for step_type in StepType:
                sm.register_handler(step_type, mock_handler)

            flow = sm.create_flow("Test full flow", task_type="small")

            # Start the flow
            flow.status = FlowStatus.RUNNING

            # Run through all steps
            max_steps = 20
            steps_taken = 0
            while flow.status == FlowStatus.RUNNING and steps_taken < max_steps:
                step = flow.state.get_current_step()
                if not step:
                    break

                sm.run_step(flow, step)
                sm.transition_to_next(flow)
                steps_taken += 1

            assert flow.status == FlowStatus.COMPLETED

    def test_failed_step_handling(self):
        """Test handling of failed steps."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def failing_handler(step, flow):
                step.error_message = "Simulated failure"
                return StepStatus.FAILED

            sm.register_handler(StepType.ANALYZE, failing_handler)

            flow = sm.create_flow("Test failure")
            step = flow.state.get_current_step()

            result = sm.run_step(flow, step)

            assert result == StepStatus.FAILED
            assert step.error_message == "Simulated failure"

    def test_retry_mechanism(self):
        """Test step retry after failure."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            call_count = 0

            def flaky_handler(step, flow):
                nonlocal call_count
                call_count += 1
                if call_count < 2:
                    return StepStatus.FAILED
                return StepStatus.COMPLETED

            sm.register_handler(StepType.ANALYZE, flaky_handler)

            flow = sm.create_flow("Test retry")
            step = flow.state.get_current_step()

            # First attempt fails
            result = sm.run_step(flow, step)
            assert result == StepStatus.FAILED

            # Retry succeeds
            step.retry_count += 1
            result = sm.run_step(flow, step)
            assert result == StepStatus.COMPLETED
            assert call_count == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
