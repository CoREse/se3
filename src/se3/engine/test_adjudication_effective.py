"""Tests for the adjudication effective-text layer (G5).

Co-located with the engine per the charter's engine-internal test exception:
these exercise ``_effective_task_description_base`` / ``_latest_adjudicated_output``
priority, the ``_build_step_inputs`` plan override, and the self_check
``_build_source_pool`` switch to the adjudicated text (so a new issue re-quoting
an abolished clause is dropped by validation).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from se3.engine.models import FlowInstance, Step, StepStatus, StepType
from se3.engine.state_machine import (
    StateMachine,
    _effective_task_description_base,
    _latest_adjudicated_output,
)
from se3.engine.steps.self_check import (
    _build_source_pool,
    _validate_and_filter_issues,
)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _completed(step_type: StepType, outputs: dict) -> Step:
    step = Step(step_type=step_type)
    step.status = StepStatus.COMPLETED
    step.outputs.update(outputs)
    return step


def _flow(task="original task text") -> FlowInstance:
    return FlowInstance(task_description=task)


def _add(flow: FlowInstance, step: Step) -> Step:
    flow.state.add_step(step)
    return step


def _issue(quote: str, expected: str = "return None", file="src/foo.py", line=42):
    """A well-formed self_check issue whose grounding survives everything
    except the verbatim_quote substring check."""
    return {
        "severity": "high",
        "actual_behavior": "returns 0",
        "expected_behavior": expected,
        "divergence": "when x is None",
        "expectation_source": {"type": "task_description", "verbatim_quote": quote},
        "evidence_lines": [f"{file}:{line}"],
        "missing_in": [],
    }


# --------------------------------------------------------------------------- #
# _latest_adjudicated_output
# --------------------------------------------------------------------------- #

def test_latest_adjudicated_output_none_without_ruling():
    flow = _flow()
    _add(flow, _completed(StepType.DISCOVERY, {"refined_description": "refined"}))
    assert _latest_adjudicated_output(flow, "adjudicated_description") is None


def test_latest_adjudicated_output_latest_wins():
    flow = _flow()
    _add(flow, _completed(StepType.ADJUDICATE, {"adjudicated_description": "gen1"}))
    _add(flow, _completed(StepType.ADJUDICATE, {"adjudicated_description": "gen2"}))
    assert _latest_adjudicated_output(flow, "adjudicated_description") == "gen2"


def test_latest_adjudicated_output_skips_empty_newer_generation():
    # gen2 only rewrote the plan (empty description) — the live description
    # override is still gen1's; an empty newer value must not veto it.
    flow = _flow()
    _add(flow, _completed(StepType.ADJUDICATE, {"adjudicated_description": "gen1"}))
    _add(flow, _completed(StepType.ADJUDICATE, {"adjudicated_plan": [{"group_id": "G1"}]}))
    assert _latest_adjudicated_output(flow, "adjudicated_description") == "gen1"


def test_latest_adjudicated_output_ignores_non_completed():
    flow = _flow()
    running = Step(step_type=StepType.ADJUDICATE)
    running.status = StepStatus.RUNNING
    running.outputs["adjudicated_description"] = "not yet"
    _add(flow, running)
    assert _latest_adjudicated_output(flow, "adjudicated_description") is None


# --------------------------------------------------------------------------- #
# _effective_task_description_base — priority adjudicated > refined > original
# --------------------------------------------------------------------------- #

def test_base_original_without_any_override():
    flow = _flow("original task text")
    assert _effective_task_description_base(flow) == "original task text"


def test_base_refined_overrides_original():
    flow = _flow("original")
    _add(flow, _completed(StepType.DISCOVERY, {"refined_description": "refined text"}))
    assert _effective_task_description_base(flow) == "refined text"


def test_base_adjudicated_overrides_refined_and_original():
    flow = _flow("original")
    _add(flow, _completed(StepType.DISCOVERY, {"refined_description": "refined text"}))
    _add(flow, _completed(StepType.ADJUDICATE, {"adjudicated_description": "ruled text"}))
    assert _effective_task_description_base(flow) == "ruled text"


def test_base_multi_generation_takes_latest_adjudication():
    flow = _flow("original")
    _add(flow, _completed(StepType.ADJUDICATE, {"adjudicated_description": "gen1"}))
    _add(flow, _completed(StepType.ADJUDICATE, {"adjudicated_description": "gen2"}))
    assert _effective_task_description_base(flow) == "gen2"


def test_base_plan_only_ruling_falls_through_to_refined():
    # A ruling that changed only the plan leaves the description at refined.
    flow = _flow("original")
    _add(flow, _completed(StepType.DISCOVERY, {"refined_description": "refined text"}))
    _add(flow, _completed(StepType.ADJUDICATE, {"adjudicated_plan": [{"group_id": "G1"}]}))
    assert _effective_task_description_base(flow) == "refined text"


# --------------------------------------------------------------------------- #
# _build_step_inputs — both call points pick up adjudicated text + plan
# --------------------------------------------------------------------------- #

def _sm() -> StateMachine:
    return StateMachine(Path(tempfile.mkdtemp()))


def test_build_step_inputs_uses_adjudicated_description():
    sm = _sm()
    flow = _flow("original")
    _add(flow, _completed(StepType.ADJUDICATE, {"adjudicated_description": "ruled desc"}))
    inputs = sm._build_step_inputs(flow, StepType.IMPLEMENT)
    assert inputs["task_description"] == "ruled desc"


def test_build_step_inputs_no_adjudication_unchanged():
    sm = _sm()
    flow = _flow("original")
    _add(flow, _completed(StepType.PLAN, {
        "plan": {"proposal": {}, "design": {}},
        "task_groups": [{"group_id": "G1", "tasks": []}],
    }))
    inputs = sm._build_step_inputs(flow, StepType.IMPLEMENT)
    assert inputs["task_description"] == "original"
    assert inputs["task_groups"] == [{"group_id": "G1", "tasks": []}]


def test_build_step_inputs_plan_override():
    sm = _sm()
    flow = _flow("original")
    _add(flow, _completed(StepType.PLAN, {
        "plan": {"proposal": {}, "design": {}},
        "task_groups": [{"group_id": "G1", "tasks": []}],
    }))
    ruled_plan = [{"group_id": "G1", "name": "ruled", "tasks": [{"description": "new"}]}]
    _add(flow, _completed(StepType.ADJUDICATE, {"adjudicated_plan": ruled_plan}))
    inputs = sm._build_step_inputs(flow, StepType.IMPLEMENT)
    assert inputs["task_groups"] == ruled_plan


def test_build_step_inputs_self_check_signals_adjudication():
    sm = _sm()
    flow = _flow("original")
    _add(flow, _completed(StepType.ADJUDICATE, {"adjudicated_description": "ruled desc"}))
    inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)
    assert inputs["task_description_base"] == "ruled desc"
    assert inputs["adjudicated_description"] == "ruled desc"


def test_transition_to_fix_uses_adjudicated_description():
    # The other effective-text call point: re-entering implement on the fix
    # loop must also see the ruled description, via _compose_effective_task_description.
    sm = _sm()
    flow = _flow("original")
    flow.state.selected_steps = [StepType.IMPLEMENT, StepType.SELF_CHECK]
    implement = _add(flow, _completed(StepType.IMPLEMENT, {"files_changed": ["a.py"]}))
    _add(flow, _completed(StepType.ADJUDICATE, {"adjudicated_description": "ruled desc"}))
    trigger = _add(flow, Step(step_type=StepType.SELF_CHECK))
    trigger.status = StepStatus.REVISION_NEEDED
    trigger.outputs.update({"fix_needed": True, "fix_instructions": "fix", "fix_context": {}})

    sm._transition_to_fix(flow, trigger)
    assert implement.inputs["task_description"] == "ruled desc"


# --------------------------------------------------------------------------- #
# _build_source_pool — switch to adjudicated text
# --------------------------------------------------------------------------- #

def test_source_pool_unchanged_without_adjudication():
    inputs = {
        "task_description_base": "refined base",
        "original_task_description": "canonical original",
    }
    pool = _build_source_pool(inputs)
    assert "refined base" in pool
    assert "canonical original" in pool


def test_source_pool_drops_original_when_adjudicated():
    inputs = {
        "task_description_base": "ruled base",  # already the adjudicated text
        "original_task_description": "canonical original with dead clause",
        "adjudicated_description": "ruled base",
    }
    pool = _build_source_pool(inputs)
    assert "ruled base" in pool
    assert "canonical original with dead clause" not in pool


# --------------------------------------------------------------------------- #
# _validate_and_filter_issues — dead-clause quotes dropped, live ones kept
# --------------------------------------------------------------------------- #

def test_dead_clause_issue_dropped_after_adjudication():
    # The abolished clause "raise when x is None" only appears in the
    # superseded original; with adjudication in effect it is out of the pool.
    inputs = {
        "task_description_base": "Return None when x is None",
        "original_task_description": "Return None when x is None, and raise when x is None",
        "adjudicated_description": "Return None when x is None",
        "changes_made": {"files_changed": ["src/foo.py"]},
    }
    kept, stats = _validate_and_filter_issues(
        [_issue(quote="raise when x is None")], inputs
    )
    assert kept == []
    assert stats["quote_not_in_source_count"] == 1


def test_live_clause_issue_kept_after_adjudication():
    inputs = {
        "task_description_base": "Return None when x is None",
        "original_task_description": "Return None when x is None, and raise when x is None",
        "adjudicated_description": "Return None when x is None",
        "changes_made": {"files_changed": ["src/foo.py"]},
    }
    kept, _ = _validate_and_filter_issues(
        [_issue(quote="Return None when x is None")], inputs
    )
    assert len(kept) == 1
