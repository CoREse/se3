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
        issue_mgr.create("Bug A", "desc A")
        issue_mgr.create("Bug B", "desc B")

        result = _run_cmd(["list"], project_dir)
        assert result.exit_code == 0
        assert "Bug A" in result.output
        assert "Bug B" in result.output

    def test_list_excludes_closed_by_default(self, project_dir, issue_mgr):
        issue_mgr.create("Open one", "d")
        issue_mgr.create("Closed one", "d")
        issue_mgr.update_status("002", IssueStatus.WONT_FIX)

        result = _run_cmd(["list"], project_dir)
        assert result.exit_code == 0
        assert "Open one" in result.output
        assert "Closed one" not in result.output

    def test_list_all_includes_closed(self, project_dir, issue_mgr):
        issue_mgr.create("Open one", "d")
        issue_mgr.create("Closed one", "d")
        issue_mgr.update_status("002", IssueStatus.WONT_FIX)

        result = _run_cmd(["list", "--all"], project_dir)
        assert result.exit_code == 0
        assert "Open one" in result.output
        assert "Closed one" in result.output


class TestShowCommand:
    def test_show_existing(self, project_dir, issue_mgr):
        issue_mgr.create("Show me", "Detailed description here")

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


class TestResetCommand:
    def test_reset_in_progress(self, project_dir, issue_mgr):
        issue_mgr.create("Stuck issue", "d")
        issue_mgr.update_status("001", IssueStatus.IN_PROGRESS)

        result = _run_cmd(["reset", "001"], project_dir)
        assert result.exit_code == 0
        assert "reset to open" in result.output

    def test_reset_non_in_progress(self, project_dir, issue_mgr):
        issue_mgr.create("Open issue", "d")

        result = _run_cmd(["reset", "001"], project_dir)
        assert result.exit_code == 1
        assert "Error" in result.output

    def test_reset_nonexistent(self, project_dir, issue_mgr):
        result = _run_cmd(["reset", "999"], project_dir)
        assert result.exit_code == 1
        assert "Error" in result.output


class TestDefaultCommand:
    def test_no_subcommand_lists_issues(self, project_dir, issue_mgr):
        issue_mgr.create("Default list", "d")

        result = _run_cmd([], project_dir)
        assert result.exit_code == 0
        assert "Default list" in result.output
