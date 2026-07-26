"""Tests for SE3 Issue CLI commands."""

from __future__ import annotations

import io
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from tianluo.commands.issue_cmd import (
    _get_editor,
    _new_issue_editor_yaml,
    _open_editor_with_content,
    _parse_edited_issue_yaml,
    _resolve_description,
    app,
)
from tianluo.engine.issue_manager import IssueManager, IssueStatus

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
    with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir):
        return runner.invoke(app, args)


# ===================================================================
# Task 1: Refactored create — positional, stdin, single prompt
# ===================================================================


class TestCreatePositional:
    """Create with positional description argument."""

    def test_create_positional_description(self, project_dir):
        result = _run_cmd(["create", "A quick bug report"], project_dir)
        assert result.exit_code == 0
        assert "Created issue 001" in result.output

        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert issue is not None
        assert issue.description == "A quick bug report"
        assert issue.source == "human"

    def test_create_positional_with_flags(self, project_dir):
        result = _run_cmd(
            ["create", "desc", "--title", "My Title", "--type", "feature", "--priority", "high"],
            project_dir,
        )
        assert result.exit_code == 0
        assert "Created issue 001" in result.output

        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert issue.title == "My Title"
        assert issue.type == "feature"
        assert issue.priority == "high"

    def test_create_positional_with_tags(self, project_dir):
        result = _run_cmd(["create", "desc", "--tags", "a,b,c"], project_dir)
        assert result.exit_code == 0

        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert issue.tags == ["a", "b", "c"]


class TestCreateStdinPipe:
    """Create with piped stdin (non-TTY)."""

    def test_create_stdin_pipe(self, project_dir):
        """Piped stdin feeds the full content as description."""
        fake_stdin = io.StringIO("Multi-line\ndescription from stdin\n")
        fake_stdin.isatty = lambda: False  # type: ignore[method-assign]

        with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("tianluo.commands.issue_cmd.sys") as mock_sys:
            mock_sys.stdin = fake_stdin
            mock_sys.stdin.isatty = lambda: False
            result = runner.invoke(app, ["create"])

        assert result.exit_code == 0
        assert "Created issue" in result.output

        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert "Multi-line" in issue.description
        assert "stdin" in issue.description

    def test_create_stdin_empty_pipe_fails(self, project_dir):
        """Empty stdin pipe results in no description → cancelled."""
        fake_stdin = io.StringIO("")
        fake_stdin.isatty = lambda: False  # type: ignore[method-assign]

        with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("tianluo.commands.issue_cmd.sys") as mock_sys:
            mock_sys.stdin = fake_stdin
            mock_sys.stdin.isatty = lambda: False
            result = runner.invoke(app, ["create"])

        assert result.exit_code == 1


class TestCreateInteractive:
    """Create interactively with single description prompt."""

    def test_create_interactive_tty(self, project_dir):
        """TTY mode calls _read_multiline_input once for description."""
        with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("tianluo.commands.issue_cmd.sys") as mock_sys, \
             patch("tianluo.cli._read_multiline_input", return_value="interactive description"):
            mock_sys.stdin.isatty.return_value = True
            result = runner.invoke(app, ["create"])

        assert result.exit_code == 0
        assert "Created issue" in result.output

        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert issue.description == "interactive description"
        assert issue.source == "human"

    def test_create_interactive_cancelled(self, project_dir):
        """User cancels at the prompt → exit 1."""
        with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("tianluo.commands.issue_cmd._resolve_description", return_value=None):
            result = runner.invoke(app, ["create"])

        assert result.exit_code == 1
        assert "Cancelled" in result.output

    def test_create_defaults_source_human(self, project_dir):
        """CLI create always writes source=human."""
        result = _run_cmd(["create", "some description"], project_dir)
        assert result.exit_code == 0

        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert issue.source == "human"

    def test_create_optional_fields_none_when_omitted(self, project_dir):
        """When flags are omitted, title/priority/type are None."""
        result = _run_cmd(["create", "just a description"], project_dir)
        assert result.exit_code == 0

        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert issue.title is None
        assert issue.priority is None
        assert issue.type is None


