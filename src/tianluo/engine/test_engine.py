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
from .steps import charter_freshness


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
                StepType.PLAN,
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

    def test_charter_refactor_sequences_exact(self):
        """Each task type's sequence matches the charter refactor (task book item 12)."""
        expected = {
            "feature": [
                StepType.ANALYZE, StepType.PLAN, StepType.IMPLEMENT, StepType.TEST,
                StepType.SELF_CHECK, StepType.INVARIANT_CHECK, StepType.CHARTER_FRESHNESS,
                StepType.VERSION_ANALYZE, StepType.COMMIT, StepType.SUMMARIZE,
            ],
            "bugfix": [
                StepType.ANALYZE, StepType.PLAN, StepType.IMPLEMENT, StepType.TEST,
                StepType.SELF_CHECK, StepType.INVARIANT_CHECK, StepType.CHARTER_FRESHNESS,
                StepType.VERSION_ANALYZE, StepType.COMMIT, StepType.SUMMARIZE,
            ],
            "review": [
                StepType.ANALYZE, StepType.INVARIANT_CHECK, StepType.SUMMARIZE,
            ],
            "small": [
                StepType.ANALYZE, StepType.IMPLEMENT, StepType.TEST,
                StepType.CHARTER_FRESHNESS, StepType.VERSION_ANALYZE,
                StepType.COMMIT, StepType.SUMMARIZE,
            ],
            "directive": [
                StepType.ANALYZE, StepType.PLAN, StepType.IMPLEMENT,
                StepType.CHARTER_FRESHNESS, StepType.VERSION_ANALYZE,
                StepType.COMMIT, StepType.SUMMARIZE,
            ],
            "discovery": [
                StepType.DISCOVERY, StepType.ANALYZE, StepType.PLAN, StepType.IMPLEMENT,
                StepType.TEST, StepType.SELF_CHECK, StepType.INVARIANT_CHECK,
                StepType.CHARTER_FRESHNESS, StepType.VERSION_ANALYZE, StepType.COMMIT,
                StepType.SUMMARIZE,
            ],
        }
        for task_type, seq in expected.items():
            assert get_default_step_sequence(task_type) == seq, task_type

    def test_retired_spec_steps_absent_from_all_sequences(self):
        """The retired spec governance steps appear in no default sequence."""
        retired = {StepType.VERIFY_SPEC, StepType.UPDATE_SPEC, StepType.SPEC_GATE}
        for task_type in ("feature", "bugfix", "review", "small", "directive", "discovery"):
            seq = set(get_default_step_sequence(task_type))
            assert not (seq & retired), f"{task_type} still contains a retired spec step"

    def test_invariant_check_placement(self):
        """INVARIANT_CHECK follows SELF_CHECK; CHARTER_FRESHNESS precedes VERSION_ANALYZE."""
        for task_type in ("feature", "bugfix", "discovery"):
            seq = get_default_step_sequence(task_type)
            assert seq.index(StepType.INVARIANT_CHECK) == seq.index(StepType.SELF_CHECK) + 1
            assert seq.index(StepType.CHARTER_FRESHNESS) == seq.index(StepType.VERSION_ANALYZE) - 1
        # review routes ANALYZE -> INVARIANT_CHECK -> SUMMARIZE (no self_check upstream).
        review = get_default_step_sequence("review")
        assert review.index(StepType.INVARIANT_CHECK) == review.index(StepType.ANALYZE) + 1
        # small / directive get only the non-blocking CHARTER_FRESHNESS, no INVARIANT_CHECK.
        for task_type in ("small", "directive"):
            seq = get_default_step_sequence(task_type)
            assert StepType.INVARIANT_CHECK not in seq
            assert seq.index(StepType.CHARTER_FRESHNESS) == seq.index(StepType.VERSION_ANALYZE) - 1

    def test_default_sequences_end_with_summarize(self):
        """summarize is the final step of every default task-type sequence."""
        for task_type in (
            "feature",
            "bugfix",
            "review",
            "small",
            "directive",
            "discovery",
        ):
            seq = get_default_step_sequence(task_type)
            assert seq[-1] == StepType.SUMMARIZE, (
                f"{task_type} sequence must end with SUMMARIZE"
            )
            assert seq.count(StepType.SUMMARIZE) == 1

        # Non-review sequences place SUMMARIZE immediately after COMMIT.
        for task_type in ("feature", "bugfix", "small", "directive", "discovery"):
            seq = get_default_step_sequence(task_type)
            assert seq.index(StepType.SUMMARIZE) == seq.index(StepType.COMMIT) + 1

        # Unknown task type falls back to feature, which includes summarize.
        assert get_default_step_sequence("???")[-1] == StepType.SUMMARIZE


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

    def test_run_step_on_running_called_after_running(self):
        """on_running fires exactly once, AFTER the step is RUNNING and before
        the handler runs — so the orchestrator only persists a 'running' anchor
        for a step that genuinely entered RUNNING."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            seen = []

            def mock_handler(step, flow):
                # By the time the handler runs, on_running must already have
                # observed the RUNNING status.
                seen.append(("handler", step.status))
                return StepStatus.COMPLETED

            sm.register_handler(StepType.ANALYZE, mock_handler)
            flow = sm.create_flow("Test on_running")
            step = flow.state.get_current_step()

            def on_running(s):
                seen.append(("on_running", s.status))

            sm.run_step(flow, step, on_running=on_running)

            assert seen[0] == ("on_running", StepStatus.RUNNING)
            assert seen[1][0] == "handler"
            # Exactly one on_running invocation.
            assert [s for s in seen if s[0] == "on_running"] == [
                ("on_running", StepStatus.RUNNING)
            ]

    def test_run_step_on_running_not_called_without_handler(self):
        """A missing-handler step fails BEFORE entering RUNNING, so on_running
        is never invoked — no dangling 'running' anchor is left behind."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))
            flow = sm.create_flow("Test no handler on_running")
            step = flow.state.get_current_step()

            called = []
            result = sm.run_step(
                flow, step, on_running=lambda s: called.append(s))

            assert result == StepStatus.FAILED
            assert called == []


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
            state_dir = Path(tmpdir) / "tianluo" / "state"
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

    def test_completion_advances_step_index_to_total(self):
        """On completion, current_step_index advances to len(selected_steps).

        This unifies the "completed steps / total steps" counting semantics so
        every consumer of engine state (aggregator, history, web console)
        reports total/total (e.g. 13/13) and progress 1.0.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def mock_handler(step, flow):
                step.status = StepStatus.COMPLETED
                return StepStatus.COMPLETED

            for step_type in StepType:
                sm.register_handler(step_type, mock_handler)

            flow = sm.create_flow("Test completion index", task_type="small")
            flow.status = FlowStatus.RUNNING

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
            assert flow.state.current_step_index == len(flow.state.selected_steps)

    def test_resume_completed_flow_does_not_raise(self):
        """Calling transition_to_next on a completed flow self-heals the index.

        The completion branch leaves current_step_index out of range; the
        next transition recovers the real index via selected.index(...) rather
        than raising TransitionError.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def mock_handler(step, flow):
                step.status = StepStatus.COMPLETED
                return StepStatus.COMPLETED

            for step_type in StepType:
                sm.register_handler(step_type, mock_handler)

            flow = sm.create_flow("Test resume completed", task_type="small")
            flow.status = FlowStatus.RUNNING

            steps_taken = 0
            while flow.status == FlowStatus.RUNNING and steps_taken < 20:
                step = flow.state.get_current_step()
                if not step:
                    break
                sm.run_step(flow, step)
                sm.transition_to_next(flow)
                steps_taken += 1

            assert flow.state.current_step_index == len(flow.state.selected_steps)
            # Out-of-range index must not raise on a subsequent transition.
            result = sm.transition_to_next(flow)
            assert result is None

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


