"""Tests for implement_handler's execution strategy dispatch.

Verifies the four dispatch outcomes introduced by the
``implement.use_worktree`` / linear-chain short-circuit rules:

1. ``use_worktree=False`` + DAG-eligible groups → sequential
2. ``use_worktree=True`` + linear chain (above LOC threshold) → sequential
3. ``use_worktree=True`` + fork DAG (above LOC threshold) → DAG parallel
4. ``use_worktree=True`` + ``_should_use_dag`` False (small LOC) → sequential

Dispatch is verified by patching ``_run_dag_parallel`` (asserts it is or
is not called) and ``LLMCaller`` (one call per group on the sequential
path). No real git or worktree operations are performed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from tianluo.config import ImplementConfig
from tianluo.agent_runner import AgentInvocationIntent
from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


# Fork DAG: G1 → G2 and G1 → G3. fork_from is non-empty, so
# _relay_plan_is_linear() returns False and DAG is preserved.
FORK_GROUPS = [
    {
        "group_id": "G1",
        "group_order": 1,
        "depends_on": [],
        "tasks": [{"id": 1, "estimated_loc": 200}],
    },
    {
        "group_id": "G2",
        "group_order": 2,
        "depends_on": ["G1"],
        "tasks": [{"id": 2, "estimated_loc": 200}],
    },
    {
        "group_id": "G3",
        "group_order": 3,
        "depends_on": ["G1"],
        "tasks": [{"id": 3, "estimated_loc": 200}],
    },
]

# Linear chain: G1 → G2 → G3. fork_from empty, single root → linear.
LINEAR_GROUPS = [
    {
        "group_id": "G1",
        "group_order": 1,
        "depends_on": [],
        "tasks": [{"id": 1, "estimated_loc": 200}],
    },
    {
        "group_id": "G2",
        "group_order": 2,
        "depends_on": ["G1"],
        "tasks": [{"id": 2, "estimated_loc": 200}],
    },
    {
        "group_id": "G3",
        "group_order": 3,
        "depends_on": ["G2"],
        "tasks": [{"id": 3, "estimated_loc": 200}],
    },
]

# Small multi-group below the default LOC threshold (300). _should_use_dag
# returns False because total_loc <= threshold.
SMALL_GROUPS = [
    {
        "group_id": "G1",
        "group_order": 1,
        "depends_on": [],
        "tasks": [{"id": 1, "estimated_loc": 50}],
    },
    {
        "group_id": "G2",
        "group_order": 2,
        "depends_on": ["G1"],
        "tasks": [{"id": 2, "estimated_loc": 50}],
    },
]


def _make_step_flow(tmp_path, groups, decomposition="granular"):
    """Build an IMPLEMENT step/flow pair for the dispatch tests.

    The doctrine defaults to ``granular`` because every group above carries
    per-task ``estimated_loc``: the LOC gate these tests exercise is granular /
    legacy scheduling by definition. Stating it explicitly also keeps the
    fixture honest — omitting the key entirely no longer means "legacy", it
    means the current default doctrine (capability), which bypasses that gate.
    """
    step = Step(
        step_type=StepType.IMPLEMENT,
        step_id="test-dispatch",
        inputs={
            "task_description": "test",
            "task_type": "feature",
            "task_groups": groups,
            "spec_content": {},
            "design_doc": {},
            "plan_decomposition": decomposition,
        },
    )
    flow = FlowInstance(
        flow_id="test-flow",
        task_description="test",
        change_path=tmp_path / "tianluo",
    )
    return step, flow


_SEQ_PARSED = {
    "files_changed": ["a.py"],
    "tests_added": [],
    "test_mapping": {},
    "summary": "done",
    "completion_status": "complete",
    "incomplete_tasks": [],
    "restricted_edits": [],
}


_IMP = "tianluo.engine.steps.implement"


# ---------------------------------------------------------------------------
# Dispatch tests
# ---------------------------------------------------------------------------


class TestExecutionStrategyDispatch:
    """implement_handler's four dispatch paths."""

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}.parse_json_response", return_value=_SEQ_PARSED)
    @patch(f"{_IMP}.LLMCaller")
    @patch(f"{_IMP}._run_dag_parallel")
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(group_loc_threshold=300, use_worktree=False),
    )
    def test_use_worktree_false_forces_sequential_even_on_dag_eligible_groups(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_dag,
        mock_caller_cls,
        mock_parse,
        mock_resolve,
        tmp_path,
    ):
        """use_worktree=False + fork DAG with total_loc > threshold: no DAG call."""
        from tianluo.engine.steps.implement import implement_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps(_SEQ_PARSED)
        mock_caller_cls.return_value = mock_caller

        step, flow = _make_step_flow(tmp_path, FORK_GROUPS)
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_not_called()
        # Sequential path creates one LLMCaller per group (3 groups).
        assert mock_caller_cls.call_count == len(FORK_GROUPS)

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}.parse_json_response", return_value=_SEQ_PARSED)
    @patch(f"{_IMP}.LLMCaller")
    @patch(f"{_IMP}._run_dag_parallel")
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(group_loc_threshold=300, use_worktree=True),
    )
    def test_linear_chain_falls_back_to_sequential(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_dag,
        mock_caller_cls,
        mock_parse,
        mock_resolve,
        tmp_path,
    ):
        """use_worktree=True + linear chain above LOC threshold: no DAG call."""
        from tianluo.engine.steps.implement import implement_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps(_SEQ_PARSED)
        mock_caller_cls.return_value = mock_caller

        step, flow = _make_step_flow(tmp_path, LINEAR_GROUPS)
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_not_called()
        assert mock_caller_cls.call_count == len(LINEAR_GROUPS)

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}._run_dag_parallel", return_value=StepStatus.COMPLETED)
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(group_loc_threshold=300, use_worktree=True),
    )
    def test_fork_dag_uses_dag_parallel(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_dag,
        mock_resolve,
        tmp_path,
    ):
        """use_worktree=True + fork DAG above LOC threshold: DAG parallel runs."""
        from tianluo.engine.steps.implement import implement_handler

        step, flow = _make_step_flow(tmp_path, FORK_GROUPS)
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_called_once()

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}.parse_json_response", return_value=_SEQ_PARSED)
    @patch(f"{_IMP}.LLMCaller")
    @patch(f"{_IMP}._run_dag_parallel")
    @patch(f"{_IMP}._run_single_llm_call", return_value=StepStatus.COMPLETED)
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(group_loc_threshold=300, use_worktree=True),
    )
    def test_small_multi_group_collapses_to_single_call(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_single,
        mock_dag,
        mock_caller_cls,
        mock_parse,
        mock_resolve,
        tmp_path,
    ):
        """use_worktree=True + total LOC <= threshold: LOC-merge single call, no DAG."""
        from tianluo.engine.steps.implement import implement_handler

        step, flow = _make_step_flow(tmp_path, SMALL_GROUPS)
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_not_called()
        # Small multi-group tasks take the LOC-merge single-call path,
        # not the per-group sequential loop.
        mock_single.assert_called_once()