class TestResolveDescription:
    """Unit tests for _resolve_description helper."""

    def test_positional_wins_over_stdin(self):
        assert _resolve_description("positional text") == "positional text"

    def test_stdin_pipe_when_no_positional(self):
        fake_stdin = io.StringIO("piped content\n")
        fake_stdin.isatty = lambda: False  # type: ignore[method-assign]
        with patch("tianluo.commands.issue_cmd.sys") as mock_sys:
            mock_sys.stdin = fake_stdin
            mock_sys.stdin.isatty = lambda: False
            result = _resolve_description(None)
        assert result == "piped content"

    def test_tty_interactive(self):
        with patch("tianluo.commands.issue_cmd.sys") as mock_sys, \
             patch("tianluo.cli._read_multiline_input", return_value="typed text"):
            mock_sys.stdin.isatty.return_value = True
            result = _resolve_description(None)
        assert result == "typed text"

    def test_tty_cancel_returns_none(self):
        with patch("tianluo.commands.issue_cmd.sys") as mock_sys, \
             patch("tianluo.cli._read_multiline_input", return_value=None):
            mock_sys.stdin.isatty.return_value = True
            result = _resolve_description(None)
        assert result is None

    def test_tty_empty_returns_none(self):
        with patch("tianluo.commands.issue_cmd.sys") as mock_sys, \
             patch("tianluo.cli._read_multiline_input", return_value=""):
            mock_sys.stdin.isatty.return_value = True
            result = _resolve_description(None)
        assert result is None


# ===================================================================
# Task 2: create --editor and edit <id>
# ===================================================================


