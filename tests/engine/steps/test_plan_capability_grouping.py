"""Tests for PLAN's capability decomposition doctrine.

Covers the four sizing criteria, the three granularity tiers, the
artifact-split guardrail, the coarse output schema, and — in the opposite
direction — that the ``granular`` legacy doctrine still produces exactly the
prompt it produced before the doctrine split existed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.plan_decomposition import PlanDecomposition, PlanGranularity
from tianluo.engine.steps import plan as plan_mod
from tianluo.engine.steps.plan import (
    ARTIFACT_SPLIT_GUARDRAIL,
    CAPABILITY_GRANULARITY_AUTO,
    CAPABILITY_GRANULARITY_CONSERVATIVE,
    CAPABILITY_GRANULARITY_SINGLE,
    CAPABILITY_JSON_SCHEMA,
    CAPABILITY_TASKS_SECTION,
    TASKS_SECTION,
    VERSION_FILE_GUARDRAIL,
    _build_prompt,
    plan_handler,
)


_PROMPT_KWARGS = dict(
    task_description="Add an export capability",
    task_type="feature",
    scope="module",
    project_summary="a project",
    revision_section="",
)


def _capability_prompt(depth="full", granularity=PlanGranularity.AUTO):
    return _build_prompt(
        depth=depth,
        decomposition=PlanDecomposition.CAPABILITY,
        granularity=granularity,
        **_PROMPT_KWARGS,
    )


def _granular_prompt(depth="full"):
    return _build_prompt(
        depth=depth,
        decomposition=PlanDecomposition.GRANULAR,
        **_PROMPT_KWARGS,
    )


# ---------------------------------------------------------------------------
# The grouping doctrine
# ---------------------------------------------------------------------------


class TestCapabilityGroupingDoctrine:
    """The only splitting criterion is "can one autonomous call carry it"."""

    def test_prompt_states_the_single_splitting_criterion(self):
        prompt = _capability_prompt()
        assert "can a single autonomous implement call safely carry this?" in prompt
        assert "The ONLY criterion for" in prompt

    def test_prompt_carries_all_four_sizing_criteria(self):
        # Line wrapping is incidental; assert on the unwrapped text.
        flat = " ".join(_capability_prompt().split())
        # 1. one capability, one call can do it -> one group
        assert "One capability, and one call can complete it → **one group**" in flat
        # 2. one capability one call cannot carry -> split
        assert "a single call cannot carry" in flat
        assert "**two or more groups**" in flat
        # 3. two distinct capabilities one call can still do -> still one group
        assert "naturally distinct capabilities" in flat
        assert "**still one group**" in flat
        assert "Distinctness alone is not a reason to split" in flat
        # 4. on the edge -> one capability per group, aggregation lowers the bar
        assert 'On the edge between "can" and "cannot"' in flat
        assert "**one capability per group**" in flat
        assert (
            "more capabilities a group aggregates, the LOWER the threshold"
            in flat
        )
        assert "aggregation makes you more conservative, never less" in flat

    def test_prompt_forbids_a_per_task_listing(self):
        prompt = _capability_prompt()
        assert "Do NOT enumerate individual tasks inside a group" in prompt
        assert "planning / sub-agent system" in prompt

    def test_prompt_keeps_proposal_and_design(self):
        """The human gate and the fix loop's design context still need them."""
        prompt = _capability_prompt()
        assert "## Part 1: Proposal" in prompt
        assert "## Part 2: Design" in prompt

    def test_bugfix_depth_keeps_its_lightweight_design(self):
        prompt = _capability_prompt(depth="medium")
        assert "## Part 2: Design (lightweight)" in prompt
        assert "## Part 3: Task Groups (capability units)" in prompt

    def test_shallow_depth_labels_the_section_instructions(self):
        prompt = _capability_prompt(depth="shallow")
        assert "## Instructions: Task Groups (capability units)" in prompt
        assert "## Part 1: Proposal" not in prompt

    def test_capability_prompt_never_carries_the_granular_tasks_section(self):
        prompt = _capability_prompt()
        assert "### Task Structure" not in prompt
        assert "estimated_loc" not in prompt


# ---------------------------------------------------------------------------
# Granularity tiers
# ---------------------------------------------------------------------------