# Two coarse capability groups: no per-task breakdown, no estimated_loc, and
# independent of each other — the default doctrine's primary multi-group shape.
CAPABILITY_GROUPS = [
    {
        "group_id": "G1",
        "name": "Export pipeline",
        "description": "Deliver CSV export end to end.",
        "group_order": 1,
        "depends_on": [],
    },
    {
        "group_id": "G2",
        "name": "Import pipeline",
        "description": "Deliver CSV import end to end.",
        "group_order": 2,
        "depends_on": [],
    },
]


class TestCapabilityGroupPrompt:
    """A coarse group must be told to decompose itself, not to run a task list."""

    def test_capability_variant_replaces_the_enumeration_wording(self):
        from tianluo.engine.steps.implement import (
            IMPLEMENT_CAPABILITY_GROUP_PROMPT,
            IMPLEMENT_GROUP_PROMPT,
        )

        fields = dict(
            task_description="TD",
            task_type="feature",
            design_section="",
            current_group=json.dumps(CAPABILITY_GROUPS[0]),
            previous_results="No previous groups.",
            spec_summary="SS",
            root_cause_section="",
        )
        capability = IMPLEMENT_CAPABILITY_GROUP_PROMPT.format(**fields)
        granular = IMPLEMENT_GROUP_PROMPT.format(**fields)

        # The granular template points at an enumeration a capability group
        # does not carry; the capability one must not.
        assert "Implement the tasks listed in Current Group Tasks above." in granular
        assert (
            "Implement the tasks listed in Current Group Tasks above."
            not in capability
        )
        assert "## Current Capability Group" in capability
        assert "Decompose the task described in Current Capability Group" in capability
        assert "sub-agent" in capability
        # Tests belong to the group itself — the artifact-split ban's
        # execution-side counterpart.
        assert "own tests" in capability

        # The legacy template is untouched by the derivation.
        assert "Capability" not in granular
        # Everything not doctrine-specific keeps a single source.
        assert "Do Not Bump Version Files" in capability
        assert "Agent Safety: Process Cleanup" in capability
        assert '"completion_status"' in capability

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}.parse_json_response", return_value=_SEQ_PARSED)
    @patch(f"{_IMP}.LLMCaller")
    @patch(f"{_IMP}._run_dag_parallel")
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch(
        "tianluo.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(group_loc_threshold=300, use_worktree=False),
    )
    def test_sequential_capability_groups_get_the_capability_prompt(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_dag,
        mock_caller_cls,
        mock_parse,
        mock_resolve,
        tmp_path,
    ):
        from tianluo.engine.steps.implement import implement_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps(_SEQ_PARSED)
        mock_caller_cls.return_value = mock_caller

        step, flow = _make_step_flow(
            tmp_path, CAPABILITY_GROUPS, decomposition="capability",
        )
        assert implement_handler(step, flow) == StepStatus.COMPLETED

        prompts = [c.kwargs["prompt"] for c in mock_caller.call.call_args_list]
        assert len(prompts) == len(CAPABILITY_GROUPS)
        for prompt in prompts:
            assert "## Current Capability Group" in prompt
            assert "Decompose the task described in Current Capability Group" in prompt
            assert (
                "Implement the tasks listed in Current Group Tasks above."
                not in prompt
            )

    def test_dag_path_forwards_the_doctrine_to_the_group_executor(self):
        """The DAG path is the capability doctrine's primary multi-group shape."""
        import inspect

        from tianluo.engine.steps import implement as impl

        assert (
            "capability_mode"
            in inspect.signature(impl._run_dag_parallel).parameters
        )
        assert (
            "capability_mode"
            in inspect.signature(impl._make_execute_fn).parameters
        )
        assert (
            impl._group_prompt_template(True)
            is impl.IMPLEMENT_CAPABILITY_GROUP_PROMPT
        )
        assert impl._group_prompt_template(False) is impl.IMPLEMENT_GROUP_PROMPT

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}._run_dag_parallel", return_value=StepStatus.COMPLETED)
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch(
        "tianluo.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(group_loc_threshold=300, use_worktree=True),
    )
    def test_independent_capability_groups_run_dag_with_capability_mode(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_dag,
        mock_resolve,
        tmp_path,
    ):
        from tianluo.engine.steps.implement import implement_handler

        step, flow = _make_step_flow(
            tmp_path, CAPABILITY_GROUPS, decomposition="capability",
        )
        assert implement_handler(step, flow) == StepStatus.COMPLETED
        mock_dag.assert_called_once()
        assert mock_dag.call_args.kwargs["capability_mode"] is True


