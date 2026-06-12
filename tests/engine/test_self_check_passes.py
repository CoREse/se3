"""Integration tests for configurable self_check N-pass requirement.

Verifies:
- Default N=1 behaves like baseline (no extra instances)
- N=3 creates 3 self_check instances on 3 consecutive COMPLETED
- Early failure short-circuit (REVISION_NEEDED stops creating repeats)
- Pass index and flags are correctly injected into step inputs
- Convergence gate: default off means prev issues don't cause COMPLETED shortcut
- Convergence gate: on means same issues across rounds trigger converged=True
- Intra-round: pass #2+ never sees prev_self_check_issues even when convergence on
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from se3.config import WorkflowConfig
from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.state_machine import StateMachine
from se3.engine.steps.self_check import self_check_handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state_machine(tmp_path, workflow_cfg=None):
    """Create a StateMachine with optional WorkflowConfig override."""
    cfg = workflow_cfg or WorkflowConfig()
    with patch("se3.engine.state_machine.PersistenceManager"):
        sm = StateMachine(project_root=tmp_path)
    # Patch _get_workflow_config so the override survives across method calls
    sm._get_workflow_config = lambda **kwargs: cfg
    return sm


def _make_flow(
    tmp_path,
    selected_steps=None,
    task_type="feature",
    task_description="Implement feature X",
):
    flow = FlowInstance(
        flow_id="test-npass-flow",
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
# 1. Default N=1 — baseline unchanged
# ---------------------------------------------------------------------------


class TestDefaultNPassUnchanged:
    """N=1 (default) must behave exactly like the pre-feature baseline."""

    @pytest.fixture
    def sm(self, tmp_path):
        return _make_state_machine(tmp_path)

    @pytest.fixture
    def flow_with_self_check_completed(self, tmp_path):
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
            outputs={"files_changed": ["src/feature.py"], "summary": "Added feature"},
        )
        _add_step(
            flow,
            StepType.TEST,
            StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True, "overall_passed": True}},
        )
        sc_step = _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.COMPLETED,
            outputs={"issues": [], "actionable_count": 0},
        )
        flow.state.current_step_id = sc_step.step_id
        return flow, sc_step

    def test_single_self_check_completes_to_verify_spec(self, sm, flow_with_self_check_completed):
        """With N=1, one COMPLETED self_check transitions directly to verify_spec."""
        flow, sc_step = flow_with_self_check_completed

        next_step = sm.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.VERIFY_SPEC
        # Only one self_check in history
        sc_history = [
            sid for sid in flow.state.step_history
            if flow.state.steps[sid].step_type == StepType.SELF_CHECK
        ]
        assert len(sc_history) == 1

    def test_no_repeat_step_created(self, sm, flow_with_self_check_completed):
        flow, _ = flow_with_self_check_completed
        sm.transition_to_next(flow)

        sc_steps = [
            sid for sid in flow.state.step_history
            if flow.state.steps[sid].step_type == StepType.SELF_CHECK
        ]
        assert len(sc_steps) == 1


# ---------------------------------------------------------------------------
# 2. N=3 — three consecutive COMPLETED creates 3 instances
# ---------------------------------------------------------------------------


class TestNPassesRequiredThreeAllPass:
    """N=3 with all three COMPLETED → 3 self_check instances → verify_spec."""

    @pytest.fixture
    def sm(self, tmp_path):
        return _make_state_machine(
            tmp_path, WorkflowConfig(self_check_passes_required=3)
        )

    @pytest.fixture
    def flow_ready_for_self_check(self, tmp_path):
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
            outputs={"files_changed": ["src/a.py"]},
        )
        _add_step(
            flow,
            StepType.TEST,
            StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )
        return flow

    def test_three_passes_then_verify_spec(self, sm, flow_ready_for_self_check):
        """Three COMPLETED self_checks, then the 4th transition goes to verify_spec."""
        flow = flow_ready_for_self_check

        # Create initial self_check #1
        sc1 = _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.COMPLETED,
            outputs={"issues": [], "actionable_count": 0},
        )
        flow.state.current_step_id = sc1.step_id

        # Transition 1: should create repeat #2
        sc2 = sm.transition_to_next(flow)
        assert sc2 is not None
        assert sc2.step_type == StepType.SELF_CHECK
        assert sc2.step_id != sc1.step_id
        # Simulate completion
        sc2.status = StepStatus.COMPLETED
        sc2.outputs = {"issues": [], "actionable_count": 0}
        flow.state.current_step_id = sc2.step_id

        # Transition 2: should create repeat #3
        sc3 = sm.transition_to_next(flow)
        assert sc3 is not None
        assert sc3.step_type == StepType.SELF_CHECK
        assert sc3.step_id not in (sc1.step_id, sc2.step_id)
        # Simulate completion
        sc3.status = StepStatus.COMPLETED
        sc3.outputs = {"issues": [], "actionable_count": 0}
        flow.state.current_step_id = sc3.step_id

        # Transition 3: all 3 passes done → verify_spec
        next_step = sm.transition_to_next(flow)
        assert next_step is not None
        assert next_step.step_type == StepType.VERIFY_SPEC

        # History should have exactly 3 self_check steps
        sc_steps = [
            sid for sid in flow.state.step_history
            if flow.state.steps[sid].step_type == StepType.SELF_CHECK
        ]
        assert len(sc_steps) == 3

    def test_step_history_increments_correctly(self, sm, flow_ready_for_self_check):
        """Each repeat step gets a new sequential step_id."""
        flow = flow_ready_for_self_check

        sc1 = _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.COMPLETED,
            outputs={"issues": []},
        )
        flow.state.current_step_id = sc1.step_id

        sc2 = sm.transition_to_next(flow)
        sc2.status = StepStatus.COMPLETED
        flow.state.current_step_id = sc2.step_id

        sc3 = sm.transition_to_next(flow)
        sc3.status = StepStatus.COMPLETED
        flow.state.current_step_id = sc3.step_id

        # step_ids should follow the NN_ format and be in order
        sc_steps = [
            flow.state.steps[sid]
            for sid in flow.state.step_history
            if flow.state.steps[sid].step_type == StepType.SELF_CHECK
        ]
        assert len(sc_steps) == 3
        # step_ids have format "NN_self_check_uuid8" — check NN increments
        seq_nums = [int(s.step_id.split("_")[0]) for s in sc_steps]
        assert seq_nums == sorted(seq_nums)
        assert len(set(seq_nums)) == 3  # all distinct

    def test_current_step_index_does_not_advance_during_repeats(self, sm, flow_ready_for_self_check):
        """During repeat passes, current_step_index stays at the SELF_CHECK slot."""
        flow = flow_ready_for_self_check
        sc_idx = flow.state.selected_steps.index(StepType.SELF_CHECK)

        sc1 = _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.COMPLETED,
            outputs={"issues": []},
        )
        flow.state.current_step_id = sc1.step_id
        flow.state.current_step_index = sc_idx

        sc2 = sm.transition_to_next(flow)
        assert flow.state.current_step_index == sc_idx
        sc2.status = StepStatus.COMPLETED
        flow.state.current_step_id = sc2.step_id

        sc3 = sm.transition_to_next(flow)
        assert flow.state.current_step_index == sc_idx


# ---------------------------------------------------------------------------
# 3. Early failure short-circuit
# ---------------------------------------------------------------------------


class TestShortCircuitOnFailure:
    """Any REVISION_NEEDED during N passes aborts the remaining passes."""

    @pytest.fixture
    def sm(self, tmp_path):
        return _make_state_machine(
            tmp_path, WorkflowConfig(self_check_passes_required=3)
        )

    @pytest.fixture
    def flow_with_implement(self, tmp_path):
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
        impl = _add_step(
            flow,
            StepType.IMPLEMENT,
            StepStatus.COMPLETED,
            outputs={"files_changed": ["src/a.py"]},
        )
        _add_step(
            flow,
            StepType.TEST,
            StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )
        return flow, impl

    def test_short_circuit_on_first_failure(self, sm, flow_with_implement):
        """N=3, #1 REVISION_NEEDED → no #2/#3, enter fix-loop."""
        flow, impl_step = flow_with_implement

        sc1 = _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Fix it",
                "fix_context": {"reason": "self_check", "issues": [{"severity": "high"}]},
            },
        )
        flow.state.current_step_id = sc1.step_id

        next_step = sm.transition_to_next(flow)

        # Should go to fix (IMPLEMENT), not create another self_check
        assert next_step is not None
        assert next_step.step_type == StepType.IMPLEMENT
        assert next_step.step_id == impl_step.step_id

        # Only one self_check in history
        sc_count = sum(
            1 for sid in flow.state.step_history
            if flow.state.steps[sid].step_type == StepType.SELF_CHECK
        )
        assert sc_count == 1

    def test_short_circuit_mid_pass(self, sm, flow_with_implement):
        """N=3, #1 COMPLETED, #2 REVISION_NEEDED → no #3, enter fix-loop."""
        flow, impl_step = flow_with_implement

        sc1 = _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.COMPLETED,
            outputs={"issues": [], "actionable_count": 0},
        )
        flow.state.current_step_id = sc1.step_id

        # Transition creates #2
        sc2 = sm.transition_to_next(flow)
        assert sc2 is not None
        assert sc2.step_type == StepType.SELF_CHECK

        # Simulate #2 finding issues
        sc2.status = StepStatus.REVISION_NEEDED
        sc2.outputs = {
            "fix_needed": True,
            "fix_instructions": "Still broken",
            "fix_context": {"reason": "self_check", "issues": [{"severity": "medium"}]},
        }
        flow.state.current_step_id = sc2.step_id

        next_step = sm.transition_to_next(flow)

        # Should go to fix, not create #3
        assert next_step is not None
        assert next_step.step_type == StepType.IMPLEMENT

        sc_count = sum(
            1 for sid in flow.state.step_history
            if flow.state.steps[sid].step_type == StepType.SELF_CHECK
        )
        assert sc_count == 2


