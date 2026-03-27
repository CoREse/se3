"""Tests for ClaudeCodeRunner (claude_runner module).

Tests cover:
- Single-command execution (no fallback traversal)
- Usage limit detection (keywords, exit codes)
- Timeout detection
- detect_infra_error() composite method
- popen and retry_with_next (backward compat)
- ClaudeRunner alias
- Helper methods
"""

import subprocess
import warnings
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from se3.config import load_claude_commands
from se3.claude_runner import (
    ClaudeCodeRunner,
    ClaudeRunner,
    USAGE_LIMIT_KEYWORDS,
)
from se3.agent_runner import AgentRunner, InfraErrorType


# =============================================================================
# Config Loading (unchanged — tests load_claude_commands via delegation)
# =============================================================================

class TestLoadClaudeCommands:
    """Test loading and sorting claude commands."""

    def test_default_when_no_config(self, tmp_path):
        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(tmp_path)
        assert len(commands) == 1
        assert commands[0]["cmd"] == "claude"
        assert commands[0]["priority"] == 0

    def test_project_config_overrides(self, tmp_path):
        global_se3_dir = tmp_path / ".se3"
        global_se3_dir.mkdir()
        (global_se3_dir / "config.yaml").write_text("claude_commands:\n  - cmd: global-claude\n    priority: 10\n")
        (tmp_path / "se3.yaml").write_text("claude_commands:\n  - cmd: project-claude\n    priority: 5\n")
        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(tmp_path)
        assert len(commands) == 1
        assert commands[0]["cmd"] == "project-claude"

    def test_global_used_when_no_project_config(self, tmp_path):
        global_se3_dir = tmp_path / ".se3"
        global_se3_dir.mkdir()
        (global_se3_dir / "config.yaml").write_text("claude_commands:\n  - cmd: global-claude\n    priority: 10\n")
        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(tmp_path)
        assert commands[0]["cmd"] == "global-claude"

    def test_priority_sorting(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("""claude_commands:
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
        (tmp_path / "se3.yaml").write_text("claude_commands:\n  - claude\n  - kclaude\n")
        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(tmp_path)
        assert len(commands) == 2
        assert all(isinstance(c, dict) for c in commands)
        assert all("cmd" in c and "priority" in c for c in commands)

    def test_missing_priority_defaults_to_zero(self, tmp_path):
        (tmp_path / "se3.yaml").write_text("claude_commands:\n  - cmd: claude\n")
        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(tmp_path)
        assert commands[0]["priority"] == 0

    def test_no_project_root_uses_global_only(self, tmp_path):
        global_se3_dir = tmp_path / ".se3"
        global_se3_dir.mkdir()
        (global_se3_dir / "config.yaml").write_text("claude_commands:\n  - cmd: global-only\n    priority: 1\n")
        with patch("se3.config.Path.home", return_value=tmp_path):
            commands = load_claude_commands(None)
        assert commands[0]["cmd"] == "global-only"


# =============================================================================
# ClaudeCodeRunner identity & alias
# =============================================================================

class TestClaudeCodeRunnerIdentity:
    """Test class identity and AgentRunner inheritance."""

    def test_is_agent_runner(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        assert isinstance(runner, AgentRunner)

    def test_alias_works(self):
        assert ClaudeRunner is ClaudeCodeRunner

    def test_construct_with_command(self):
        runner = ClaudeCodeRunner(command={"cmd": "my-claude", "priority": 5})
        assert runner.command["cmd"] == "my-claude"

    def test_construct_with_commands_takes_first(self):
        runner = ClaudeCodeRunner(commands=[
            {"cmd": "first", "priority": 10},
            {"cmd": "second", "priority": 5},
        ])
        assert runner.command["cmd"] == "first"

    def test_construct_with_no_args_loads_default(self, tmp_path):
        with patch("se3.config.Path.home", return_value=tmp_path):
            runner = ClaudeCodeRunner(project_root=tmp_path)
        assert runner.command["cmd"] == "claude"


# =============================================================================
# Usage Limit Detection
# =============================================================================

class TestDetectUsageLimit:
    """Test usage/rate limit detection from output."""

    @pytest.mark.parametrize("keyword", USAGE_LIMIT_KEYWORDS)
    def test_detects_keywords_in_stderr(self, keyword):
        assert ClaudeCodeRunner.detect_usage_limit(1, "", f"Error: {keyword} exceeded") is True

    @pytest.mark.parametrize("keyword", USAGE_LIMIT_KEYWORDS)
    def test_detects_keywords_in_stdout(self, keyword):
        assert ClaudeCodeRunner.detect_usage_limit(1, f"Sorry, {keyword} reached", "") is True

    def test_case_insensitive(self):
        assert ClaudeCodeRunner.detect_usage_limit(1, "", "USAGE LIMIT reached") is True
        assert ClaudeCodeRunner.detect_usage_limit(1, "Rate Limit Exceeded", "") is True

    def test_no_limit_normal_output(self):
        assert ClaudeCodeRunner.detect_usage_limit(0, "Hello world", "") is False

    def test_no_limit_normal_error(self):
        assert ClaudeCodeRunner.detect_usage_limit(1, "", "File not found") is False

    def test_exit_code_2_with_error_and_limit(self):
        assert ClaudeCodeRunner.detect_usage_limit(2, "", "error: rate limit") is True

    def test_exit_code_2_without_keywords(self):
        assert ClaudeCodeRunner.detect_usage_limit(2, "", "some other error") is False

    def test_none_stdout_stderr(self):
        assert ClaudeCodeRunner.detect_usage_limit(0, None, None) is False


# =============================================================================
# Timeout Detection
# =============================================================================

class TestDetectTimeout:
    """Test timeout detection."""

    def test_exit_code_124_is_timeout(self):
        assert ClaudeCodeRunner.detect_timeout(124) is True

    def test_other_exit_codes_not_timeout(self):
        assert ClaudeCodeRunner.detect_timeout(0) is False
        assert ClaudeCodeRunner.detect_timeout(1) is False
        assert ClaudeCodeRunner.detect_timeout(2) is False


# =============================================================================
# detect_infra_error()
# =============================================================================

class TestDetectInfraError:
    """Test composite infrastructure error detection."""

    def test_usage_limit(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        result = runner.detect_infra_error(1, "", "usage limit exceeded")
        assert result == InfraErrorType.USAGE_LIMIT

    def test_timeout(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        result = runner.detect_infra_error(124, "", "")
        assert result == InfraErrorType.TIMEOUT

    def test_none_on_success(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        result = runner.detect_infra_error(0, "ok", "")
        assert result == InfraErrorType.NONE

    def test_none_on_task_failure(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        result = runner.detect_infra_error(1, "", "file not found")
        assert result == InfraErrorType.NONE


# =============================================================================
# ClaudeCodeRunner.run() — Single command, no fallback
# =============================================================================

class TestClaudeCodeRunnerRun:
    """Test synchronous run (single command)."""

    def test_success(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude-a", "priority": 10})
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

    def test_failure_returns_directly(self):
        """Non-limit failure returns immediately (no fallback)."""
        runner = ClaudeCodeRunner(command={"cmd": "claude-a", "priority": 10})
        fail_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="file not found"
        )
        with patch("subprocess.run", return_value=fail_result) as mock_run:
            result = runner.run(["-p", "hi"])
        assert mock_run.call_count == 1
        assert result.returncode == 1

    def test_timeout_returns_124(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude-a", "priority": 10})
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude-a", timeout=30)):
            result = runner.run(["-p", "hi"], timeout=30)
        assert result.returncode == 124

    def test_usage_limit_returns_result(self):
        """Usage limit is returned (not retried — that's LLMCaller's job)."""
        runner = ClaudeCodeRunner(command={"cmd": "claude-a", "priority": 10})
        limit_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="usage limit exceeded"
        )
        with patch("subprocess.run", return_value=limit_result) as mock_run:
            result = runner.run(["-p", "hi"])
        assert mock_run.call_count == 1
        assert result.returncode == 1


# =============================================================================
# ClaudeCodeRunner.popen() — Async (backward compat)
# =============================================================================

class TestClaudeCodeRunnerPopen:
    """Test async process spawning."""

    def test_popen_uses_command(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude-a", "priority": 10})
        mock_proc = MagicMock(spec=subprocess.Popen)
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            proc, idx = runner.popen(["-p", "hi"])
        assert idx == 0
        assert proc is mock_proc
        assert mock_popen.call_args[0][0][0] == "claude-a"

    def test_popen_with_cmd_index(self):
        """cmd_index still works for backward compat with commands list."""
        runner = ClaudeCodeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])
        mock_proc = MagicMock(spec=subprocess.Popen)
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            proc, idx = runner.popen(["-p", "hi"], cmd_index=1)
        assert idx == 1
        assert mock_popen.call_args[0][0][0] == "claude-b"


# =============================================================================
# ClaudeCodeRunner.retry_with_next() — Deprecated
# =============================================================================

class TestRetryWithNext:
    """Test retry_with_next (deprecated, kept for collab compat)."""

    def test_retry_emits_deprecation_warning(self):
        runner = ClaudeCodeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])
        mock_proc = MagicMock(spec=subprocess.Popen)
        with patch("subprocess.Popen", return_value=mock_proc):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                runner.retry_with_next(0, ["-p", "hi"])
                assert len(w) == 1
                assert issubclass(w[0].category, DeprecationWarning)

    def test_retry_exhausted(self):
        runner = ClaudeCodeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
        ])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = runner.retry_with_next(0, ["-p", "hi"])
        assert result is None


# =============================================================================
# Helper Methods
# =============================================================================

class TestHelperMethods:
    """Test get_command and get_next_command."""

    def test_get_command_default(self):
        runner = ClaudeCodeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])
        assert runner.get_command() == "claude-a"
        assert runner.get_command(0) == "claude-a"
        assert runner.get_command(1) == "claude-b"

    def test_get_command_out_of_range(self):
        runner = ClaudeCodeRunner(commands=[{"cmd": "claude-a", "priority": 10}])
        assert runner.get_command(99) == "claude-a"

    def test_get_next_command(self):
        runner = ClaudeCodeRunner(commands=[
            {"cmd": "claude-a", "priority": 10},
            {"cmd": "claude-b", "priority": 5},
        ])
        assert runner.get_next_command("claude-a") == "claude-b"
        assert runner.get_next_command("claude-b") is None

    def test_get_next_command_not_found(self):
        runner = ClaudeCodeRunner(commands=[{"cmd": "claude-a", "priority": 10}])
        assert runner.get_next_command("nonexistent") is None
