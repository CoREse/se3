"""Tests for the implement step handler."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType


def _make_step_and_flow(tmp_path: Path, task_groups: list[dict]) -> tuple[Step, FlowInstance]:
    """Create a Step and FlowInstance for testing implement_handler."""
    step = Step(
        step_type=StepType.IMPLEMENT,
        step_id="test-implement",
        inputs={
            "task_description": "Test task",
            "task_type": "feature",
            "task_groups": task_groups,
            "design_doc": {},
        },
    )
    flow = FlowInstance(
        task_description="Test task",
        change_path=tmp_path / "tianluo",
    )
    return step, flow


TWO_GROUPS = [
    {
        "group_id": "G1",
        "group_order": 1,
        "depends_on": [],
        "tasks": [{"id": 1, "description": "Task 1", "estimated_loc": 200}],
    },
    {
        "group_id": "G2",
        "group_order": 2,
        "depends_on": ["G1"],
        "tasks": [{"id": 2, "description": "Task 2", "estimated_loc": 200}],
    },
]


# Forking DAG: G1 → G2 and G1 → G3. Used where the test must route to
# DAG parallel; a linear chain (TWO_GROUPS) now short-circuits to the
# sequential path per the implement.use_worktree / linear-chain rules.
FORK_GROUPS = [
    {
        "group_id": "G1",
        "group_order": 1,
        "depends_on": [],
        "tasks": [{"id": 1, "description": "Task 1", "estimated_loc": 200}],
    },
    {
        "group_id": "G2",
        "group_order": 2,
        "depends_on": ["G1"],
        "tasks": [{"id": 2, "description": "Task 2", "estimated_loc": 200}],
    },
    {
        "group_id": "G3",
        "group_order": 3,
        "depends_on": ["G1"],
        "tasks": [{"id": 3, "description": "Task 3", "estimated_loc": 200}],
    },
]


class TestImplementHandlerEmptyRepoFallback:
    """Verify implement_handler falls back to sequential when no commits exist."""

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("tianluo.engine.steps.implement.parse_json_response")
    @patch("tianluo.engine.steps.implement.LLMCaller")
    @patch("tianluo.engine.steps.implement._run_dag_parallel")
    @patch("tianluo.engine.steps.implement.has_commits", return_value=False)
    def test_skips_dag_when_no_commits(
        self,
        mock_has_commits,
        mock_dag_parallel,
        mock_llm_cls,
        mock_parse_json,
        mock_injection,
        tmp_path,
    ):
        """DAG parallel must be skipped when has_commits() returns False."""
        from tianluo.engine.steps.implement import implement_handler

        mock_parse_json.return_value = {
            "files_changed": ["a.py"],
            "tests_added": [],
            "test_mapping": {},
            "summary": "ok",
            "completion_status": "complete",
            "incomplete_tasks": [],
            "restricted_edits": [],
        }
        mock_caller = MagicMock()
        mock_caller.call.return_value = "{}"
        mock_llm_cls.return_value = mock_caller

        # Use FORK_GROUPS (not TWO_GROUPS) because a linear chain now
        # short-circuits to sequential via _relay_plan_is_linear *before*
        # the has_commits=False guard is reached. FORK_GROUPS preserves
        # the test's original intent: verifying DAG fallback on empty repo.
        step, flow = _make_step_and_flow(tmp_path, FORK_GROUPS)
        result = implement_handler(step, flow)

        # DAG parallel must NOT have been called
        mock_dag_parallel.assert_not_called()
        # Sequential path calls LLMCaller once per group
        assert mock_llm_cls.call_count == 3
        assert result == StepStatus.COMPLETED

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("tianluo.engine.steps.implement._run_dag_parallel", return_value=StepStatus.COMPLETED)
    @patch("tianluo.engine.steps.implement.has_commits", return_value=True)
    def test_uses_dag_when_commits_exist(
        self,
        mock_has_commits,
        mock_dag_parallel,
        mock_injection,
        tmp_path,
    ):
        """DAG parallel should be used when has_commits() returns True."""
        from tianluo.engine.steps.implement import implement_handler

        step, flow = _make_step_and_flow(tmp_path, FORK_GROUPS)
        result = implement_handler(step, flow)

        # DAG parallel SHOULD have been called
        mock_dag_parallel.assert_called_once()
        assert result == StepStatus.COMPLETED