# ---------------------------------------------------------------------------
# 4. Inputs: pass_index and flags present
# ---------------------------------------------------------------------------


class TestInputsPassIndexAndFlagsPresent:
    """Verify each self_check Step.inputs carries correct metadata."""

    @pytest.fixture
    def sm(self, tmp_path):
        return _make_state_machine(
            tmp_path,
            WorkflowConfig(
                self_check_passes_required=3,
                self_check_convergence_enabled=False,
            ),
        )

    @pytest.fixture
    def flow_with_history(self, tmp_path):
        flow = _make_flow(tmp_path)
        _add_step(
            flow,
            StepType.ANALYZE,
            StepStatus.COMPLETED,
            outputs={
                "task_type": "feature",
                "spec_content": {"base": "spec"},
            },
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
        return flow

    def test_initial_self_check_has_pass_index_1(self, sm, flow_with_history):
        inputs = sm._build_step_inputs(flow_with_history, StepType.SELF_CHECK)
        assert inputs["self_check_pass_index"] == 1
        assert inputs["self_check_passes_required"] == 3
        assert inputs["self_check_convergence_enabled"] is False

    def test_second_pass_has_pass_index_2(self, sm, flow_with_history):
        # Simulate one completed self_check already in history
        _add_step(
            flow_with_history,
            StepType.SELF_CHECK,
            StepStatus.COMPLETED,
            outputs={"issues": []},
        )
        inputs = sm._build_step_inputs(flow_with_history, StepType.SELF_CHECK)
        assert inputs["self_check_pass_index"] == 2
        assert inputs["self_check_passes_required"] == 3

    def test_third_pass_has_pass_index_3(self, sm, flow_with_history):
        _add_step(
            flow_with_history,
            StepType.SELF_CHECK,
            StepStatus.COMPLETED,
            outputs={"issues": []},
        )
        _add_step(
            flow_with_history,
            StepType.SELF_CHECK,
            StepStatus.COMPLETED,
            outputs={"issues": []},
        )
        inputs = sm._build_step_inputs(flow_with_history, StepType.SELF_CHECK)
        assert inputs["self_check_pass_index"] == 3

    def test_pass_index_resets_after_non_self_check(self, sm, flow_with_history):
        """After a fix-loop (which introduces TEST/IMPLEMENT), pass_index resets to 1."""
        _add_step(
            flow_with_history,
            StepType.SELF_CHECK,
            StepStatus.REVISION_NEEDED,
            outputs={"issues": [{"severity": "high"}]},
        )
        _add_step(
            flow_with_history,
            StepType.IMPLEMENT,
            StepStatus.COMPLETED,
            outputs={"files_changed": ["a.py"]},
        )
        _add_step(
            flow_with_history,
            StepType.TEST,
            StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )
        inputs = sm._build_step_inputs(flow_with_history, StepType.SELF_CHECK)
        assert inputs["self_check_pass_index"] == 1

    def test_pass_index_resets_after_revision_needed_self_check(self, sm, flow_with_history):
        """A REVISION_NEEDED self_check breaks the streak, next pass_index is 1."""
        _add_step(
            flow_with_history,
            StepType.SELF_CHECK,
            StepStatus.COMPLETED,
            outputs={"issues": []},
        )
        _add_step(
            flow_with_history,
            StepType.SELF_CHECK,
            StepStatus.REVISION_NEEDED,
            outputs={"issues": [{"severity": "high"}]},
        )
        # After fix loop, history has COMPLETED → REVISION_NEEDED self_checks
        # Next self_check should be pass_index=1 because the streak is broken
        _add_step(
            flow_with_history,
            StepType.IMPLEMENT,
            StepStatus.COMPLETED,
            outputs={"files_changed": ["a.py"]},
        )
        _add_step(
            flow_with_history,
            StepType.TEST,
            StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )
        inputs = sm._build_step_inputs(flow_with_history, StepType.SELF_CHECK)
        assert inputs["self_check_pass_index"] == 1


# ---------------------------------------------------------------------------
# 5. Convergence gate: default off
# ---------------------------------------------------------------------------


class TestConvergenceGateDefaultOff:
    """With convergence_enabled=False (default), _issues_converged is NOT called."""

    @pytest.fixture
    def sm(self, tmp_path):
        return _make_state_machine(
            tmp_path,
            WorkflowConfig(self_check_convergence_enabled=False),
        )

    def test_convergence_disabled_same_issues_still_revision_needed(self, sm, tmp_path):
        """Even with identical issues across rounds, REVISION_NEEDED is returned
        when ``self_check_convergence_enabled`` is False."""
        from se3.engine.steps.self_check import self_check_handler

        valid_issue = {
            "severity": "high",
            "actual_behavior": "missing null check",
            "expected_behavior": "validates input",
            "divergence": "crashes on None input",
            "expectation_source": {
                "type": "task_description",
                "verbatim_quote": "convergence gate test",
            },
            "evidence_lines": ["a.py:1"],
            "missing_in": [],
            "out_of_scope": False,
        }

        flow = _make_flow(tmp_path)
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "convergence gate test",
                "changes_made": {"files_changed": [{"path": "a.py", "action": "modify"}]},
                "test_results": {},
                "spec_content": {},
                "self_check_convergence_enabled": False,
                "self_check_pass_index": 1,
                "self_check_passes_required": 1,
                # Same issues as "previous" — without the gate this would converge
                "prev_self_check_issues": [valid_issue],
            },
        )

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_caller_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = json.dumps({
                "issues": [valid_issue],
                "summary": "same issue",
            })
            mock_caller_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        # Should be REVISION_NEEDED, not COMPLETED (convergence is off)
        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs.get("converged") is None


