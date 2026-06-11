"""Tests for SE3 Issue CLI commands."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from se3.commands.issue_cmd import app
from se3.engine.issue_manager import IssueManager, IssueStatus

runner = CliRunner()


@pytest.fixture
def project_dir(tmp_path):
    """Create a temporary project directory with .git marker."""
    (tmp_path / ".git").mkdir()
    return tmp_path


@pytest.fixture
def issue_mgr(project_dir):
    """Create an IssueManager for the temp project."""
    mgr = IssueManager(project_dir)
    mgr._ensure_dirs()
    return mgr


def _run_cmd(args, project_dir):
    """Run CLI command with project_root patched."""
    with patch("se3.commands.issue_cmd.get_project_root", return_value=project_dir):
        return runner.invoke(app, args)


class TestListCommand:
    def test_list_empty(self, project_dir, issue_mgr):
        result = _run_cmd(["list"], project_dir)
        assert result.exit_code == 0
        assert "No" in result.output and "issues" in result.output

    def test_list_with_issues(self, project_dir, issue_mgr):
        issue_mgr.create("desc A", title="Bug A")
        issue_mgr.create("desc B", title="Bug B")

        result = _run_cmd(["list"], project_dir)
        assert result.exit_code == 0
        assert "Bug A" in result.output
        assert "Bug B" in result.output

    def test_list_excludes_closed_by_default(self, project_dir, issue_mgr):
        issue_mgr.create("d", title="Open one")
        issue_mgr.create("d", title="Closed one")
        issue_mgr.update_status("002", IssueStatus.WONT_FIX)

        result = _run_cmd(["list"], project_dir)
        assert result.exit_code == 0
        assert "Open one" in result.output
        assert "Closed one" not in result.output

    def test_list_all_includes_closed(self, project_dir, issue_mgr):
        issue_mgr.create("d", title="Open one")
        issue_mgr.create("d", title="Closed one")
        issue_mgr.update_status("002", IssueStatus.WONT_FIX)

        result = _run_cmd(["list", "--all"], project_dir)
        assert result.exit_code == 0
        assert "Open one" in result.output
        assert "Closed one" in result.output


class TestShowCommand:
    def test_show_existing(self, project_dir, issue_mgr):
        issue_mgr.create("Detailed description here", title="Show me")

        result = _run_cmd(["show", "001"], project_dir)
        assert result.exit_code == 0
        assert "Show me" in result.output
        assert "Detailed description here" in result.output

    def test_show_nonexistent(self, project_dir, issue_mgr):
        result = _run_cmd(["show", "999"], project_dir)
        assert result.exit_code == 1
        assert "not found" in result.output


class TestCreateCommand:
    def test_create_interactive(self, project_dir):
        with patch("se3.commands.issue_cmd.get_project_root", return_value=project_dir):
            result = runner.invoke(
                app,
                ["create"],
                input="New Bug\nThis is a bug description\nbug\nhigh\nsource:test,bug\n",
            )
        assert result.exit_code == 0
        assert "Created issue 001" in result.output

        # Verify file was created
        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert issue is not None
        assert issue.title == "New Bug"
        assert issue.priority == "high"
        assert issue.tags == ["source:test", "bug"]

    def test_create_with_defaults(self, project_dir):
        with patch("se3.commands.issue_cmd.get_project_root", return_value=project_dir):
            result = runner.invoke(
                app,
                ["create"],
                input="Simple Issue\nJust a simple issue\n\n\n\n",
            )
        assert result.exit_code == 0
        assert "Created issue 001" in result.output

    def test_create_cancelled(self, project_dir):
        """User cancels at the first prompt (None returned from _prompt_field)."""
        with patch("se3.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("se3.commands.issue_cmd._prompt_field", return_value=None):
            result = runner.invoke(app, ["create"])
        assert result.exit_code == 1
        assert "Cancelled" in result.output

        mgr = IssueManager(project_dir)
        assert mgr.load("001") is None

    def test_create_uses_prompt_field_with_defaults(self, project_dir):
        """create_cmd composes mgr.create() correctly from _prompt_field returns."""
        side_effects = [
            "Mocked Title",
            "Mocked description with\nmultiple lines",
            "feature",
            "low",
            "tag1,tag2",
        ]
        with patch("se3.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("se3.commands.issue_cmd._prompt_field", side_effect=side_effects) as mock_prompt:
            result = runner.invoke(app, ["create"])

        assert result.exit_code == 0
        assert "Created issue 001" in result.output
        assert mock_prompt.call_count == 5

        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert issue is not None
        assert issue.title == "Mocked Title"
        assert "multiple lines" in issue.description
        assert issue.type == "feature"
        assert issue.priority == "low"
        assert issue.tags == ["tag1", "tag2"]


class TestPromptField:
    """Unit tests for the _prompt_field helper itself."""

    def test_tty_delegates_to_read_multiline_input(self):
        from se3.commands import issue_cmd

        with patch.object(issue_cmd.sys, "stdin") as mock_stdin, \
             patch("se3.cli._read_multiline_input", return_value="user content") as mock_read:
            mock_stdin.isatty.return_value = True
            result = issue_cmd._prompt_field("Title", "Enter title:")
        assert result == "user content"
        mock_read.assert_called_once_with(prompt_title="Title", prompt_message="Enter title:")

    def test_tty_empty_falls_back_to_default(self):
        from se3.commands import issue_cmd

        with patch.object(issue_cmd.sys, "stdin") as mock_stdin, \
             patch("se3.cli._read_multiline_input", return_value=""):
            mock_stdin.isatty.return_value = True
            result = issue_cmd._prompt_field("Type", "Enter type:", default="bug")
        assert result == "bug"

    def test_tty_none_propagates_cancellation(self):
        from se3.commands import issue_cmd

        with patch.object(issue_cmd.sys, "stdin") as mock_stdin, \
             patch("se3.cli._read_multiline_input", return_value=None):
            mock_stdin.isatty.return_value = True
            result = issue_cmd._prompt_field("Title", "Enter title:", default="ignored")
        assert result is None

    def test_non_tty_reads_one_line(self):
        import io

        from se3.commands import issue_cmd

        fake_stdin = io.StringIO("first line\nsecond line\n")
        fake_stdin.isatty = lambda: False  # type: ignore[method-assign]
        with patch.object(issue_cmd.sys, "stdin", fake_stdin):
            first = issue_cmd._prompt_field("F1", "msg")
            second = issue_cmd._prompt_field("F2", "msg")
        assert first == "first line"
        assert second == "second line"

    def test_non_tty_empty_falls_back_to_default(self):
        import io

        from se3.commands import issue_cmd

        fake_stdin = io.StringIO("\n")
        fake_stdin.isatty = lambda: False  # type: ignore[method-assign]
        with patch.object(issue_cmd.sys, "stdin", fake_stdin):
            result = issue_cmd._prompt_field("Type", "msg", default="bug")
        assert result == "bug"


class TestResetCommand:
    def test_reset_in_progress(self, project_dir, issue_mgr):
        issue_mgr.create("d", title="Stuck issue")
        issue_mgr.update_status("001", IssueStatus.IN_PROGRESS)

        result = _run_cmd(["reset", "001"], project_dir)
        assert result.exit_code == 0
        assert "reset to open" in result.output

    def test_reset_non_in_progress(self, project_dir, issue_mgr):
        issue_mgr.create("d", title="Open issue")

        result = _run_cmd(["reset", "001"], project_dir)
        assert result.exit_code == 1
        assert "Error" in result.output

    def test_reset_nonexistent(self, project_dir, issue_mgr):
        result = _run_cmd(["reset", "999"], project_dir)
        assert result.exit_code == 1
        assert "Error" in result.output


class TestDefaultCommand:
    def test_no_subcommand_lists_issues(self, project_dir, issue_mgr):
        issue_mgr.create("d", title="Default list")

        result = _run_cmd([], project_dir)
        assert result.exit_code == 0
        assert "Default list" in result.output
