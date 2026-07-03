"""Tests for the state machine fix loop functionality.

These tests verify the test-verify-fix loop mechanism that automatically
transitions back to the implement step when tests fail.
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from se3.engine.models import (
    FIX_HISTORY_MAX_ENTRIES,
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from se3.config import DEFAULT_MAX_FIX_ITERATIONS, ConfigError, WorkflowConfig
from se3.engine.state_machine import StateMachine


class TestFixIterationTracking:
    """Test cases for fix iteration tracking in State class."""

    def test_increment_fix_iteration_initial(self):
        """Test that increment_fix_iteration starts at 1."""
        state = State()
        assert state.get_fix_iteration() == 0

        iteration = state.increment_fix_iteration()

        assert iteration == 1
        assert state.get_fix_iteration() == 1
        assert state.context["fix_iterations"] == 1

    def test_increment_fix_iteration_multiple(self):
        """Test that increment_fix_iteration increments correctly."""
        state = State()

        assert state.increment_fix_iteration() == 1
        assert state.increment_fix_iteration() == 2
        assert state.increment_fix_iteration() == 3
        assert state.get_fix_iteration() == 3

    def test_increment_fix_iteration_tracks_history(self):
        """Test that increment_fix_iteration tracks history."""
        state = State()

        state.increment_fix_iteration(fix_context={"reason": "test_failure"})
        state.increment_fix_iteration(fix_context={"reason": "spec_compliance"})

        assert len(state.fix_history) == 2
        assert state.fix_history[0]["iteration"] == 1
        assert state.fix_history[0]["reason"] == "test_failure"
        assert state.fix_history[1]["iteration"] == 2
        assert state.fix_history[1]["reason"] == "spec_compliance"

    def test_fix_history_capped_at_max_entries(self):
        """fix_history must be capped to a sliding window so that an
        unbounded run (max_fix_iterations=0) cannot inflate memory /
        engine.json size linearly with iteration count. The cap retains
        the most recent entries because every consumer cares about recency.
        """
        state = State()

        for i in range(FIX_HISTORY_MAX_ENTRIES + 25):
            state.increment_fix_iteration(fix_context={"reason": f"r{i}"})

        # Counter keeps growing; only the stored history is bounded
        assert state.fix_iterations == FIX_HISTORY_MAX_ENTRIES + 25
        assert len(state.fix_history) == FIX_HISTORY_MAX_ENTRIES
        # Sliding window keeps the most recent entries
        assert state.fix_history[-1]["iteration"] == FIX_HISTORY_MAX_ENTRIES + 25
        assert state.fix_history[0]["iteration"] == 26

    def test_fix_iteration_serialization(self):
        """Test that fix iteration data serializes correctly."""
        state = State()
        state.increment_fix_iteration(fix_context={"test": "data"})

        data = state.to_dict()

        assert data["fix_iterations"] == 1
        assert len(data["fix_history"]) == 1
        assert data["fix_history"][0]["iteration"] == 1

    def test_fix_iteration_deserialization(self):
        """Test that fix iteration data deserializes correctly."""
        data = {
            "current_step_id": None,
            "step_history": [],
            "steps": {},
            "context": {},
            "selected_steps": [],
            "current_step_index": 0,
            "review_iterations": {},
            "fix_iterations": 2,
            "fix_history": [
                {"iteration": 1, "reason": "test"},
                {"iteration": 2, "reason": "test2"},
            ],
        }

        state = State.from_dict(data)

        assert state.fix_iterations == 2
        assert state.get_fix_iteration() == 2
        assert len(state.fix_history) == 2


class TestTransitionToFix:
    """Test cases for the _transition_to_fix method."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine."""
        return StateMachine(project_root=tmp_path)

    @pytest.fixture
    def flow_with_implement(self, tmp_path):
        """Create a flow with an implement step in history."""
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [
            StepType.ANALYZE,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERIFY_SPEC,
        ]

        # Create and add implement step
        implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            inputs={"task_groups": []},
            outputs={"files_changed": ["test.py"]},
        )
        flow.state.add_step(implement_step)

        # Create and add test step
        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": False}},
        )
        flow.state.add_step(test_step)

        # Create and add verify_spec step
        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Fix the bug",
                "fix_context": {"test_failed": True},
            },
        )
        flow.state.add_step(verify_step)
        flow.state.current_step_id = verify_step.step_id

        return flow, implement_step, verify_step

    def test_transition_to_fix_finds_implement_step(self, state_machine, flow_with_implement):
        """Test that _transition_to_fix finds the implement step."""
        flow, implement_step, verify_step = flow_with_implement

        result = state_machine._transition_to_fix(flow, verify_step)

        assert result is not None
        assert result.step_id == implement_step.step_id

    def test_transition_to_fix_increments_iteration(self, state_machine, flow_with_implement):
        """Test that _transition_to_fix increments fix iteration."""
        flow, _, verify_step = flow_with_implement

        assert flow.state.get_fix_iteration() == 0

        state_machine._transition_to_fix(flow, verify_step)

        assert flow.state.get_fix_iteration() == 1

    def test_transition_to_fix_sets_inputs(self, state_machine, flow_with_implement):
        """Test that _transition_to_fix sets correct inputs on implement step."""
        flow, implement_step, verify_step = flow_with_implement

        state_machine._transition_to_fix(flow, verify_step)

        assert implement_step.inputs["fix_instructions"] == "Fix the bug"
        assert implement_step.inputs["fix_context"]["test_failed"] is True
        assert implement_step.inputs["is_fix_iteration"] is True
        assert implement_step.inputs["fix_iteration"] == 1

    def test_transition_to_fix_resets_step_status(self, state_machine, flow_with_implement):
        """Test that _transition_to_fix resets the implement step status."""
        flow, implement_step, verify_step = flow_with_implement

        assert implement_step.status == StepStatus.COMPLETED

        state_machine._transition_to_fix(flow, verify_step)

        assert implement_step.status == StepStatus.PENDING

    def test_transition_to_fix_updates_current_step(self, state_machine, flow_with_implement):
        """Test that _transition_to_fix updates flow state to point to implement."""
        flow, implement_step, verify_step = flow_with_implement

        state_machine._transition_to_fix(flow, verify_step)

        assert flow.state.current_step_id == implement_step.step_id

    def test_transition_to_fix_returns_none_when_no_fix_needed(self, state_machine, flow_with_implement):
        """Test that _transition_to_fix returns None when fix_needed is False."""
        flow, _, verify_step = flow_with_implement
        verify_step.outputs["fix_needed"] = False

        result = state_machine._transition_to_fix(flow, verify_step)

        assert result is None

    def test_transition_to_fix_returns_none_when_no_implement_step(self, state_machine, tmp_path):
        """Test that _transition_to_fix returns None when no implement step exists."""
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )

        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Fix it",
            },
        )

        result = state_machine._transition_to_fix(flow, verify_step)

        assert result is None


