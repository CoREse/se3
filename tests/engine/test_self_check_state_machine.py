"""Tests for SELF_CHECK integration with the state machine.

Verifies that:
- transition_to_next() recognizes SELF_CHECK REVISION_NEEDED and triggers fix loop
- _transition_to_fix() logs the correct source when triggered by SELF_CHECK
- _build_step_inputs() provides SELF_CHECK with test_results, changes_made, spec_content, fix_iteration
- fix_iterations shared counter increments correctly for SELF_CHECK triggers
- max_fix_iterations is respected when SELF_CHECK triggers the fix loop
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.state_machine import StateMachine


class TestSelfCheckTransitionToNext:
    """Test that transition_to_next handles SELF_CHECK REVISION_NEEDED."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        with patch("se3.engine.state_machine.PersistenceManager"):
            return StateMachine(project_root=tmp_path)

    def _make_flow_with_self_check(self, status=StepStatus.REVISION_NEEDED):
        flow = FlowInstance(
            flow_id="test-sc-flow",
            task_description="Test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
            StepType.VERIFY_SPEC,
            StepType.COMMIT,
        ]

        implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={
                "files_changed": ["src/foo.py"],
                "implemented_groups": [{"group_id": "G1"}],
            },
        )
        flow.state.add_step(implement_step)

        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True, "phases": []}},
        )
        flow.state.add_step(test_step)

        self_check_step = Step(
            step_type=StepType.SELF_CHECK,
            status=status,
            outputs={
                "self_check_result": "issues_found",
                "issues": [
                    {"severity": "high", "description": "Missing null check", "location": "src/foo.py:42"},
                ],
                "actionable_count": 1,
                "fix_needed": True,
                "fix_instructions": "Self-check found 1 issue(s) that need fixing:\n- [high] src/foo.py:42: Missing null check",
                "fix_context": {"reason": "self_check", "issues": [{"severity": "high"}], "iteration": 1},
            },
        )
        flow.state.add_step(self_check_step)
        flow.state.current_step_id = self_check_step.step_id

        return flow, implement_step, self_check_step

    def test_self_check_revision_needed_triggers_fix_loop(self, state_machine):
        """SELF_CHECK returning REVISION_NEEDED should transition back to IMPLEMENT."""
        flow, implement_step, _ = self._make_flow_with_self_check()

        next_step = state_machine.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.IMPLEMENT
        assert next_step.step_id == implement_step.step_id
        assert flow.state.get_fix_iteration() == 1

    def test_self_check_revision_sets_implement_fix_inputs(self, state_machine):
        """Fix loop from SELF_CHECK should set fix inputs on implement step."""
        flow, implement_step, _ = self._make_flow_with_self_check()

        state_machine.transition_to_next(flow)

        assert implement_step.inputs["is_fix_iteration"] is True
        assert implement_step.inputs["fix_iteration"] == 1
        assert "Self-check found" in implement_step.inputs["fix_instructions"]
        assert implement_step.inputs["fix_context"]["reason"] == "self_check"

    def test_self_check_respects_max_fix_iterations(self, state_machine):
        """When max iterations are exhausted, SELF_CHECK should fail the flow."""
        flow, _, self_check_step = self._make_flow_with_self_check()
        flow.state.fix_iterations = 3

        with patch.object(state_machine, '_get_max_fix_iterations', return_value=3):
            next_step = state_machine.transition_to_next(flow)

        assert next_step is None
        assert flow.status == FlowStatus.FAILED

    def test_self_check_completed_proceeds_to_verify_spec(self, state_machine):
        """SELF_CHECK returning COMPLETED should proceed to VERIFY_SPEC."""
        flow, _, self_check_step = self._make_flow_with_self_check(status=StepStatus.COMPLETED)
        self_check_step.outputs["fix_needed"] = False

        next_step = state_machine.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.VERIFY_SPEC

    def test_self_check_shares_fix_iteration_with_test_and_verify(self, state_machine):
        """Fix iterations from SELF_CHECK share the global counter with TEST and VERIFY_SPEC."""
        flow, _, _ = self._make_flow_with_self_check()

        # First fix from self_check
        state_machine.transition_to_next(flow)
        assert flow.state.get_fix_iteration() == 1

        # Simulate the flow going through implement -> test -> self_check again
        # and self_check returning REVISION_NEEDED again
        self_check_step2 = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Still has issues",
                "fix_context": {"reason": "self_check", "issues": [], "iteration": 2},
            },
        )
        flow.state.add_step(self_check_step2)
        flow.state.current_step_id = self_check_step2.step_id

        state_machine.transition_to_next(flow)
        assert flow.state.get_fix_iteration() == 2


