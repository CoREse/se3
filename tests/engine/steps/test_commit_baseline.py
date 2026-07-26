"""Tests for _has_changes() baseline commit diff logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from tianluo.engine.steps.commit import _has_changes


def _run_result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class TestHasChangesBaseline:
    """Tests for _has_changes with baseline_commit parameter."""

    @patch("tianluo.engine.steps.commit.subprocess.run")
    def test_no_baseline_uses_git_status(self, mock_run):
        """Without baseline_commit, falls back to git status --porcelain."""
        mock_run.return_value = _run_result(stdout="M file.py\n")
        assert _has_changes(Path("/tmp/proj")) is True
        args = mock_run.call_args[0][0]
        assert args == ["git", "status", "--porcelain"]

    @patch("tianluo.engine.steps.commit.subprocess.run")
    def test_no_baseline_clean_working_tree(self, mock_run):
        """Without baseline, clean working tree returns False."""
        mock_run.return_value = _run_result(stdout="")
        assert _has_changes(Path("/tmp/proj")) is False

    @patch("tianluo.engine.steps.commit.subprocess.run")
    def test_baseline_with_diff_detected(self, mock_run):
        """With baseline_commit, git diff exit code 1 means changes exist."""
        # git diff --quiet exits 1 when there are differences
        mock_run.return_value = _run_result(returncode=1)
        assert _has_changes(Path("/tmp/proj"), baseline_commit="abc123") is True
        args = mock_run.call_args[0][0]
        assert args == ["git", "diff", "abc123", "HEAD", "--quiet"]

    @patch("tianluo.engine.steps.commit.subprocess.run")
    def test_baseline_no_diff_clean_tree(self, mock_run):
        """baseline == HEAD and clean tree -> no changes."""
        # First call: git diff --quiet exits 0 (no diff)
        # Second call: git status --porcelain returns empty
        mock_run.side_effect = [
            _run_result(returncode=0),  # git diff
            _run_result(stdout=""),  # git status
        ]
        assert _has_changes(Path("/tmp/proj"), baseline_commit="abc123") is False
        assert mock_run.call_count == 2

    @patch("tianluo.engine.steps.commit.subprocess.run")
    def test_baseline_no_diff_but_dirty_tree(self, mock_run):
        """baseline == HEAD but working tree has uncommitted changes."""
        mock_run.side_effect = [
            _run_result(returncode=0),  # git diff: no committed diff
            _run_result(stdout="M dirty.py\n"),  # git status: uncommitted changes
        ]
        assert _has_changes(Path("/tmp/proj"), baseline_commit="abc123") is True

    @patch("tianluo.engine.steps.commit.subprocess.run")
    def test_baseline_git_diff_error_falls_back(self, mock_run):
        """If git diff fails (returncode > 1), falls back to git status."""
        mock_run.side_effect = [
            _run_result(returncode=128, stderr="fatal: bad object"),
            _run_result(stdout="M file.py\n"),
        ]
        assert _has_changes(Path("/tmp/proj"), baseline_commit="badref") is True

    @patch("tianluo.engine.steps.commit.subprocess.run")
    def test_baseline_git_diff_exception_falls_back(self, mock_run):
        """If git diff raises an exception, falls back to git status."""
        mock_run.side_effect = [
            OSError("git not found"),
            _run_result(stdout=""),
        ]
        assert _has_changes(Path("/tmp/proj"), baseline_commit="abc123") is False


class TestCommitHandlerPassesBaseline:
    """commit_handler passes flow.baseline_commit to _has_changes."""

    @patch("tianluo.engine.steps.commit._has_changes", return_value=False)
    def test_baseline_commit_forwarded(self, mock_has_changes):
        """commit_handler passes baseline_commit from flow to _has_changes."""
        from tianluo.engine.models import FlowInstance, Step, StepType, StepStatus

        flow = MagicMock(spec=FlowInstance)
        flow.baseline_commit = "deadbeef"
        flow.change_path = Path("/tmp/proj/se3.yaml")
        flow.flow_id = "test-flow"
        flow.task_type = "bugfix"
        flow.task_description = "fix something"

        step = MagicMock(spec=Step)
        step.inputs = {}
        step.outputs = {}

        from tianluo.engine.steps.commit import commit_handler
        result = commit_handler(step, flow)

        mock_has_changes.assert_called_once_with(
            Path("/tmp/proj"),
            baseline_commit="deadbeef",
        )
        assert result == StepStatus.COMPLETED
        assert step.outputs["committed"] is False

    @patch("tianluo.engine.steps.commit._has_changes", return_value=False)
    def test_no_baseline_commit_passes_none(self, mock_has_changes):
        """commit_handler passes None when flow has no baseline_commit."""
        from tianluo.engine.models import FlowInstance, Step, StepType, StepStatus

        flow = MagicMock(spec=FlowInstance)
        flow.baseline_commit = None
        flow.change_path = Path("/tmp/proj/se3.yaml")
        flow.flow_id = "test-flow"
        flow.task_type = "bugfix"
        flow.task_description = "fix something"

        step = MagicMock(spec=Step)
        step.inputs = {}
        step.outputs = {}

        from tianluo.engine.steps.commit import commit_handler
        result = commit_handler(step, flow)

        mock_has_changes.assert_called_once_with(
            Path("/tmp/proj"),
            baseline_commit=None,
        )