class TestGranularityTiers:
    """auto / single / conservative are mutually exclusive directives."""

    @pytest.mark.parametrize(
        "granularity,expected",
        [
            (PlanGranularity.AUTO, CAPABILITY_GRANULARITY_AUTO),
            (PlanGranularity.SINGLE, CAPABILITY_GRANULARITY_SINGLE),
            (PlanGranularity.CONSERVATIVE, CAPABILITY_GRANULARITY_CONSERVATIVE),
        ],
    )
    def test_exactly_one_tier_appears_in_a_prompt(self, granularity, expected):
        prompt = _capability_prompt(granularity=granularity)
        others = {
            CAPABILITY_GRANULARITY_AUTO,
            CAPABILITY_GRANULARITY_SINGLE,
            CAPABILITY_GRANULARITY_CONSERVATIVE,
        } - {expected}
        assert expected in prompt
        for other in others:
            assert other not in prompt

    def test_tier_headings_are_distinct(self):
        headings = {
            CAPABILITY_GRANULARITY_AUTO.splitlines()[0],
            CAPABILITY_GRANULARITY_SINGLE.splitlines()[0],
            CAPABILITY_GRANULARITY_CONSERVATIVE.splitlines()[0],
        }
        assert len(headings) == 3

    def test_single_demands_exactly_one_group(self):
        prompt = _capability_prompt(granularity=PlanGranularity.SINGLE)
        assert "Emit **exactly one** task group" in prompt
        assert "do not split under any" in prompt

    def test_conservative_is_more_split_prone_than_auto(self):
        conservative = _capability_prompt(
            granularity=PlanGranularity.CONSERVATIVE,
        )
        auto = _capability_prompt(granularity=PlanGranularity.AUTO)
        assert "Lower the splitting threshold" in conservative
        assert "err\ntoward MORE groups" in conservative
        # auto explicitly refuses to bias either way
        assert "Do not inflate the count" in auto
        assert "Lower the splitting threshold" not in auto

    def test_unknown_granularity_falls_back_to_auto(self):
        """A malformed persisted value must not drop the directive entirely."""
        prompt = _build_prompt(
            depth="full",
            decomposition=PlanDecomposition.CAPABILITY,
            granularity="nonsense",
            **_PROMPT_KWARGS,
        )
        assert CAPABILITY_GRANULARITY_AUTO in prompt

    def test_granularity_is_ignored_under_granular(self):
        for granularity in PlanGranularity:
            prompt = _build_prompt(
                depth="full",
                decomposition=PlanDecomposition.GRANULAR,
                granularity=granularity,
                **_PROMPT_KWARGS,
            )
            assert prompt == _granular_prompt()


# ---------------------------------------------------------------------------
# The artifact-split guardrail
# ---------------------------------------------------------------------------


class TestArtifactSplitGuardrail:
    """Same form as VERSION_FILE_GUARDRAIL, so it is testable the same way."""

    def test_guardrail_has_the_same_form_as_the_version_guardrail(self):
        artifact = ARTIFACT_SPLIT_GUARDRAIL.lstrip("\n")
        version = VERSION_FILE_GUARDRAIL.lstrip("\n")
        assert artifact.startswith("## Guardrail:")
        assert version.startswith("## Guardrail:")
        # Both are a heading + prose + a bulleted list of forbidden shapes.
        assert "\n- " in artifact
        assert "\n- " in version
        assert artifact.endswith("\n")
        assert version.endswith("\n")

    def test_guardrail_names_the_three_forbidden_group_shapes(self):
        text = ARTIFACT_SPLIT_GUARDRAIL
        assert "a separate **test** group" in text
        assert "a separate **docs** group" in text
        assert "a separate **config** group" in text

    def test_guardrail_makes_tests_part_of_every_group(self):
        assert "part of what **each group itself delivers**" in ARTIFACT_SPLIT_GUARDRAIL
        assert "covered by\nits own tests" in ARTIFACT_SPLIT_GUARDRAIL

    def test_guardrail_forbids_file_module_and_layer_splits(self):
        text = ARTIFACT_SPLIT_GUARDRAIL
        assert "a file set, a module boundary, or a code layer" in text
        assert "never\nalong files, modules, or code layers" in text

    def test_guardrail_still_allows_a_test_system_capability(self):
        """Otherwise "fix the flaky test runner" becomes unplannable."""
        assert "fix the flaky retry in the test runner" in ARTIFACT_SPLIT_GUARDRAIL

    def test_guardrail_is_only_attached_under_capability(self):
        assert ARTIFACT_SPLIT_GUARDRAIL in _capability_prompt()
        for depth in ("full", "medium", "shallow"):
            assert ARTIFACT_SPLIT_GUARDRAIL not in _granular_prompt(depth=depth)

    def test_version_guardrail_survives_in_both_doctrines(self):
        assert VERSION_FILE_GUARDRAIL in _capability_prompt()
        assert VERSION_FILE_GUARDRAIL in _granular_prompt()