# ---------------------------------------------------------------------------
# 6. Convergence gate: explicit on
# ---------------------------------------------------------------------------


class TestConvergenceGateEnabled:
    """With convergence_enabled=True, identical issues across rounds trigger COMPLETED."""

    @pytest.fixture
    def sm(self, tmp_path):
        return _make_state_machine(
            tmp_path,
            WorkflowConfig(self_check_convergence_enabled=True),
        )

    def test_convergence_enabled_same_issues_returns_completed(self, sm, tmp_path):
        from se3.engine.steps.self_check import self_check_handler

        valid_issue = {
            "severity": "high",
            "actual_behavior": "missing null check",
            "expected_behavior": "validates input",
            "divergence": "crashes on None input",
            "expectation_source": {
                "type": "task_description",
                "verbatim_quote": "convergence gate test",
            },
            "evidence_lines": ["a.py:1"],
            "missing_in": [],
            "out_of_scope": False,
        }

        flow = _make_flow(tmp_path)
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "convergence gate test",
                "changes_made": {"files_changed": [{"path": "a.py", "action": "modify"}]},
                "test_results": {},
                "spec_content": {},
                "self_check_convergence_enabled": True,
                "self_check_pass_index": 1,
                "self_check_passes_required": 1,
                "prev_self_check_issues": [valid_issue],
            },
        )

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_caller_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = json.dumps({
                "issues": [valid_issue],
                "summary": "same issue",
            })
            mock_caller_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs.get("converged") is True


