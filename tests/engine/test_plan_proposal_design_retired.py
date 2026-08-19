"""Regression: PLAN emits scheduling data only — no proposal, no design.

PLAN's output contract was narrowed to ``task_groups`` plus its complexity /
effort / decomposition metadata. These tests pin the *absence* of the retired
artifacts on the write side (prompt, schema, step outputs, the step-to-step
channels, the downstream implement / commit / confirm consumers) and the
*presence* of the read side (a persisted legacy flow whose PLAN outputs still
carry ``plan.proposal`` / ``plan.design`` must keep rendering).

The asymmetry is deliberate: this repository's ``tianluo/history/`` and
``tianluo/state/archive/`` hold real flows recorded under the old contract, so
deleting the read path would turn historical data into a crash source.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tianluo.engine.models import (
    STEP_POOL,
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)


_DUMMY = "X"


# ---------------------------------------------------------------------------
# PLAN prompt + schema
# ---------------------------------------------------------------------------


def _plan_prompt(decomposition, task_type="feature"):
    from tianluo.engine.plan_decomposition import PlanGranularity
    from tianluo.engine.steps.plan import _build_prompt

    return _build_prompt(
        task_description="Add an export capability",
        task_type=task_type,
        scope="module",
        project_summary="a project",
        revision_section="",
        decomposition=decomposition,
        granularity=PlanGranularity.AUTO,
    )


@pytest.mark.parametrize("doctrine", ["capability", "granular"])
@pytest.mark.parametrize(
    "task_type", ["feature", "discovery", "bugfix", "fix", "small", "review"],
)
def test_plan_prompt_never_asks_for_a_proposal_or_design(doctrine, task_type):
    """Both doctrines, every task type — the depth tiers are gone with them."""
    prompt = _plan_prompt(doctrine, task_type=task_type)
    lowered = prompt.lower()
    assert "proposal" not in lowered
    assert "design" not in lowered


@pytest.mark.parametrize("doctrine", ["capability", "granular"])
def test_plan_prompt_solicits_only_the_scheduling_fields(doctrine):
    prompt = _plan_prompt(doctrine)
    assert '"task_groups"' in prompt
    assert '"total_complexity"' in prompt
    assert '"estimated_effort"' in prompt
    # The `plan` wrapper existed only to hold proposal/design.
    assert '"plan"' not in prompt


def test_plan_module_drops_the_retired_sections_and_depth_tiering():
    import tianluo.engine.steps.plan as plan_mod

    for gone in (
        "PROPOSAL_SECTION",
        "DESIGN_SECTION",
        "DESIGN_SECTION_BUGFIX",
        "FULL_JSON_SCHEMA",
        "MEDIUM_JSON_SCHEMA",
        "SHALLOW_JSON_SCHEMA",
        "_get_prompt_depth",
    ):
        assert not hasattr(plan_mod, gone), gone


def test_plan_prompt_builder_takes_no_depth_argument():
    import inspect

    from tianluo.engine.steps.plan import _build_prompt

    assert "depth" not in inspect.signature(_build_prompt).parameters


def test_display_plan_takes_no_depth_argument():
    import inspect

    from tianluo.engine.steps.plan import _display_plan

    params = inspect.signature(_display_plan).parameters
    assert "depth" not in params
    assert "plan" not in params


def test_step_pool_plan_outputs_carry_no_plan_wrapper():
    outputs = STEP_POOL[StepType.PLAN]["outputs"]
    assert "task_groups" in outputs
    assert "plan" not in outputs


def test_step_pool_downstream_inputs_drop_the_retired_channels():
    assert "design_doc" not in STEP_POOL[StepType.IMPLEMENT]["inputs"]
    assert "proposal" not in STEP_POOL[StepType.COMMIT]["inputs"]


# ---------------------------------------------------------------------------
# PLAN handler outputs
# ---------------------------------------------------------------------------


def _flow_and_step(tmp_path, decomposition="capability"):
    flow = FlowInstance(
        flow_id="plan-no-design",
        task_description="Add an export capability",
        task_type="feature",
        status=FlowStatus.RUNNING,
        change_path=tmp_path / "tianluo",
    )
    flow.state.context["plan_decomposition"] = decomposition
    step = Step(
        step_type=StepType.PLAN,
        step_id="plan-step",
        inputs={
            "task_description": "Add an export capability",
            "task_type": "feature",
            "scope": "module",
            "project_summary": "a project",
        },
    )
    return flow, step


def _run_plan(flow, step, response):
    import tianluo.engine.steps.plan as plan_mod

    with patch.object(plan_mod, "LLMCaller") as caller_cls, \
            patch(
                "tianluo.engine.context_builder.get_charter_injection",
                return_value="",
            ), \
            patch(
                "tianluo.engine.context_builder.get_code_index_injection",
                return_value="",
            ), \
            patch(
                "tianluo.engine.context_builder.get_issue_discovery_injection",
                return_value=None,
            ), \
            patch(
                "tianluo.engine.context_builder.get_runtime_environment_injection",
                return_value="",
            ):
        caller = MagicMock()
        caller.call.return_value = json.dumps(response)
        caller_cls.return_value = caller
        status = plan_mod.plan_handler(step, flow)
    return status, caller


_RESPONSE = {
    "task_groups": [
        {
            "group_id": "G1",
            "name": "Export",
            "description": "deliver export with its tests",
            "group_order": 1,
            "depends_on": [],
        },
    ],
    "total_complexity": "medium",
    "estimated_effort": "a day",
}


def test_plan_handler_writes_no_proposal_or_design(tmp_path):
    flow, step = _flow_and_step(tmp_path)
    status, _ = _run_plan(flow, step, _RESPONSE)

    assert status == StepStatus.COMPLETED
    assert "plan" not in step.outputs
    assert "proposal" not in step.outputs
    assert "design" not in step.outputs
    assert step.outputs["task_groups"] == _RESPONSE["task_groups"]
    assert step.outputs["total_complexity"] == "medium"
    assert step.outputs["estimated_effort"] == "a day"
    assert step.outputs["plan_group_count"] == 1


def test_plan_handler_drops_a_model_supplied_plan_wrapper(tmp_path):
    """Even if the model volunteers one, PLAN does not persist it."""
    flow, step = _flow_and_step(tmp_path)
    response = dict(
        _RESPONSE,
        plan={"proposal": {"summary": "s"}, "design": {"overview": "o"}},
    )
    status, _ = _run_plan(flow, step, response)

    assert status == StepStatus.COMPLETED
    assert "plan" not in step.outputs


# ---------------------------------------------------------------------------
# implement: no ## Design Document on any prompt path
# ---------------------------------------------------------------------------


def _implement_templates() -> dict[str, tuple[str, dict]]:
    from tianluo.engine.steps.implement import (
        FIX_PROMPT,
        HOLISTIC_IMPLEMENT_PROMPT,
        IMPLEMENT_CAPABILITY_GROUP_PROMPT,
        IMPLEMENT_GROUP_PROMPT,
        IMPLEMENT_PROMPT,
    )

    grouped_fields = dict(
        task_description=_DUMMY,
        task_type="feature",
        root_cause_section="",
        current_group=_DUMMY,
        previous_results=_DUMMY,
    )
    return {
        "single_call": (IMPLEMENT_PROMPT, dict(
            task_description=_DUMMY,
            task_type="feature",
            root_cause_section="",
            task_groups=_DUMMY,
        )),
        "holistic": (HOLISTIC_IMPLEMENT_PROMPT, dict(
            task_description=_DUMMY,
            task_type="small",
            root_cause_section="",
            execution_mode=_DUMMY,
            analysis_context=_DUMMY,
            continuation_context=_DUMMY,
        )),
        "grouped": (IMPLEMENT_GROUP_PROMPT, dict(grouped_fields)),
        "capability_group": (IMPLEMENT_CAPABILITY_GROUP_PROMPT, dict(grouped_fields)),
        "fix": (FIX_PROMPT, dict(
            task_description=_DUMMY,
            root_cause_section="",
            fix_instructions=_DUMMY,
            fix_context=_DUMMY,
            fix_history=_DUMMY,
            fix_iteration=1,
        )),
    }


@pytest.mark.parametrize("path", sorted(_implement_templates()))
def test_implement_prompt_paths_have_no_design_document(path):
    template, fields = _implement_templates()[path]
    assert "{design_section}" not in template
    rendered = template.format(**fields)
    assert "## Design Document" not in rendered


def test_implement_handler_ignores_a_legacy_design_doc_input(tmp_path):
    """A resumed old flow still carrying design_doc injects nothing."""
    from tianluo.engine.steps import implement as implement_mod

    flow = FlowInstance(
        flow_id="impl-legacy-design",
        task_description="Do the thing",
        task_type="small",
        status=FlowStatus.RUNNING,
        change_path=tmp_path / "tianluo",
    )
    step = Step(
        step_type=StepType.IMPLEMENT,
        step_id="impl-step",
        inputs={
            "task_description": "Do the thing",
            "task_type": "small",
            "design_doc": {"overview": "LEGACY-DESIGN-MARKER"},
        },
    )

    with patch.object(
        implement_mod, "_run_single_llm_call", return_value=StepStatus.COMPLETED,
    ) as mock_call, \
            patch.object(implement_mod, "_resolve_files_changed"), \
            patch(
                "tianluo.engine.context_builder.get_charter_injection",
                return_value="",
            ), \
            patch(
                "tianluo.engine.context_builder.get_code_index_injection",
                return_value="",
            ), \
            patch(
                "tianluo.engine.context_builder.get_issue_discovery_injection",
                return_value=None,
            ), \
            patch(
                "tianluo.engine.context_builder.get_runtime_environment_injection",
                return_value="",
            ):
        assert implement_mod.implement_handler(step, flow) == StepStatus.COMPLETED

    prompt = mock_call.call_args.args[0]
    assert "## Design Document" not in prompt
    assert "LEGACY-DESIGN-MARKER" not in prompt


# ---------------------------------------------------------------------------
# confirm: the PLAN review prompt no longer points at proposal / design
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doctrine", ["capability", "granular"])
def test_plan_confirm_prompt_reviews_scheduling_data_only(doctrine):
    from tianluo.engine.context_builder import build_plan_confirm_prompt

    prompt = build_plan_confirm_prompt(
        step_output={"task_groups": [{"group_id": "G1", "name": "Export"}]},
        task_description="Add an export capability",
        decomposition=doctrine,
    )
    assert "## Output of the PLAN Step (task_groups)" in prompt
    assert "consult the proposal and design" not in prompt


# ---------------------------------------------------------------------------
# Read side: a persisted legacy flow still renders
# ---------------------------------------------------------------------------


_LEGACY_PLAN_OUTPUTS = {
    "plan": {
        "proposal": {
            "summary": "LEGACY-PROPOSAL-SUMMARY",
            "files_to_modify": ["a.py"],
            "files_to_create": [],
            "rationale": "because",
        },
        "design": {
            "overview": "LEGACY-DESIGN-OVERVIEW",
            "components": [{"name": "C1", "responsibilities": "does things"}],
        },
    },
    "task_groups": [
        {"group_id": "G1", "name": "core", "tasks": [{"estimated_loc": 30}]},
    ],
    "total_complexity": "medium",
}


def test_legacy_plan_step_still_renders_its_proposal_and_design():
    """`luo history show` / the CLI renderer must not lose old flows."""
    from tianluo.engine.step_renderers import _render_plan

    step = Step(step_type=StepType.PLAN, status=StepStatus.COMPLETED)
    step.outputs = json.loads(json.dumps(_LEGACY_PLAN_OUTPUTS))

    with patch("tianluo.engine.step_renderers.render_proposal") as proposal, \
            patch("tianluo.engine.step_renderers.render_design") as design, \
            patch("tianluo.engine.step_renderers.render_full") as full:
        _render_plan(step)

    proposal.assert_called_once()
    assert proposal.call_args.args[0]["summary"] == "LEGACY-PROPOSAL-SUMMARY"
    design.assert_called_once()
    assert design.call_args.args[0]["overview"] == "LEGACY-DESIGN-OVERVIEW"
    # Task groups still render alongside them.
    assert full.call_count == 1


def test_new_plan_step_renders_without_a_plan_wrapper():
    """The renderer must not report an empty plan for the new output shape."""
    from tianluo.engine.step_renderers import _render_plan

    step = Step(step_type=StepType.PLAN, status=StepStatus.COMPLETED)
    step.outputs = {
        "task_groups": [
            {"group_id": "G1", "name": "Export", "description": "deliver export"},
        ],
        "total_complexity": "medium",
    }

    with patch("tianluo.engine.step_renderers.render_proposal") as proposal, \
            patch("tianluo.engine.step_renderers.render_design") as design, \
            patch("tianluo.engine.step_renderers.render_full") as full:
        _render_plan(step)

    proposal.assert_not_called()
    design.assert_not_called()
    full.assert_called_once()
    assert "Export" in full.call_args.args[0]


def test_display_helpers_survive_for_the_legacy_render_path():
    """`render_proposal` / `render_design` have a live caller and must stay."""
    from tianluo.engine import display

    assert callable(display.render_proposal)
    assert callable(display.render_design)


def test_history_show_renders_a_legacy_plan_flow(tmp_path, capsys):
    """End to end over the `luo history show` detail dict of an old flow."""
    from tianluo.commands import history_cmd

    detail = {
        "steps": [
            {
                "step_type": "plan",
                "status": "completed",
                "outputs": json.loads(json.dumps(_LEGACY_PLAN_OUTPUTS)),
            },
        ],
    }
    history_cmd._show_plan_artifacts(detail)
    out = capsys.readouterr().out
    assert out.strip()
