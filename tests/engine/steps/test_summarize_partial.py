"""Tests for summarize step partial completion handling."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tianluo.engine.models import FlowInstance, Step
from tianluo.engine.steps.summarize import (
    _build_completion_section,
    _create_basic_summary_text,
)


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


class TestBuildCompletionSection:
    """Tests for _build_completion_section."""

    def test_complete_status_returns_empty(self):
        """Complete status with no restricted edits returns empty string."""
        result = _build_completion_section("complete", [], "", [], [])
        assert result == ""

    def test_partial_status_with_incomplete_tasks(self):
        section = _build_completion_section(
            "partial",
            ["Edit .claude/config", "Update migration"],
            "Implemented core logic",
            [],
            [],
        )
        assert "Status: **partial**" in section
        assert "Incomplete Tasks" in section
        assert "Edit .claude/config" in section
        assert "Update migration" in section
        assert "Implemented core logic" in section

    def test_partial_with_dict_tasks_shows_reason(self):
        section = _build_completion_section(
            "partial",
            [{"description": "Edit .claude/config", "reason": "restricted file"}],
            "",
            [],
            [],
        )
        assert "Edit .claude/config" in section
        assert "restricted file" in section

    def test_restricted_edits_applied(self):
        section = _build_completion_section(
            "complete",
            [],
            "",
            [{"file_path": ".claude/CLAUDE.md"}],
            [],
        )
        assert "Restricted Edits Applied" in section
        assert ".claude/CLAUDE.md" in section

    def test_restricted_edits_failed(self):
        section = _build_completion_section(
            "partial",
            [],
            "",
            [],
            [{"file_path": ".claude/CLAUDE.md", "error": "old_string not found"}],
        )
        assert "Restricted Edits Failed" in section
        assert ".claude/CLAUDE.md" in section
        assert "old_string not found" in section

    def test_all_fields_combined(self):
        section = _build_completion_section(
            "partial",
            ["Task A"],
            "Did most of the work",
            [{"file_path": "a.py"}],
            [{"file_path": "b.py", "error": "conflict"}],
        )
        assert "Status: **partial**" in section
        assert "Incomplete Tasks" in section
        assert "Task A" in section
        assert "Did most of the work" in section
        assert "Restricted Edits Applied" in section
        assert "Restricted Edits Failed" in section


class TestCreateBasicSummaryTextPartial:
    """Tests for _create_basic_summary_text with partial completion."""

    def test_complete_default_says_completed(self):
        flow = _make_flow()
        text = _create_basic_summary_text(flow, {}, {}, "Fix bug")
        assert "Completed" in text
        assert "Partially completed" not in text

    def test_partial_says_partially_completed(self):
        flow = _make_flow()
        text = _create_basic_summary_text(
            flow, {}, {}, "Fix bug",
            incomplete_tasks=["Edit config"],
            completion_status="partial",
        )
        assert "Partially completed" in text

    def test_partial_includes_incomplete_tasks_strings(self):
        flow = _make_flow()
        text = _create_basic_summary_text(
            flow, {}, {}, "Fix bug",
            incomplete_tasks=["Edit config", "Update docs"],
            completion_status="partial",
        )
        assert "Incomplete Tasks" in text
        assert "Edit config" in text
        assert "Update docs" in text

    def test_partial_includes_incomplete_tasks_dicts(self):
        flow = _make_flow()
        text = _create_basic_summary_text(
            flow, {}, {}, "Fix bug",
            incomplete_tasks=[{"description": "Edit .claude/config", "reason": "restricted"}],
            completion_status="partial",
        )
        assert "Edit .claude/config" in text
        assert "restricted" in text

    def test_no_incomplete_tasks_no_section(self):
        flow = _make_flow()
        text = _create_basic_summary_text(
            flow, {}, {}, "Fix bug",
            incomplete_tasks=[],
            completion_status="partial",
        )
        assert "Incomplete Tasks" not in text

    def test_backward_compatible_without_new_args(self):
        """Calling without new args still works (defaults)."""
        flow = _make_flow()
        text = _create_basic_summary_text(flow, {}, {}, "Fix bug")
        assert "Completed" in text
        assert "Incomplete Tasks" not in text