# ---------------------------------------------------------------------------
# 7. Intra-round: pass #2+ never sees prev issues even when convergence on
# ---------------------------------------------------------------------------


class TestIntraRoundNoPrevIssues:
    """When convergence is enabled, prev_self_check_issues is only injected for pass_index==1."""

    @pytest.fixture
    def sm(self, tmp_path):
        return _make_state_machine(
            tmp_path,
            WorkflowConfig(
                self_check_convergence_enabled=True,
                self_check_passes_required=2,
            ),
        )

    def test_pass_1_gets_prev_issues_when_convergence_on(self, sm, tmp_path):
        flow = _make_flow(tmp_path)
        flow.state.increment_fix_iteration(fix_context={"reason": "self_check"})
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
        _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.REVISION_NEEDED,
            outputs={"issues": [{"severity": "high", "description": "D", "location": "f.py:1"}]},
        )
        # After fix loop: implement → test, now building self_check #1
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
        # No completed self_check at tail yet → pass_index should be 1
        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)
        assert inputs["self_check_pass_index"] == 1
        assert "prev_self_check_issues" in inputs
        assert len(inputs["prev_self_check_issues"]) == 1

    def test_pass_2_no_prev_issues_even_when_convergence_on(self, sm, tmp_path):
        flow = _make_flow(tmp_path)
        flow.state.increment_fix_iteration(fix_context={"reason": "self_check"})
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
        _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.REVISION_NEEDED,
            outputs={"issues": [{"severity": "high", "description": "D", "location": "f.py:1"}]},
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
        # Simulate pass #1 completed
        _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.COMPLETED,
            outputs={"issues": []},
        )
        # Now building pass #2
        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)
        assert inputs["self_check_pass_index"] == 2
        # prev_self_check_issues must NOT be present for pass >= 2
        assert "prev_self_check_issues" not in inputs


