"""Tests for implement step: restricted_edits and completion status detection.

Tests cover:
- _apply_restricted_edits() helper function
- restricted_edits integration in _run_single_llm_call()
- restricted_edits integration in group-by-group path
- Completion status detection (complete/partial/failed)
- Backward compatibility when new fields are absent
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from se3.engine.models import FlowInstance, Step, StepStatus, StepType
from se3.engine.steps.implement import (
    _apply_restricted_edits,
    implement_handler,
)


class TestApplyRestrictedEdits:
    """Test _apply_restricted_edits helper function."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_successful_edit(self):
        """Test a basic successful restricted edit."""
        target = self.project_root / "test.txt"
        target.write_text("hello world", encoding="utf-8")

        edits = [{"file_path": "test.txt", "old_string": "hello", "new_string": "goodbye"}]
        applied, failed = _apply_restricted_edits(edits, self.project_root)

        assert len(applied) == 1
        assert len(failed) == 0
        assert target.read_text(encoding="utf-8") == "goodbye world"

    def test_file_not_found(self):
        """Test edit on non-existent file."""
        edits = [{"file_path": "missing.txt", "old_string": "x", "new_string": "y"}]
        applied, failed = _apply_restricted_edits(edits, self.project_root)

        assert len(applied) == 0
        assert len(failed) == 1
        assert "File not found" in failed[0]["error"]

    def test_old_string_not_found(self):
        """Test edit where old_string doesn't exist in file."""
        target = self.project_root / "test.txt"
        target.write_text("hello world", encoding="utf-8")

        edits = [{"file_path": "test.txt", "old_string": "nonexistent", "new_string": "y"}]
        applied, failed = _apply_restricted_edits(edits, self.project_root)

        assert len(applied) == 0
        assert len(failed) == 1
        assert "old_string not found" in failed[0]["error"]

    def test_missing_file_path(self):
        """Test edit with missing file_path."""
        edits = [{"file_path": "", "old_string": "x", "new_string": "y"}]
        applied, failed = _apply_restricted_edits(edits, self.project_root)

        assert len(applied) == 0
        assert len(failed) == 1
        assert "Missing" in failed[0]["error"]

    def test_missing_old_string(self):
        """Test edit with missing old_string."""
        edits = [{"file_path": "test.txt", "old_string": "", "new_string": "y"}]
        applied, failed = _apply_restricted_edits(edits, self.project_root)

        assert len(applied) == 0
        assert len(failed) == 1

    def test_multiple_edits_mixed_results(self):
        """Test multiple edits where some succeed and some fail."""
        f1 = self.project_root / "good.txt"
        f1.write_text("replace me", encoding="utf-8")

        edits = [
            {"file_path": "good.txt", "old_string": "replace me", "new_string": "done"},
            {"file_path": "missing.txt", "old_string": "x", "new_string": "y"},
        ]
        applied, failed = _apply_restricted_edits(edits, self.project_root)

        assert len(applied) == 1
        assert len(failed) == 1
        assert f1.read_text(encoding="utf-8") == "done"

    def test_only_replaces_first_occurrence(self):
        """Test that only the first occurrence is replaced."""
        target = self.project_root / "test.txt"
        target.write_text("aaa bbb aaa", encoding="utf-8")

        edits = [{"file_path": "test.txt", "old_string": "aaa", "new_string": "ccc"}]
        applied, failed = _apply_restricted_edits(edits, self.project_root)

        assert len(applied) == 1
        assert target.read_text(encoding="utf-8") == "ccc bbb aaa"

    def test_subdirectory_file(self):
        """Test edit on file in subdirectory."""
        subdir = self.project_root / ".claude"
        subdir.mkdir()
        target = subdir / "CLAUDE.md"
        target.write_text("old content", encoding="utf-8")

        edits = [{"file_path": ".claude/CLAUDE.md", "old_string": "old content", "new_string": "new content"}]
        applied, failed = _apply_restricted_edits(edits, self.project_root)

        assert len(applied) == 1
        assert target.read_text(encoding="utf-8") == "new content"

    def test_empty_edits_list(self):
        """Test with empty edits list."""
        applied, failed = _apply_restricted_edits([], self.project_root)
        assert applied == []
        assert failed == []


