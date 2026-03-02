"""Tests for claude_runner and config modules.

Tests cover:
- Claude command loading and priority sorting
- Usage limit detection (keywords, exit codes)
- Timeout detection
- Fallback to next command on limit/timeout
- All commands exhausted behavior
- on_retry callback mechanism
- popen and retry_with_next
- CLI claude-cmd subcommand
"""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from se3.config import load_claude_commands
from se3.claude_runner import (
    ClaudeRunner,
    USAGE_LIMIT_KEYWORDS,
)


# =============================================================================
# Config Loading
# =============================================================================

class TestLoadClaudeCommands:
    """Test loading and sorting claude commands."""

    def test_default_when_no_config(self, tmp_path):
        """Should return default 'claude' command when no config exists."""
        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(tmp_path)
        assert len(commands) == 1
        assert commands[0]["cmd"] == "claude"
        assert commands[0]["priority"] == 0

    def test_project_config_overrides(self, tmp_path):
        """Project config should override global."""
        # Create global config
        global_se3_dir = tmp_path / ".se3"
        global_se3_dir.mkdir()
        global_config = global_se3_dir / "config.yaml"
        global_config.write_text("claude_commands:\n  - cmd: global-claude\n    priority: 10\n")

        # Create project config
        project_config = tmp_path / "se3.yaml"
        project_config.write_text("claude_commands:\n  - cmd: project-claude\n    priority: 5\n")

        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(tmp_path)

        # Project config should override global
        assert len(commands) == 1
        assert commands[0]["cmd"] == "project-claude"

    def test_global_used_when_no_project_config(self, tmp_path):
        """Global config should be used when no project config exists."""
        global_se3_dir = tmp_path / ".se3"
        global_se3_dir.mkdir()
        global_config = global_se3_dir / "config.yaml"
        global_config.write_text("claude_commands:\n  - cmd: global-claude\n    priority: 10\n")

        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(tmp_path)

        assert commands[0]["cmd"] == "global-claude"

    def test_priority_sorting(self, tmp_path):
        """Commands should be sorted by priority (higher first)."""
        project_config = tmp_path / "se3.yaml"
        project_config.write_text("""claude_commands:
  - cmd: low
    priority: 1
  - cmd: high
    priority: 10
  - cmd: mid
    priority: 5
""")

        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(tmp_path)

        assert [c["cmd"] for c in commands] == ["high", "mid", "low"]

    def test_string_entries_normalized(self, tmp_path):
        """String command entries should be normalized to dicts."""
        project_config = tmp_path / "se3.yaml"
        project_config.write_text("claude_commands:\n  - claude\n  - kclaude\n")

        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(tmp_path)

        assert len(commands) == 2
        assert all(isinstance(c, dict) for c in commands)
        assert all("cmd" in c and "priority" in c for c in commands)

    def test_missing_priority_defaults_to_zero(self, tmp_path):
        """Commands without priority should default to 0."""
        project_config = tmp_path / "se3.yaml"
        project_config.write_text("claude_commands:\n  - cmd: claude\n")

        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(tmp_path)

        assert commands[0]["priority"] == 0

    def test_no_project_root_uses_global_only(self, tmp_path):
        """When project_root is None, should use global config only."""
        global_se3_dir = tmp_path / ".se3"
        global_se3_dir.mkdir()
        global_config = global_se3_dir / "config.yaml"
        global_config.write_text("claude_commands:\n  - cmd: global-only\n    priority: 1\n")

        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(None)

        assert commands[0]["cmd"] == "global-only"


# =============================================================================
# Usage Limit Detection
# =============================================================================