class TestCharterRefactorRouting:
    """G6: INVARIANT_CHECK joins the fix loop; CHARTER_FRESHNESS is non-blocking;
    all six task-type sequences run through the state machine."""

    def _run_to_completion(self, sm, flow, max_steps=40):
        flow.status = FlowStatus.RUNNING
        taken = 0
        while flow.status == FlowStatus.RUNNING and taken < max_steps:
            step = flow.state.get_current_step()
            if not step:
                break
            sm.run_step(flow, step)
            sm.transition_to_next(flow)
            taken += 1
        return taken

    def test_all_six_sequences_run_through(self):
        """Every task type's default sequence drives to COMPLETED with mock handlers."""
        for task_type in ("feature", "bugfix", "review", "small", "directive", "discovery"):
            with tempfile.TemporaryDirectory() as tmpdir:
                sm = StateMachine(Path(tmpdir))

                def mock_handler(step, flow):
                    return StepStatus.COMPLETED

                for step_type in StepType:
                    sm.register_handler(step_type, mock_handler)

                flow = sm.create_flow(f"seq {task_type}", task_type=task_type)
                sm.init_flow(flow)
                self._run_to_completion(sm, flow)
                assert flow.status == FlowStatus.COMPLETED, task_type

    def test_invariant_check_revision_routes_to_implement_fix_loop(self):
        """INVARIANT_CHECK REVISION_NEEDED routes back to IMPLEMENT and bumps fix_iteration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            invariant_calls = {"n": 0}

            def mock_handler(step, flow):
                return StepStatus.COMPLETED

            def invariant_handler(step, flow):
                invariant_calls["n"] += 1
                if invariant_calls["n"] == 1:
                    # First pass flags a recorded-invariant violation.
                    step.outputs["fix_needed"] = True
                    step.outputs["fix_instructions"] = "restore the documented invariant"
                    step.outputs["fix_context"] = {"reason": "invariant_check", "issues": []}
                    return StepStatus.REVISION_NEEDED
                return StepStatus.COMPLETED

            for step_type in StepType:
                sm.register_handler(step_type, mock_handler)
            sm.register_handler(StepType.INVARIANT_CHECK, invariant_handler)

            flow = sm.create_flow("invariant fix loop", task_type="feature")
            sm.init_flow(flow)
            flow.status = FlowStatus.RUNNING

            assert flow.state.get_fix_iteration() == 0

            # Drive until the first INVARIANT_CHECK has fired and routed.
            saw_implement_after_invariant = False
            taken = 0
            while flow.status == FlowStatus.RUNNING and taken < 60:
                step = flow.state.get_current_step()
                if not step:
                    break
                sm.run_step(flow, step)
                nxt = sm.transition_to_next(flow)
                if (
                    invariant_calls["n"] == 1
                    and nxt is not None
                    and nxt.step_type == StepType.IMPLEMENT
                ):
                    saw_implement_after_invariant = True
                    assert flow.state.get_fix_iteration() == 1
                taken += 1

            assert saw_implement_after_invariant, "INVARIANT_CHECK did not route to the implement fix loop"
            assert invariant_calls["n"] >= 2, "INVARIANT_CHECK was not re-run after the fix"
            assert flow.status == FlowStatus.COMPLETED

    def test_invariant_check_shares_max_fix_iterations_exhaustion(self):
        """An always-failing INVARIANT_CHECK exhausts the shared bound and FAILs the flow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))
            # Force a tiny shared bound so the loop exhausts quickly.
            sm._get_max_fix_iterations = lambda: 2  # type: ignore[assignment]

            def mock_handler(step, flow):
                return StepStatus.COMPLETED

            def always_revision(step, flow):
                step.outputs["fix_needed"] = True
                step.outputs["fix_instructions"] = "still broken"
                step.outputs["fix_context"] = {"reason": "invariant_check", "issues": []}
                return StepStatus.REVISION_NEEDED

            for step_type in StepType:
                sm.register_handler(step_type, mock_handler)
            sm.register_handler(StepType.INVARIANT_CHECK, always_revision)

            flow = sm.create_flow("exhaust invariant", task_type="feature")
            sm.init_flow(flow)
            self._run_to_completion(sm, flow, max_steps=80)

            assert flow.status == FlowStatus.FAILED

    def test_charter_freshness_is_non_blocking(self):
        """CHARTER_FRESHNESS never routes a fix loop even if it 'flags' an update."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def mock_handler(step, flow):
                return StepStatus.COMPLETED

            def freshness_handler(step, flow):
                # Advisory output; must NOT divert the flow.
                step.outputs["charter_update_needed"] = True
                step.outputs["touched_classes"] = ["top-level architecture"]
                return StepStatus.COMPLETED

            for step_type in StepType:
                sm.register_handler(step_type, mock_handler)
            sm.register_handler(StepType.CHARTER_FRESHNESS, freshness_handler)

            flow = sm.create_flow("charter freshness", task_type="small")
            sm.init_flow(flow)
            self._run_to_completion(sm, flow)

            assert flow.status == FlowStatus.COMPLETED
            assert flow.state.get_fix_iteration() == 0

    def test_freeze_invariant_anchors_idempotent(self):
        """The anchor set is frozen once at flow start and not re-read on resume."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))
            flow = sm.create_flow("freeze anchors", task_type="feature")
            sm.init_flow(flow)

            anchors = flow.state.context.get("invariant_anchors")
            assert isinstance(anchors, dict)
            assert "charter" in anchors and "task_description" in anchors

            # Mutate then re-init: the one-shot guard must keep the frozen value.
            anchors["charter"] = "FROZEN-SENTINEL"
            sm.init_flow(flow)
            assert flow.state.context["invariant_anchors"]["charter"] == "FROZEN-SENTINEL"