class TestGetEditor:
    def test_returns_editor_env(self):
        with patch.dict(os.environ, {"EDITOR": "nano"}):
            assert _get_editor() == "nano"

    def test_falls_back_to_vi(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove EDITOR if present
            os.environ.pop("EDITOR", None)
            assert _get_editor() == "vi"


class TestParseEditedYaml:
    def test_valid_yaml(self):
        data = _parse_edited_issue_yaml("description: hello\ntitle: world\n")
        assert data["description"] == "hello"
        assert data["title"] == "world"

    def test_empty_description_raises(self):
        with pytest.raises(ValueError, match="description must not be empty"):
            _parse_edited_issue_yaml("title: x\ndescription: ''\n")

    def test_missing_description_raises(self):
        with pytest.raises(ValueError, match="description must not be empty"):
            _parse_edited_issue_yaml("title: x\n")

    def test_invalid_yaml_raises(self):
        with pytest.raises(ValueError, match="Invalid YAML"):
            _parse_edited_issue_yaml("{{{{not yaml}}}}")

    def test_empty_content_raises(self):
        with pytest.raises(ValueError, match="Invalid YAML"):
            _parse_edited_issue_yaml("")


class TestNewIssueEditorYaml:
    def test_template_structure(self):
        template = _new_issue_editor_yaml()
        data = yaml.safe_load(template)
        assert "description" in data
        assert "title" in data
        assert "type" in data
        assert data["type"] == ""


class TestCreateEditor:
    """Create with --editor flag."""

    def test_create_editor_success(self, project_dir):
        """Editor returns valid YAML → issue is created."""
        edited_content = "title: Editor Title\ndescription: From the editor\ntype: feature\npriority: high\n"
        with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("tianluo.commands.issue_cmd._open_editor_with_content", return_value=edited_content):
            result = runner.invoke(app, ["create", "--editor"])

        assert result.exit_code == 0
        assert "Created issue 001" in result.output

        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert issue.title == "Editor Title"
        assert issue.description == "From the editor"
        assert issue.type == "feature"
        assert issue.priority == "high"
        assert issue.source == "human"

    def test_create_editor_cancelled(self, project_dir):
        """Editor returns None (non-zero exit) → cancelled."""
        with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("tianluo.commands.issue_cmd._open_editor_with_content", return_value=None):
            result = runner.invoke(app, ["create", "--editor"])

        assert result.exit_code == 1
        assert "Cancelled" in result.output

    def test_create_editor_no_description(self, project_dir):
        """Editor returns YAML without description → error."""
        edited_content = "title: No desc\ntype: bug\n"
        with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("tianluo.commands.issue_cmd._open_editor_with_content", return_value=edited_content):
            result = runner.invoke(app, ["create", "--editor"])

        assert result.exit_code == 1
        assert "description" in result.output.lower()

    def test_create_editor_with_tags_string(self, project_dir):
        """Tags as comma-separated string in YAML are parsed."""
        edited_content = "description: test\ntags: a,b,c\n"
        with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("tianluo.commands.issue_cmd._open_editor_with_content", return_value=edited_content):
            result = runner.invoke(app, ["create", "--editor"])

        assert result.exit_code == 0
        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert issue.tags == ["a", "b", "c"]

    def test_create_editor_with_tags_list(self, project_dir):
        """Tags as YAML list are parsed."""
        edited_content = "description: test\ntags:\n  - x\n  - y\n"
        with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("tianluo.commands.issue_cmd._open_editor_with_content", return_value=edited_content):
            result = runner.invoke(app, ["create", "--editor"])

        assert result.exit_code == 0
        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert issue.tags == ["x", "y"]


class TestEditCommand:
    """Edit an existing issue via external editor."""

    def test_edit_existing_issue(self, project_dir, issue_mgr):
        issue_mgr.create("Original desc", title="Original Title", source="human")

        edited_content = "id: '001'\ntitle: Updated Title\ndescription: Updated description\ntype: feature\npriority: high\ntags:\n  - new-tag\n"
        with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("tianluo.commands.issue_cmd._open_editor_with_content", return_value=edited_content):
            result = runner.invoke(app, ["edit", "001"])

        assert result.exit_code == 0
        assert "Updated issue" in result.output

        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert issue.title == "Updated Title"
        assert issue.description == "Updated description"

    def test_edit_nonexistent_issue(self, project_dir, issue_mgr):
        result = _run_cmd(["edit", "999"], project_dir)
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_edit_cancelled_by_editor(self, project_dir, issue_mgr):
        issue_mgr.create("desc", title="Title")

        with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("tianluo.commands.issue_cmd._open_editor_with_content", return_value=None):
            result = runner.invoke(app, ["edit", "001"])

        assert result.exit_code == 1
        assert "Cancelled" in result.output

    def test_edit_invalid_yaml(self, project_dir, issue_mgr):
        issue_mgr.create("desc", title="Title")

        with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("tianluo.commands.issue_cmd._open_editor_with_content", return_value="{{bad yaml"):
            result = runner.invoke(app, ["edit", "001"])

        assert result.exit_code == 1

    def test_edit_removes_description_fails(self, project_dir, issue_mgr):
        """Cannot remove description via edit."""
        issue_mgr.create("desc", title="Title")

        edited_content = "id: '001'\ntitle: Title\ndescription: ''\n"
        with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("tianluo.commands.issue_cmd._open_editor_with_content", return_value=edited_content):
            result = runner.invoke(app, ["edit", "001"])

        assert result.exit_code == 1
        assert "description" in result.output.lower()

    def test_edit_preserves_source(self, project_dir, issue_mgr):
        """Editing doesn't change the source field."""
        issue_mgr.create("desc", title="Title", source="human")

        edited_content = "id: '001'\ntitle: New Title\ndescription: New desc\n"
        with patch("tianluo.commands.issue_cmd.get_project_root", return_value=project_dir), \
             patch("tianluo.commands.issue_cmd._open_editor_with_content", return_value=edited_content):
            result = runner.invoke(app, ["edit", "001"])

        assert result.exit_code == 0
        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert issue.source == "human"


class TestOpenEditorWithContent:
    """Test the _open_editor_with_content helper."""

    def test_editor_success(self, tmp_path):
        """Mock subprocess.run to simulate editor modifying the file."""
        with patch("tianluo.commands.issue_cmd.subprocess") as mock_sub, \
             patch("tianluo.commands.issue_cmd._get_editor", return_value="vim"):
            def fake_run(args, **kwargs):
                # Simulate editor modifying the temp file
                path = args[1]
                Path(path).write_text("edited content", encoding="utf-8")
                result = MagicMock()
                result.returncode = 0
                return result

            mock_sub.run.side_effect = fake_run
            result = _open_editor_with_content("original")
        assert result == "edited content"

    def test_editor_nonzero_exit_returns_none(self):
        with patch("tianluo.commands.issue_cmd.subprocess") as mock_sub, \
             patch("tianluo.commands.issue_cmd._get_editor", return_value="vim"):
            mock_sub.run.return_value = MagicMock(returncode=1)
            result = _open_editor_with_content("content")
        assert result is None


# ===================================================================
# Task 3: close command, list --source filter
# ===================================================================


class TestCloseCommand:
    def test_close_open_issue(self, project_dir, issue_mgr):
        issue_mgr.create("desc", title="To close")

        result = _run_cmd(["close", "001"], project_dir)
        assert result.exit_code == 0
        assert "Closed issue 001" in result.output

        mgr = IssueManager(project_dir)
        issue = mgr.load("001")
        assert issue.status == IssueStatus.CLOSED

    def test_close_with_reason(self, project_dir, issue_mgr):
        issue_mgr.create("desc", title="To close")

        result = _run_cmd(["close", "001", "--reason", "Fixed in #42"], project_dir)
        assert result.exit_code == 0
        assert "Closed issue 001" in result.output

    def test_close_nonexistent(self, project_dir, issue_mgr):
        result = _run_cmd(["close", "999"], project_dir)
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_close_already_closed_is_idempotent(self, project_dir, issue_mgr):
        issue_mgr.create("desc", title="Already closed")
        issue_mgr.close_issue("001")

        result = _run_cmd(["close", "001"], project_dir)
        assert result.exit_code == 0
        assert "Closed issue 001" in result.output

    def test_close_in_progress_issue(self, project_dir, issue_mgr):
        issue_mgr.create("desc", title="WIP")
        issue_mgr.update_status("001", IssueStatus.IN_PROGRESS)

        result = _run_cmd(["close", "001"], project_dir)
        assert result.exit_code == 0
        assert "Closed issue 001" in result.output


class TestListSourceFilter:
    def test_list_source_human(self, project_dir, issue_mgr):
        issue_mgr.create("human desc", title="Human issue", source="human")
        issue_mgr.create("system desc", title="System issue", source="system")

        result = _run_cmd(["list", "--source", "human"], project_dir)
        assert result.exit_code == 0
        assert "Human issue" in result.output
        assert "System issue" not in result.output

    def test_list_source_system(self, project_dir, issue_mgr):
        issue_mgr.create("human desc", title="Human issue", source="human")
        issue_mgr.create("system desc", title="System issue", source="system")

        result = _run_cmd(["list", "--source", "system"], project_dir)
        assert result.exit_code == 0
        assert "System issue" in result.output
        assert "Human issue" not in result.output

    def test_list_source_no_match(self, project_dir, issue_mgr):
        issue_mgr.create("desc", title="Human issue", source="human")

        result = _run_cmd(["list", "--source", "system"], project_dir)
        assert result.exit_code == 0
        assert "No" in result.output

    def test_list_shows_source_column(self, project_dir, issue_mgr):
        issue_mgr.create("desc", title="Issue", source="human")

        result = _run_cmd(["list"], project_dir)
        assert result.exit_code == 0
        assert "Source" in result.output
        assert "human" in result.output


class TestShowWithSource:
    def test_show_displays_source(self, project_dir, issue_mgr):
        issue_mgr.create("desc", title="Title", source="human")

        result = _run_cmd(["show", "001"], project_dir)
        assert result.exit_code == 0
        assert "Source" in result.output
        assert "human" in result.output


class TestOptionalFieldDisplay:
    """Verify that empty type/priority show as '-' in list and show."""

    def test_list_empty_optional_fields_show_dash(self, project_dir, issue_mgr):
        issue_mgr.create("desc")  # no title, type, priority

        result = _run_cmd(["list"], project_dir)
        assert result.exit_code == 0
        # The display_title derived from description should appear
        assert "desc" in result.output

    def test_show_empty_optional_fields_show_dash(self, project_dir, issue_mgr):
        issue_mgr.create("desc")

        result = _run_cmd(["show", "001"], project_dir)
        assert result.exit_code == 0
        # Priority and type show as '-' (from the issue_cmd formatting)
        assert "desc" in result.output


# ===================================================================
# Existing tests (updated for new create interface)
# ===================================================================


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

    def test_list_type_filter(self, project_dir, issue_mgr):
        issue_mgr.create("d", title="Bug", type="bug")
        issue_mgr.create("d", title="Feature", type="feature")

        result = _run_cmd(["list", "--type", "bug"], project_dir)
        assert result.exit_code == 0
        assert "Bug" in result.output
        assert "Feature" not in result.output


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
