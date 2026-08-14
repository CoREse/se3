"""Tests for PARTIAL status handling in state machine.

Tests that:
1. transition_to_next() allows ordinary/planned PARTIAL steps to flow forward
2. _build_step_inputs() forwards implement outputs when status is PARTIAL
3. PARTIAL does not trigger fix loops; direct/small IMPLEMENT is the explicit
   exception and re-enters until its whole-task contract is complete
"""

from __future__ import annotations

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


@pytest.fixture
def state_machine(tmp_path):
    """Create a StateMachine with a temporary project root."""
    with patch("tianluo.engine.state_machine.PersistenceManager"):
        sm = StateMachine(project_root=tmp_path)
        sm.persistence = Mock()
        sm.persistence.save_flow = Mock()
        return sm


def _make_flow_with_implement(step_status, step_outputs=None):
    """Helper: create a flow with implement step at given status, followed by commit."""
    flow = FlowInstance(
        task_description="test task",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = [StepType.IMPLEMENT, StepType.COMMIT]
    flow.state.current_step_index = 0

    implement_step = Step(
        step_type=StepType.IMPLEMENT,
        status=step_status,
        outputs=step_outputs or {},
    )
    flow.state.add_step(implement_step)
    flow.state.current_step_id = implement_step.step_id
    return flow, implement_step


class TestPartialTransition:
    """Test that transition_to_next() handles PARTIAL status correctly."""

    def test_partial_transitions_to_next_step(self, state_machine):
        """PARTIAL should transition to the next step like COMPLETED."""
        flow, impl_step = _make_flow_with_implement(StepStatus.PARTIAL)

        next_step = state_machine.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.COMMIT
        assert next_step.status == StepStatus.PENDING

    def test_completed_still_transitions(self, state_machine):
        """Sanity check: COMPLETED still works after our change."""
        flow, impl_step = _make_flow_with_implement(StepStatus.COMPLETED)

        next_step = state_machine.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.COMMIT

    def test_failed_does_not_transition(self, state_machine):
        """FAILED should NOT transition to next step."""
        flow, impl_step = _make_flow_with_implement(StepStatus.FAILED)

        next_step = state_machine.transition_to_next(flow)

        assert next_step is None

    def test_partial_does_not_trigger_fix_loop(self, state_machine):
        """PARTIAL on a TEST step should NOT trigger fix loop (it's not REVISION_NEEDED)."""
        flow = FlowInstance(
            task_description="test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT, StepType.TEST, StepType.COMMIT]
        flow.state.current_step_index = 1

        # Add implement step in history
        impl_step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED)
        flow.state.add_step(impl_step)

        # Add test step as current with PARTIAL status
        test_step = Step(step_type=StepType.TEST, status=StepStatus.PARTIAL)
        flow.state.add_step(test_step)
        flow.state.current_step_id = test_step.step_id

        next_step = state_machine.transition_to_next(flow)

        # Should go to COMMIT, not back to IMPLEMENT
        assert next_step is not None
        assert next_step.step_type == StepType.COMMIT

    def test_partial_completes_flow_when_last_step(self, state_machine):
        """PARTIAL on the last step should complete the flow."""
        flow = FlowInstance(
            task_description="test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT]
        flow.state.current_step_index = 0

        impl_step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.PARTIAL)
        flow.state.add_step(impl_step)
        flow.state.current_step_id = impl_step.step_id

        next_step = state_machine.transition_to_next(flow)

        assert next_step is None
        assert flow.status == FlowStatus.COMPLETED


class TestHolisticPartialContinuation:
    """Only direct/small partial implementation is forced to continue."""

    @patch("tianluo.engine.state_machine.clear_phase1_cache")
    def test_direct_partial_reenters_same_implement(
        self, mock_clear_cache, state_machine,
    ):
        flow, impl_step = _make_flow_with_implement(
            StepStatus.PARTIAL,
            {
                "files_changed": ["partial.py"],
                "completion_status": "partial",
                "incomplete_tasks": ["finish integration"],
            },
        )
        flow.state.selected_steps = [StepType.IMPLEMENT, StepType.TEST]
        flow.state.context["effective_implementation_strategy"] = "direct"

        next_step = state_machine.transition_to_next(flow)

        assert next_step is impl_step
        assert impl_step.status == StepStatus.PENDING
        assert flow.state.current_step_id == impl_step.step_id
        assert impl_step.inputs["resumed"] is True
        assert impl_step.inputs["retry_count"] == 1
        assert impl_step.inputs["previous_output"]["files_changed"] == [
            "partial.py"
        ]
        mock_clear_cache.assert_called_once()

    @patch("tianluo.engine.state_machine.clear_phase1_cache")
    def test_complete_with_incomplete_tasks_still_reenters(
        self, mock_clear_cache, state_machine,
    ):
        flow, impl_step = _make_flow_with_implement(
            StepStatus.COMPLETED,
            {
                "completion_status": "complete",
                "incomplete_tasks": ["still pending"],
            },
        )
        flow.state.context["effective_implementation_strategy"] = "direct"

        assert state_machine.transition_to_next(flow) is impl_step
        assert impl_step.status == StepStatus.PENDING

    def test_direct_complete_and_empty_advances_to_test(self, state_machine):
        flow, _ = _make_flow_with_implement(
            StepStatus.COMPLETED,
            {"completion_status": "complete", "incomplete_tasks": []},
        )
        flow.state.selected_steps = [StepType.IMPLEMENT, StepType.TEST]
        flow.state.context["effective_implementation_strategy"] = "direct"

        next_step = state_machine.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.TEST

    @patch("tianluo.engine.state_machine.clear_phase1_cache")
    def test_small_partial_uses_the_same_continuation_rule(
        self, mock_clear_cache, state_machine,
    ):
        flow, impl_step = _make_flow_with_implement(
            StepStatus.PARTIAL,
            {"completion_status": "partial", "incomplete_tasks": ["rest"]},
        )
        flow.task_type = "small"
        impl_step.inputs["task_type"] = "small"

        assert state_machine.transition_to_next(flow) is impl_step
        assert impl_step.status == StepStatus.PENDING
        mock_clear_cache.assert_called_once()


class TestPartialRunLoop:
    """Test that init_flow() + run_step()/transition_to_next() handles PARTIAL without retrying."""

    def test_partial_does_not_retry(self, state_machine):
        """PARTIAL status should NOT trigger retry logic."""
        flow, impl_step = _make_flow_with_implement(StepStatus.PENDING)
        flow.state.selected_steps = [StepType.IMPLEMENT]
        flow.status = FlowStatus.RUNNING

        call_count = 0

        def handler(step, fl):
            nonlocal call_count
            call_count += 1
            step.status = StepStatus.PARTIAL
            step.outputs["summary"] = "partial work"
            return StepStatus.PARTIAL

        state_machine.register_handler(StepType.IMPLEMENT, handler)

        state_machine.init_flow(flow)
        step_status = state_machine.run_step(flow, impl_step)
        state_machine.transition_to_next(flow)

        # Handler should be called exactly once (no retries)
        assert call_count == 1
        assert step_status == StepStatus.PARTIAL
        assert flow.status == FlowStatus.COMPLETED


class TestBuildStepInputsPartial:
    """Test that _build_step_inputs() forwards implement outputs when PARTIAL."""

    def test_partial_implement_outputs_forwarded(self, state_machine):
        """Implement outputs should be forwarded when status is PARTIAL."""
        flow = FlowInstance(
            task_description="test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT, StepType.COMMIT]

        impl_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.PARTIAL,
            outputs={
                "files_changed": ["a.py"],
                "implemented_groups": ["G1"],
                "tests_added": ["test_a.py"],
                "test_mapping": {"a.py": ["test_a.py"]},
                "summary": "Implemented G1, could not implement G2",
                "completion_status": "partial",
                "incomplete_tasks": [{"id": 3, "reason": "permission denied"}],
                "restricted_edits_applied": [{"file": ".claude/x.md"}],
                "restricted_edits_failed": [{"file": ".claude/y.md", "error": "not found"}],
            },
        )
        flow.state.add_step(impl_step)

        inputs = state_machine._build_step_inputs(flow, StepType.COMMIT)

        # Existing fields
        assert inputs["changes_made"]["files_changed"] == ["a.py"]
        assert inputs["changes_made"]["implemented_groups"] == ["G1"]
        assert inputs["tests_added"] == ["test_a.py"]
        assert inputs["test_mapping"] == {"a.py": ["test_a.py"]}
        # New fields
        assert inputs["implement_summary"] == "Implemented G1, could not implement G2"
        assert inputs["completion_status"] == "partial"
        assert inputs["incomplete_tasks"] == [{"id": 3, "reason": "permission denied"}]
        assert inputs["restricted_edits_applied"] == [{"file": ".claude/x.md"}]
        assert inputs["restricted_edits_failed"] == [{"file": ".claude/y.md", "error": "not found"}]

    def test_completed_implement_outputs_forwarded(self, state_machine):
        """Implement outputs should still be forwarded when status is COMPLETED."""
        flow = FlowInstance(
            task_description="test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT, StepType.COMMIT]

        impl_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={
                "files_changed": ["b.py"],
                "implemented_groups": [],
                "tests_added": [],
                "test_mapping": {},
                "summary": "All done",
                "completion_status": "complete",
                "incomplete_tasks": [],
                "restricted_edits_applied": [],
                "restricted_edits_failed": [],
            },
        )
        flow.state.add_step(impl_step)

        inputs = state_machine._build_step_inputs(flow, StepType.COMMIT)

        assert inputs["implement_summary"] == "All done"
        assert inputs["completion_status"] == "complete"
        assert inputs["incomplete_tasks"] == []

    def test_failed_implement_outputs_not_forwarded(self, state_machine):
        """FAILED implement step outputs should NOT be forwarded."""
        flow = FlowInstance(
            task_description="test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT, StepType.COMMIT]

        impl_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.FAILED,
            outputs={
                "summary": "Should not appear",
                "completion_status": "failed",
            },
        )
        flow.state.add_step(impl_step)

        inputs = state_machine._build_step_inputs(flow, StepType.COMMIT)

        assert "implement_summary" not in inputs
        assert "completion_status" not in inputs

    def test_backward_compatible_defaults(self, state_machine):
        """When implement outputs lack new fields, defaults are used."""
        flow = FlowInstance(
            task_description="test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT, StepType.COMMIT]

        # Old-style implement step with no new fields
        impl_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={
                "files_changed": ["c.py"],
            },
        )
        flow.state.add_step(impl_step)

        inputs = state_machine._build_step_inputs(flow, StepType.COMMIT)

        assert inputs["implement_summary"] == ""
        assert inputs["completion_status"] == "complete"
        assert inputs["incomplete_tasks"] == []
        assert inputs["restricted_edits_applied"] == []
        assert inputs["restricted_edits_failed"] == []

    def test_partial_forwarded_to_summarize(self, state_machine):
        """PARTIAL implement outputs should be forwarded to SUMMARIZE step too."""
        flow = FlowInstance(
            task_description="test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT, StepType.SUMMARIZE]

        impl_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.PARTIAL,
            outputs={
                "summary": "Partial work done",
                "completion_status": "partial",
                "incomplete_tasks": [{"id": 1, "reason": "restricted"}],
            },
        )
        flow.state.add_step(impl_step)

        inputs = state_machine._build_step_inputs(flow, StepType.SUMMARIZE)

        assert inputs["implement_summary"] == "Partial work done"
        assert inputs["completion_status"] == "partial"
        assert inputs["incomplete_tasks"] == [{"id": 1, "reason": "restricted"}]


class TestRunStepDiscoveredIssuesPartial:
    """Test that run_step() collects discovered_issues from PARTIAL steps."""

    def test_partial_step_discovered_issues_collected(self, state_machine):
        """PARTIAL steps should have their discovered_issues collected."""
        flow = FlowInstance(
            task_description="test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT]
        impl_step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.PENDING)
        flow.state.add_step(impl_step)
        flow.state.current_step_id = impl_step.step_id

        discovered = [{"title": "found a thing", "type": "b-class"}]

        def handler(step, fl):
            step.status = StepStatus.PARTIAL
            step.outputs["discovered_issues"] = discovered
            return StepStatus.PARTIAL

        state_machine.register_handler(StepType.IMPLEMENT, handler)

        mock_discovery = Mock()
        state_machine._get_issue_discovery = Mock(return_value=mock_discovery)

        state_machine.run_step(flow, impl_step)

        mock_discovery.collect_issues_from_output.assert_called_once_with(
            flow, "implement", impl_step.outputs,
        )

    def test_completed_step_discovered_issues_still_collected(self, state_machine):
        """Sanity check: COMPLETED steps still have discovered_issues collected."""
        flow = FlowInstance(
            task_description="test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT]
        impl_step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.PENDING)
        flow.state.add_step(impl_step)
        flow.state.current_step_id = impl_step.step_id

        def handler(step, fl):
            step.status = StepStatus.COMPLETED
            step.outputs["discovered_issues"] = [{"title": "issue"}]
            return StepStatus.COMPLETED

        state_machine.register_handler(StepType.IMPLEMENT, handler)

        mock_discovery = Mock()
        state_machine._get_issue_discovery = Mock(return_value=mock_discovery)

        state_machine.run_step(flow, impl_step)

        mock_discovery.collect_issues_from_output.assert_called_once()

    def test_failed_step_discovered_issues_not_collected(self, state_machine):
        """FAILED steps should NOT have discovered_issues collected."""
        flow = FlowInstance(
            task_description="test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT]
        impl_step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.PENDING)
        flow.state.add_step(impl_step)
        flow.state.current_step_id = impl_step.step_id

        def handler(step, fl):
            step.status = StepStatus.FAILED
            step.outputs["discovered_issues"] = [{"title": "issue"}]
            return StepStatus.FAILED

        state_machine.register_handler(StepType.IMPLEMENT, handler)

        mock_discovery = Mock()
        state_machine._get_issue_discovery = Mock(return_value=mock_discovery)

        state_machine.run_step(flow, impl_step)

        mock_discovery.collect_issues_from_output.assert_not_called()


class TestPartialFlowDoesNotFail:
    """Test that PARTIAL implement does not set flow status to FAILED."""

    def test_partial_implement_does_not_fail_flow(self, state_machine):
        """A PARTIAL implement step should not cause FlowStatus.FAILED."""
        flow = FlowInstance(
            task_description="test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT, StepType.COMMIT]
        flow.state.current_step_index = 0

        impl_step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.PENDING)
        flow.state.add_step(impl_step)
        flow.state.current_step_id = impl_step.step_id

        call_order = []

        def impl_handler(step, fl):
            call_order.append("implement")
            step.status = StepStatus.PARTIAL
            step.outputs["summary"] = "partial work"
            step.outputs["completion_status"] = "partial"
            step.outputs["incomplete_tasks"] = [{"id": 1, "reason": "restricted"}]
            step.outputs["files_changed"] = ["a.py"]
            return StepStatus.PARTIAL

        def commit_handler(step, fl):
            call_order.append("commit")
            # Verify commit received partial status inputs
            assert step.inputs.get("completion_status") == "partial"
            assert step.inputs.get("incomplete_tasks") == [{"id": 1, "reason": "restricted"}]
            step.status = StepStatus.COMPLETED
            return StepStatus.COMPLETED

        state_machine.register_handler(StepType.IMPLEMENT, impl_handler)
        state_machine.register_handler(StepType.COMMIT, commit_handler)

        state_machine.init_flow(flow)

        # Drive the flow manually: run_step + transition_to_next
        while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED):
            current = flow.state.get_current_step()
            if not current:
                flow.status = FlowStatus.COMPLETED
                break
            state_machine.run_step(flow, current)
            if not state_machine.transition_to_next(flow):
                break

        assert flow.status == FlowStatus.COMPLETED
        assert flow.status != FlowStatus.FAILED
        assert call_order == ["implement", "commit"]

    def test_partial_implement_through_full_pipeline(self, state_machine):
        """End-to-end: PARTIAL implement → commit → summarize with correct input forwarding."""
        flow = FlowInstance(
            task_description="test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT, StepType.COMMIT, StepType.SUMMARIZE]
        flow.state.current_step_index = 0

        impl_step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.PENDING)
        flow.state.add_step(impl_step)
        flow.state.current_step_id = impl_step.step_id

        received_inputs = {}

        def impl_handler(step, fl):
            step.status = StepStatus.PARTIAL
            step.outputs.update({
                "summary": "Did G1, could not do G2",
                "completion_status": "partial",
                "incomplete_tasks": [{"id": 3, "reason": "permission denied"}],
                "files_changed": ["a.py", "b.py"],
                "implemented_groups": ["G1"],
                "tests_added": ["test_a.py"],
                "test_mapping": {"a.py": ["test_a.py"]},
                "restricted_edits_applied": [{"file": ".claude/x.md"}],
                "restricted_edits_failed": [],
            })
            return StepStatus.PARTIAL

        def commit_handler(step, fl):
            received_inputs["commit"] = dict(step.inputs)
            step.status = StepStatus.COMPLETED
            return StepStatus.COMPLETED

        def summarize_handler(step, fl):
            received_inputs["summarize"] = dict(step.inputs)
            step.status = StepStatus.COMPLETED
            return StepStatus.COMPLETED

        state_machine.register_handler(StepType.IMPLEMENT, impl_handler)
        state_machine.register_handler(StepType.COMMIT, commit_handler)
        state_machine.register_handler(StepType.SUMMARIZE, summarize_handler)

        state_machine.init_flow(flow)

        # Drive the flow manually: run_step + transition_to_next
        while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED):
            current = flow.state.get_current_step()
            if not current:
                flow.status = FlowStatus.COMPLETED
                break
            state_machine.run_step(flow, current)
            if not state_machine.transition_to_next(flow):
                break

        assert flow.status == FlowStatus.COMPLETED

        # Verify commit received correct inputs
        ci = received_inputs["commit"]
        assert ci["completion_status"] == "partial"
        assert ci["incomplete_tasks"] == [{"id": 3, "reason": "permission denied"}]
        assert ci["implement_summary"] == "Did G1, could not do G2"
        assert ci["restricted_edits_applied"] == [{"file": ".claude/x.md"}]
        assert ci["changes_made"]["files_changed"] == ["a.py", "b.py"]

        # Verify summarize received correct inputs
        si = received_inputs["summarize"]
        assert si["completion_status"] == "partial"
        assert si["incomplete_tasks"] == [{"id": 3, "reason": "permission denied"}]
        assert si["implement_summary"] == "Did G1, could not do G2"