class TestHolisticExecutionStrategy:
    """A single capability group — and ``small`` — run one whole-task executor.

    The shape is read off PLAN's group count, so these fixtures carry a real
    ``task_groups`` list rather than a routing flag.
    """

    @staticmethod
    def _flow_step(
        tmp_path,
        *,
        task_type="feature",
        decomposition="capability",
        granularity="auto",
        groups=None,
    ):
        flow = FlowInstance(
            flow_id="holistic-flow",
            task_description="Implement the complete requirement",
            task_type=task_type,
            change_path=tmp_path / "tianluo",
        )
        flow.state.context["plan_decomposition"] = decomposition
        flow.state.context["plan_granularity"] = granularity
        if groups is None:
            groups = [
                {
                    "group_id": "G1",
                    "name": "MUST_NOT_APPEAR",
                    "description": "the whole requirement",
                    "group_order": 1,
                    "depends_on": [],
                }
            ]
        step = Step(
            step_type=StepType.IMPLEMENT,
            step_id="holistic-implement",
            inputs={
                "task_description": "Implement the complete requirement",
                "task_type": task_type,
                "plan_decomposition": decomposition,
                "plan_granularity": granularity,
                "task_groups": groups,
                "analysis_context": {
                    "scope": "cross-module",
                    "complexity": "medium",
                    "reasoning": "one autonomous call is reasonable",
                    "project_summary": "project-summary-marker",
                },
                "root_cause_report": {
                    "root_cause": "root-cause-marker",
                    "evidence": [],
                },
            },
        )
        return flow, step

    def test_single_capability_group_is_holistic(self, tmp_path):
        from tianluo.engine.steps.implement import _holistic_execution_mode

        flow, step = self._flow_step(tmp_path)
        assert _holistic_execution_mode(step, flow) == "single_group"

    def test_two_capability_groups_are_not_holistic(self, tmp_path):
        from tianluo.engine.steps.implement import _holistic_execution_mode

        flow, step = self._flow_step(
            tmp_path,
            groups=[
                {"group_id": "G1", "group_order": 1, "depends_on": []},
                {"group_id": "G2", "group_order": 2, "depends_on": []},
            ],
        )
        assert _holistic_execution_mode(step, flow) is None

    def test_forced_single_granularity_is_holistic_despite_many_groups(
        self, tmp_path,
    ):
        """`single` is a guarantee the engine keeps, not a request to PLAN."""
        from tianluo.engine.steps.implement import _holistic_execution_mode

        flow, step = self._flow_step(
            tmp_path,
            granularity="single",
            groups=[
                {"group_id": "G1", "group_order": 1, "depends_on": []},
                {"group_id": "G2", "group_order": 2, "depends_on": []},
                {"group_id": "G3", "group_order": 3, "depends_on": ["G1"]},
            ],
        )
        assert _holistic_execution_mode(step, flow) == "single_group"

    def test_forced_single_is_read_off_context_when_inputs_lack_it(
        self, tmp_path,
    ):
        """Persisted state stays authoritative if the input never carried it."""
        from tianluo.engine.steps.implement import _holistic_execution_mode

        flow, step = self._flow_step(
            tmp_path,
            granularity="single",
            groups=[
                {"group_id": "G1", "group_order": 1, "depends_on": []},
                {"group_id": "G2", "group_order": 2, "depends_on": []},
            ],
        )
        del step.inputs["plan_granularity"]
        assert _holistic_execution_mode(step, flow) == "single_group"

    def test_forced_single_does_not_reshape_the_granular_doctrine(
        self, tmp_path,
    ):
        """Granularity only applies under capability; legacy stays grouped."""
        from tianluo.engine.steps.implement import _holistic_execution_mode

        flow, step = self._flow_step(
            tmp_path,
            decomposition="granular",
            granularity="single",
            groups=[
                {"group_id": "G1", "group_order": 1, "depends_on": []},
                {"group_id": "G2", "group_order": 2, "depends_on": []},
            ],
        )
        assert _holistic_execution_mode(step, flow) is None

    def test_conservative_granularity_still_follows_the_group_count(
        self, tmp_path,
    ):
        from tianluo.engine.steps.implement import _holistic_execution_mode

        flow, step = self._flow_step(
            tmp_path,
            granularity="conservative",
            groups=[
                {"group_id": "G1", "group_order": 1, "depends_on": []},
                {"group_id": "G2", "group_order": 2, "depends_on": []},
            ],
        )
        assert _holistic_execution_mode(step, flow) is None

    def test_zero_capability_groups_reach_the_whole_task_shape(self, tmp_path):
        """A groupless capability plan must not get the per-group contract.

        PLAN now rejects an empty plan, so this can only arrive from a plan
        persisted before that guard; the whole task still has to be delivered
        by one autonomous call, which is the one-group shape.
        """
        from tianluo.engine.steps.implement import _holistic_execution_mode

        flow, step = self._flow_step(tmp_path, groups=[])
        assert _holistic_execution_mode(step, flow) == "single_group"

    def test_a_flow_that_has_not_run_plan_is_not_holistic(self, tmp_path):
        """No count at all is "unknown", which must not read as "one"."""
        from tianluo.engine.steps.implement import _holistic_execution_mode

        flow, step = self._flow_step(tmp_path)
        del step.inputs["task_groups"]
        assert _holistic_execution_mode(step, flow) is None

    def test_single_granular_group_is_not_holistic(self, tmp_path):
        """Legacy doctrine keeps its group path even at one group."""
        from tianluo.engine.steps.implement import _holistic_execution_mode

        flow, step = self._flow_step(tmp_path, decomposition="granular")
        assert _holistic_execution_mode(step, flow) is None

    def test_a_doctrineless_flow_is_shaped_the_way_plan_planned_it(
        self, tmp_path,
    ):
        """PLAN and IMPLEMENT must not fall back to different doctrines.

        A pre-upgrade ``--type pending`` flow recorded the provisional
        ``effective_implementation_strategy: not_applicable`` and no doctrine
        at all. Resumed after the upgrade, ANALYZE rebuilds a sequence with
        PLAN, and PLAN — reading ``PlanModeResolver.view`` — runs the
        capability prompt and emits one coarse group with no ``tasks`` and no
        ``estimated_loc``. IMPLEMENT must therefore reach the whole-task call,
        not the per-task group prompt for an enumeration that does not exist.
        """
        from tianluo.engine.plan_decomposition import PlanModeResolver
        from tianluo.engine.steps.implement import _holistic_execution_mode

        flow, step = self._flow_step(tmp_path)
        del flow.state.context["plan_decomposition"]
        del flow.state.context["plan_granularity"]
        del step.inputs["plan_decomposition"]
        del step.inputs["plan_granularity"]
        flow.state.context["requested_implementation_strategy"] = "planned"
        flow.state.context["effective_implementation_strategy"] = "not_applicable"

        assert (
            PlanModeResolver.view(flow.state.context).decomposition.value
            == "capability"
        )
        assert _holistic_execution_mode(step, flow) == "single_group"

    def test_a_legacy_planned_flow_still_keeps_its_group_path(self, tmp_path):
        """The one legacy marker that really means "granular" is honoured.

        A flow planned before the model existed carries per-task
        ``estimated_loc`` and must keep the LOC-driven scheduling it was
        planned under; ``planned`` is the marker that says so.
        """
        from tianluo.engine.steps.implement import _holistic_execution_mode

        flow, step = self._flow_step(tmp_path)
        del flow.state.context["plan_decomposition"]
        del flow.state.context["plan_granularity"]
        del step.inputs["plan_decomposition"]
        del step.inputs["plan_granularity"]
        flow.state.context["effective_implementation_strategy"] = "planned"
        assert _holistic_execution_mode(step, flow) is None

    @staticmethod
    def _legacy_direct_flow_step(tmp_path):
        """A flow created under the retired axis: no PLAN, hence no groups."""
        flow = FlowInstance(
            flow_id="legacy-direct-flow",
            task_description="Implement the complete requirement",
            task_type="feature",
            change_path=tmp_path / "tianluo",
        )
        flow.state.context["effective_implementation_strategy"] = "direct"
        step = Step(
            step_type=StepType.IMPLEMENT,
            step_id="legacy-direct-implement",
            inputs={
                "task_description": "Implement the complete requirement",
                "task_type": "feature",
                "effective_implementation_strategy": "direct",
                "analysis_context": {
                    "scope": "cross-module",
                    "complexity": "medium",
                    "reasoning": "one call carries it",
                    "project_summary": "project-summary-marker",
                },
            },
        )
        return flow, step

    def test_legacy_direct_flow_is_holistic(self, tmp_path):
        """Upgrading mid-flow must not reclassify a `direct` run as grouped."""
        from tianluo.engine.state_machine import StateMachine
        from tianluo.engine.steps.implement import _holistic_execution_mode

        flow, step = self._legacy_direct_flow_step(tmp_path)
        assert _holistic_execution_mode(step, flow) == "legacy_direct"
        # The auto-continuation gate must see the same shape, or a PARTIAL
        # result would advance to TEST instead of re-entering IMPLEMENT.
        assert StateMachine._is_holistic_implement_step(flow, step)

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}._run_single_llm_call", return_value=StepStatus.COMPLETED)
    @patch(
        "tianluo.engine.context_builder.get_runtime_environment_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_code_index_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_charter_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    def test_legacy_direct_flow_runs_the_whole_task_prompt(
        self,
        mock_issue,
        mock_charter,
        mock_index,
        mock_runtime,
        mock_run,
        mock_resolve,
        tmp_path,
    ):
        """No empty `## Task Groups` list, and the partial attempt survives."""
        from tianluo.engine.steps.implement import implement_handler

        flow, step = self._legacy_direct_flow_step(tmp_path)
        step.inputs["previous_output"] = {
            "summary": "PARTIAL-ATTEMPT-MARKER",
            "completion_status": "partial",
        }
        status = implement_handler(step, flow)

        assert status == StepStatus.COMPLETED
        prompt = mock_run.call_args.args[0]
        assert "Task Groups" not in prompt
        assert "PARTIAL-ATTEMPT-MARKER" in prompt
        assert "project-summary-marker" in prompt
        # It never ran PLAN, so the prompt must not claim a sizing verdict.
        assert "single capability group" not in prompt
        assert "retired direct implementation strategy" in prompt
        assert mock_run.call_args.kwargs["implemented_groups_override"] == []
        assert (
            mock_run.call_args.kwargs["invocation_intent"]
            == AgentInvocationIntent.DIRECT_IMPLEMENTATION
        )
        assert step.outputs["implemented_groups"] == []

    def test_legacy_direct_flow_that_ran_plan_is_a_single_group(self, tmp_path):
        """ANALYZE rebuilds the sequence, so such a flow can acquire a plan."""
        from tianluo.engine.state_machine import StateMachine
        from tianluo.engine.steps.implement import _holistic_execution_mode

        flow, step = self._legacy_direct_flow_step(tmp_path)
        step.inputs["task_groups"] = [
            {"group_id": "G1", "group_order": 1, "depends_on": []},
            {"group_id": "G2", "group_order": 2, "depends_on": []},
        ]
        assert _holistic_execution_mode(step, flow) == "single_group"
        assert StateMachine._is_holistic_implement_step(flow, step)

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}._run_single_llm_call", return_value=StepStatus.COMPLETED)
    @patch(
        "tianluo.engine.context_builder.get_runtime_environment_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_code_index_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_charter_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    def test_legacy_direct_flow_that_ran_plan_keeps_its_groups_in_the_prompt(
        self,
        mock_issue,
        mock_charter,
        mock_index,
        mock_runtime,
        mock_run,
        mock_resolve,
        tmp_path,
    ):
        """The prompt must not deny a plan the flow's own history holds."""
        from tianluo.engine.steps.implement import implement_handler

        flow, step = self._legacy_direct_flow_step(tmp_path)
        step.inputs["task_groups"] = [
            {"group_id": "G1", "name": "GROUP-ONE-MARKER", "depends_on": []},
            {"group_id": "G2", "name": "GROUP-TWO-MARKER", "depends_on": []},
        ]
        assert implement_handler(step, flow) == StepStatus.COMPLETED

        prompt = mock_run.call_args.args[0]
        assert "retired direct implementation strategy" not in prompt
        assert "outline only" in prompt
        assert "GROUP-ONE-MARKER" in prompt and "GROUP-TWO-MARKER" in prompt
        assert (
            mock_run.call_args.kwargs["invocation_intent"]
            == AgentInvocationIntent.DIRECT_IMPLEMENTATION
        )

    def test_small_is_holistic_regardless_of_group_count(self, tmp_path):
        from tianluo.engine.steps.implement import _holistic_execution_mode

        flow, step = self._flow_step(
            tmp_path,
            task_type="small",
            decomposition="granular",
            groups=[
                {"group_id": "G1", "group_order": 1, "depends_on": []},
                {"group_id": "G2", "group_order": 2, "depends_on": []},
            ],
        )
        assert _holistic_execution_mode(step, flow) == "small"

    def test_handler_and_state_machine_agree_on_the_shape(self, tmp_path):
        """One predicate, two callers: the gate cannot disagree with the run."""
        from tianluo.engine.state_machine import StateMachine
        from tianluo.engine.steps.implement import _holistic_execution_mode

        for kwargs in (
            {},
            {"decomposition": "granular"},
            {"task_type": "small"},
            {"groups": [
                {"group_id": "G1", "group_order": 1, "depends_on": []},
                {"group_id": "G2", "group_order": 2, "depends_on": []},
            ]},
            {"granularity": "single", "groups": [
                {"group_id": "G1", "group_order": 1, "depends_on": []},
                {"group_id": "G2", "group_order": 2, "depends_on": []},
            ]},
        ):
            flow, step = self._flow_step(tmp_path, **kwargs)
            assert (
                _holistic_execution_mode(step, flow) is not None
            ) is StateMachine._is_holistic_implement_step(flow, step)

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}._run_single_llm_call", return_value=StepStatus.COMPLETED)
    @patch(
        "tianluo.engine.context_builder.get_runtime_environment_injection",
        return_value="\nRUNTIME-MARKER",
    )
    @patch(
        "tianluo.engine.context_builder.get_code_index_injection",
        return_value="\nCODE-INDEX-MARKER",
    )
    @patch(
        "tianluo.engine.context_builder.get_charter_injection",
        return_value="\nCHARTER-MARKER",
    )
    @patch(
        "tianluo.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    def test_single_group_prompt_is_whole_requirement_and_has_full_context(
        self,
        mock_issue,
        mock_charter,
        mock_index,
        mock_runtime,
        mock_run,
        mock_resolve,
        tmp_path,
    ):
        from tianluo.engine.steps.implement import implement_handler

        flow, step = self._flow_step(tmp_path)
        status = implement_handler(step, flow)

        assert status == StepStatus.COMPLETED
        prompt = mock_run.call_args.args[0]
        assert "Implement the complete requirement" in prompt
        assert "Independently analyze" in prompt
        assert "implement the requirement in full" in prompt
        assert "run targeted validation" in prompt
        assert "project-summary-marker" in prompt
        assert "root-cause-marker" in prompt
        assert "CHARTER-MARKER" in prompt
        assert "CODE-INDEX-MARKER" in prompt
        assert "Task Groups" not in prompt
        assert "MUST_NOT_APPEAR" not in prompt
        # The execution contract names PLAN's sizing verdict, not a strategy.
        assert "single capability group" in prompt
        assert "implementation_strategy" not in prompt
        assert mock_run.call_args.kwargs["implemented_groups_override"] == []
        assert (
            mock_run.call_args.kwargs["invocation_intent"]
            == AgentInvocationIntent.DIRECT_IMPLEMENTATION
        )
        assert step.outputs["implemented_groups"] == []

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}._run_single_llm_call", return_value=StepStatus.COMPLETED)
    @patch(
        "tianluo.engine.context_builder.get_runtime_environment_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_code_index_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_charter_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    def test_single_group_prompt_carries_the_design_and_the_group_scope(
        self,
        mock_issue,
        mock_charter,
        mock_index,
        mock_runtime,
        mock_run,
        mock_resolve,
        tmp_path,
    ):
        """A one-group plan is still a plan: its design must reach the call."""
        from tianluo.engine.steps.implement import implement_handler

        flow, step = self._flow_step(
            tmp_path,
            groups=[
                {
                    "group_id": "G1",
                    "name": "MUST_NOT_APPEAR",
                    "description": "GROUP-SCOPE-MARKER",
                    "group_order": 1,
                    "depends_on": [],
                }
            ],
        )
        step.inputs["design_doc"] = {
            "overview": "DESIGN-OVERVIEW-MARKER",
            "architecture_decisions": ["DESIGN-DECISION-MARKER"],
        }
        assert implement_handler(step, flow) == StepStatus.COMPLETED

        prompt = mock_run.call_args.args[0]
        assert "## Design Document" in prompt
        assert "DESIGN-OVERVIEW-MARKER" in prompt
        assert "DESIGN-DECISION-MARKER" in prompt
        # PLAN's scope statement for this one call, without reintroducing the
        # per-group enumeration this mode denies.
        assert "GROUP-SCOPE-MARKER" in prompt
        assert "MUST_NOT_APPEAR" not in prompt
        assert "Task Groups" not in prompt

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}._run_single_llm_call", return_value=StepStatus.COMPLETED)
    @patch(
        "tianluo.engine.context_builder.get_runtime_environment_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_code_index_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_charter_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    def test_forced_single_collapse_also_carries_the_design(
        self,
        mock_issue,
        mock_charter,
        mock_index,
        mock_runtime,
        mock_run,
        mock_resolve,
        tmp_path,
    ):
        """Pinning the shape must not drop what PLAN designed for it."""
        from tianluo.engine.steps.implement import implement_handler

        flow, step = self._flow_step(
            tmp_path,
            granularity="single",
            groups=[
                {"group_id": "G1", "group_order": 1, "depends_on": []},
                {"group_id": "G2", "group_order": 2, "depends_on": []},
            ],
        )
        step.inputs["design_doc"] = {"overview": "DESIGN-OVERVIEW-MARKER"}
        assert implement_handler(step, flow) == StepStatus.COMPLETED

        prompt = mock_run.call_args.args[0]
        assert "## Design Document" in prompt
        assert "DESIGN-OVERVIEW-MARKER" in prompt

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}._run_single_llm_call", return_value=StepStatus.COMPLETED)
    @patch(
        "tianluo.engine.context_builder.get_runtime_environment_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_code_index_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_charter_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    def test_legacy_direct_prompt_renders_no_design_section(
        self,
        mock_issue,
        mock_charter,
        mock_index,
        mock_runtime,
        mock_run,
        mock_resolve,
        tmp_path,
    ):
        """The one mode that truly has no plan must not claim to have one."""
        from tianluo.engine.steps.implement import implement_handler

        flow, step = self._legacy_direct_flow_step(tmp_path)
        # Defensive: such a flow never ran PLAN, so even a design_doc left in
        # inputs by an unrelated path must not turn into a design section.
        step.inputs["design_doc"] = {"overview": "MUST_NOT_APPEAR"}
        assert implement_handler(step, flow) == StepStatus.COMPLETED

        prompt = mock_run.call_args.args[0]
        assert "## Design Document" not in prompt
        assert "MUST_NOT_APPEAR" not in prompt

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}._run_single_llm_call", return_value=StepStatus.COMPLETED)
    @patch(
        "tianluo.engine.context_builder.get_runtime_environment_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_code_index_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_charter_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    def test_forced_single_collapses_groups_into_one_call_without_losing_them(
        self,
        mock_issue,
        mock_charter,
        mock_index,
        mock_runtime,
        mock_run,
        mock_resolve,
        tmp_path,
    ):
        """One call runs, and the plan it must cover is still in the prompt."""
        from tianluo.engine.steps.implement import implement_handler

        flow, step = self._flow_step(
            tmp_path,
            granularity="single",
            groups=[
                {
                    "group_id": "G1",
                    "name": "GROUP-ONE-MARKER",
                    "group_order": 1,
                    "depends_on": [],
                },
                {
                    "group_id": "G2",
                    "name": "GROUP-TWO-MARKER",
                    "group_order": 2,
                    "depends_on": ["G1"],
                },
            ],
        )
        status = implement_handler(step, flow)

        assert status == StepStatus.COMPLETED
        assert mock_run.call_count == 1
        prompt = mock_run.call_args.args[0]
        assert "plan_granularity=single" in prompt
        assert "GROUP-ONE-MARKER" in prompt
        assert "GROUP-TWO-MARKER" in prompt
        # The forced collapse must not claim PLAN sized this as one group.
        assert "PLAN sized this task as a single capability group" not in prompt
        assert mock_run.call_args.kwargs["implemented_groups_override"] == []
        assert (
            mock_run.call_args.kwargs["invocation_intent"]
            == AgentInvocationIntent.DIRECT_IMPLEMENTATION
        )
        assert step.outputs["implemented_groups"] == []

    @patch(f"{_IMP}._apply_restricted_edits", return_value=([], []))
    @patch(f"{_IMP}.LLMCaller")
    def test_nonempty_incomplete_tasks_override_complete_and_preserve_outputs(
        self, mock_caller_cls, mock_apply, tmp_path,
    ):
        from tianluo.engine.steps.implement import _run_single_llm_call

        flow, step = self._flow_step(tmp_path)
        step.outputs.update({
            "files_changed": ["first.py"],
            "tests_added": ["test_first.py"],
            "test_mapping": {"first.py": ["test_first.py"]},
        })
        mock_caller_cls.return_value.call.return_value = json.dumps({
            "files_changed": ["second.py"],
            "tests_added": ["test_second.py"],
            "test_mapping": {"second.py": ["test_second.py"]},
            "summary": "more work remains",
            "completion_status": "complete",
            "incomplete_tasks": ["finish integration"],
            "restricted_edits": [],
        })

        status = _run_single_llm_call(
            "prompt",
            step,
            flow,
            tmp_path,
            [],
            0,
            implemented_groups_override=[],
            invocation_intent=AgentInvocationIntent.DIRECT_IMPLEMENTATION,
            preserve_existing_outputs=True,
        )

        assert status == StepStatus.PARTIAL
        assert step.outputs["completion_status"] == "partial"
        assert step.outputs["incomplete_tasks"] == ["finish integration"]
        assert step.outputs["implemented_groups"] == []
        assert step.outputs["files_changed"] == ["first.py", "second.py"]
        assert step.outputs["tests_added"] == [
            "test_first.py", "test_second.py",
        ]

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}._run_single_llm_call", return_value=StepStatus.COMPLETED)
    @patch(
        "tianluo.engine.context_builder.get_runtime_environment_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_code_index_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_charter_injection",
        return_value="",
    )
    @patch(
        "tianluo.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    def test_small_shares_executor_but_not_direct_intent(
        self,
        mock_issue,
        mock_charter,
        mock_index,
        mock_runtime,
        mock_run,
        mock_resolve,
        tmp_path,
    ):
        from tianluo.engine.steps.implement import implement_handler

        flow, step = self._flow_step(
            tmp_path, task_type="small", decomposition="granular",
        )
        status = implement_handler(step, flow)

        assert status == StepStatus.COMPLETED
        assert "Small task type" in mock_run.call_args.args[0]
        assert (
            mock_run.call_args.kwargs["invocation_intent"]
            == AgentInvocationIntent.DEFAULT
        )
        assert step.outputs["implemented_groups"] == []

    @patch(f"{_IMP}._apply_restricted_edits", return_value=([], []))
    @patch(f"{_IMP}.LLMCaller")
    def test_junk_structured_result_is_partial_not_complete(
        self, mock_caller_cls, mock_apply, tmp_path,
    ):
        from tianluo.engine.steps.implement import _run_single_llm_call

        flow, step = self._flow_step(tmp_path)
        mock_caller_cls.return_value.call.return_value = json.dumps(
            {"note": "no JSON found"}
        )

        status = _run_single_llm_call(
            "prompt",
            step,
            flow,
            tmp_path,
            [],
            0,
            implemented_groups_override=[],
            invocation_intent=AgentInvocationIntent.DIRECT_IMPLEMENTATION,
            preserve_existing_outputs=True,
        )

        # A dict carrying none of the implementation-summary keys is not an
        # implementation report: the holistic path must stay recoverable
        # instead of advancing to TEST on zero implementation.
        assert status == StepStatus.PARTIAL
        assert step.outputs["completion_status"] == "partial"
        assert step.outputs["incomplete_tasks"]

    @patch(f"{_IMP}._apply_restricted_edits", return_value=([], []))
    @patch(f"{_IMP}.LLMCaller")
    def test_grouped_complete_with_leftover_tasks_stays_complete(
        self, mock_caller_cls, mock_apply, tmp_path,
    ):
        from tianluo.engine.steps.implement import _run_single_llm_call

        flow, step = self._flow_step(
            tmp_path,
            groups=[
                {"group_id": "G1", "group_order": 1, "depends_on": []},
                {"group_id": "G2", "group_order": 2, "depends_on": ["G1"]},
            ],
        )
        mock_caller_cls.return_value.call.return_value = json.dumps({
            "files_changed": ["second.py"],
            "tests_added": [],
            "test_mapping": {},
            "summary": "done with leftovers",
            "completion_status": "complete",
            "incomplete_tasks": ["finish integration"],
            "restricted_edits": [],
        })

        status = _run_single_llm_call(
            "prompt", step, flow, tmp_path, [], 0,
        )

        # Grouped flows keep their historical recording: an honest
        # complete-with-leftover report still records "complete" — the
        # partial coercion is a holistic-path rule only.
        assert status == StepStatus.COMPLETED
        assert step.outputs["completion_status"] == "complete"
        assert step.outputs["incomplete_tasks"] == ["finish integration"]