class TestCompletionStatusSingleCall:
    """Test completion status detection in _run_single_llm_call path."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        self.flow = FlowInstance(
            flow_id="test-flow",
            task_description="Test task",
            task_type="feature",
            change_path=self.project_root / "change",
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_step(self):
        return Step(
            step_type=StepType.IMPLEMENT,
            step_id="impl-001",
            inputs={
                "task_description": "test",
                "task_type": "feature",
                "task_groups": [{"group_id": "G1", "tasks": ["t1"]}],
                "spec_content": {},
            },
            outputs={},
        )

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_complete_status_returns_completed(self, mock_parse, mock_caller_cls, mock_inj):
        """Default/complete status returns COMPLETED."""
        mock_parse.return_value = {
            "files_changed": ["a.py"],
            "tests_added": [],
            "test_mapping": {},
            "summary": "Done",
            "completion_status": "complete",
            "incomplete_tasks": [],
        }
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step()
        result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["completion_status"] == "complete"
        assert step.outputs["incomplete_tasks"] == []

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_partial_status_returns_partial(self, mock_parse, mock_caller_cls, mock_inj):
        """Partial status returns PARTIAL."""
        mock_parse.return_value = {
            "files_changed": ["a.py"],
            "tests_added": [],
            "test_mapping": {},
            "summary": "Partially done",
            "completion_status": "partial",
            "incomplete_tasks": ["Could not edit .claude/CLAUDE.md due to permission"],
        }
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step()
        result = implement_handler(step, self.flow)

        assert result == StepStatus.PARTIAL
        assert step.outputs["completion_status"] == "partial"
        assert len(step.outputs["incomplete_tasks"]) == 1

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_failed_status_returns_failed(self, mock_parse, mock_caller_cls, mock_inj):
        """Failed status returns FAILED."""
        mock_parse.return_value = {
            "files_changed": [],
            "tests_added": [],
            "test_mapping": {},
            "summary": "Could not proceed",
            "completion_status": "failed",
            "incomplete_tasks": ["All tasks blocked"],
        }
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step()
        result = implement_handler(step, self.flow)

        assert result == StepStatus.FAILED
        assert step.outputs["completion_status"] == "failed"

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_missing_completion_status_defaults_to_completed(self, mock_parse, mock_caller_cls, mock_inj):
        """Missing completion_status field defaults to COMPLETED (backward compat)."""
        mock_parse.return_value = {
            "files_changed": ["a.py"],
            "tests_added": [],
            "test_mapping": {},
            "summary": "Done",
        }
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step()
        result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["completion_status"] == "complete"

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_summary_stored_in_outputs(self, mock_parse, mock_caller_cls, mock_inj):
        """Summary from LLM response is stored in step.outputs."""
        mock_parse.return_value = {
            "files_changed": [],
            "tests_added": [],
            "test_mapping": {},
            "summary": "Implemented feature X",
            "completion_status": "complete",
        }
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step()
        implement_handler(step, self.flow)

        assert step.outputs["summary"] == "Implemented feature X"


class TestRestrictedEditsSingleCall:
    """Test restricted_edits integration in _run_single_llm_call path."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        self.flow = FlowInstance(
            flow_id="test-flow",
            task_description="Test task",
            task_type="feature",
            change_path=self.project_root / "change",
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_step(self):
        return Step(
            step_type=StepType.IMPLEMENT,
            step_id="impl-001",
            inputs={
                "task_description": "test",
                "task_type": "feature",
                "task_groups": [{"group_id": "G1", "tasks": ["t1"]}],
                "spec_content": {},
            },
            outputs={},
        )

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_restricted_edits_applied_and_tracked(self, mock_parse, mock_caller_cls, mock_inj):
        """Restricted edits are applied and results stored in outputs."""
        # Create target file
        claude_dir = self.project_root / ".claude"
        claude_dir.mkdir()
        target = claude_dir / "CLAUDE.md"
        target.write_text("old line", encoding="utf-8")

        mock_parse.return_value = {
            "files_changed": ["src/main.py"],
            "tests_added": [],
            "test_mapping": {},
            "summary": "Done with restricted",
            "completion_status": "complete",
            "restricted_edits": [
                {"file_path": ".claude/CLAUDE.md", "old_string": "old line", "new_string": "new line"},
            ],
        }
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step()
        result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        assert len(step.outputs["restricted_edits_applied"]) == 1
        assert ".claude/CLAUDE.md" in step.outputs["files_changed"]
        assert target.read_text(encoding="utf-8") == "new line"

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_no_restricted_edits_field_backward_compat(self, mock_parse, mock_caller_cls, mock_inj):
        """Missing restricted_edits field doesn't break anything."""
        mock_parse.return_value = {
            "files_changed": ["a.py"],
            "tests_added": [],
            "test_mapping": {},
            "summary": "Done",
        }
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step()
        result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        assert "restricted_edits_applied" not in step.outputs

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_failed_restricted_edits_tracked(self, mock_parse, mock_caller_cls, mock_inj):
        """Failed restricted edits are tracked in outputs."""
        mock_parse.return_value = {
            "files_changed": [],
            "tests_added": [],
            "test_mapping": {},
            "summary": "Tried",
            "completion_status": "partial",
            "incomplete_tasks": ["Could not edit missing file"],
            "restricted_edits": [
                {"file_path": "missing.txt", "old_string": "x", "new_string": "y"},
            ],
        }
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step()
        result = implement_handler(step, self.flow)

        assert result == StepStatus.PARTIAL
        assert len(step.outputs["restricted_edits_failed"]) == 1
        assert step.outputs["restricted_edits_applied"] == []


