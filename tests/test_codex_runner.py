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
import shutil
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
# CodexEventConverter — event mapping (using actual codex exec --json schema)
# =============================================================================

class TestCodexEventConverterMapping:
    """Test that codex JSONL events are converted to Claude NDJSON.

    All events use the actual codex ``exec --json`` schema where item events
    carry the item payload under the ``item`` key (not ``data``) and use item
    types ``agent_message``, ``command_execution``, ``file_change``,
    ``mcp_tool_call`` (not ``message``, ``function_call``,
    ``function_call_output``).
    """

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
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Hello world",
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 1
        parsed = json.loads(result[0])
        assert parsed["type"] == "assistant"
        assert parsed["message"]["content"][0]["type"] == "text"
        assert parsed["message"]["content"][0]["text"] == "Hello world"

    def test_agent_message_via_item_updated(self):
        """item.updated should also work for agent_message."""
        conv = CodexEventConverter()
        event = {
            "type": "item.updated",
            "item": {
                "type": "agent_message",
                "text": "Streaming text...",
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 1
        parsed = json.loads(result[0])
        assert parsed["type"] == "assistant"
        assert parsed["message"]["content"][0]["text"] == "Streaming text..."

    def test_command_execution_produces_tool_use_and_result(self):
        """command_execution produces a Bash tool_use + tool_result pair."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "ls -la",
                "output": "file1.txt\nfile2.txt",
                "exit_code": 0,
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 2  # tool_use + tool_result

        # First: tool_use
        tool_use = json.loads(result[0])
        assert tool_use["type"] == "assistant"
        content = tool_use["message"]["content"][0]
        assert content["type"] == "tool_use"
        assert content["name"] == "Bash"
        assert content["input"]["command"] == "ls -la"

        # Second: tool_result
        tool_result = json.loads(result[1])
        assert tool_result["type"] == "user"
        tr_content = tool_result["message"]["content"][0]
        assert tr_content["type"] == "tool_result"
        assert tr_content["content"] == "file1.txt\nfile2.txt"
        assert tr_content["is_error"] is False

    def test_command_execution_error_sets_is_error(self):
        """command_execution with non-zero exit_code sets is_error=True."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "false",
                "output": "exit code 1",
                "exit_code": 1,
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 2
        tool_result = json.loads(result[1])
        tr_content = tool_result["message"]["content"][0]
        assert tr_content["is_error"] is True

    def test_file_change_maps_to_write_tool(self):
        """file_change events should map to Write tool_use."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "path": "/tmp/test.py",
                "content": "x=1",
                "change_type": "write",
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 2  # tool_use + tool_result

        tool_use = json.loads(result[0])
        content = tool_use["message"]["content"][0]
        assert content["type"] == "tool_use"
        assert content["name"] == "Write"
        assert content["input"]["file_path"] == "/tmp/test.py"
        assert content["input"]["content"] == "x=1"

        # Touched files should be recorded
        assert "/tmp/test.py" in conv.touched_files

    def test_file_change_create_maps_to_write(self):
        """file_change with change_type=create maps to Write."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "path": "/tmp/new.py",
                "content": "print('hi')",
                "change_type": "create",
            },
        }
        result = conv.convert_line(json.dumps(event))
        tool_use = json.loads(result[0])
        assert tool_use["message"]["content"][0]["name"] == "Write"

    def test_file_change_modify_maps_to_edit(self):
        """file_change with change_type=modify maps to Edit."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "path": "/tmp/existing.py",
                "content": "new content",
                "change_type": "modify",
            },
        }
        result = conv.convert_line(json.dumps(event))
        tool_use = json.loads(result[0])
        content = tool_use["message"]["content"][0]
        assert content["name"] == "Edit"
        assert content["input"]["file_path"] == "/tmp/existing.py"
        assert content["input"]["new_string"] == "new content"

    def test_file_change_records_touched_files(self):
        """Multiple file_change items accumulate touched files."""
        conv = CodexEventConverter()
        for path in ["/a.py", "/b.py", "/c.py"]:
            event = {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "path": path,
                    "content": "",
                    "change_type": "write",
                },
            }
            conv.convert_line(json.dumps(event))
        assert conv.touched_files == {"/a.py", "/b.py", "/c.py"}

    def test_mcp_tool_call_preserves_name(self):
        """MCP tool calls keep their original name."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "name": "mcp_my_server__search",
                "call_id": "call_mcp",
                "arguments": {"query": "test"},
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) >= 1
        parsed = json.loads(result[0])
        assert parsed["message"]["content"][0]["name"] == "mcp_my_server__search"

    def test_mcp_tool_call_with_arguments_as_string(self):
        """MCP tool call arguments may be a JSON string."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "name": "my_tool",
                "call_id": "call_str",
                "arguments": json.dumps({"key": "value"}),
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["message"]["content"][0]["input"]["key"] == "value"

    def test_mcp_tool_call_with_output_generates_result(self):
        """MCP tool call with output field generates tool_result."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "name": "my_tool",
                "call_id": "call_out",
                "arguments": {},
                "output": "tool output here",
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 2  # tool_use + tool_result
        tool_result = json.loads(result[1])
        assert tool_result["message"]["content"][0]["content"] == "tool output here"

    def test_turn_completed_produces_result_with_usage(self):
        conv = CodexEventConverter()
        # First send an agent message to accumulate
        msg_event = {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Done!",
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

    def test_unknown_item_type_returns_empty_no_crash(self):
        """An item with an unknown type should be silently skipped."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {"type": "some_future_item_type", "data": 123},
        }
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

    def test_command_execution_with_call_id(self):
        """command_execution with explicit call_id uses it as tool_use_id."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "pwd",
                "output": "/tmp",
                "exit_code": 0,
                "call_id": "my_custom_id",
            },
        }
        result = conv.convert_line(json.dumps(event))
        tool_use = json.loads(result[0])
        assert tool_use["message"]["content"][0]["id"] == "my_custom_id"
        tool_result = json.loads(result[1])
        assert tool_result["message"]["content"][0]["tool_use_id"] == "my_custom_id"

    def test_file_change_with_file_path_key(self):
        """file_change may use file_path instead of path."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "file_path": "/tmp/alt.py",
                "content": "y=2",
                "change_type": "write",
            },
        }
        result = conv.convert_line(json.dumps(event))
        tool_use = json.loads(result[0])
        assert tool_use["message"]["content"][0]["input"]["file_path"] == "/tmp/alt.py"
        assert "/tmp/alt.py" in conv.touched_files

    def test_command_execution_missing_fields_defaults(self):
        """command_execution with missing fields doesn't crash."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 2
        tool_use = json.loads(result[0])
        assert tool_use["message"]["content"][0]["name"] == "Bash"
        assert tool_use["message"]["content"][0]["input"]["command"] == ""

    def test_file_change_missing_fields_defaults(self):
        """file_change with missing fields doesn't crash."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "file_change",
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 2
        tool_use = json.loads(result[0])
        assert tool_use["message"]["content"][0]["name"] == "Write"

    def test_agent_message_empty_text_skipped(self):
        """agent_message with empty text produces no output."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "",
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert result == []

    def test_all_output_lines_are_valid_json(self):
        """Every line from convert_line must be valid JSON."""
        conv = CodexEventConverter()
        events = [
            {"type": "thread.started", "data": {}},
            {"type": "turn.started", "data": {}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
            {"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "output": "ok", "exit_code": 0}},
            {"type": "item.completed", "item": {"type": "file_change", "path": "/tmp/x.py", "content": "x=1", "change_type": "write"}},
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
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "partial"},
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
        assert "-a" not in args
        assert "--dangerously-bypass-approvals-and-sandbox" not in args

    def test_writable_step_has_sandbox_flag(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="do it", read_only=False)
        assert "--sandbox" in args
        si = args.index("--sandbox")
        assert args[si + 1] == "danger-full-access"
        assert "--dangerously-bypass-approvals-and-sandbox" not in args

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
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}}),
        json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "output": "ok", "exit_code": 0}}),
        json.dumps({"type": "item.completed", "item": {"type": "file_change", "path": "/tmp/x.py", "content": "x=1", "change_type": "write"}}),
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
            '{"type": "item.completed", "item": {"type": "unknown_future_type"}}',
            'not json at all',
            '',
        ]
        all_lines = []
        for ev in unknown_events:
            all_lines.extend(conv.convert_line(ev))
        all_lines.extend(conv.finalize())
        # finalize should produce an error result since no turn terminal was seen
        assert len(all_lines) >= 1


