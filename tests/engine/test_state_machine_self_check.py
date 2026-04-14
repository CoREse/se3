"""Integration tests for SELF_CHECK step in the state machine.

Verifies that SELF_CHECK integrates correctly with the state machine:
transition_to_next handles REVISION_NEEDED, fix_iterations are shared
across TEST/SELF_CHECK/VERIFY_SPEC, _build_step_inputs populates the
right data, and step sequences include SELF_CHECK in the right position.
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
    get_default_step_sequence,
)
from se3.engine.state_machine import StateMachine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state_machine(tmp_path):
    with patch("se3.engine.state_machine.PersistenceManager"):
        return StateMachine(project_root=tmp_path)


def _make_flow(
    tmp_path,
    selected_steps=None,
    task_type="feature",
    task_description="Implement feature X",
):
    flow = FlowInstance(
        flow_id="test-sc-flow",
        task_description=task_description,
        task_type=task_type,
        status=FlowStatus.RUNNING,
    )
    if selected_steps:
        flow.state.selected_steps = selected_steps
    return flow


def _add_step(flow, step_type, status, outputs=None, inputs=None):
    step = Step(
        step_type=step_type,
        status=status,
        inputs=inputs or {},
        outputs=outputs or {},
    )
    flow.state.add_step(step)
    return step


# ---------------------------------------------------------------------------
# 1. SELF_CHECK REVISION_NEEDED triggers _transition_to_fix
# ---------------------------------------------------------------------------


class TestSelfCheckRevisionNeeded:
    """Verify that SELF_CHECK's REVISION_NEEDED triggers the fix loop."""

    @pytest.fixture
    def sm(self, tmp_path):
        return _make_state_machine(tmp_path)

    @pytest.fixture
    def flow_with_self_check_revision(self, tmp_path):
        flow = _make_flow(
            tmp_path,
            selected_steps=[
                StepType.IMPLEMENT,
                StepType.TEST,
                StepType.SELF_CHECK,
                StepType.VERIFY_SPEC,
                StepType.COMMIT,
            ],
        )

        implement_step = _add_step(
            flow,
            StepType.IMPLEMENT,
            StepStatus.COMPLETED,
            outputs={"files_changed": ["src/feature.py"], "summary": "Added feature"},
        )

        _add_step(
            flow,
            StepType.TEST,
            StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True, "overall_passed": True}},
        )

        self_check_step = _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Missing null check in feature.py:30",
                "fix_context": {
                    "reason": "self_check",
                    "issues": [
                        {
                            "severity": "critical",
                            "description": "Missing null check",
                            "location": "src/feature.py:30",
                        }
                    ],
                    "iteration": 1,
                },
                "actionable_count": 1,
                "issues": [
                    {
                        "severity": "critical",
                        "description": "Missing null check",
                        "location": "src/feature.py:30",
                    }
                ],
            },
        )
        flow.state.current_step_id = self_check_step.step_id
        return flow, implement_step, self_check_step

    def test_triggers_fix_loop(self, sm, flow_with_self_check_revision):
        flow, implement_step, _ = flow_with_self_check_revision

        next_step = sm.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.IMPLEMENT
        assert next_step.step_id == implement_step.step_id

    def test_increments_fix_iteration(self, sm, flow_with_self_check_revision):
        flow, _, _ = flow_with_self_check_revision
        assert flow.state.get_fix_iteration() == 0

        sm.transition_to_next(flow)

        assert flow.state.get_fix_iteration() == 1

    def test_sets_fix_inputs_on_implement(self, sm, flow_with_self_check_revision):
        flow, implement_step, _ = flow_with_self_check_revision

        sm.transition_to_next(flow)

        assert implement_step.inputs["is_fix_iteration"] is True
        assert implement_step.inputs["fix_iteration"] == 1
        assert "Missing null check" in implement_step.inputs["fix_instructions"]
        assert implement_step.inputs["fix_context"]["reason"] == "self_check"

    def test_resets_implement_to_pending(self, sm, flow_with_self_check_revision):
        flow, implement_step, _ = flow_with_self_check_revision

        sm.transition_to_next(flow)

        assert implement_step.status == StepStatus.PENDING


