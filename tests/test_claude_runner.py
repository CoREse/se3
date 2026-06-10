"""Tests for ClaudeCodeRunner (claude_runner module).

Tests cover:
- Single-command execution (no fallback traversal)
- Usage limit detection (keywords, exit codes)
- Timeout detection
- detect_infra_error() composite method
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

from se3.config import load_claude_commands, load_claude_subprocess_config
from se3.claude_runner import (
    ClaudeCodeRunner,
    ClaudeRunner,
    USAGE_LIMIT_KEYWORDS,
    _MAX_ARG_BYTES,
)
from se3.agent_runner import AgentRunner, InfraErrorType


def _argv_after_skip_perms(argv):
    """Return the slice of argv immediately following ``--dangerously-skip-permissions``.

    Used by tests to assert the presence and position of ``--setting-sources``
    without hard-coding numeric indexes — the runner is free to insert other
    flags before/after this pair so long as the pair stays adjacent.
    """
    assert "--dangerously-skip-permissions" in argv, argv
    idx = argv.index("--dangerously-skip-permissions")
    return argv[idx + 1:]


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

class TestResolveArgsStdinPath:
    """``_resolve_args`` rewrites oversized ``-p <text>`` to stdin.

    This replaces the old ``-p @tmpfile`` fallback, which made Claude Code
    treat the prompt as a *referenced* file (bounded by the Read tool's
    25k-token ceiling) rather than as the actual user message.
    """

    def test_short_prompt_stays_in_argv(self, tmp_path):
        prompt = "short prompt"
        args, stdin_prompt = ClaudeCodeRunner._resolve_args(["-p", prompt], cwd=tmp_path)
        assert args == ["-p", prompt]
        assert stdin_prompt is None

    def test_large_prompt_routed_to_stdin(self, tmp_path):
        """Oversized prompt drops its argv value; stdin carries the payload."""
        prompt = "x" * (_MAX_ARG_BYTES + 1)
        args, stdin_prompt = ClaudeCodeRunner._resolve_args(["-p", prompt], cwd=tmp_path)
        assert args == ["-p"]
        assert stdin_prompt == prompt
        tmp_dir = tmp_path / "se3" / "tmp"
        if tmp_dir.exists():
            assert list(tmp_dir.glob("*.prompt")) == []

    def test_boundary_exact_threshold_stays_in_argv(self, tmp_path):
        prompt = "a" * _MAX_ARG_BYTES
        assert len(prompt.encode("utf-8")) == _MAX_ARG_BYTES
        args, stdin_prompt = ClaudeCodeRunner._resolve_args(["-p", prompt], cwd=tmp_path)
        assert args == ["-p", prompt]
        assert stdin_prompt is None

    def test_boundary_one_over_threshold_uses_stdin(self, tmp_path):
        prompt = "a" * (_MAX_ARG_BYTES + 1)
        args, stdin_prompt = ClaudeCodeRunner._resolve_args(["-p", prompt], cwd=tmp_path)
        assert args == ["-p"]
        assert stdin_prompt == prompt

    def test_multibyte_utf8_counted_correctly(self, tmp_path):
        char_count = (_MAX_ARG_BYTES // 3) + 1
        prompt = "一" * char_count
        assert len(prompt) < _MAX_ARG_BYTES
        assert len(prompt.encode("utf-8")) > _MAX_ARG_BYTES
        args, stdin_prompt = ClaudeCodeRunner._resolve_args(["-p", prompt], cwd=tmp_path)
        assert args == ["-p"]
        assert stdin_prompt == prompt

    def test_prompt_payload_preserved_byte_for_byte(self, tmp_path):
        prompt = "mixed: ascii + 中文 + émojis 🎉" * 5000
        assert len(prompt.encode("utf-8")) > _MAX_ARG_BYTES
        args, stdin_prompt = ClaudeCodeRunner._resolve_args(["-p", prompt], cwd=tmp_path)
        assert stdin_prompt == prompt

    def test_existing_at_file_syntax_passes_through(self, tmp_path):
        """``-p @file`` is Claude CLI's own file-reference syntax, left as-is."""
        prompt_file = tmp_path / "my_prompt.txt"
        prompt_file.write_text("hello from file", encoding="utf-8")
        args, stdin_prompt = ClaudeCodeRunner._resolve_args(
            ["-p", f"@{prompt_file}"], cwd=tmp_path,
        )
        assert args == ["-p", f"@{prompt_file}"]
        assert stdin_prompt is None

    def test_bare_at_arg_passes_through(self, tmp_path):
        args, stdin_prompt = ClaudeCodeRunner._resolve_args(
            ["@/some/path.txt"], cwd=tmp_path,
        )
        assert args == ["@/some/path.txt"]
        assert stdin_prompt is None

    def test_non_prompt_args_unaffected(self, tmp_path):
        long_arg = "x" * (_MAX_ARG_BYTES + 100)
        args, stdin_prompt = ClaudeCodeRunner._resolve_args(
            ["--model", "opus", "--verbose", long_arg], cwd=tmp_path,
        )
        assert args == ["--model", "opus", "--verbose", long_arg]
        assert stdin_prompt is None

    def test_multiple_oversized_prompts_last_wins_with_warning(self, tmp_path):
        """Pathological pattern — two oversized ``-p`` in one invocation.
        Last one wins (only one stdin stream), warning emitted."""
        p1 = "x" * (_MAX_ARG_BYTES + 1)
        p2 = "y" * (_MAX_ARG_BYTES + 1)
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            args, stdin_prompt = ClaudeCodeRunner._resolve_args(
                ["-p", p1, "-p", p2], cwd=tmp_path,
            )
        assert stdin_prompt == p2
        assert args == ["-p", "-p"]
        assert any("Multiple oversized -p" in str(w.message) for w in captured)