class TestDetectUsageLimit:
    """Test usage/rate limit detection from output."""

    @pytest.mark.parametrize("keyword", USAGE_LIMIT_KEYWORDS)
    def test_detects_keywords_in_stderr(self, keyword):
        assert ClaudeRunner.detect_usage_limit(1, "", f"Error: {keyword} exceeded") is True

    @pytest.mark.parametrize("keyword", USAGE_LIMIT_KEYWORDS)
    def test_detects_keywords_in_stdout(self, keyword):
        assert ClaudeRunner.detect_usage_limit(1, f"Sorry, {keyword} reached", "") is True

    def test_case_insensitive(self):
        assert ClaudeRunner.detect_usage_limit(1, "", "USAGE LIMIT reached") is True
        assert ClaudeRunner.detect_usage_limit(1, "Rate Limit Exceeded", "") is True

    def test_no_limit_normal_output(self):
        assert ClaudeRunner.detect_usage_limit(0, "Hello world", "") is False

    def test_no_limit_normal_error(self):
        assert ClaudeRunner.detect_usage_limit(1, "", "File not found") is False

    def test_exit_code_2_with_error_and_limit(self):
        assert ClaudeRunner.detect_usage_limit(2, "", "error: rate limit") is True

    def test_exit_code_2_without_keywords(self):
        # Exit code 2 without explicit rate limit keywords should NOT be usage limit
        assert ClaudeRunner.detect_usage_limit(2, "", "some other error") is False

    def test_none_stdout_stderr(self):
        assert ClaudeRunner.detect_usage_limit(0, None, None) is False


# =============================================================================
# Timeout Detection
# =============================================================================

class TestDetectTimeout:
    """Test timeout detection."""

    def test_exit_code_124_is_timeout(self):
        assert ClaudeRunner.detect_timeout(124) is True

    def test_other_exit_codes_not_timeout(self):
        assert ClaudeRunner.detect_timeout(0) is False
        assert ClaudeRunner.detect_timeout(1) is False
        assert ClaudeRunner.detect_timeout(2) is False


# =============================================================================
# ClaudeRunner.run() — Synchronous with Fallback
# =============================================================================

class TestClaudeRunnerRun:
    """Test synchronous run with fallback."""

    def test_success_on_first_command(self):
        runner = ClaudeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])

        mock_result = subprocess.CompletedProcess(
            args=["claude-a", "-p", "hi"], returncode=0,
            stdout="response", stderr=""
        )

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = runner.run(["-p", "hi"], timeout=30)

        assert result.returncode == 0
        assert result.stdout == "response"
        mock_run.assert_called_once()
        assert mock_run.call_args[0][0][0] == "claude-a"

    def test_fallback_on_usage_limit(self):
        runner = ClaudeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])

        limit_result = subprocess.CompletedProcess(
            args=["claude-a", "-p", "hi"], returncode=1,
            stdout="", stderr="Error: usage limit exceeded"
        )
        success_result = subprocess.CompletedProcess(
            args=["claude-b", "-p", "hi"], returncode=0,
            stdout="response", stderr=""
        )

        with patch("subprocess.run", side_effect=[limit_result, success_result]) as mock_run:
            result = runner.run(["-p", "hi"])

        assert result.returncode == 0
        assert result.stdout == "response"
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[0][0][0][0] == "claude-a"
        assert mock_run.call_args_list[1][0][0][0] == "claude-b"

    def test_fallback_on_timeout(self):
        runner = ClaudeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])

        success_result = subprocess.CompletedProcess(
            args=["claude-b", "-p", "hi"], returncode=0,
            stdout="response", stderr=""
        )

        with patch("subprocess.run", side_effect=[
            subprocess.TimeoutExpired(cmd="claude-a", timeout=30),
            success_result,
        ]) as mock_run:
            result = runner.run(["-p", "hi"], timeout=30)

        assert result.returncode == 0
        assert mock_run.call_count == 2

    def test_all_commands_exhausted(self):
        runner = ClaudeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])

        limit_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="rate limit"
        )

        with patch("subprocess.run", return_value=limit_result):
            result = runner.run(["-p", "hi"])

        # Returns last result even though all exhausted
        assert result.returncode == 1

    def test_non_limit_failure_returns_immediately(self):
        runner = ClaudeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])

        fail_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="file not found"
        )

        with patch("subprocess.run", return_value=fail_result) as mock_run:
            result = runner.run(["-p", "hi"])

        # Should NOT try claude-b since it's not a limit failure
        assert mock_run.call_count == 1
        assert result.returncode == 1

    def test_on_retry_callback(self):
        runner = ClaudeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])

        limit_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="usage limit"
        )
        success_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )

        new_args = ["-p", "retry prompt"]
        callback = MagicMock(return_value=new_args)

        with patch("subprocess.run", side_effect=[limit_result, success_result]) as mock_run:
            result = runner.run(["-p", "original"], on_retry=callback)

        callback.assert_called_once_with(1, "claude-a")
        # Second call should use new args (plain text prompts passed directly)
        second_call_args = mock_run.call_args_list[1][0][0]
        assert second_call_args[0] == "claude-b"
        assert "-p" in second_call_args
        assert "retry prompt" in second_call_args

    def test_on_retry_returns_none_keeps_original_args(self):
        runner = ClaudeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])

        limit_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="usage limit"
        )
        success_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )

        callback = MagicMock(return_value=None)

        with patch("subprocess.run", side_effect=[limit_result, success_result]) as mock_run:
            result = runner.run(["-p", "original"], on_retry=callback)

        # Should keep original args (plain text prompts passed directly)
        second_call_args = mock_run.call_args_list[1][0][0]
        assert second_call_args[0] == "claude-b"
        assert "-p" in second_call_args
        assert "original" in second_call_args


