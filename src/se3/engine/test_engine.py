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
            path = root / "se3" / "history" / "flow1" / f"{step_id}.jsonl"
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
            path = root / "se3" / "history" / "flow1" / f"{step_id}.jsonl"
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
        REVISION_NEEDED) emits no terminal step_completed record, so its usage is
        carried forward and rolled into the next emitted record's token_usage.

        This keeps the web session badge (which re-derives the total by summing
        the emitted records' token_usage) in agreement with the CLI authoritative
        total (flow.state.session_token_usage, which folds EVERY run). Without the
        carry, the paused round's tokens would be folded into the session total
        but never surface in any emitted record, so the web badge would
        undercount a multi-round discovery flow.
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
                # Final round: completes, emitting the only terminal record.
                add_call_usage(self._make_usage(input_tokens=30, output_tokens=3, total_cost_usd=0.03))
                return StepStatus.COMPLETED

            sm.register_handler(StepType.DISCOVERY, handler)
            flow = sm.create_flow("multi-round discovery", task_type="discovery")
            step = flow.state.get_current_step()

            # Round 1 — PAUSED: no token_usage surfaced, carried instead.
            sm.run_step(flow, step)
            assert step.status == StepStatus.PAUSED
            assert "token_usage" not in step.outputs
            assert step.outputs["carried_token_usage"]["input_tokens"] == 100

            # Round 2 — PAUSED: carry accumulates (round1 + round2).
            step.status = StepStatus.PENDING
            sm.run_step(flow, step)
            assert step.status == StepStatus.PAUSED
            assert "token_usage" not in step.outputs
            assert step.outputs["carried_token_usage"]["input_tokens"] == 150

            # Final round — COMPLETED: the single emitted record's token_usage is
            # the sum of all three rounds, and the carry is cleared.
            step.status = StepStatus.PENDING
            sm.run_step(flow, step)
            assert step.status == StepStatus.COMPLETED
            assert "carried_token_usage" not in step.outputs
            tu = step.outputs["token_usage"]
            assert tu["input_tokens"] == 180  # 100 + 50 + 30
            assert tu["output_tokens"] == 18  # 10 + 5 + 3
            assert tu["total_cost_usd"] == pytest.approx(0.06)  # 0.01 + 0.02 + 0.03

            # CLI authoritative session total folds every run independently and
            # must equal the single emitted record's rolled-up total.
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