# =============================================================================
# Stdin lifecycle — end-to-end through run()/popen()
# =============================================================================

class TestStdinLifecycle:
    """End-to-end: an oversized ``-p`` prompt reaches the child via stdin."""

    def test_run_pipes_large_prompt_via_stdin(self, tmp_path):
        prompt = "x" * (_MAX_ARG_BYTES + 1)
        runner = ClaudeCodeRunner(command={"cmd": "echo", "priority": 0})
        captured_cmd: list = []
        captured_input = {}

        def mock_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            captured_input["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="ok", stderr="",
            )

        with patch("subprocess.run", side_effect=mock_run):
            result = runner.run(["-p", prompt], cwd=tmp_path)

        # Argv must not carry an @file tempfile reference.
        assert not any(a.startswith("@") for a in captured_cmd)
        # stdin carries the full prompt.
        assert captured_input["input"] == prompt
        assert result.returncode == 0
        tmp_dir = tmp_path / "se3" / "tmp"
        assert not tmp_dir.exists() or list(tmp_dir.glob("*.prompt")) == []

    def test_run_small_prompt_no_stdin_input(self, tmp_path):
        runner = ClaudeCodeRunner(command={"cmd": "echo", "priority": 0})
        captured_input = {}

        def mock_run(cmd, **kwargs):
            captured_input["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="ok", stderr="",
            )

        with patch("subprocess.run", side_effect=mock_run):
            runner.run(["-p", "small"], cwd=tmp_path)
        assert captured_input["input"] is None

    def test_run_large_prompt_timeout_no_tmp_files(self, tmp_path):
        prompt = "x" * (_MAX_ARG_BYTES + 1)
        runner = ClaudeCodeRunner(command={"cmd": "echo", "priority": 0})

        def mock_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd="echo", timeout=30)

        with patch("subprocess.run", side_effect=mock_run):
            result = runner.run(["-p", prompt], timeout=30, cwd=tmp_path)
        assert result.returncode == 124
        tmp_dir = tmp_path / "se3" / "tmp"
        assert not tmp_dir.exists() or list(tmp_dir.glob("*.prompt")) == []