class TestTransitionToNextWithFixLoop:
    """Test cases for transition_to_next handling REVISION_NEEDED from VERIFY_SPEC."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine."""
        return StateMachine(project_root=tmp_path)

    @pytest.fixture
    def flow_with_verify_revision(self, tmp_path):
        """Create a flow with VERIFY_SPEC in REVISION_NEEDED state."""
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
            outputs={"files_changed": []},
        )
        flow.state.add_step(implement_step)

        # Create and add test step
        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": False}},
        )
        flow.state.add_step(test_step)

        # Create and add verify_spec step with REVISION_NEEDED
        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Fix the bug",
                "fix_context": {"test_failed": True},
            },
        )
        flow.state.add_step(verify_step)
        flow.state.current_step_id = verify_step.step_id

        return flow, implement_step, verify_step

    def test_transition_to_next_calls_transition_to_fix(self, state_machine, flow_with_verify_revision):
        """Test that transition_to_next calls _transition_to_fix for VERIFY_SPEC REVISION_NEEDED."""
        flow, implement_step, _ = flow_with_verify_revision

        next_step = state_machine.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_id == implement_step.step_id
        assert next_step.step_type == StepType.IMPLEMENT

    def test_transition_to_next_respects_max_iterations(self, state_machine, flow_with_verify_revision):
        """Test that transition_to_next respects max fix iterations."""
        flow, _, verify_step = flow_with_verify_revision

        # Set iteration at max
        flow.state.fix_iterations = 3

        with patch.object(state_machine, '_get_max_fix_iterations', return_value=3):
            next_step = state_machine.transition_to_next(flow)

        # Should fail the flow instead of continuing
        assert next_step is None
        assert flow.status == FlowStatus.FAILED

    def test_max_fix_iterations_zero_does_not_fail(self, state_machine, flow_with_verify_revision):
        """max_fix_iterations=0 (sentinel) bypasses exhaustion — flow stays RUNNING.

        Also asserts that ``IssueDiscovery.create_from_fix_loop_exhaustion``
        is never invoked under the unlimited sentinel: a regression that
        moved that call outside the ``> 0`` guard would otherwise produce
        spurious A-class issues in unlimited mode without any test
        catching it.
        """
        flow, implement_step, _ = flow_with_verify_revision

        # Already wildly past any sane upper bound — the sentinel must still allow continuation.
        flow.state.fix_iterations = 200

        # Stub out the IssueDiscovery so we can verify the exhaustion path
        # never fires the A-class issue under the unlimited sentinel.
        mock_discovery = Mock()
        with patch.object(state_machine, '_get_max_fix_iterations', return_value=0), \
             patch.object(state_machine, '_get_issue_discovery', return_value=mock_discovery):
            next_step = state_machine.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_id == implement_step.step_id
        assert next_step.step_type == StepType.IMPLEMENT
        assert flow.status != FlowStatus.FAILED
        mock_discovery.create_from_fix_loop_exhaustion.assert_not_called()

    def test_max_fix_iterations_zero_drives_many_iterations_without_failure(
        self, state_machine, flow_with_verify_revision
    ):
        """Drive ``transition_to_next`` 30 times under sentinel mode and verify
        the flow never naturally terminates from exhaustion.

        Existing tests cover the single-transition guard, prompt rendering,
        and hot-edit cache invalidation, but none drive the state machine
        through many real iterations. A future regression where a non-bypass
        code path inadvertently calls ``flow.status = FlowStatus.FAILED`` on
        iteration count would slip past the single-transition tests; this
        test catches it by exercising the full loop drive.
        """
        flow, implement_step, verify_step = flow_with_verify_revision

        ITERATIONS = 30
        with patch.object(state_machine, '_get_max_fix_iterations', return_value=0):
            for i in range(ITERATIONS):
                # Re-arm the verify_step as the trigger on each pass: the prior
                # _transition_to_fix repointed current_step_id at implement.
                verify_step.status = StepStatus.REVISION_NEEDED
                flow.state.current_step_id = verify_step.step_id

                next_step = state_machine.transition_to_next(flow)

                assert next_step is not None, (
                    f"iteration {i+1}: sentinel mode must always grant another "
                    "fix attempt, got None (flow likely flipped to FAILED)"
                )
                assert next_step.step_id == implement_step.step_id
                assert flow.status == FlowStatus.RUNNING, (
                    f"iteration {i+1}: flow.status must stay RUNNING under "
                    f"the sentinel, got {flow.status}"
                )

        assert flow.state.get_fix_iteration() == ITERATIONS
        assert len(flow.state.fix_history) == ITERATIONS

    def test_max_fix_iterations_negative_defensive_does_not_fail(self, state_machine, flow_with_verify_revision):
        """Belt-and-braces: even if a negative slipped past config validation
        (which rejects negatives fail-fast), the state machine's `> 0` guard
        must not flip the flow to FAILED.
        """
        flow, implement_step, _ = flow_with_verify_revision
        flow.state.fix_iterations = 50

        with patch.object(state_machine, '_get_max_fix_iterations', return_value=-1):
            next_step = state_machine.transition_to_next(flow)

        assert next_step is not None
        assert flow.status != FlowStatus.FAILED

    def test_max_fix_iterations_one_allows_single_attempt(self, state_machine, flow_with_verify_revision):
        """max_fix_iterations=1 — the smallest finite cap — must allow exactly
        one fix attempt before FAILED. Guards against an off-by-one regression
        (e.g. someone changing `>=` to `>`) on the most sensitive boundary.
        """
        flow, implement_step, _ = flow_with_verify_revision

        # Round 1: fresh flow, no fixes yet. The single allowed fix attempt
        # must be granted: 0 < 1, so transition_to_fix runs.
        assert flow.state.get_fix_iteration() == 0
        with patch.object(state_machine, '_get_max_fix_iterations', return_value=1):
            next_step = state_machine.transition_to_next(flow)

        assert next_step is not None, "max=1 must permit the first fix attempt"
        assert next_step.step_id == implement_step.step_id
        assert flow.status != FlowStatus.FAILED
        assert flow.state.get_fix_iteration() == 1

        # Round 2: one fix has been consumed (iteration is now 1). The cap is
        # 1, so 1 >= 1 must trigger FAILED — no second attempt.
        # Reset current step to verify_step REVISION_NEEDED to re-enter the
        # loop branch.
        verify_step = next(
            s for s in flow.state.steps.values()
            if s.step_type == StepType.VERIFY_SPEC
        )
        verify_step.status = StepStatus.REVISION_NEEDED
        flow.state.current_step_id = verify_step.step_id

        with patch.object(state_machine, '_get_max_fix_iterations', return_value=1):
            next_step_after = state_machine.transition_to_next(flow)

        assert next_step_after is None, "max=1 must FAIL after one fix attempt"
        assert flow.status == FlowStatus.FAILED

    def test_transition_to_next_increments_iteration_on_fix(self, state_machine, flow_with_verify_revision):
        """Test that transition_to_next increments fix iteration when transitioning to fix."""
        flow, _, _ = flow_with_verify_revision

        assert flow.state.get_fix_iteration() == 0

        state_machine.transition_to_next(flow)

        assert flow.state.get_fix_iteration() == 1


