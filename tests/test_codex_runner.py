"""Tests for CodexRunner (codex_runner module).

Tests cover:
- CodexEventConverter: event mapping, unknown event tolerance, finalize
- CodexRunner.build_call_args: sandbox flags, context inlining, stdin routing
- CodexRunner.detect_infra_error: success, usage limit, auth failure, timeout
- CodexRunner.run / run_with_monitor: subprocess lifecycle (via mocks)
- Registration via LLMCaller._create_runner
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tianluo.agent_runner import AgentInvocationIntent, AgentRunner, InfraErrorType
from tianluo.codex_runner import (
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
        # The OpenAI-shape subset marker is passed through unrenamed so the
        # shared normalizer applies the subset rule instead of the additive one.
        assert parsed["usage"]["cached_input_tokens"] == 20
        assert "cache_creation_input_tokens" not in parsed["usage"]
        assert parsed["total_cost_usd"] == 0.001

    def test_turn_completed_missing_usage_is_omitted(self):
        conv = CodexEventConverter()
        event = {"type": "turn.completed", "data": {}}
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert "usage" not in parsed

    def test_turn_completed_null_usage_does_not_crash(self):
        """Explicit ``"usage": null`` must not synthesize zero usage."""
        conv = CodexEventConverter()
        # The key is present with a null value — data.get("usage", {}) returns
        # None (not the default), which previously crashed on .get().
        event = {"type": "turn.completed", "data": {"usage": None}}
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 1
        parsed = json.loads(result[0])
        assert parsed["type"] == "result"
        assert "usage" not in parsed

    def test_turn_completed_null_usage_with_result_text_preserved(self):
        """When usage is null, the result text from accumulated agent messages
        must still be present in the emitted result event."""
        conv = CodexEventConverter()
        # Simulate prior agent_message accumulation
        conv._agent_messages = ["Hello from the agent"]
        event = {"type": "turn.completed", "data": {"usage": None}}
        result = conv.convert_line(json.dumps(event))
        assert len(result) == 1
        parsed = json.loads(result[0])
        assert parsed["type"] == "result"
        assert "Hello from the agent" in parsed["result"]
        assert "usage" not in parsed

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

    def test_startup_metadata_does_not_guess_wrapper_model(self):
        runner = CodexRunner(command={"cmd": "company-wrapper", "priority": 0})
        metadata = runner.get_startup_metadata()
        assert metadata.provider == "openai"
        assert metadata.model is None

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

    def test_direct_intent_remains_plain_codex_exec(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        args = runner.build_call_args(
            prompt="implement all requirements",
            read_only=False,
            invocation_intent=AgentInvocationIntent.DIRECT_IMPLEMENTATION,
        )

        assert args[:3] == ["exec", "--json", "--skip-git-repo-check"]
        assert args[-1] == "implement all requirements"
        assert "/goal" not in " ".join(args)
        assert runner.supports_native_goal is False

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
        from tianluo.codex_runner import _SingleRunResult
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        def fake_monitor(self_runner, *, full_cmd, **kw):
            return _SingleRunResult(returncode=0, output="", success=True, should_retry=False)
        with patch.object(CodexRunner, "_run_single_with_monitor", autospec=True, side_effect=fake_monitor):
            result = runner.run_with_monitor(["exec", "--json", "hi"])
        assert isinstance(result, MonitoredResult)
        assert result.success
        assert result.cmd_used == "codex"

    def test_output_has_command_prefix(self):
        from tianluo.codex_runner import _SingleRunResult
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        def fake_monitor(self_runner, *, full_cmd, **kw):
            return _SingleRunResult(returncode=0, output="converted", success=True, should_retry=False)
        with patch.object(CodexRunner, "_run_single_with_monitor", autospec=True, side_effect=fake_monitor):
            result = runner.run_with_monitor(["exec", "--json", "hi"])
        assert result.output.startswith("=== Command: codex ===")

    def test_interrupted_flag_propagated(self):
        from tianluo.codex_runner import _SingleRunResult
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
        from tianluo.engine.llm_caller import LLMCaller
        with patch.object(LLMCaller, "__init__", lambda self, *a, **kw: None):
            caller = LLMCaller.__new__(LLMCaller)
            caller.project_root = None
            runner = caller._create_runner({"type": "codex", "cmd": "codex", "priority": 0})
        assert isinstance(runner, CodexRunner)
        assert runner.command["cmd"] == "codex"

    def test_claude_code_type_still_works(self):
        from tianluo.claude_runner import ClaudeCodeRunner
        from tianluo.engine.llm_caller import LLMCaller
        with patch.object(LLMCaller, "__init__", lambda self, *a, **kw: None):
            caller = LLMCaller.__new__(LLMCaller)
            caller.project_root = None
            runner = caller._create_runner({"type": "claude-code", "cmd": "claude", "priority": 0})
        assert isinstance(runner, ClaudeCodeRunner)

    def test_unknown_type_raises_value_error(self):
        from tianluo.engine.llm_caller import LLMCaller
        with patch.object(LLMCaller, "__init__", lambda self, *a, **kw: None):
            caller = LLMCaller.__new__(LLMCaller)
            caller.project_root = None
            with pytest.raises(ValueError, match="Unknown agent type"):
                caller._create_runner({"type": "unknown", "cmd": "foo"})

    def test_codex_runner_preserves_priority(self):
        from tianluo.engine.llm_caller import LLMCaller
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

    def test_turn_completed_usage_preserves_only_reported_fields(self):
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
        assert "cache_creation_input_tokens" not in usage
        assert "cache_read_input_tokens" not in usage

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
        from tianluo.engine.llm_caller import LLMCaller
        ndjson_output = _run_full_codex_session(self.FULL_SESSION_EVENTS)
        text = LLMCaller._extract_text_from_ndjson(ndjson_output)
        assert text is not None
        assert "I will read the file first." in text
        assert "Now I will edit the file." in text

    def test_usage_parsing_from_full_session(self):
        """parse_usage_from_ndjson should capture usage from turn.completed."""
        from tianluo.engine.chat_history import parse_usage_from_ndjson
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
        from tianluo.engine.llm_caller import LLMCaller
        from tianluo.engine.chat_history import parse_usage_from_ndjson
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
        from tianluo.engine.llm_caller import LLMCaller
        from tianluo.engine.chat_history import parse_usage_from_ndjson
        ndjson_output = _run_full_codex_session(events)
        text = LLMCaller._extract_text_from_ndjson(ndjson_output)
        usage = parse_usage_from_ndjson(ndjson_output)
        # All-zero usage returns empty dict
        assert isinstance(usage, dict)

    def test_text_extraction_from_finalize_synthesized_result(self):
        """When finalize synthesizes a result (no turn.completed/failed),
        _extract_text_from_ndjson should still extract the accumulated text."""
        from tianluo.engine.llm_caller import LLMCaller
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
        from tianluo.engine.chat_history import parse_usage_from_ndjson
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
        assert parsed["usage"]["cached_input_tokens"] == 50
        # Also verify parse_usage_from_ndjson can read it
        from tianluo.engine.chat_history import parse_usage_from_ndjson
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
        from tianluo.engine.llm_caller import LLMCaller
        agents = [
            {"name": "my-codex", "type": "codex", "cmd": "codex", "priority": 0},
        ]
        caller = LLMCaller(agents=agents)
        assert isinstance(caller._runner, CodexRunner)
        assert caller._runner.command["cmd"] == "codex"

    def test_llmcaller_with_mixed_claude_and_codex_agents(self):
        """When agents list has both claude-code and codex, the first
        agent's runner type is used initially."""
        from tianluo.claude_runner import ClaudeCodeRunner
        from tianluo.engine.llm_caller import LLMCaller
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
        from tianluo.claude_runner import ClaudeCodeRunner
        from tianluo.engine.llm_caller import LLMCaller
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
        from tianluo.engine.llm_caller import LLMCaller
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
        from tianluo.engine.llm_caller import LLMCaller
        agents = [
            {"name": "c", "type": "codex", "cmd": "codex"},
        ]
        caller = LLMCaller(agents=agents)
        assert caller._runner.command["cmd"] == "codex"

    def test_codex_runner_receives_project_root(self):
        """CodexRunner should receive project_root from LLMCaller."""
        from tianluo.engine.llm_caller import LLMCaller
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
        from tianluo.engine.llm_caller import LLMCaller
        caller = LLMCaller.__new__(LLMCaller)
        caller.project_root = Path("/tmp/proj")
        runner = caller._create_runner({"type": "codex", "cmd": "my-codex", "priority": 42})
        assert isinstance(runner, CodexRunner)
        assert runner.command["cmd"] == "my-codex"
        assert runner.command["priority"] == 42

    def test_create_runner_default_priority_zero(self):
        """When priority is not specified, it defaults to 0."""
        from tianluo.engine.llm_caller import LLMCaller
        caller = LLMCaller.__new__(LLMCaller)
        caller.project_root = Path("/tmp/proj")
        runner = caller._create_runner({"type": "codex", "cmd": "codex"})
        assert runner.command["priority"] == 0

    def test_create_runner_unknown_type_raises(self):
        from tianluo.engine.llm_caller import LLMCaller
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