# ---------------------------------------------------------------------------
# 2. fix_iterations shared counter across TEST, SELF_CHECK, VERIFY_SPEC
# ---------------------------------------------------------------------------


class TestSharedFixIterations:
    """Verify that fix_iterations is a single global counter shared by all three steps."""

    @pytest.fixture
    def sm(self, tmp_path):
        return _make_state_machine(tmp_path)

    def _build_flow_at_step(self, tmp_path, trigger_type, trigger_outputs):
        """Build a flow where `trigger_type` is the current step in REVISION_NEEDED."""
        flow = _make_flow(
            tmp_path,
            selected_steps=[
                StepType.IMPLEMENT,
                StepType.TEST,
                StepType.SELF_CHECK,
                StepType.VERIFY_SPEC,
                StepType.COMMIT,
            ],
        )

        _add_step(
            flow,
            StepType.IMPLEMENT,
            StepStatus.COMPLETED,
            outputs={"files_changed": ["a.py"]},
        )

        if trigger_type != StepType.TEST:
            _add_step(
                flow,
                StepType.TEST,
                StepStatus.COMPLETED,
                outputs={"test_results": {"passed": True}},
            )

        if trigger_type == StepType.VERIFY_SPEC:
            _add_step(
                flow,
                StepType.SELF_CHECK,
                StepStatus.COMPLETED,
                outputs={"issues": [], "actionable_count": 0},
            )

        trigger_step = _add_step(
            flow, trigger_type, StepStatus.REVISION_NEEDED, outputs=trigger_outputs
        )
        flow.state.current_step_id = trigger_step.step_id
        return flow

    def test_counter_increments_from_test(self, sm, tmp_path):
        flow = self._build_flow_at_step(
            tmp_path,
            StepType.TEST,
            {
                "fix_needed": True,
                "fix_instructions": "Tests failed",
                "fix_context": {"test_failed": True},
            },
        )
        sm.transition_to_next(flow)
        assert flow.state.get_fix_iteration() == 1

    def test_counter_increments_from_self_check(self, sm, tmp_path):
        flow = self._build_flow_at_step(
            tmp_path,
            StepType.SELF_CHECK,
            {
                "fix_needed": True,
                "fix_instructions": "Self-check issues",
                "fix_context": {"reason": "self_check"},
            },
        )
        sm.transition_to_next(flow)
        assert flow.state.get_fix_iteration() == 1

    def test_counter_increments_from_verify_spec(self, sm, tmp_path):
        flow = self._build_flow_at_step(
            tmp_path,
            StepType.VERIFY_SPEC,
            {
                "fix_needed": True,
                "fix_instructions": "Spec issues",
                "fix_context": {"spec_issues": True},
            },
        )
        sm.transition_to_next(flow)
        assert flow.state.get_fix_iteration() == 1

    def test_cumulative_across_steps(self, sm, tmp_path):
        """Simulate: TEST triggers fix (iter 1), then SELF_CHECK triggers fix (iter 2)."""
        flow = _make_flow(
            tmp_path,
            selected_steps=[
                StepType.IMPLEMENT,
                StepType.TEST,
                StepType.SELF_CHECK,
                StepType.VERIFY_SPEC,
                StepType.COMMIT,
            ],
        )
        _add_step(
            flow,
            StepType.IMPLEMENT,
            StepStatus.COMPLETED,
            outputs={"files_changed": ["a.py"]},
        )

        # First: TEST triggers fix → iter 1
        test_step = _add_step(
            flow,
            StepType.TEST,
            StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Tests failed",
                "fix_context": {"test_failed": True},
            },
        )
        flow.state.current_step_id = test_step.step_id

        sm.transition_to_next(flow)
        assert flow.state.get_fix_iteration() == 1

        # After fix, TEST passes, SELF_CHECK triggers fix → iter 2
        test_step.status = StepStatus.COMPLETED
        test_step.outputs = {"test_results": {"passed": True}}

        sc_step = _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Code review issues",
                "fix_context": {"reason": "self_check"},
            },
        )
        flow.state.current_step_id = sc_step.step_id

        sm.transition_to_next(flow)
        assert flow.state.get_fix_iteration() == 2


# ---------------------------------------------------------------------------
# 3. max_fix_iterations enforcement for SELF_CHECK
# ---------------------------------------------------------------------------