class TestTransitionToFixFromSelfCheck:
    """Test _transition_to_fix behavior when triggered by SELF_CHECK."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        with patch("se3.engine.state_machine.PersistenceManager"):
            return StateMachine(project_root=tmp_path)

    def test_fix_history_records_self_check_trigger(self, state_machine):
        """Fix history should record trigger_step_type as 'self_check'."""
        flow = FlowInstance(
            flow_id="test-sc-fix",
            task_description="Test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT, StepType.TEST, StepType.SELF_CHECK]

        implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": ["a.py"]},
        )
        flow.state.add_step(implement_step)

        self_check_step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Fix missing error handling",
                "fix_context": {"reason": "self_check", "issues": []},
            },
        )
        flow.state.add_step(self_check_step)
        flow.state.current_step_id = self_check_step.step_id

        state_machine._transition_to_fix(flow, self_check_step)

        assert len(flow.state.fix_history) == 1
        assert flow.state.fix_history[0]["trigger_step_type"] == "self_check"
        assert flow.state.fix_history[0]["reason"] == "self_check"

    def test_fix_transition_prints_self_check_source(self, state_machine, capsys):
        """The fix transition log should show 'Source: self_check (code review)'."""
        flow = FlowInstance(
            flow_id="test-sc-print",
            task_description="Test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.IMPLEMENT, StepType.SELF_CHECK]

        implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": ["a.py"]},
        )
        flow.state.add_step(implement_step)

        self_check_step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Fix issues",
                "fix_context": {"reason": "self_check"},
            },
        )
        flow.state.add_step(self_check_step)
        flow.state.current_step_id = self_check_step.step_id

        state_machine._transition_to_fix(flow, self_check_step)

        captured = capsys.readouterr()
        assert "Source: self_check (code review)" in captured.out
        assert "Code review found actionable issues" in captured.out


class TestBuildStepInputsSelfCheck:
    """Test that _build_step_inputs correctly builds inputs for SELF_CHECK."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        return StateMachine(project_root=tmp_path)

    def test_self_check_receives_test_results(self, state_machine):
        """SELF_CHECK inputs should include test_results from TEST step."""
        flow = FlowInstance(
            flow_id="test-inputs",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )

        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True, "phases": [{"name": "default", "passed": True}]}},
        )
        flow.state.add_step(test_step)

        inputs = state_machine._build_step_inputs(flow, StepType.SELF_CHECK)

        assert "test_results" in inputs
        assert inputs["test_results"]["passed"] is True

    def test_self_check_receives_changes_made(self, state_machine):
        """SELF_CHECK inputs should include changes_made from IMPLEMENT step."""
        flow = FlowInstance(
            flow_id="test-inputs",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )

        implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={
                "files_changed": ["src/a.py", "src/b.py"],
                "implemented_groups": [{"group_id": "G1", "summary": "Did stuff"}],
            },
        )
        flow.state.add_step(implement_step)

        inputs = state_machine._build_step_inputs(flow, StepType.SELF_CHECK)

        assert "changes_made" in inputs
        assert inputs["changes_made"]["files_changed"] == ["src/a.py", "src/b.py"]
        assert len(inputs["changes_made"]["implemented_groups"]) == 1

    def test_self_check_receives_fix_iteration_in_fix_loop(self, state_machine):
        """SELF_CHECK should receive fix_iteration when in a fix loop."""
        flow = FlowInstance(
            flow_id="test-inputs",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        flow.state.increment_fix_iteration(fix_context={"reason": "test_failure"})

        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )
        flow.state.add_step(test_step)

        inputs = state_machine._build_step_inputs(flow, StepType.SELF_CHECK)

        assert inputs["fix_iteration"] == 1

    def test_self_check_no_fix_iteration_when_zero(self, state_machine):
        """SELF_CHECK should not have fix_iteration when not in fix loop."""
        flow = FlowInstance(
            flow_id="test-inputs",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )

        inputs = state_machine._build_step_inputs(flow, StepType.SELF_CHECK)

        assert "fix_iteration" not in inputs

    def test_self_check_outputs_accessible_to_downstream(self, state_machine):
        """SELF_CHECK outputs should be accessible to downstream steps like VERIFY_SPEC."""
        flow = FlowInstance(
            flow_id="test-downstream",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )

        self_check_step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.COMPLETED,
            outputs={
                "self_check_result": "passed",
                "issues": [],
                "actionable_count": 0,
            },
        )
        flow.state.add_step(self_check_step)

        inputs = state_machine._build_step_inputs(flow, StepType.VERIFY_SPEC)

        assert inputs["self_check_result"] == "passed"
        assert inputs["self_check_issues"] == []

    def test_self_check_receives_all_required_inputs(self, state_machine):
        """Integration: SELF_CHECK receives test_results, changes_made, task_description."""
        flow = FlowInstance(
            flow_id="test-full",
            task_description="Implement feature X",
            status=FlowStatus.RUNNING,
        )

        analyze_step = Step(
            step_type=StepType.ANALYZE,
            status=StepStatus.COMPLETED,
            outputs={
                "task_type": "feature",
                "scope": "engine",
                "spec_content": "Spec content here",
                "project_summary": "Project summary",
                "relevant_specs": [{"name": "flow-engine"}],
            },
        )
        flow.state.add_step(analyze_step)

        implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={
                "files_changed": ["src/engine/foo.py"],
                "implemented_groups": [{"group_id": "G1"}],
                "summary": "Implemented feature X",
            },
        )
        flow.state.add_step(implement_step)

        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True, "phases": []}},
        )
        flow.state.add_step(test_step)

        inputs = state_machine._build_step_inputs(flow, StepType.SELF_CHECK)

        assert inputs["task_description"] == "Implement feature X"
        assert inputs["test_results"]["passed"] is True
        assert inputs["changes_made"]["files_changed"] == ["src/engine/foo.py"]