class TestSentinelModeTriggerStepCoverage:
    """Locks the contract that the unlimited-mode bypass at
    ``state_machine.transition_to_next`` (line ~514: ``if max_fix_iterations
    > 0 and current_iteration >= max_fix_iterations``) applies uniformly to
    all three trigger step types — TEST, SELF_CHECK, and VERIFY_SPEC.

    The sister test class above (``TestTransitionToNextWithFixLoop``)
    exercises sentinel-mode bypass repeatedly, but only via the VERIFY_SPEC
    trigger. A regression that special-cases the guard for one of the three
    step types (e.g. ``if step_type == VERIFY_SPEC and ...``) would slip
    past CI. These tests parametrize across all three trigger types.
    """

    @pytest.fixture
    def state_machine(self, tmp_path):
        return StateMachine(project_root=tmp_path)

    def _build_flow_with_revision(self, trigger_step_type: StepType):
        """Build a fix-loop-ready flow whose current step is in
        REVISION_NEEDED on the requested trigger step type.
        """
        flow = FlowInstance(
            flow_id=f"test-flow-{trigger_step_type.value}",
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
            outputs={"files_changed": []},
        )
        flow.state.add_step(implement_step)
        trigger_step = Step(
            step_type=trigger_step_type,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "fix the issue",
                "fix_context": {"reason": trigger_step_type.value},
            },
        )
        flow.state.add_step(trigger_step)
        flow.state.current_step_id = trigger_step.step_id
        return flow, implement_step, trigger_step

    @pytest.mark.parametrize(
        "trigger_step_type",
        [StepType.TEST, StepType.SELF_CHECK, StepType.VERIFY_SPEC],
    )
    def test_sentinel_zero_bypasses_exhaustion_for_all_triggers(
        self, state_machine, trigger_step_type
    ):
        """max_fix_iterations=0 must bypass exhaustion regardless of which
        of the three trigger step types raised REVISION_NEEDED.
        """
        flow, implement_step, _ = self._build_flow_with_revision(trigger_step_type)
        # Past any sane upper bound — the sentinel must still allow continuation.
        flow.state.fix_iterations = 200

        # This test locks the exhaustion-bypass contract, which is orthogonal to
        # the SELF_CHECK adjudication routing. Disable the periodic adjudication
        # backstop (adjudicate_period=0) so a fix_iterations=200 SELF_CHECK is not
        # diverted to ADJUDICATE — the fix-loop grant we assert here stays on the
        # implement path. (Adjudication routing has its own dedicated tests.)
        mock_discovery = Mock()
        with patch.object(state_machine, "_get_max_fix_iterations", return_value=0), \
             patch.object(state_machine, "_get_workflow_config",
                          return_value=WorkflowConfig(adjudicate_period=0)), \
             patch.object(state_machine, "_get_issue_discovery", return_value=mock_discovery):
            next_step = state_machine.transition_to_next(flow)

        assert next_step is not None, (
            f"sentinel mode must grant a fix attempt for trigger="
            f"{trigger_step_type.value}, got None"
        )
        assert next_step.step_id == implement_step.step_id
        assert flow.status != FlowStatus.FAILED
        mock_discovery.create_from_fix_loop_exhaustion.assert_not_called()

    @pytest.mark.parametrize(
        "trigger_step_type",
        [StepType.TEST, StepType.SELF_CHECK, StepType.VERIFY_SPEC],
    )
    def test_finite_cap_still_fails_for_all_triggers(
        self, state_machine, trigger_step_type
    ):
        """Belt-and-braces companion: with a finite cap that has been
        reached, the flow MUST flip to FAILED for every trigger type. This
        is what the parametrized sentinel test above is contrasting with —
        without it, a regression that always-bypassed the cap would also
        pass the sentinel test trivially.
        """
        flow, _, _ = self._build_flow_with_revision(trigger_step_type)
        flow.state.fix_iterations = 3

        with patch.object(state_machine, "_get_max_fix_iterations", return_value=3), \
             patch.object(state_machine, "_get_issue_discovery", return_value=None):
            next_step = state_machine.transition_to_next(flow)

        assert next_step is None
        assert flow.status == FlowStatus.FAILED


class TestSentinelEndToEndConfigLoad:
    """Integration coverage: drive the TEST-trigger fix loop with the real
    config-load path (no ``_get_max_fix_iterations`` mock) so the
    ``WorkflowConfig.load`` -> state_machine plumbing is locked end-to-end.

    The parametrized sentinel tests above mock ``_get_max_fix_iterations``
    directly, which is sufficient to lock the comparison branch but not the
    upstream parse: a regression in ``WorkflowConfig.from_dict`` that
    silently dropped ``max_fix_iterations: 0`` (e.g. by treating ``0`` as
    falsy and falling back to the default 100) would slip past those tests.
    Drive a real config file under each scenario to catch that.
    """

    def _build_flow_with_test_revision(self):
        flow = FlowInstance(
            flow_id="test-flow-test-trigger",
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
            outputs={"files_changed": []},
        )
        flow.state.add_step(implement_step)
        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "tests failed; fix the bug",
                "fix_context": {"test_failed": True},
            },
        )
        flow.state.add_step(test_step)
        flow.state.current_step_id = test_step.step_id
        return flow, implement_step, test_step

    def test_test_trigger_sentinel_zero_via_real_config(self, tmp_path):
        """``workflow.max_fix_iterations: 0`` in se3.yaml must propagate
        through the real ``WorkflowConfig.load`` path so the TEST trigger
        bypasses exhaustion at iteration 200. No mock of
        ``_get_max_fix_iterations``.
        """
        (tmp_path / "se3.yaml").write_text(
            "workflow:\n  max_fix_iterations: 0\n"
        )
        state_machine = StateMachine(project_root=tmp_path)

        flow, implement_step, _ = self._build_flow_with_test_revision()
        flow.state.fix_iterations = 200

        mock_discovery = Mock()
        with patch.object(state_machine, "_get_issue_discovery", return_value=mock_discovery):
            next_step = state_machine.transition_to_next(flow)

        assert next_step is not None, (
            "TEST trigger under sentinel 0 (loaded from real se3.yaml) must "
            "grant a fix attempt even at iteration 200"
        )
        assert next_step.step_id == implement_step.step_id
        assert flow.status != FlowStatus.FAILED
        mock_discovery.create_from_fix_loop_exhaustion.assert_not_called()

    def test_test_trigger_finite_cap_via_real_config(self, tmp_path):
        """Companion to the sentinel test: with ``max_fix_iterations: 3``
        loaded from real se3.yaml and the flow already at iteration 3, the
        TEST trigger must FAIL — proving the config value is honored, not
        silently overridden.
        """
        (tmp_path / "se3.yaml").write_text(
            "workflow:\n  max_fix_iterations: 3\n"
        )
        state_machine = StateMachine(project_root=tmp_path)

        flow, _, _ = self._build_flow_with_test_revision()
        flow.state.fix_iterations = 3

        with patch.object(state_machine, "_get_issue_discovery", return_value=None):
            next_step = state_machine.transition_to_next(flow)

        assert next_step is None
        assert flow.status == FlowStatus.FAILED


class TestBuildStepInputsWithFixContext:
    """Test cases for _build_step_inputs including fix context."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine."""
        return StateMachine(project_root=tmp_path)

    @pytest.fixture
    def flow_with_fix_history(self, tmp_path):
        """Create a flow with fix history."""
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )

        # Add fix history
        flow.state.increment_fix_iteration(fix_context={"reason": "test_failure"})

        # Create and add test step
        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": False, "stdout": "Error"}},
        )
        flow.state.add_step(test_step)

        # Create and add verify_spec step
        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.COMPLETED,
            outputs={
                "verification_result": {"issues": []},
                "fix_instructions": "Fix it",
                "fix_context": {"test_failed": True},
            },
        )
        flow.state.add_step(verify_step)

        return flow

    def test_build_step_inputs_includes_fix_iteration_for_implement(self, state_machine, flow_with_fix_history):
        """Test that _build_step_inputs includes fix_iteration when building IMPLEMENT inputs."""
        inputs = state_machine._build_step_inputs(flow_with_fix_history, StepType.IMPLEMENT)

        assert inputs["fix_iteration"] == 1
        assert len(inputs["fix_history"]) == 1

    def test_build_step_inputs_includes_test_results_for_implement(self, state_machine, flow_with_fix_history):
        """Test that _build_step_inputs includes test_results when in fix loop."""
        inputs = state_machine._build_step_inputs(flow_with_fix_history, StepType.IMPLEMENT)

        assert "test_results" in inputs
        assert inputs["test_results"]["passed"] is False

    def test_build_step_inputs_no_fix_context_when_iteration_zero(self, state_machine, tmp_path):
        """Test that _build_step_inputs doesn't include fix context when iteration is 0."""
        flow = FlowInstance(
            flow_id="test-flow-123",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )

        inputs = state_machine._build_step_inputs(flow, StepType.IMPLEMENT)

        # Should not have fix-specific keys when not in a fix loop
        assert "fix_iteration" not in inputs
        assert "fix_history" not in inputs