class TestMaxFixIterationsForSelfCheck:
    """Verify that when max_fix_iterations is exhausted, SELF_CHECK doesn't trigger fix loop."""

    @pytest.fixture
    def sm(self, tmp_path):
        return _make_state_machine(tmp_path)

    def test_proceeds_to_verify_spec_at_max(self, sm, tmp_path):
        flow = _make_flow(
            tmp_path,
            selected_steps=[
                StepType.IMPLEMENT,
                StepType.TEST,
                StepType.SELF_CHECK,
                StepType.VERIFY_SPEC,
                StepType.COMMIT,
            ],
        )
        _add_step(
            flow,
            StepType.IMPLEMENT,
            StepStatus.COMPLETED,
            outputs={"files_changed": ["a.py"]},
        )
        _add_step(
            flow,
            StepType.TEST,
            StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )

        sc_step = _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Issues remain",
                "fix_context": {"reason": "self_check"},
                "actionable_count": 1,
            },
        )
        flow.state.current_step_id = sc_step.step_id

        # Exhaust fix iterations
        flow.state.fix_iterations = 3

        with patch.object(sm, "_get_max_fix_iterations", return_value=3):
            next_step = sm.transition_to_next(flow)

        assert next_step is None
        assert flow.status == FlowStatus.FAILED

    def test_fix_iteration_not_incremented_at_max(self, sm, tmp_path):
        flow = _make_flow(
            tmp_path,
            selected_steps=[
                StepType.IMPLEMENT,
                StepType.TEST,
                StepType.SELF_CHECK,
                StepType.VERIFY_SPEC,
            ],
        )
        _add_step(
            flow,
            StepType.IMPLEMENT,
            StepStatus.COMPLETED,
            outputs={"files_changed": []},
        )
        _add_step(
            flow,
            StepType.TEST,
            StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )

        sc_step = _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Still issues",
                "fix_context": {"reason": "self_check"},
            },
        )
        flow.state.current_step_id = sc_step.step_id
        flow.state.fix_iterations = 3

        with patch.object(sm, "_get_max_fix_iterations", return_value=3):
            sm.transition_to_next(flow)

        # Counter should NOT have been incremented further
        assert flow.state.get_fix_iteration() == 3


# ---------------------------------------------------------------------------
# 4. _build_step_inputs for SELF_CHECK
# ---------------------------------------------------------------------------


class TestBuildStepInputsForSelfCheck:
    """Verify _build_step_inputs constructs the right inputs for SELF_CHECK."""

    @pytest.fixture
    def sm(self, tmp_path):
        return _make_state_machine(tmp_path)

    def _make_flow_with_history(self, tmp_path):
        flow = _make_flow(tmp_path)
        _add_step(
            flow,
            StepType.ANALYZE,
            StepStatus.COMPLETED,
            outputs={
                "task_type": "feature",
                "scope": "src/engine",
                "spec_content": {"base": "Base spec"},
            },
        )
        _add_step(
            flow,
            StepType.IMPLEMENT,
            StepStatus.COMPLETED,
            outputs={
                "files_changed": ["src/feature.py", "tests/test_feature.py"],
                "implemented_groups": ["G1"],
                "summary": "Implemented feature X",
            },
        )
        _add_step(
            flow,
            StepType.TEST,
            StepStatus.COMPLETED,
            outputs={
                "test_results": {
                    "overall_passed": True,
                    "phases": [{"name": "default", "passed": True}],
                }
            },
        )
        return flow

    def test_includes_test_results(self, sm, tmp_path):
        flow = self._make_flow_with_history(tmp_path)

        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)

        assert "test_results" in inputs
        assert inputs["test_results"]["overall_passed"] is True

    def test_includes_changes_made(self, sm, tmp_path):
        flow = self._make_flow_with_history(tmp_path)

        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)

        assert "changes_made" in inputs
        assert "src/feature.py" in inputs["changes_made"]["files_changed"]

    def test_includes_spec_content(self, sm, tmp_path):
        flow = self._make_flow_with_history(tmp_path)

        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)

        assert "spec_content" in inputs
        assert inputs["spec_content"]["base"] == "Base spec"

    def test_includes_task_description(self, sm, tmp_path):
        flow = self._make_flow_with_history(tmp_path)

        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)

        assert inputs["task_description"] == "Implement feature X"

    def test_includes_fix_iteration_when_in_fix_loop(self, sm, tmp_path):
        flow = self._make_flow_with_history(tmp_path)
        flow.state.fix_iterations = 2

        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)

        assert inputs["fix_iteration"] == 2

    def test_no_fix_iteration_when_initial(self, sm, tmp_path):
        flow = self._make_flow_with_history(tmp_path)

        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)

        assert inputs.get("fix_iteration") is None or inputs.get("fix_iteration") == 0

    def test_fallback_to_history_for_test_results(self, sm, tmp_path):
        """When SELF_CHECK receives inputs, test_results should come from TEST step history."""
        flow = _make_flow(tmp_path)

        _add_step(
            flow,
            StepType.IMPLEMENT,
            StepStatus.COMPLETED,
            outputs={"files_changed": ["a.py"]},
        )

        _add_step(
            flow,
            StepType.TEST,
            StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True, "command": "pytest"}},
        )

        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)

        assert inputs["test_results"]["passed"] is True
        assert inputs["test_results"]["command"] == "pytest"