# =============================================================================
# ClaudeRunner.popen() — Async
# =============================================================================

class TestClaudeRunnerPopen:
    """Test async process spawning."""

    def test_popen_uses_first_command(self):
        runner = ClaudeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])

        mock_proc = MagicMock(spec=subprocess.Popen)

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            proc, idx = runner.popen(["-p", "hi"])

        assert idx == 0
        assert proc is mock_proc
        assert mock_popen.call_args[0][0][0] == "claude-a"

    def test_popen_with_cmd_index(self):
        runner = ClaudeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])

        mock_proc = MagicMock(spec=subprocess.Popen)

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            proc, idx = runner.popen(["-p", "hi"], cmd_index=1)

        assert idx == 1
        assert mock_popen.call_args[0][0][0] == "claude-b"


# =============================================================================
# ClaudeRunner.retry_with_next()
# =============================================================================

class TestRetryWithNext:
    """Test retry_with_next fallback."""

    def test_retry_succeeds(self):
        runner = ClaudeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])

        mock_proc = MagicMock(spec=subprocess.Popen)

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            result = runner.retry_with_next(0, ["-p", "hi"])

        assert result is not None
        proc, idx = result
        assert idx == 1
        assert mock_popen.call_args[0][0][0] == "claude-b"

    def test_retry_exhausted(self):
        runner = ClaudeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
        ])

        result = runner.retry_with_next(0, ["-p", "hi"])
        assert result is None


# =============================================================================
# Helper Methods
# =============================================================================

class TestHelperMethods:
    """Test get_command and get_next_command."""

    def test_get_command_default(self):
        runner = ClaudeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])
        assert runner.get_command() == "claude-a"
        assert runner.get_command(0) == "claude-a"
        assert runner.get_command(1) == "claude-b"

    def test_get_command_out_of_range(self):
        runner = ClaudeRunner(commands=[{"cmd": "claude-a", "priority": 10}])
        assert runner.get_command(99) == "claude-a"

    def test_get_next_command(self):
        runner = ClaudeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])
        assert runner.get_next_command("claude-a") == "claude-b"
        assert runner.get_next_command("claude-b") is None

    def test_get_next_command_not_found(self):
        runner = ClaudeRunner(commands=[{"cmd": "claude-a", "priority": 10}])
        assert runner.get_next_command("nonexistent") is None