# ---------------------------------------------------------------------------
# 8. Handler outputs: pass_index and passes_required written back
# ---------------------------------------------------------------------------


class TestPassIndexInOutputs:
    """Verify that self_check_pass_index and self_check_passes_required
    are written to step.outputs so history renderers can read them."""

    @pytest.fixture
    def flow(self, tmp_path):
        return FlowInstance(
            flow_id="test-flow-out",
            task_description="Implement feature X",
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "test-change",
        )

    def _make_step(self, pass_index=1, passes_required=1, **extra_inputs) -> Step:
        # Substantive task_description + matching changes_made path so any
        # new-schema test issues survive ``_validate_and_filter_issues``.
        inputs = {
            "task_description": "Pass-index propagation regression test",
            "changes_made": {
                "files_changed": [{"path": "a.py", "action": "modify"}],
            },
            "test_results": {"passed": True, "returncode": 0},
            "spec_content": {},
            "self_check_pass_index": pass_index,
            "self_check_passes_required": passes_required,
        }
        inputs.update(extra_inputs)
        return Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=inputs,
        )

    def test_pass_index_and_required_in_outputs_no_issues(self, flow):
        """When self_check passes cleanly, outputs must carry pass metadata."""
        step = self._make_step(pass_index=2, passes_required=3)
        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = '{"issues": [], "summary": "OK"}'
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["self_check_pass_index"] == 2
        assert step.outputs["self_check_passes_required"] == 3

    def test_pass_index_and_required_in_outputs_with_issues(self, flow):
        """When issues are found, outputs must still carry pass metadata."""
        step = self._make_step(
            pass_index=2, passes_required=3,
            fix_iteration=1, max_fix_iterations=10,
        )
        valid_issue = {
            "severity": "medium",
            "actual_behavior": "broken behavior",
            "expected_behavior": "correct behavior",
            "divergence": "concrete failure",
            "expectation_source": {
                "type": "task_description",
                "verbatim_quote": "Pass-index propagation regression test",
            },
            "evidence_lines": ["a.py:1"],
            "missing_in": [],
            "out_of_scope": False,
        }
        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = json.dumps({
                "issues": [valid_issue],
                "summary": "Issues found",
            })
            mock_cls.return_value = mock_caller

            result = self_check_handler(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["self_check_pass_index"] == 2
        assert step.outputs["self_check_passes_required"] == 3

    def test_pass_index_defaults_when_not_in_inputs(self, flow):
        """When pass_index / passes_required are absent from inputs,
        defaults (1 / 1) must be written to outputs."""
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "test",
                "changes_made": {},
                "test_results": {"passed": True, "returncode": 0},
                "spec_content": {},
            },
        )
        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = '{"issues": [], "summary": "OK"}'
            mock_cls.return_value = mock_caller

            self_check_handler(step, flow)

        assert step.outputs["self_check_pass_index"] == 1
        assert step.outputs["self_check_passes_required"] == 1


