"""Tests for the salvage command.

Covers normal session, corrupted session, no session, no diff,
independent step failure, and issue creation scenarios.
"""

from __future__ import annotations

import json
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

from se3.commands.salvage_cmd import (
    salvage,
    _load_session,
    _assess_git_diff,
    _commit_changes,
    _create_salvage_issues,
    _archive_session,
)
from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)


@pytest.fixture
def project_root(tmp_path):
    """Create a minimal project directory."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "se3" / "state").mkdir(parents=True)
    (tmp_path / "se3" / "issues" / "open").mkdir(parents=True)
    (tmp_path / "se3" / "issues" / "closed").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def valid_flow():
    """Create a valid flow instance."""
    flow = FlowInstance(
        flow_id="test-flow-salvage",
        task_description="Implement user auth",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = [StepType.IMPLEMENT, StepType.TEST]

    impl = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED)
    flow.state.add_step(impl)

    test = Step(step_type=StepType.TEST, status=StepStatus.RUNNING)
    flow.state.add_step(test)
    flow.state.current_step_id = test.step_id

    return flow


def _write_flow_state(project_root: Path, flow: FlowInstance) -> None:
    """Write flow state to engine.json."""
    state_file = project_root / "se3" / "state" / "engine.json"
    data = flow.to_dict()
    state_file.write_text(json.dumps(data, default=str), encoding="utf-8")


class TestLoadSession:
    """Tests for session loading."""

    def test_load_valid_session(self, project_root, valid_flow):
        _write_flow_state(project_root, valid_flow)

        flow, warnings = _load_session(project_root)

        assert flow is not None
        assert flow.flow_id == valid_flow.flow_id
        assert len(warnings) == 0

    def test_load_corrupted_session(self, project_root):
        state_file = project_root / "se3" / "state" / "engine.json"
        state_file.write_text('{"flow_id": "test", "task_description": "task", "status": "running", "state": {', encoding="utf-8")

        flow, warnings = _load_session(project_root)

        # Should either recover partially or return None
        assert len(warnings) > 0

    def test_load_no_session(self, project_root):
        flow, warnings = _load_session(project_root)

        assert flow is None
        assert len(warnings) > 0


class TestAssessGitDiff:
    """Tests for git diff assessment."""

    @patch("se3.commands.salvage_cmd.subprocess.run")
    def test_assess_with_changes(self, mock_run):
        mock_run.side_effect = [
            MagicMock(stdout=" M src/auth.py\n M src/models.py\n", returncode=0),
            MagicMock(stdout="2 files changed, 30 insertions(+)", returncode=0),
            MagicMock(stdout="diff --git a/src/auth.py...", returncode=0),
        ]

        info = _assess_git_diff(Path("/fake"))

        assert info["changed_file_count"] == 2
        assert len(info["changed_files"]) == 2

    @patch("se3.commands.salvage_cmd.subprocess.run")
    def test_assess_no_changes(self, mock_run):
        mock_run.side_effect = [
            MagicMock(stdout="", returncode=0),
            MagicMock(stdout="", returncode=0),
            MagicMock(stdout="", returncode=0),
        ]

        info = _assess_git_diff(Path("/fake"))

        assert info["changed_file_count"] == 0


class TestCommitChanges:
    """Tests for committing changes."""

    def test_skip_when_no_changes(self):
        result = _commit_changes(Path("/fake"), None, {"changed_file_count": 0})
        assert result is None

    @patch("se3.commands.salvage_cmd.subprocess.run")
    def test_commit_with_changes(self, mock_run, valid_flow):
        mock_run.side_effect = [
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=0, stdout="", stderr=""),  # git commit
            MagicMock(returncode=0, stdout="abc1234\n"),  # git rev-parse
        ]

        result = _commit_changes(
            Path("/fake"),
            valid_flow,
            {"changed_file_count": 3},
        )

        assert result == "abc1234"

    @patch("se3.commands.salvage_cmd.subprocess.run")
    def test_commit_message_contains_task(self, mock_run, valid_flow):
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="abc1234\n"),
        ]

        _commit_changes(
            Path("/fake"),
            valid_flow,
            {"changed_file_count": 1},
        )

        # Check the commit message
        commit_call = mock_run.call_args_list[1]
        commit_msg = commit_call[0][0][3]  # git commit -m <msg>
        assert "[salvage]" in commit_msg
        assert "Implement user auth" in commit_msg


class TestCreateSalvageIssues:
    """Tests for issue creation."""

    def test_creates_issue_with_flow(self, project_root, valid_flow):
        issues = _create_salvage_issues(
            project_root, valid_flow,
            {"changed_files": ["src/auth.py"], "changed_file_count": 1},
        )

        assert len(issues) == 1
        assert "Incomplete" in issues[0].title
        assert "auto-discovered" in issues[0].tags
        assert "source:salvage" in issues[0].tags
        assert issues[0].priority == "medium"

    def test_creates_issue_without_flow(self, project_root):
        issues = _create_salvage_issues(
            project_root, None,
            {"changed_files": ["src/auth.py"], "changed_file_count": 1},
        )

        assert len(issues) == 1
        assert "source:salvage" in issues[0].tags

    def test_no_issue_when_no_flow_and_no_changes(self, project_root):
        issues = _create_salvage_issues(
            project_root, None,
            {"changed_files": [], "changed_file_count": 0},
        )

        assert len(issues) == 0

    def test_issue_includes_step_history(self, project_root, valid_flow):
        issues = _create_salvage_issues(
            project_root, valid_flow,
            {"changed_files": [], "changed_file_count": 0},
        )

        assert len(issues) == 1
        assert "implement" in issues[0].description.lower()


class TestArchiveSession:
    """Tests for session archiving."""

    def test_archives_existing_session(self, project_root, valid_flow):
        _write_flow_state(project_root, valid_flow)

        result = _archive_session(project_root)

        assert result is True
        assert not (project_root / "se3" / "state" / "engine.json").exists()
        archive_dir = project_root / "se3" / "state" / "archive"
        assert archive_dir.exists()
        assert len(list(archive_dir.glob("*.json"))) == 1

    def test_skip_when_no_session(self, project_root):
        result = _archive_session(project_root)
        assert result is False


class TestSalvageFullPipeline:
    """Tests for the full salvage pipeline."""

    @patch("se3.commands.salvage_cmd.subprocess.run")
    def test_salvage_with_valid_session(self, mock_run, project_root, valid_flow):
        _write_flow_state(project_root, valid_flow)

        # Mock git commands for no changes
        mock_run.side_effect = [
            MagicMock(stdout="", returncode=0),  # git status
            MagicMock(stdout="", returncode=0),  # git diff --stat
            MagicMock(stdout="", returncode=0),  # git diff HEAD
        ]

        exit_code = salvage(project_root)

        assert exit_code == 0

    @patch("se3.commands.salvage_cmd.subprocess.run")
    def test_salvage_with_no_session(self, mock_run, project_root):
        # Mock git commands for no changes
        mock_run.side_effect = [
            MagicMock(stdout="", returncode=0),
            MagicMock(stdout="", returncode=0),
            MagicMock(stdout="", returncode=0),
        ]

        exit_code = salvage(project_root)

        assert exit_code == 0

    def test_each_step_independent(self, project_root):
        """Each step should be independently fault-tolerant."""
        with patch("se3.commands.salvage_cmd._load_session", side_effect=Exception("boom")):
            with patch("se3.commands.salvage_cmd._assess_git_diff", return_value={"changed_file_count": 0}):
                with patch("se3.commands.salvage_cmd._create_salvage_issues", return_value=[]):
                    with patch("se3.commands.salvage_cmd._archive_session", return_value=False):
                        exit_code = salvage(project_root)
                        # Only step 1 fails, rest succeed
                        assert exit_code == 1  # has failure

    @patch("se3.commands.salvage_cmd.subprocess.run")
    def test_salvage_with_corrupted_session(self, mock_run, project_root):
        """Corrupted session should still allow git-diff-based salvage."""
        state_file = project_root / "se3" / "state" / "engine.json"
        state_file.write_text("{corrupted json data", encoding="utf-8")

        mock_run.side_effect = [
            MagicMock(stdout=" M src/file.py\n", returncode=0),
            MagicMock(stdout="1 file changed", returncode=0),
            MagicMock(stdout="diff content", returncode=0),
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=0, stdout="", stderr=""),  # git commit
            MagicMock(returncode=0, stdout="abc1234\n"),  # git rev-parse
        ]

        exit_code = salvage(project_root)

        # Should still succeed even with corrupted session
        assert exit_code == 0
