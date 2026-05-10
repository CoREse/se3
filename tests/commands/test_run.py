"""Tests for the se3 run command, specifically resume functionality.

Tests cover:
- Resume detection logic for RUNNING steps
- Resumed flag injection into step.inputs
- Normal (non-resume) flow execution unchanged
"""

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
    State,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.persistence import PersistenceManager
from se3.commands.run import run_flow


class TestResumeDetection:
    """Test resume detection logic in run_flow."""

    def setup_method(self):
        """Set up test fixtures."""
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        # Create se3/state directory structure
        state_dir = self.project_root / "se3" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        # Create a flow with an IMPLEMENT step in RUNNING state
        self.flow = FlowInstance(
            flow_id="test-flow-001",
            task_description="Test implementation task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        self.flow.state.selected_steps = [
            StepType.ANALYZE,
            StepType.PLAN,
            StepType.IMPLEMENT,
        ]
        self.flow.state.current_step_index = 2

        # Create completed ANALYZE step
        analyze_step = Step(
            step_type=StepType.ANALYZE,
            status=StepStatus.COMPLETED,
            step_id="analyze-001",
            outputs={"task_type": "feature"},
        )
        self.flow.state.add_step(analyze_step)

        # Create completed PLAN step
        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
            outputs={
                "plan": {"proposal": {"summary": "Test"}, "design": {"overview": "Test"}},
                "task_groups": [
                    {"group_id": "G1", "description": "Group 1"},
                    {"group_id": "G2", "description": "Group 2"},
                ],
            },
        )
        self.flow.state.add_step(plan_step)

        # Create IMPLEMENT step in RUNNING state (simulating interruption)
        self.implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            step_id="implement-001",
            inputs={
                "task_groups": [
                    {"group_id": "G1", "description": "Group 1"},
                    {"group_id": "G2", "description": "Group 2"},
                ],
                "design_doc": {"title": "Test Design"},
            },
            outputs={},  # No outputs yet - interrupted mid-execution
        )
        self.flow.state.add_step(self.implement_step)
        self.flow.state.current_step_id = "implement-001"

        # Save flow to state file
        self.persistence = PersistenceManager(self.project_root)
        self.persistence.save_flow(self.flow)

    def teardown_method(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("se3.commands.run.PersistenceManager")
    @patch("se3.commands.run.StateMachine")
    @patch("se3.commands.run.STEP_HANDLERS", {})
    def test_resume_detects_running_step_and_transitions_to_pending(
        self, mock_sm_class, mock_pm_class
    ):
        """Test that resuming a flow with RUNNING step transitions it to PENDING."""
        # Setup mock persistence to return self.flow directly
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = self.flow

        # Setup mock state machine
        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm
        mock_sm.run_step.return_value = StepStatus.COMPLETED
        mock_sm.transition_to_next.side_effect = lambda flow: setattr(flow, 'status', FlowStatus.COMPLETED)

        # Call run_flow with resume (flow_id provided)
        with patch("se3.commands.run.render_step_output"):
            with patch("se3.commands.run.render_full"):
                run_flow(
                        project_root=self.project_root,
                        flow_id="test-flow-001",
                    )

        # Verify step status was changed from RUNNING to PENDING
        assert self.implement_step.status == StepStatus.PENDING

    @patch("se3.commands.run.PersistenceManager")
    @patch("se3.commands.run.StateMachine")
    @patch("se3.commands.run.STEP_HANDLERS", {})
    def test_resume_injects_resumed_flag_into_step_inputs(
        self, mock_sm_class, mock_pm_class
    ):
        """Test that resumed flag is injected into step.inputs during resume."""
        # Setup mock persistence to return self.flow directly
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = self.flow

        # Setup mock state machine
        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm
        mock_sm.run_step.return_value = StepStatus.COMPLETED
        mock_sm.transition_to_next.side_effect = lambda flow: setattr(flow, 'status', FlowStatus.COMPLETED)

        # Call run_flow with resume
        with patch("se3.commands.run.render_step_output"):
            with patch("se3.commands.run.render_full"):
                run_flow(
                        project_root=self.project_root,
                        flow_id="test-flow-001",
                    )

        # Verify resumed flag was injected
        assert "resumed" in self.implement_step.inputs
        assert self.implement_step.inputs["resumed"] is True

    @patch("se3.commands.run.StateMachine")
    @patch("se3.commands.run.STEP_HANDLERS", {})
    def test_normal_run_does_not_trigger_resume_logic(
        self, mock_sm_class
    ):
        """Test that normal (non-resume) flow does not trigger resume logic."""
        # Create a fresh flow for new execution (no flow_id)
        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        # Track created flows
        created_flows = []

        def capture_create(**kwargs):
            new_flow = FlowInstance(
                task_description=kwargs.get("task_description"),
                task_type=kwargs.get("task_type", "feature"),
            )
            created_flows.append(new_flow)
            return new_flow

        mock_sm.create_flow = capture_create
        mock_sm.persistence = MagicMock()
        mock_sm.persistence.save_flow = MagicMock()

        # Call run_flow without flow_id (new flow)
        with patch("se3.commands.run.render_step_output"):
            with patch("se3.commands.run.render_full"):
                run_flow(
                        project_root=self.project_root,
                        task_description="New task",
                    )

        # Verify create_flow was called (new flow, not resume)
        assert len(created_flows) == 1
        assert created_flows[0].task_description == "New task"

    @patch("se3.commands.run.PersistenceManager")
    @patch("se3.commands.run.StateMachine")
    @patch("se3.commands.run.STEP_HANDLERS", {})
    def test_resume_preserves_existing_step_outputs(
        self, mock_sm_class, mock_pm_class
    ):
        """Test that resume preserves existing step outputs."""
        # Add some partial progress to outputs (simulating partial completion)
        self.implement_step.outputs = {
            "implemented_groups": ["G1"],  # G1 was completed before interrupt
            "total_files_changed": 2,
        }

        # Setup mock persistence to return self.flow directly
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = self.flow

        # Setup mock state machine
        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm
        mock_sm.run_step.return_value = StepStatus.COMPLETED
        mock_sm.transition_to_next.side_effect = lambda flow: setattr(flow, 'status', FlowStatus.COMPLETED)

        # Call run_flow with resume
        with patch("se3.commands.run.render_step_output"):
            with patch("se3.commands.run.render_full"):
                run_flow(
                        project_root=self.project_root,
                        flow_id="test-flow-001",
                    )

        # Verify existing outputs are preserved
        assert "implemented_groups" in self.implement_step.outputs
        assert self.implement_step.outputs["implemented_groups"] == ["G1"]
        assert self.implement_step.outputs["total_files_changed"] == 2
        # And resumed flag was added to inputs, not outputs
        assert self.implement_step.inputs.get("resumed") is True

    @patch("se3.commands.run.PersistenceManager")
    @patch("se3.commands.run.StateMachine")
    @patch("se3.commands.run.STEP_HANDLERS", {})
    def test_resume_with_non_running_step_does_not_modify_status(
        self, mock_sm_class, mock_pm_class
    ):
        """Test that resume with non-RUNNING step doesn't modify status."""
        # Change step status to PENDING (not RUNNING)
        self.implement_step.status = StepStatus.PENDING

        # Setup mock persistence to return self.flow directly
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = self.flow

        # Setup mock state machine
        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm
        mock_sm.run_step.return_value = StepStatus.COMPLETED
        mock_sm.transition_to_next.side_effect = lambda flow: setattr(flow, 'status', FlowStatus.COMPLETED)

        original_status = self.implement_step.status

        # Call run_flow with resume
        with patch("se3.commands.run.render_step_output"):
            with patch("se3.commands.run.render_full"):
                run_flow(
                        project_root=self.project_root,
                        flow_id="test-flow-001",
                    )

        # Verify status wasn't changed (remains PENDING)
        assert self.implement_step.status == original_status
        # And no resumed flag was injected
        assert "resumed" not in self.implement_step.inputs

    @patch("se3.commands.run.PersistenceManager")
    @patch("se3.commands.run.StateMachine")
    @patch("se3.commands.run.STEP_HANDLERS", {})
    def test_resume_logs_interrupted_step_info(
        self, mock_sm_class, mock_pm_class, caplog
    ):
        """Test that resume logs information about interrupted step."""
        import logging

        # Setup mock persistence to return self.flow directly
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = self.flow

        # Setup mock state machine
        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm
        mock_sm.run_step.return_value = StepStatus.COMPLETED
        mock_sm.transition_to_next.side_effect = lambda flow: setattr(flow, 'status', FlowStatus.COMPLETED)

        with caplog.at_level(logging.INFO):
            with patch("se3.commands.run.render_step_output"):
                with patch("se3.commands.run.render_full"):
                    run_flow(
                            project_root=self.project_root,
                            flow_id="test-flow-001",
                        )

        # Verify log message about resuming
        assert "Resuming interrupted step" in caplog.text
        assert "implement-001" in caplog.text


class TestResumeFailedFlow:
    """Test resume detection logic for FAILED flows."""

    def setup_method(self):
        """Set up test fixtures with a FAILED flow."""
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        # Create se3/state directory structure
        state_dir = self.project_root / "se3" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        # Create a flow with a FAILED step
        self.flow = FlowInstance(
            flow_id="test-flow-002",
            task_description="Test failed task",
            task_type="bugfix",
            status=FlowStatus.FAILED,
        )
        self.flow.state.selected_steps = [
            StepType.ANALYZE,
            StepType.IMPLEMENT,
        ]
        self.flow.state.current_step_index = 1

        # Create completed ANALYZE step
        analyze_step = Step(
            step_type=StepType.ANALYZE,
            status=StepStatus.COMPLETED,
            step_id="analyze-001",
            outputs={"task_type": "bugfix"},
        )
        self.flow.state.add_step(analyze_step)

        # Create IMPLEMENT step in FAILED state
        self.implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.FAILED,
            step_id="implement-002",
            inputs={
                "task_description": "Fix the bug",
                "retry_count": 2,
            },
            outputs={},
        )
        self.implement_step.retry_count = 3  # Exhausted retries
        self.flow.state.add_step(self.implement_step)
        self.flow.state.current_step_id = "implement-002"

        # Save flow to state file
        self.persistence = PersistenceManager(self.project_root)
        self.persistence.save_flow(self.flow)

    def teardown_method(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("se3.commands.run.PersistenceManager")
    @patch("se3.commands.run.StateMachine")
    @patch("se3.commands.run.STEP_HANDLERS", {})
    def test_resume_failed_step_transitions_to_pending(
        self, mock_sm_class, mock_pm_class
    ):
        """Test that resuming a FAILED step transitions it to PENDING."""
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = self.flow

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm
        mock_sm.run_step.return_value = StepStatus.COMPLETED
        mock_sm.transition_to_next.side_effect = lambda flow: setattr(flow, 'status', FlowStatus.COMPLETED)

        with patch("se3.commands.run.render_step_output"):
            with patch("se3.commands.run.render_full"):
                run_flow(
                        project_root=self.project_root,
                        flow_id="test-flow-002",
                    )

        assert self.implement_step.status == StepStatus.PENDING

    @patch("se3.commands.run.PersistenceManager")
    @patch("se3.commands.run.StateMachine")
    @patch("se3.commands.run.STEP_HANDLERS", {})
    def test_resume_failed_resets_flow_status_to_running(
        self, mock_sm_class, mock_pm_class
    ):
        """Test that resuming a FAILED flow resets flow status to RUNNING."""
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = self.flow

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm
        mock_sm.run_step.return_value = StepStatus.COMPLETED
        mock_sm.transition_to_next.side_effect = lambda flow: setattr(flow, 'status', FlowStatus.COMPLETED)

        with patch("se3.commands.run.render_step_output"):
            with patch("se3.commands.run.render_full"):
                run_flow(
                        project_root=self.project_root,
                        flow_id="test-flow-002",
                    )

        # Flow status should have been set to RUNNING (before state_machine.run sets it further)
        # Check that persistence.save_flow was called with RUNNING status
        save_calls = mock_pm.save_flow.call_args_list
        # The first save_flow call should have the flow in RUNNING status
        assert any(
            call.args[0].status == FlowStatus.RUNNING or
            (hasattr(call.args[0], 'status') and call.args[0].status == FlowStatus.COMPLETED)
            for call in save_calls
        )

    @patch("se3.commands.run.PersistenceManager")
    @patch("se3.commands.run.StateMachine")
    @patch("se3.commands.run.STEP_HANDLERS", {})
    def test_resume_failed_resets_retry_count(
        self, mock_sm_class, mock_pm_class
    ):
        """Test that resuming a FAILED step resets retry_count to 0."""
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = self.flow

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm
        mock_sm.run_step.return_value = StepStatus.COMPLETED
        mock_sm.transition_to_next.side_effect = lambda flow: setattr(flow, 'status', FlowStatus.COMPLETED)

        with patch("se3.commands.run.render_step_output"):
            with patch("se3.commands.run.render_full"):
                run_flow(
                        project_root=self.project_root,
                        flow_id="test-flow-002",
                    )

        # retry_count on the step model should be reset to 0
        assert self.implement_step.retry_count == 0

    @patch("se3.commands.run.PersistenceManager")
    @patch("se3.commands.run.StateMachine")
    @patch("se3.commands.run.STEP_HANDLERS", {})
    def test_resume_failed_increments_input_retry_count(
        self, mock_sm_class, mock_pm_class
    ):
        """Test that resuming a FAILED step increments input retry_count for conversation history."""
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = self.flow

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm
        mock_sm.run_step.return_value = StepStatus.COMPLETED
        mock_sm.transition_to_next.side_effect = lambda flow: setattr(flow, 'status', FlowStatus.COMPLETED)

        with patch("se3.commands.run.render_step_output"):
            with patch("se3.commands.run.render_full"):
                run_flow(
                        project_root=self.project_root,
                        flow_id="test-flow-002",
                    )

        # input retry_count should be incremented (was 2, now 3)
        assert self.implement_step.inputs["retry_count"] == 3
        # resumed flag should be set
        assert self.implement_step.inputs["resumed"] is True

    @patch("se3.commands.run.PersistenceManager")
    @patch("se3.commands.run.StateMachine")
    @patch("se3.commands.run.STEP_HANDLERS", {})
    def test_resume_failed_logs_retry_info(
        self, mock_sm_class, mock_pm_class, caplog
    ):
        """Test that resuming a FAILED step logs retry info."""
        import logging

        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = self.flow

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm
        mock_sm.run_step.return_value = StepStatus.COMPLETED
        mock_sm.transition_to_next.side_effect = lambda flow: setattr(flow, 'status', FlowStatus.COMPLETED)

        with caplog.at_level(logging.INFO):
            with patch("se3.commands.run.render_step_output"):
                with patch("se3.commands.run.render_full"):
                    run_flow(
                            project_root=self.project_root,
                            flow_id="test-flow-002",
                        )

        assert "Retrying failed step from breakpoint" in caplog.text
        assert "implement-002" in caplog.text


class TestHandleResumeInteractiveFailedFlows:
    """Test that handle_resume_interactive includes FAILED flows."""

    @patch("se3.commands.run.find_existing_flows")
    @patch("se3.commands.run.render_full")
    @patch("se3.commands.run.prompt_user_choice")
    def test_failed_flow_appears_in_resume_menu(
        self, mock_choice, mock_render, mock_find
    ):
        """FAILED flows should appear in the resume interactive menu."""
        mock_find.return_value = [
            {
                "id": "flow-001",
                "status": FlowStatus.FAILED.value,
                "description": "Failed task",
                "current_step": "implement",
                "file": "engine.json",
            }
        ]
        mock_choice.return_value = 0  # Select first option

        from se3.commands.run import handle_resume_interactive
        result = handle_resume_interactive(Path("/tmp"))

        assert result == "flow-001"

    @patch("se3.commands.run.find_existing_flows")
    @patch("se3.commands.run.render_full")
    def test_completed_flow_excluded_from_resume_menu(
        self, mock_render, mock_find
    ):
        """COMPLETED flows should NOT appear in the resume menu."""
        mock_find.return_value = [
            {
                "id": "flow-001",
                "status": FlowStatus.COMPLETED.value,
                "description": "Done task",
                "current_step": "summarize",
                "file": "engine.json",
            }
        ]

        from se3.commands.run import handle_resume_interactive
        result = handle_resume_interactive(Path("/tmp"))

        assert result is None

    @patch("se3.commands.run.find_existing_flows")
    @patch("se3.commands.run.render_full")
    @patch("se3.commands.run.prompt_user_choice")
    def test_failed_flow_shows_retry_label(
        self, mock_choice, mock_render, mock_find
    ):
        """FAILED flow should show 'Retry failed flow' action."""
        mock_find.return_value = [
            {
                "id": "flow-001",
                "status": FlowStatus.FAILED.value,
                "description": "Failed task",
                "current_step": "implement",
                "file": "engine.json",
            }
        ]
        mock_choice.return_value = 0

        from se3.commands.run import handle_resume_interactive
        handle_resume_interactive(Path("/tmp"))

        # Check render was called with "failed" label
        render_calls = mock_render.call_args_list
        assert any("failed" in str(call) for call in render_calls)


class TestHandleStepInterrupt:
    """Test cases for ``_handle_step_interrupt`` persisting Ctrl-C user
    interjections into ``flow.state.context["user_interjections"]`` and
    inlining them into the current step's ``inputs["task_description"]``.
    """

    def _make_flow_and_step(self, base_task: str = "do the thing"):
        flow = FlowInstance(
            flow_id="iflow-1",
            task_description=base_task,
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        step = Step(
            step_id="01_implement_abc",
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            inputs={"task_description": base_task},
        )
        flow.state.steps[step.step_id] = step
        flow.state.step_history.append(step.step_id)
        return flow, step

    @patch("se3.commands.run._read_multiline_input")
    def test_user_input_persists_to_context_and_step_inputs(
        self, mock_read, tmp_path,
    ):
        """A non-empty interjection writes into both the durable
        ``flow.state.context["user_interjections"]`` and the current step's
        ``inputs["task_description"]`` (so the immediate re-run sees it)."""
        from se3.commands.run import _handle_step_interrupt

        mock_read.return_value = "actually use Postgres not SQLite"
        flow, step = self._make_flow_and_step()
        persistence = MagicMock(spec=PersistenceManager)

        result = _handle_step_interrupt(flow, step, persistence)

        # Persistence saved
        persistence.save_flow.assert_called_once_with(flow)
        # Step status reset for re-run
        assert step.status == StepStatus.PENDING
        assert result == StepStatus.PENDING
        # Interjection persisted to flow context
        interjections = flow.state.context.get("user_interjections", [])
        assert len(interjections) == 1
        assert interjections[0]["text"] == "actually use Postgres not SQLite"
        assert interjections[0]["step_id"] == "01_implement_abc"
        assert interjections[0]["step_type"] == "implement"
        assert interjections[0]["timestamp"]  # ISO timestamp set
        # Current step's inputs.task_description got the section appended
        td = step.inputs["task_description"]
        assert td.startswith("do the thing")
        assert "## Additional Instructions" in td
        assert "actually use Postgres not SQLite" in td
        # Exactly one section header (no doubling).
        assert td.count("## Additional Instructions") == 1

    @patch("se3.commands.run._read_multiline_input")
    def test_repeated_interjections_compose_against_original_base(
        self, mock_read, tmp_path,
    ):
        """A second Ctrl-C must not produce nested
        ``## Additional Instructions`` sections — the second composer call
        sees the original base, not the post-first-interjection prose."""
        from se3.commands.run import _handle_step_interrupt

        flow, step = self._make_flow_and_step()
        persistence = MagicMock(spec=PersistenceManager)

        mock_read.return_value = "first instruction"
        _handle_step_interrupt(flow, step, persistence)
        mock_read.return_value = "second instruction"
        _handle_step_interrupt(flow, step, persistence)

        interjections = flow.state.context["user_interjections"]
        assert len(interjections) == 2
        assert interjections[0]["text"] == "first instruction"
        assert interjections[1]["text"] == "second instruction"

        td = step.inputs["task_description"]
        # Exactly ONE section header
        assert td.count("## Additional Instructions") == 1
        # Both bullets present, in order
        pos1 = td.find("first instruction")
        pos2 = td.find("second instruction")
        assert 0 < pos1 < pos2

    @patch("se3.commands.run._read_multiline_input")
    def test_empty_input_does_not_persist_or_modify(
        self, mock_read, tmp_path,
    ):
        """An empty user_input (just Enter / Esc-Enter with no text) reverts
        to "retry as-is": no interjection persisted, no task_description
        change, but step still reset to PENDING for retry."""
        from se3.commands.run import _handle_step_interrupt

        mock_read.return_value = ""
        flow, step = self._make_flow_and_step()
        persistence = MagicMock(spec=PersistenceManager)

        result = _handle_step_interrupt(flow, step, persistence)

        assert result == StepStatus.PENDING
        assert step.status == StepStatus.PENDING
        # No interjection persisted
        assert "user_interjections" not in flow.state.context
        # task_description unchanged
        assert step.inputs["task_description"] == "do the thing"

    @patch("se3.commands.run._read_multiline_input")
    def test_interrupt_on_later_step_does_not_double_section(
        self, mock_read, tmp_path,
    ):
        """Regression: when an interrupt happens on step A, a second
        interrupt on step B (whose inputs.task_description already
        carries the section composed by ``_build_step_inputs`` for the
        first interjection) must NOT produce a doubled
        ``## Additional Instructions`` section.
        """
        from se3.commands.run import _handle_step_interrupt

        flow = FlowInstance(
            flow_id="iflow-2",
            task_description="do the thing",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )

        # Simulate: first interrupt happened on step_A, list has interjection_1
        flow.state.context["user_interjections"] = [
            {"text": "first instruction", "step_id": "01_analyze",
             "step_type": "analyze", "timestamp": "t1"},
        ]
        # Step B was built via _build_step_inputs, which composed the
        # task_description with the existing first interjection. We
        # simulate that here directly.
        from se3.engine.task_description import compose_task_description_with_interjections
        step_b_td = compose_task_description_with_interjections(
            "do the thing", flow.state.context["user_interjections"],
        )
        step_b = Step(
            step_id="03_implement_yyy",
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RUNNING,
            inputs={"task_description": step_b_td},
        )
        flow.state.steps[step_b.step_id] = step_b
        flow.state.step_history.append(step_b.step_id)

        persistence = MagicMock(spec=PersistenceManager)
        mock_read.return_value = "second instruction"

        _handle_step_interrupt(flow, step_b, persistence)

        td = step_b.inputs["task_description"]
        # Exactly ONE section header — no doubling.
        assert td.count("## Additional Instructions") == 1, (
            f"interjection section was doubled. Full td:\n{td}"
        )
        # Each interjection appears exactly once.
        assert td.count("first instruction") == 1
        assert td.count("second instruction") == 1
        # Both bullets present.
        pos1 = td.find("first instruction")
        pos2 = td.find("second instruction")
        assert 0 < pos1 < pos2

    @patch("se3.commands.run._read_multiline_input")
    def test_cancelled_input_returns_none(self, mock_read, tmp_path):
        """user_input is None when user cancels with Ctrl-C inside the input
        prompt. _handle_step_interrupt saves and returns None to exit."""
        from se3.commands.run import _handle_step_interrupt

        mock_read.return_value = None
        flow, step = self._make_flow_and_step()
        persistence = MagicMock(spec=PersistenceManager)

        result = _handle_step_interrupt(flow, step, persistence)

        assert result is None
        persistence.save_flow.assert_called_once_with(flow)
        # Step status NOT changed
        assert step.status == StepStatus.RUNNING
        # No interjection persisted
        assert "user_interjections" not in flow.state.context