# =============================================================================
# Setting Sources Isolation (--setting-sources)
# =============================================================================

class TestClaudeSubprocessSettingSources:
    """SE3-spawned Claude subprocesses MUST always pass ``--setting-sources``
    so a downstream project's ``.claude/settings.json`` ``permissions.deny``
    cannot lock the SE3 worker out of its own tools.

    Default is ``user``; explicit configuration via
    ``claude_subprocess.setting_sources`` in ``se3.yaml`` can opt back into
    project/local sources.  These tests cover the three argv-emission sites
    (``run``, ``popen``, ``run_with_monitor``) plus configuration loading.
    """

    def test_run_default_argv_contains_setting_sources_user(self):
        runner = ClaudeCodeRunner(command={"cmd": "claude-a", "priority": 10})
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner.run(["-p", "hi"])
        argv = mock_run.call_args[0][0]
        tail = _argv_after_skip_perms(argv)
        assert tail[0] == "--setting-sources"
        assert tail[1] == "user"

    def test_run_with_monitor_default_argv_contains_setting_sources_user(self, tmp_path):
        """``run_with_monitor`` builds argv before delegating to the internal
        monitor loop.  Patch ``_run_single_with_monitor`` so we can capture
        the constructed ``full_cmd`` without spinning up a real subprocess."""
        from se3.claude_runner import _SingleRunResult

        runner = ClaudeCodeRunner(command={"cmd": "claude-a", "priority": 10})
        captured = {}

        def fake_monitor(self, *, full_cmd, **_kwargs):
            captured["full_cmd"] = list(full_cmd)
            return _SingleRunResult(
                returncode=0, output="", success=True, should_retry=False,
            )

        with patch.object(
            ClaudeCodeRunner, "_run_single_with_monitor", autospec=True,
            side_effect=fake_monitor,
        ):
            runner.run_with_monitor(["-p", "hi"], cwd=tmp_path)

        argv = captured["full_cmd"]
        assert argv[0] == "claude-a"
        tail = _argv_after_skip_perms(argv)
        assert tail[0] == "--setting-sources"
        assert tail[1] == "user"

    def test_explicit_setting_sources_user_project(self):
        runner = ClaudeCodeRunner(
            command={"cmd": "claude-a", "priority": 10},
            setting_sources=["user", "project"],
        )
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner.run(["-p", "hi"])
        argv = mock_run.call_args[0][0]
        tail = _argv_after_skip_perms(argv)
        assert tail[0] == "--setting-sources"
        assert tail[1] == "user,project"

    def test_project_settings_json_does_not_leak_into_argv(self, tmp_path):
        """A target project's ``.claude/settings.json`` (with deny rules) must
        NOT influence the SE3 subprocess argv: the runner pulls its sources
        from ``se3.yaml``'s ``claude_subprocess.setting_sources``, not from
        the target project's Claude settings file.
        """
        # Simulate a downstream project that denies core tools for its own
        # verifier sub-LLMs.  SE3 must not honour these for its own children.
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            '{"permissions": {"deny": ["Read", "Write", "Edit", "Bash"]}}',
            encoding="utf-8",
        )
        # No se3.yaml present → defaults apply.
        with patch("se3.config.Path.home", return_value=tmp_path):
            runner = ClaudeCodeRunner(
                project_root=tmp_path,
                command={"cmd": "claude-a", "priority": 10},
            )
        assert runner.setting_sources == ["user"]

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner.run(["-p", "hi"], cwd=tmp_path)

        argv = mock_run.call_args[0][0]
        tail = _argv_after_skip_perms(argv)
        assert tail[0] == "--setting-sources"
        assert tail[1] == "user"
        # The target project's settings file must not appear anywhere in
        # the argv — SE3 never references it.
        assert not any("settings.json" in a for a in argv)

    def test_yaml_setting_sources_loaded_into_runner(self, tmp_path):
        """``claude_subprocess.setting_sources: [user, project]`` in
        ``se3.yaml`` is loaded by the Runner constructor when
        ``setting_sources`` isn't passed explicitly."""
        (tmp_path / "se3.yaml").write_text(
            "claude_subprocess:\n  setting_sources: [user, project]\n",
            encoding="utf-8",
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            runner = ClaudeCodeRunner(
                project_root=tmp_path,
                command={"cmd": "claude-a", "priority": 10},
            )
        assert runner.setting_sources == ["user", "project"]

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            runner.run(["-p", "hi"])
        argv = mock_run.call_args[0][0]
        tail = _argv_after_skip_perms(argv)
        assert tail[0] == "--setting-sources"
        assert tail[1] == "user,project"

    def test_empty_list_setting_sources_fails_fast(self, tmp_path):
        """``claude_subprocess.setting_sources: []`` is a config error —
        the loader raises rather than silently producing argv with an
        empty ``--setting-sources`` value."""
        (tmp_path / "se3.yaml").write_text(
            "claude_subprocess:\n  setting_sources: []\n",
            encoding="utf-8",
        )
        with patch("se3.config.Path.home", return_value=tmp_path):
            with pytest.raises(ValueError, match="setting_sources"):
                load_claude_subprocess_config(tmp_path)


# =============================================================================
# G1: Stderr Isolation — child stderr never mixed into stdout NDJSON
# =============================================================================

class TestStderrIsolation:
    """The child process's stderr MUST be kept separate from stdout so that
    NDJSON on stdout stays clean for downstream JSON parsers."""

    def test_child_stderr_pipe_not_merged_to_stdout(self, tmp_path):
        """_run_single_with_monitor uses stderr=PIPE, not stderr=STDOUT."""
        from se3.claude_runner import _SingleRunResult

        runner = ClaudeCodeRunner(command={"cmd": "claude-a", "priority": 10})
        captured_kwargs = {}

        def fake_monitor(self, *, full_cmd, **_kwargs):
            captured_kwargs.update(_kwargs)
            return _SingleRunResult(
                returncode=0, output="", success=True, should_retry=False,
            )

        with patch.object(
            ClaudeCodeRunner, "_run_single_with_monitor",
            side_effect=fake_monitor,
        ):
            runner.run_with_monitor(["-p", "hi"], cwd=tmp_path)

    def test_monitored_child_uses_separate_stderr(self):
        """_run_single_with_monitor passes stderr=PIPE, not STDOUT."""
        runner = ClaudeCodeRunner(command={"cmd": "claude-a", "priority": 10})
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # already exited
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read.return_value = ""
        mock_proc.stderr = MagicMock()

        captured_kwargs = {}
        def mock_popen(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return mock_proc

        with patch("subprocess.Popen", side_effect=mock_popen), \
             patch("shutil.which", return_value="/usr/bin/claude-a"), \
             patch("se3.claude_runner._spawn_stderr_reader", return_value=MagicMock()):
            try:
                runner._run_single_with_monitor(
                    full_cmd=["claude-a", "-p", "hi"],
                    cmd_name="claude-a", cmd_index=0,
                    log_file=None,
                    wall_timeout=None, inactivity_timeout=1800,
                    cwd=None, env={},
                    on_output=None, on_activity=None,
                    start_time=0,
                )
            except Exception:
                pass  # We only care about kwargs, not the result

        assert captured_kwargs.get("stderr") == subprocess.PIPE, (
            f"Expected stderr=PIPE, got {captured_kwargs.get('stderr')}"
        )

    def test_run_with_monitor_stdout_prefix_does_not_contain_stderr_messages(self, tmp_path):
        """run_with_monitor wraps output with '=== Command: ... ===' prefix.
        The [claude-runner] status messages go to sys.stderr (parent), not
        into output."""
        from se3.claude_runner import MonitoredResult

        runner = ClaudeCodeRunner(command={"cmd": "claude-a", "priority": 10})

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # already exited
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.read.return_value = '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}'
        mock_proc.returncode = 0
        mock_proc.stderr = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc), \
             patch("shutil.which", return_value="/usr/bin/claude-a"), \
             patch("se3.claude_runner._spawn_stderr_reader", return_value=MagicMock()), \
             patch("sys.stderr"):
            result = runner.run_with_monitor(["-p", "hi"], cwd=tmp_path)

        assert isinstance(result, MonitoredResult)
        # Output should start with the command prefix, not contain
        # [claude-runner] messages (those go to the parent's stderr).
        assert result.output.startswith("=== Command: claude-a ===")
        assert "[claude-runner] Running command" not in result.output
        assert "[claude-runner] Command" not in result.output


# =============================================================================
# build_call_args() — Intent-to-argv translation
# =============================================================================

class TestBuildCallArgs:
    """build_call_args translates intent into Claude Code CLI arguments.

    The output must be byte-for-byte identical to the old inline assembly
    in ``LLMCaller._call_with_retry`` (before the intent-passing refactor).
    """

    def test_basic_prompt(self):
        """Normal prompt → --output-format stream-json --verbose -p <prompt>."""
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_call_args(
            prompt="hello world",
            read_only=False,
        )
        assert args == [
            "--output-format", "stream-json",
            "--verbose",
            "-p", "hello world",
        ]

    def test_read_only_appends_disallowed_tools(self):
        """read_only=True → appends --disallowedTools for write tools."""
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_call_args(
            prompt="analyze this",
            read_only=True,
        )
        assert "--disallowedTools" in args
        di = args.index("--disallowedTools")
        disallowed = args[di + 1:]
        assert disallowed == ["Write", "Edit", "NotebookEdit", "AskUserQuestion"]

    def test_writable_step_no_disallowed_tools(self):
        """read_only=False → no --disallowedTools in args."""
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_call_args(
            prompt="implement this",
            read_only=False,
        )
        assert "--disallowedTools" not in args

    def test_context_files_appended(self, tmp_path):
        """Existing context files → --file <path> pairs."""
        f1 = tmp_path / "spec.md"
        f1.write_text("# Spec", encoding="utf-8")
        f2 = tmp_path / "notes.md"
        f2.write_text("# Notes", encoding="utf-8")
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_call_args(
            prompt="do it",
            read_only=False,
            context_files=[f1, f2],
        )
        assert "--file" in args
        fi = args.index("--file")
        assert args[fi + 1] == str(f1)
        assert args[fi + 2] == "--file"
        assert args[fi + 3] == str(f2)

    def test_nonexistent_context_file_skipped(self, tmp_path):
        """Context files that don't exist are silently skipped."""
        missing = tmp_path / "does_not_exist.md"
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_call_args(
            prompt="do it",
            read_only=False,
            context_files=[missing],
        )
        assert "--file" not in args

    def test_read_only_with_context_files(self, tmp_path):
        """Both read_only and context_files → all flags present."""
        f = tmp_path / "spec.md"
        f.write_text("# Spec", encoding="utf-8")
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_call_args(
            prompt="analyze",
            read_only=True,
            context_files=[f],
        )
        assert "--disallowedTools" in args
        assert "--file" in args
        # --file comes after --disallowedTools
        di = args.index("--disallowedTools")
        fi = args.index("--file")
        assert fi > di

    def test_no_context_files_none(self):
        """context_files=None → no --file in args."""
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_call_args(
            prompt="hi",
            read_only=False,
            context_files=None,
        )
        assert "--file" not in args

    def test_empty_context_files_list(self):
        """context_files=[] → no --file in args."""
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_call_args(
            prompt="hi",
            read_only=False,
            context_files=[],
        )
        assert "--file" not in args

    def test_prompt_preserved_verbatim(self):
        """Prompt with special chars is preserved verbatim in args."""
        prompt = "line1\nline2\ttab\"quotes'中文"
        runner = ClaudeCodeRunner(command={"cmd": "claude", "priority": 0})
        args = runner.build_call_args(prompt=prompt, read_only=False)
        p_idx = args.index("-p")
        assert args[p_idx + 1] == prompt
