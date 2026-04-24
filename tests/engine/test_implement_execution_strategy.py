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

from se3.config import ImplementConfig
from se3.engine.models import FlowInstance, Step, StepStatus, StepType


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


def _make_step_flow(tmp_path, groups):
    step = Step(
        step_type=StepType.IMPLEMENT,
        step_id="test-dispatch",
        inputs={
            "task_description": "test",
            "task_type": "feature",
            "task_groups": groups,
            "spec_content": {},
            "design_doc": {},
        },
    )
    flow = FlowInstance(
        flow_id="test-flow",
        task_description="test",
        change_path=tmp_path / "se3",
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


_IMP = "se3.engine.steps.implement"


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
    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
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
        from se3.engine.steps.implement import implement_handler

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
    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
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
        from se3.engine.steps.implement import implement_handler

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
    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
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
        from se3.engine.steps.implement import implement_handler

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
    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
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
        from se3.engine.steps.implement import implement_handler

        step, flow = _make_step_flow(tmp_path, SMALL_GROUPS)
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_not_called()
        # Small multi-group tasks take the LOC-merge single-call path,
        # not the per-group sequential loop.
        mock_single.assert_called_once()