class TestBuildStepInputsSelfCheck:
    """Test _build_step_inputs propagation for SELF_CHECK in fix loop."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        return StateMachine(project_root=tmp_path)

    @pytest.fixture
    def flow_in_fix_loop(self):
        flow = FlowInstance(
            flow_id="test-flow-sc",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        flow.state.increment_fix_iteration(fix_context={"reason": "self_check"})

        impl_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": ["b.py"]},
        )
        flow.state.add_step(impl_step)

        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        )
        flow.state.add_step(test_step)

        prev_sc = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "issues": [{"severity": "high", "description": "Missing null check", "location": "handler.py:42"}],
            },
        )
        flow.state.add_step(prev_sc)

        return flow

    def test_propagates_fix_iteration(self, state_machine, flow_in_fix_loop):
        inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.SELF_CHECK)
        assert inputs["fix_iteration"] == 1

    def test_propagates_fix_history(self, state_machine, flow_in_fix_loop):
        inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.SELF_CHECK)
        assert len(inputs["fix_history"]) == 1
        assert inputs["fix_history"][0]["reason"] == "self_check"

    def test_propagates_max_fix_iterations(self, state_machine, flow_in_fix_loop):
        inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.SELF_CHECK)
        assert "max_fix_iterations" in inputs
        # Pin to exact configured default (no se3.yaml override in fixture).
        assert inputs["max_fix_iterations"] == DEFAULT_MAX_FIX_ITERATIONS

    def test_propagates_prev_self_check_issues_on_fix_iteration_pass_one(self, state_machine, flow_in_fix_loop):
        """``prev_self_check_issues`` is injected unconditionally on
        ``pass_index == 1 and fix_iteration > 0`` regardless of
        ``self_check_convergence_enabled``. The schema-rewrite commit
        relies on prev_issues being available so the LLM can produce the
        ``previous_issue_resolutions`` array; gating on convergence_enabled
        would leave the new contract under-served when the flag is off."""
        inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.SELF_CHECK)
        assert len(inputs["prev_self_check_issues"]) == 1
        assert inputs["prev_self_check_issues"][0]["description"] == "Missing null check"

    def test_propagates_prev_self_check_issues_with_convergence_enabled(self, state_machine, flow_in_fix_loop):
        """Same behavior with convergence_enabled=True (no longer gated)."""
        from se3.config import WorkflowConfig
        with patch.object(WorkflowConfig, 'load', return_value=WorkflowConfig(
            self_check_convergence_enabled=True,
        )):
            inputs = state_machine._build_step_inputs(flow_in_fix_loop, StepType.SELF_CHECK)
        assert len(inputs["prev_self_check_issues"]) == 1
        assert inputs["prev_self_check_issues"][0]["description"] == "Missing null check"

    def test_no_prev_data_when_not_in_fix_loop(self, state_machine):
        flow = FlowInstance(
            flow_id="test-flow-sc2",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        inputs = state_machine._build_step_inputs(flow, StepType.SELF_CHECK)
        assert "prev_self_check_issues" not in inputs
        assert "fix_iteration" not in inputs


class TestPrevInputsDeepCopy:
    """Previous-iteration data passed into inputs must be deep-copied so that
    later mutations on step.outputs cannot corrupt the snapshot the next step sees."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        return StateMachine(project_root=tmp_path)

    def test_self_check_prev_issues_is_deep_copied(self, state_machine):
        from se3.config import WorkflowConfig
        flow = FlowInstance(
            flow_id="test-deepcopy-sc",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        flow.state.increment_fix_iteration(fix_context={"reason": "self_check"})
        flow.state.add_step(Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED, outputs={}))
        prev_sc = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.REVISION_NEEDED,
            outputs={"issues": [{"severity": "high", "description": "D", "location": "f.py:1"}]},
        )
        flow.state.add_step(prev_sc)

        with patch.object(WorkflowConfig, 'load', return_value=WorkflowConfig(
            self_check_convergence_enabled=True,
        )):
            inputs = state_machine._build_step_inputs(flow, StepType.SELF_CHECK)
        inputs["prev_self_check_issues"][0]["description"] = "MUTATED"
        assert prev_sc.outputs["issues"][0]["description"] == "D"

    def test_implement_test_results_deep_copied(self, state_machine):
        """IMPLEMENT fix-context carries a deep copy of the most recent TEST
        step's ``test_results`` so later mutations of the snapshot can't
        corrupt the originals stored on the step."""
        flow = FlowInstance(
            flow_id="test-deepcopy-impl",
            task_description="Test task",
            status=FlowStatus.RUNNING,
        )
        flow.state.increment_fix_iteration(fix_context={"reason": "test_failure"})
        flow.state.add_step(Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED, outputs={}))
        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": False, "failures": [{"name": "t1"}]}},
        )
        flow.state.add_step(test_step)

        inputs = state_machine._build_step_inputs(flow, StepType.IMPLEMENT)

        inputs["test_results"]["failures"][0]["name"] = "MUTATED"

        assert test_step.outputs["test_results"]["failures"][0]["name"] == "t1"


class TestMultiIterationAccumulation:
    """Verify that fix_history and previous_output behave correctly across
    multiple fix iterations — the scenario that was never tested before."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        return StateMachine(project_root=tmp_path)

    def test_fix_history_accumulates_across_iterations(self, state_machine):
        flow = FlowInstance(
            flow_id="test-accum-history",
            task_description="T",
            status=FlowStatus.RUNNING,
        )
        flow.state.increment_fix_iteration(fix_context={"reason": "test_failure"})
        flow.state.increment_fix_iteration(fix_context={"reason": "self_check"})
        flow.state.increment_fix_iteration(fix_context={"reason": "spec_compliance"})
        flow.state.add_step(Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED, outputs={}))

        inputs = state_machine._build_step_inputs(flow, StepType.IMPLEMENT)

        assert len(inputs["fix_history"]) == 3
        reasons = [e["reason"] for e in inputs["fix_history"]]
        assert reasons == ["test_failure", "self_check", "spec_compliance"]
        iterations = [e["iteration"] for e in inputs["fix_history"]]
        assert iterations == [1, 2, 3]

    def test_fix_history_snapshot_is_independent_of_state(self, state_machine):
        flow = FlowInstance(
            flow_id="test-accum-snapshot",
            task_description="T",
            status=FlowStatus.RUNNING,
        )
        flow.state.increment_fix_iteration(fix_context={"reason": "test_failure"})
        flow.state.add_step(Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED, outputs={}))

        inputs = state_machine._build_step_inputs(flow, StepType.IMPLEMENT)
        # Mutating the snapshot must not pollute state.fix_history.
        inputs["fix_history"].append({"iteration": 99, "reason": "fake"})
        assert len(flow.state.fix_history) == 1

    def test_transition_to_fix_caps_previous_output_size(self, tmp_path):
        from se3.engine.state_machine import _PREVIOUS_OUTPUT_MAX_BYTES

        sm = StateMachine(project_root=tmp_path)
        flow = FlowInstance(
            flow_id="test-truncate-prevout",
            task_description="T",
            status=FlowStatus.RUNNING,
        )
        huge = "x" * (_PREVIOUS_OUTPUT_MAX_BYTES * 2)
        impl_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"big_blob": huge, "files_changed": ["a.py"]},
        )
        flow.state.add_step(impl_step)

        trigger = Step(
            step_type=StepType.TEST,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "re-run",
                "fix_context": {"reason": "test_failure"},
            },
        )
        flow.state.add_step(trigger)

        with patch.object(sm, "persistence") as mock_pers:
            mock_pers.save_flow = Mock()
            result = sm._transition_to_fix(flow, trigger)

        assert result is impl_step
        prev = impl_step.inputs["previous_output"]
        assert prev.get("_truncated") is True
        assert prev["_original_size"] > _PREVIOUS_OUTPUT_MAX_BYTES
        assert len(prev["preview"]) <= _PREVIOUS_OUTPUT_MAX_BYTES

    def test_transition_to_fix_excludes_nested_previous_output(self, tmp_path):
        sm = StateMachine(project_root=tmp_path)
        flow = FlowInstance(
            flow_id="test-no-nest-prevout",
            task_description="T",
            status=FlowStatus.RUNNING,
        )
        # Simulate an LLM that echoed previous_output back into outputs.
        impl_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={
                "files_changed": ["a.py"],
                "previous_output": {"stale": "data"},
            },
        )
        flow.state.add_step(impl_step)
        trigger = Step(
            step_type=StepType.TEST,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "re-run",
                "fix_context": {"reason": "test_failure"},
            },
        )
        flow.state.add_step(trigger)

        with patch.object(sm, "persistence") as mock_pers:
            mock_pers.save_flow = Mock()
            sm._transition_to_fix(flow, trigger)

        prev = impl_step.inputs["previous_output"]
        # The key must not have re-nested a "previous_output" child.
        assert "previous_output" not in prev
        assert "files_changed" in prev


class TestInferFixReason:
    def test_known_trigger_types(self):
        from se3.engine.state_machine import _infer_fix_reason
        assert _infer_fix_reason("test") == "test_failure"
        assert _infer_fix_reason("self_check") == "self_check"
        assert _infer_fix_reason("verify_spec") == "spec_compliance"

    def test_unknown_type_returns_trigger_itself(self):
        from se3.engine.state_machine import _infer_fix_reason
        # Not silently labeled as "spec_compliance" anymore — returns the input.
        assert _infer_fix_reason("lint") == "lint"
        assert _infer_fix_reason("") == "unknown"


class TestMaxFixIterations:
    """Test cases for max fix iterations configuration."""

    def test_get_max_fix_iterations_default(self, tmp_path):
        """Test that default max fix iterations is 100."""
        from se3.config import DEFAULT_MAX_FIX_ITERATIONS

        state_machine = StateMachine(project_root=tmp_path)

        result = state_machine._get_max_fix_iterations()

        assert result == DEFAULT_MAX_FIX_ITERATIONS == 100

    def test_get_max_fix_iterations_from_config(self, tmp_path):
        """Test that max fix iterations can be loaded from config."""
        # Create se3.yaml with custom max_fix_iterations
        config_content = """