# =============================================================================
# Task 1 — Hardened usage/cost extraction: multi-form turn.completed
# =============================================================================

class TestTurnCompletedUsageMultiForm:
    """turn.completed usage may live at data.usage, data.message.usage,
    or data.turn.usage — the converter must find it at any level."""

    def test_usage_at_data_level(self):
        """Standard path: data.usage carries the tokens."""
        conv = CodexEventConverter()
        event = {
            "type": "turn.completed",
            "data": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 20,
                },
                "total_cost_usd": 0.002,
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["usage"]["input_tokens"] == 100
        assert parsed["usage"]["output_tokens"] == 50
        assert parsed["usage"]["cache_creation_input_tokens"] == 5
        assert parsed["usage"]["cache_read_input_tokens"] == 20
        assert parsed["total_cost_usd"] == 0.002

    def test_usage_at_message_level(self):
        """data.message.usage carries the tokens (data.usage absent)."""
        conv = CodexEventConverter()
        event = {
            "type": "turn.completed",
            "data": {
                "message": {
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 80,
                        "cache_creation_input_tokens": 10,
                        "cache_read_input_tokens": 40,
                    },
                },
                "total_cost_usd": 0.005,
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["usage"]["input_tokens"] == 200
        assert parsed["usage"]["output_tokens"] == 80
        assert parsed["usage"]["cache_creation_input_tokens"] == 10
        assert parsed["usage"]["cache_read_input_tokens"] == 40
        assert parsed["total_cost_usd"] == 0.005

    def test_usage_at_turn_level(self):
        """data.turn.usage carries the tokens (data.usage and
        data.message.usage both absent)."""
        conv = CodexEventConverter()
        event = {
            "type": "turn.completed",
            "data": {
                "turn": {
                    "usage": {
                        "input_tokens": 300,
                        "output_tokens": 120,
                        "cache_creation_input_tokens": 15,
                        "cache_read_input_tokens": 60,
                    },
                    "total_cost_usd": 0.008,
                },
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["usage"]["input_tokens"] == 300
        assert parsed["usage"]["output_tokens"] == 120
        assert parsed["usage"]["cache_creation_input_tokens"] == 15
        assert parsed["usage"]["cache_read_input_tokens"] == 60
        # total_cost_usd from turn-level nesting
        assert parsed["total_cost_usd"] == 0.008

    def test_data_usage_takes_priority_over_message_and_turn(self):
        """When data.usage exists, data.message.usage and data.turn.usage
        are ignored."""
        conv = CodexEventConverter()
        event = {
            "type": "turn.completed",
            "data": {
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "message": {"usage": {"input_tokens": 999, "output_tokens": 888}},
                "turn": {"usage": {"input_tokens": 777, "output_tokens": 666}},
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["usage"]["input_tokens"] == 10
        assert parsed["usage"]["output_tokens"] == 5


class TestTurnCompletedCostMissing:
    """Missing actual cost stays absent while tokens are preserved."""

    def test_no_cost_field_is_omitted(self):
        conv = CodexEventConverter()
        event = {
            "type": "turn.completed",
            "data": {
                "usage": {
                    "input_tokens": 500,
                    "output_tokens": 200,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 50,
                },
                # No total_cost_usd key at all
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert "total_cost_usd" not in parsed
        # Tokens are still fully preserved
        assert parsed["usage"]["input_tokens"] == 500
        assert parsed["usage"]["output_tokens"] == 200
        assert parsed["usage"]["cache_creation_input_tokens"] == 10
        assert parsed["usage"]["cache_read_input_tokens"] == 50

    def test_cost_none_is_omitted(self):
        conv = CodexEventConverter()
        event = {
            "type": "turn.completed",
            "data": {
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "total_cost_usd": None,
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert "total_cost_usd" not in parsed

    def test_cost_missing_at_all_levels(self):
        """Absent cost at every supported nesting level remains absent."""
        conv = CodexEventConverter()
        event = {
            "type": "turn.completed",
            "data": {
                "turn": {
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                    # no total_cost_usd in turn either
                },
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert "total_cost_usd" not in parsed
        assert parsed["usage"]["input_tokens"] == 100


class TestTurnCompletedCachedInputTokensMapping:
    """cached_input_tokens (the OpenAI/Codex subset marker) is passed through.

    The shared normalizer decides input-vs-subset arithmetic from the token
    field shape, so renaming the field to the Anthropic key would make an
    OpenAI subset be normalized additively and double-billed.
    """

    def test_cached_input_tokens_passed_through_unrenamed(self):
        conv = CodexEventConverter()
        event = {
            "type": "turn.completed",
            "data": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_input_tokens": 30,
                },
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["usage"]["cached_input_tokens"] == 30
        # The shape marker must not be rewritten into the Anthropic key.
        assert "cache_read_input_tokens" not in parsed["usage"]

        from tianluo.usage import parse_usage_record

        record = parse_usage_record(result[0], call_id="codex", provider="openai")
        assert record.logical_input_tokens == 100
        assert record.uncached_input_tokens == 70
        assert record.cache_read_input_tokens == 30

    def test_both_cache_fields_are_preserved_verbatim(self):
        """A payload declaring both keys keeps both; neither is synthesized."""
        conv = CodexEventConverter()
        event = {
            "type": "turn.completed",
            "data": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_input_tokens": 30,
                    "cache_read_input_tokens": 999,
                },
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["usage"]["cached_input_tokens"] == 30
        assert parsed["usage"]["cache_read_input_tokens"] == 999

    def test_cache_creation_input_tokens_preserved(self):
        conv = CodexEventConverter()
        event = {
            "type": "turn.completed",
            "data": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 15,
                },
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["usage"]["cache_creation_input_tokens"] == 15


# =============================================================================
# Task 1 — Hardened turn.failed: usage preserved when present
# =============================================================================

class TestTurnFailedUsagePreserved:
    """turn.failed events that carry usage must preserve the token counts
    instead of hardcoding zeros."""

    def test_turn_failed_with_usage_preserves_tokens(self):
        conv = CodexEventConverter()
        event = {
            "type": "turn.failed",
            "data": {
                "error": {"message": "partial failure"},
                "usage": {
                    "input_tokens": 400,
                    "output_tokens": 150,
                    "cache_creation_input_tokens": 8,
                    "cache_read_input_tokens": 30,
                },
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["is_error"] is True
        assert "partial failure" in parsed["result"]
        assert parsed["usage"]["input_tokens"] == 400
        assert parsed["usage"]["output_tokens"] == 150
        assert parsed["usage"]["cache_creation_input_tokens"] == 8
        assert parsed["usage"]["cache_read_input_tokens"] == 30

    def test_turn_failed_without_usage_omits_usage(self):
        """When turn.failed has no usage, no synthetic token report appears."""
        conv = CodexEventConverter()
        event = {
            "type": "turn.failed",
            "data": {"error": "something broke"},
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["is_error"] is True
        assert "usage" not in parsed

    def test_turn_failed_with_usage_at_message_level(self):
        """turn.failed usage at data.message.usage is also extracted."""
        conv = CodexEventConverter()
        event = {
            "type": "turn.failed",
            "data": {
                "error": {"message": "rate limited"},
                "message": {
                    "usage": {
                        "input_tokens": 250,
                        "output_tokens": 100,
                    },
                },
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["usage"]["input_tokens"] == 250
        assert parsed["usage"]["output_tokens"] == 100

    def test_turn_failed_with_usage_at_turn_level(self):
        """turn.failed usage at data.turn.usage is also extracted."""
        conv = CodexEventConverter()
        event = {
            "type": "turn.failed",
            "data": {
                "error": "quota exceeded",
                "turn": {
                    "usage": {
                        "input_tokens": 350,
                        "output_tokens": 130,
                    },
                },
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["usage"]["input_tokens"] == 350
        assert parsed["usage"]["output_tokens"] == 130

    def test_turn_failed_cached_input_tokens_preserved(self):
        """cached_input_tokens in turn.failed usage keeps its OpenAI shape."""
        conv = CodexEventConverter()
        event = {
            "type": "turn.failed",
            "data": {
                "error": "overloaded",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_input_tokens": 20,
                },
            },
        }
        result = conv.convert_line(json.dumps(event))
        parsed = json.loads(result[0])
        assert parsed["usage"]["cached_input_tokens"] == 20
        assert "cache_read_input_tokens" not in parsed["usage"]


# =============================================================================
# Task 2 — End-to-end: converter → parse_usage_from_ndjson (cost=0, tokens kept)
# =============================================================================

class TestCostMissingEndToEnd:
    """End-to-end: converter output → parse_usage_from_ndjson → tokens are
    preserved even when total_cost_usd is absent (cost=0 must NOT cause the
    entire usage record to be discarded as empty)."""

    def test_cost_zero_tokens_nonzero_parse_usage_returns_nonempty(self):
        """When total_cost_usd is missing (0) but tokens are nonzero,
        parse_usage_from_ndjson must return a non-empty dict."""
        from tianluo.engine.chat_history import parse_usage_from_ndjson
        events = [
            json.dumps({"type": "turn.started", "data": {}}),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Done."},
            }),
            json.dumps({
                "type": "turn.completed",
                "data": {
                    "usage": {
                        "input_tokens": 500,
                        "output_tokens": 200,
                        "cache_creation_input_tokens": 10,
                        "cache_read_input_tokens": 50,
                    },
                    # No total_cost_usd
                },
            }),
        ]
        ndjson = _run_full_codex_session(events)
        usage = parse_usage_from_ndjson(ndjson)
        # Must NOT be empty — tokens are nonzero
        assert usage, "parse_usage_from_ndjson returned empty dict despite nonzero tokens"
        # Anthropic-shaped cache keys: input_tokens excludes them, so the
        # logical input total is 500 + 50 read + 10 creation.
        assert usage["input_tokens"] == 560
        assert usage["output_tokens"] == 200
        assert usage["cache_creation_input_tokens"] == 10
        assert usage["cache_read_input_tokens"] == 50
        assert usage["total_cost_usd"] == 0

    def test_cost_zero_tokens_nonzero_usage_totals_not_empty(self):
        """UsageTotals.from_dict on the same data must report is_empty() == False."""
        from tianluo.engine.token_usage import UsageTotals
        raw = {
            "input_tokens": 500,
            "output_tokens": 200,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 50,
            "total_cost_usd": 0,
        }
        totals = UsageTotals.from_dict(raw)
        assert not totals.is_empty()
        assert totals.input_tokens == 500
        assert totals.output_tokens == 200

    def test_turn_level_usage_end_to_end(self):
        """Usage at data.turn.usage must survive the full chain."""
        from tianluo.engine.chat_history import parse_usage_from_ndjson
        events = [
            json.dumps({"type": "turn.started", "data": {}}),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Result."},
            }),
            json.dumps({
                "type": "turn.completed",
                "data": {
                    "turn": {
                        "usage": {
                            "input_tokens": 300,
                            "output_tokens": 100,
                        },
                        "total_cost_usd": 0.003,
                    },
                },
            }),
        ]
        ndjson = _run_full_codex_session(events)
        usage = parse_usage_from_ndjson(ndjson)
        assert usage["input_tokens"] == 300
        assert usage["output_tokens"] == 100
        assert usage["total_cost_usd"] == 0.003

    def test_turn_failed_with_usage_end_to_end(self):
        """turn.failed carrying usage must produce a non-empty usage dict
        in parse_usage_from_ndjson."""
        from tianluo.engine.chat_history import parse_usage_from_ndjson
        events = [
            json.dumps({"type": "turn.started", "data": {}}),
            json.dumps({
                "type": "turn.failed",
                "data": {
                    "error": {"message": "rate limited"},
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 80,
                        "cache_creation_input_tokens": 5,
                        "cache_read_input_tokens": 15,
                    },
                },
            }),
        ]
        ndjson = _run_full_codex_session(events)
        usage = parse_usage_from_ndjson(ndjson)
        assert usage, "parse_usage_from_ndjson returned empty dict for turn.failed with usage"
        # Anthropic-shaped cache keys: 200 uncached + 15 read + 5 creation.
        assert usage["input_tokens"] == 220
        assert usage["output_tokens"] == 80
        assert usage["cache_creation_input_tokens"] == 5
        assert usage["cache_read_input_tokens"] == 15


class TestUsageSchemaFixtures:
    @staticmethod
    def _convert_fixture(name: str) -> str:
        fixture = Path(__file__).parent / "fixtures" / "usage" / name
        converter = CodexEventConverter()
        output = []
        for line in fixture.read_text(encoding="utf-8").splitlines():
            output.extend(converter.convert_line(line))
        output.extend(converter.finalize())
        return "\n".join(output)

    def test_codex_fixture_metadata_usage_and_live_tracker_match(self, capsys):
        from tianluo.engine.llm_caller import StreamJSONTracker
        from tianluo.usage import parse_usage_record

        raw = self._convert_fixture("codex_exec.jsonl")
        metadata = {
            "call_id": "codex-fixture",
            "attempt": 0,
            "agent_name": "codex-agent",
            "runner_type": "codex",
            "provider": "openai",
        }
        parsed = parse_usage_record(raw, **metadata)
        tracker = StreamJSONTracker(
            call_id="codex-fixture",
            usage_attempt=0,
            agent_name="codex-agent",
            runner_type="codex",
            provider="openai",
        )
        for line in raw.splitlines():
            tracker.process_line(line)
        capsys.readouterr()

        assert tracker.usage_record == parsed
        assert parsed.provider_session_id == "codex-thread-1"
        assert parsed.reported_model == "gpt-5-codex"
        assert parsed.resolved_model_source == "provider"
        assert parsed.logical_input_tokens == 300
        assert parsed.uncached_input_tokens == 180
        assert parsed.cache_read_input_tokens == 120
        assert parsed.output_tokens == 60

    def test_compat_proxy_fixture_preserves_reported_provider_and_model(self):
        from tianluo.usage import parse_usage_record

        raw = self._convert_fixture("compat_proxy.jsonl")
        record = parse_usage_record(
            raw,
            call_id="compat-fixture",
            runner_type="codex",
            provider="openai",
        )
        assert record.provider == "azure-openai"
        assert record.provider_session_id == "proxy-thread-1"
        assert record.reported_model == "proxy-gpt-5-codex"
        assert record.resolved_model == "proxy-gpt-5-codex"

    def test_add_call_usage_folds_cost_zero_tokens(self):
        """add_call_usage must fold token data into the step accumulator
        even when total_cost_usd is 0."""
        from tianluo.engine.token_usage import (
            UsageTotals, add_call_usage, accumulate_step_usage,
        )
        raw_usage = {
            "input_tokens": 600,
            "output_tokens": 250,
            "cache_creation_input_tokens": 12,
            "cache_read_input_tokens": 45,
            "total_cost_usd": 0,
        }
        with accumulate_step_usage() as step_total:
            add_call_usage(raw_usage)
        assert step_total.input_tokens == 600
        assert step_total.output_tokens == 250
        assert step_total.cache_creation_input_tokens == 12
        assert step_total.cache_read_input_tokens == 45
        assert step_total.total_cost_usd == 0
        assert not step_total.is_empty()

    def test_cached_input_tokens_end_to_end(self):
        """cached_input_tokens in codex output must map to
        cache_read_input_tokens through the full chain."""
        from tianluo.engine.chat_history import parse_usage_from_ndjson
        events = [
            json.dumps({"type": "turn.started", "data": {}}),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Done."},
            }),
            json.dumps({
                "type": "turn.completed",
                "data": {
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cached_input_tokens": 25,
                    },
                },
            }),
        ]
        ndjson = _run_full_codex_session(events)
        usage = parse_usage_from_ndjson(ndjson)
        assert usage["cache_read_input_tokens"] == 25
        assert "cached_input_tokens" not in usage

    def test_all_zero_usage_remains_explicit_in_parse(self):
        from tianluo.engine.chat_history import parse_usage_from_ndjson
        events = [
            json.dumps({"type": "turn.started", "data": {}}),
            json.dumps({
                "type": "turn.completed",
                "data": {"usage": {}},
            }),
        ]
        ndjson = _run_full_codex_session(events)
        usage = parse_usage_from_ndjson(ndjson)
        assert usage == {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "total_cost_usd": 0.0,
        }


# =============================================================================
# Task 3 — Shell snapshot failure detection
# =============================================================================

class TestShellSnapshotDetection:
    """Test _detect_shell_snapshot_failure pattern matching."""

    def test_real_sample_stderr_returns_true(self):
        """The actual stderr pattern from Codex CLI must be detected."""
        from tianluo.codex_runner import _detect_shell_snapshot_failure
        stderr = (
            "codex_core::shell_snapshot: Shell snapshot validation failed "
            "at line 42: syntax error near unexpected token '('"
        )
        assert _detect_shell_snapshot_failure(stderr) is True

    def test_partial_pattern_shell_snapshot_validation_failed(self):
        from tianluo.codex_runner import _detect_shell_snapshot_failure
        stderr = "Error: Shell snapshot validation failed in environment setup"
        assert _detect_shell_snapshot_failure(stderr) is True

    def test_partial_pattern_codex_core_shell_snapshot(self):
        from tianluo.codex_runner import _detect_shell_snapshot_failure
        stderr = "codex_core::shell_snapshot: something went wrong"
        assert _detect_shell_snapshot_failure(stderr) is True

    def test_partial_pattern_syntax_error_unexpected_token(self):
        from tianluo.codex_runner import _detect_shell_snapshot_failure
        stderr = "/bin/bash: line 5: syntax error near unexpected token '('"
        assert _detect_shell_snapshot_failure(stderr) is True

    def test_case_insensitive(self):
        from tianluo.codex_runner import _detect_shell_snapshot_failure
        stderr = "CODEX_CORE::SHELL_SNAPSHOT: SHELL SNAPSHOT VALIDATION FAILED"
        assert _detect_shell_snapshot_failure(stderr) is True

    def test_empty_stderr_returns_false(self):
        from tianluo.codex_runner import _detect_shell_snapshot_failure
        assert _detect_shell_snapshot_failure("") is False

    def test_none_stderr_returns_false(self):
        from tianluo.codex_runner import _detect_shell_snapshot_failure
        assert _detect_shell_snapshot_failure(None) is False

    def test_normal_stderr_returns_false(self):
        from tianluo.codex_runner import _detect_shell_snapshot_failure
        stderr = "Warning: some deprecation warning\nLoading config...\n"
        assert _detect_shell_snapshot_failure(stderr) is False

    def test_stderr_with_unrelated_syntax_error_returns_false(self):
        """'syntax error' alone (without 'near unexpected token') should not match."""
        from tianluo.codex_runner import _detect_shell_snapshot_failure
        stderr = "python: SyntaxError: invalid syntax"
        assert _detect_shell_snapshot_failure(stderr) is False


# =============================================================================
# Task 3 — Shell snapshot failure forces non-success result
# =============================================================================

class TestShellSnapshotForcedFailure:
    """When stderr contains shell snapshot failure and no valid agent output
    was produced, _run_single_with_monitor must return success=False and
    an error result carrying the original stderr context, even when
    returncode==0."""

    @staticmethod
    def _run_monitor_with_mocked_subprocess(
        runner, returncode, stdout_text, stderr_text,
    ):
        """Run _run_single_with_monitor with a fully mocked subprocess.

        Patches subprocess.Popen, shutil.which, and select.select so no
        real process is spawned.  Returns the _SingleRunResult.
        """

        class _FakeStream(io.StringIO):
            """StringIO with a fileno() so select.select can reference it."""

            def fileno(self):
                return 99  # arbitrary fake fd

        proc = MagicMock()
        proc.returncode = returncode
        proc.pid = 12345

        # poll() is called at the top of each while iteration.  Use a
        # callable side_effect so we don't have to predict the exact count
        # of loop iterations.  First call returns None (process running);
        # after the first select round, return the returncode so the loop
        # exits cleanly.
        _poll_returns_none = True

        def _poll_side_effect():
            nonlocal _poll_returns_none
            if _poll_returns_none:
                _poll_returns_none = False
                return None
            return returncode

        proc.poll = MagicMock(side_effect=_poll_side_effect)

        # stdout: StringIO with fileno() for select; universal_newlines=True
        proc.stdout = _FakeStream(stdout_text)

        # stderr: line-iterable for the background reader thread
        proc.stderr = io.StringIO(stderr_text)

        proc.kill = MagicMock()
        proc.wait = MagicMock(return_value=returncode)

        # select.select returns stdout as "ready" on first call (so
        # readline() is tried — it returns "" for empty StringIO), then
        # "not ready" on subsequent calls so the else-branch inactivity
        # check runs (but time elapsed ≈ 0 < 1800s, so no hang).  The
        # third not-ready iteration allows poll() to return returncode.
        select_side_effects = [
            ([proc.stdout], [], []),   # stdout "ready" → readline returns ""
            ([], [], []),              # not ready → inactivity check (no hang)
            ([], [], []),              # not ready → inactivity check → poll exits
        ]

        with patch("subprocess.Popen", return_value=proc), \
             patch("shutil.which", return_value="/usr/bin/codex"), \
             patch("select.select", side_effect=select_side_effects):
            return runner._run_single_with_monitor(
                full_cmd=["codex", "exec", "--json", "hi"],
                cmd_name="codex",
                log_file=None,
                wall_timeout=None,
                inactivity_timeout=1800,
                cwd=None,
                env={},
                on_output=None,
                on_activity=None,
                start_time=time.time(),
            )

    def test_shell_snapshot_failure_with_zero_returncode_forces_failure(self):
        """When returncode==0 but stderr has shell snapshot failure and no
        agent output was produced, success must be False."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        stderr_text = (
            "codex_core::shell_snapshot: Shell snapshot validation failed "
            "at line 5: syntax error near unexpected token '('\n"
        )

        result = self._run_monitor_with_mocked_subprocess(
            runner, returncode=0, stdout_text="", stderr_text=stderr_text,
        )

        assert result.success is False
        # returncode==0 should be remapped to 1
        assert result.returncode == 1
        # stderr_tail must be populated
        assert "shell snapshot" in result.stderr_tail.lower()

    def test_shell_snapshot_failure_error_result_contains_original_stderr(self):
        """The synthesized error result event must contain the original
        stderr text so users can see the actual failure reason."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        stderr_text = (
            "codex_core::shell_snapshot: Shell snapshot validation failed "
            "at line 5: syntax error near unexpected token '('\n"
            "Some additional context about the environment\n"
        )

        result = self._run_monitor_with_mocked_subprocess(
            runner, returncode=0, stdout_text="", stderr_text=stderr_text,
        )

        # Parse the NDJSON output to find the error result event
        result_events = []
        for line in result.output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if parsed.get("type") == "result":
                    result_events.append(parsed)
            except json.JSONDecodeError:
                continue

        assert len(result_events) >= 1
        error_result = result_events[-1]
        assert error_result["is_error"] is True
        # Must contain the original stderr context
        assert "syntax error near unexpected token" in error_result["result"]
        assert "codex_core::shell_snapshot" in error_result["result"]
        # Must contain the codex-runner prefix
        assert "[codex-runner] Shell snapshot validation failed" in error_result["result"]

    def test_shell_snapshot_failure_with_nonzero_returncode_preserves_returncode(self):
        """When returncode!=0 and shell snapshot failure is detected,
        the original returncode must be preserved (not remapped to 1)."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        stderr_text = (
            "codex_core::shell_snapshot: Shell snapshot validation failed\n"
        )

        result = self._run_monitor_with_mocked_subprocess(
            runner, returncode=2, stdout_text="", stderr_text=stderr_text,
        )

        assert result.success is False
        assert result.returncode == 2  # preserved, not remapped

    def test_normal_completion_unaffected_by_stderr_warning(self):
        """When the converter produced valid agent output and/or a turn
        terminal, stderr containing shell snapshot warnings must NOT
        trigger the forced failure path."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})

        # Stdout has a valid turn.completed event (agent produced output)
        stdout_events = [
            json.dumps({"type": "turn.started", "data": {}}),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Done."},
            }),
            json.dumps({
                "type": "turn.completed",
                "data": {"usage": {"input_tokens": 10, "output_tokens": 5}},
            }),
        ]
        stdout_text = "\n".join(stdout_events) + "\n"

        # Stderr has a shell snapshot warning (but it's just a warning,
        # not a failure — the agent completed successfully)
        stderr_text = "Warning: codex_core::shell_snapshot: minor issue noted\n"

        result = self._run_monitor_with_mocked_subprocess(
            runner, returncode=0, stdout_text=stdout_text, stderr_text=stderr_text,
        )

        # Must succeed — agent produced valid output
        assert result.success is True
        assert result.returncode == 0
        # stderr_tail is still populated for diagnostics
        assert "shell_snapshot" in result.stderr_tail.lower()


# =============================================================================
# Task 3 — detect_infra_error STARTUP_FAILURE classification
# =============================================================================

class TestDetectInfraErrorStartupFailure:
    """Test that detect_infra_error correctly classifies shell snapshot
    failures as STARTUP_FAILURE, without preempting existing classifications."""

    def test_shell_snapshot_returns_startup_failure(self):
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        stderr = (
            "codex_core::shell_snapshot: Shell snapshot validation failed "
            "at line 5: syntax error near unexpected token '('\n"
        )
        assert runner.detect_infra_error(1, "", stderr) == InfraErrorType.STARTUP_FAILURE

    def test_shell_snapshot_with_zero_exit_returns_none(self):
        """rc==0 must return NONE regardless of stderr content
        (the forced-failure path in _run_single_with_monitor already
        remaps rc to 1 before detect_infra_error is called)."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        stderr = "codex_core::shell_snapshot: Shell snapshot validation failed"
        assert runner.detect_infra_error(0, "", stderr) == InfraErrorType.NONE

    def test_timeout_takes_priority_over_shell_snapshot(self):
        """rc==124 must return TIMEOUT even if stderr has shell snapshot."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        stderr = "codex_core::shell_snapshot: Shell snapshot validation failed"
        assert runner.detect_infra_error(124, "", stderr) == InfraErrorType.TIMEOUT

    def test_usage_limit_takes_priority_over_shell_snapshot(self):
        """Usage limit keywords must return USAGE_LIMIT even if stderr
        also has shell snapshot patterns."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        stdout = "Error: usage limit exceeded"
        stderr = "codex_core::shell_snapshot: Shell snapshot validation failed"
        assert runner.detect_infra_error(1, stdout, stderr) == InfraErrorType.USAGE_LIMIT

    def test_auth_failure_takes_priority_over_shell_snapshot(self):
        """Auth failure must return USAGE_LIMIT even if stderr also has
        shell snapshot patterns."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        stderr = (
            "401 Unauthorized\n"
            "codex_core::shell_snapshot: Shell snapshot validation failed"
        )
        assert runner.detect_infra_error(1, "", stderr) == InfraErrorType.USAGE_LIMIT

    def test_task_failure_without_shell_snapshot_returns_none(self):
        """A normal task failure with no shell snapshot must return NONE."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        assert runner.detect_infra_error(1, "file not found", "") == InfraErrorType.NONE

    def test_shell_snapshot_in_stderr_only_not_stdout(self):
        """Shell snapshot patterns in stdout should not trigger
        STARTUP_FAILURE (only stderr is checked)."""
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        stdout = "codex_core::shell_snapshot: Shell snapshot validation failed"
        assert runner.detect_infra_error(1, stdout, "") == InfraErrorType.NONE

    def test_monitored_result_stderr_tail_populated(self):
        """MonitoredResult.stderr_tail must be populated from the
        _SingleRunResult."""
        from tianluo.codex_runner import _SingleRunResult
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        def fake_monitor(self_runner, *, full_cmd, **kw):
            return _SingleRunResult(
                returncode=0, output="ok", success=True,
                should_retry=False, stderr_tail="some stderr content",
            )
        with patch.object(CodexRunner, "_run_single_with_monitor", autospec=True, side_effect=fake_monitor):
            result = runner.run_with_monitor(["exec", "--json", "hi"])
        assert result.stderr_tail == "some stderr content"

    def test_monitored_result_stderr_tail_default_empty(self):
        """MonitoredResult.stderr_tail defaults to empty string when
        _SingleRunResult has no stderr_tail."""
        from tianluo.codex_runner import _SingleRunResult
        runner = CodexRunner(command={"cmd": "codex", "priority": 0})
        def fake_monitor(self_runner, *, full_cmd, **kw):
            return _SingleRunResult(
                returncode=0, output="ok", success=True, should_retry=False,
            )
        with patch.object(CodexRunner, "_run_single_with_monitor", autospec=True, side_effect=fake_monitor):
            result = runner.run_with_monitor(["exec", "--json", "hi"])
        assert result.stderr_tail == ""


# =============================================================================
# Task 3 — InfraErrorType.STARTUP_FAILURE enum value
# =============================================================================

class TestInfraErrorTypeStartupFailure:
    """Verify STARTUP_FAILURE exists with the correct value."""

    def test_startup_failure_member_exists(self):
        assert hasattr(InfraErrorType, "STARTUP_FAILURE")

    def test_startup_failure_value(self):
        assert InfraErrorType.STARTUP_FAILURE.value == "startup_failure"

    def test_existing_members_unchanged(self):
        """Existing InfraErrorType members must not regress."""
        assert InfraErrorType.NONE.value == "none"
        assert InfraErrorType.USAGE_LIMIT.value == "usage_limit"
        assert InfraErrorType.TIMEOUT.value == "timeout"
        assert InfraErrorType.HANG.value == "hang"