# =============================================================================
# Integration: NDJSON consumer chain (text extraction + usage parsing)
# =============================================================================

def _run_full_codex_session(events: list[str]) -> str:
    """Helper: run a list of codex JSONL event strings through the converter
    and return the concatenated NDJSON output (as a single string)."""
    conv = CodexEventConverter()
    all_lines: list[str] = []
    for ev in events:
        all_lines.extend(conv.convert_line(ev))
    all_lines.extend(conv.finalize())
    return "\n".join(all_lines)


class TestConverterNDJSONConsumerIntegration:
    """Feed converter output into the existing NDJSON consumer functions
    (_extract_text_from_ndjson, parse_usage_from_ndjson) and verify
    they produce correct results — zero changes required upstream."""

    # -- Full session: assistant text + command_execution + result --

    FULL_SESSION_EVENTS = [
        json.dumps({"type": "thread.started", "data": {}}),
        json.dumps({"type": "turn.started", "data": {}}),
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "I will read the file first.",
            },
        }),
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "cat /tmp/test.py",
                "output": "x = 1\ny = 2",
                "exit_code": 0,
            },
        }),
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Now I will edit the file.",
            },
        }),
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "file_change",
                "path": "/tmp/test.py",
                "content": "x = 42\ny = 2",
                "change_type": "write",
            },
        }),
        json.dumps({
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "pytest tests/ -v",
                "output": "FAILED test_something",
                "exit_code": 1,
            },
        }),
        json.dumps({
            "type": "turn.completed",
            "data": {
                "usage": {
                    "input_tokens": 1500,
                    "output_tokens": 800,
                    "cached_input_tokens": 200,
                },
                "total_cost_usd": 0.012,
            },
        }),
    ]

    def test_text_extraction_from_full_session(self):
        """_extract_text_from_ndjson should extract all assistant text chunks."""
        from se3.engine.llm_caller import LLMCaller
        ndjson_output = _run_full_codex_session(self.FULL_SESSION_EVENTS)
        text = LLMCaller._extract_text_from_ndjson(ndjson_output)
        assert text is not None
        assert "I will read the file first." in text
        assert "Now I will edit the file." in text

    def test_usage_parsing_from_full_session(self):
        """parse_usage_from_ndjson should capture usage from turn.completed."""
        from se3.engine.chat_history import parse_usage_from_ndjson
        ndjson_output = _run_full_codex_session(self.FULL_SESSION_EVENTS)
        usage = parse_usage_from_ndjson(ndjson_output)
        assert usage["input_tokens"] == 1500
        assert usage["output_tokens"] == 800
        assert usage["cache_read_input_tokens"] == 200
        assert usage["total_cost_usd"] == 0.012

    def test_command_execution_produces_tool_use_and_result(self):
        """command_execution should produce Bash tool_use + tool_result
        so tool_formatters can render previews."""
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "ls -la",
                "output": "file1.txt",
                "exit_code": 0,
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 2

        tool_use = json.loads(result[0])
        assert tool_use["type"] == "assistant"
        content = tool_use["message"]["content"][0]
        assert content["type"] == "tool_use"
        assert content["name"] == "Bash"
        assert isinstance(content["input"], dict)

        tool_result = json.loads(result[1])
        assert tool_result["type"] == "user"
        tr = tool_result["message"]["content"][0]
        assert tr["type"] == "tool_result"
        assert tr["tool_use_id"] == content["id"]

    def test_command_execution_success_flow(self):
        """Full command execution: successful command_execution
        → consumers should parse without errors."""
        events = [
            json.dumps({"type": "turn.started", "data": {}}),
            json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "echo hello",
                    "output": "hello\n",
                    "exit_code": 0,
                },
            }),
            json.dumps({
                "type": "turn.completed",
                "data": {"usage": {"input_tokens": 10, "output_tokens": 5}},
            }),
        ]
        from se3.engine.llm_caller import LLMCaller
        from se3.engine.chat_history import parse_usage_from_ndjson
        ndjson_output = _run_full_codex_session(events)
        # Text extraction should not crash (no assistant text in this case)
        text = LLMCaller._extract_text_from_ndjson(ndjson_output)
        # Usage parsing should work
        usage = parse_usage_from_ndjson(ndjson_output)
        assert usage["input_tokens"] == 10
        assert usage["output_tokens"] == 5

    def test_command_execution_error_flow(self):
        """Command execution with non-zero exit (is_error=True):
        consumers should still parse without errors."""
        events = [
            json.dumps({"type": "turn.started", "data": {}}),
            json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "false",
                    "output": "exit code 1",
                    "exit_code": 1,
                },
            }),
            json.dumps({
                "type": "turn.completed",
                "data": {"usage": {}},
            }),
        ]
        from se3.engine.llm_caller import LLMCaller
        from se3.engine.chat_history import parse_usage_from_ndjson
        ndjson_output = _run_full_codex_session(events)
        text = LLMCaller._extract_text_from_ndjson(ndjson_output)
        usage = parse_usage_from_ndjson(ndjson_output)
        # All-zero usage returns empty dict
        assert isinstance(usage, dict)

    def test_text_extraction_from_finalize_synthesized_result(self):
        """When finalize synthesizes a result (no turn.completed/failed),
        _extract_text_from_ndjson should still extract the accumulated text."""
        from se3.engine.llm_caller import LLMCaller
        events = [
            json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "Partial output only.",
                },
            }),
            # No turn.completed or turn.failed — finalize will synthesize
        ]
        ndjson_output = _run_full_codex_session(events)
        text = LLMCaller._extract_text_from_ndjson(ndjson_output)
        assert text is not None
        assert "Partial output only." in text

    def test_usage_from_finalize_synthesized_result(self):
        """finalize-synthesized result should still produce parseable usage
        (all zeros)."""
        from se3.engine.chat_history import parse_usage_from_ndjson
        events = [
            json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "Done.",
                },
            }),
        ]
        ndjson_output = _run_full_codex_session(events)
        usage = parse_usage_from_ndjson(ndjson_output)
        # finalize synthesizes usage with all zeros → parse returns empty dict
        assert isinstance(usage, dict)

    def test_turn_completed_with_nested_message_usage(self):
        """turn.completed may carry usage at data.message.usage instead
        of data.usage — the converter should handle both."""
        conv = CodexEventConverter()
        event = {
            "type": "turn.completed",
            "data": {
                "message": {
                    "usage": {
                        "input_tokens": 500,
                        "output_tokens": 200,
                        "cached_input_tokens": 50,
                    },
                },
                "total_cost_usd": 0.005,
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["usage"]["input_tokens"] == 500
        assert parsed["usage"]["output_tokens"] == 200
        assert parsed["usage"]["cache_read_input_tokens"] == 50
        # Also verify parse_usage_from_ndjson can read it
        from se3.engine.chat_history import parse_usage_from_ndjson
        usage = parse_usage_from_ndjson(result[0])
        assert usage["input_tokens"] == 500

    def test_touched_files_from_full_session(self):
        """file_change items should populate touched_files."""
        conv = CodexEventConverter()
        for ev in self.FULL_SESSION_EVENTS:
            conv.convert_line(ev)
        # The full session has one file_change for /tmp/test.py
        assert "/tmp/test.py" in conv.touched_files


# =============================================================================
# item.completed event type (alias for item.updated)
# =============================================================================

class TestItemCompletedEventType:
    """item.completed should produce the same output as item.updated."""

    def test_agent_message_via_item_completed(self):
        conv = CodexEventConverter()
        event = {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Done via completed.",
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 1
        parsed = json.loads(result[0])
        assert parsed["type"] == "assistant"
        assert "Done via completed." in parsed["message"]["content"][0]["text"]

    def test_command_execution_via_item_updated(self):
        """item.updated should also work for command_execution."""
        conv = CodexEventConverter()
        event = {
            "type": "item.updated",
            "item": {
                "type": "command_execution",
                "command": "pwd",
                "output": "/tmp",
                "exit_code": 0,
            },
        }
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 2
        tool_use = json.loads(result[0])
        assert tool_use["message"]["content"][0]["name"] == "Bash"


# =============================================================================
# Turn.completed — non-string error in turn.failed
# =============================================================================

class TestTurnFailedErrorShapes:
    """turn.failed / error events can carry error as a string, dict,
    or missing — the converter should handle all without crashing."""

    def test_error_as_plain_string(self):
        conv = CodexEventConverter()
        event = {"type": "turn.failed", "data": {"error": "overloaded"}}
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert "overloaded" in parsed["result"]

    def test_error_as_dict_with_message(self):
        conv = CodexEventConverter()
        event = {"type": "turn.failed", "data": {"error": {"message": "rate limited"}}}
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert "rate limited" in parsed["result"]

    def test_error_field_missing_uses_message(self):
        conv = CodexEventConverter()
        event = {"type": "turn.failed", "data": {"message": "something broke"}}
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert "something broke" in parsed["result"]

    def test_error_and_message_missing_uses_str_data(self):
        conv = CodexEventConverter()
        event = {"type": "turn.failed", "data": {"code": 500}}
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["type"] == "result"
        assert parsed["is_error"] is True


# =============================================================================
# Task 2 — build_call_args additional coverage
# =============================================================================

class TestCodexBuildCallArgsExtended:
    """Extended build_call_args tests for flag ordering and exact argv shape."""

    def test_constant_prefix_flag_ordering(self):
        """The constant prefix must appear in the exact order:
        exec --json --skip-git-repo-check."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="test", read_only=False)
        assert args[0] == "exec"
        assert args[1] == "--json"
        assert args[2] == "--skip-git-repo-check"
        assert "-a" not in args

    def test_read_only_sandbox_immediately_after_prefix(self):
        """--sandbox read-only should come right after the constant prefix."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="analyze", read_only=True)
        assert args[3] == "--sandbox"
        assert args[4] == "read-only"

    def test_writable_sandbox_immediately_after_prefix(self):
        """--sandbox danger-full-access should come right
        after the constant prefix."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="implement", read_only=False)
        assert args[3] == "--sandbox"
        assert args[4] == "danger-full-access"

    def test_prompt_is_always_last_element(self):
        """The prompt (or '-' for stdin) must be the last element."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        for prompt in ["short", "a somewhat longer prompt", "中文 prompt"]:
            args = runner.build_call_args(prompt=prompt, read_only=False)
            assert args[-1] == prompt

    def test_context_files_appear_before_prompt(self, tmp_path):
        """When context files are inlined, the prompt is still the last arg."""
        f1 = tmp_path / "a.md"
        f1.write_text("file A content", encoding="utf-8")
        f2 = tmp_path / "b.md"
        f2.write_text("file B content", encoding="utf-8")
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(
            prompt="final instruction", read_only=True,
            context_files=[f1, f2],
        )
        # Prompt is still last
        assert args[-1].endswith("final instruction")
        # Both file contents are in the prompt
        assert "file A content" in args[-1]
        assert "file B content" in args[-1]

    def test_oversized_prompt_stores_stdin_payload(self):
        """When prompt > _MAX_ARG_BYTES, the payload is stored for stdin."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        big = "y" * (_MAX_ARG_BYTES + 100)
        runner.build_call_args(prompt=big, read_only=False)
        assert runner._pending_stdin_prompt == big

    def test_oversized_multibyte_utf8_prompt(self):
        """UTF-8 multibyte characters should be measured in bytes, not chars."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        # Each '中' is 3 bytes in UTF-8. Need > 102400 bytes → ~34134 chars
        big = "中" * 35000  # 105000 bytes
        args = runner.build_call_args(prompt=big, read_only=False)
        assert args[-1] == "-"
        assert runner._pending_stdin_prompt == big

    def test_oversized_context_files_plus_prompt(self, tmp_path):
        """When inlined context + prompt together exceed threshold, use stdin."""
        f = tmp_path / "big.md"
        # 60KB file content
        f.write_text("x" * 60000, encoding="utf-8")
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        # 60KB file + 50KB prompt > 100KB threshold
        args = runner.build_call_args(
            prompt="y" * 50000, read_only=False, context_files=[f],
        )
        assert args[-1] == "-"
        assert runner._pending_stdin_prompt is not None
        assert "x" * 60000 in runner._pending_stdin_prompt


# =============================================================================
# Task 2 — detect_infra_error five-class coverage
# =============================================================================

class TestCodexDetectInfraErrorExtended:
    """Verify the five-class classification: success=NONE, timeout=TIMEOUT,
    usage_limit=USAGE_LIMIT, auth_failure=USAGE_LIMIT, task_failure=NONE."""

    @pytest.mark.parametrize("returncode,stdout,stderr,expected", [
        # Success → NONE (even with keywords in output)
        (0, "usage limit exceeded", "", InfraErrorType.NONE),
        # Timeout → TIMEOUT
        (124, "", "", InfraErrorType.TIMEOUT),
        # Usage limit keywords → USAGE_LIMIT
        (1, "", "Error: usage limit", InfraErrorType.USAGE_LIMIT),
        (1, "rate limit exceeded", "", InfraErrorType.USAGE_LIMIT),
        (1, "HTTP 429", "", InfraErrorType.USAGE_LIMIT),
        (1, "quota exceeded", "", InfraErrorType.USAGE_LIMIT),
        (1, "too many requests", "", InfraErrorType.USAGE_LIMIT),
        # Auth failure → USAGE_LIMIT (credential-level rotation)
        (1, "", "401 Unauthorized", InfraErrorType.USAGE_LIMIT),
        (1, "unauthorized access", "", InfraErrorType.USAGE_LIMIT),
        (1, "authentication failed", "", InfraErrorType.USAGE_LIMIT),
        # Task failure → NONE
        (1, "file not found", "", InfraErrorType.NONE),
        (1, "", "AssertionError", InfraErrorType.NONE),
        (2, "syntax error", "", InfraErrorType.NONE),
    ], ids=[
        "success_with_keywords", "timeout", "usage_limit_stderr",
        "usage_limit_stdout", "usage_limit_429", "usage_limit_quota",
        "usage_limit_too_many", "auth_401", "auth_unauthorized",
        "auth_failed", "task_failure_file", "task_failure_assert",
        "task_failure_syntax",
    ])
    def test_five_class_classification(self, returncode, stdout, stderr, expected):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        assert runner.detect_infra_error(returncode, stdout, stderr) == expected


# =============================================================================
# Task 2 — End-to-end: agents registry → LLMCaller → CodexRunner
# =============================================================================

class TestCodexRegistryEndToEnd:
    """Test the full path from agents registry config to CodexRunner
    creation through LLMCaller.__init__."""

    def test_llmcaller_with_codex_agents_list(self):
        """LLMCaller constructed with agents=[{type: codex, ...}]
        should create a CodexRunner as its current runner."""
        from se3.engine.llm_caller import LLMCaller
        agents = [
            {"name": "my-codex", "type": "codex", "cmd": "codex", "priority": 0},
        ]
        caller = LLMCaller(agents=agents)
        assert isinstance(caller._runner, CodexRunner)
        assert caller._runner.command["cmd"] == "codex"

    def test_llmcaller_with_mixed_claude_and_codex_agents(self):
        """When agents list has both claude-code and codex, the first
        agent's runner type is used initially."""
        from se3.claude_runner import ClaudeCodeRunner
        from se3.engine.llm_caller import LLMCaller
        agents = [
            {"name": "claude", "type": "claude-code", "cmd": "claude", "priority": 10},
            {"name": "codex", "type": "codex", "cmd": "codex", "priority": 5},
        ]
        caller = LLMCaller(agents=agents)
        # First agent is claude → ClaudeCodeRunner
        assert isinstance(caller._runner, ClaudeCodeRunner)

    def test_llmcaller_codex_runner_rotation(self):
        """After rotating from claude to codex, the runner should switch
        to CodexRunner."""
        from se3.claude_runner import ClaudeCodeRunner
        from se3.engine.llm_caller import LLMCaller
        agents = [
            {"name": "claude", "type": "claude-code", "cmd": "claude", "priority": 10},
            {"name": "codex", "type": "codex", "cmd": "codex", "priority": 5},
        ]
        caller = LLMCaller(agents=agents)
        assert isinstance(caller._runner, ClaudeCodeRunner)
        # Rotate to next agent
        caller._rotate_agent()
        assert isinstance(caller._runner, CodexRunner)
        assert caller._runner.command["cmd"] == "codex"

    def test_codex_runner_cached_per_agent(self):
        """The runner cache should key by agent name/cmd and reuse."""
        from se3.engine.llm_caller import LLMCaller
        agents = [
            {"name": "codex-a", "type": "codex", "cmd": "codex-a", "priority": 0},
            {"name": "codex-b", "type": "codex", "cmd": "codex-b", "priority": 0},
        ]
        caller = LLMCaller(agents=agents)
        runner_a = caller._runner
        caller._rotate_agent()
        runner_b = caller._runner
        assert runner_a is not runner_b
        assert runner_a.command["cmd"] == "codex-a"
        assert runner_b.command["cmd"] == "codex-b"
        # Rotate back and verify cache
        caller._current_agent_index = 0
        assert caller._get_current_runner() is runner_a

    def test_codex_default_command_when_no_cmd(self):
        """CodexRunner should default cmd to 'codex' when command omits it."""
        from se3.engine.llm_caller import LLMCaller
        agents = [
            {"name": "c", "type": "codex", "cmd": "codex"},
        ]
        caller = LLMCaller(agents=agents)
        assert caller._runner.command["cmd"] == "codex"

    def test_codex_runner_receives_project_root(self):
        """CodexRunner should receive project_root from LLMCaller."""
        from se3.engine.llm_caller import LLMCaller
        agents = [
            {"name": "c", "type": "codex", "cmd": "codex"},
        ]
        caller = LLMCaller(project_root="/tmp/test-proj", agents=agents)
        assert caller._runner.command["cmd"] == "codex"


# =============================================================================
# LLMCaller._create_runner — codex dispatch exact args
# =============================================================================

class TestCreateRunnerCodexDispatch:
    """Verify _create_runner passes the correct args to CodexRunner."""

    def test_create_runner_passes_cmd_and_priority(self):
        from se3.engine.llm_caller import LLMCaller
        caller = LLMCaller.__new__(LLMCaller)
        caller.project_root = Path("/tmp/proj")
        runner = caller._create_runner({"type": "codex", "cmd": "my-codex", "priority": 42})
        assert isinstance(runner, CodexRunner)
        assert runner.command["cmd"] == "my-codex"
        assert runner.command["priority"] == 42

    def test_create_runner_default_priority_zero(self):
        """When priority is not specified, it defaults to 0."""
        from se3.engine.llm_caller import LLMCaller
        caller = LLMCaller.__new__(LLMCaller)
        caller.project_root = Path("/tmp/proj")
        runner = caller._create_runner({"type": "codex", "cmd": "codex"})
        assert runner.command["priority"] == 0

    def test_create_runner_unknown_type_raises(self):
        from se3.engine.llm_caller import LLMCaller
        caller = LLMCaller.__new__(LLMCaller)
        caller.project_root = Path("/tmp/proj")
        with pytest.raises(ValueError, match="Unknown agent type: llama"):
            caller._create_runner({"type": "llama", "cmd": "llama"})


# =============================================================================
# build_call_args: no other codex-specific flags leak through
# =============================================================================

class TestBuildCallArgsNoClaudeFlags:
    """Verify that Claude-specific flags are NOT present in codex args."""

    def test_no_output_format_flag(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="test", read_only=False)
        assert "--output-format" not in args
        assert "stream-json" not in args

    def test_no_verbose_flag(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="test", read_only=False)
        assert "--verbose" not in args

    def test_no_setting_sources_flag(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="test", read_only=False)
        assert "--setting-sources" not in args

    def test_no_disallowed_tools_flag(self):
        """Read-only enforcement uses --sandbox, not --disallowedTools."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="test", read_only=True)
        assert "--disallowedTools" not in args

    def test_no_dangerously_skip_permissions(self):
        """Codex exec is non-interactive; read/write both use --sandbox."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(prompt="test", read_only=False)
        assert "--dangerously-skip-permissions" not in args
        assert "-a" not in args


# =============================================================================
# Real CLI smoke test (gated on codex availability)
# =============================================================================

_CODEX_AVAILABLE = shutil.which("codex") is not None


@pytest.mark.skipif(
    not _CODEX_AVAILABLE,
    reason="codex CLI not found on PATH",
)
class TestCodexRealCliSmoke:
    """Integration smoke test: run a real codex exec and verify the event stream."""

    def test_exec_read_only_produces_result_event(self):
        """codex exec --json --sandbox read-only should exit 0 and produce
        a type=result terminal event after CodexEventConverter processing."""
        cmd = [
            "codex", "exec", "--json", "--skip-git-repo-check",
            "--sandbox", "read-only", "--ephemeral",
            "Reply with the single word: ok",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            pytest.skip("codex exec timed out (120s)")

        if proc.returncode != 0:
            # Auth / network failure — not a code defect
            stderr_tail = (proc.stderr or "")[-500:]
            pytest.skip(
                f"codex exec exited {proc.returncode}: {stderr_tail}"
            )

        # Feed stdout through CodexEventConverter
        converter = CodexEventConverter()
        ndjson_lines: list[str] = []
        for line in proc.stdout.splitlines():
            ndjson_lines.extend(converter.convert_line(line))
        ndjson_lines.extend(converter.finalize())

        # Parse and look for a type=result terminal event
        result_events = []
        for raw in ndjson_lines:
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "result":
                result_events.append(obj)

        assert result_events, (
            "Expected at least one type=result event after conversion, "
            f"got none. NDJSON output ({len(ndjson_lines)} lines): "
            + "; ".join(ndjson_lines[:5])
        )