# ---------------------------------------------------------------------------
# 9. CONFIRM + N-pass interaction
# ---------------------------------------------------------------------------


class TestNPassWithConfirm:
    """CONFIRM steps must not break the self_check pass streak counter."""

    @pytest.fixture
    def sm(self, tmp_path):
        return _make_state_machine(
            tmp_path, WorkflowConfig(self_check_passes_required=3)
        )

    def test_confirm_skipped_when_counting_consecutive_passes(self, sm, tmp_path):
        """A CONFIRM step in step_history does not reset the pass counter."""
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
            flow, StepType.IMPLEMENT, StepStatus.COMPLETED,
            outputs={"files_changed": ["a.py"]},
        )
        _add_step(
            flow, StepType.TEST, StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )
        sc1 = _add_step(
            flow, StepType.SELF_CHECK, StepStatus.COMPLETED, outputs={"issues": []}
        )
        sc2 = _add_step(
            flow, StepType.SELF_CHECK, StepStatus.COMPLETED, outputs={"issues": []}
        )
        sc3 = _add_step(
            flow, StepType.SELF_CHECK, StepStatus.COMPLETED, outputs={"issues": []}
        )

        # Insert a CONFIRM step in history (as would happen when confirmation
        # is configured for the self_check step)
        _add_step(
            flow, StepType.CONFIRM, StepStatus.COMPLETED,
            outputs={
                "review_result": {
                    "approved": False,
                    "step_to_review_id": sc3.step_id,
                }
            },
        )

        # Simulate revision: sc3 was reset to PENDING and then re-completed.
        # For this test transition_to_next only needs to see the final COMPLETED.
        sc3.status = StepStatus.COMPLETED
        flow.state.current_step_id = sc3.step_id
        flow.state.current_step_index = flow.state.selected_steps.index(
            StepType.SELF_CHECK
        )

        next_step = sm.transition_to_next(flow)

        # If CONFIRM were not skipped, count would be 0 and a fresh self_check
        # round would start. Instead we should advance to VERIFY_SPEC.
        assert next_step is not None
        assert next_step.step_type == StepType.VERIFY_SPEC

    def test_confirm_in_selected_steps_advances_not_repeats(self, sm, tmp_path):
        """With CONFIRM in selected_steps, revision+re-completion advances, not repeats."""
        flow = _make_flow(
            tmp_path,
            selected_steps=[
                StepType.IMPLEMENT,
                StepType.TEST,
                StepType.SELF_CHECK,
                StepType.CONFIRM,
                StepType.VERIFY_SPEC,
                StepType.COMMIT,
            ],
        )
        _add_step(
            flow, StepType.IMPLEMENT, StepStatus.COMPLETED,
            outputs={"files_changed": ["a.py"]},
        )
        _add_step(
            flow, StepType.TEST, StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )
        sc1 = _add_step(
            flow, StepType.SELF_CHECK, StepStatus.COMPLETED, outputs={"issues": []}
        )
        sc2 = _add_step(
            flow, StepType.SELF_CHECK, StepStatus.COMPLETED, outputs={"issues": []}
        )
        sc3 = _add_step(
            flow, StepType.SELF_CHECK, StepStatus.COMPLETED, outputs={"issues": []}
        )
        _add_step(
            flow, StepType.CONFIRM, StepStatus.COMPLETED,
            outputs={
                "review_result": {
                    "approved": False,
                    "step_to_review_id": sc3.step_id,
                }
            },
        )

        # Simulate revision: sc3 was reset to PENDING and then re-completed.
        # For this test transition_to_next only needs to see the final COMPLETED.
        sc3.status = StepStatus.COMPLETED
        flow.state.current_step_id = sc3.step_id
        flow.state.current_step_index = flow.state.selected_steps.index(
            StepType.SELF_CHECK
        )

        next_step = sm.transition_to_next(flow)

        # Must NOT start a fresh round of N self_checks
        assert next_step is not None
        assert next_step.step_type != StepType.SELF_CHECK
        # With CONFIRM in selected_steps, next is CONFIRM
        assert next_step.step_type == StepType.CONFIRM