class TestKnowledgeGuardIntegration:
    """G6: end-to-end, state-machine-level integration for the two knowledge
    guards, driven through the *real* routing in real task-type sequences (the
    isolated-handler behavior is covered by tests/test_charter_freshness.py and
    tests/test_why_comment_guard.py):

    - the charter_freshness closed loop (propose -> gate -> apply) auto-updates
      the on-disk charter and the flow proceeds straight to version_analyze,
      re-running NONE of the already-COMPLETED review steps (no reflow — the
      sub-edit deliberately does not re-trigger invariant_check/self_check/test);
    - a sequence without invariant_check keeps charter_freshness advisory-only;
    - an invariant_check WHY:-deletion REVISION_NEEDED rides the SAME shared
      implement fix loop (and the same shared max_fix_iterations bound) as
      TEST/SELF_CHECK, and coexists with the charter auto-update in one flow.
    """

    _DISK_CHARTER = (
        "# Charter\n\n"
        "## Purpose\n"
        "The alpha subsystem drives the widget loop.\n\n"
        "## Conventions\n"
        "- Log via logging.\n"
    )

    _REPLACE_PATCH = [{
        "op": "replace",
        "old_text": "The alpha subsystem drives the widget loop.",
        "new_text": "The beta subsystem drives the widget loop.",
    }]

    _REVIEW_STEPS = (StepType.TEST, StepType.SELF_CHECK, StepType.INVARIANT_CHECK)

    # --- helpers -------------------------------------------------------

    def _write_charter(self, root, text):
        se3_dir = root / "tianluo"
        se3_dir.mkdir(parents=True, exist_ok=True)
        (se3_dir / "charter.md").write_text(text, encoding="utf-8")

    def _install_fake_caller(self, monkeypatch, responses):
        """Stub charter_freshness's LLMCaller with a queued-response fake (same
        shape tests/test_charter_freshness.py uses)."""
        state = {"prompts": [], "responses": list(responses), "calls": 0,
                 "init_kwargs": None}

        class FakeCaller:
            def __init__(self, *args, **kwargs):
                state["init_kwargs"] = kwargs

            def call(self, prompt, **kwargs):
                state["calls"] += 1
                state["prompts"].append(prompt)
                if not state["responses"]:
                    raise AssertionError("unexpected extra LLM call")
                return state["responses"].pop(0)

        monkeypatch.setattr(charter_freshness, "LLMCaller", FakeCaller)
        return state

    def _propose(self, update, patch, *, touched=None, suggested="do it"):
        return json.dumps({
            "charter_update_needed": update,
            "touched_classes": touched or (["top-level architecture"] if update else []),
            "reason": "r",
            "suggested_update": suggested if update else "",
            "patch": patch,
        })

    def _gate(self, admitted, *, violations=None, weakened=None):
        return json.dumps({
            "admitted": admitted,
            "violations": violations or [],
            "weakened_removals": weakened or [],
        })

    def _register_recording_mocks(self, sm, *, changed_files=("src/foo.py",)):
        """Register a COMPLETED-returning mock for every step type. IMPLEMENT
        publishes ``files_changed`` so _build_step_inputs hands charter_freshness
        a non-empty ``changes_made`` (the diff that makes the closed loop run)."""
        def make(step_type):
            def handler(step, flow):
                if step_type == StepType.IMPLEMENT:
                    step.outputs["files_changed"] = list(changed_files)
                return StepStatus.COMPLETED
            return handler

        for step_type in StepType:
            sm.register_handler(step_type, make(step_type))

    def _drive(self, sm, flow, order, max_steps=80):
        """Run the flow to a terminal status, recording the type of every step
        actually executed (fix-loop re-runs included)."""
        flow.status = FlowStatus.RUNNING
        taken = 0
        while flow.status == FlowStatus.RUNNING and taken < max_steps:
            step = flow.state.get_current_step()
            if not step:
                break
            order.append(step.step_type)
            sm.run_step(flow, step)
            sm.transition_to_next(flow)
            taken += 1
        return taken

    def _make_flow(self, sm, tmp_path, task_type):
        flow = sm.create_flow(f"touch the architecture ({task_type})", task_type=task_type)
        # project_root = change_path.parent; keep it == tmp_path so the handler
        # reads/writes the same tianluo/charter.md the anchors were frozen from.
        flow.change_path = tmp_path / "change"
        # Skip the pre-implement baseline subprocess (no git / no tests here).
        flow.state.baseline_failures = []
        sm.init_flow(flow)
        return flow

    def _charter_step(self, flow):
        return next(
            s for s in flow.state.steps.values()
            if s.step_type == StepType.CHARTER_FRESHNESS
        )

    # --- tests ---------------------------------------------------------

    def test_charter_auto_update_does_not_reflow_completed_review_steps(
        self, tmp_path, monkeypatch,
    ):
        """A feature flow whose charter_freshness passes the gate writes the
        charter and advances straight to version_analyze — invariant_check,
        self_check and test each ran exactly once and NONE re-ran after the
        auto-update."""
        self._write_charter(tmp_path, self._DISK_CHARTER)
        state = self._install_fake_caller(monkeypatch, [
            self._propose(True, self._REPLACE_PATCH),
            self._gate(True),
        ])
        sm = StateMachine(tmp_path)
        self._register_recording_mocks(sm)
        sm.register_handler(
            StepType.CHARTER_FRESHNESS, charter_freshness.charter_freshness_handler,
        )

        flow = self._make_flow(sm, tmp_path, "feature")
        order = []
        self._drive(sm, flow, order)

        assert flow.status == FlowStatus.COMPLETED
        # propose + gate only.
        assert state["calls"] == 2
        # The charter was actually rewritten on disk (closed loop applied).
        on_disk = (tmp_path / "tianluo" / "charter.md").read_text(encoding="utf-8")
        assert "beta subsystem" in on_disk
        assert "alpha subsystem" not in on_disk

        cf_step = self._charter_step(flow)
        assert cf_step.outputs["charter_auto_updated"] is True

        # No fix loop was triggered: each review step ran exactly once.
        for st in self._REVIEW_STEPS:
            assert order.count(st) == 1, st
        assert order.count(StepType.CHARTER_FRESHNESS) == 1

        # Nothing re-ran AFTER the charter auto-update; the flow only moved
        # forward into version_analyze -> commit -> summarize.
        last_cf = max(i for i, s in enumerate(order) if s == StepType.CHARTER_FRESHNESS)
        tail = order[last_cf + 1:]
        assert not (set(tail) & set(self._REVIEW_STEPS)), (
            "a review step re-ran after the charter auto-update"
        )
        assert StepType.VERSION_ANALYZE in tail
        assert StepType.COMMIT in tail
        assert StepType.SUMMARIZE in tail
        assert (
            tail.index(StepType.VERSION_ANALYZE)
            < tail.index(StepType.COMMIT)
            < tail.index(StepType.SUMMARIZE)
        )
        # The auto-update also did not bump the shared fix counter.
        assert flow.state.get_fix_iteration() == 0

    def test_charter_freshness_stays_advisory_without_invariant_check_in_sequence(
        self, tmp_path, monkeypatch,
    ):
        """The 'small' sequence has no invariant_check, so even when the LLM
        proposes a concrete patch the precondition fails: charter_freshness stays
        advisory (single propose call, no gate), leaves the charter byte-for-byte
        unchanged, and the flow still COMPLETES."""
        self._write_charter(tmp_path, self._DISK_CHARTER)
        state = self._install_fake_caller(monkeypatch, [
            self._propose(True, self._REPLACE_PATCH),
        ])
        sm = StateMachine(tmp_path)
        self._register_recording_mocks(sm)
        sm.register_handler(
            StepType.CHARTER_FRESHNESS, charter_freshness.charter_freshness_handler,
        )

        flow = self._make_flow(sm, tmp_path, "small")
        assert StepType.INVARIANT_CHECK not in flow.state.selected_steps
        order = []
        self._drive(sm, flow, order)

        assert flow.status == FlowStatus.COMPLETED
        # Only the propose call fired; the gate never ran.
        assert state["calls"] == 1
        # Prefer-stale-over-degraded: disk unchanged.
        assert (tmp_path / "tianluo" / "charter.md").read_text(encoding="utf-8") == self._DISK_CHARTER

        cf_step = self._charter_step(flow)
        assert cf_step.outputs["charter_auto_updated"] is False
        assert cf_step.outputs["degraded_reason"] == "invariant_check_not_completed"
        # The advisory suggestion is still surfaced for the summarize/WebUI channel.
        assert cf_step.outputs["suggested_update"] == "do it"

    def test_why_comment_deletion_routes_shared_fix_loop_and_coexists_with_charter_update(
        self, tmp_path, monkeypatch,
    ):
        """A first-pass invariant_check flags a WHY:-comment deletion
        (REVISION_NEEDED) — it rides the SAME implement fix loop as TEST/SELF_CHECK
        (fix_iteration bumped, implement re-run) — then passes; the charter
        auto-update then applies in the same flow, and the flow completes with no
        review step re-running after the charter write."""
        self._write_charter(tmp_path, self._DISK_CHARTER)
        state = self._install_fake_caller(monkeypatch, [
            self._propose(True, self._REPLACE_PATCH),
            self._gate(True),
        ])
        sm = StateMachine(tmp_path)
        self._register_recording_mocks(sm)
        sm.register_handler(
            StepType.CHARTER_FRESHNESS, charter_freshness.charter_freshness_handler,
        )

        inv_calls = {"n": 0}

        def invariant_handler(step, flow):
            inv_calls["n"] += 1
            if inv_calls["n"] == 1:
                # Mimic the why-comment hard guard's REVISION_NEEDED output: a
                # WHY:-prefixed comment was deleted without restatement.
                step.outputs["fix_needed"] = True
                step.outputs["fix_instructions"] = (
                    "restore the deleted `# WHY:` comment or restate its intent"
                )
                step.outputs["fix_context"] = {
                    "reason": "invariant_check",
                    "issues": [{"type": "why_comment_deleted", "quote": "# WHY: bound"}],
                }
                return StepStatus.REVISION_NEEDED
            return StepStatus.COMPLETED

        sm.register_handler(StepType.INVARIANT_CHECK, invariant_handler)

        flow = self._make_flow(sm, tmp_path, "feature")
        assert flow.state.get_fix_iteration() == 0
        order = []
        self._drive(sm, flow, order)

        assert flow.status == FlowStatus.COMPLETED
        # The why-deletion REVISION_NEEDED routed the shared fix loop.
        assert inv_calls["n"] >= 2, "invariant_check was not re-run after the WHY fix"
        assert flow.state.get_fix_iteration() >= 1
        assert order.count(StepType.IMPLEMENT) >= 2, "implement fix loop did not re-run"

        # Both guards succeeded in the same flow: the charter was auto-updated.
        on_disk = (tmp_path / "tianluo" / "charter.md").read_text(encoding="utf-8")
        assert "beta subsystem" in on_disk
        cf_step = self._charter_step(flow)
        assert cf_step.outputs["charter_auto_updated"] is True

        # The charter auto-update itself did not reflow any review step.
        last_cf = max(i for i, s in enumerate(order) if s == StepType.CHARTER_FRESHNESS)
        assert not (set(order[last_cf + 1:]) & set(self._REVIEW_STEPS))

    def test_why_comment_deletion_respects_shared_max_fix_iterations_bound(
        self, tmp_path, monkeypatch,
    ):
        """An always-failing WHY:-deletion invariant_check exhausts the SAME
        shared max_fix_iterations bound as TEST/SELF_CHECK and FAILs the flow —
        the charter auto-update never runs (no propose/gate call)."""
        self._write_charter(tmp_path, self._DISK_CHARTER)
        state = self._install_fake_caller(monkeypatch, [])  # any LLM call is a failure
        sm = StateMachine(tmp_path)
        # Tiny shared bound so the loop exhausts quickly.
        sm._get_max_fix_iterations = lambda: 2  # type: ignore[assignment]
        self._register_recording_mocks(sm)
        sm.register_handler(
            StepType.CHARTER_FRESHNESS, charter_freshness.charter_freshness_handler,
        )

        def always_why_revision(step, flow):
            step.outputs["fix_needed"] = True
            step.outputs["fix_instructions"] = "restore the deleted `# WHY:` comment"
            step.outputs["fix_context"] = {
                "reason": "invariant_check",
                "issues": [{"type": "why_comment_deleted"}],
            }
            return StepStatus.REVISION_NEEDED

        sm.register_handler(StepType.INVARIANT_CHECK, always_why_revision)

        flow = self._make_flow(sm, tmp_path, "feature")
        order = []
        self._drive(sm, flow, order)

        assert flow.status == FlowStatus.FAILED
        # The flow died in the invariant fix loop, before charter_freshness — so
        # the closed loop never made an LLM call and never touched the charter.
        assert state["calls"] == 0
        assert StepType.CHARTER_FRESHNESS not in order
        assert (tmp_path / "tianluo" / "charter.md").read_text(encoding="utf-8") == self._DISK_CHARTER