class TestStepSequencesIncludeSelfCheck:
    """Test that the correct step sequences include SELF_CHECK."""

    def test_feature_includes_self_check(self):
        from se3.engine.models import get_default_step_sequence
        seq = get_default_step_sequence("feature")
        assert StepType.SELF_CHECK in seq
        test_idx = seq.index(StepType.TEST)
        sc_idx = seq.index(StepType.SELF_CHECK)
        ic_idx = seq.index(StepType.INVARIANT_CHECK)
        assert test_idx < sc_idx < ic_idx

    def test_bugfix_includes_self_check(self):
        from se3.engine.models import get_default_step_sequence
        seq = get_default_step_sequence("bugfix")
        assert StepType.SELF_CHECK in seq
        test_idx = seq.index(StepType.TEST)
        sc_idx = seq.index(StepType.SELF_CHECK)
        ic_idx = seq.index(StepType.INVARIANT_CHECK)
        assert test_idx < sc_idx < ic_idx

    def test_discovery_includes_self_check(self):
        from se3.engine.models import get_default_step_sequence
        seq = get_default_step_sequence("discovery")
        assert StepType.SELF_CHECK in seq
        test_idx = seq.index(StepType.TEST)
        sc_idx = seq.index(StepType.SELF_CHECK)
        ic_idx = seq.index(StepType.INVARIANT_CHECK)
        assert test_idx < sc_idx < ic_idx

    def test_small_excludes_self_check(self):
        from se3.engine.models import get_default_step_sequence
        seq = get_default_step_sequence("small")
        assert StepType.SELF_CHECK not in seq

    def test_directive_excludes_self_check(self):
        from se3.engine.models import get_default_step_sequence
        seq = get_default_step_sequence("directive")
        assert StepType.SELF_CHECK not in seq


class TestSelfCheckHandlerRegistration:
    """Test that self_check_handler is properly registered."""

    def test_handler_in_step_handlers(self):
        from se3.engine.steps import STEP_HANDLERS
        assert StepType.SELF_CHECK in STEP_HANDLERS

    def test_handler_in_all(self):
        from se3.engine.steps import __all__
        assert "self_check_handler" in __all__

    def test_handler_is_callable(self):
        from se3.engine.steps import STEP_HANDLERS
        assert callable(STEP_HANDLERS[StepType.SELF_CHECK])
