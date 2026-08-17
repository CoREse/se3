"""PLAN decomposition/granularity contract, persistence and single-path routing.

Replaces ``test_implementation_strategy.py``. The retired axis was tested for
what it *removed* from the step sequence; this model removes nothing, so the
tests here assert the opposite invariant — every PLAN-bearing task type keeps
its PLAN no matter what the plan mode says.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tianluo.config import ConfigError, WorkflowConfig
from tianluo.engine import plan_decomposition as plan_mode_mod
from tianluo.engine.plan_decomposition import (
    LEGACY_EFFECTIVE_STRATEGY_KEY,
    PLAN_DECOMPOSITION_KEY,
    PLAN_GRANULARITY_KEY,
    PLAN_MODE_REASON_KEY,
    PlanDecomposition,
    PlanGranularity,
    PlanModeError,
    PlanModeResolver,
    PlanModeView,
)
from tianluo.engine.models import (
    FlowInstance,
    Step,
    StepStatus,
    StepType,
    get_default_step_sequence,
)
from tianluo.engine.schema import build_context_from_flow
from tianluo.engine.state_machine import StateMachine


PLAN_BEARING_TYPES = ("feature", "bugfix", "discovery")
PLANLESS_TYPES = ("small", "review", "survey")


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


def test_module_exposes_no_step_sequence_transform():
    """The new model must be structurally incapable of rewriting a sequence.

    This is the whole reason the module was created rather than renamed: the
    old resolver owned ``apply_to_steps``, and keeping that API alive in the
    new namespace would leave a loaded gun for a future caller.
    """
    source = inspect.getsource(plan_mode_mod)
    # It cannot touch a sequence it never imports the vocabulary for.
    assert "from .models import" not in source
    assert "get_default_step_sequence" not in source
    assert "apply_to_steps" not in source
    assert "selected_steps" not in source
    for name in ("snapshot_context", "restore_context", "infer_legacy_effective"):
        assert not hasattr(PlanModeResolver, name)


def test_retired_module_is_gone():
    with pytest.raises(ImportError):
        __import__("tianluo.engine.implementation_strategy")


def test_engine_package_exports_only_the_new_symbols():
    import tianluo.engine as engine

    assert engine.PlanModeResolver is PlanModeResolver
    for stale in (
        "ImplementationStrategyResolver",
        "ImplementationStrategyView",
        "ImplementationStrategyError",
        "RequestedImplementationStrategy",
        "EffectiveImplementationStrategy",
    ):
        assert not hasattr(engine, stale)
        assert stale not in engine.__all__


# ---------------------------------------------------------------------------
# resolve_requested: explicit > configuration > default
# ---------------------------------------------------------------------------


def test_defaults_are_capability_auto():
    assert PlanModeResolver.resolve_requested() == (
        PlanDecomposition.CAPABILITY,
        PlanGranularity.AUTO,
    )


def test_explicit_request_wins_over_configuration():
    configured = WorkflowConfig(
        plan_decomposition="granular",
        plan_granularity="conservative",
        plan_decomposition_explicit=True,
        plan_granularity_explicit=True,
    )
    assert PlanModeResolver.resolve_requested(
        "capability", "single", configured
    ) == (PlanDecomposition.CAPABILITY, PlanGranularity.SINGLE)


def test_configuration_wins_over_default():
    configured = WorkflowConfig(
        plan_decomposition="granular",
        plan_granularity="single",
        plan_decomposition_explicit=True,
        plan_granularity_explicit=True,
    )
    assert PlanModeResolver.resolve_requested(None, None, configured) == (
        PlanDecomposition.GRANULAR,
        PlanGranularity.SINGLE,
    )


def test_axes_resolve_independently():
    """An explicit granularity must not drag the doctrine off configuration."""
    configured = WorkflowConfig(
        plan_decomposition="granular",
        plan_decomposition_explicit=True,
    )
    assert PlanModeResolver.resolve_requested(None, "single", configured) == (
        PlanDecomposition.GRANULAR,
        PlanGranularity.SINGLE,
    )


def test_mapping_shaped_configuration_is_accepted():
    assert PlanModeResolver.resolve_requested(
        None, None, {"plan_decomposition": "granular"}
    ) == (PlanDecomposition.GRANULAR, PlanGranularity.AUTO)


@pytest.mark.parametrize(
    ("decomposition", "granularity"),
    [("direct", None), ("planned", None), (None, "planned"), (None, "medium")],
)
def test_invalid_values_raise_plan_mode_error_listing_the_legal_set(
    decomposition, granularity
):
    with pytest.raises(PlanModeError) as excinfo:
        PlanModeResolver.resolve_requested(decomposition, granularity)
    message = str(excinfo.value)
    if decomposition is not None:
        assert "capability" in message and "granular" in message
    else:
        assert "auto" in message and "single" in message
        assert "conservative" in message


def test_invalid_configured_value_names_the_config_path():
    with pytest.raises(PlanModeError) as excinfo:
        PlanModeResolver.resolve_requested(
            None, None, {"plan_granularity": "coarse"}
        )
    assert "workflow.plan_granularity" in str(excinfo.value)


# ---------------------------------------------------------------------------
# initialize_context: write-once persistence
# ---------------------------------------------------------------------------


def test_initialize_context_persists_all_three_keys():
    context: dict = {}
    view = PlanModeResolver.initialize_context(
        context, explicit_decomposition="granular", explicit_granularity="single"
    )
    assert context[PLAN_DECOMPOSITION_KEY] == "granular"
    assert context[PLAN_GRANULARITY_KEY] == "single"
    assert context[PLAN_MODE_REASON_KEY]
    assert view.decomposition is PlanDecomposition.GRANULAR
    assert view.granularity is PlanGranularity.SINGLE


def test_initialize_context_is_write_once():
    context = {
        PLAN_DECOMPOSITION_KEY: "capability",
        PLAN_GRANULARITY_KEY: "conservative",
        PLAN_MODE_REASON_KEY: "recorded earlier",
    }
    view = PlanModeResolver.initialize_context(
        context, explicit_decomposition="granular", explicit_granularity="single"
    )
    assert view.decomposition is PlanDecomposition.CAPABILITY
    assert view.granularity is PlanGranularity.CONSERVATIVE
    assert context[PLAN_DECOMPOSITION_KEY] == "capability"
    assert context[PLAN_GRANULARITY_KEY] == "conservative"
    assert context[PLAN_MODE_REASON_KEY] == "recorded earlier"


def test_reason_records_which_source_won():
    explicit = PlanModeResolver.initialize_context(
        {}, explicit_decomposition="granular"
    )
    assert "explicit request" in explicit.reason

    configured = PlanModeResolver.initialize_context(
        {},
        configured_workflow=WorkflowConfig(
            plan_granularity="single", plan_granularity_explicit=True
        ),
    )
    assert "project configuration" in configured.reason

    defaulted = PlanModeResolver.initialize_context({})
    assert "default" in defaulted.reason


def test_non_explicit_configuration_reads_as_default():
    """A config object at its defaults must not claim the project chose them."""
    view = PlanModeResolver.initialize_context(
        {}, configured_workflow=WorkflowConfig()
    )
    assert view.decomposition is PlanDecomposition.CAPABILITY
    assert "project configuration" not in view.reason


# ---------------------------------------------------------------------------
# view: read-only, legacy-tolerant
# ---------------------------------------------------------------------------


def test_view_returns_persisted_values():
    context = {
        PLAN_DECOMPOSITION_KEY: "granular",
        PLAN_GRANULARITY_KEY: "auto",
        PLAN_MODE_REASON_KEY: "because",
    }
    view = PlanModeResolver.view(context)
    assert view.decomposition is PlanDecomposition.GRANULAR
    assert view.reason == "because"
    assert view.legacy_strategy is None
    assert view.inferred is False


@pytest.mark.parametrize(
    ("legacy", "decomposition", "granularity"),
    [
        ("direct", PlanDecomposition.CAPABILITY, PlanGranularity.SINGLE),
        ("planned", PlanDecomposition.GRANULAR, PlanGranularity.AUTO),
        ("not_applicable", PlanDecomposition.CAPABILITY, PlanGranularity.AUTO),
    ],
)
def test_view_projects_legacy_flows_without_writing_back(
    legacy, decomposition, granularity
):
    context = {LEGACY_EFFECTIVE_STRATEGY_KEY: legacy}
    view = PlanModeResolver.view(context)
    assert view.legacy_strategy == legacy
    assert view.inferred is True
    assert view.decomposition is decomposition
    assert view.granularity is granularity
    # Non-mutating: describing an old flow must not upgrade it on disk.
    assert context == {LEGACY_EFFECTIVE_STRATEGY_KEY: legacy}


def test_view_of_an_empty_context_reports_defaults_as_inferred():
    view = PlanModeResolver.view({})
    assert view.inferred is True
    assert view.legacy_strategy is None
    assert view.decomposition is PlanDecomposition.CAPABILITY


def test_projection_carries_the_legacy_annotation():
    projection = PlanModeResolver.view(
        {LEGACY_EFFECTIVE_STRATEGY_KEY: "direct"}
    ).to_projection()
    assert projection["legacy_strategy"] == "direct"
    assert projection[PLAN_DECOMPOSITION_KEY] == "capability"
    # The write payload stays strictly the three context keys.
    assert set(PlanModeView(
        decomposition=PlanDecomposition.CAPABILITY,
        granularity=PlanGranularity.AUTO,
        reason="",
    ).to_dict()) == {
        PLAN_DECOMPOSITION_KEY,
        PLAN_GRANULARITY_KEY,
        PLAN_MODE_REASON_KEY,
    }


# ---------------------------------------------------------------------------
# Single-path routing at flow creation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task_type", PLAN_BEARING_TYPES)
@pytest.mark.parametrize(
    ("decomposition", "granularity"),
    [
        (None, None),
        ("capability", "single"),
        ("capability", "conservative"),
        ("granular", "auto"),
    ],
)
def test_plan_survives_every_plan_mode(
    tmp_path: Path, task_type, decomposition, granularity
):
    flow = StateMachine(tmp_path).create_flow(
        "task",
        task_type=task_type,
        plan_decomposition=decomposition,
        plan_granularity=granularity,
    )
    steps = flow.state.selected_steps
    assert StepType.PLAN in steps
    assert StepType.IMPLEMENT in steps
    # No plan-mode-driven trimming means no orphaned confirmation gate: every
    # CONFIRM still sits directly behind the step it guards.
    for index, step in enumerate(steps):
        if step is StepType.CONFIRM:
            assert index > 0 and steps[index - 1] is not StepType.CONFIRM


@pytest.mark.parametrize("task_type", PLAN_BEARING_TYPES)
def test_sequence_is_a_function_of_task_type_alone(tmp_path: Path, task_type):
    baseline = StateMachine(tmp_path / "a").create_flow(
        "task", task_type=task_type
    ).state.selected_steps
    single = StateMachine(tmp_path / "b").create_flow(
        "task",
        task_type=task_type,
        plan_decomposition="capability",
        plan_granularity="single",
    ).state.selected_steps
    assert single == baseline


@pytest.mark.parametrize("task_type", PLANLESS_TYPES)
def test_planless_sequences_are_untouched(tmp_path: Path, task_type):
    flow = StateMachine(tmp_path).create_flow(
        "task",
        task_type=task_type,
        plan_decomposition="capability",
        plan_granularity="single",
    )
    assert flow.state.selected_steps == list(get_default_step_sequence(task_type))


def test_fallback_sequence_types_also_keep_plan(tmp_path: Path):
    # ``refactor`` has no table entry and falls back to the feature sequence;
    # it must still be planned rather than silently collapsed.
    flow = StateMachine(tmp_path).create_flow(
        "task", task_type="refactor", plan_granularity="single"
    )
    assert StepType.PLAN in flow.state.selected_steps


def test_new_flow_persists_the_plan_mode_into_context(tmp_path: Path):
    flow = StateMachine(tmp_path).create_flow(
        "task",
        task_type="feature",
        plan_decomposition="granular",
        plan_granularity="conservative",
    )
    assert flow.state.context[PLAN_DECOMPOSITION_KEY] == "granular"
    assert flow.state.context[PLAN_GRANULARITY_KEY] == "conservative"
    assert flow.state.context[PLAN_MODE_REASON_KEY]


def test_invalid_explicit_request_uses_the_config_error_contract(tmp_path: Path):
    with pytest.raises(ConfigError) as excinfo:
        StateMachine(tmp_path).create_flow(
            "task", task_type="feature", plan_decomposition="direct"
        )
    assert "capability" in str(excinfo.value)


def test_plan_mode_round_trips_through_hot_cold_persistence(tmp_path: Path):
    machine = StateMachine(tmp_path)
    machine.create_flow(
        "task",
        task_type="feature",
        plan_decomposition="granular",
        plan_granularity="single",
    )

    restored = machine.persistence.load_flow()
    assert restored is not None
    assert restored.state.context[PLAN_DECOMPOSITION_KEY] == "granular"
    assert restored.state.context[PLAN_GRANULARITY_KEY] == "single"
    assert restored.state.context[PLAN_MODE_REASON_KEY]


def test_resume_keeps_the_persisted_plan_mode_after_a_config_change(tmp_path: Path):
    config_path = tmp_path / "tianluo.yaml"
    config_path.write_text(
        "workflow:\n  plan_decomposition: granular\n", encoding="utf-8"
    )
    original = StateMachine(tmp_path).create_flow("task", task_type="feature")
    assert original.state.context[PLAN_DECOMPOSITION_KEY] == "granular"

    config_path.write_text(
        "workflow:\n  plan_decomposition: capability\n", encoding="utf-8"
    )
    resumed, is_resumed = StateMachine(tmp_path).load_or_create_flow()

    assert is_resumed is True
    assert resumed.flow_id == original.flow_id
    assert resumed.state.context[PLAN_DECOMPOSITION_KEY] == "granular"


def test_legacy_direct_flow_resume_does_not_rewrite_its_sequence(tmp_path: Path):
    """A flow created under the retired axis keeps its recorded path verbatim.

    The old ``direct`` path really did persist a PLAN-less sequence; resuming
    it under the single-path model must describe that state, never repair it.
    """
    machine = StateMachine(tmp_path)
    legacy = FlowInstance(task_description="legacy", task_type="feature")
    legacy.state.selected_steps = [
        StepType.ANALYZE,
        StepType.IMPLEMENT,
        StepType.TEST,
        StepType.COMMIT,
    ]
    legacy.state.context["requested_implementation_strategy"] = "direct"
    legacy.state.context[LEGACY_EFFECTIVE_STRATEGY_KEY] = "direct"
    original_steps = list(legacy.state.selected_steps)
    machine.persistence.save_flow(legacy)

    resumed, is_resumed = machine.load_or_create_flow(
        "replacement task",
        plan_decomposition="granular",
        plan_granularity="conservative",
    )

    assert is_resumed is True
    assert resumed.state.selected_steps == original_steps
    assert PLAN_DECOMPOSITION_KEY not in resumed.state.context
    assert PLAN_GRANULARITY_KEY not in resumed.state.context
    assert resumed.state.context[LEGACY_EFFECTIVE_STRATEGY_KEY] == "direct"


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


def test_progress_projection_exposes_the_persisted_plan_mode(tmp_path: Path):
    machine = StateMachine(tmp_path)
    flow = machine.create_flow(
        "task",
        task_type="feature",
        plan_decomposition="capability",
        plan_granularity="single",
    )
    progress = machine.get_progress(flow)
    assert progress[PLAN_DECOMPOSITION_KEY] == "capability"
    assert progress[PLAN_GRANULARITY_KEY] == "single"
    assert progress["legacy_strategy"] is None


def test_context_projection_reads_group_count_off_the_plan_output():
    flow = FlowInstance(task_description="t", task_type="feature")
    flow.state.context[PLAN_DECOMPOSITION_KEY] = "capability"
    flow.state.context[PLAN_GRANULARITY_KEY] = "auto"
    plan = Step(
        step_type=StepType.PLAN,
        status=StepStatus.COMPLETED,
        outputs={"task_groups": [{"group_id": "G1"}, {"group_id": "G2"}]},
    )
    flow.state.add_step(plan)
    flow.state.selected_steps = [StepType.PLAN, StepType.IMPLEMENT]

    context = build_context_from_flow(flow.to_dict())
    assert context[PLAN_DECOMPOSITION_KEY] == "capability"
    assert context["plan_group_count"] == 2
    assert "requested_implementation_strategy" not in context
    assert "effective_implementation_strategy" not in context


def test_context_projection_of_a_legacy_flow_is_read_only():
    flow_dict = {
        "flow_id": "old",
        "status": "completed",
        "task_type": "feature",
        "state": {
            "context": {LEGACY_EFFECTIVE_STRATEGY_KEY: "planned"},
            "steps": {},
            "step_history": [],
            "selected_steps": ["analyze", "plan", "implement"],
        },
    }
    snapshot = json.loads(json.dumps(flow_dict))

    context = build_context_from_flow(flow_dict)

    assert context["legacy_strategy"] == "planned"
    assert context[PLAN_DECOMPOSITION_KEY] == "granular"
    assert context["plan_group_count"] is None
    assert flow_dict == snapshot


def test_context_projection_of_a_planless_new_flow_has_no_group_count():
    flow = FlowInstance(task_description="t", task_type="small")
    flow.state.context[PLAN_DECOMPOSITION_KEY] = "capability"
    flow.state.context[PLAN_GRANULARITY_KEY] = "auto"
    context = build_context_from_flow(flow.to_dict())
    assert context["plan_group_count"] is None


# ---------------------------------------------------------------------------
# Execution shape predicate
# ---------------------------------------------------------------------------


def _shape(**inputs):
    return plan_mode_mod.holistic_execution_mode(
        task_type=inputs.pop("task_type", "feature"),
        inputs=inputs,
        context=inputs.pop("context", {}),
    )


def test_single_group_is_the_whole_task_shape():
    assert _shape(
        plan_decomposition="capability",
        plan_granularity="auto",
        task_groups=[{"group_id": "G1"}],
    ) == plan_mode_mod.HOLISTIC_MODE_SINGLE_GROUP


def test_auto_granularity_lets_the_group_count_decide():
    assert _shape(
        plan_decomposition="capability",
        plan_granularity="auto",
        task_groups=[{"group_id": "G1"}, {"group_id": "G2"}],
    ) is None


@pytest.mark.parametrize("group_count", [0, 1, 2, 5])
def test_forced_single_pins_one_call_whatever_plan_emitted(group_count):
    """`single` cannot be contingent on the LLM obeying a prompt sentence.

    A plan that ignores the forced-single directive must still run as one
    autonomous call — otherwise the guarantee migrating users had from the
    retired ``direct`` strategy silently becomes a multi-worktree DAG.
    """
    assert _shape(
        plan_decomposition="capability",
        plan_granularity="single",
        task_groups=[{"group_id": f"G{i}"} for i in range(group_count)],
    ) == plan_mode_mod.HOLISTIC_MODE_SINGLE_GROUP


def test_forced_single_is_honoured_from_persisted_context_alone():
    assert plan_mode_mod.holistic_execution_mode(
        task_type="feature",
        inputs={"task_groups": [{"group_id": "G1"}, {"group_id": "G2"}]},
        context={
            PLAN_DECOMPOSITION_KEY: "capability",
            PLAN_GRANULARITY_KEY: "single",
        },
    ) == plan_mode_mod.HOLISTIC_MODE_SINGLE_GROUP


def test_forced_single_does_not_apply_to_the_granular_doctrine():
    assert _shape(
        plan_decomposition="granular",
        plan_granularity="single",
        task_groups=[{"group_id": "G1"}, {"group_id": "G2"}],
    ) is None


def test_a_flow_with_no_doctrine_key_falls_back_the_way_plan_does():
    """The doctrine fallback must be the one PLAN itself used.

    ``PlanModeResolver.view`` projects a missing doctrine onto the current
    default, so PLAN emitted coarse capability groups for such a flow. Reading
    the same decision differently downstream would hand those groups to the
    granular per-task contract they deliberately do not carry.
    """
    assert _shape(
        plan_granularity="single",
        task_groups=[{"group_id": "G1"}, {"group_id": "G2"}],
    ) == plan_mode_mod.HOLISTIC_MODE_SINGLE_GROUP
    assert _shape(
        task_groups=[{"group_id": "G1"}],
    ) == plan_mode_mod.HOLISTIC_MODE_SINGLE_GROUP
    # ...but the one legacy marker that genuinely means "granular" still wins.
    assert _shape(
        effective_implementation_strategy="planned",
        task_groups=[{"group_id": "G1"}],
    ) is None


def test_legacy_direct_flow_keeps_the_whole_task_shape_on_resume():
    """A `direct` flow has no PLAN and no groups — grouped work is impossible.

    Classifying it as grouped would hand IMPLEMENT an empty group list and drop
    the whole-task prompt, the DIRECT_IMPLEMENTATION intent and the PARTIAL
    auto-continuation gate the flow was already running under.
    """
    assert plan_mode_mod.holistic_execution_mode(
        task_type="feature",
        inputs={},
        context={LEGACY_EFFECTIVE_STRATEGY_KEY: "direct"},
    ) == plan_mode_mod.HOLISTIC_MODE_LEGACY_DIRECT


def test_legacy_direct_is_recognised_from_step_inputs_too():
    """A step whose inputs were built before the axis retired still carries it."""
    assert _shape(
        effective_implementation_strategy="Direct",
    ) == plan_mode_mod.HOLISTIC_MODE_LEGACY_DIRECT


@pytest.mark.parametrize("group_count", [1, 2, 3])
def test_legacy_direct_flow_that_did_run_plan_reads_its_shape_off_the_plan(
    group_count,
):
    """Such a flow *can* acquire a plan: ANALYZE rebuilds the sequence.

    Once it has, the legacy branch's prompt ("carries no plan and no task
    groups") is a lie its own history contradicts, so the single-group branch
    must take over — same one-call contract, groups kept as planning context.
    """
    assert _shape(
        effective_implementation_strategy="direct",
        task_groups=[{"group_id": f"G{i}"} for i in range(group_count)],
    ) == plan_mode_mod.HOLISTIC_MODE_SINGLE_GROUP


def test_legacy_direct_projects_onto_capability_and_forced_single():
    """The projection is what keeps a planned `direct` flow on one call."""
    inputs = {"task_groups": [{"group_id": "G1"}, {"group_id": "G2"}]}
    context = {LEGACY_EFFECTIVE_STRATEGY_KEY: "direct"}
    assert plan_mode_mod.is_capability_decomposition(inputs, context)
    assert plan_mode_mod.is_forced_single_group(inputs, context)


def test_legacy_planned_flow_stays_grouped():
    assert _shape(
        effective_implementation_strategy="planned",
        task_groups=[{"group_id": "G1"}],
    ) is None


def test_persisted_doctrine_wins_over_a_stale_legacy_strategy_key():
    """Once the new model owns the decision, the retired key is inert."""
    assert _shape(
        plan_decomposition="granular",
        plan_granularity="auto",
        effective_implementation_strategy="direct",
        task_groups=[{"group_id": "G1"}, {"group_id": "G2"}],
    ) is None


def test_small_type_outranks_a_legacy_direct_marker():
    assert _shape(
        task_type="small",
        effective_implementation_strategy="direct",
    ) == plan_mode_mod.HOLISTIC_MODE_SMALL


def test_is_legacy_direct_flow_predicate():
    assert plan_mode_mod.is_legacy_direct_flow(
        {}, {LEGACY_EFFECTIVE_STRATEGY_KEY: "direct"},
    )
    assert not plan_mode_mod.is_legacy_direct_flow(
        {}, {LEGACY_EFFECTIVE_STRATEGY_KEY: "not_applicable"},
    )
    assert not plan_mode_mod.is_legacy_direct_flow({}, {})
    assert not plan_mode_mod.is_legacy_direct_flow(
        {PLAN_DECOMPOSITION_KEY: "capability"},
        {LEGACY_EFFECTIVE_STRATEGY_KEY: "direct"},
    )
    # A plan the flow acquired on resume ends the legacy classification; an
    # empty list is still "no groups", so the whole-task branch keeps it.
    assert not plan_mode_mod.is_legacy_direct_flow(
        {"task_groups": [{"group_id": "G1"}]},
        {LEGACY_EFFECTIVE_STRATEGY_KEY: "direct"},
    )
    assert plan_mode_mod.is_legacy_direct_flow(
        {"task_groups": []}, {LEGACY_EFFECTIVE_STRATEGY_KEY: "direct"},
    )


def test_is_forced_single_group_reads_inputs_then_context():
    assert plan_mode_mod.is_forced_single_group(
        {PLAN_GRANULARITY_KEY: "single"}, {},
    )
    assert plan_mode_mod.is_forced_single_group(
        {}, {PLAN_GRANULARITY_KEY: "single"},
    )
    for granularity in ("auto", "conservative"):
        assert not plan_mode_mod.is_forced_single_group(
            {PLAN_GRANULARITY_KEY: granularity}, {},
        )
    assert not plan_mode_mod.is_forced_single_group({}, {})


# ---------------------------------------------------------------------------
# ANALYZE no longer carries the routing question
# ---------------------------------------------------------------------------


def _run_analyze(project_root: Path, *, task_type: str, analyze_output: dict):
    from tianluo.engine.steps import analyze as analyze_mod
    import tianluo.engine.context_builder as context_builder

    flow = StateMachine(project_root).create_flow("task", task_type=task_type)
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


def test_analyze_prompt_carries_no_routing_question(tmp_path: Path):
    _flow, _step, status, call_kwargs = _run_analyze(
        tmp_path,
        task_type="feature",
        analyze_output={
            "task_type": "feature",
            "scope": "engine",
            "complexity": "medium",
            "reasoning": "why",
            "root_cause_clear": True,
        },
    )
    assert status is StepStatus.COMPLETED
    prompt = call_kwargs["prompt"]
    for banned in ("direct|planned", "implementation_strategy", "strategy_reason"):
        assert banned not in prompt
    assert "implementation_strategy" not in call_kwargs["json_schema_hint"]


def test_analyze_writes_no_strategy_outputs_or_context(tmp_path: Path):
    flow, step, status, _ = _run_analyze(
        tmp_path,
        task_type="feature",
        analyze_output={
            "task_type": "feature",
            "root_cause_clear": True,
            # A stray recommendation from an older prompt must simply be ignored.
            "implementation_strategy": "direct",
            "strategy_reason": "one call carries it",
        },
    )
    assert status is StepStatus.COMPLETED
    for banned in (
        "requested_implementation_strategy",
        "effective_implementation_strategy",
        "strategy_reason",
    ):
        assert banned not in step.outputs
        assert banned not in flow.state.context
    assert StepType.PLAN in flow.state.selected_steps


def test_reclassified_flow_gets_a_planned_sequence(tmp_path: Path):
    flow, _step, status, _ = _run_analyze(
        tmp_path,
        task_type="small",
        analyze_output={"task_type": "feature", "root_cause_clear": True},
    )
    assert status is StepStatus.COMPLETED
    assert flow.task_type == "feature"
    assert StepType.PLAN in flow.state.selected_steps
    assert StepType.IMPLEMENT in flow.state.selected_steps


def test_worktree_flow_keeps_its_merge_tail_after_the_rebuild(tmp_path: Path):
    from tianluo.engine.steps import analyze as analyze_mod
    import tianluo.engine.context_builder as context_builder

    machine = StateMachine(tmp_path)
    with patch.object(
        machine, "_resolve_main_checkout_root", return_value=tmp_path
    ):
        flow = machine.create_flow(
            "task", task_type="feature", is_worktree_mode=True
        )
    step = flow.state.get_current_step()
    assert step is not None

    with patch.object(analyze_mod, "_collect_project_summary", return_value="ctx"), \
        patch.object(analyze_mod, "LLMCaller") as caller_cls, \
        patch.object(context_builder, "get_issue_discovery_injection", return_value=""), \
        patch.object(context_builder, "get_charter_injection", return_value=""), \
        patch.object(context_builder, "get_code_index_injection", return_value=""), \
        patch.object(context_builder, "ensure_code_index_fresh", return_value=None), \
        patch.object(context_builder, "get_runtime_environment_injection", return_value=""):
        caller_cls.return_value.call.return_value = json.dumps(
            {"task_type": "feature", "root_cause_clear": True}
        )
        assert analyze_mod.analyze_handler(step, flow) is StepStatus.COMPLETED

    steps = flow.state.selected_steps
    assert StepType.PLAN in steps
    assert steps.index(StepType.VERSION_RECONCILE) == (
        steps.index(StepType.MERGE_INTEGRATE) + 1
    )


def test_bugfix_with_unclear_root_cause_still_gets_investigate(tmp_path: Path):
    flow, _step, status, _ = _run_analyze(
        tmp_path,
        task_type="bugfix",
        analyze_output={"task_type": "bugfix", "root_cause_clear": False},
    )
    assert status is StepStatus.COMPLETED
    steps = flow.state.selected_steps
    assert StepType.INVESTIGATE in steps
    assert steps.index(StepType.INVESTIGATE) < steps.index(StepType.PLAN)


# ---------------------------------------------------------------------------
# IMPLEMENT input forwarding
# ---------------------------------------------------------------------------


def test_implement_inputs_carry_the_plan_mode_not_a_strategy(tmp_path: Path):
    machine = StateMachine(tmp_path)
    flow = machine.create_flow(
        "task",
        task_type="feature",
        plan_decomposition="capability",
        plan_granularity="conservative",
    )
    inputs = machine._build_step_inputs(flow, StepType.IMPLEMENT)
    assert inputs[PLAN_DECOMPOSITION_KEY] == "capability"
    assert inputs[PLAN_GRANULARITY_KEY] == "conservative"
    assert "effective_implementation_strategy" not in inputs
    assert "strategy_reason" not in inputs
    assert "strategy_reason" not in inputs["analysis_context"]