class TestStreamProgressHistory:
    """Cover record_stream_progress writing + skipping, and daemon incremental
    reads over in-progress (partial) lines (flow-engine Chat History)."""

    def _step(self):
        # jsonl stem follows the NN_<step_type>_<hash> convention so the daemon
        # reader can parse the authoritative step_type from the file name.
        return "01_discovery_abc12345"

    def test_record_stream_progress_writes_partial_line(self):
        from . import chat_history

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            step_id = self._step()
            raw_obj = {"type": "assistant", "message": {"content": "x"}}
            chat_history.record_stream_progress(
                root, "flow1", step_id, "discovery", "🔧 Read: foo.py",
                raw_obj, attempt=0,
            )
            path = root / "tianluo" / "history" / "flow1" / f"{step_id}.jsonl"
            lines = path.read_text(encoding="utf-8").strip().split("\n")
            assert len(lines) == 1
            rec = json.loads(lines[0])
            assert rec["type"] == "stream_progress"
            assert rec["role"] == "assistant"
            assert rec["partial"] is True
            assert rec["step_type"] == "discovery"
            assert rec["content"] == "🔧 Read: foo.py"
            assert rec["raw_json"] == [raw_obj]
            assert rec["attempt"] == 0
            assert "timestamp" in rec

    def test_get_step_history_skips_stream_progress(self):
        from . import chat_history

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            step_id = self._step()
            chat_history.record_prompt(root, "flow1", step_id, "discovery", "do it", 0)
            chat_history.record_stream_progress(
                root, "flow1", step_id, "discovery", "PARTIAL_ONLY", None, attempt=0,
            )
            # Final assistant result (NDJSON with a text block).
            ndjson = json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "FINAL"}]}}
            )
            chat_history.record_response(root, "flow1", step_id, "discovery", ndjson, 0)

            session = chat_history.get_step_history(root, "flow1", step_id)
            assert session is not None
            roles = [m.role for m in session.messages]
            # Only the user prompt + the final assistant turn survive; the
            # stream_progress line is skipped.
            assert roles == ["user", "assistant"]
            assert all("PARTIAL_ONLY" not in m.content for m in session.messages)

    def test_format_history_for_retry_skips_stream_progress(self):
        from . import chat_history

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            step_id = self._step()
            chat_history.record_prompt(root, "flow1", step_id, "discovery", "do it", 0)
            chat_history.record_stream_progress(
                root, "flow1", step_id, "discovery", "PARTIAL_ONLY", None, attempt=0,
            )
            ndjson = json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "FINAL"}]}}
            )
            chat_history.record_response(root, "flow1", step_id, "discovery", ndjson, 0)

            ctx = chat_history.format_history_for_retry(root, "flow1", step_id)
            assert ctx is not None
            assert "PARTIAL_ONLY" not in ctx

    def test_truncated_last_line_does_not_break_reads(self):
        from . import chat_history

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            step_id = self._step()
            chat_history.record_prompt(root, "flow1", step_id, "discovery", "do it", 0)
            chat_history.record_stream_progress(
                root, "flow1", step_id, "discovery", "P1", None, attempt=0,
            )
            # Simulate a half-written final line (e.g. process killed mid-write).
            path = root / "tianluo" / "history" / "flow1" / f"{step_id}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write('{"type": "stream_progress", "content": "broke')  # no newline, no close
            # Earlier valid lines still parse; the malformed tail is skipped.
            session = chat_history.get_step_history(root, "flow1", step_id)
            assert session is not None
            assert [m.role for m in session.messages] == ["user"]

    def test_read_flow_incremental_cursor_over_progress(self):
        """The daemon reader advances its per-file cursor across progress lines
        without losing or re-emitting any record."""
        from . import chat_history
        from ..daemon.history import DaemonHistoryReader

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            step_id = self._step()
            fname = f"{step_id}.jsonl"
            reader = DaemonHistoryReader(lambda: [str(root)])

            # First two progress lines.
            chat_history.record_stream_progress(root, "flow1", step_id, "discovery", "P1", None, 0)
            chat_history.record_stream_progress(root, "flow1", step_id, "discovery", "P2", None, 0)

            first = reader.read_flow("flow1", cursor=None)
            assert first.mode == "full"
            assert [r["message"]["content"] for r in first.records] == ["P1", "P2"]
            # Authoritative step_type parsed from the file name.
            assert all(r["step_type"] == "discovery" for r in first.records)
            assert first.cursor[fname] == 2

            # One more progress line + the final result.
            chat_history.record_stream_progress(root, "flow1", step_id, "discovery", "P3", None, 0)
            ndjson = json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "FINAL"}]}}
            )
            chat_history.record_response(root, "flow1", step_id, "discovery", ndjson, 0)

            second = reader.read_flow("flow1", cursor=first.cursor)
            assert second.mode == "append"
            # Exactly the two NEW lines — no dup of P1/P2, no loss of P3/final.
            contents = [r["message"].get("content") for r in second.records]
            assert "P1" not in contents and "P2" not in contents
            assert "P3" in contents
            types = [r["message"].get("type") for r in second.records]
            assert "stream_progress" in types  # P3
            assert len(second.records) == 2
            assert second.cursor[fname] == 4