# ---------------------------------------------------------------------------
# The coarse output schema
# ---------------------------------------------------------------------------


class TestCapabilitySchema:
    """Groups carry the five scheduling fields and nothing else."""

    def test_schema_declares_no_tasks_array(self):
        assert '"tasks"' not in CAPABILITY_JSON_SCHEMA
        assert "Do NOT emit a `tasks` array" in CAPABILITY_JSON_SCHEMA

    def test_schema_declares_the_five_scheduling_fields(self):
        for field in (
            '"group_id"', '"name"', '"description"',
            '"group_order"', '"depends_on"',
        ):
            assert field in CAPABILITY_JSON_SCHEMA

    def test_schema_keeps_proposal_and_design(self):
        assert '"proposal"' in CAPABILITY_JSON_SCHEMA
        assert '"design"' in CAPABILITY_JSON_SCHEMA

    def test_schema_explains_that_independent_groups_run_in_parallel(self):
        assert "run in parallel" in CAPABILITY_JSON_SCHEMA

    def test_schema_hint_carries_no_tasks_key(self):
        assert '"tasks"' not in plan_mod.CAPABILITY_JSON_SCHEMA_HINT
        assert '"tasks"' in plan_mod.GRANULAR_JSON_SCHEMA_HINT

    def test_schema_reaches_the_prompt_as_valid_json(self):
        """The schema block is appended verbatim, so its braces stay single."""
        prompt = _capability_prompt()
        assert '"group_id": "G1"' in prompt
        block = prompt[prompt.index("```json", prompt.index("Respond in JSON")):]
        block = block[len("```json"):block.index("```", 3)]
        assert "{{" not in block
        assert json.loads(block.replace('"...', '"x').replace('...', ''))


# ---------------------------------------------------------------------------
# Legacy lock: the granular prompt must not drift
# ---------------------------------------------------------------------------


class TestGranularDoctrineIsFrozen:
    """`granular` is a legacy value: its prompt and products do not change."""

    def test_granular_sections_are_unchanged(self):
        prompt = _granular_prompt()
        assert TASKS_SECTION.format(part_label="Part 3") in prompt
        assert "### Grouping Principles" in prompt
        assert "- **High cohesion within groups**" in prompt
        assert "Include `estimated_loc` (integer)" in prompt

    def test_granular_prompt_has_no_capability_doctrine_vocabulary(self):
        prompt = _granular_prompt()
        assert "capability units" not in prompt
        assert "Sizing Criteria" not in prompt
        assert "autonomous implement call" not in prompt
        assert CAPABILITY_TASKS_SECTION.splitlines()[0] not in prompt

    @pytest.mark.parametrize("depth", ["full", "medium", "shallow"])
    def test_granular_prompt_matches_the_recorded_section_order(self, depth):
        """Ordering is part of the frozen shape, not just the section set."""
        prompt = _granular_prompt(depth=depth)
        tasks_at = prompt.index("Task Groups")
        guardrail_at = prompt.index(VERSION_FILE_GUARDRAIL)
        schema_at = prompt.index("Respond in JSON format:")
        assert tasks_at < guardrail_at < schema_at


# ---------------------------------------------------------------------------
# plan_handler dispatch and outputs
# ---------------------------------------------------------------------------


def _flow_and_step(tmp_path, *, decomposition=None, granularity=None):
    flow = FlowInstance(
        flow_id="plan-flow",
        task_description="Add an export capability",
        task_type="feature",
        change_path=tmp_path / "tianluo",
    )
    if decomposition is not None:
        flow.state.context["plan_decomposition"] = decomposition
    if granularity is not None:
        flow.state.context["plan_granularity"] = granularity
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


_CAPABILITY_RESPONSE = {
    "plan": {
        "proposal": {"summary": "add export"},
        "design": {"overview": "one component"},
    },
    "task_groups": [
        {
            "group_id": "G1",
            "name": "Export",
            "description": "deliver export with its tests",
            "group_order": 1,
            "depends_on": [],
        },
        {
            "group_id": "G2",
            "name": "Import",
            "description": "deliver import with its tests",
            "group_order": 2,
            "depends_on": [],
        },
    ],
    "total_complexity": "medium",
    "estimated_effort": "a day",
}


def _run_plan(flow, step, response):
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
        status = plan_handler(step, flow)
    return status, caller