class TestFlowTypeMatrix:
    """End-to-end matrix across the plan-bearing and planless task types.

    Multi-group flows keep their PLAN -> IMPLEMENT scheduling data; a flow
    whose IMPLEMENT never saw a PLAN shares the whole-task executor without
    task_groups; review/survey never gain an IMPLEMENT segment at all.
    """

    def _flow_with_history(
        self, tmp_path, *, task_type, history_steps, decomposition="capability",
    ):
        from tianluo.engine.state_machine import StateMachine

        machine = StateMachine(tmp_path)
        flow = FlowInstance(
            flow_id=f"matrix-{task_type}",
            task_description="matrix task",
            task_type=task_type,
        )
        flow.state.context["plan_decomposition"] = decomposition
        flow.state.context["plan_granularity"] = "auto"
        flow.state.selected_steps = [
            StepType.ANALYZE,
            StepType.IMPLEMENT,
            StepType.TEST,
        ]
        for step in history_steps:
            flow.state.add_step(step)
        flow.state.current_step_index = flow.state.selected_steps.index(
            StepType.IMPLEMENT
        )
        return machine, flow

    def test_granular_task_groups_flow_into_implement_inputs(self, tmp_path):
        machine, flow = self._flow_with_history(
            tmp_path,
            task_type="feature",
            decomposition="granular",
            history_steps=[
                Step(
                    step_type=StepType.PLAN,
                    status=StepStatus.COMPLETED,
                    outputs={
                        "task_groups": [
                            {
                                "group_id": "G1",
                                "group_order": 1,
                                "depends_on": [],
                                "tasks": [{"id": 1, "estimated_loc": 40}],
                            }
                        ]
                    },
                )
            ],
        )
        inputs = machine._build_step_inputs(flow, StepType.IMPLEMENT)
        assert inputs["task_groups"][0]["group_id"] == "G1"
        assert inputs["task_groups"][0]["tasks"][0]["estimated_loc"] == 40

    def test_implement_inherits_the_doctrine_plan_recorded(self, tmp_path):
        """PLAN's own record outranks a flow context that predates it.

        A pre-upgrade flow has no doctrine in its context, so PLAN resolved one
        by projection and wrote it to its outputs. Handing IMPLEMENT the empty
        context instead would let it re-project independently and disagree with
        the plan it is about to execute.
        """
        machine, flow = self._flow_with_history(
            tmp_path,
            task_type="feature",
            history_steps=[
                Step(
                    step_type=StepType.PLAN,
                    status=StepStatus.COMPLETED,
                    outputs={
                        "task_groups": [{"group_id": "G1"}],
                        "plan_decomposition": "capability",
                        "plan_granularity": "auto",
                    },
                )
            ],
        )
        del flow.state.context["plan_decomposition"]
        del flow.state.context["plan_granularity"]
        flow.state.context["effective_implementation_strategy"] = "not_applicable"

        inputs = machine._build_step_inputs(flow, StepType.IMPLEMENT)
        assert inputs["plan_decomposition"] == "capability"
        assert inputs["plan_granularity"] == "auto"

    def test_context_doctrine_is_used_when_plan_recorded_none(self, tmp_path):
        """A PLAN step from before the record existed falls back to context."""
        machine, flow = self._flow_with_history(
            tmp_path,
            task_type="feature",
            decomposition="granular",
            history_steps=[
                Step(
                    step_type=StepType.PLAN,
                    status=StepStatus.COMPLETED,
                    outputs={"task_groups": [{"group_id": "G1"}]},
                )
            ],
        )
        inputs = machine._build_step_inputs(flow, StepType.IMPLEMENT)
        assert inputs["plan_decomposition"] == "granular"

    def test_planless_inputs_never_carry_plan_task_groups(self, tmp_path):
        machine, flow = self._flow_with_history(
            tmp_path,
            task_type="feature",
            history_steps=[
                Step(
                    step_type=StepType.ANALYZE,
                    status=StepStatus.COMPLETED,
                    outputs={
                        "task_type": "feature",
                        "analysis_context": {"scope": "whole requirement"},
                    },
                ),
                Step(
                    step_type=StepType.INVESTIGATE,
                    status=StepStatus.COMPLETED,
                    outputs={
                        "root_cause_report": {"root_cause": "clear"},
                    },
                ),
            ],
        )
        inputs = machine._build_step_inputs(flow, StepType.IMPLEMENT)
        assert "task_groups" not in inputs

    def test_review_and_survey_never_gain_implement(self, tmp_path):
        from tianluo.engine.state_machine import StateMachine

        machine = StateMachine(tmp_path)
        for task_type in ("review", "survey"):
            flow = machine.create_flow("task", task_type=task_type)
            assert StepType.IMPLEMENT not in flow.state.selected_steps
            assert StepType.PLAN not in flow.state.selected_steps

    def test_small_keeps_holistic_implement_without_a_plan(self, tmp_path):
        from tianluo.engine.state_machine import StateMachine

        machine = StateMachine(tmp_path)
        flow = machine.create_flow("task", task_type="small")
        assert StepType.IMPLEMENT in flow.state.selected_steps
        assert StepType.PLAN not in flow.state.selected_steps
        # The plan mode is still recorded (it costs nothing and keeps the
        # projection uniform), but a planless type never consults it.
        assert flow.state.context["plan_decomposition"] == "capability"

    def test_holistic_partial_continuations_are_bounded(self, tmp_path):
        from tianluo.engine.state_machine import (
            _HOLISTIC_CONTINUATION_LIMIT,
            StateMachine,
        )

        machine = StateMachine(tmp_path)
        flow = FlowInstance(
            flow_id="bounded-direct",
            task_description="bounded direct task",
            task_type="feature",
        )
        flow.state.context["plan_decomposition"] = "capability"
        flow.state.context["plan_granularity"] = "auto"
        flow.state.selected_steps = [
            StepType.ANALYZE,
            StepType.IMPLEMENT,
            StepType.TEST,
        ]
        step = Step(
            step_type=StepType.IMPLEMENT,
            step_id="bounded-implement",
            inputs={
                "task_description": "task",
                "task_type": "feature",
                # One capability group == today's holistic execution shape.
                "task_groups": [{"group_id": "G1", "name": "whole task"}],
            },
            outputs={
                "completion_status": "partial",
                "incomplete_tasks": ["still working"],
            },
            status=StepStatus.PARTIAL,
        )
        flow.state.add_step(step)
        flow.state.current_step_id = step.step_id
        flow.state.current_step_index = flow.state.selected_steps.index(
            StepType.IMPLEMENT
        )

        # Each automatic continuation re-arms the same step...
        for _ in range(_HOLISTIC_CONTINUATION_LIMIT):
            returned = machine.transition_to_next(flow)
            assert returned is step
            assert step.status == StepStatus.PENDING
            # ...and the next agent call reports partial again.
            step.status = StepStatus.PARTIAL

        # Past the limit the automatic loop stops: the step is persisted
        # FAILED so run.py routes into its Retry/Skip/Abort decision path.
        returned = machine.transition_to_next(flow)
        assert returned is None
        assert step.status == StepStatus.FAILED
        assert step.error_message
        assert step.inputs["holistic_continuations"] == (
            _HOLISTIC_CONTINUATION_LIMIT + 1
        )

        # A user Retry re-runs the handler; a further partial result must not
        # silently loop again — it fails back into the decision path.
        step.status = StepStatus.PENDING
        step.outputs["completion_status"] = "complete"
        step.outputs["incomplete_tasks"] = []
        step.status = StepStatus.PARTIAL
        step.outputs["completion_status"] = "partial"
        returned = machine.transition_to_next(flow)
        assert step.status == StepStatus.FAILED

    def _make_direct_flow_with_partial_implement(self, tmp_path):
        """Build a single-group flow whose IMPLEMENT carries a partial record."""
        from tianluo.engine.state_machine import StateMachine

        machine = StateMachine(tmp_path)
        flow = FlowInstance(
            flow_id="skip-direct",
            task_description="skip direct task",
            task_type="feature",
        )
        flow.state.context["plan_decomposition"] = "capability"
        flow.state.context["plan_granularity"] = "auto"
        flow.state.selected_steps = [
            StepType.ANALYZE,
            StepType.IMPLEMENT,
            StepType.TEST,
        ]
        step = Step(
            step_type=StepType.IMPLEMENT,
            step_id="skip-implement",
            inputs={
                "task_description": "task",
                "task_type": "feature",
                # One capability group == today's holistic execution shape.
                "task_groups": [{"group_id": "G1", "name": "whole task"}],
            },
            outputs={
                "completion_status": "partial",
                "incomplete_tasks": ["still working"],
            },
            status=StepStatus.PARTIAL,
        )
        flow.state.add_step(step)
        flow.state.current_step_id = step.step_id
        flow.state.current_step_index = flow.state.selected_steps.index(
            StepType.IMPLEMENT
        )
        return machine, flow, step

    def test_holistic_partial_skip_advances_past_exhausted_step(self, tmp_path):
        from tianluo.engine.state_machine import _HOLISTIC_CONTINUATION_LIMIT

        machine, flow, step = self._make_direct_flow_with_partial_implement(
            tmp_path
        )

        # Exhaust the automatic continuation budget exactly as the bounded
        # test does: each transition re-arms the step and the agent reports
        # partial again, until the final transition persists FAILED and
        # returns None so run.py routes into its Retry/Skip/Abort path.
        for _ in range(_HOLISTIC_CONTINUATION_LIMIT):
            machine.transition_to_next(flow)
            step.status = StepStatus.PARTIAL

        returned = machine.transition_to_next(flow)
        assert returned is None
        assert step.status == StepStatus.FAILED
        continuations_before_skip = step.inputs["holistic_continuations"]

        # run.py's Skip decision: force COMPLETED, mark the one-shot skip
        # flag, and transition. The gate must NOT re-capture the partial
        # record — a Skip must advance past the failed step, not re-present
        # the same failure prompt.
        step.status = StepStatus.COMPLETED
        step.inputs["holistic_skip_forced"] = True
        returned = machine.transition_to_next(flow)
        assert returned is not None
        assert returned is not step
        assert returned.step_type == StepType.TEST
        assert flow.state.get_current_step() is returned
        assert step.status == StepStatus.COMPLETED
        assert step.inputs["holistic_continuations"] == continuations_before_skip
        # The flag is one-shot: consumed by the transition so a later fix
        # loop reusing this step cannot inherit the skip intent.
        assert "holistic_skip_forced" not in step.inputs

    def test_holistic_partial_skip_under_limit_does_not_rearm(self, tmp_path):
        machine, flow, step = self._make_direct_flow_with_partial_implement(
            tmp_path
        )

        # An LLM error can leave the step FAILED while outputs still carry
        # the previous partial record (preserve_existing_outputs). A default
        # transition from a COMPLETED status with that record still re-arms
        # the automatic continuation (crash-resume semantics)...
        step.status = StepStatus.FAILED
        step.error_message = "boom"
        step.status = StepStatus.COMPLETED
        returned = machine.transition_to_next(flow)
        assert returned is step
        assert step.status == StepStatus.PENDING

        # ...but the explicit user Skip must advance without re-invoking the
        # implement agent — otherwise Skip silently becomes a paid Retry.
        step.status = StepStatus.COMPLETED
        step.inputs["holistic_skip_forced"] = True
        returned = machine.transition_to_next(flow)
        assert returned is not None
        assert returned is not step
        assert returned.step_type == StepType.TEST
        assert flow.state.get_current_step() is returned
        assert step.status == StepStatus.COMPLETED

