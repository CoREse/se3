"""Tests for commit step message generation and partial completion handling."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from tianluo.engine.models import FlowInstance, Step, StepStatus
from tianluo.engine.steps.commit import _generate_commit_message


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


class TestCommitMessageFromVersionAnalyze:
    """Tests for commit_message priority 1: from version_analyze step."""

    def test_commit_message_from_version_analyze_is_used(self):
        """commit_message from version_analyze takes highest priority."""
        flow = _make_flow()
        step = _make_step({
            "commit_message": "Add scope mechanism to verify_spec issues",
            "proposal": {"summary": "This should not be used"},
            "implement_summary": "This should also not be used",
        })
        msg = _generate_commit_message(flow, step)
        first_line = msg.split("\n")[0]
        assert "Add scope mechanism to verify_spec issues" in first_line
        assert "This should not be used" not in first_line

    def test_commit_message_from_version_analyze_truncated(self):
        """commit_message longer than 72 chars is truncated."""
        flow = _make_flow()
        long_msg = "A" * 80
        step = _make_step({
            "commit_message": long_msg,
        })
        msg = _generate_commit_message(flow, step)
        first_line = msg.split("\n")[0]
        # task_type prefix + ": " + truncated message
        assert "..." in first_line
        # The commit_message part should be truncated to 69 + "..."
        msg_part = first_line.split(": ", 1)[1]
        assert len(msg_part) <= 72

    def test_commit_message_from_version_analyze_has_type_prefix(self):
        """commit_message from version_analyze gets task_type prefix."""
        flow = _make_flow(task_type="feature")
        step = _make_step({
            "commit_message": "Add new login flow",
        })
        msg = _generate_commit_message(flow, step)
        first_line = msg.split("\n")[0]
        assert first_line.startswith("feature: ")

    def test_empty_commit_message_falls_through(self):
        """Empty commit_message falls through to proposal summary."""
        flow = _make_flow()
        step = _make_step({
            "commit_message": "",
            "proposal": {"summary": "Fix auth token refresh logic"},
        })
        msg = _generate_commit_message(flow, step)
        first_line = msg.split("\n")[0]
        assert "Fix auth token refresh logic" in first_line

    def test_none_commit_message_falls_through(self):
        """None commit_message falls through to proposal summary."""
        flow = _make_flow()
        step = _make_step({
            "commit_message": None,
            "proposal": {"summary": "Fix auth token refresh logic"},
        })
        msg = _generate_commit_message(flow, step)
        first_line = msg.split("\n")[0]
        assert "Fix auth token refresh logic" in first_line


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


class TestCommitMessageTemplateFallback:
    """Tests for commit message template fallback when no summary available."""

    def test_task_description_used_as_fallback(self):
        """Task description is used directly when no summary available."""
        flow = _make_flow(task_description="Fix memory leak in cache")
        step = _make_step({
            "proposal": {},
            "implement_summary": "",
        })
        msg = _generate_commit_message(flow, step)
        first_line = msg.split("\n")[0]
        assert "Fix memory leak in cache" in first_line

    def test_task_description_truncated_at_60_chars(self):
        """Long task description is truncated to 60 chars in fallback."""
        long_desc = "A" * 80
        flow = _make_flow(task_description=long_desc)
        step = _make_step({
            "proposal": {},
            "implement_summary": "",
        })
        msg = _generate_commit_message(flow, step)
        first_line = msg.split("\n")[0]
        desc_part = first_line.split(": ", 1)[1]
        assert len(desc_part) == 60

    def test_no_llm_call_in_fallback_path(self):
        """Commit message generation never calls LLM."""
        flow = _make_flow()
        step = _make_step({
            "proposal": {},
            "implement_summary": "",
        })
        # This should succeed without any LLM infrastructure
        msg = _generate_commit_message(flow, step)
        assert msg  # non-empty message generated

    def test_flow_id_always_appended(self):
        """Flow ID is always added to the commit message."""
        flow = _make_flow(flow_id="my-flow-123")
        step = _make_step({
            "commit_message": "Add feature X",
        })
        msg = _generate_commit_message(flow, step)
        assert "Flow: my-flow-123" in msg