# ---------------------------------------------------------------------------
# 5. Step sequences: SELF_CHECK position and presence
# ---------------------------------------------------------------------------


class TestStepSequences:
    """Verify SELF_CHECK appears in the right sequences at the right position."""

    def test_feature_includes_self_check(self):
        seq = get_default_step_sequence("feature")
        assert StepType.SELF_CHECK in seq

    def test_feature_self_check_between_test_and_verify(self):
        seq = get_default_step_sequence("feature")
        test_idx = seq.index(StepType.TEST)
        sc_idx = seq.index(StepType.SELF_CHECK)
        vs_idx = seq.index(StepType.VERIFY_SPEC)
        assert test_idx < sc_idx < vs_idx

    def test_bugfix_includes_self_check(self):
        seq = get_default_step_sequence("bugfix")
        assert StepType.SELF_CHECK in seq

    def test_bugfix_self_check_between_test_and_verify(self):
        seq = get_default_step_sequence("bugfix")
        test_idx = seq.index(StepType.TEST)
        sc_idx = seq.index(StepType.SELF_CHECK)
        vs_idx = seq.index(StepType.VERIFY_SPEC)
        assert test_idx < sc_idx < vs_idx

    def test_discovery_includes_self_check(self):
        seq = get_default_step_sequence("discovery")
        assert StepType.SELF_CHECK in seq

    def test_discovery_self_check_between_test_and_verify(self):
        seq = get_default_step_sequence("discovery")
        test_idx = seq.index(StepType.TEST)
        sc_idx = seq.index(StepType.SELF_CHECK)
        vs_idx = seq.index(StepType.VERIFY_SPEC)
        assert test_idx < sc_idx < vs_idx

    def test_small_does_not_include_self_check(self):
        seq = get_default_step_sequence("small")
        assert StepType.SELF_CHECK not in seq

    def test_directive_does_not_include_self_check(self):
        seq = get_default_step_sequence("directive")
        assert StepType.SELF_CHECK not in seq

    def test_review_does_not_include_self_check(self):
        seq = get_default_step_sequence("review")
        assert StepType.SELF_CHECK not in seq


# ---------------------------------------------------------------------------
# 6. Handler registration
# ---------------------------------------------------------------------------


class TestHandlerRegistration:
    """Verify SELF_CHECK handler is registered in STEP_HANDLERS."""

    def test_self_check_in_step_handlers(self):
        from se3.engine.steps import STEP_HANDLERS

        assert StepType.SELF_CHECK in STEP_HANDLERS

    def test_self_check_handler_is_callable(self):
        from se3.engine.steps import STEP_HANDLERS

        handler = STEP_HANDLERS[StepType.SELF_CHECK]
        assert callable(handler)

    def test_self_check_handler_is_correct_function(self):
        from se3.engine.steps import STEP_HANDLERS
        from se3.engine.steps.self_check import self_check_handler

        assert STEP_HANDLERS[StepType.SELF_CHECK] is self_check_handler