class TestCompletionStatusGroupByGroup:
    """Test completion status aggregation in group-by-group path."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        self.flow = FlowInstance(
            flow_id="test-flow",
            task_description="Test task",
            task_type="feature",
            change_path=self.project_root / "change",
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_step(self, groups):
        return Step(
            step_type=StepType.IMPLEMENT,
            step_id="impl-001",
            inputs={
                "task_description": "test",
                "task_type": "feature",
                "task_groups": groups,
                "spec_content": {},
            },
            outputs={},
        )

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_all_groups_complete(self, mock_parse, mock_caller_cls, mock_inj):
        """All groups completing returns COMPLETED."""
        groups = [
            {"group_id": "G1", "group_order": 1, "tasks": ["t1"]},
            {"group_id": "G2", "group_order": 2, "tasks": ["t2"]},
        ]
        mock_parse.side_effect = [
            {"files_changed": ["a.py"], "summary": "G1 done", "completion_status": "complete"},
            {"files_changed": ["b.py"], "summary": "G2 done", "completion_status": "complete"},
        ]
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step(groups)
        result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["completion_status"] == "complete"

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_one_group_partial_returns_partial(self, mock_parse, mock_caller_cls, mock_inj):
        """One group being partial makes overall status PARTIAL."""
        groups = [
            {"group_id": "G1", "group_order": 1, "tasks": ["t1"]},
            {"group_id": "G2", "group_order": 2, "tasks": ["t2"]},
        ]
        mock_parse.side_effect = [
            {"files_changed": ["a.py"], "summary": "G1 done", "completion_status": "complete"},
            {
                "files_changed": ["b.py"],
                "summary": "G2 partial",
                "completion_status": "partial",
                "incomplete_tasks": ["Could not edit restricted file"],
            },
        ]
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step(groups)
        result = implement_handler(step, self.flow)

        assert result == StepStatus.PARTIAL
        assert step.outputs["completion_status"] == "partial"
        assert len(step.outputs["incomplete_tasks"]) == 1

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_failed_group_overrides_partial(self, mock_parse, mock_caller_cls, mock_inj):
        """A failed group makes overall status FAILED even if others are partial."""
        groups = [
            {"group_id": "G1", "group_order": 1, "tasks": ["t1"]},
            {"group_id": "G2", "group_order": 2, "tasks": ["t2"]},
        ]
        mock_parse.side_effect = [
            {"files_changed": [], "summary": "G1 failed", "completion_status": "failed", "incomplete_tasks": ["all blocked"]},
            {"files_changed": ["b.py"], "summary": "G2 partial", "completion_status": "partial", "incomplete_tasks": ["some blocked"]},
        ]
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step(groups)
        result = implement_handler(step, self.flow)

        assert result == StepStatus.FAILED
        assert step.outputs["completion_status"] == "failed"
        assert len(step.outputs["incomplete_tasks"]) == 2

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_missing_status_defaults_complete(self, mock_parse, mock_caller_cls, mock_inj):
        """Missing completion_status in group responses defaults to complete."""
        groups = [
            {"group_id": "G1", "group_order": 1, "tasks": ["t1"]},
            {"group_id": "G2", "group_order": 2, "tasks": ["t2"]},
        ]
        mock_parse.side_effect = [
            {"files_changed": ["a.py"], "summary": "G1 done"},
            {"files_changed": ["b.py"], "summary": "G2 done"},
        ]
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step(groups)
        result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["completion_status"] == "complete"

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_group_restricted_edits_aggregated(self, mock_parse, mock_caller_cls, mock_inj):
        """Restricted edits from multiple groups are aggregated."""
        # Create files for edits
        f1 = self.project_root / "a.txt"
        f1.write_text("old_a", encoding="utf-8")
        f2 = self.project_root / "b.txt"
        f2.write_text("old_b", encoding="utf-8")

        groups = [
            {"group_id": "G1", "group_order": 1, "tasks": ["t1"]},
            {"group_id": "G2", "group_order": 2, "tasks": ["t2"]},
        ]
        mock_parse.side_effect = [
            {
                "files_changed": [],
                "summary": "G1",
                "completion_status": "complete",
                "restricted_edits": [{"file_path": "a.txt", "old_string": "old_a", "new_string": "new_a"}],
            },
            {
                "files_changed": [],
                "summary": "G2",
                "completion_status": "complete",
                "restricted_edits": [{"file_path": "b.txt", "old_string": "old_b", "new_string": "new_b"}],
            },
        ]
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step(groups)
        result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        assert len(step.outputs["restricted_edits_applied"]) == 2
        assert f1.read_text(encoding="utf-8") == "new_a"
        assert f2.read_text(encoding="utf-8") == "new_b"
        assert "a.txt" in step.outputs["files_changed"]
        assert "b.txt" in step.outputs["files_changed"]

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("se3.engine.steps.implement.LLMCaller")
    @patch("se3.engine.steps.implement.parse_json_response")
    def test_summary_aggregated_across_groups(self, mock_parse, mock_caller_cls, mock_inj):
        """Summary is concatenated from all groups."""
        groups = [
            {"group_id": "G1", "group_order": 1, "tasks": ["t1"]},
            {"group_id": "G2", "group_order": 2, "tasks": ["t2"]},
        ]
        mock_parse.side_effect = [
            {"files_changed": [], "summary": "Added models", "completion_status": "complete"},
            {"files_changed": [], "summary": "Added views", "completion_status": "complete"},
        ]
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step(groups)
        implement_handler(step, self.flow)

        assert "Added models" in step.outputs["summary"]
        assert "Added views" in step.outputs["summary"]