class TestPlanHandlerDispatch:
    """The handler reads back the persisted doctrine; it never re-decides."""

    def test_capability_parse_survives_groups_without_tasks(self, tmp_path):
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        status, _ = _run_plan(flow, step, _CAPABILITY_RESPONSE)

        assert status == StepStatus.COMPLETED
        assert len(step.outputs["task_groups"]) == 2
        assert "tasks" not in step.outputs["task_groups"][0]

    def test_outputs_record_the_mode_and_group_count(self, tmp_path):
        flow, step = _flow_and_step(
            tmp_path, decomposition="capability", granularity="conservative",
        )
        _run_plan(flow, step, _CAPABILITY_RESPONSE)

        assert step.outputs["plan_decomposition"] == "capability"
        assert step.outputs["plan_granularity"] == "conservative"
        assert step.outputs["plan_group_count"] == 2

    def test_group_count_matches_a_single_group_plan(self, tmp_path):
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        response = dict(
            _CAPABILITY_RESPONSE,
            task_groups=_CAPABILITY_RESPONSE["task_groups"][:1],
        )
        _run_plan(flow, step, response)

        assert step.outputs["plan_group_count"] == 1

    def test_capability_prompt_is_sent_for_a_capability_flow(self, tmp_path):
        flow, step = _flow_and_step(
            tmp_path, decomposition="capability", granularity="single",
        )
        _, caller = _run_plan(flow, step, _CAPABILITY_RESPONSE)

        prompt = caller.call.call_args.kwargs["prompt"]
        assert ARTIFACT_SPLIT_GUARDRAIL in prompt
        assert CAPABILITY_GRANULARITY_SINGLE in prompt
        assert caller.call.call_args.kwargs["json_schema_hint"] == (
            plan_mod.CAPABILITY_JSON_SCHEMA_HINT
        )

    def test_granular_flow_gets_the_legacy_prompt_and_hint(self, tmp_path):
        flow, step = _flow_and_step(tmp_path, decomposition="granular")
        _, caller = _run_plan(flow, step, _CAPABILITY_RESPONSE)

        prompt = caller.call.call_args.kwargs["prompt"]
        assert ARTIFACT_SPLIT_GUARDRAIL not in prompt
        assert "estimated_loc" in prompt
        assert caller.call.call_args.kwargs["json_schema_hint"] == (
            plan_mod.GRANULAR_JSON_SCHEMA_HINT
        )
        assert step.outputs["plan_decomposition"] == "granular"

    def test_flow_without_a_persisted_mode_plans_under_the_default(self, tmp_path):
        flow, step = _flow_and_step(tmp_path)
        _, caller = _run_plan(flow, step, _CAPABILITY_RESPONSE)

        assert step.outputs["plan_decomposition"] == "capability"
        assert ARTIFACT_SPLIT_GUARDRAIL in caller.call.call_args.kwargs["prompt"]

    def test_legacy_planned_flow_keeps_the_granular_doctrine(self, tmp_path):
        """An old `planned` flow resumed into PLAN must not switch doctrine."""
        flow, step = _flow_and_step(tmp_path)
        flow.state.context["effective_implementation_strategy"] = "planned"
        _, caller = _run_plan(flow, step, _CAPABILITY_RESPONSE)

        assert step.outputs["plan_decomposition"] == "granular"
        assert ARTIFACT_SPLIT_GUARDRAIL not in caller.call.call_args.kwargs["prompt"]

    def test_step_inputs_override_the_context_lookup(self, tmp_path):
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        step.inputs["plan_decomposition"] = "granular"
        _run_plan(flow, step, _CAPABILITY_RESPONSE)

        assert step.outputs["plan_decomposition"] == "granular"

    def test_forced_single_logs_when_the_model_splits_anyway(
        self, tmp_path, caplog,
    ):
        flow, step = _flow_and_step(
            tmp_path, decomposition="capability", granularity="single",
        )
        with caplog.at_level("WARNING"):
            _run_plan(flow, step, _CAPABILITY_RESPONSE)

        assert "plan_granularity=single" in caplog.text
        # Not silently collapsed: every planned group still reaches IMPLEMENT.
        assert len(step.outputs["task_groups"]) == 2

    def test_display_renders_coarse_groups_without_raising(self, tmp_path):
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        with patch.object(plan_mod, "logger") as mock_logger:
            _run_plan(flow, step, _CAPABILITY_RESPONSE)
            # _display_plan failures are swallowed into a warning; assert none.
            warnings = [
                c for c in mock_logger.warning.call_args_list
                if "format plan output" in str(c)
            ]
            assert warnings == []
