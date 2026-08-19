"""Tests for PLAN's capability decomposition doctrine.

Covers the four sizing criteria, the three granularity tiers, the
artifact-split guardrail, the coarse output schema, and — in the opposite
direction — that the ``granular`` legacy doctrine still produces exactly the
prompt it produced before the doctrine split existed.

Thought-experiment baseline (a semantic assertion, not an LLM-behaviour
verification): under the task-unit doctrine, the merge input of flow
``20260818-092937_4fb52e72`` — test parallelisation plus an unrelated
discovery change — must size as **2 groups**, not the 4 phase-cut groups the
old capability-edge bias produced. "Test parallelisation" is one coherent
task: PLAN may not pre-cut it along implementation phases into a chained
G1-G3 sequence (the old output), because how a task decomposes internally is
the implement call's job. The unrelated discovery change is an independent
task and gets its own group, so the two run in parallel in isolated
worktrees. The two prompt semantics that force that 2-group answer are
asserted in ``TestCapabilityGroupingDoctrine.test_thought_experiment_baseline_semantics``;
nothing here runs an LLM.
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
    GRANULAR_JSON_SCHEMA,
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


def _capability_prompt(granularity=PlanGranularity.AUTO):
    return _build_prompt(
        decomposition=PlanDecomposition.CAPABILITY,
        granularity=granularity,
        **_PROMPT_KWARGS,
    )


def _granular_prompt():
    return _build_prompt(
        decomposition=PlanDecomposition.GRANULAR,
        **_PROMPT_KWARGS,
    )


# ---------------------------------------------------------------------------
# The grouping doctrine
# ---------------------------------------------------------------------------


class TestCapabilityGroupingDoctrine:
    """The only splitting criterion is "can one autonomous call carry it"."""

    def test_prompt_states_the_single_splitting_criterion(self):
        flat = " ".join(_capability_prompt().split())
        assert "can a single autonomous implement call safely carry this?" in flat
        assert "The ONLY criterion for" in flat

    def test_prompt_carries_all_four_sizing_criteria(self):
        # Line wrapping is incidental; assert on the unwrapped text.
        flat = " ".join(_capability_prompt().split())
        # 1. one task, one call can do it -> one group
        assert "One task, and one call can complete it → **one group**" in flat
        # 2. one task one call cannot carry -> split
        assert "One task that a single call cannot carry" in flat
        assert "**two or more groups**" in flat
        # 3. mutually unrelated independent tasks -> one group each
        assert "mutually unrelated, independent tasks" in flat
        assert "**one group each**" in flat
        assert "executed in parallel in isolated worktrees" in flat
        assert (
            "Aspects of one coherent task are not independent tasks and stay "
            "together in its group" in flat
        )
        # 4. default to aggregation; split only at the capability edge
        assert "Default to aggregation — **one task, one group**" in flat
        assert "capability edge" in flat
        assert (
            "cannot complete it, or that forcing it into one call would "
            "substantially degrade the quality of the execution" in flat
        )
        assert (
            "Never pre-cut a single task along implementation phases, "
            "implementation paths, or artifact types" in flat
        )

    @pytest.mark.parametrize("granularity", list(PlanGranularity))
    def test_old_split_bias_wording_is_gone(self, granularity):
        """The reversed bias must not survive in ANY rendering of the prompt.

        Every granularity tier is rendered: the retired wording lived in the
        tier directives too, so an auto-only assertion would let a
        ``conservative`` flow keep re-teaching the per-capability unit.
        """
        flat = " ".join(_capability_prompt(granularity=granularity).split())
        for stale in (
            "one capability per group",
            "the LOWER the threshold",
            "aggregation makes you more conservative",
            "naturally distinct capabilities",
            "Distinctness alone is not a reason to split",
            'On the edge between "can" and "cannot"',
            # Old unit-of-grouping statements: the unit is a task everywhere,
            # including the section header, the guardrail, and the schema.
            "capability units",
            "deliverable capability units",
            "Group by Capability",
            "its own capability is implemented",
            "one capability's tests",
        ):
            assert stale not in flat

    def test_thought_experiment_baseline_semantics(self):
        """The two semantics behind the 2-groups-not-4 baseline (see module docstring)."""
        flat = " ".join(_capability_prompt().split())
        # One coherent task may not be pre-cut by PLAN along implementation
        # phases — its internal decomposition is the implement call's job.
        assert (
            "Never pre-cut a single task along implementation phases, "
            "implementation paths, or artifact types" in flat
        )
        # Mutually unrelated tasks each get their own group, so they can run
        # in parallel in isolated worktrees.
        assert "mutually unrelated, independent tasks → **one group each**" in flat
        assert "executed in parallel in isolated worktrees" in flat

    def test_prompt_forbids_a_per_task_listing(self):
        prompt = _capability_prompt()
        assert "Do NOT enumerate individual tasks inside a group" in prompt
        assert "planning / sub-agent system" in prompt

    def test_prompt_asks_for_no_proposal_or_design(self):
        """PLAN emits scheduling data only; there is no artifact to review."""
        prompt = _capability_prompt()
        assert "Proposal" not in prompt
        assert "Design" not in prompt
        assert "proposal" not in prompt
        assert "design" not in prompt

    def test_prompt_labels_the_task_groups_section_once(self):
        """No Part 1/Part 2 remain, so the section is not numbered."""
        prompt = _capability_prompt()
        assert "## Instructions: Task Groups (task units)" in prompt
        assert "Part 1" not in prompt
        assert "Part 2" not in prompt
        assert "Part 3" not in prompt

    @pytest.mark.parametrize(
        "task_type", ["feature", "discovery", "bugfix", "fix", "small", "review"],
    )
    def test_every_task_type_gets_the_same_prompt(self, task_type):
        """Depth tiering is gone: it only ever selected proposal/design."""
        kwargs = dict(_PROMPT_KWARGS, task_type=task_type)
        prompt = _build_prompt(
            decomposition=PlanDecomposition.CAPABILITY,
            granularity=PlanGranularity.AUTO,
            **kwargs,
        )
        baseline = _capability_prompt()
        assert prompt == baseline.replace(
            "## Task Type\nfeature", f"## Task Type\n{task_type}",
        )

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

    def test_auto_sizes_by_independent_task_count(self):
        auto = _capability_prompt(granularity=PlanGranularity.AUTO)
        flat = " ".join(auto.split())
        assert (
            "The group count is the number of mutually independent tasks in "
            "this requirement" in flat
        )
        assert "normally one group per task" in flat
        assert "capability edge" in flat

    def test_conservative_is_more_split_prone_than_auto(self):
        conservative = _capability_prompt(
            granularity=PlanGranularity.CONSERVATIVE,
        )
        auto = _capability_prompt(granularity=PlanGranularity.AUTO)
        assert "Lower the splitting threshold" in conservative
        assert "err toward MORE groups" in " ".join(conservative.split())
        # The tier lowers the threshold but keeps the task-unit doctrine: what
        # it prefers is one group per sub-task, never one per capability.
        assert "Prefer one group per sub-task" in " ".join(conservative.split())
        # auto defaults to aggregation: one group per task, split only at the
        # capability edge
        assert "Do not inflate the count" in auto
        assert "normally one group per task" in auto
        assert "Lower the splitting threshold" not in auto

    def test_unknown_granularity_falls_back_to_auto(self):
        """A malformed persisted value must not drop the directive entirely."""
        prompt = _build_prompt(
            decomposition=PlanDecomposition.CAPABILITY,
            granularity="nonsense",
            **_PROMPT_KWARGS,
        )
        assert CAPABILITY_GRANULARITY_AUTO in prompt

    def test_granularity_is_ignored_under_granular(self):
        for granularity in PlanGranularity:
            prompt = _build_prompt(
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

    def test_guardrail_still_allows_a_test_system_task(self):
        """Otherwise "fix the flaky test runner" becomes unplannable."""
        assert "fix the flaky retry in the test runner" in ARTIFACT_SPLIT_GUARDRAIL

    def test_guardrail_carries_the_task_grouping_unit(self):
        """Every unit-of-grouping statement in the guardrail names the task.

        The guardrail is appended directly after CAPABILITY_TASKS_SECTION, so
        a leftover capability-unit statement here would re-teach the old
        doctrine to the model in the very prompt that reversed it.
        """
        flat = " ".join(ARTIFACT_SPLIT_GUARDRAIL.split())
        assert "Group by Task, Never by Artifact Type" in flat
        assert "its own task is implemented AND covered by its own tests" in flat
        assert "Groups are cut along task units only" in flat
        assert "A group whose *task* happens to concern the test system" in flat
        assert "carving one task's tests, docs or config out" in flat
        for stale in ("deliverable capability units", "one capability's tests",
                      "its own capability"):
            assert stale not in flat

    def test_guardrail_is_only_attached_under_capability(self):
        assert ARTIFACT_SPLIT_GUARDRAIL in _capability_prompt()
        assert ARTIFACT_SPLIT_GUARDRAIL not in _granular_prompt()

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

    def test_schema_example_names_a_task_not_a_capability(self):
        """The example group must not teach the old capability-unit wording."""
        assert '"name": "Task this group delivers"' in CAPABILITY_JSON_SCHEMA
        assert "Capability this group delivers" not in CAPABILITY_JSON_SCHEMA

    def test_schema_carries_no_proposal_or_design(self):
        for schema in (CAPABILITY_JSON_SCHEMA, GRANULAR_JSON_SCHEMA):
            assert "proposal" not in schema
            assert "design" not in schema
        assert '"plan"' not in CAPABILITY_JSON_SCHEMA
        assert '"plan"' not in GRANULAR_JSON_SCHEMA

    def test_schema_explains_that_independent_groups_run_in_parallel(self):
        assert "run in parallel" in CAPABILITY_JSON_SCHEMA

    def test_schema_requires_acyclic_dependencies(self):
        # The parse guard rejects a cycle, which costs a whole re-plan; saying
        # so up front is cheaper than the retry.
        assert "acyclic" in CAPABILITY_JSON_SCHEMA

    def test_schema_hint_carries_no_tasks_key(self):
        assert '"tasks"' not in plan_mod.CAPABILITY_JSON_SCHEMA_HINT
        assert '"tasks"' in plan_mod.GRANULAR_JSON_SCHEMA_HINT

    def test_schema_hints_carry_no_proposal_or_design(self):
        for hint in (
            plan_mod.CAPABILITY_JSON_SCHEMA_HINT,
            plan_mod.GRANULAR_JSON_SCHEMA_HINT,
        ):
            assert "proposal" not in hint
            assert "design" not in hint
            assert '"plan"' not in hint

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
        assert TASKS_SECTION in prompt
        assert "### Grouping Principles" in prompt
        assert "- **High cohesion within groups**" in prompt
        assert "Include `estimated_loc` (integer)" in prompt

    def test_granular_prompt_has_no_capability_doctrine_vocabulary(self):
        prompt = _granular_prompt()
        assert "capability units" not in prompt
        assert "Sizing Criteria" not in prompt
        assert "autonomous implement call" not in prompt
        assert CAPABILITY_TASKS_SECTION.splitlines()[0] not in prompt

    def test_granular_prompt_matches_the_recorded_section_order(self):
        """Ordering is part of the frozen shape, not just the section set."""
        prompt = _granular_prompt()
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

    def test_empty_capability_plan_fails_instead_of_completing(self, tmp_path):
        """Zero groups is a failed capability plan, not a coarser one.

        Storing it would let IMPLEMENT read an execution shape off a count of
        zero, which is neither the whole-task contract nor a DAG.
        """
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        response = dict(_CAPABILITY_RESPONSE, task_groups=[])
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "task_groups" in (step.error_message or "")
        assert "task_groups" not in step.outputs

    @pytest.mark.parametrize(
        "groups",
        [
            {"G1": {"name": "Export", "description": "deliver export"}},
            "G1: deliver export",
        ],
        ids=["dict", "string"],
    )
    def test_non_list_capability_groups_fail_instead_of_completing(
        self, tmp_path, groups,
    ):
        """A group enumeration IMPLEMENT cannot read a count off is a failure.

        `plan_group_count` only trusts a list, so a dict of one group would
        project as a count of 1 to history/WebUI while IMPLEMENT sees no shape
        and falls through to the grouped path with zero groups.
        """
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "task_groups" in (step.error_message or "")
        assert "task_groups" not in step.outputs
        assert "plan_group_count" not in step.outputs

    @pytest.mark.parametrize(
        "groups",
        [
            ["deliver export", "deliver import"],
            [_CAPABILITY_RESPONSE["task_groups"][0], "deliver import"],
            [{"description": "deliver export", "group_order": 1}],
            [{"group_id": "  ", "description": "deliver export"}],
            [{"group_id": 1, "description": "deliver export"}],
        ],
        ids=[
            "all-strings",
            "mixed",
            "no-identity",
            "blank-identity",
            "non-string-identity",
        ],
    )
    def test_capability_groups_that_are_not_group_objects_fail(
        self, tmp_path, groups,
    ):
        """Entries IMPLEMENT cannot schedule are a failed plan, not a coarse one.

        `_extract_sorted_groups` drops every non-dict entry, so a list of bare
        strings records a count of 2 while IMPLEMENT collapses it into one
        legacy call — neither the declared DAG nor the whole-task contract.
        """
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "task_groups" in (step.error_message or "")
        assert "task_groups" not in step.outputs
        assert "plan_group_count" not in step.outputs

    def test_capability_group_named_but_without_group_id_fails(self, tmp_path):
        """`name` is not an identity: `transitive_reduce` indexes `group_id`.

        The linear-chain preview swallows the resulting KeyError and leaves DAG
        selected, so accepting such a plan aborts IMPLEMENT with a traceback
        instead of failing here where the step can retry.
        """
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        groups = [
            {"name": "Export", "description": "deliver export", "group_order": 1},
            {
                "name": "Import",
                "description": "deliver import",
                "group_order": 2,
                "depends_on": [],
            },
        ]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "group_id" in (step.error_message or "")
        assert "task_groups" not in step.outputs
        assert "plan_group_count" not in step.outputs

    @pytest.mark.parametrize(
        "description",
        [None, "", "   "],
        ids=["missing", "empty", "blank"],
    )
    def test_capability_group_without_a_description_fails(
        self, tmp_path, description,
    ):
        """A coarse group's description is its whole work statement.

        Capability plans carry no per-task list, so a group reduced to bare
        scheduling metadata gives its isolated implement call nothing to work
        from — every such call falls back to the overall task description and
        implements the whole task, producing conflicting leaf merges.
        """
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        second = {"group_id": "G2", "group_order": 2, "depends_on": []}
        if description is not None:
            second["description"] = description
        groups = [_CAPABILITY_RESPONSE["task_groups"][0], second]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "description" in (step.error_message or "")
        assert "G2" in (step.error_message or "")
        assert "task_groups" not in step.outputs
        assert "plan_group_count" not in step.outputs

    @pytest.mark.parametrize(
        "repeated_id",
        ["G1", "G1 "],
        ids=["exact", "whitespace-variant"],
    )
    def test_capability_groups_with_a_repeated_group_id_fail(
        self, tmp_path, repeated_id,
    ):
        """A duplicate id is unrecoverable once persisted, so PLAN must reject it.

        The two topologies fail differently and neither can be retried out of:
        on the DAG path `DAGScheduler._build_dag` raises a raw
        `ValueError: Duplicate group_id`, and re-running IMPLEMENT re-reads the
        same groups; on the sequential path the repeat collapses into one node
        and both groups share `group_step_id` and the `impl/<flow>/G1` branch.
        Only PLAN's own retry can produce a different plan.
        """
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        groups = [
            _CAPABILITY_RESPONSE["task_groups"][0],
            {
                "group_id": repeated_id,
                "name": "Import",
                "description": "deliver import with its tests",
                "group_order": 2,
                "depends_on": [],
            },
        ]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "group_id" in (step.error_message or "")
        assert "G1" in (step.error_message or "")
        assert "task_groups" not in step.outputs
        assert "plan_group_count" not in step.outputs

    def test_capability_duplicate_survives_a_forked_dependency_shape(self, tmp_path):
        """The reported shape: a forked DAG whose last two groups share an id.

        The fork keeps the relay preview non-linear, so this plan reaches the
        DAG builder rather than collapsing to sequential — the path where the
        raw scheduler ValueError surfaces.
        """
        flow, step = _flow_and_step(tmp_path, decomposition="capability")

        def group(gid, order, depends_on):
            return {
                "group_id": gid,
                "name": f"Capability {gid}",
                "description": f"deliver {gid} with its tests",
                "group_order": order,
                "depends_on": depends_on,
            }

        groups = [
            group("G1", 1, []),
            group("G2", 2, ["G1"]),
            group("G3", 3, ["G1"]),
            group("G3", 4, ["G1"]),
        ]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "G3" in (step.error_message or "")
        assert "task_groups" not in step.outputs

    @pytest.mark.parametrize(
        "group_order",
        ["1", None, ["1"], True],
        ids=["quoted-int", "null", "list", "bool"],
    )
    def test_capability_unorderable_group_order_fails(self, tmp_path, group_order):
        """A group_order the sort cannot compare must bounce back into PLAN.

        `_extract_sorted_groups` sorts on the raw value at the very top of the
        grouped IMPLEMENT dispatch, before any path branches, comparing the
        groups' orders against each other and against the `0` default of a group
        that omits the field. A quoted order beside a plain one — a routine JSON
        typing slip — raises a raw `TypeError` there, and a Retry re-reads the
        same persisted values and dies identically.
        """
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        groups = [
            dict(_CAPABILITY_RESPONSE["task_groups"][0], group_order=group_order),
            _CAPABILITY_RESPONSE["task_groups"][1],
        ]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "group_order" in (step.error_message or "")
        assert "G1" in (step.error_message or "")
        assert "task_groups" not in step.outputs
        assert "plan_group_count" not in step.outputs

    def test_capability_quoted_group_order_would_abort_implement(self, tmp_path):
        """Pin the divergence the guard closes: the sort really does die on it."""
        from tianluo.engine.steps.implement import _extract_sorted_groups

        groups = [
            dict(_CAPABILITY_RESPONSE["task_groups"][0], group_order="1"),
            _CAPABILITY_RESPONSE["task_groups"][1],
        ]
        with pytest.raises(TypeError):
            _extract_sorted_groups(groups)

    def test_capability_missing_group_order_is_accepted(self, tmp_path):
        """An omitted order is orderable: every group takes the same `0` default."""
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        groups = []
        for raw in _CAPABILITY_RESPONSE["task_groups"]:
            group = dict(raw)
            group.pop("group_order", None)
            groups.append(group)
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.COMPLETED
        assert step.outputs["plan_group_count"] == 2

    def test_capability_distinct_group_ids_are_accepted(self, tmp_path):
        """The uniqueness guard must not fire on a well-formed multi-group plan."""
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        status, _ = _run_plan(flow, step, _CAPABILITY_RESPONSE)

        assert status == StepStatus.COMPLETED
        assert step.outputs["plan_group_count"] == 2

    def test_capability_dependency_cycle_fails(self, tmp_path):
        """A cycle is unrecoverable once persisted, so PLAN must reject it.

        Under the capability doctrine every multi-group plan reaches
        `DAGScheduler`, whose `_build_dag` raises a raw
        `ValueError: Cycle detected in DAG`; the linear-chain preview that runs
        first swallows its own exception and leaves DAG selected. A Retry
        re-reads the same edges and dies identically — only a new plan breaks
        the cycle.
        """
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        groups = [
            dict(_CAPABILITY_RESPONSE["task_groups"][0], depends_on=["G2"]),
            dict(_CAPABILITY_RESPONSE["task_groups"][1], depends_on=["G1"]),
        ]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "cycle" in (step.error_message or "").lower()
        assert "task_groups" not in step.outputs
        assert "plan_group_count" not in step.outputs

    def test_capability_self_dependency_fails(self, tmp_path):
        """A group depending on itself is a one-node cycle, not an ordering."""
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        groups = [dict(_CAPABILITY_RESPONSE["task_groups"][0], depends_on=["G1"])]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "cycle" in (step.error_message or "").lower()

    def test_capability_cycle_behind_a_fork_fails(self, tmp_path):
        """The cycle need not involve every group to abort the whole schedule."""
        flow, step = _flow_and_step(tmp_path, decomposition="capability")

        def group(gid, depends_on):
            return {
                "group_id": gid,
                "name": f"Capability {gid}",
                "description": f"deliver {gid} with its tests",
                "group_order": 1,
                "depends_on": depends_on,
            }

        groups = [
            group("G1", []),
            group("G2", ["G1", "G4"]),
            group("G3", ["G1"]),
            group("G4", ["G2"]),
        ]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "cycle" in (step.error_message or "").lower()

    @pytest.mark.parametrize(
        "depends_on",
        ["G1", 3, {"G1": True}],
        ids=["string", "int", "mapping"],
    )
    def test_capability_non_list_depends_on_fails(self, tmp_path, depends_on):
        """The scheduler iterates the edges directly; any other shape aborts it."""
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        groups = [
            _CAPABILITY_RESPONSE["task_groups"][0],
            dict(_CAPABILITY_RESPONSE["task_groups"][1], depends_on=depends_on),
        ]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "depends_on" in (step.error_message or "")
        assert "task_groups" not in step.outputs

    @pytest.mark.parametrize(
        "dep",
        [{"group_id": "G1"}, None, "", "  "],
        ids=["object", "null", "empty", "blank"],
    )
    def test_capability_non_id_depends_on_entry_fails(self, tmp_path, dep):
        """An edge that is not an id string can neither be matched nor ordered.

        `DAGScheduler` tests membership with `dep not in all_ids`, so an
        unhashable entry raises before any ordering happens.
        """
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        groups = [dict(_CAPABILITY_RESPONSE["task_groups"][0], depends_on=[dep])]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "depends_on" in (step.error_message or "")

    def test_capability_acyclic_edges_are_accepted(self, tmp_path):
        """A well-formed chain (including a redundant edge) must still pass."""
        flow, step = _flow_and_step(tmp_path, decomposition="capability")

        def group(gid, depends_on):
            return {
                "group_id": gid,
                "name": f"Capability {gid}",
                "description": f"deliver {gid} with its tests",
                "group_order": 1,
                "depends_on": depends_on,
            }

        groups = [group("G1", []), group("G2", ["G1"]), group("G3", ["G1", "G2"])]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.COMPLETED
        assert step.outputs["plan_group_count"] == 3

    def test_capability_missing_depends_on_is_accepted(self, tmp_path):
        """An omitted (or null) depends_on is "no edges", not a malformed plan."""
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        first = dict(_CAPABILITY_RESPONSE["task_groups"][0])
        first.pop("depends_on")
        groups = [first, dict(_CAPABILITY_RESPONSE["task_groups"][1], depends_on=None)]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.COMPLETED
        assert step.outputs["plan_group_count"] == 2

    def test_capability_dangling_edge_fails(self, tmp_path):
        """An edge naming no declared group is a mistyped reference, not an order.

        A fresh plan's enumeration is complete — nothing has been completed or
        pre-merged — so no edge can legitimately point outside it. Left to
        `DAGScheduler`, the edge is dropped as "already satisfied" with only a
        log warning and the dependent group gets in_degree 0, running
        concurrently in a worktree that lacks its prerequisite's code.
        `DAGScheduler`'s tolerance is for the *reduced* to-run set of a recovery
        run, which never flows through this check.
        """
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        groups = [
            _CAPABILITY_RESPONSE["task_groups"][0],
            dict(_CAPABILITY_RESPONSE["task_groups"][1], depends_on=["G0"]),
        ]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "depends_on" in (step.error_message or "")
        assert "G0" in (step.error_message or "")
        assert "task_groups" not in step.outputs
        assert "plan_group_count" not in step.outputs

    def test_capability_dangling_edge_alongside_a_valid_one_fails(self, tmp_path):
        """One resolvable edge does not excuse an unresolvable sibling edge."""
        flow, step = _flow_and_step(tmp_path, decomposition="capability")

        def group(gid, depends_on):
            return {
                "group_id": gid,
                "name": f"Capability {gid}",
                "description": f"deliver {gid} with its tests",
                "group_order": 1,
                "depends_on": depends_on,
            }

        groups = [group("G1", []), group("G2", ["G1", "G7"])]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "G7" in (step.error_message or "")

    def test_padded_ids_and_edges_are_persisted_stripped(self, tmp_path):
        """Validation and scheduling must key on the same strings.

        The guard resolves edges against stripped ids, but `DAGScheduler`
        builds `all_ids` from whatever the persisted groups carry. If only the
        validation stripped, `"G1 "` would resolve here and then miss `all_ids`
        there, dropping the edge as already satisfied.
        """
        flow, step = _flow_and_step(tmp_path, decomposition="capability")

        def group(gid, depends_on):
            return {
                "group_id": gid,
                "name": f"Capability {gid.strip()}",
                "description": f"deliver {gid.strip()} with its tests",
                "group_order": 1,
                "depends_on": depends_on,
            }

        groups = [group("G1", []), group(" G2 ", ["G1 "])]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.COMPLETED
        stored = step.outputs["task_groups"]
        assert [g["group_id"] for g in stored] == ["G1", "G2"]
        assert stored[1]["depends_on"] == ["G1"]

        # The whole point: the scheduler now sees the declared ordering.
        from tianluo.engine.dag_scheduler import DAGScheduler

        assert DAGScheduler(stored)._in_degree == {"G1": 0, "G2": 1}

    def test_a_rejected_plan_is_not_rewritten(self, tmp_path):
        """Normalization applies only once the whole enumeration passes.

        A failed plan is discarded and re-planned, so half-rewriting it would
        only obscure what the model actually emitted.
        """
        flow, step = _flow_and_step(tmp_path, decomposition="capability")

        def group(gid, depends_on):
            return {
                "group_id": gid,
                "name": f"Capability {gid.strip()}",
                "description": f"deliver {gid.strip()} with its tests",
                "group_order": 1,
                "depends_on": depends_on,
            }

        groups = [group("G1 ", []), group("G2", ["G9"])]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert groups[0]["group_id"] == "G1 "

    def test_a_padded_id_still_collides_with_its_unpadded_twin(self, tmp_path):
        """Stripping ids must not turn a duplicate into two accepted groups."""
        flow, step = _flow_and_step(tmp_path, decomposition="capability")
        groups = [
            dict(_CAPABILITY_RESPONSE["task_groups"][0], group_id="G1"),
            dict(_CAPABILITY_RESPONSE["task_groups"][1], group_id="G1 ", depends_on=[]),
        ]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.FAILED
        assert "G1" in (step.error_message or "")

    def test_granular_plan_tolerates_a_dangling_edge(self, tmp_path):
        """The edge guard is capability-only; legacy behaviour is unchanged."""
        flow, step = _flow_and_step(tmp_path, decomposition="granular")
        groups = [
            _CAPABILITY_RESPONSE["task_groups"][0],
            dict(_CAPABILITY_RESPONSE["task_groups"][1], depends_on=["G0"]),
        ]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.COMPLETED

    def test_granular_plan_tolerates_a_dependency_cycle(self, tmp_path):
        """The edge guard is capability-only; legacy behaviour is intact."""
        flow, step = _flow_and_step(tmp_path, decomposition="granular")
        groups = [
            dict(_CAPABILITY_RESPONSE["task_groups"][0], depends_on=["G2"]),
            dict(_CAPABILITY_RESPONSE["task_groups"][1], depends_on=["G1"]),
        ]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.COMPLETED
        assert len(step.outputs["task_groups"]) == 2

    def test_granular_plan_tolerates_duplicate_group_ids(self, tmp_path):
        """The uniqueness guard is capability-only; legacy behaviour is intact."""
        flow, step = _flow_and_step(tmp_path, decomposition="granular")
        duplicate = dict(_CAPABILITY_RESPONSE["task_groups"][1], group_id="G1")
        groups = [_CAPABILITY_RESPONSE["task_groups"][0], duplicate]
        response = dict(_CAPABILITY_RESPONSE, task_groups=groups)
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.COMPLETED
        assert len(step.outputs["task_groups"]) == 2

    def test_granular_plan_tolerates_non_object_groups(self, tmp_path):
        """The per-entry guard is capability-only; legacy behaviour is intact."""
        flow, step = _flow_and_step(tmp_path, decomposition="granular")
        response = dict(
            _CAPABILITY_RESPONSE, task_groups=["deliver export", "deliver import"],
        )
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.COMPLETED
        assert step.outputs["task_groups"] == ["deliver export", "deliver import"]

    def test_empty_granular_plan_keeps_its_legacy_behaviour(self, tmp_path):
        """The guard is capability-only; `granular` stays byte-for-byte legacy."""
        flow, step = _flow_and_step(tmp_path, decomposition="granular")
        response = dict(_CAPABILITY_RESPONSE, task_groups=[])
        status, _ = _run_plan(flow, step, response)

        assert status == StepStatus.COMPLETED
        assert step.outputs["task_groups"] == []

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