class TestStreamJSONTrackerUsage:
    """Cover usage/cost capture from the type:"result" NDJSON message."""

    def _tracker(self):
        from .llm_caller import StreamJSONTracker

        # No flow_id/step_id -> no progress writes; we only inspect .usage.
        return StreamJSONTracker()

    def test_usage_starts_empty(self):
        tracker = self._tracker()
        assert tracker.usage.is_empty()

    def test_capture_nested_message_usage_and_top_cost(self):
        tracker = self._tracker()
        line = json.dumps(
            {
                "type": "result",
                "result": "done",
                "total_cost_usd": 0.0123,
                "message": {
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_creation_input_tokens": 10,
                        "cache_read_input_tokens": 20,
                    }
                },
            }
        )
        tracker.process_line(line)
        u = tracker.usage
        assert u.input_tokens == 100
        assert u.output_tokens == 50
        assert u.cache_creation_input_tokens == 10
        assert u.cache_read_input_tokens == 20
        assert u.total_cost_usd == pytest.approx(0.0123)

    def test_capture_top_level_usage(self):
        tracker = self._tracker()
        line = json.dumps(
            {
                "type": "result",
                "total_cost_usd": 0.5,
                "usage": {"input_tokens": 7, "output_tokens": 3},
            }
        )
        tracker.process_line(line)
        u = tracker.usage
        assert u.input_tokens == 7
        assert u.output_tokens == 3
        assert u.total_cost_usd == pytest.approx(0.5)

    def test_result_without_usage_stays_zero(self):
        tracker = self._tracker()
        tracker.process_line(json.dumps({"type": "result", "result": "x"}))
        assert tracker.usage.is_empty()

    def test_result_partial_usage_fields_default_zero(self):
        tracker = self._tracker()
        line = json.dumps(
            {
                "type": "result",
                "message": {"usage": {"input_tokens": 42}},
            }
        )
        tracker.process_line(line)
        u = tracker.usage
        assert u.input_tokens == 42
        assert u.output_tokens == 0
        assert u.cache_creation_input_tokens == 0
        assert u.cache_read_input_tokens == 0
        assert u.total_cost_usd == 0.0

    def test_malformed_usage_does_not_raise(self):
        tracker = self._tracker()
        # usage is not a dict -> swallowed, stays empty.
        tracker.process_line(
            json.dumps({"type": "result", "usage": "not-a-dict", "total_cost_usd": None})
        )
        assert tracker.usage.is_empty()

    def test_non_result_lines_do_not_set_usage(self):
        tracker = self._tracker()
        tracker.process_line(
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}
            )
        )
        assert tracker.usage.is_empty()

    def test_invalid_json_silently_skipped(self):
        tracker = self._tracker()
        tracker.process_line("not json at all {")
        assert tracker.usage.is_empty()

    def test_capture_integrates_with_step_accumulator(self):
        """tracker.usage folds into the step scope via add_call_usage."""
        from .token_usage import accumulate_step_usage, add_call_usage

        tracker = self._tracker()
        tracker.process_line(
            json.dumps(
                {
                    "type": "result",
                    "total_cost_usd": 0.01,
                    "message": {"usage": {"input_tokens": 5, "output_tokens": 2}},
                }
            )
        )
        with accumulate_step_usage() as step:
            add_call_usage(tracker.usage)
            add_call_usage(tracker.usage)  # simulate a second call/retry
        assert step.input_tokens == 10
        assert step.output_tokens == 4
        assert step.total_cost_usd == pytest.approx(0.02)


