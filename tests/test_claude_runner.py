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
    _MAX_ARG_BYTES,
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


# =============================================================================
# _resolve_args() — Auto-file large prompts
# =============================================================================

class TestResolveArgsAutoFile:
    """Test automatic filing of large prompt arguments in _resolve_args()."""

    def test_short_prompt_passes_directly(self, tmp_path):
        """Prompts below the threshold are passed as-is."""
        prompt = "short prompt"
        result = ClaudeCodeRunner._resolve_args(["-p", prompt], cwd=tmp_path)
        assert result == ["-p", prompt]

    def test_large_prompt_auto_filed(self, tmp_path):
        """Prompts exceeding _MAX_ARG_BYTES are written to a temp file."""
        # Use ASCII so 1 char == 1 byte
        prompt = "x" * (_MAX_ARG_BYTES + 1)
        result = ClaudeCodeRunner._resolve_args(["-p", prompt], cwd=tmp_path)

        assert result[0] == "-p"
        assert result[1].startswith("@")
        temp_path = Path(result[1][1:])
        assert temp_path.exists()
        assert temp_path.suffix == ".prompt"
        assert temp_path.parent == tmp_path / "se3" / "tmp"
        assert temp_path.read_text(encoding="utf-8") == prompt
        temp_path.unlink()

    def test_boundary_exact_threshold_passes_directly(self, tmp_path):
        """A prompt whose byte length == _MAX_ARG_BYTES is NOT filed."""
        prompt = "a" * _MAX_ARG_BYTES
        assert len(prompt.encode("utf-8")) == _MAX_ARG_BYTES
        result = ClaudeCodeRunner._resolve_args(["-p", prompt], cwd=tmp_path)
        assert result == ["-p", prompt]

    def test_boundary_one_over_threshold_is_filed(self, tmp_path):
        """A prompt whose byte length == _MAX_ARG_BYTES + 1 IS filed."""
        prompt = "a" * (_MAX_ARG_BYTES + 1)
        result = ClaudeCodeRunner._resolve_args(["-p", prompt], cwd=tmp_path)
        assert result[1].startswith("@")
        Path(result[1][1:]).unlink()

    def test_multibyte_utf8_counted_correctly(self, tmp_path):
        """Multi-byte chars push byte length past the threshold even when
        char count is below it."""
        # U+4E00 (一) is 3 bytes in UTF-8
        char_count = (_MAX_ARG_BYTES // 3) + 1
        prompt = "\u4e00" * char_count
        assert len(prompt) < _MAX_ARG_BYTES  # char count below threshold
        assert len(prompt.encode("utf-8")) > _MAX_ARG_BYTES  # byte count above
        result = ClaudeCodeRunner._resolve_args(["-p", prompt], cwd=tmp_path)
        assert result[1].startswith("@")
        temp_path = Path(result[1][1:])
        assert temp_path.read_text(encoding="utf-8") == prompt
        temp_path.unlink()

    def test_existing_at_file_syntax_unchanged(self, tmp_path):
        """Existing @file prompt syntax still works and is unaffected."""
        prompt_file = tmp_path / "my_prompt.txt"
        prompt_file.write_text("hello from file", encoding="utf-8")
        result = ClaudeCodeRunner._resolve_args(
            ["-p", f"@{prompt_file}"], cwd=tmp_path,
        )
        assert result[0] == "-p"
        # Should have been re-written to a temp file via the @file branch
        assert result[1].startswith("@")
        temp_path = Path(result[1][1:])
        assert temp_path.read_text(encoding="utf-8") == "hello from file"
        temp_path.unlink()

    def test_temp_file_content_matches_original(self, tmp_path):
        """The temp file contains the exact original prompt, byte-for-byte."""
        prompt = "mixed: ascii + 中文 + émojis 🎉" * 5000
        assert len(prompt.encode("utf-8")) > _MAX_ARG_BYTES
        result = ClaudeCodeRunner._resolve_args(["-p", prompt], cwd=tmp_path)
        temp_path = Path(result[1][1:])
        assert temp_path.read_text(encoding="utf-8") == prompt
        temp_path.unlink()

    def test_non_prompt_args_unaffected(self, tmp_path):
        """Other arguments are passed through unchanged regardless of length."""
        long_arg = "x" * (_MAX_ARG_BYTES + 100)
        result = ClaudeCodeRunner._resolve_args(
            ["--model", "opus", "--verbose", long_arg], cwd=tmp_path,
        )
        assert result == ["--model", "opus", "--verbose", long_arg]

    def test_write_failure_cleans_up_temp_file(self, tmp_path):
        """If writing to the temp file fails, the orphan file is removed."""
        prompt = "x" * (_MAX_ARG_BYTES + 1)
        with patch("tempfile.NamedTemporaryFile") as mock_ntf:
            mock_file = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            # Create a real temp file path so unlink can be verified
            real_tmp = tmp_path / "se3" / "tmp" / "fake.prompt"
            real_tmp.parent.mkdir(parents=True, exist_ok=True)
            real_tmp.write_text("placeholder")
            mock_file.name = str(real_tmp)
            mock_file.write.side_effect = OSError("disk full")
            mock_ntf.return_value = mock_file

            with pytest.raises(OSError, match="disk full"):
                ClaudeCodeRunner._resolve_args(["-p", prompt], cwd=tmp_path)

            # Orphan temp file should have been cleaned up
            assert not real_tmp.exists()


# =============================================================================
# Auto-file lifecycle — end-to-end through run()/popen()
# =============================================================================

class TestAutoFileLifecycle:
    """End-to-end tests for temp file lifecycle through run() and popen()."""

    def test_run_passes_at_file_and_cleans_up(self, tmp_path):
        """run() passes @file arg to subprocess and cleans up temp file after."""
        prompt = "x" * (_MAX_ARG_BYTES + 1)
        runner = ClaudeCodeRunner(command={"cmd": "echo", "priority": 0})

        captured_cmd = []

        def mock_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="ok", stderr="",
            )

        with patch("subprocess.run", side_effect=mock_run):
            result = runner.run(["-p", prompt], cwd=tmp_path)

        # Subprocess should have received an @file arg
        at_args = [a for a in captured_cmd if a.startswith("@")]
        assert len(at_args) == 1
        temp_path = Path(at_args[0][1:])

        # Temp file should be cleaned up after run() returns
        assert not temp_path.exists()
        assert result.returncode == 0

    def test_run_cleans_up_on_subprocess_failure(self, tmp_path):
        """run() cleans up temp files even when subprocess fails."""
        prompt = "x" * (_MAX_ARG_BYTES + 1)
        runner = ClaudeCodeRunner(command={"cmd": "echo", "priority": 0})

        captured_cmd = []

        def mock_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="error",
            )

        with patch("subprocess.run", side_effect=mock_run):
            runner.run(["-p", prompt], cwd=tmp_path)

        at_args = [a for a in captured_cmd if a.startswith("@")]
        assert len(at_args) == 1
        assert not Path(at_args[0][1:]).exists()

    def test_run_cleans_up_on_timeout(self, tmp_path):
        """run() cleans up temp files on timeout."""
        prompt = "x" * (_MAX_ARG_BYTES + 1)
        runner = ClaudeCodeRunner(command={"cmd": "echo", "priority": 0})

        captured_cmd = []

        def mock_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            raise subprocess.TimeoutExpired(cmd="echo", timeout=30)

        with patch("subprocess.run", side_effect=mock_run):
            result = runner.run(["-p", prompt], timeout=30, cwd=tmp_path)

        at_args = [a for a in captured_cmd if a.startswith("@")]
        assert len(at_args) == 1
        assert not Path(at_args[0][1:]).exists()
        assert result.returncode == 124

    def test_popen_attaches_temp_files_for_caller_cleanup(self, tmp_path):
        """popen() attaches temp files to the process for caller cleanup."""
        prompt = "x" * (_MAX_ARG_BYTES + 1)
        runner = ClaudeCodeRunner(command={"cmd": "echo", "priority": 0})

        mock_proc = MagicMock(spec=subprocess.Popen)
        with patch("subprocess.Popen", return_value=mock_proc):
            proc, idx = runner.popen(["-p", prompt], cwd=tmp_path)

        assert hasattr(proc, "_se3_temp_files")
        assert len(proc._se3_temp_files) == 1
        temp_path = proc._se3_temp_files[0]
        assert temp_path.exists()
        assert temp_path.suffix == ".prompt"

        # Simulate caller cleanup
        for tf in proc._se3_temp_files:
            tf.unlink(missing_ok=True)
        assert not temp_path.exists()

    def test_popen_cleans_up_on_popen_failure(self, tmp_path):
        """popen() cleans up temp files if Popen creation fails."""
        prompt = "x" * (_MAX_ARG_BYTES + 1)
        runner = ClaudeCodeRunner(command={"cmd": "echo", "priority": 0})

        with patch("subprocess.Popen", side_effect=OSError("spawn failed")):
            with pytest.raises(OSError, match="spawn failed"):
                runner.popen(["-p", prompt], cwd=tmp_path)

        # No orphan temp files should remain
        tmp_dir = tmp_path / "se3" / "tmp"
        remaining = list(tmp_dir.glob("*.prompt"))
        assert len(remaining) == 0
