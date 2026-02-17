"""Tests for the fullcycle command."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from .fullcycle import (
    sanitize_change_name,
    run_full_cycle,
)


class TestSanitizeChangeName:
    """Test the sanitize_change_name function."""

    def test_simple_description(self):
        """Should convert simple description to valid change name."""
        assert sanitize_change_name("fix login bug") == "fix-login-bug"

    def test_description_with_special_chars(self):
        """Should remove special characters."""
        assert sanitize_change_name("fix: login bug!") == "fix-login-bug"

    def test_description_with_multiple_spaces(self):
        """Should handle multiple spaces."""
        assert sanitize_change_name("fix   login    bug") == "fix-login-bug"

    def test_description_with_underscores(self):
        """Should convert underscores to hyphens."""
        assert sanitize_change_name("fix_login_bug") == "fix-login-bug"

    def test_long_description(self):
        """Should truncate long descriptions."""
        long_desc = "a" * 100
        result = sanitize_change_name(long_desc)
        assert len(result) <= 50

    def test_empty_description(self):
        """Should handle empty description."""
        assert sanitize_change_name("") == ""

    def test_description_with_slashes(self):
        """Should preserve slashes for namespacing."""
        assert sanitize_change_name("feature/add login") == "feature/add-login"


class TestRunFullCycle:
    """Test the run_full_cycle function."""

    @patch("se3_tools.commands.fullcycle.run_session_start")
    @patch("se3_tools.commands.fullcycle.run_session_done")
    @patch("se3_tools.commands.fullcycle.Path.mkdir")
    @patch("se3_tools.commands.fullcycle.Path.write_text")
    def test_quick_mode(self, mock_write_text, mock_mkdir, mock_done, mock_start, tmp_path):
        """Quick mode should use 'small' workflow."""
        mock_start.return_value = {
            "git": {"branch": "main", "uncommitted_count": 0},
            "actions": [],
        }
        mock_done.return_value = {
            "uncommitted_changes": {"has_changes": False},
            "actions": [],
        }

        result = run_full_cycle("test description", str(tmp_path), quick=True)

        assert result["success"] is True
        assert result["quick_mode"] is True
        assert result["phases"]["work"]["workflow"] == "small"

    @patch("se3_tools.commands.fullcycle.run_session_start")
    @patch("se3_tools.commands.fullcycle.run_session_done")
    @patch("se3_tools.commands.fullcycle.Path.mkdir")
    @patch("se3_tools.commands.fullcycle.Path.write_text")
    def test_normal_mode(self, mock_write_text, mock_mkdir, mock_done, mock_start, tmp_path):
        """Normal mode should use 'feature' workflow."""
        mock_start.return_value = {
            "git": {"branch": "main", "uncommitted_count": 0},
            "actions": [],
        }
        mock_done.return_value = {
            "uncommitted_changes": {"has_changes": False},
            "actions": [],
        }

        result = run_full_cycle("test description", str(tmp_path), quick=False)

        assert result["success"] is True
        assert result["quick_mode"] is False
        assert result["phases"]["work"]["workflow"] == "feature"

    @patch("se3_tools.commands.fullcycle.run_session_start")
    def test_start_phase_actions(self, mock_start, tmp_path):
        """Should handle critical start phase actions."""
        mock_start.return_value = {
            "git": {"branch": "main", "uncommitted_count": 0},
            "actions": [{"type": "ask_user", "question": "What to build?"}],
        }

        result = run_full_cycle("test", str(tmp_path), quick=True)

        assert result["success"] is False
        assert "error" in result
        assert result["error"] == "Start phase requires manual intervention"

    @patch("se3_tools.commands.fullcycle.run_session_start")
    @patch("se3_tools.commands.fullcycle.run_session_done")
    @patch("se3_tools.commands.fullcycle.Path.mkdir")
    @patch("se3_tools.commands.fullcycle.Path.write_text")
    @patch("se3_tools.commands.fullcycle.Path.exists")
    def test_duplicate_change_name(self, mock_exists, mock_write_text, mock_mkdir, mock_done, mock_start, tmp_path):
        """Should handle duplicate change names by appending timestamp."""
        mock_exists.return_value = True
        mock_start.return_value = {
            "git": {"branch": "main", "uncommitted_count": 0},
            "actions": [],
        }
        mock_done.return_value = {
            "uncommitted_changes": {"has_changes": False},
            "actions": [],
        }

        result = run_full_cycle("test description", str(tmp_path), quick=True)

        assert result["success"] is True
        # Change name should have timestamp appended
        assert "-20" in result["change_name"]  # Timestamp format includes year

    @patch("se3_tools.commands.fullcycle.run_session_start")
    @patch("se3_tools.commands.fullcycle.run_session_done")
    @patch("se3_tools.commands.fullcycle.Path.mkdir")
    @patch("se3_tools.commands.fullcycle.Path.write_text")
    def test_result_structure(self, mock_write_text, mock_mkdir, mock_done, mock_start, tmp_path):
        """Result should have expected structure."""
        mock_start.return_value = {
            "git": {"branch": "main", "uncommitted_count": 0},
            "actions": [],
        }
        mock_done.return_value = {
            "uncommitted_changes": {"has_changes": True, "count": 2, "files": ["file1.py"]},
            "actions": [{"type": "commit"}],
        }

        result = run_full_cycle("test description", str(tmp_path), quick=True)

        assert "description" in result
        assert "quick_mode" in result
        assert "project_root" in result
        assert "phases" in result
        assert "success" in result
        assert "actions" in result
        assert "change_name" in result

        # Check phases
        assert "start" in result["phases"]
        assert "work" in result["phases"]
        assert "implementation" in result["phases"]
        assert "done" in result["phases"]

    @patch("se3_tools.commands.fullcycle.run_session_start")
    @patch("se3_tools.commands.fullcycle.run_session_done")
    @patch("se3_tools.commands.fullcycle.Path.mkdir")
    @patch("se3_tools.commands.fullcycle.Path.write_text")
    def test_actions_include_commit_when_changes(self, mock_write_text, mock_mkdir, mock_done, mock_start, tmp_path):
        """Should include commit action when there are uncommitted changes."""
        mock_start.return_value = {
            "git": {"branch": "main", "uncommitted_count": 2},
            "actions": [],
        }
        mock_done.return_value = {
            "uncommitted_changes": {"has_changes": True, "count": 2, "files": ["file1.py", "file2.py"]},
            "actions": [{"type": "commit"}],
        }

        result = run_full_cycle("test description", str(tmp_path), quick=True)

        action_types = [a["type"] for a in result["actions"]]
        assert "commit" in action_types
        assert "handoff" in action_types
