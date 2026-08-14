"""Implementation-strategy data contract and persistence tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tianluo.config import ConfigError
from tianluo.engine.implementation_strategy import (
    AUTO_FALLBACK_REASON,
    AUTO_NOT_REQUESTED_FALLBACK_REASON,
    AUTO_UNPARSEABLE_FALLBACK_REASON,
    EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY,
    IMPLEMENTATION_STRATEGY_FINALIZED_KEY,
    IMPLEMENTATION_STRATEGY_REASON_KEY,
    REQUESTED_IMPLEMENTATION_STRATEGY_KEY,
    ImplementationStrategyResolver,
)
from tianluo.engine.models import (
    EffectiveImplementationStrategy,
    FlowInstance,
    RequestedImplementationStrategy,
    StepStatus,
    StepType,
    get_default_step_sequence,
)
from tianluo.engine.schema import build_context_from_flow
from tianluo.engine.state_machine import StateMachine


@pytest.mark.parametrize("task_type", ["feature", "bugfix", "discovery"])
def test_only_plan_to_implement_types_have_strategy_surface(task_type: str):
    assert ImplementationStrategyResolver.has_choice_surface(task_type)
    sequence = get_default_step_sequence(task_type)
    assert StepType.PLAN in sequence
    assert StepType.IMPLEMENT in sequence


@pytest.mark.parametrize("task_type", ["small", "review", "survey", "pending"])
def test_types_without_plan_to_implement_surface_are_not_applicable(task_type: str):
    assert not ImplementationStrategyResolver.has_choice_surface(task_type)


def test_choice_surface_is_derived_from_the_default_sequence():
    # The criterion is the default sequence itself: ``get_default_step_sequence``
    # falls back to the feature sequence for types without their own table
    # entry (investigate/refactor reached via preset or the engine API), and
    # those flows really do run PLAN -> IMPLEMENT — so a direct request must
    # honor them instead of reading "not applicable".
    assert ImplementationStrategyResolver.has_choice_surface("investigate")
    assert ImplementationStrategyResolver.has_choice_surface("refactor")


def test_pending_type_never_inherits_the_fallback_surface():
    # "pending" defers strategy resolution to the ANALYZE-resolved type; it
    # must never be classified by the fallback sequence.
    assert not ImplementationStrategyResolver.has_choice_surface("pending")


def test_direct_request_on_fallback_sequence_type_removes_plan(tmp_path: Path):
    flow = StateMachine(tmp_path).create_flow(
        "task",
        task_type="refactor",
        implementation_strategy="direct",
    )
    assert flow.state.context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == "direct"
    assert StepType.PLAN not in flow.state.selected_steps
    assert StepType.CONFIRM not in flow.state.selected_steps
    assert StepType.IMPLEMENT in flow.state.selected_steps


@pytest.mark.parametrize(
    ("explicit", "configured", "expected"),
    [
        ("auto", "direct", RequestedImplementationStrategy.AUTO),
        ("direct", "planned", RequestedImplementationStrategy.DIRECT),
        ("planned", "auto", RequestedImplementationStrategy.PLANNED),
        (None, "auto", RequestedImplementationStrategy.AUTO),
        (None, "direct", RequestedImplementationStrategy.DIRECT),
        (None, "planned", RequestedImplementationStrategy.PLANNED),
        (None, None, RequestedImplementationStrategy.PLANNED),
    ],
)
def test_requested_strategy_priority(explicit, configured, expected):
    assert (
        ImplementationStrategyResolver.resolve_requested(explicit, configured)
        is expected
    )


@pytest.mark.parametrize("task_type", ["small", "review", "survey"])
def test_new_non_applicable_flow_persists_distinct_effective_value(
    tmp_path: Path,
    task_type: str,
):
    machine = StateMachine(tmp_path)
    flow = machine.create_flow(
        "task",
        task_type=task_type,
        implementation_strategy="direct",
    )

    assert flow.state.context[REQUESTED_IMPLEMENTATION_STRATEGY_KEY] == "direct"
    assert (
        flow.state.context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY]
        == "not_applicable"
    )
    assert flow.state.context[IMPLEMENTATION_STRATEGY_REASON_KEY]


def test_direct_new_flow_rewrites_plan_segment(tmp_path: Path):
    machine = StateMachine(tmp_path)
    flow = machine.create_flow(
        "task",
        task_type="feature",
        implementation_strategy="direct",
    )

    assert flow.state.context[REQUESTED_IMPLEMENTATION_STRATEGY_KEY] == "direct"
    assert flow.state.context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == "direct"
    assert StepType.PLAN not in flow.state.selected_steps
    assert StepType.CONFIRM not in flow.state.selected_steps
    assert StepType.IMPLEMENT in flow.state.selected_steps


@pytest.mark.parametrize("task_type", ["feature", "bugfix", "discovery"])
def test_direct_keeps_all_non_plan_default_gates(tmp_path: Path, task_type: str):
    flow = StateMachine(tmp_path).create_flow(
        "task",
        task_type=task_type,
        implementation_strategy="direct",
    )

    expected = [
        step
        for step in get_default_step_sequence(task_type)
        if step is not StepType.PLAN
    ]
    assert flow.state.selected_steps == expected


def test_planned_keeps_plan_and_its_confirmation(tmp_path: Path):
    flow = StateMachine(tmp_path).create_flow(
        "task",
        task_type="feature",
        implementation_strategy="planned",
    )

    plan_index = flow.state.selected_steps.index(StepType.PLAN)
    assert flow.state.selected_steps[plan_index + 1] is StepType.CONFIRM


def test_direct_keeps_e2e_and_worktree_merge_gates(tmp_path: Path):
    (tmp_path / "tianluo.yaml").write_text(
        "e2e:\n  enabled: true\n",
        encoding="utf-8",
    )
    flow = StateMachine(tmp_path).create_flow(
        "task",
        task_type="feature",
        is_worktree_mode=True,
        implementation_strategy="direct",
    )

    assert StepType.PLAN not in flow.state.selected_steps
    assert StepType.E2E in flow.state.selected_steps
    commit_index = flow.state.selected_steps.index(StepType.COMMIT)
    assert flow.state.selected_steps[commit_index + 1 : commit_index + 3] == [
        StepType.MERGE_INTEGRATE,
        StepType.VERSION_RECONCILE,
    ]


@pytest.mark.parametrize("task_type", ["small", "review", "survey"])
def test_non_applicable_sequence_is_unchanged_by_direct_request(
    tmp_path: Path,
    task_type: str,
):
    flow = StateMachine(tmp_path).create_flow(
        "task",
        task_type=task_type,
        implementation_strategy="direct",
    )

    assert flow.state.selected_steps == get_default_step_sequence(task_type)


def test_auto_remains_pending_for_applicable_flow_until_analyze(tmp_path: Path):
    flow = StateMachine(tmp_path).create_flow(
        "task",
        task_type="feature",
        implementation_strategy="auto",
    )
    assert flow.state.context[REQUESTED_IMPLEMENTATION_STRATEGY_KEY] == "auto"
    assert flow.state.context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] is None


def test_pending_type_defers_effective_resolution(tmp_path: Path):
    flow = StateMachine(tmp_path).create_flow(
        "task",
        task_type="pending",
        implementation_strategy="planned",
    )
    assert flow.state.context[REQUESTED_IMPLEMENTATION_STRATEGY_KEY] == "planned"
    assert flow.state.context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] is None


def test_invalid_explicit_request_uses_config_error_contract(tmp_path: Path):
    with pytest.raises(ConfigError, match="implementation strategy request"):
        StateMachine(tmp_path).create_flow(
            "task",
            task_type="feature",
            implementation_strategy="automatic",
        )


def test_strategy_round_trips_through_hot_cold_persistence(tmp_path: Path):
    machine = StateMachine(tmp_path)
    flow = machine.create_flow(
        "task",
        task_type="feature",
        implementation_strategy="direct",
    )

    restored = machine.persistence.load_flow()
    assert restored is not None
    assert restored.state.context[REQUESTED_IMPLEMENTATION_STRATEGY_KEY] == "direct"
    assert restored.state.context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == "direct"
    assert restored.state.context[IMPLEMENTATION_STRATEGY_REASON_KEY]


def test_resume_keeps_persisted_effective_after_config_change(tmp_path: Path):
    config_path = tmp_path / "tianluo.yaml"
    config_path.write_text(
        "workflow:\n  implementation_strategy: direct\n",
        encoding="utf-8",
    )
    first_machine = StateMachine(tmp_path)
    original = first_machine.create_flow("task", task_type="feature")
    assert original.state.context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == "direct"

    config_path.write_text(
        "workflow:\n  implementation_strategy: planned\n",
        encoding="utf-8",
    )
    resumed, is_resumed = StateMachine(tmp_path).load_or_create_flow()

    assert is_resumed is True
    assert resumed.flow_id == original.flow_id
    assert resumed.state.context[REQUESTED_IMPLEMENTATION_STRATEGY_KEY] == "direct"
    assert resumed.state.context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == "direct"
    assert StepType.PLAN not in resumed.state.selected_steps


def test_legacy_resume_does_not_rewrite_or_add_strategy_context(tmp_path: Path):
    machine = StateMachine(tmp_path)
    legacy = FlowInstance(task_description="legacy", task_type="feature")
    legacy.state.selected_steps = [
        StepType.ANALYZE,
        StepType.PLAN,
        StepType.CONFIRM,
        StepType.IMPLEMENT,
    ]
    original_steps = list(legacy.state.selected_steps)
    machine.persistence.save_flow(legacy)

    resumed, is_resumed = machine.load_or_create_flow(
        "replacement task",
        implementation_strategy="direct",
    )

    assert is_resumed is True
    assert resumed.state.selected_steps == original_steps
    assert REQUESTED_IMPLEMENTATION_STRATEGY_KEY not in resumed.state.context
    assert EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY not in resumed.state.context


def test_persist_effective_is_write_once():
    context = {
        REQUESTED_IMPLEMENTATION_STRATEGY_KEY: "auto",
        EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY: None,
        IMPLEMENTATION_STRATEGY_REASON_KEY: "pending",
    }
    first = ImplementationStrategyResolver.persist_effective(
        context,
        "direct",
        "analysis recommends a holistic call",
    )
    second = ImplementationStrategyResolver.persist_effective(
        context,
        "planned",
        "new config should not replace the persisted decision",
    )

    assert first.effective is EffectiveImplementationStrategy.DIRECT
    assert second.effective is EffectiveImplementationStrategy.DIRECT
    assert context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == "direct"
    assert context[IMPLEMENTATION_STRATEGY_REASON_KEY] == (
        "analysis recommends a holistic call"
    )


def test_auto_analyze_recommendation_is_persisted_once():
    context = {
        REQUESTED_IMPLEMENTATION_STRATEGY_KEY: "auto",
        EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY: None,
        IMPLEMENTATION_STRATEGY_REASON_KEY: "pending",
    }

    first = ImplementationStrategyResolver.finalize_for_analyze(
        context,
        task_type="feature",
        analyze_output={
            "implementation_strategy": "direct",
            "strategy_reason": "One autonomous call is appropriate.",
        },
        recommendation_requested=True,
    )
    second = ImplementationStrategyResolver.finalize_for_analyze(
        context,
        task_type="feature",
        analyze_output={
            "implementation_strategy": "planned",
            "implementation_strategy_reason": "A later retry suggested otherwise.",
        },
        recommendation_requested=True,
    )

    assert first.effective is EffectiveImplementationStrategy.DIRECT
    assert second.effective is EffectiveImplementationStrategy.DIRECT
    assert context[IMPLEMENTATION_STRATEGY_REASON_KEY] == (
        "One autonomous call is appropriate."
    )


def test_spontaneous_recommendation_is_honored_and_reason_stays_truthful():
    # The question was not carried, yet the response volunteered a valid
    # choice: honoring it beats defaulting to planned with a usable answer in
    # hand. And when nothing is volunteered, the recorded reason must not
    # assert a request that never happened — it is shown verbatim in the CLI
    # summary, the WebUI and history JSON.
    volunteered = {
        REQUESTED_IMPLEMENTATION_STRATEGY_KEY: "auto",
        EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY: "not_applicable",
        IMPLEMENTATION_STRATEGY_REASON_KEY: "provisional",
    }
    view = ImplementationStrategyResolver.finalize_for_analyze(
        volunteered,
        task_type="feature",
        analyze_output={
            "implementation_strategy": "direct",
            "strategy_reason": "One autonomous call carries it.",
        },
        recommendation_requested=False,
    )
    assert view.effective is EffectiveImplementationStrategy.DIRECT
    assert view.reason == "One autonomous call carries it."

    silent = {
        REQUESTED_IMPLEMENTATION_STRATEGY_KEY: "auto",
        EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY: "not_applicable",
        IMPLEMENTATION_STRATEGY_REASON_KEY: "provisional",
    }
    fallback = ImplementationStrategyResolver.finalize_for_analyze(
        silent,
        task_type="feature",
        analyze_output={},
        recommendation_requested=False,
    )
    assert fallback.effective is EffectiveImplementationStrategy.PLANNED
    assert fallback.reason == AUTO_NOT_REQUESTED_FALLBACK_REASON
    assert fallback.reason != AUTO_FALLBACK_REASON


def test_auto_gate_survives_a_provisional_not_applicable_effective():
    # initialize_context stamps a PROVISIONAL not_applicable for a no-surface
    # creation-time type; that is not a decision, and it must not silence the
    # question ANALYZE may need answered after reclassification.
    context = {}
    ImplementationStrategyResolver.initialize_context(
        context,
        task_type="small",
        selected_steps=get_default_step_sequence("small"),
        explicit_request="auto",
    )
    assert context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == "not_applicable"
    assert ImplementationStrategyResolver.should_request_auto_recommendation(
        context, task_type="small",
    )

    ImplementationStrategyResolver.finalize_for_analyze(
        context,
        task_type="small",
        analyze_output={},
        recommendation_requested=True,
    )
    # Finalized: the one-time decision closes the gate.
    assert not ImplementationStrategyResolver.should_request_auto_recommendation(
        context, task_type="small",
    )


@pytest.mark.parametrize("recommendation", ["direct", "direct ", " Direct", "DIRECT"])
def test_auto_recommendation_tolerates_whitespace_and_case(recommendation):
    context = {
        REQUESTED_IMPLEMENTATION_STRATEGY_KEY: "auto",
        EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY: None,
        IMPLEMENTATION_STRATEGY_REASON_KEY: "pending",
    }

    result = ImplementationStrategyResolver.finalize_for_analyze(
        context,
        task_type="feature",
        analyze_output={
            "implementation_strategy": recommendation,
            "strategy_reason": "single coherent task",
        },
        recommendation_requested=True,
    )

    assert result.effective is EffectiveImplementationStrategy.DIRECT
    assert context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == "direct"


@pytest.mark.parametrize(
    ("analyze_output", "expected_reason"),
    [
        ({}, AUTO_FALLBACK_REASON),
        ({"implementation_strategy": ""}, AUTO_FALLBACK_REASON),
        (
            {"implementation_strategy": "automatic"},
            AUTO_UNPARSEABLE_FALLBACK_REASON,
        ),
        (
            {"implementation_strategy": "direct"},
            AUTO_UNPARSEABLE_FALLBACK_REASON,
        ),
        (
            {
                "implementation_strategy": "direct",
                "implementation_strategy_reason": "   ",
            },
            AUTO_UNPARSEABLE_FALLBACK_REASON,
        ),
    ],
)
def test_auto_invalid_or_incomplete_result_falls_back_to_planned(
    analyze_output, expected_reason
):
    context = {
        REQUESTED_IMPLEMENTATION_STRATEGY_KEY: "auto",
        EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY: None,
        IMPLEMENTATION_STRATEGY_REASON_KEY: "pending",
    }

    result = ImplementationStrategyResolver.finalize_for_analyze(
        context,
        task_type="feature",
        analyze_output=analyze_output,
        recommendation_requested=True,
    )

    assert result.effective is EffectiveImplementationStrategy.PLANNED
    assert result.reason == expected_reason


@pytest.mark.parametrize(
    ("task_type", "steps", "expected"),
    [
        (
            "feature",
            [StepType.ANALYZE, StepType.PLAN, StepType.IMPLEMENT],
            EffectiveImplementationStrategy.PLANNED,
        ),
        (
            "bugfix",
            ["analyze", "plan", "confirm", "implement"],
            EffectiveImplementationStrategy.PLANNED,
        ),
        (
            "small",
            [StepType.ANALYZE, StepType.IMPLEMENT],
            EffectiveImplementationStrategy.NOT_APPLICABLE,
        ),
        (
            "review",
            [StepType.ANALYZE, StepType.INVARIANT_CHECK],
            EffectiveImplementationStrategy.NOT_APPLICABLE,
        ),
        (
            "survey",
            [StepType.ANALYZE, StepType.INVESTIGATE],
            EffectiveImplementationStrategy.NOT_APPLICABLE,
        ),
    ],
)
def test_legacy_inference_is_read_only(task_type, steps, expected):
    context = {"unrelated": {"preserved": True}}
    before_context = copy.deepcopy(context)
    before_steps = list(steps)

    view = ImplementationStrategyResolver.view(
        context,
        task_type=task_type,
        selected_steps=steps,
    )

    assert view.effective is expected
    assert view.inferred is True
    assert context == before_context
    assert steps == before_steps


def test_context_detail_projection_infers_legacy_without_mutating_flow_dict():
    flow_dict = {
        "flow_id": "legacy-flow",
        "status": "running",
        "task_description": "legacy task",
        "task_type": "feature",
        "updated_at": "2026-08-13T00:00:00",
        "state": {
            "steps": {},
            "step_history": [],
            "selected_steps": ["analyze", "plan", "implement"],
            "context": {"legacy": True},
        },
    }
    original = copy.deepcopy(flow_dict)

    detail = build_context_from_flow(flow_dict)

    assert detail[REQUESTED_IMPLEMENTATION_STRATEGY_KEY] == "planned"
    assert detail[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == "planned"
    assert detail[IMPLEMENTATION_STRATEGY_REASON_KEY]
    assert flow_dict == original


def test_progress_projection_exposes_persisted_strategy(tmp_path: Path):
    machine = StateMachine(tmp_path)
    flow = machine.create_flow(
        "task",
        task_type="feature",
        implementation_strategy="direct",
    )
    progress = machine.get_progress(flow)
    assert progress[REQUESTED_IMPLEMENTATION_STRATEGY_KEY] == "direct"
    assert progress[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == "direct"


def _run_strategy_analyze(
    project_root: Path,
    *,
    requested: str,
    task_type: str,
    analyze_output: dict,
):
    from tianluo.engine.steps import analyze as analyze_mod
    import tianluo.engine.context_builder as context_builder

    flow = StateMachine(project_root).create_flow(
        "task",
        task_type=task_type,
        implementation_strategy=requested,
    )
    step = flow.state.get_current_step()
    assert step is not None and step.step_type is StepType.ANALYZE

    with patch.object(analyze_mod, "_collect_project_summary", return_value="ctx"), \
        patch.object(analyze_mod, "LLMCaller") as caller_cls, \
        patch.object(context_builder, "get_issue_discovery_injection", return_value=""), \
        patch.object(context_builder, "get_charter_injection", return_value=""), \
        patch.object(context_builder, "get_code_index_injection", return_value=""), \
        patch.object(context_builder, "ensure_code_index_fresh", return_value=None), \
        patch.object(context_builder, "get_runtime_environment_injection", return_value=""):
        caller_cls.return_value.call.return_value = json.dumps(analyze_output)
        status = analyze_mod.analyze_handler(step, flow)

    return flow, step, status, caller_cls.return_value.call.call_args.kwargs


def test_failed_sequence_rebuild_unwinds_the_finalized_strategy(tmp_path: Path):
    # The finalized decision and the sequence it implies must be persisted
    # atomically: a stamped effective='direct' beside a sequence still holding
    # PLAN is exactly the dangling-PLAN state the transform prevents, and a
    # later Skip of ANALYZE would execute it.
    from tianluo.engine.steps import analyze as analyze_mod
    import tianluo.engine.context_builder as context_builder

    flow = StateMachine(tmp_path).create_flow(
        "task", task_type="feature", implementation_strategy="auto",
    )
    step = flow.state.get_current_step()
    assert step is not None
    assert StepType.PLAN in flow.state.selected_steps

    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected sequence shape")

    with patch.object(analyze_mod, "_collect_project_summary", return_value="ctx"), \
        patch.object(analyze_mod, "LLMCaller") as caller_cls, \
        patch.object(analyze_mod, "_update_flow_steps", side_effect=boom), \
        patch.object(context_builder, "get_issue_discovery_injection", return_value=""), \
        patch.object(context_builder, "get_charter_injection", return_value=""), \
        patch.object(context_builder, "get_code_index_injection", return_value=""), \
        patch.object(context_builder, "ensure_code_index_fresh", return_value=None), \
        patch.object(context_builder, "get_runtime_environment_injection", return_value=""):
        caller_cls.return_value.call.return_value = json.dumps(
            {
                "task_type": "feature",
                "root_cause_clear": True,
                "implementation_strategy": "direct",
                "strategy_reason": "One call carries it.",
            }
        )
        status = analyze_mod.analyze_handler(step, flow)

    assert status is StepStatus.FAILED
    assert StepType.PLAN in flow.state.selected_steps
    # No finalized 'direct' may survive beside a sequence that still has PLAN.
    assert flow.state.context.get(IMPLEMENTATION_STRATEGY_FINALIZED_KEY) is not True
    assert flow.state.context.get(EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY) != "direct"
    # The pre-finalization request survives untouched for the retry.
    assert flow.state.context[REQUESTED_IMPLEMENTATION_STRATEGY_KEY] == "auto"


def test_auto_analyze_prompt_covers_decision_dimensions_and_persists_direct(
    tmp_path: Path,
):
    flow, step, status, call_kwargs = _run_strategy_analyze(
        tmp_path,
        requested="auto",
        task_type="feature",
        analyze_output={
            "task_type": "feature",
            "scope": "engine",
            "complexity": "medium",
            "reasoning": "bounded change",
            "root_cause_clear": True,
            "implementation_strategy": "direct",
            "implementation_strategy_reason": "A single call is reasonable.",
        },
    )

    assert status is StepStatus.COMPLETED
    prompt = call_kwargs["prompt"]
    for dimension in (
        "task scale",
        "module coupling",
        "dependency-chain depth",
        "independent worktrees",
        "fine-grained task groups",
        "one autonomous implementation call",
    ):
        assert dimension in prompt
    assert "implementation_strategy" in call_kwargs["json_schema_hint"]
    assert flow.state.context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == "direct"
    assert flow.state.context[IMPLEMENTATION_STRATEGY_REASON_KEY] == (
        "A single call is reasonable."
    )
    assert step.outputs[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == "direct"
    assert StepType.PLAN not in flow.state.selected_steps
    assert not ImplementationStrategyResolver.should_request_auto_recommendation(
        flow.state.context,
        task_type="feature",
    )


def test_auto_analyze_missing_recommendation_rebuilds_planned_sequence(
    tmp_path: Path,
):
    flow, _step, status, _call_kwargs = _run_strategy_analyze(
        tmp_path,
        requested="auto",
        task_type="feature",
        analyze_output={
            "task_type": "feature",
            "root_cause_clear": True,
        },
    )

    assert status is StepStatus.COMPLETED
    assert flow.state.context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == "planned"
    assert flow.state.context[IMPLEMENTATION_STRATEGY_REASON_KEY] == (
        AUTO_FALLBACK_REASON
    )
    plan_index = flow.state.selected_steps.index(StepType.PLAN)
    assert flow.state.selected_steps[plan_index + 1] is StepType.CONFIRM


@pytest.mark.parametrize("requested", ["direct", "planned"])
def test_explicit_strategy_does_not_request_analyze_recommendation(
    tmp_path: Path,
    requested: str,
):
    flow, _step, status, call_kwargs = _run_strategy_analyze(
        tmp_path,
        requested=requested,
        task_type="feature",
        analyze_output={
            "task_type": "feature",
            "root_cause_clear": True,
        },
    )

    assert status is StepStatus.COMPLETED
    assert "implementation_strategy" not in call_kwargs["prompt"]
    assert "implementation_strategy" not in call_kwargs["json_schema_hint"]
    assert flow.state.context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == requested


@pytest.mark.parametrize("task_type", ["small", "review", "survey"])
def test_planless_type_still_asks_but_finalizes_not_applicable(
    tmp_path: Path,
    task_type: str,
):
    # ANALYZE is what RESOLVES the task type, so an AUTO request always carries
    # the (conditional) question — a preset-created 'small' may be reclassified
    # into a choice-surface type. When the classification stays planless the
    # decision is still not_applicable and the sequence is untouched.
    flow, _step, status, call_kwargs = _run_strategy_analyze(
        tmp_path,
        requested="auto",
        task_type=task_type,
        analyze_output={
            "task_type": task_type,
            "root_cause_clear": True,
        },
    )

    assert status is StepStatus.COMPLETED
    assert "implementation_strategy" in call_kwargs["prompt"]
    assert flow.state.context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == (
        "not_applicable"
    )
    assert flow.state.selected_steps == get_default_step_sequence(task_type)


def test_reclassified_planless_flow_asks_and_records_the_recommendation(
    tmp_path: Path,
):
    # The divergence this gate exists for: a flow created as 'small' with
    # --implementation-strategy auto that ANALYZE reclassifies to 'feature'.
    # The question must have been asked, and the model's answer honored.
    flow, _step, status, call_kwargs = _run_strategy_analyze(
        tmp_path,
        requested="auto",
        task_type="small",
        analyze_output={
            "task_type": "feature",
            "root_cause_clear": True,
            "implementation_strategy": "direct",
            "strategy_reason": "One autonomous call carries it.",
        },
    )

    assert status is StepStatus.COMPLETED
    assert "implementation_strategy" in call_kwargs["prompt"]
    assert flow.state.context[EFFECTIVE_IMPLEMENTATION_STRATEGY_KEY] == "direct"
    assert flow.state.context[IMPLEMENTATION_STRATEGY_REASON_KEY] == (
        "One autonomous call carries it."
    )
    assert StepType.PLAN not in flow.state.selected_steps


def test_direct_bugfix_keeps_investigate_when_root_cause_is_unclear(tmp_path: Path):
    flow, _step, status, _call_kwargs = _run_strategy_analyze(
        tmp_path,
        requested="direct",
        task_type="bugfix",
        analyze_output={
            "task_type": "bugfix",
            "root_cause_clear": False,
        },
    )

    assert status is StepStatus.COMPLETED
    assert StepType.PLAN not in flow.state.selected_steps
    assert StepType.INVESTIGATE in flow.state.selected_steps
    assert flow.state.selected_steps.index(StepType.INVESTIGATE) < (
        flow.state.selected_steps.index(StepType.IMPLEMENT)
    )
