"""Tests for adjudication trigger routing in the state machine (group G3, task 2).

When a SELF_CHECK step returns REVISION_NEEDED, ``transition_to_next`` now
evaluates the oscillation triggers BEFORE the normal fix routing. A trigger hit
routes to a dynamically-inserted ADJUDICATE step; a miss keeps the original
``_transition_to_fix`` path. Only SELF_CHECK feeds the ledger, so TEST /
INVARIANT_CHECK REVISION_NEEDED must stay on the fix loop untouched.

Covers:
  - trigger hit → ADJUDICATE step inserted and made current
  - miss → IMPLEMENT (fix loop) as before
  - periodic backstop forces ADJUDICATE every N fix iterations
  - ADJUDICATE is added to the sequence and survives a persistence round-trip
    (so --resume can recover at the break point)
  - TEST-sourced REVISION_NEEDED is never diverted to ADJUDICATE
  - the global max_fix_iterations bound still caps the flow
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from tianluo.config import WorkflowConfig
from tianluo.engine import adjudication
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.state_machine import StateMachine


_QUOTE = "handle the empty-input edge case"
_SELECTED = [
    StepType.IMPLEMENT,
    StepType.TEST,
    StepType.SELF_CHECK,
    StepType.COMMIT,
]


def _make_state_machine(tmp_path, cfg=None):
    with patch("tianluo.engine.state_machine.PersistenceManager"):
        sm = StateMachine(project_root=tmp_path)
    if cfg is not None:
        # Pin the workflow config so tests do not depend on an on-disk se3.yaml.
        sm._get_workflow_config = lambda: cfg  # type: ignore[assignment]
    return sm


def _issue(*, expected="returns None", path="a.py", line=1, quote=_QUOTE):
    return {
        "severity": "high",
        "actual_behavior": "broken behavior here",
        "expected_behavior": expected,
        "divergence": "concrete failure mode",
        "expectation_source": {"type": "task_description", "verbatim_quote": quote},
        "evidence_lines": [f"{path}:{line}"],
        "missing_in": [],
        "out_of_scope": False,
    }


def _make_flow(tmp_path, *, trigger_type=StepType.SELF_CHECK, issues=None):
    flow = FlowInstance(
        flow_id="adj-route-flow",
        task_description="Implement the parser and handle the empty-input edge case",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = list(_SELECTED)

    implement = Step(
        step_type=StepType.IMPLEMENT,
        status=StepStatus.COMPLETED,
        outputs={"files_changed": ["a.py"], "summary": "done"},
    )
    flow.state.add_step(implement)

    test = Step(
        step_type=StepType.TEST,
        status=StepStatus.COMPLETED,
        outputs={"test_results": {"passed": True, "overall_passed": True}},
    )
    flow.state.add_step(test)

    trigger_issues = issues if issues is not None else [_issue()]
    trigger = Step(
        step_type=trigger_type,
        status=StepStatus.REVISION_NEEDED,
        outputs={
            "fix_needed": True,
            "fix_instructions": "fix it",
            "fix_context": {"reason": "self_check", "issues": trigger_issues},
            "issues": trigger_issues,
        },
    )
    flow.state.add_step(trigger)
    flow.state.current_step_id = trigger.step_id
    flow.state.current_step_index = flow.state.selected_steps.index(trigger_type)
    return flow, implement, trigger


def _seed_oscillation(flow):
    """Seed the ledger so the *current* round (expected='returns zero') looks
    like an oscillation against a prior round (expected='returns None')."""
    adjudication.record_self_check_round(
        flow.state.context, [_issue(expected="returns None")], round_id="prior",
    )


# ---------------------------------------------------------------------------
# Routing: hit vs miss
# ---------------------------------------------------------------------------

class TestTriggerRouting:
    def test_oscillation_hit_routes_to_adjudicate(self, tmp_path):
        cfg = WorkflowConfig(max_fix_iterations=0, adjudicate_period=0)
        sm = _make_state_machine(tmp_path, cfg)
        flow, implement, trigger = _make_flow(
            tmp_path, issues=[_issue(expected="returns zero")]
        )
        _seed_oscillation(flow)
        # The current round must be on the ledger before trigger evaluation
        # (self_check records it in production); simulate that here.
        adjudication.record_self_check_round(
            flow.state.context, trigger.outputs["issues"], round_id="cur",
        )

        next_step = sm.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.ADJUDICATE
        assert flow.state.current_step_id == next_step.step_id
        # Inserted immediately before the SELF_CHECK slot.
        assert StepType.ADJUDICATE in flow.state.selected_steps
        adj_idx = flow.state.selected_steps.index(StepType.ADJUDICATE)
        assert flow.state.selected_steps[adj_idx + 1] == StepType.SELF_CHECK
        # Fix loop NOT entered: implement stays COMPLETED, no fix iteration.
        assert implement.status == StepStatus.COMPLETED
        assert flow.state.get_fix_iteration() == 0

    def test_miss_routes_to_fix_loop(self, tmp_path):
        cfg = WorkflowConfig(max_fix_iterations=10, adjudicate_period=0)
        sm = _make_state_machine(tmp_path, cfg)
        flow, implement, trigger = _make_flow(tmp_path)
        # Empty ledger apart from this round → no signal, period disabled → miss.
        adjudication.record_self_check_round(
            flow.state.context, trigger.outputs["issues"], round_id="cur",
        )

        next_step = sm.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.IMPLEMENT
        assert next_step.step_id == implement.step_id
        assert StepType.ADJUDICATE not in flow.state.selected_steps
        assert flow.state.get_fix_iteration() == 1

    def test_periodic_backstop_forces_adjudicate(self, tmp_path):
        """Every N fix iterations, adjudicate is forced even with no structural
        signal on the ledger."""
        cfg = WorkflowConfig(max_fix_iterations=0, adjudicate_period=5)
        sm = _make_state_machine(tmp_path, cfg)
        flow, implement, trigger = _make_flow(tmp_path)
        # No oscillation seed; just advance the fix-iteration counter to N.
        flow.state.fix_iterations = 5
        adjudication.record_self_check_round(
            flow.state.context, trigger.outputs["issues"], round_id="cur",
        )

        next_step = sm.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.ADJUDICATE
        # Baseline was reset to the current iteration so the next sweep waits N.
        ledger = flow.state.context[adjudication.LEDGER_KEY]
        assert ledger["period_baseline"] == 5

    def test_below_period_no_backstop(self, tmp_path):
        cfg = WorkflowConfig(max_fix_iterations=0, adjudicate_period=5)
        sm = _make_state_machine(tmp_path, cfg)
        flow, implement, trigger = _make_flow(tmp_path)
        flow.state.fix_iterations = 4  # one short of the period
        adjudication.record_self_check_round(
            flow.state.context, trigger.outputs["issues"], round_id="cur",
        )

        next_step = sm.transition_to_next(flow)

        assert next_step.step_type == StepType.IMPLEMENT


# ---------------------------------------------------------------------------
# Source gating: only SELF_CHECK feeds the triggers
# ---------------------------------------------------------------------------

class TestSourceGating:
    def test_test_revision_never_adjudicates(self, tmp_path):
        """A TEST-sourced REVISION_NEEDED must keep the fix routing even when the
        ledger is primed with an oscillation that WOULD fire for SELF_CHECK."""
        cfg = WorkflowConfig(max_fix_iterations=10, adjudicate_period=0)
        sm = _make_state_machine(tmp_path, cfg)
        flow, implement, trigger = _make_flow(
            tmp_path, trigger_type=StepType.TEST,
            issues=[_issue(expected="returns zero")],
        )
        _seed_oscillation(flow)

        next_step = sm.transition_to_next(flow)

        assert next_step.step_type == StepType.IMPLEMENT
        assert StepType.ADJUDICATE not in flow.state.selected_steps


# ---------------------------------------------------------------------------
# Resume / persistence + bound
# ---------------------------------------------------------------------------

class TestResumeAndBound:
    def test_adjudicate_survives_state_roundtrip(self, tmp_path):
        cfg = WorkflowConfig(max_fix_iterations=0, adjudicate_period=0)
        sm = _make_state_machine(tmp_path, cfg)
        flow, implement, trigger = _make_flow(
            tmp_path, issues=[_issue(expected="returns zero")]
        )
        _seed_oscillation(flow)
        adjudication.record_self_check_round(
            flow.state.context, trigger.outputs["issues"], round_id="cur",
        )
        adj_step = sm.transition_to_next(flow)
        assert adj_step.step_type == StepType.ADJUDICATE

        # Round-trip the state as --resume would: serialize + deserialize.
        restored = State.from_dict(flow.state.to_dict())
        assert StepType.ADJUDICATE in restored.selected_steps
        assert restored.current_step_id == adj_step.step_id
        resumed = restored.get_current_step()
        assert resumed is not None
        assert resumed.step_type == StepType.ADJUDICATE
        assert resumed.status == StepStatus.PENDING

    def test_max_fix_iterations_still_caps(self, tmp_path):
        """Even with an active oscillation, the global bound halts the flow when
        exhausted — adjudication must not let a diseased flow run forever."""
        cfg = WorkflowConfig(max_fix_iterations=3, adjudicate_period=0)
        sm = _make_state_machine(tmp_path, cfg)
        flow, implement, trigger = _make_flow(
            tmp_path, issues=[_issue(expected="returns zero")]
        )
        _seed_oscillation(flow)
        flow.state.fix_iterations = 3  # already at the bound
        with patch.object(sm, "_get_issue_discovery", return_value=None):
            next_step = sm.transition_to_next(flow)

        assert next_step is None
        assert flow.status == FlowStatus.FAILED
        assert StepType.ADJUDICATE not in flow.state.selected_steps
