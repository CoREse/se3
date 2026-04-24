"""Tests for sequential-fallback reason propagation in implement_handler.

G1 introduced two short-circuit rules that push an otherwise
DAG-parallel-eligible plan onto the sequential path:

1. ``implement.use_worktree=False``
2. RelayPlan is a linear chain (no forks, single root)

G2 surfaces the *reason* for the short-circuit in the Implementation Plan
panel. These tests verify that ``implement_handler`` passes the correct
``sequential_reason`` keyword to ``_display_task_plan`` for each path:

- ``use_worktree=False``     → ``"use_worktree=False"``
- linear-chain fallback      → ``"linear chain"``
- natural sequential (small) → ``None`` (no short-circuit occurred)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from se3.config import ImplementConfig
from se3.engine.models import FlowInstance, Step, StepStatus, StepType


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

_PARSED = {
    "files_changed": ["a.py"],
    "tests_added": [],
    "test_mapping": {},
    "summary": "done",
    "completion_status": "complete",
    "incomplete_tasks": [],
    "restricted_edits": [],
}

_IMP = "se3.engine.steps.implement"


def _make_step_flow(tmp_path, groups):
    step = Step(
        step_type=StepType.IMPLEMENT,
        step_id="test-reason",
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


def _extract_sequential_reason(mock_display):
    """Find the sequential-strategy call and return its reason kwarg.

    ``_display_task_plan`` may be called several times (e.g. by an early
    ``single`` path). We pick the call whose ``strategy`` positional arg
    is ``"sequential"``.
    """
    for call in mock_display.call_args_list:
        args = call.args
        kwargs = call.kwargs
        strategy = args[1] if len(args) > 1 else kwargs.get("strategy")
        if strategy == "sequential":
            return kwargs.get("sequential_reason")
    raise AssertionError(
        "No sequential _display_task_plan call found; "
        f"calls were: {mock_display.call_args_list}"
    )


class TestSequentialReasonPropagation:
    """Verify ``sequential_reason`` is threaded to the display helper."""

    @patch(f"{_IMP}._display_task_plan")
    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}.parse_json_response", return_value=_PARSED)
    @patch(f"{_IMP}.LLMCaller")
    @patch(f"{_IMP}._run_dag_parallel")
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch(
        "se3.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(
            group_loc_threshold=300, use_worktree=False,
        ),
    )
    def test_use_worktree_false_reports_reason(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_dag,
        mock_caller_cls,
        mock_parse,
        mock_resolve,
        mock_display,
        tmp_path,
    ):
        """use_worktree=False short-circuit sets reason ``use_worktree=False``."""
        from se3.engine.steps.implement import implement_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps(_PARSED)
        mock_caller_cls.return_value = mock_caller

        step, flow = _make_step_flow(tmp_path, FORK_GROUPS)
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_not_called()
        reason = _extract_sequential_reason(mock_display)
        assert reason == "use_worktree=False"

    @patch(f"{_IMP}._display_task_plan")
    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}.parse_json_response", return_value=_PARSED)
    @patch(f"{_IMP}.LLMCaller")
    @patch(f"{_IMP}._run_dag_parallel")
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch(
        "se3.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(
            group_loc_threshold=300, use_worktree=True,
        ),
    )
    def test_linear_chain_reports_reason(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_dag,
        mock_caller_cls,
        mock_parse,
        mock_resolve,
        mock_display,
        tmp_path,
    ):
        """Linear-chain short-circuit sets reason ``linear chain``."""
        from se3.engine.steps.implement import implement_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps(_PARSED)
        mock_caller_cls.return_value = mock_caller

        step, flow = _make_step_flow(tmp_path, LINEAR_GROUPS)
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_not_called()
        reason = _extract_sequential_reason(mock_display)
        assert reason == "linear chain"

    @patch(f"{_IMP}._display_task_plan")
    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}.parse_json_response", return_value=_PARSED)
    @patch(f"{_IMP}.LLMCaller")
    @patch(f"{_IMP}._run_dag_parallel")
    @patch(f"{_IMP}.has_commits", return_value=False)
    @patch(
        "se3.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(
            group_loc_threshold=300, use_worktree=True,
        ),
    )
    def test_has_commits_false_reports_reason(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_dag,
        mock_caller_cls,
        mock_parse,
        mock_resolve,
        mock_display,
        tmp_path,
    ):
        """has_commits=False fallback sets reason ``no commits``."""
        from se3.engine.steps.implement import implement_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps(_PARSED)
        mock_caller_cls.return_value = mock_caller

        step, flow = _make_step_flow(tmp_path, FORK_GROUPS)
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_not_called()
        reason = _extract_sequential_reason(mock_display)
        assert reason == "no commits"

    @patch(f"{_IMP}._display_task_plan")
    @patch(f"{_IMP}._run_single_llm_call", return_value=StepStatus.COMPLETED)
    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}.parse_json_response", return_value=_PARSED)
    @patch(f"{_IMP}.LLMCaller")
    @patch(f"{_IMP}._run_dag_parallel")
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch(
        "se3.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(
            group_loc_threshold=300, use_worktree=True,
        ),
    )
    def test_small_loc_merge_no_sequential_reason(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_dag,
        mock_caller_cls,
        mock_parse,
        mock_resolve,
        mock_single,
        mock_display,
        tmp_path,
    ):
        """Small multi-group merges via ``single`` path; no sequential display."""
        from se3.engine.steps.implement import implement_handler

        step, flow = _make_step_flow(tmp_path, SMALL_GROUPS)
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_not_called()
        mock_single.assert_called_once()
        # The display helper is called with strategy="single", not
        # "sequential" — so no sequential_reason is emitted at all.
        strategies = [
            (c.args[1] if len(c.args) > 1 else c.kwargs.get("strategy"))
            for c in mock_display.call_args_list
        ]
        assert "single" in strategies
        assert "sequential" not in strategies

    @patch(f"{_IMP}._display_task_plan")
    @patch(f"{_IMP}._run_dag_parallel", return_value=StepStatus.COMPLETED)
    @patch(f"{_IMP}._resolve_files_changed")
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch(
        "se3.engine.context_builder.get_issue_discovery_injection",
        return_value=None,
    )
    @patch.object(
        ImplementConfig,
        "load",
        return_value=ImplementConfig(
            group_loc_threshold=300, use_worktree=True,
        ),
    )
    def test_fork_dag_no_sequential_display(
        self,
        mock_cfg,
        mock_inj,
        mock_commits,
        mock_resolve,
        mock_dag,
        mock_display,
        tmp_path,
    ):
        """Fork DAG takes the DAG parallel path; sequential display not triggered."""
        from se3.engine.steps.implement import implement_handler

        step, flow = _make_step_flow(tmp_path, FORK_GROUPS)
        result = implement_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_dag.assert_called_once()
        strategies = [
            (c.args[1] if len(c.args) > 1 else c.kwargs.get("strategy"))
            for c in mock_display.call_args_list
        ]
        assert "dag_parallel" in strategies
        assert "sequential" not in strategies


class TestDisplayTaskPlanForwards:
    """Verify the ``_display_task_plan`` wrapper forwards the new kwarg."""

    def test_forwards_sequential_reason_to_formatter(self):
        """_display_task_plan passes sequential_reason to format_implement_plan."""
        from se3.engine.steps import implement as impl_mod

        groups = [
            {
                "group_id": "G1",
                "group_order": 1,
                "depends_on": [],
                "tasks": [{"id": 1, "estimated_loc": 100}],
            },
            {
                "group_id": "G2",
                "group_order": 2,
                "depends_on": ["G1"],
                "tasks": [{"id": 2, "estimated_loc": 100}],
            },
        ]

        captured = {}

        class _FakeFormatter:
            def __init__(self, console=None):
                self.console = console

            def format_implement_plan(self, **kwargs):
                captured.update(kwargs)
                return "panel"

        class _FakeConsole:
            def print(self, *_a, **_k):
                return None

        with patch(
            "se3.engine.formatters.TaskFormatter", _FakeFormatter,
        ), patch(
            "se3.engine.display.get_console", return_value=_FakeConsole(),
        ):
            impl_mod._display_task_plan(
                groups,
                "sequential",
                200,
                300,
                sequential_reason="linear chain",
            )

        assert captured.get("sequential_reason") == "linear chain"
        assert captured.get("execution_strategy") == "sequential"

    def test_default_sequential_reason_is_none(self):
        """Omitting sequential_reason forwards None to the formatter."""
        from se3.engine.steps import implement as impl_mod

        groups = [
            {
                "group_id": "G1",
                "group_order": 1,
                "depends_on": [],
                "tasks": [{"id": 1, "estimated_loc": 100}],
            },
        ]

        captured = {}

        class _FakeFormatter:
            def __init__(self, console=None):
                self.console = console

            def format_implement_plan(self, **kwargs):
                captured.update(kwargs)
                return "panel"

        class _FakeConsole:
            def print(self, *_a, **_k):
                return None

        with patch(
            "se3.engine.formatters.TaskFormatter", _FakeFormatter,
        ), patch(
            "se3.engine.display.get_console", return_value=_FakeConsole(),
        ):
            impl_mod._display_task_plan(groups, "single", 100, 0)

        assert captured.get("sequential_reason") is None