class TestSessionTokenUsageModel:
    """State.session_token_usage round-trip and old-file compatibility."""

    def test_default_session_usage_is_empty(self):
        state = State()
        assert state.session_token_usage.is_empty()

    def test_session_usage_round_trips(self):
        from .token_usage import UsageTotals

        flow = FlowInstance(task_description="usage round-trip")
        flow.state.session_token_usage = UsageTotals(
            input_tokens=100,
            output_tokens=40,
            cache_creation_input_tokens=5,
            cache_read_input_tokens=20,
            total_cost_usd=0.0123,
        )

        restored = FlowInstance.from_dict(flow.to_dict())
        u = restored.state.session_token_usage
        assert u.input_tokens == 100
        assert u.output_tokens == 40
        assert u.cache_creation_input_tokens == 5
        assert u.cache_read_input_tokens == 20
        assert u.total_cost_usd == pytest.approx(0.0123)

    def test_session_usage_serializes_as_primitive_dict(self):
        from .token_usage import UsageTotals

        state = State(session_token_usage=UsageTotals(input_tokens=3))
        payload = state.to_dict()["session_token_usage"]
        assert isinstance(payload, dict)
        assert payload["input_tokens"] == 3
        # JSON-primitive: survives a json round-trip unchanged.
        assert json.loads(json.dumps(payload)) == payload

    def test_old_engine_json_without_field_loads_empty(self):
        # An engine.json written by an older build has no session_token_usage.
        state = State.from_dict(
            {
                "current_step_id": None,
                "step_history": [],
                "steps": {},
                "context": {},
                "selected_steps": [],
            }
        )
        assert state.session_token_usage.is_empty()

    def test_other_state_fields_unaffected_by_round_trip(self):
        from .token_usage import UsageTotals

        state = State(
            fix_iterations=2,
            baseline_failures=["tests/test_x.py::test_y"],
            session_token_usage=UsageTotals(input_tokens=1, total_cost_usd=0.5),
        )
        restored = State.from_dict(state.to_dict())
        assert restored.fix_iterations == 2
        assert restored.baseline_failures == ["tests/test_x.py::test_y"]
        assert restored.session_token_usage.input_tokens == 1


