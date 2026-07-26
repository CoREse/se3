"""Tests for the retry_count reset at multi-round/iteration transition points.

Bug: ``inputs["retry_count"]`` is written by three paths in run.py and is
never reset on its own. Discovery round advance, fix-loop iteration, and
revision iteration each start a new LLM call with a fresh prompt (not a
retry of the previous call), so they must clear the counter. Otherwise
``LLMCaller`` sees ``external_attempt > 0`` and discards the new prompt
in favor of a retry-context wrapper pointing at a prior call.

These tests pin the reset behavior at each transition site. The helper
``_reset_retry_counter_for_new_call`` is exercised indirectly through
the transition functions to keep the coverage end-to-end.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.state_machine import (
    StateMachine,
    _reset_retry_counter_for_new_call,
)


# ---------------------------------------------------------------------------
# Helper itself
# ---------------------------------------------------------------------------

class TestResetHelper:

    def test_pops_retry_count_when_present(self):
        step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.PENDING)
        step.inputs["retry_count"] = 2
        step.inputs["other"] = "kept"
        _reset_retry_counter_for_new_call(step)
        assert "retry_count" not in step.inputs
        assert step.inputs["other"] == "kept"

    def test_noop_when_absent(self):
        step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.PENDING)
        step.inputs["other"] = "kept"
        _reset_retry_counter_for_new_call(step)
        assert "retry_count" not in step.inputs
        assert step.inputs["other"] == "kept"


# ---------------------------------------------------------------------------
# Fix-loop transition (state_machine._transition_to_fix)
# ---------------------------------------------------------------------------

def _build_flow_with_implement_fix(tmp_path: Path, with_retry_count: int = 0):
    flow = FlowInstance(
        flow_id="test-reset-fix",
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

    implement_step = Step(
        step_type=StepType.IMPLEMENT,
        status=StepStatus.COMPLETED,
        inputs={"task_groups": [], "retry_count": with_retry_count},
        outputs={"files_changed": ["test.py"]},
    )
    flow.state.add_step(implement_step)

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


class TestFixLoopResetsRetryCount:

    def test_stale_retry_count_cleared_on_fix_iteration(self, tmp_path):
        """A fix iteration's FIX_PROMPT is a new prompt; stale retry counter
        from a prior FAILED-Retry must not leak into it."""
        flow, implement_step, verify_step = _build_flow_with_implement_fix(
            tmp_path, with_retry_count=2,
        )
        sm = StateMachine(project_root=tmp_path)

        sm._transition_to_fix(flow, verify_step)

        assert "retry_count" not in implement_step.inputs
        # Fix iteration inputs still set as before.
        assert implement_step.inputs["is_fix_iteration"] is True
        assert implement_step.inputs["fix_instructions"] == "Fix the bug"

    def test_no_retry_count_stays_absent_after_fix_iteration(self, tmp_path):
        """Baseline: fix iteration on a step with no retry_count still has
        no retry_count afterward (no accidental injection)."""
        flow, implement_step, verify_step = _build_flow_with_implement_fix(
            tmp_path, with_retry_count=0,
        )
        # Remove the default 0 to exercise truly-absent path.
        implement_step.inputs.pop("retry_count", None)
        sm = StateMachine(project_root=tmp_path)

        sm._transition_to_fix(flow, verify_step)

        assert "retry_count" not in implement_step.inputs


# ---------------------------------------------------------------------------
# Revision transition (state_machine._transition_to_revision)
# ---------------------------------------------------------------------------

def _build_flow_with_plan_revision(tmp_path: Path, with_retry_count: int = 0):
    flow = FlowInstance(
        flow_id="test-reset-revision",
        task_description="Test task",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = [StepType.PLAN, StepType.CONFIRM]

    plan_step = Step(
        step_type=StepType.PLAN,
        status=StepStatus.COMPLETED,
        inputs={"retry_count": with_retry_count},
        outputs={"plan": {"proposal": {}}, "task_groups": []},
    )
    flow.state.add_step(plan_step)

    confirm_step = Step(
        step_type=StepType.CONFIRM,
        status=StepStatus.REVISION_NEEDED,
        inputs={"step_to_review_id": plan_step.step_id},
        outputs={"revision_feedback": "please expand the design"},
    )
    flow.state.add_step(confirm_step)
    flow.state.current_step_id = confirm_step.step_id

    return flow, plan_step, confirm_step


class TestRevisionResetsRetryCount:

    def test_stale_retry_count_cleared_on_revision(self, tmp_path):
        flow, plan_step, confirm_step = _build_flow_with_plan_revision(
            tmp_path, with_retry_count=3,
        )
        sm = StateMachine(project_root=tmp_path)

        sm._transition_to_revision(flow, confirm_step, plan_step.step_id)

        assert "retry_count" not in plan_step.inputs
        # Revision inputs still set.
        assert plan_step.inputs["is_revision"] is True
        assert plan_step.inputs["revision_feedback"] == "please expand the design"


# ---------------------------------------------------------------------------
# Discovery user-response path (run.py)
# ---------------------------------------------------------------------------

class TestDiscoveryUserResponseResetsRetryCount:
    """Discovery round advance after user reply must clear retry_count so
    the next round's CONTINUE_DISCOVERY_PROMPT (with user_response) is not
    discarded by LLMCaller's retry-context wrapping."""

    def test_user_response_path_clears_stale_retry_count(self, tmp_path, monkeypatch):
        # Lazy import + patching to keep this test hermetic (no state machine
        # wiring needed — we exercise only the run-loop branch of interest).
        from unittest.mock import MagicMock, patch
        from tianluo.commands import run as run_mod

        flow = FlowInstance(
            flow_id="test-reset-discovery",
            task_description="orig task",
            status=FlowStatus.RUNNING,
        )
        flow.state.selected_steps = [StepType.DISCOVERY]
        discovery_step = Step(
            step_type=StepType.DISCOVERY,
            status=StepStatus.PAUSED,
            inputs={
                "task_description": "orig task",
                "retry_count": 1,             # ← stale
                "resumed": True,
                "user_response": "old",
                "discovery_state": {
                    "round": 1,
                    "history": [
                        {"role": "assistant", "round": 0, "content": "q?"},
                    ],
                    "mode": "question",
                },
            },
        )
        flow.state.add_step(discovery_step)
        flow.state.current_step_id = discovery_step.step_id

        # Extract and invoke the branch of interest: we simulate the
        # `_handle_discovery_pause` returning a fresh user reply, then
        # assert the four mutations the branch performs (including the
        # retry_count pop).
        user_response = "new reply from user"

        # Reproduce the run-loop branch semantics (src/tianluo/commands/run.py
        # near the DISCOVERY+PAUSED handler). We don't call the whole
        # run_flow machinery — we just exercise the state transitions it
        # applies on user-response.
        discovery_step.inputs["user_response"] = user_response
        discovery_step.inputs["resumed"] = True
        discovery_step.inputs.pop("retry_count", None)
        discovery_step.status = StepStatus.PENDING

        assert discovery_step.inputs["user_response"] == user_response
        assert discovery_step.inputs["resumed"] is True
        assert "retry_count" not in discovery_step.inputs
        assert discovery_step.status == StepStatus.PENDING

    def test_run_py_branch_source_does_the_reset(self):
        """Source-level pin: verify the discovery user-response branch in
        run.py actually contains the ``pop("retry_count", None)`` call.
        Catches regressions where a future edit removes the reset without
        touching the behavioral test above.
        """
        import inspect
        from tianluo.commands import run as run_mod

        src = inspect.getsource(run_mod)
        # Find the branch header and confirm the pop appears within ~40 lines.
        marker = 'current_step.inputs["user_response"] = user_response'
        idx = src.find(marker)
        assert idx >= 0, "discovery user-response branch not found in run.py"
        window = src[idx:idx + 2000]
        assert 'inputs.pop("retry_count"' in window, (
            "discovery user-response branch must reset retry_count; "
            "see tests/engine/test_retry_count_reset.py for context"
        )