workflow:
  max_fix_iterations: 5
"""
        (tmp_path / "se3.yaml").write_text(config_content)

        state_machine = StateMachine(project_root=tmp_path)

        result = state_machine._get_max_fix_iterations()

        assert result == 5

    def test_get_max_fix_iterations_zero_sentinel(self, tmp_path):
        """max_fix_iterations: 0 is preserved (sentinel for unlimited)."""
        config_content = """
workflow:
  max_fix_iterations: 0
"""
        (tmp_path / "se3.yaml").write_text(config_content)

        state_machine = StateMachine(project_root=tmp_path)

        assert state_machine._get_max_fix_iterations() == 0

    def test_get_max_fix_iterations_null_sentinel(self, tmp_path):
        """max_fix_iterations: null normalizes to the sentinel 0."""
        config_content = """
workflow:
  max_fix_iterations: null
"""
        (tmp_path / "se3.yaml").write_text(config_content)

        state_machine = StateMachine(project_root=tmp_path)

        assert state_machine._get_max_fix_iterations() == 0


class TestUnlimitedSentinelEndToEnd:
    """End-to-end coverage for the integration path that injects
    `max_fix_iterations=0` from se3.yaml through `_build_step_inputs` into
    each step's inputs and finally into the LLM prompt.

    Existing tests mock `_get_max_fix_iterations` directly or set inputs
    directly. This class exercises the full chain — a regression where
    `state_machine._build_step_inputs` accidentally re-introduced an `or`
    short-circuit on the propagation lines would be caught here, where the
    earlier tests bypass that code path entirely.
    """

    def _write_unlimited_yaml(self, project_root: Path) -> None:
        (project_root / "se3.yaml").write_text(
            "workflow:\n  max_fix_iterations: 0\n"
        )

    def _flow_in_fix_loop(self, fix_iteration: int) -> FlowInstance:
        flow = FlowInstance(
            flow_id="unlimited-e2e",
            task_description="Test",
            status=FlowStatus.RUNNING,
        )
        for _ in range(fix_iteration):
            flow.state.increment_fix_iteration(fix_context={"reason": "test_failure"})

        flow.state.add_step(Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": ["a.py"]},
        ))
        flow.state.add_step(Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": False, "returncode": 1, "stdout": "", "stderr": ""}},
        ))
        flow.state.add_step(Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "issues": [],
                "fix_instructions": "fix it",
                "verification_result": {"issues": []},
            },
        ))
        return flow

    @pytest.mark.parametrize("fix_iteration", [1, 5, 200])
    def test_self_check_prompt_says_unlimited_across_iterations(
        self, tmp_path, fix_iteration
    ):
        """Same end-to-end check for SELF_CHECK: se3.yaml(0) → inputs(0) →
        prompt('unlimited').
        """
        from se3.engine.steps.self_check import self_check_handler

        self._write_unlimited_yaml(tmp_path)
        sm = StateMachine(project_root=tmp_path)
        flow = self._flow_in_fix_loop(fix_iteration)

        assert sm._get_max_fix_iterations() == 0

        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)
        assert inputs["max_fix_iterations"] == 0
        assert inputs["fix_iteration"] == fix_iteration

        step = Step(step_type=StepType.SELF_CHECK, status=StepStatus.PENDING, inputs=inputs)
        flow.change_path = tmp_path

        mock_response = '{"issues": [], "summary": "ok"}'

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller
            self_check_handler(step, flow)
            prompt = mock_caller.call.call_args[1]["prompt"]

        assert "unlimited" in prompt.lower()
        assert "final fix attempt" not in prompt.lower()
        assert f"of {fix_iteration}" not in prompt

    def test_self_check_initial_iteration_unlimited_no_warning(self, tmp_path):
        """Same contract for SELF_CHECK at fix_iteration=0."""
        from se3.engine.steps.self_check import self_check_handler

        flow = self._flow_in_fix_loop(0)
        flow.change_path = tmp_path

        self._write_unlimited_yaml(tmp_path)
        inputs = {
            "task_description": "Test",
            "spec_content": {},
            "changes_made": {},
            "test_results": {"passed": True, "returncode": 0, "stdout": "OK"},
            "fix_iteration": 0,
            "max_fix_iterations": 0,
        }
        step = Step(step_type=StepType.SELF_CHECK, status=StepStatus.PENDING, inputs=inputs)

        mock_response = '{"issues": [], "summary": "ok"}'

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_caller_class:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_caller_class.return_value = mock_caller
            self_check_handler(step, flow)
            prompt = mock_caller.call.call_args[1]["prompt"]

        assert "no previous fix attempts" in prompt.lower()
        assert "final fix attempt" not in prompt.lower()
        assert "fix iteration:" not in prompt.lower()
        assert "unlimited mode" in prompt.lower()


class TestUnlimitedAndConvergenceInteraction:
    """Lock the safety contract: with ``max_fix_iterations=0`` (unlimited) AND
    ``self_check_convergence_enabled=true``, the convergence shortcut MUST
    still fire when the LLM re-reports identical self-check issues.

    Convergence is the spec's documented stalled-loop safety mechanism for
    unlimited mode (see se3-config 'Stalled-loop safety guidance'). A
    regression that disabled convergence under the unlimited sentinel would
    convert a stalled loop into an actually-infinite loop. Existing tests
    cover unlimited propagation OR convergence in isolation, but not the
    combination — this class binds both contracts together.
    """

    def _write_yaml(self, project_root: Path) -> None:
        # Deferral (default threshold 3) takes precedence over convergence: when
        # enabled, every non-empty finding is deferred or fixed, never dropped by
        # the convergence shortcut (see self_check.py ``convergence_blocked_by_defer``).
        # To exercise convergence-as-loop-break in isolation, deferral must be
        # turned off (threshold 0), so this safety-contract test sets it explicitly.
        (project_root / "se3.yaml").write_text(
            "workflow:\n"
            "  max_fix_iterations: 0\n"
            "  self_check_convergence_enabled: true\n"
            "  self_check_defer_fix_threshold: 0\n"
        )

    def _flow_in_fix_loop_with_prev_self_check(
        self, prev_issues: list[dict],
    ) -> FlowInstance:
        flow = FlowInstance(
            flow_id="unlimited-convergence-e2e",
            # Substantive task_description so verbatim_quote validation
            # can substring-match against the source pool.
            task_description="Test fix loop with convergence safety",
            status=FlowStatus.RUNNING,
        )
        flow.state.increment_fix_iteration(fix_context={"reason": "self_check"})
        flow.state.add_step(Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": ["a.py", "b.py"]},
        ))
        flow.state.add_step(Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True, "returncode": 0}},
        ))
        flow.state.add_step(Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.REVISION_NEEDED,
            outputs={"issues": prev_issues},
        ))
        return flow

    def _new_schema_issue(
        self, severity: str, path: str, line: int,
        actual: str = "broken behavior", expected: str = "correct behavior",
        divergence: str = "concrete failure",
    ) -> dict:
        return {
            "severity": severity,
            "actual_behavior": actual,
            "expected_behavior": expected,
            "divergence": divergence,
            "expectation_source": {
                "type": "task_description",
                "verbatim_quote": "Test fix loop with convergence safety",
            },
            "evidence_lines": [f"{path}:{line}"],
            "missing_in": [],
            "out_of_scope": False,
        }

    def test_convergence_fires_under_unlimited_mode(self, tmp_path):
        """End-to-end: unlimited cap + convergence enabled + LLM repeats the
        previous self-check issues → COMPLETED (loop breaks). Without
        convergence this would loop forever in unlimited mode.
        """
        from se3.engine.steps.self_check import self_check_handler
        import json

        # Non-critical/high severities: the convergence shortcut may only fire
        # when no critical/high finding is present (a critical/high finding
        # always re-enters the fix loop). Using low/medium keeps this test's
        # focus on the unlimited-mode loop-break contract.
        prev_issues = [
            self._new_schema_issue("low", "a.py", 1,
                                   actual="x", divergence="x crashes"),
            self._new_schema_issue("medium", "b.py", 2,
                                   actual="y", divergence="y leaks"),
        ]

        self._write_yaml(tmp_path)
        sm = StateMachine(project_root=tmp_path)
        flow = self._flow_in_fix_loop_with_prev_self_check(prev_issues)

        assert sm._get_max_fix_iterations() == 0
        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)
        assert inputs["max_fix_iterations"] == 0
        assert inputs["self_check_convergence_enabled"] is True
        assert inputs["prev_self_check_issues"] == prev_issues
        assert inputs["self_check_pass_index"] == 1

        step = Step(step_type=StepType.SELF_CHECK, status=StepStatus.PENDING, inputs=inputs)
        flow.change_path = tmp_path
        mock_response = json.dumps({"issues": prev_issues, "summary": "same as before"})

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_cls.return_value = mock_caller
            result = self_check_handler(step, flow)

        assert result == StepStatus.COMPLETED, (
            "convergence MUST short-circuit the loop under unlimited mode; "
            "otherwise repeated identical findings produce an infinite loop"
        )
        assert step.outputs.get("converged") is True
        assert step.outputs.get("unresolved_issues") == prev_issues
        # NOTE: ``max_fix_iterations`` is only written into outputs on the
        # REVISION_NEEDED branch. The convergence path returns COMPLETED, so
        # it lives in inputs (already asserted above) but not in outputs —
        # that's intentional, COMPLETED steps don't carry a fix-loop counter.

    def test_no_convergence_when_disabled_under_unlimited(self, tmp_path):
        """Companion contract: when convergence is OFF under unlimited mode,
        the LLM repeating the same issues does NOT short-circuit — the loop
        keeps going. This is the regression risk the previous test guards
        against: convergence must be the explicit mechanism that breaks it.

        Note: prev_self_check_issues is now injected unconditionally (the
        ``convergence_enabled`` gate has been removed for the schema-rewrite
        commit so the new ``previous_issue_resolutions`` schema works).
        Convergence as a runtime *short-circuit* still requires the flag.
        """
        from se3.engine.steps.self_check import self_check_handler
        import json

        prev_issues = [
            self._new_schema_issue("high", "a.py", 1,
                                   actual="x", divergence="x crashes"),
        ]

        # convergence_enabled defaults to False — only set unlimited
        (tmp_path / "se3.yaml").write_text(
            "workflow:\n  max_fix_iterations: 0\n"
        )
        sm = StateMachine(project_root=tmp_path)
        flow = self._flow_in_fix_loop_with_prev_self_check(prev_issues)

        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)
        assert inputs["max_fix_iterations"] == 0
        assert inputs["self_check_convergence_enabled"] is False
        # prev_self_check_issues IS injected (gate dropped) — but
        # convergence_enabled=False means the short-circuit won't fire.
        assert inputs["prev_self_check_issues"] == prev_issues

        step = Step(step_type=StepType.SELF_CHECK, status=StepStatus.PENDING, inputs=inputs)
        flow.change_path = tmp_path
        mock_response = json.dumps({"issues": prev_issues, "summary": "same"})

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = mock_response
            mock_cls.return_value = mock_caller
            result = self_check_handler(step, flow)

        # Without the convergence_enabled flag the short-circuit doesn't
        # fire — handler reports REVISION_NEEDED. Under unlimited mode this
        # would loop forever — exactly the regression this test is locking.
        assert result == StepStatus.REVISION_NEEDED
        assert not step.outputs.get("converged")


class TestUnlimitedOutputDiskShape:
    """Lock the on-disk shape of ``step.outputs`` when running under the
    unlimited sentinel. ``step.outputs`` is persisted to engine.json via
    JSON serialization; consumers reading it back (status displays,
    post-mortem renderers) must see the sentinel preserved as ``0``, not
    silently rewritten to ``None`` or a different value by some future
    output-massaging code.

    This guards against a subtle foot-gun: a future consumer might
    misinterpret ``max_fix_iterations: 0`` as 'limit reached' rather than
    'unlimited'. The handler comments now document the sentinel, but a
    serialization round-trip test makes the contract executable.
    """

    def test_self_check_outputs_serialize_with_sentinel(self, tmp_path):
        """self_check → JSON → back: max_fix_iterations stays ``0`` (int)."""
        from se3.engine.steps.self_check import self_check_handler
        import json as _json

        flow = FlowInstance(
            flow_id="disk-shape-sc",
            # Substantive task_description for verbatim_quote validation.
            task_description="Disk shape sentinel preservation test",
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "c",
        )
        flow.state.selected_steps = [StepType.SELF_CHECK]

        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Disk shape sentinel preservation test",
                "changes_made": {
                    "files_changed": [{"path": "a.py", "action": "modify"}],
                },
                "test_results": {"passed": True, "returncode": 0},
                "spec_content": {},
                "fix_iteration": 11,
                "max_fix_iterations": 0,  # the unlimited sentinel
            },
        )
        # New-schema valid issue so it survives validation and triggers
        # the REVISION_NEEDED branch (where outputs["max_fix_iterations"]
        # is written).
        valid_issue = {
            "severity": "low",
            "actual_behavior": "broken on disk shape edge case",
            "expected_behavior": "sentinel preserved as int 0",
            "divergence": "consumer would div-by-zero",
            "expectation_source": {
                "type": "task_description",
                "verbatim_quote": "Disk shape sentinel preservation test",
            },
            "evidence_lines": ["a.py:1"],
            "missing_in": [],
            "out_of_scope": False,
        }
        response = _json.dumps({
            "issues": [valid_issue],
            "summary": "issue",
        })

        with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = response
            mock_cls.return_value = mock_caller
            self_check_handler(step, flow)

        roundtripped = _json.loads(_json.dumps(step.outputs))
        assert roundtripped["max_fix_iterations"] == 0
        assert isinstance(roundtripped["max_fix_iterations"], int)
        assert roundtripped["max_fix_iterations"] is not None


class TestFixLoopIntegration:
    """Integration tests for the complete fix loop."""

    @pytest.fixture
    def state_machine(self, tmp_path):
        """Create a test state machine with mocked persistence."""
        with patch("se3.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=tmp_path)
            return sm

    def test_complete_fix_loop_cycle(self, state_machine):
        """Test a complete cycle through the fix loop."""
        # Create flow
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

        # Add implement step
        implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": []},
        )
        flow.state.add_step(implement_step)

        # Add test step
        test_step = Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": False}},
        )
        flow.state.add_step(test_step)

        # Add verify_spec step with REVISION_NEEDED
        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Fix the bug",
                "fix_context": {"test_failed": True},
            },
        )
        flow.state.add_step(verify_step)
        flow.state.current_step_id = verify_step.step_id

        # Transition should go back to implement
        next_step = state_machine.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.IMPLEMENT
        assert flow.state.get_fix_iteration() == 1
        assert implement_step.inputs["is_fix_iteration"] is True


class TestCreateFlowConfigValidation:
    """End-to-end fail-fast validation through ``create_flow``.

    Locks the contract that an invalid ``workflow.max_fix_iterations`` in
    se3.yaml fails the flow creation, not just ``WorkflowConfig.from_dict``
    in isolation.
    """

    def test_create_flow_negative_max_fix_iterations_fails_fast(self, tmp_path):
        """create_flow MUST raise ConfigError when yaml has a negative cap."""
        (tmp_path / "se3.yaml").write_text(
            "workflow:\n  max_fix_iterations: -1\n"
        )
        sm = StateMachine(project_root=tmp_path)
        with pytest.raises(ConfigError):
            sm.create_flow("test task", task_type="feature")

    def test_create_flow_zero_max_fix_iterations_is_unlimited(self, tmp_path):
        """create_flow accepts 0 (sentinel for unlimited) without error."""
        (tmp_path / "se3.yaml").write_text(
            "workflow:\n  max_fix_iterations: 0\n"
        )
        sm = StateMachine(project_root=tmp_path)
        # Should not raise
        flow = sm.create_flow("test task", task_type="feature")
        assert flow is not None
        assert sm._get_max_fix_iterations() == 0


class TestHotEditMaxFixIterations:
    """Locks the cache-invalidation contract on ``transition_to_next``.

    ``_workflow_config_cache`` is reset at the start of each transition so a
    yaml hot-edit of ``workflow.max_fix_iterations`` is observed on the next
    transition. These tests guard against a future memoization change that
    silently breaks that property.
    """

    def _make_flow_at_iteration(self, iteration: int) -> FlowInstance:
        flow = FlowInstance(
            flow_id="hot-edit-flow",
            task_description="hot edit test",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERIFY_SPEC,
        ]
        # Simulate having gone through fix loop already
        for _ in range(iteration):
            flow.state.increment_fix_iteration()
        # Add an implement step + a verify_spec step in REVISION_NEEDED so
        # transition_to_next routes through the fix-loop path.
        implement_step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": []},
        )
        flow.state.add_step(implement_step)
        verify_step = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "Fix the bug",
                "fix_context": {"reason": "spec_failure"},
            },
        )
        flow.state.add_step(verify_step)
        flow.state.current_step_id = verify_step.step_id
        return flow

    def test_unlimited_to_finite_triggers_failed_when_already_over_cap(self, tmp_path):
        """Flipping yaml from 0 (unlimited) to a finite cap below current
        iteration MUST cause the next transition to set FAILED."""
        yaml_path = tmp_path / "se3.yaml"
        yaml_path.write_text("workflow:\n  max_fix_iterations: 0\n")
        sm = StateMachine(project_root=tmp_path)
        # Warm the cache as if we'd already transitioned under unlimited
        assert sm._get_workflow_config().max_fix_iterations == 0

        flow = self._make_flow_at_iteration(iteration=10)

        # Hot-edit: drop to 5, well below the current 10 iterations
        yaml_path.write_text("workflow:\n  max_fix_iterations: 5\n")

        # transition_to_next must invalidate cache, see new cap, mark FAILED
        next_step = sm.transition_to_next(flow)
        assert next_step is None
        assert flow.status == FlowStatus.FAILED

    def test_finite_to_unlimited_stops_failed_transitions(self, tmp_path):
        """Flipping yaml from a finite cap to 0 (unlimited) MUST stop the
        next transition from going FAILED even if iteration > old cap."""
        yaml_path = tmp_path / "se3.yaml"
        yaml_path.write_text("workflow:\n  max_fix_iterations: 5\n")
        sm = StateMachine(project_root=tmp_path)
        # Warm the cache under the finite cap
        assert sm._get_workflow_config().max_fix_iterations == 5

        flow = self._make_flow_at_iteration(iteration=10)

        # Hot-edit: flip to unlimited
        yaml_path.write_text("workflow:\n  max_fix_iterations: 0\n")

        # transition_to_next must invalidate cache, see unlimited, NOT FAILED
        next_step = sm.transition_to_next(flow)
        assert flow.status != FlowStatus.FAILED
        # Should route into a fix step rather than terminate
        assert next_step is not None


class TestNPassSentinelComposition:
    """Lock the contract for ``self_check_passes_required > 1`` composed with
    ``max_fix_iterations = 0`` (unlimited).

    The two counters live in different code paths — N-pass is driven by
    ``_count_consecutive_self_check_completed`` against the workflow-config
    pass count, while max_fix_iterations is checked in
    ``transition_to_next`` against the State's fix_iterations. A regression
    that conflated the two (e.g. short-circuiting the N-pass loop because
    ``max_fix_iterations <= 0``, or vice-versa) would only surface when both
    knobs are turned at once. This class exercises that composition.
    """

    def test_n_pass_creates_all_instances_under_unlimited_sentinel(self, tmp_path):
        """N=3 + max_fix_iterations=0: state machine must still create
        self_check instances #1, #2, #3 sequentially as each completes clean,
        then advance to verify_spec on the 4th transition."""
        from se3.config import WorkflowConfig

        cfg = WorkflowConfig(
            max_fix_iterations=0,
            self_check_passes_required=3,
        )
        with patch("se3.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=tmp_path)
        sm._get_workflow_config = lambda **kwargs: cfg

        flow = FlowInstance(
            flow_id="npass-sentinel-flow",
            task_description="Test",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
            StepType.VERIFY_SPEC,
            StepType.COMMIT,
        ]
        flow.state.add_step(Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": ["a.py"]},
        ))
        flow.state.add_step(Step(
            step_type=StepType.TEST,
            status=StepStatus.COMPLETED,
            outputs={"test_results": {"passed": True}},
        ))

        # Pass #1
        sc1 = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.COMPLETED,
            outputs={"issues": [], "actionable_count": 0},
        )
        flow.state.add_step(sc1)
        flow.state.current_step_id = sc1.step_id

        sc2 = sm.transition_to_next(flow)
        assert sc2 is not None
        assert sc2.step_type == StepType.SELF_CHECK, (
            "unlimited mode must NOT short-circuit the N-pass loop"
        )
        sc2.status = StepStatus.COMPLETED
        sc2.outputs = {"issues": [], "actionable_count": 0}
        flow.state.current_step_id = sc2.step_id

        sc3 = sm.transition_to_next(flow)
        assert sc3 is not None
        assert sc3.step_type == StepType.SELF_CHECK
        sc3.status = StepStatus.COMPLETED
        sc3.outputs = {"issues": [], "actionable_count": 0}
        flow.state.current_step_id = sc3.step_id

        # After 3 consecutive clean self_checks, advance to verify_spec.
        next_step = sm.transition_to_next(flow)
        assert next_step is not None
        assert next_step.step_type == StepType.VERIFY_SPEC, (
            "after N consecutive clean self_check passes, the flow must "
            "advance even under unlimited cap"
        )

        # Exactly 3 SELF_CHECK steps in history.
        sc_count = sum(
            1 for sid in flow.state.step_history
            if flow.state.steps[sid].step_type == StepType.SELF_CHECK
        )
        assert sc_count == 3
        # max_fix_iterations sentinel was not consumed by the N-pass loop —
        # the fix counter stays at 0 because no REVISION_NEEDED occurred.
        assert flow.state.get_fix_iteration() == 0
        assert flow.status == FlowStatus.RUNNING


class TestWorkflowConfigFallbackContract:
    """Locks the fail-fast invariant for ``_get_workflow_config``.

    Documented contract: a startup ``ConfigError`` (no prior successful
    load) must propagate so the user is forced to fix se3.yaml before the
    flow runs. Only mid-flow ConfigErrors after a successful load are
    swallowed in favor of last-known-good config.

    Regression: an early ``IOError``/``OSError`` used to promote the
    default ``WorkflowConfig()`` to ``_workflow_config_last_good``,
    silently disabling startup-style fail-fast for the rest of the
    StateMachine's lifetime — a subsequent ``ConfigError`` (e.g. user
    fixes the IO error then introduces a yaml typo) would be swallowed
    instead of raised.
    """

    def test_ioerror_does_not_promote_defaults_to_last_good(self, tmp_path):
        sm = StateMachine(project_root=tmp_path)

        # First load: simulate IOError → fallback to defaults for the
        # current transition only.
        with patch(
            "se3.engine.state_machine.WorkflowConfig.load",
            side_effect=IOError("disk full"),
        ):
            cfg1 = sm._get_workflow_config()
        assert cfg1.max_fix_iterations == DEFAULT_MAX_FIX_ITERATIONS

        # Critical invariant: the IOError fallback must NOT be cached as
        # last-known-good, otherwise the next ConfigError would be
        # silently swallowed.
        assert getattr(sm, "_workflow_config_last_good", None) is None

        # Reset the per-transition cache (transition_to_next does this) and
        # then trigger a ConfigError. With no real prior load on record, it
        # MUST propagate.
        sm._workflow_config_cache = None
        with patch(
            "se3.engine.state_machine.WorkflowConfig.load",
            side_effect=ConfigError("invalid yaml"),
        ):
            with pytest.raises(ConfigError):
                sm._get_workflow_config()

    def test_successful_load_then_configerror_uses_last_good(self, tmp_path):
        """After at least one successful load, a subsequent ConfigError
        falls back to last-known-good rather than crashing the flow."""
        from se3.config import WorkflowConfig

        sm = StateMachine(project_root=tmp_path)
        good = WorkflowConfig(max_fix_iterations=7)

        with patch(
            "se3.engine.state_machine.WorkflowConfig.load",
            return_value=good,
        ):
            assert sm._get_workflow_config().max_fix_iterations == 7

        # Mid-flow yaml hot-edit introduces an invalid value.
        sm._workflow_config_cache = None
        with patch(
            "se3.engine.state_machine.WorkflowConfig.load",
            side_effect=ConfigError("hot-edit gone wrong"),
        ):
            cfg = sm._get_workflow_config()
        assert cfg.max_fix_iterations == 7  # last-known-good


class TestFixHistoryClampOnLoad:
    """Locks the retroactive-clamp invariant on ``State.from_dict``.

    Without it, an engine.json written by an older build with more than
    ``FIX_HISTORY_MAX_ENTRIES`` entries would be loaded verbatim and the
    oversized list would be deepcopied per transition / re-persisted on
    every save until the next append finally trimmed it.
    """

    def test_oversized_fix_history_is_clamped_on_load(self):
        oversized = [
            {"iteration": i, "reason": "test_failure"}
            for i in range(FIX_HISTORY_MAX_ENTRIES + 25)
        ]
        data = {
            "current_step_id": None,
            "step_history": [],
            "steps": {},
            "context": {"fix_history": oversized},
            "selected_steps": [],
            "current_step_index": 0,
            "review_iterations": {},
            "fix_iterations": len(oversized),
            "fix_history": oversized,
        }
        state = State.from_dict(data)
        assert len(state.fix_history) == FIX_HISTORY_MAX_ENTRIES
        # Tail-keep policy: oldest entries dropped, most recent kept.
        assert state.fix_history[0]["iteration"] == 25
        assert state.fix_history[-1]["iteration"] == FIX_HISTORY_MAX_ENTRIES + 24
        # The mirrored copy in ``state.context['fix_history']`` must be
        # clamped too — diverging sources of truth break consumers that
        # read either path.
        assert state.context["fix_history"] is state.fix_history

    def test_within_cap_fix_history_unchanged_on_load(self):
        """Loads under the cap pass through unchanged."""
        history = [
            {"iteration": i, "reason": "test_failure"}
            for i in range(5)
        ]
        data = {
            "fix_history": history,
        }
        state = State.from_dict(data)
        assert state.fix_history == history

    def test_unlimited_mode_oversized_fix_history_clamped(self):
        """Regression: a degenerate run in unlimited mode (max_fix_iterations=0)
        could produce engine.json with cap+ entries before the sliding-window
        cap was added. Loading such a file must trim to FIX_HISTORY_MAX_ENTRIES
        so resumed flows do not re-inflate memory and persist the oversized
        copy on every save.
        """
        oversized_count = FIX_HISTORY_MAX_ENTRIES * 2 + 37  # well over the cap
        oversized = [
            {"iteration": i, "reason": "test_failure", "trigger_step_type": "test"}
            for i in range(oversized_count)
        ]
        data = {
            "current_step_id": None,
            "step_history": [],
            "steps": {},
            "context": {"fix_history": oversized},
            "selected_steps": [],
            "current_step_index": 0,
            "review_iterations": {},
            "fix_iterations": oversized_count,
            "fix_history": oversized,
        }
        state = State.from_dict(data)
        assert len(state.fix_history) == FIX_HISTORY_MAX_ENTRIES
        # Tail-keep: oldest entries dropped, most recent kept.
        assert state.fix_history[0]["iteration"] == oversized_count - FIX_HISTORY_MAX_ENTRIES
        assert state.fix_history[-1]["iteration"] == oversized_count - 1
        # context mirror must be the same clamped list.
        assert state.context["fix_history"] is state.fix_history
        assert len(state.context["fix_history"]) == FIX_HISTORY_MAX_ENTRIES


class TestUnlimitedSentinelHighIterationDrive:
    """End-to-end coverage gap closer: drive ``transition_to_next`` through
    150+ real iterations with ``max_fix_iterations=0`` and assert
    ``flow.status`` stays ``RUNNING`` throughout — no single existing test
    exercises the natural-loop drive at high iteration counts.

    Existing high-iteration tests either mock ``_get_max_fix_iterations``
    on a single transition (``test_max_fix_iterations_zero_does_not_fail``
    bumps ``flow.state.fix_iterations`` to 200 then performs ONE
    transition) or stop at 30 (``test_max_fix_iterations_zero_drives_many_
    iterations_without_failure``). A regression that miscounted past 100
    real iterations would slip past both. This test compounds 150 real
    increments through the canonical drive path to lock the contract.
    """

    @pytest.fixture
    def state_machine(self, tmp_path):
        with patch("se3.engine.state_machine.PersistenceManager"):
            return StateMachine(project_root=tmp_path)

    @pytest.fixture
    def flow_with_verify_revision(self, tmp_path):
        flow = FlowInstance(
            flow_id="unlimited-150-iter",
            task_description="Drive 150 iterations under sentinel",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERIFY_SPEC,
            StepType.COMMIT,
        ]
        impl = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            outputs={"files_changed": []},
        )
        flow.state.add_step(impl)
        verify = Step(
            step_type=StepType.VERIFY_SPEC,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "fix_needed": True,
                "fix_instructions": "x",
                "fix_context": {"test_failed": True},
            },
        )
        flow.state.add_step(verify)
        flow.state.current_step_id = verify.step_id
        return flow, impl, verify

    def test_unlimited_drives_150_iterations_without_failure(
        self, state_machine, flow_with_verify_revision,
    ):
        flow, implement_step, verify_step = flow_with_verify_revision

        ITERATIONS = 150
        with patch.object(
            state_machine, "_get_max_fix_iterations", return_value=0
        ):
            for i in range(ITERATIONS):
                verify_step.status = StepStatus.REVISION_NEEDED
                flow.state.current_step_id = verify_step.step_id
                next_step = state_machine.transition_to_next(flow)

                assert next_step is not None, (
                    f"iteration {i+1}: sentinel must grant another attempt"
                )
                assert next_step.step_id == implement_step.step_id
                assert flow.status == FlowStatus.RUNNING, (
                    f"iteration {i+1}: flow.status must stay RUNNING, got "
                    f"{flow.status}"
                )

        # The fix-iteration counter has compounded past every cap a finite
        # configuration would allow (default 100, common ceiling 50, etc.).
        assert flow.state.get_fix_iteration() == ITERATIONS
        assert flow.status == FlowStatus.RUNNING