# ---------------------------------------------------------------------------
# 10. Backward compatibility: no workflow config
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Flows without workflow config should behave identically to before the feature."""

    def test_no_config_defaults_to_single_pass_no_convergence(self, tmp_path):
        with patch("se3.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=tmp_path)
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
            outputs={"files_changed": ["a.py"]},
        )
        _add_step(
            flow,
            StepType.TEST,
            StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )
        sc = _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.COMPLETED,
            outputs={"issues": []},
        )
        flow.state.current_step_id = sc.step_id

        next_step = sm.transition_to_next(flow)
        assert next_step is not None
        assert next_step.step_type == StepType.VERIFY_SPEC

    def test_inputs_default_flags(self, tmp_path):
        with patch("se3.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=tmp_path)
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
            outputs={"test_results": {"passed": True}},
        )
        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)
        assert inputs["self_check_pass_index"] == 1
        assert inputs["self_check_passes_required"] == 1
        assert inputs["self_check_convergence_enabled"] is False


# ---------------------------------------------------------------------------
# 11. Resume mid-N-pass: save_flow -> load_flow preserves pass position
# ---------------------------------------------------------------------------


class TestResumeMidNPass:
    """Interrupting during pass #2 of N=3 and resuming must continue correctly."""

    def test_resume_mid_pass_preserves_pass_index(self, tmp_path):
        """After save/load between passes, the resumed flow continues from #2 to #3."""
        # Use a real PersistenceManager (not mocked) so save/load round-trips
        sm = StateMachine(project_root=tmp_path)
        # Override _get_workflow_config after real init
        sm._get_workflow_config = lambda **kwargs: WorkflowConfig(
            self_check_passes_required=3
        )

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
            outputs={"files_changed": ["src/a.py"]},
        )
        _add_step(
            flow,
            StepType.TEST,
            StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )

        # Pass #1 COMPLETED
        sc1 = _add_step(
            flow,
            StepType.SELF_CHECK,
            StepStatus.COMPLETED,
            outputs={"issues": [], "actionable_count": 0},
        )
        flow.state.current_step_id = sc1.step_id
        sc_idx = flow.state.selected_steps.index(StepType.SELF_CHECK)
        flow.state.current_step_index = sc_idx

        # Transition creates pass #2 (PENDING)
        sc2 = sm.transition_to_next(flow)
        assert sc2 is not None
        assert sc2.step_type == StepType.SELF_CHECK
        assert sc2.inputs["self_check_pass_index"] == 2
        assert sc2.inputs["self_check_passes_required"] == 3

        # Simulate an interrupt: persist the flow with sc2 as PENDING
        sm.persistence.save_flow(flow)

        # Load the flow back (as if --resume)
        loaded_flow = sm.persistence.load_flow()
        assert loaded_flow is not None
        assert loaded_flow.state.current_step_id == sc2.step_id

        # Verify the loaded step still has the correct pass metadata
        loaded_sc2 = loaded_flow.state.steps[sc2.step_id]
        assert loaded_sc2.step_type == StepType.SELF_CHECK
        assert loaded_sc2.status == StepStatus.PENDING
        assert loaded_sc2.inputs["self_check_pass_index"] == 2
        assert loaded_sc2.inputs["self_check_passes_required"] == 3

        # Simulate pass #2 completing
        loaded_sc2.status = StepStatus.COMPLETED
        loaded_sc2.outputs = {"issues": [], "actionable_count": 0}
        loaded_flow.state.current_step_id = loaded_sc2.step_id

        # Transition creates pass #3
        sc3 = sm.transition_to_next(loaded_flow)
        assert sc3 is not None
        assert sc3.step_type == StepType.SELF_CHECK
        assert sc3.inputs["self_check_pass_index"] == 3

        # Simulate pass #3 completing
        sc3.status = StepStatus.COMPLETED
        sc3.outputs = {"issues": [], "actionable_count": 0}
        loaded_flow.state.current_step_id = sc3.step_id

        # Next transition goes to verify_spec (all 3 passes done)
        next_step = sm.transition_to_next(loaded_flow)
        assert next_step is not None
        assert next_step.step_type == StepType.VERIFY_SPEC


# ---------------------------------------------------------------------------
# 12. Effective pass count derived from nested self_check chains
# ---------------------------------------------------------------------------


_AGENTS_YAML = """agents:
  a: {cmd: claude-a}
  b: {cmd: claude-b}
  c: {cmd: claude-c}
"""


