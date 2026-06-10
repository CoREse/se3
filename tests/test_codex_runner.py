"""Tests for CodexRunner (codex_runner module).

Tests cover:
- CodexEventConverter: event mapping, unknown event tolerance, finalize
- CodexRunner.build_call_args: sandbox flags, context inlining, stdin routing
- CodexRunner.detect_infra_error: success, usage limit, auth failure, timeout
- CodexRunner.run / run_with_monitor: subprocess lifecycle (via mocks)
- Registration via LLMCaller._create_runner
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from se3.agent_runner import AgentRunner, InfraErrorType
from se3.codex_runner import (
    CodexEventConverter,
    CodexRunner,
    MonitoredResult,
    _MAX_ARG_BYTES,
)


# =============================================================================
# CodexEventConverter — event mapping
# =============================================================================

class TestCodexEventConverterMapping:
    """Test that codex JSONL events are converted to Claude NDJSON."""

    def test_thread_started_returns_empty(self):
        conv = CodexEventConverter()
        result = conv.convert_line(json.dumps({"type": "thread.started"}))
        assert result == []

    def test_turn_started_returns_empty(self):
        conv = CodexEventConverter()
        result = conv.convert_line(json.dumps({"type": "turn.started"}))
        assert result == []

    def test_agent_message_produces_assistant_event(self):
        conv = CodexEventConverter()
        event = {
            "type": "item.updated",
            "data": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello world"}],
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 1
        parsed = json.loads(result[0])
        assert parsed["type"] == "assistant"
        assert parsed["message"]["content"][0]["type"] == "text"
        assert parsed["message"]["content"][0]["text"] == "Hello world"

    def test_function_call_produces_tool_use(self):
        conv = CodexEventConverter()
        event = {
            "type": "item.updated",
            "data": {
                "type": "function_call",
                "name": "shell",
                "call_id": "call_123",
                "arguments": json.dumps({"command": "ls -la"}),
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 1
        parsed = json.loads(result[0])
        assert parsed["type"] == "assistant"
        content = parsed["message"]["content"][0]
        assert content["type"] == "tool_use"
        assert content["name"] == "Bash"  # mapped from "shell"
        assert content["id"] == "call_123"
        assert content["input"]["command"] == "ls -la"

    def test_function_call_output_produces_tool_result(self):
        conv = CodexEventConverter()
        event = {
            "type": "item.updated",
            "data": {
                "type": "function_call_output",
                "call_id": "call_123",
                "output": "file1.txt\nfile2.txt",
                "is_error": False,
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 1
        parsed = json.loads(result[0])
        assert parsed["type"] == "user"
        content = parsed["message"]["content"][0]
        assert content["type"] == "tool_result"
        assert content["tool_use_id"] == "call_123"
        assert content["content"] == "file1.txt\nfile2.txt"
        assert content["is_error"] is False

    def test_function_call_output_error(self):
        conv = CodexEventConverter()
        event = {
            "type": "item.updated",
            "data": {
                "type": "function_call_output",
                "call_id": "call_456",
                "output": "Permission denied",
                "is_error": True,
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["message"]["content"][0]["is_error"] is True

    def test_turn_completed_produces_result_with_usage(self):
        conv = CodexEventConverter()
        # First send an agent message to accumulate
        msg_event = {
            "type": "item.updated",
            "data": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done!"}],
            },
        }
        conv.convert_line(json.dumps(msg_event))

        # Then turn.completed with usage
        event = {
            "type": "turn.completed",
            "data": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_input_tokens": 20,
                },
                "total_cost_usd": 0.001,
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 1
        parsed = json.loads(result[0])
        assert parsed["type"] == "result"
        assert parsed["result"] == "Done!"
        assert parsed["usage"]["input_tokens"] == 100
        assert parsed["usage"]["output_tokens"] == 50
        assert parsed["usage"]["cache_read_input_tokens"] == 20
        assert parsed["usage"]["cache_creation_input_tokens"] == 0
        assert parsed["total_cost_usd"] == 0.001

    def test_turn_completed_missing_usage_defaults_to_zero(self):
        conv = CodexEventConverter()
        event = {"type": "turn.completed", "data": {}}
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["usage"]["input_tokens"] == 0
        assert parsed["usage"]["output_tokens"] == 0
        assert parsed["usage"]["cache_creation_input_tokens"] == 0
        assert parsed["usage"]["cache_read_input_tokens"] == 0

    def test_turn_failed_produces_error_result(self):
        conv = CodexEventConverter()
        event = {
            "type": "turn.failed",
            "data": {"error": {"message": "Model overloaded"}},
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 1
        parsed = json.loads(result[0])
        assert parsed["type"] == "result"
        assert parsed["subtype"] == "error"
        assert parsed["is_error"] is True
        assert "Model overloaded" in parsed["result"]

    def test_error_event_produces_error_result(self):
        conv = CodexEventConverter()
        event = {
            "type": "error",
            "data": {"message": "Something went wrong"},
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 1
        parsed = json.loads(result[0])
        assert parsed["type"] == "result"
        assert parsed["is_error"] is True

    def test_unknown_event_type_returns_empty_no_crash(self):
        conv = CodexEventConverter()
        event = {"type": "some.future.event.v2", "data": {"x": 1}}
        result = conv.convert_line(json.dumps(event))
        assert result == []

    def test_non_json_line_returns_empty_no_crash(self):
        conv = CodexEventConverter()
        result = conv.convert_line("this is not json at all")
        assert result == []

    def test_empty_line_returns_empty(self):
        conv = CodexEventConverter()
        result = conv.convert_line("")
        assert result == []

    def test_file_change_maps_to_write_tool(self):
        """file_change events should map to Write tool_use."""
        conv = CodexEventConverter()
        event = {
            "type": "item.updated",
            "data": {
                "type": "function_call",
                "name": "write_file",
                "call_id": "call_fc",
                "arguments": json.dumps({"file_path": "/tmp/test.py", "content": "x=1"}),
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["message"]["content"][0]["name"] == "Write"

    def test_mcp_tool_call_preserves_name(self):
        """MCP tool calls keep their original name."""
        conv = CodexEventConverter()
        event = {
            "type": "item.updated",
            "data": {
                "type": "function_call",
                "name": "mcp_my_server__search",
                "call_id": "call_mcp",
                "arguments": json.dumps({"query": "test"}),
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["message"]["content"][0]["name"] == "mcp_my_server__search"

    def test_tool_name_mapping_shell_to_bash(self):
        conv = CodexEventConverter()
        assert conv._map_tool_name("shell") == "Bash"
        assert conv._map_tool_name("bash") == "Bash"
        assert conv._map_tool_name("apply_patch") == "Edit"
        assert conv._map_tool_name("write_file") == "Write"
        assert conv._map_tool_name("read_file") == "Read"
        assert conv._map_tool_name("unknown_tool") == "unknown_tool"

    def test_function_call_arguments_as_dict(self):
        """Arguments may already be a dict (not a JSON string)."""
        conv = CodexEventConverter()
        event = {
            "type": "item.updated",
            "data": {
                "type": "function_call",
                "name": "shell",
                "call_id": "call_dict",
                "arguments": {"command": "echo hi"},
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["message"]["content"][0]["input"]["command"] == "echo hi"

    def test_all_output_lines_are_valid_json(self):
        """Every line from convert_line must be valid JSON."""
        conv = CodexEventConverter()
        events = [
            {"type": "thread.started", "data": {}},
            {"type": "turn.started", "data": {}},
            {"type": "item.updated", "data": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}},
            {"type": "item.updated", "data": {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": "{}"}},
            {"type": "item.updated", "data": {"type": "function_call_output", "call_id": "c1", "output": "ok"}},
            {"type": "turn.completed", "data": {"usage": {}}},
        ]
        all_lines = []
        for ev in events:
            all_lines.extend(conv.convert_line(json.dumps(ev)))
        assert len(all_lines) > 0
        for line in all_lines:
            parsed = json.loads(line)
            assert parsed["type"] in ("assistant", "user", "result")


# =============================================================================
# CodexEventConverter.finalize()
# =============================================================================

class TestCodexEventConverterFinalize:
    """Test the finalize method — trailing event synthesis."""

    def test_finalize_after_turn_completed_returns_empty(self):
        conv = CodexEventConverter()
        conv.convert_line(json.dumps({"type": "turn.completed", "data": {}}))
        assert conv.finalize() == []

    def test_finalize_after_turn_failed_returns_empty(self):
        conv = CodexEventConverter()
        conv.convert_line(json.dumps({"type": "turn.failed", "data": {"error": "x"}}))
        assert conv.finalize() == []

    def test_finalize_with_accumulated_messages_synthesizes_result(self):
        conv = CodexEventConverter()
        conv.convert_line(json.dumps({
            "type": "item.updated",
            "data": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "partial"}]},
        }))
        result = conv.finalize()
        assert len(result) == 1
        parsed = json.loads(result[0])
        assert parsed["type"] == "result"
        assert parsed["result"] == "partial"

    def test_finalize_no_output_synthesizes_error(self):
        conv = CodexEventConverter()
        result = conv.finalize()
        assert len(result) == 1
        parsed = json.loads(result[0])
        assert parsed["type"] == "result"
        assert parsed["is_error"] is True
        assert "without producing output" in parsed["result"]

    def test_finalize_idempotent(self):
        conv = CodexEventConverter()
        conv.finalize()
        # Second call should also return empty (terminal already seen)
        assert conv.finalize() == []


# =============================================================================
# CodexRunner identity
# =============================================================================

class TestCodexRunnerIdentity:
    """Test class identity and AgentRunner inheritance."""

    def test_is_agent_runner(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        assert isinstance(runner, AgentRunner)

    def test_construct_with_command(self):
        runner = CodexRunner(command={"cmd": "my-codex", "priority": 5})
        assert runner.command["cmd"] == "my-codex"
        assert runner.command["priority"] == 5

    def test_construct_with_no_args_uses_default(self):
        runner = CodexRunner()
        assert runner.command["cmd"] == "codex"
        assert runner.command["priority"] == 0

    def test_construct_with_project_root(self):
        runner = CodexRunner(project_root=Path("/some/path"))
        assert runner.command["cmd"] == "codex"


# =============================================================================
# CodexRunner.build_call_args
# =============================================================================

class TestCodexBuildCallArgs:
    """build_call_args translates intent into codex exec CLI arguments."""

    def test_basic_prompt_contains_exec_json(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="hello", read_only=False)
        assert "exec" in args
        assert "--json" in args
        assert "--skip-git-repo-check" in args
        assert "-a" in args
        assert "never" in args

    def test_writable_step_has_bypass_flag(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="do it", read_only=False)
        assert "--dangerously-bypass-approvals-and-sandbox" in args
        assert "--sandbox" not in args

    def test_read_only_step_has_sandbox_flag(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="analyze", read_only=True)
        assert "--sandbox" in args
        si = args.index("--sandbox")
        assert args[si + 1] == "read-only"
        assert "--dangerously-bypass-approvals-and-sandbox" not in args

    def test_prompt_as_positional_arg(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="hello world", read_only=False)
        # Prompt should be the last argument
        assert args[-1] == "hello world"

    def test_context_files_inlined_into_prompt(self, tmp_path):
        f = tmp_path / "spec.md"
        f.write_text("# My Spec\nSome content", encoding="utf-8")
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(
            prompt="analyze this",
            read_only=False,
            context_files=[f],
        )
        # Prompt (last arg) should contain inlined file content
        final_prompt = args[-1]
        assert "## File:" in final_prompt
        assert "# My Spec" in final_prompt
        assert "analyze this" in final_prompt
        # No --file flag
        assert "--file" not in args

    def test_context_files_nonexistent_skipped(self, tmp_path):
        missing = tmp_path / "missing.md"
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(
            prompt="analyze",
            read_only=False,
            context_files=[missing],
        )
        assert "## File:" not in args[-1]

    def test_context_files_none(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="hi", read_only=False, context_files=None)
        assert args[-1] == "hi"

    def test_no_file_flag_ever(self):
        """Codex has no --file equivalent; never emit it."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="hi", read_only=False)
        assert "--file" not in args

    def test_oversized_prompt_uses_stdin_marker(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        big_prompt = "x" * (_MAX_ARG_BYTES + 1)
        args = runner.build_call_args(prompt=big_prompt, read_only=False)
        assert args[-1] == "-"
        assert runner._pending_stdin_prompt == big_prompt

    def test_small_prompt_no_stdin_marker(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="small", read_only=False)
        assert args[-1] == "small"
        assert runner._pending_stdin_prompt is None

    def test_read_only_with_context_files(self, tmp_path):
        f = tmp_path / "spec.md"
        f.write_text("content", encoding="utf-8")
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(
            prompt="analyze",
            read_only=True,
            context_files=[f],
        )
        assert "--sandbox" in args
        assert "## File:" in args[-1]


# =============================================================================
# CodexRunner.detect_infra_error
# =============================================================================

class TestCodexDetectInfraError:
    """Test infrastructure error classification."""

    def test_success_returns_none(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        assert runner.detect_infra_error(0, "ok", "") == InfraErrorType.NONE

    def test_success_with_keywords_still_none(self):
        """Success (returncode 0) must not trigger false positives even if
        the output contains usage limit keywords (e.g. prompt echo)."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        assert runner.detect_infra_error(0, "usage limit exceeded", "") == InfraErrorType.NONE

    def test_usage_limit_detected(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        assert runner.detect_infra_error(1, "", "Error: usage limit exceeded") == InfraErrorType.USAGE_LIMIT

    def test_rate_limit_detected(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        assert runner.detect_infra_error(1, "rate limit hit", "") == InfraErrorType.USAGE_LIMIT

    def test_429_detected(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        assert runner.detect_infra_error(1, "HTTP 429 Too Many Requests", "") == InfraErrorType.USAGE_LIMIT

    def test_quota_exceeded_detected(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        assert runner.detect_infra_error(1, "quota exceeded", "") == InfraErrorType.USAGE_LIMIT

    def test_auth_failure_detected_as_usage_limit(self):
        """Auth failures (401/unauthorized) are credential-level rotation
        triggers, mapped to USAGE_LIMIT."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        assert runner.detect_infra_error(1, "", "401 Unauthorized") == InfraErrorType.USAGE_LIMIT

    def test_auth_failed_detected(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        assert runner.detect_infra_error(1, "authentication failed", "") == InfraErrorType.USAGE_LIMIT

    def test_timeout_detected(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        assert runner.detect_infra_error(124, "", "") == InfraErrorType.TIMEOUT

    def test_task_failure_returns_none(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        assert runner.detect_infra_error(1, "file not found", "") == InfraErrorType.NONE

    def test_case_insensitive(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        assert runner.detect_infra_error(1, "USAGE LIMIT reached", "") == InfraErrorType.USAGE_LIMIT

    def test_scan_only_tail(self):
        """Keywords in early output (prompt echo) should not trigger false
        positives — only the last 3000 chars / 20 lines are scanned."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        # Build output where the keyword is at the beginning, with enough
        # filler lines to push it out of the "last 20 lines" window.
        keyword_line = "usage limit mentioned in docs"
        filler_lines = "\n".join(f"line {i}" for i in range(100))
        early = keyword_line + "\n" + filler_lines + "\n" + "x" * 3001
        assert runner.detect_infra_error(1, early, "") == InfraErrorType.NONE


# =============================================================================
# CodexRunner.run() — Synchronous execution
# =============================================================================

class TestCodexRunnerRun:
    """Test synchronous run method."""

    def test_success_output_is_converted_ndjson(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        codex_output = json.dumps({
            "type": "turn.completed",
            "data": {"usage": {"input_tokens": 10, "output_tokens": 5}},
        })
        mock_result = subprocess.CompletedProcess(
            args=["codex"], returncode=0, stdout=codex_output, stderr=""
        )
        with patch("subprocess.run", return_value=mock_result):
            result = runner.run(["exec", "--json", "hi"])
        assert result.returncode == 0
        # Output should contain converted Claude NDJSON
        parsed = json.loads(result.stdout)
        assert parsed["type"] == "result"

    def test_timeout_returns_124(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=30)):
            result = runner.run(["exec", "--json", "hi"], timeout=30)
        assert result.returncode == 124

    def test_failure_returns_nonzero(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        fail_result = subprocess.CompletedProcess(
            args=["codex"], returncode=1, stdout="", stderr="error"
        )
        with patch("subprocess.run", return_value=fail_result):
            result = runner.run(["exec", "--json", "hi"])
        assert result.returncode == 1

    def test_env_scrubs_claudecode(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        captured_env = {}
        mock_result = subprocess.CompletedProcess(
            args=["codex"], returncode=0, stdout="", stderr=""
        )
        def mock_run(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return mock_result
        with patch("subprocess.run", side_effect=mock_run):
            with patch.dict("os.environ", {"CLAUDECODE": "1", "HOME": "/tmp"}):
                runner.run(["exec", "--json", "hi"])
        assert "CLAUDECODE" not in captured_env


# =============================================================================
# CodexRunner.run_with_monitor — Monitored execution
# =============================================================================

class TestCodexRunWithMonitor:
    """Test monitored execution (via mocks)."""

    def test_returns_monitored_result(self):
        from se3.codex_runner import _SingleRunResult
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        def fake_monitor(self_runner, *, full_cmd, **kw):
            return _SingleRunResult(returncode=0, output="", success=True, should_retry=False)
        with patch.object(CodexRunner, "_run_single_with_monitor", autospec=True, side_effect=fake_monitor):
            result = runner.run_with_monitor(["exec", "--json", "hi"])
        assert isinstance(result, MonitoredResult)
        assert result.success
        assert result.cmd_used == "codex"

    def test_output_has_command_prefix(self):
        from se3.codex_runner import _SingleRunResult
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        def fake_monitor(self_runner, *, full_cmd, **kw):
            return _SingleRunResult(returncode=0, output="converted", success=True, should_retry=False)
        with patch.object(CodexRunner, "_run_single_with_monitor", autospec=True, side_effect=fake_monitor):
            result = runner.run_with_monitor(["exec", "--json", "hi"])
        assert result.output.startswith("=== Command: codex ===")

    def test_interrupted_flag_propagated(self):
        from se3.codex_runner import _SingleRunResult
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        def fake_monitor(self_runner, *, full_cmd, **kw):
            return _SingleRunResult(returncode=-2, output="partial", success=False, should_retry=False, interrupted=True)
        with patch.object(CodexRunner, "_run_single_with_monitor", autospec=True, side_effect=fake_monitor):
            result = runner.run_with_monitor(["exec", "--json", "hi"])
        assert result.interrupted
        assert result.returncode == -2

    def test_exception_returns_error_result(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        with patch.object(CodexRunner, "_run_single_with_monitor", side_effect=RuntimeError("boom")):
            result = runner.run_with_monitor(["exec", "--json", "hi"])
        assert result.returncode == 1
        assert "boom" in result.output

    def test_command_not_found_returns_127(self):
        """shutil.which returning None should short-circuit with 127."""
        runner = CodexRunner(command={"cmd": "nonexistent-codex", "priority": 0})
        with patch("shutil.which", return_value=None):
            result = runner._run_single_with_monitor(
                full_cmd=["nonexistent-codex", "exec"],
                cmd_name="nonexistent-codex",
                log_file=None,
                wall_timeout=None,
                inactivity_timeout=1800,
                cwd=None,
                env={},
                on_output=None,
                on_activity=None,
                start_time=0,
            )
        assert result.returncode == 127
        assert result.should_retry is True


# =============================================================================
# LLMCaller._create_runner — codex registration
# =============================================================================

class TestLLMCallerCodexRegistration:
    """Test that type: codex is correctly dispatched in _create_runner."""

    def test_codex_type_creates_codex_runner(self):
        from se3.engine.llm_caller import LLMCaller
        with patch.object(LLMCaller, "__init__", lambda self, *a, **kw: None):
            caller = LLMCaller.__new__(LLMCaller)
            caller.project_root = None
            runner = caller._create_runner({"type": "codex", "cmd": "codex", "priority": 0})
        assert isinstance(runner, CodexRunner)
        assert runner.command["cmd"] == "codex"

    def test_claude_code_type_still_works(self):
        from se3.claude_runner import ClaudeCodeRunner
        from se3.engine.llm_caller import LLMCaller
        with patch.object(LLMCaller, "__init__", lambda self, *a, **kw: None):
            caller = LLMCaller.__new__(LLMCaller)
            caller.project_root = None
            runner = caller._create_runner({"type": "claude-code", "cmd": "claude", "priority": 0})
        assert isinstance(runner, ClaudeCodeRunner)

    def test_unknown_type_raises_value_error(self):
        from se3.engine.llm_caller import LLMCaller
        with patch.object(LLMCaller, "__init__", lambda self, *a, **kw: None):
            caller = LLMCaller.__new__(LLMCaller)
            caller.project_root = None
            with pytest.raises(ValueError, match="Unknown agent type"):
                caller._create_runner({"type": "unknown", "cmd": "foo"})

    def test_codex_runner_preserves_priority(self):
        from se3.engine.llm_caller import LLMCaller
        with patch.object(LLMCaller, "__init__", lambda self, *a, **kw: None):
            caller = LLMCaller.__new__(LLMCaller)
            caller.project_root = None
            runner = caller._create_runner({"type": "codex", "cmd": "my-codex", "priority": 20})
        assert runner.command["priority"] == 20


# =============================================================================
# CodexEventConverter — output lines are all valid JSON with expected types
# =============================================================================

class TestConverterOutputContract:
    """Verify the converter's output contract: every line is valid JSON
    and every type is in {assistant, user, result}."""

    SAMPLE_EVENTS = [
        '{"type": "thread.started", "data": {}}',
        '{"type": "turn.started", "data": {}}',
        json.dumps({"type": "item.updated", "data": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]}}),
        json.dumps({"type": "item.updated", "data": {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": json.dumps({"command": "ls"})}}),
        json.dumps({"type": "item.updated", "data": {"type": "function_call_output", "call_id": "c1", "output": "ok", "is_error": False}}),
        json.dumps({"type": "item.updated", "data": {"type": "function_call", "name": "write_file", "call_id": "c2", "arguments": json.dumps({"file_path": "/tmp/x.py", "content": "x=1"})}}),
        json.dumps({"type": "item.updated", "data": {"type": "function_call_output", "call_id": "c2", "output": "written", "is_error": False}}),
        json.dumps({"type": "turn.completed", "data": {"usage": {"input_tokens": 100, "output_tokens": 50}}}),
    ]

    def test_all_output_lines_valid_json(self):
        conv = CodexEventConverter()
        all_lines = []
        for line in self.SAMPLE_EVENTS:
            all_lines.extend(conv.convert_line(line))
        all_lines.extend(conv.finalize())
        assert len(all_lines) > 0
        for line in all_lines:
            parsed = json.loads(line)  # Must not raise
            assert parsed["type"] in ("assistant", "user", "result")

    def test_turn_completed_usage_four_fields(self):
        """turn.completed result must have all four usage fields."""
        conv = CodexEventConverter()
        all_lines = []
        for line in self.SAMPLE_EVENTS:
            all_lines.extend(conv.convert_line(line))
        result_lines = [l for l in all_lines if json.loads(l)["type"] == "result"]
        assert len(result_lines) >= 1
        parsed = json.loads(result_lines[0])
        usage = parsed["usage"]
        assert "input_tokens" in usage
        assert "output_tokens" in usage
        assert "cache_creation_input_tokens" in usage
        assert "cache_read_input_tokens" in usage

    def test_unknown_events_dont_crash_converter(self):
        """A stream with only unknown events should not crash."""
        conv = CodexEventConverter()
        unknown_events = [
            '{"type": "v2.new_feature", "data": {"x": 1}}',
            '{"type": "item.updated", "data": {"type": "unknown_future_type"}}',
            'not json at all',
            '',
        ]
        all_lines = []
        for ev in unknown_events:
            all_lines.extend(conv.convert_line(ev))
        all_lines.extend(conv.finalize())
        # finalize should produce an error result since no turn terminal was seen
        assert len(all_lines) >= 1
        parsed = json.loads(all_lines[-1])
        assert parsed["type"] == "result"
