"""Tests for commit step partial completion handling."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.models import FlowInstance, Step, StepStatus
from se3.engine.steps.commit import _generate_commit_message


def _make_flow(**kwargs) -> FlowInstance:
    defaults = {
        "flow_id": "test-flow-001",
        "task_description": "Fix authentication bug",
        "task_type": "bugfix",
        "change_path": Path("/tmp/project/se3.yaml"),
    }
    defaults.update(kwargs)
    flow = MagicMock(spec=FlowInstance)
    for k, v in defaults.items():
        setattr(flow, k, v)
    return flow


def _make_step(inputs: dict | None = None) -> Step:
    step = MagicMock(spec=Step)
    step.inputs = inputs or {}
    step.outputs = {}
    return step


class TestCommitPartialCompletion:
    """Tests for _generate_commit_message with partial completion."""

    def test_defaults_to_complete_when_missing(self):
        """Missing completion_status defaults to 'complete' — no incomplete section."""
        flow = _make_flow()
        step = _make_step({
            "proposal": {"summary": "Fix auth token refresh logic"},
        })
        msg = _generate_commit_message(flow, step)
        assert "Incomplete tasks" not in msg

    def test_complete_status_no_incomplete_section(self):
        """Explicit 'complete' status produces no incomplete section."""
        flow = _make_flow()
        step = _make_step({
            "proposal": {"summary": "Fix auth token refresh logic"},
            "completion_status": "complete",
            "incomplete_tasks": [],
        })
        msg = _generate_commit_message(flow, step)
        assert "Incomplete tasks" not in msg

    def test_partial_status_includes_incomplete_tasks_strings(self):
        """Partial completion with string tasks lists them in the body."""
        flow = _make_flow()
        step = _make_step({
            "proposal": {"summary": "Fix auth token refresh logic"},
            "completion_status": "partial",
            "incomplete_tasks": ["Update CLAUDE.md permissions", "Add migration script"],
        })
        msg = _generate_commit_message(flow, step)
        assert "Incomplete tasks (partial completion):" in msg
        assert "Update CLAUDE.md permissions" in msg
        assert "Add migration script" in msg

    def test_partial_status_includes_incomplete_tasks_dicts(self):
        """Partial completion with dict tasks shows description and reason."""
        flow = _make_flow()
        step = _make_step({
            "proposal": {"summary": "Fix auth token refresh logic"},
            "completion_status": "partial",
            "incomplete_tasks": [
                {"description": "Edit .claude/config", "reason": "restricted file"},
            ],
        })
        msg = _generate_commit_message(flow, step)
        assert "Edit .claude/config" in msg
        assert "restricted file" in msg

    def test_partial_with_empty_incomplete_tasks_no_section(self):
        """Partial status but empty incomplete_tasks — no section added."""
        flow = _make_flow()
        step = _make_step({
            "proposal": {"summary": "Fix auth token refresh logic"},
            "completion_status": "partial",
            "incomplete_tasks": [],
        })
        msg = _generate_commit_message(flow, step)
        assert "Incomplete tasks" not in msg

    def test_subject_line_reflects_completed_work(self):
        """Subject line uses the proposal summary (what was done), not planned."""
        flow = _make_flow()
        step = _make_step({
            "proposal": {"summary": "Fix auth token refresh logic"},
            "completion_status": "partial",
            "incomplete_tasks": ["Update docs"],
        })
        msg = _generate_commit_message(flow, step)
        first_line = msg.split("\n")[0]
        assert "Fix auth token refresh logic" in first_line

    def test_implement_summary_used_as_fallback(self):
        """When proposal has no summary, implement_summary is used."""
        flow = _make_flow()
        step = _make_step({
            "proposal": {},
            "implement_summary": "Refactored auth module and updated tests",
            "completion_status": "complete",
        })
        msg = _generate_commit_message(flow, step)
        first_line = msg.split("\n")[0]
        assert "Refactored auth module and updated tests" in first_line

    @patch("se3.engine.steps.commit.LLMCaller")
    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    def test_restricted_edits_in_llm_prompt_context(self, mock_inject, mock_caller_cls):
        """Restricted edits info is included in the LLM prompt for commit message."""
        mock_caller = MagicMock()
        mock_caller.call.return_value = "Update auth config and tests"
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow()
        step = _make_step({
            "proposal": {},
            "implement_summary": "",
            "completion_status": "complete",
            "restricted_edits_applied": [
                {"file_path": ".claude/CLAUDE.md"},
            ],
        })
        msg = _generate_commit_message(flow, step)
        # Verify the LLM was called and restricted edits were in prompt
        call_args = mock_caller.call.call_args
        prompt = call_args[1].get("prompt") or call_args[0][0] if call_args[0] else call_args[1]["prompt"]
        assert "Restricted edits applied" in prompt
        assert ".claude/CLAUDE.md" in prompt