def _real_state_machine(tmp_path):
    """A StateMachine reading real config from tmp_path (no _get_workflow_config
    override). Global ~/.se3/config.yaml is isolated via Path.home patching by
    the caller."""
    with patch("se3.engine.state_machine.PersistenceManager"):
        return StateMachine(project_root=tmp_path)


class TestEffectivePassCountFromNestedChains:
    def test_nested_without_explicit_passes_uses_chain_count(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(_AGENTS_YAML + """llm_caller:
  steps:
    self_check:
      - [a]
      - [b, c]
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            sm = _real_state_machine(tmp_path)
            # Two declared chains, no explicit self_check_passes_required.
            assert sm._get_self_check_passes_required() == 2

    def test_explicit_greater_than_chains_keeps_explicit(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(_AGENTS_YAML + """workflow:
  self_check_passes_required: 4
llm_caller:
  steps:
    self_check:
      - [a]
      - [b]
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            sm = _real_state_machine(tmp_path)
            assert sm._get_self_check_passes_required() == 4

    def test_explicit_less_than_chains_warns_and_uses_explicit(self, tmp_path, caplog):
        import logging
        (tmp_path / "se3.yaml").write_text(_AGENTS_YAML + """workflow:
  self_check_passes_required: 1
llm_caller:
  steps:
    self_check:
      - [a]
      - [b]
      - [c]
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            sm = _real_state_machine(tmp_path)
            with caplog.at_level(logging.WARNING, logger="se3.engine.state_machine"):
                passes = sm._get_self_check_passes_required()

        assert passes == 1
        assert any(
            "smaller than" in rec.message and "self_check chains" in rec.message
            for rec in caplog.records
        )

    def test_flat_self_check_uses_explicit_or_default(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(_AGENTS_YAML + """llm_caller:
  steps:
    self_check: [a, b]
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            sm = _real_state_machine(tmp_path)
            # Flat list → not nested → default 1.
            assert sm._get_self_check_passes_required() == 1

    def test_no_self_check_override_uses_default(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(_AGENTS_YAML)
        with patch("se3.config.Path.home", return_value=tmp_path):
            sm = _real_state_machine(tmp_path)
            assert sm._get_self_check_passes_required() == 1

    def test_build_inputs_reports_derived_passes_required(self, tmp_path):
        (tmp_path / "se3.yaml").write_text(_AGENTS_YAML + """llm_caller:
  steps:
    self_check:
      - [a]
      - [b]
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            sm = _real_state_machine(tmp_path)
            flow = _make_flow(tmp_path)
            _add_step(
                flow, StepType.IMPLEMENT, StepStatus.COMPLETED,
                outputs={"files_changed": ["a.py"]},
            )
            _add_step(
                flow, StepType.TEST, StepStatus.COMPLETED,
                outputs={"test_results": {"passed": True}},
            )
            inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)
        assert inputs["self_check_pass_index"] == 1
        assert inputs["self_check_passes_required"] == 2


# ---------------------------------------------------------------------------
# 13. LLMCaller selects the per-pass chain via self_check_pass_index
# ---------------------------------------------------------------------------


class TestLLMCallerSelectsPassChain:
    def test_pass_index_selects_nested_chain(self, tmp_path):
        from se3.engine.llm_caller import LLMCaller

        (tmp_path / "se3.yaml").write_text(_AGENTS_YAML + """llm_caller:
  steps:
    self_check:
      - [a]
      - [b, c]
""")
        with patch("se3.config.Path.home", return_value=tmp_path):
            caller1 = LLMCaller(
                project_root=tmp_path, step_type="self_check",
                self_check_pass_index=1,
            )
            caller2 = LLMCaller(
                project_root=tmp_path, step_type="self_check",
                self_check_pass_index=2,
            )

        assert [a["name"] for a in caller1._agents] == ["a"]
        assert [a["name"] for a in caller2._agents] == ["b", "c"]

    def test_explicit_agents_argument_wins(self, tmp_path):
        from se3.engine.llm_caller import LLMCaller

        (tmp_path / "se3.yaml").write_text(_AGENTS_YAML + """llm_caller:
  steps:
    self_check:
      - [a]
      - [b]
""")
        explicit = [{"name": "x", "type": "claude-code", "cmd": "cx", "priority": 0}]
        with patch("se3.config.Path.home", return_value=tmp_path):
            caller = LLMCaller(
                project_root=tmp_path, step_type="self_check",
                self_check_pass_index=2, agents=explicit,
            )
        assert [a["name"] for a in caller._agents] == ["x"]
