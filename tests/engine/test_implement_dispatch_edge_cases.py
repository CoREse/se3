"""Edge-case tests for implement_handler's execution strategy dispatch.

Complements ``test_implement_execution_strategy.py`` by covering paths that
the happy-path dispatch tests do not exercise:

* linear-chain detection raises → DAG parallel proceeds (exception branch of
  ``try: ... except Exception``);
* ``has_commits(project_root)`` returning False falls through to sequential
  even when DAG parallel was selected;
* the ``use_worktree=False`` short-circuit is a no-op when the LOC-merge
  single-call path has already been chosen (small total LOC);
* single-group inputs are not eligible for DAG regardless of ``use_worktree``;
* the ``logger.info`` short-circuit messages are emitted (captured via the
  ``caplog`` fixture) so dispatch decisions remain visible in logs.

Like the sibling file, dispatch is verified by patching ``_run_dag_parallel``
and ``LLMCaller`` — no real git, worktree, or network operations run.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

from tianluo.config import ImplementConfig
from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

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

SINGLE_GROUP = [
    {
        "group_id": "G1",
        "group_order": 1,
        "depends_on": [],
        "tasks": [{"id": 1, "estimated_loc": 400}],
    },
]


def _make_step_flow(tmp_path, groups):
    step = Step(
        step_type=StepType.IMPLEMENT,
        step_id="test-dispatch-edge",
        inputs={
            "task_description": "test",
            "task_type": "feature",
            "task_groups": groups,
            "spec_content": {},
            "design_doc": {},
        },
    )
    flow = FlowInstance(
        flow_id="test-flow-edge",
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
# Linear-chain detection exception branch
# ---------------------------------------------------------------------------


class TestLinearChainDetectionExceptionPath:
    """If ``transitive_reduce`` raises, DAG parallel must still run."""

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}._run_dag_parallel", return_value=StepStatus.COMPLETED)
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch(f"{_IMP}.transitive_reduce", side_effect=RuntimeError("boom"))
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(group_loc_threshold=300, use_worktree=True),
    )
    def test_linear_chain_detection_raises_falls_through_to_dag(
        self,
        mock_cfg,
        mock_tr,
        mock_inj,
        mock_commits,
        mock_dag,
        mock_resolve,
        tmp_path,
    ):
        """A linear chain that normally short-circuits still runs DAG when
        the linear detector blows up — exception branch is swallowed and
        ``want_dag`` stays True."""
        from tianluo.engine.steps.implement import implement_handler

        step, flow = _make_step_flow(tmp_path, LINEAR_GROUPS)
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_called_once()


# ---------------------------------------------------------------------------
# has_commits=False fallback
# ---------------------------------------------------------------------------


class TestHasCommitsFalseFallsThroughToSequential:
    """Empty repo → warning + sequential path even when DAG was selected."""

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}.parse_json_response", return_value=_SEQ_PARSED)
    @patch(f"{_IMP}.LLMCaller")
    @patch(f"{_IMP}._run_dag_parallel")
    @patch(f"{_IMP}.has_commits", return_value=False)
    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(group_loc_threshold=300, use_worktree=True),
    )
    def test_empty_repo_skips_dag_and_runs_sequential(
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
        """Fork DAG + use_worktree=True + has_commits=False: DAG not called,
        falls through the ``if has_commits(...)`` guard into the sequential
        group-by-group loop."""
        from tianluo.engine.steps.implement import implement_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps(_SEQ_PARSED)
        mock_caller_cls.return_value = mock_caller

        step, flow = _make_step_flow(tmp_path, FORK_GROUPS)
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_not_called()
        assert mock_caller_cls.call_count == len(FORK_GROUPS)


# ---------------------------------------------------------------------------
# Small-LOC path is independent of use_worktree
# ---------------------------------------------------------------------------


class TestSmallLocPathIgnoresUseWorktree:
    """The LOC-merge single-call path precedes the dispatch decision, so
    ``use_worktree=False`` should not change its behavior."""

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}._run_dag_parallel")
    @patch(f"{_IMP}._run_single_llm_call", return_value=StepStatus.COMPLETED)
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(group_loc_threshold=300, use_worktree=False),
    )
    def test_small_loc_with_use_worktree_false_still_single_call(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_single,
        mock_dag,
        mock_resolve,
        tmp_path,
    ):
        """Small total LOC + use_worktree=False: LOC-merge path wins before
        dispatch ever runs, so ``_run_single_llm_call`` is invoked once and
        ``_run_dag_parallel`` is not."""
        from tianluo.engine.steps.implement import implement_handler

        step, flow = _make_step_flow(tmp_path, SMALL_GROUPS)
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_single.assert_called_once()
        mock_dag.assert_not_called()


# ---------------------------------------------------------------------------
# Single-group inputs are not DAG-eligible
# ---------------------------------------------------------------------------


class TestSingleGroupNotEligibleForDag:
    """Single-group inputs bypass DAG parallel regardless of use_worktree."""

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}._run_dag_parallel")
    @patch(f"{_IMP}._run_single_llm_call", return_value=StepStatus.COMPLETED)
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(group_loc_threshold=300, use_worktree=True),
    )
    def test_single_group_with_use_worktree_true_never_uses_dag(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_single,
        mock_dag,
        mock_resolve,
        tmp_path,
    ):
        """Single group → ``_should_use_dag`` returns False (len(groups) < 2)
        so dispatch never considers DAG parallel even with use_worktree=True."""
        from tianluo.engine.steps.implement import implement_handler

        step, flow = _make_step_flow(tmp_path, SINGLE_GROUP)
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_not_called()
        mock_single.assert_called_once()


# ---------------------------------------------------------------------------
# Capability doctrine bypasses the LOC gate
# ---------------------------------------------------------------------------


# Coarse capability groups: scheduling fields only, no tasks and therefore no
# estimated_loc at all — _compute_total_loc returns 0 for every one of them.
CAPABILITY_PARALLEL_GROUPS = [
    {
        "group_id": "G1",
        "name": "Export capability",
        "description": "deliver export end to end, with its tests",
        "group_order": 1,
        "depends_on": [],
    },
    {
        "group_id": "G2",
        "name": "Import capability",
        "description": "deliver import end to end, with its tests",
        "group_order": 2,
        "depends_on": [],
    },
]

CAPABILITY_LINEAR_GROUPS = [
    {
        "group_id": "G1",
        "name": "Storage capability",
        "description": "...",
        "group_order": 1,
        "depends_on": [],
    },
    {
        "group_id": "G2",
        "name": "Query capability",
        "description": "...",
        "group_order": 2,
        "depends_on": ["G1"],
    },
]


def _make_capability_step_flow(tmp_path, groups):
    step, flow = _make_step_flow(tmp_path, groups)
    step.inputs["plan_decomposition"] = "capability"
    step.inputs["plan_granularity"] = "auto"
    flow.state.context["plan_decomposition"] = "capability"
    flow.state.context["plan_granularity"] = "auto"
    return step, flow


class TestCapabilityModeBypassesLocGate:
    """Coarse groups carry no LOC, so LOC must not decide their scheduling."""

    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}._run_dag_parallel", return_value=StepStatus.COMPLETED)
    @patch(f"{_IMP}._run_single_llm_call", return_value=StepStatus.COMPLETED)
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(group_loc_threshold=300, use_worktree=True),
    )
    def test_two_independent_capability_groups_run_in_parallel(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_single,
        mock_dag,
        mock_resolve,
        tmp_path,
    ):
        """Zero total LOC must not collapse independent groups to sequential."""
        from tianluo.engine.steps.implement import implement_handler

        step, flow = _make_capability_step_flow(
            tmp_path, CAPABILITY_PARALLEL_GROUPS,
        )
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_called_once()
        # The LOC-merge single-call short-circuit must not have fired.
        mock_single.assert_not_called()

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
    def test_linear_capability_chain_still_short_circuits_to_sequential(
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
        """The three pre-existing short-circuits are untouched by the bypass."""
        from tianluo.engine.steps.implement import implement_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps(_SEQ_PARSED)
        mock_caller_cls.return_value = mock_caller

        step, flow = _make_capability_step_flow(
            tmp_path, CAPABILITY_LINEAR_GROUPS,
        )
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_not_called()
        assert mock_caller_cls.call_count == len(CAPABILITY_LINEAR_GROUPS)

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
    def test_use_worktree_false_still_forces_sequential(
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

        step, flow = _make_capability_step_flow(
            tmp_path, CAPABILITY_PARALLEL_GROUPS,
        )
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_not_called()
        assert mock_caller_cls.call_count == len(CAPABILITY_PARALLEL_GROUPS)

    def test_threshold_value_is_irrelevant_under_capability(self):
        """Any threshold, any LOC: capability scheduling is count + topology."""
        from tianluo.engine.steps.implement import _should_use_dag

        for threshold in (0, 1, 300, 10_000):
            for total_loc in (0, 50, 5_000):
                assert _should_use_dag(
                    CAPABILITY_PARALLEL_GROUPS,
                    total_loc,
                    threshold,
                    capability_mode=True,
                ) is True
        # One group is still not DAG-eligible — that rule is doctrine-agnostic.
        assert _should_use_dag(
            CAPABILITY_PARALLEL_GROUPS[:1], 0, 300, capability_mode=True,
        ) is False

    def test_granular_threshold_behaviour_is_unchanged(self):
        """The legacy LOC gate keeps its exact semantics."""
        from tianluo.engine.steps.implement import _should_use_dag

        assert _should_use_dag(FORK_GROUPS, 600, 300) is True
        assert _should_use_dag(FORK_GROUPS, 200, 300) is False
        # total_loc == 0 means "no estimate available", which historically
        # meant "do not let LOC veto the DAG".
        assert _should_use_dag(FORK_GROUPS, 0, 300) is True


# ---------------------------------------------------------------------------
# Log-message assertions
# ---------------------------------------------------------------------------


class TestDispatchLogMessages:
    """The dispatch short-circuits emit INFO logs so operators can see why
    DAG parallel was skipped. Verify via the ``caplog`` fixture."""

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
    def test_use_worktree_false_logs_skip_message(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_dag,
        mock_caller_cls,
        mock_parse,
        mock_resolve,
        tmp_path,
        caplog,
    ):
        from tianluo.engine.steps.implement import implement_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps(_SEQ_PARSED)
        mock_caller_cls.return_value = mock_caller

        step, flow = _make_step_flow(tmp_path, FORK_GROUPS)
        with caplog.at_level(logging.INFO, logger="tianluo.engine.steps.implement"):
            implement_handler(step, flow)

        assert any(
            "use_worktree=False" in rec.getMessage() and "sequential" in rec.getMessage()
            for rec in caplog.records
        ), "expected an INFO log announcing the use_worktree=False short-circuit"

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
    def test_linear_chain_logs_fallback_message(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_dag,
        mock_caller_cls,
        mock_parse,
        mock_resolve,
        tmp_path,
        caplog,
    ):
        from tianluo.engine.steps.implement import implement_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps(_SEQ_PARSED)
        mock_caller_cls.return_value = mock_caller

        step, flow = _make_step_flow(tmp_path, LINEAR_GROUPS)
        with caplog.at_level(logging.INFO, logger="tianluo.engine.steps.implement"):
            implement_handler(step, flow)

        assert any(
            "linear chain" in rec.getMessage() and "sequential" in rec.getMessage()
            for rec in caplog.records
        ), "expected an INFO log announcing the linear-chain short-circuit"