class TestRunStepTokenAggregation:
    """run_step folds per-step usage into step.outputs and session totals."""

    def _make_usage(self, **kwargs):
        from .token_usage import UsageTotals

        return UsageTotals(**kwargs)

    def test_single_step_merges_multiple_calls(self):
        """A step whose handler triggers two calls writes their merged total."""
        from .token_usage import add_call_usage

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def handler(step, flow):
                # Simulate two LLM subprocess calls (e.g. a retry) folding usage
                # into the active step scope opened by run_step.
                add_call_usage(self._make_usage(input_tokens=10, output_tokens=4, total_cost_usd=0.01))
                add_call_usage(self._make_usage(input_tokens=5, output_tokens=1, total_cost_usd=0.02))
                return StepStatus.COMPLETED

            sm.register_handler(StepType.ANALYZE, handler)
            flow = sm.create_flow("merge calls")
            step = flow.state.get_current_step()

            sm.run_step(flow, step)

            tu = step.outputs["token_usage"]
            assert tu["input_tokens"] == 15
            assert tu["output_tokens"] == 5
            assert tu["total_cost_usd"] == pytest.approx(0.03)
            # Session total reflects this one step.
            su = flow.state.session_token_usage
            assert su.input_tokens == 15
            assert su.total_cost_usd == pytest.approx(0.03)

    def test_no_call_step_writes_no_usage_field(self):
        """A step with no LLM call must not add a token_usage noise field."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def handler(step, flow):
                step.outputs["result"] = "done"
                return StepStatus.COMPLETED

            sm.register_handler(StepType.ANALYZE, handler)
            flow = sm.create_flow("no calls")
            step = flow.state.get_current_step()

            sm.run_step(flow, step)

            assert "token_usage" not in step.outputs
            assert flow.state.session_token_usage.is_empty()

    def test_multi_step_session_accumulation(self):
        """Session total equals the sum of each step's usage."""
        from .token_usage import add_call_usage

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def make_handler(amount):
                def handler(step, flow):
                    add_call_usage(self._make_usage(input_tokens=amount, total_cost_usd=amount / 1000))
                    return StepStatus.COMPLETED
                return handler

            sm.register_handler(StepType.ANALYZE, make_handler(100))
            sm.register_handler(StepType.IMPLEMENT, make_handler(250))

            flow = sm.create_flow("multi step")
            step1 = flow.state.get_current_step()
            sm.run_step(flow, step1)

            # Advance to a second step and run it.
            step1.status = StepStatus.COMPLETED
            step2 = sm.transition_to_next(flow)
            # Drive to the IMPLEMENT step (skip any intervening steps quickly).
            while step2 is not None and step2.step_type != StepType.IMPLEMENT:
                step2.status = StepStatus.COMPLETED
                step2 = sm.transition_to_next(flow)
            assert step2 is not None and step2.step_type == StepType.IMPLEMENT
            sm.run_step(flow, step2)

            assert step1.outputs["token_usage"]["input_tokens"] == 100
            assert step2.outputs["token_usage"]["input_tokens"] == 250
            su = flow.state.session_token_usage
            assert su.input_tokens == 350
            assert su.total_cost_usd == pytest.approx(0.35)

    def test_exception_step_still_aggregates_and_resets_scope(self):
        """A raising handler still records the usage gathered before it raised,
        and the step scope is reset (no contextvar leak)."""
        from .token_usage import add_call_usage, current_step_usage

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def handler(step, flow):
                add_call_usage(self._make_usage(input_tokens=8, total_cost_usd=0.005))
                raise RuntimeError("boom")

            sm.register_handler(StepType.ANALYZE, handler)
            flow = sm.create_flow("raising step")
            step = flow.state.get_current_step()

            result = sm.run_step(flow, step)

            assert result == StepStatus.FAILED
            assert step.outputs["token_usage"]["input_tokens"] == 8
            assert flow.state.session_token_usage.input_tokens == 8
            # Scope must not leak past run_step.
            assert current_step_usage() is None

    def test_paused_run_usage_carried_into_next_emitted_record(self):
        """A token-consuming run that returns a non-terminal status (PAUSED /
        REVISION_NEEDED) now publishes both `token_usage` (for display) and
        `carried_token_usage` (for cross-round accumulation), so step-level
        renderers can display usage even before the step reaches a terminal
        status. On the final terminal round, `token_usage` reflects the sum of
        all rounds and `carried_token_usage` is cleared.

        This keeps the web session badge (which re-derives the total by summing
        token_usage off emitted records) in agreement with the CLI authoritative
        total (session_token_usage, which folds every run's step_usage).
        """
        from .token_usage import add_call_usage

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            calls = {"n": 0}

            def handler(step, flow):
                calls["n"] += 1
                if calls["n"] == 1:
                    # Round 1: consumes tokens, pauses awaiting user input.
                    add_call_usage(self._make_usage(input_tokens=100, output_tokens=10, total_cost_usd=0.01))
                    return StepStatus.PAUSED
                if calls["n"] == 2:
                    # Round 2: another clarification round, still paused.
                    add_call_usage(self._make_usage(input_tokens=50, output_tokens=5, total_cost_usd=0.02))
                    return StepStatus.PAUSED
                # Final round: completes, emitting the terminal record.
                add_call_usage(self._make_usage(input_tokens=30, output_tokens=3, total_cost_usd=0.03))
                return StepStatus.COMPLETED

            sm.register_handler(StepType.DISCOVERY, handler)
            flow = sm.create_flow("multi-round discovery", task_type="discovery")
            step = flow.state.get_current_step()

            # Round 1 — PAUSED: token_usage is now published alongside carry.
            sm.run_step(flow, step)
            assert step.status == StepStatus.PAUSED
            assert step.outputs["token_usage"]["input_tokens"] == 100
            assert step.outputs["carried_token_usage"]["input_tokens"] == 100

            # Round 2 — PAUSED: both token_usage and carry accumulate.
            step.status = StepStatus.PENDING
            sm.run_step(flow, step)
            assert step.status == StepStatus.PAUSED
            assert step.outputs["token_usage"]["input_tokens"] == 150
            assert step.outputs["carried_token_usage"]["input_tokens"] == 150

            # Final round — COMPLETED: token_usage is the sum of all three
            # rounds, and the carry is cleared.
            step.status = StepStatus.PENDING
            sm.run_step(flow, step)
            assert step.status == StepStatus.COMPLETED
            assert "carried_token_usage" not in step.outputs
            tu = step.outputs["token_usage"]
            assert tu["input_tokens"] == 180  # 100 + 50 + 30
            assert tu["output_tokens"] == 18  # 10 + 5 + 3
            assert tu["total_cost_usd"] == pytest.approx(0.06)  # 0.01 + 0.02 + 0.03

            # CLI authoritative session total folds every run independently
            # (each run's step_usage only, not the combined total).
            su = flow.state.session_token_usage
            assert su.input_tokens == 180
            assert su.total_cost_usd == pytest.approx(0.06)

    def test_aggregated_usage_survives_persistence(self):
        """Both layers (step.outputs + session) round-trip through engine.json."""
        from .token_usage import add_call_usage

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def handler(step, flow):
                add_call_usage(self._make_usage(input_tokens=12, output_tokens=3, total_cost_usd=0.02))
                return StepStatus.COMPLETED

            sm.register_handler(StepType.ANALYZE, handler)
            flow = sm.create_flow("persist usage")
            step = flow.state.get_current_step()
            step_id = step.step_id
            sm.run_step(flow, step)

            sm.persistence.save_flow(flow)
            loaded = sm.persistence.load_flow()

            assert loaded is not None
            assert loaded.state.steps[step_id].outputs["token_usage"]["input_tokens"] == 12
            assert loaded.state.session_token_usage.input_tokens == 12
            assert loaded.state.session_token_usage.total_cost_usd == pytest.approx(0.02)

    def test_revision_needed_step_publishes_visible_token_usage(self):
        """A step returning REVISION_NEEDED publishes token_usage so renderers
        can display it — the root cause of the G2 fix. Previously, only
        carried_token_usage was written for non-terminal statuses, leaving
        render_step_usage / buildStepUsageFootnote unable to show this step's
        usage.

        The step_type doesn't matter for this test (ANALYZE is used because
        create_flow always starts there); the key assertion is about the
        non-terminal REVISION_NEEDED status.
        """
        from .token_usage import add_call_usage

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def handler(step, flow):
                # Step finds issues, requests revision
                add_call_usage(self._make_usage(input_tokens=200, output_tokens=40, total_cost_usd=0.04))
                return StepStatus.REVISION_NEEDED

            sm.register_handler(StepType.ANALYZE, handler)
            flow = sm.create_flow("revision_needed step")
            step = flow.state.get_current_step()

            sm.run_step(flow, step)

            assert step.status == StepStatus.REVISION_NEEDED
            # The key assertion: token_usage is now visible for renderers.
            tu = step.outputs["token_usage"]
            assert tu["input_tokens"] == 200
            assert tu["output_tokens"] == 40
            assert tu["total_cost_usd"] == pytest.approx(0.04)
            # carried_token_usage is also preserved for the next round.
            assert step.outputs["carried_token_usage"]["input_tokens"] == 200

    def test_non_terminal_then_terminal_usage_equals_sum_of_rounds(self):
        """After a non-terminal round followed by a terminal round, the
        terminal step's token_usage equals the sum of all rounds, and
        session_token_usage equals the sum of each round's increment (not the
        combined totals, which would double-count).
        """
        from .token_usage import add_call_usage

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            calls = {"n": 0}

            def handler(step, flow):
                calls["n"] += 1
                if calls["n"] == 1:
                    # Round 1: REVISION_NEEDED — consume tokens but not done.
                    add_call_usage(self._make_usage(input_tokens=300, output_tokens=60, total_cost_usd=0.05))
                    return StepStatus.REVISION_NEEDED
                # Round 2: COMPLETED — fixes applied, step now done.
                add_call_usage(self._make_usage(input_tokens=150, output_tokens=30, total_cost_usd=0.03))
                return StepStatus.COMPLETED

            sm.register_handler(StepType.ANALYZE, handler)
            flow = sm.create_flow("non-terminal then terminal")
            step = flow.state.get_current_step()

            # Round 1: REVISION_NEEDED
            sm.run_step(flow, step)
            assert step.status == StepStatus.REVISION_NEEDED
            assert step.outputs["token_usage"]["input_tokens"] == 300
            assert step.outputs["carried_token_usage"]["input_tokens"] == 300

            # Round 2: COMPLETED
            step.status = StepStatus.PENDING
            sm.run_step(flow, step)
            assert step.status == StepStatus.COMPLETED
            # Terminal token_usage = carried (300) + this round (150) = 450.
            tu = step.outputs["token_usage"]
            assert tu["input_tokens"] == 450  # 300 + 150
            assert tu["output_tokens"] == 90   # 60 + 30
            assert tu["total_cost_usd"] == pytest.approx(0.08)  # 0.05 + 0.03
            # Carry cleared.
            assert "carried_token_usage" not in step.outputs

            # Session total = sum of round increments only (not combined).
            su = flow.state.session_token_usage
            assert su.input_tokens == 450  # 300 + 150 (each round's step_usage)
            assert su.output_tokens == 90
            assert su.total_cost_usd == pytest.approx(0.08)

    def test_session_usage_not_duplicated_by_combined_totals(self):
        """session_token_usage is the sum of each round's step_usage, not the
        sum of each round's combined (carried + step_usage). If the combined
        total were added instead, the prior round's contribution would be
        double-counted every time the step re-enters.
        """
        from .token_usage import add_call_usage, UsageTotals

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            calls = {"n": 0}

            def handler(step, flow):
                calls["n"] += 1
                if calls["n"] == 1:
                    add_call_usage(self._make_usage(input_tokens=100, output_tokens=10))
                    return StepStatus.PAUSED
                if calls["n"] == 2:
                    add_call_usage(self._make_usage(input_tokens=50, output_tokens=5))
                    return StepStatus.PAUSED
                add_call_usage(self._make_usage(input_tokens=30, output_tokens=3))
                return StepStatus.COMPLETED

            sm.register_handler(StepType.DISCOVERY, handler)
            flow = sm.create_flow("no double count", task_type="discovery")
            step = flow.state.get_current_step()

            sm.run_step(flow, step)  # round 1: 100 in, 10 out
            step.status = StepStatus.PENDING
            sm.run_step(flow, step)  # round 2: 50 in, 5 out
            step.status = StepStatus.PENDING
            sm.run_step(flow, step)  # round 3: 30 in, 3 out

            # Session total = 100 + 50 + 30 = 180 in, NOT combined totals
            # (which would be 100 + 150 + 180 = 430 in if wrongly accumulated).
            su = flow.state.session_token_usage
            assert su.input_tokens == 180  # 100+50+30, not 100+150+180
            assert su.output_tokens == 18   # 10+5+3, not 10+15+18
            # The UsageTotals accumulation is strictly additive per-round.
            expected = UsageTotals(input_tokens=180, output_tokens=18)
            assert su.input_tokens == expected.input_tokens
            assert su.output_tokens == expected.output_tokens


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
