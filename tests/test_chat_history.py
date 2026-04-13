"""Tests for the chat history system."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from se3.engine.chat_history import (
    ChatMessage,
    ChatSession,
    extract_assistant_text,
    format_history_for_retry,
    get_detailed_json,
    get_flow_history,
    get_step_history,
    list_flows,
    record_prompt,
    record_response,
    render_session_detailed,
    render_session_text,
    segment_prompt,
)


@pytest.fixture
def tmp_project(tmp_path):
    """Create a temporary project directory with se3/history structure."""
    (tmp_path / "se3" / "history").mkdir(parents=True)
    return tmp_path


# --- ChatMessage serialization ---

class TestChatMessage:
    def test_to_dict(self):
        msg = ChatMessage(
            role="user",
            content="Hello",
            raw_json=[],
            timestamp="2026-01-01T00:00:00",
            step_type="analyze",
            attempt=0,
        )
        d = msg.to_dict()
        assert d["role"] == "user"
        assert d["content"] == "Hello"
        assert d["attempt"] == 0

    def test_from_dict(self):
        d = {
            "role": "assistant",
            "content": "Response text",
            "raw_json": {"type": "assistant"},
            "timestamp": "2026-01-01T00:00:00",
            "step_type": "plan",
            "attempt": 1,
        }
        msg = ChatMessage.from_dict(d)
        assert msg.role == "assistant"
        assert msg.content == "Response text"
        assert msg.attempt == 1

    def test_roundtrip(self):
        msg = ChatMessage(
            role="user",
            content="Test prompt",
            raw_json=[],
            timestamp="2026-02-27T12:00:00",
            step_type="plan",
            attempt=0,
        )
        d = msg.to_dict()
        msg2 = ChatMessage.from_dict(d)
        assert msg == msg2

    def test_roundtrip_with_dict_raw_json(self):
        """Test that raw_json as dict survives roundtrip."""
        raw_dict = {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}}
        msg = ChatMessage(
            role="assistant",
            content="Hello",
            raw_json=raw_dict,
            timestamp="2026-02-27T12:00:00",
            step_type="analyze",
            attempt=0,
        )
        d = msg.to_dict()
        assert d["raw_json"] == raw_dict
        msg2 = ChatMessage.from_dict(d)
        assert msg2.raw_json == raw_dict


# --- NDJSON parsing ---

class TestExtractAssistantText:
    def test_simple_text(self):
        ndjson = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Hello world"}]
            }
        })
        result = extract_assistant_text(ndjson)
        assert result == "Hello world"

    def test_multiple_text_chunks(self):
        lines = []
        for text in ["First chunk", "Second chunk"]:
            lines.append(json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": text}]
                }
            }))
        ndjson = "\n".join(lines)
        result = extract_assistant_text(ndjson)
        assert "First chunk" in result
        assert "Second chunk" in result

    def test_tool_use(self):
        ndjson = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Let me check"},
                    {"type": "tool_use", "id": "tu-1", "name": "Read", "input": {"file_path": "foo.py"}},
                ]
            }
        })
        result = extract_assistant_text(ndjson)
        assert "Let me check" in result
        # New format uses per-tool formatter: "[Read: foo.py]"
        assert "[Read:" in result

    def test_tool_result(self):
        # First emit a tool_use so the id->name mapping exists
        tool_use_line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "123", "name": "Read", "input": {"file_path": "test.py"}},
                ]
            }
        })
        result_line = json.dumps({
            "type": "tool_result",
            "result": {"toolUseId": "123", "content": "file contents here"}
        })
        ndjson = tool_use_line + "\n" + result_line
        result = extract_assistant_text(ndjson)
        # New format: "[Read ✓ (1 lines)]" instead of "[Tool Result: ...]"
        assert "[Read" in result

    def test_empty_input(self):
        assert extract_assistant_text("") == ""
        assert extract_assistant_text("   ") == ""

    def test_invalid_json_lines(self):
        ndjson = "not json\n" + json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "valid"}]}
        })
        result = extract_assistant_text(ndjson)
        assert "valid" in result

    def test_mixed_content(self):
        """Test assistant message with text that is itself JSON (LLM output)."""
        json_output = '{"task_type": "feature", "scope": "auth"}'
        ndjson = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": json_output}]
            }
        })
        result = extract_assistant_text(ndjson)
        # The JSON content should be preserved as-is
        assert '"task_type"' in result
        assert '"feature"' in result

    def test_edit_tool_use_semantic_preview(self):
        """Edit tool_use should show file path and diff info, not generic format."""
        ndjson = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Applying fix"},
                    {
                        "type": "tool_use",
                        "id": "tu-edit-1",
                        "name": "Edit",
                        "input": {
                            "file_path": "src/app.py",
                            "old_string": "x = 1",
                            "new_string": "x = 2\ny = 3",
                        },
                    },
                ]
            }
        })
        result = extract_assistant_text(ndjson)
        assert "Applying fix" in result
        assert "[Edit:" in result
        assert "src/app.py" in result

    def test_edit_tool_result_semantic_preview(self):
        """Edit tool_result should use per-tool formatter via id→name mapping."""
        tool_use_line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-edit-2",
                    "name": "Edit",
                    "input": {"file_path": "a.py", "old_string": "a", "new_string": "b"},
                }]
            }
        })
        result_line = json.dumps({
            "type": "tool_result",
            "result": {
                "toolUseId": "tu-edit-2",
                "content": "\u2713 edited a.py",
                "isError": False,
            }
        })
        ndjson = tool_use_line + "\n" + result_line
        result = extract_assistant_text(ndjson)
        assert "[Edit" in result
        assert "\u2713" in result

    def test_write_tool_use_semantic_preview(self):
        """Write tool_use should show file path and line count."""
        ndjson = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-write-1",
                    "name": "Write",
                    "input": {
                        "file_path": "new_file.py",
                        "content": "line1\nline2\nline3",
                    },
                }]
            }
        })
        result = extract_assistant_text(ndjson)
        assert "[Write:" in result
        assert "new_file.py" in result
        assert "3 lines" in result

    def test_write_tool_result_semantic_preview(self):
        """Write tool_result should use per-tool formatter."""
        tool_use_line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-write-2",
                    "name": "Write",
                    "input": {"file_path": "out.py", "content": "x"},
                }]
            }
        })
        result_line = json.dumps({
            "type": "tool_result",
            "result": {
                "toolUseId": "tu-write-2",
                "content": "Created out.py",
                "isError": False,
            }
        })
        ndjson = tool_use_line + "\n" + result_line
        result = extract_assistant_text(ndjson)
        assert "[Write" in result
        assert "\u2713" in result


# --- Record and retrieve ---

class TestRecordAndRetrieve:
    def test_record_prompt(self, tmp_project):
        record_prompt(tmp_project, "flow1", "step1", "analyze", "What is this?", 0)

        session = get_step_history(tmp_project, "flow1", "step1")
        assert session is not None
        assert len(session.messages) == 1
        assert session.messages[0].role == "user"
        assert session.messages[0].content == "What is this?"

    def test_record_response(self, tmp_project):
        ndjson_dict = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "It is a test"}]}
        }
        ndjson = json.dumps(ndjson_dict)
        record_response(tmp_project, "flow1", "step1", "analyze", ndjson, 0)

        session = get_step_history(tmp_project, "flow1", "step1")
        assert session is not None
        assert len(session.messages) == 1
        assert session.messages[0].role == "assistant"
        assert session.messages[0].content == "It is a test"
        # raw_json is now a list[dict], not a string
        assert session.messages[0].raw_json == [ndjson_dict]

    def test_full_conversation(self, tmp_project):
        record_prompt(tmp_project, "flow1", "step1", "analyze", "Analyze this", 0)
        ndjson = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Analysis result"}]}
        })
        record_response(tmp_project, "flow1", "step1", "analyze", ndjson, 0)

        session = get_step_history(tmp_project, "flow1", "step1")
        assert session is not None
        assert len(session.messages) == 2
        assert session.messages[0].role == "user"
        assert session.messages[1].role == "assistant"
        assert session.step_type == "analyze"

    def test_multiple_attempts(self, tmp_project):
        # Attempt 0
        record_prompt(tmp_project, "flow1", "step1", "analyze", "Try 1", 0)
        record_response(tmp_project, "flow1", "step1", "analyze", "", 0)
        # Attempt 1
        record_prompt(tmp_project, "flow1", "step1", "analyze", "Try 2", 1)
        ndjson = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Success"}]}
        })
        record_response(tmp_project, "flow1", "step1", "analyze", ndjson, 1)

        session = get_step_history(tmp_project, "flow1", "step1")
        assert session is not None
        assert len(session.messages) == 4
        assert session.messages[2].attempt == 1

    def test_no_history(self, tmp_project):
        session = get_step_history(tmp_project, "nonexistent", "step1")
        assert session is None

    def test_record_response_with_header_lines(self, tmp_project):
        """Test that '=== Command:' header lines are skipped during parsing."""
        ndjson_dict = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Response with headers"}]}
        }
        # Simulate NDJSON with header lines (as produced by CLI runner)
        raw_ndjson = "=== Command: analyze ===\n" + json.dumps(ndjson_dict) + "\n=== End ==="
        record_response(tmp_project, "flow1", "step1", "analyze", raw_ndjson, 0)

        session = get_step_history(tmp_project, "flow1", "step1")
        assert session is not None
        assert session.messages[0].content == "Response with headers"
        # raw_json should contain the parsed dict, not be empty
        assert session.messages[0].raw_json == [ndjson_dict]

    def test_record_response_with_mixed_valid_invalid_lines(self, tmp_project):
        """Test that one bad JSON line doesn't prevent parsing other lines."""
        ndjson_dict = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Valid response"}]}
        }
        # Mix valid JSON with invalid lines
        raw_ndjson = f"not valid json\n{json.dumps(ndjson_dict)}\nalso not json"
        record_response(tmp_project, "flow1", "step1", "analyze", raw_ndjson, 0)

        session = get_step_history(tmp_project, "flow1", "step1")
        assert session is not None
        assert session.messages[0].content == "Valid response"
        # raw_json should still contain the valid parsed dict
        assert session.messages[0].raw_json == [ndjson_dict]


# --- Flow history ---

class TestFlowHistory:
    def test_get_flow_history(self, tmp_project):
        record_prompt(tmp_project, "flow1", "step_a", "analyze", "P1", 0)
        record_prompt(tmp_project, "flow1", "step_b", "plan", "P2", 0)

        sessions = get_flow_history(tmp_project, "flow1")
        assert len(sessions) == 2

    def test_list_flows(self, tmp_project):
        record_prompt(tmp_project, "flow1", "s1", "analyze", "P1", 0)
        record_prompt(tmp_project, "flow2", "s1", "analyze", "P2", 0)

        flows = list_flows(tmp_project)
        assert "flow1" in flows
        assert "flow2" in flows

    def test_list_flows_empty(self, tmp_project):
        flows = list_flows(tmp_project)
        assert flows == []


# --- Retry context formatting ---

class TestFormatHistoryForRetry:
    def test_format_with_history(self, tmp_project):
        record_prompt(tmp_project, "flow1", "step1", "analyze", "Original prompt", 0)
        ndjson = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Bad response"}]}
        })
        record_response(tmp_project, "flow1", "step1", "analyze", ndjson, 0)

        context = format_history_for_retry(tmp_project, "flow1", "step1")
        assert context is not None
        assert "Previous conversation context" in context
        assert "Original prompt" in context
        assert "Bad response" in context
        # Default mode is 'continue' — check for continuation instruction
        assert "continue from where" in context.lower()

    def test_format_no_history(self, tmp_project):
        context = format_history_for_retry(tmp_project, "flow1", "nonexistent")
        assert context is None

    def test_format_truncates_long_content(self, tmp_project):
        long_prompt = "x" * 5000
        record_prompt(tmp_project, "flow1", "step1", "analyze", long_prompt, 0)

        context = format_history_for_retry(tmp_project, "flow1", "step1")
        assert context is not None
        assert "[truncated]" in context
        assert len(context) < 5000


# --- Human-readable rendering ---

class TestRenderSessionText:
    def test_render_basic(self):
        session = ChatSession(
            flow_id="flow1",
            step_id="step1",
            step_type="analyze",
            messages=[
                ChatMessage(
                    role="user",
                    content="Analyze task",
                    raw_json=[],
                    timestamp="2026-01-01T12:00:00",
                    step_type="analyze",
                    attempt=0,
                ),
                ChatMessage(
                    role="assistant",
                    content="Analysis complete",
                    raw_json=[],
                    timestamp="2026-01-01T12:00:05",
                    step_type="analyze",
                    attempt=0,
                ),
            ],
        )
        text = render_session_text(session)
        assert "Step: analyze" in text
        assert "[User Prompt]" in text
        assert "[Assistant Response]" in text
        assert "Analyze task" in text

    def test_render_truncates_long_prompts(self):
        session = ChatSession(
            flow_id="flow1",
            step_id="step1",
            step_type="analyze",
            messages=[
                ChatMessage(
                    role="user",
                    content="x" * 1000,
                    raw_json=[],
                    timestamp="2026-01-01T12:00:00",
                    step_type="analyze",
                    attempt=0,
                ),
            ],
        )
        text = render_session_text(session, truncate_prompt=100)
        assert "[truncated]" in text

    def test_render_with_ndjson(self):
        ndjson_dict = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Here is the analysis"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "foo.py"}},
                ]
            }
        }
        session = ChatSession(
            flow_id="flow1",
            step_id="step1",
            step_type="analyze",
            messages=[
                ChatMessage(
                    role="assistant",
                    content="Here is the analysis",
                    raw_json=[ndjson_dict],
                    timestamp="2026-01-01T12:00:00",
                    step_type="analyze",
                    attempt=0,
                ),
            ],
        )
        text = render_session_text(session)
        assert "Here is the analysis" in text
        # Per-tool formatter: "[Read: foo.py]"
        assert "Read:" in text
        assert "foo.py" in text

    def test_render_edit_tool_semantic_preview(self):
        """Edit tool in history should show file path and diff info."""
        ndjson_dict = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Fixing the bug"},
                    {
                        "type": "tool_use",
                        "id": "tu-e1",
                        "name": "Edit",
                        "input": {
                            "file_path": "src/handler.py",
                            "old_string": "return None",
                            "new_string": "return result",
                        },
                    },
                ]
            }
        }
        session = ChatSession(
            flow_id="flow1",
            step_id="step1",
            step_type="implement",
            messages=[
                ChatMessage(
                    role="assistant",
                    content="Fixing the bug",
                    raw_json=[ndjson_dict],
                    timestamp="2026-01-01T12:00:00",
                    step_type="implement",
                    attempt=0,
                ),
            ],
        )
        text = render_session_text(session)
        assert "Fixing the bug" in text
        assert "Edit:" in text
        assert "src/handler.py" in text

    def test_render_write_tool_semantic_preview(self):
        """Write tool in history should show file path and line count."""
        ndjson_dict = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Creating the file"},
                    {
                        "type": "tool_use",
                        "id": "tu-w1",
                        "name": "Write",
                        "input": {
                            "file_path": "tests/test_new.py",
                            "content": "import pytest\n\ndef test_something():\n    pass\n",
                        },
                    },
                ]
            }
        }
        session = ChatSession(
            flow_id="flow1",
            step_id="step1",
            step_type="implement",
            messages=[
                ChatMessage(
                    role="assistant",
                    content="Creating the file",
                    raw_json=[ndjson_dict],
                    timestamp="2026-01-01T12:00:00",
                    step_type="implement",
                    attempt=0,
                ),
            ],
        )
        text = render_session_text(session)
        assert "Creating the file" in text
        assert "Write:" in text
        assert "tests/test_new.py" in text
        assert "4 lines" in text


# --- Prompt segmentation ---

class TestSegmentPrompt:
    def test_json_mode_wrapper(self):
        prompt = (
            "CRITICAL: You MUST respond with ONLY valid JSON.\n\n"
            "You are an expert software engineering assistant.\n"
            "Analyze the task."
        )
        segments = segment_prompt(prompt)
        assert len(segments) == 2
        assert segments[0]["title"] == "JSON Mode Instruction"
        assert segments[1]["title"] == "Step Instructions"

    def test_read_only_constraint(self):
        prompt = (
            "You are an expert.\n\n"
            "## Task Description\nDo something.\n\n"
            "READ-ONLY STEP CONSTRAINT\nDo not write files."
        )
        segments = segment_prompt(prompt)
        titles = [s["title"] for s in segments]
        assert "Step Instructions" in titles
        assert "Task Description" in titles
        assert "Read-Only Constraint" in titles

    def test_language_instruction(self):
        prompt = (
            "## Task Description\nDo something.\n\n"
            "IMPORTANT: You MUST respond in Chinese."
        )
        segments = segment_prompt(prompt)
        titles = [s["title"] for s in segments]
        assert "Language Instruction" in titles

    def test_generic_sections(self):
        prompt = (
            "## Task Description\nDo something.\n\n"
            "## Changes Made\nFile changed.\n\n"
            "## Test Results\nAll passed."
        )
        segments = segment_prompt(prompt)
        titles = [s["title"] for s in segments]
        assert "Task Description" in titles
        assert "Changes Made" in titles
        assert "Test Results" in titles

    def test_available_specifications(self):
        prompt = (
            "## Available Specifications\nbase, flow-engine\n\n"
            "## Relevant Specifications\n### base\nContent here."
        )
        segments = segment_prompt(prompt)
        titles = [s["title"] for s in segments]
        assert "Available Specifications" in titles
        assert "Relevant Specifications" in titles

    def test_additional_user_instruction(self):
        prompt = (
            "## Task Description\nDo something.\n\n"
            "[Additional user instruction]\nDo it this way."
        )
        segments = segment_prompt(prompt)
        titles = [s["title"] for s in segments]
        assert "Additional User Instruction" in titles

    def test_empty_prompt(self):
        segments = segment_prompt("")
        assert segments == []

    def test_plain_text_prompt(self):
        segments = segment_prompt("Just a simple prompt.")
        assert len(segments) == 1
        assert segments[0]["title"] == "Prompt"
        assert segments[0]["content"] == "Just a simple prompt."


# --- Detailed session rendering ---

class TestRenderSessionDetailed:
    def _make_session(self, messages=None):
        if messages is None:
            ndjson_dict = {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Thinking..."},
                        {"type": "tool_use", "id": "t1", "name": "Read",
                         "input": {"file_path": "foo.py"}},
                        {"type": "text", "text": "Final answer here."},
                    ]
                }
            }
            messages = [
                ChatMessage(
                    role="user",
                    content="## Task Description\nDo something.\n\nREAD-ONLY STEP CONSTRAINT\nNo writes.",
                    raw_json=[],
                    timestamp="2026-01-01T12:00:00",
                    step_type="analyze",
                    attempt=0,
                ),
                ChatMessage(
                    role="assistant",
                    content="Final answer here.",
                    raw_json=[ndjson_dict],
                    timestamp="2026-01-01T12:00:05",
                    step_type="analyze",
                    attempt=0,
                ),
            ]
        return ChatSession(
            flow_id="flow1", step_id="step1", step_type="analyze",
            messages=messages,
        )

    def test_returns_renderables(self):
        session = self._make_session()
        renderables = render_session_detailed(session, verbose=False)
        assert len(renderables) == 2  # prompt panel + response panel

    def test_verbose_returns_renderables(self):
        session = self._make_session()
        renderables = render_session_detailed(session, verbose=True)
        assert len(renderables) == 2

    def test_non_verbose_shows_final_text(self):
        """Non-verbose mode should extract only the final text block."""
        from rich.console import Console
        from io import StringIO
        session = self._make_session()
        renderables = render_session_detailed(session, verbose=False)
        buf = StringIO()
        c = Console(file=buf, force_terminal=False, width=200)
        for r in renderables:
            c.print(r)
        output = buf.getvalue()
        # Should have the final text
        assert "Final answer here" in output
        # Should NOT have tool call details in non-verbose
        assert "Read:" not in output

    def test_verbose_shows_tool_calls(self):
        """Verbose mode should include tool call previews."""
        from rich.console import Console
        from io import StringIO
        session = self._make_session()
        renderables = render_session_detailed(session, verbose=True)
        buf = StringIO()
        c = Console(file=buf, force_terminal=False, width=200)
        for r in renderables:
            c.print(r)
        output = buf.getvalue()
        # Should have tool call info
        assert "Read:" in output

    def test_prompt_segmentation_in_panel(self):
        """Prompt panel should contain segment titles."""
        from rich.console import Console
        from io import StringIO
        session = self._make_session()
        renderables = render_session_detailed(session, verbose=False)
        buf = StringIO()
        c = Console(file=buf, force_terminal=False, width=200)
        c.print(renderables[0])  # Prompt panel
        output = buf.getvalue()
        assert "Task Description" in output
        assert "Read-Only Constraint" in output

    def test_multiple_attempts_labeling(self):
        """Sessions with multiple attempts should show 'Attempt N' labels."""
        from rich.console import Console
        from io import StringIO
        ndjson_dict = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "First try failed."}]
            }
        }
        ndjson_dict2 = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Second try succeeded."}]
            }
        }
        messages = [
            ChatMessage(
                role="user", content="Do something.",
                raw_json=[], timestamp="2026-01-01T12:00:00",
                step_type="analyze", attempt=0,
            ),
            ChatMessage(
                role="assistant", content="First try failed.",
                raw_json=[ndjson_dict], timestamp="2026-01-01T12:00:05",
                step_type="analyze", attempt=0,
            ),
            ChatMessage(
                role="user", content="Do something again.",
                raw_json=[], timestamp="2026-01-01T12:01:00",
                step_type="analyze", attempt=1,
            ),
            ChatMessage(
                role="assistant", content="Second try succeeded.",
                raw_json=[ndjson_dict2], timestamp="2026-01-01T12:01:05",
                step_type="analyze", attempt=1,
            ),
        ]
        session = self._make_session(messages=messages)
        renderables = render_session_detailed(session, verbose=False)
        # Should have 4 panels: prompt+response for each attempt
        assert len(renderables) == 4
        buf = StringIO()
        c = Console(file=buf, force_terminal=False, width=200)
        for r in renderables:
            c.print(r)
        output = buf.getvalue()
        assert "Attempt 1" in output
        assert "Attempt 2" in output
        assert "First try failed" in output
        assert "Second try succeeded" in output

    def test_empty_raw_json_falls_back_to_content(self):
        """When raw_json is empty, render_session_detailed should fall back to msg.content."""
        from rich.console import Console
        from io import StringIO
        messages = [
            ChatMessage(
                role="user", content="Do something.",
                raw_json=[], timestamp="2026-01-01T12:00:00",
                step_type="analyze", attempt=0,
            ),
            ChatMessage(
                role="assistant", content="Fallback content here.",
                raw_json=[], timestamp="2026-01-01T12:00:05",
                step_type="analyze", attempt=0,
            ),
        ]
        session = self._make_session(messages=messages)
        renderables = render_session_detailed(session, verbose=False)
        assert len(renderables) == 2
        buf = StringIO()
        c = Console(file=buf, force_terminal=False, width=200)
        for r in renderables:
            c.print(r)
        output = buf.getvalue()
        assert "Fallback content here" in output

    def test_tool_only_raw_json_shows_activity(self):
        """When raw_json has tool activity but no text, should show tool summary not '(empty response)'."""
        from rich.console import Console
        from io import StringIO
        # raw_json with only tool_result entries, no assistant text
        tool_result_dict = {
            "type": "tool_result",
            "result": {"toolUseId": "t1", "content": "file contents", "isError": False}
        }
        messages = [
            ChatMessage(
                role="user", content="Do something.",
                raw_json=[], timestamp="2026-01-01T12:00:00",
                step_type="analyze", attempt=0,
            ),
            ChatMessage(
                role="assistant", content="",
                raw_json=[tool_result_dict], timestamp="2026-01-01T12:00:05",
                step_type="analyze", attempt=0,
            ),
        ]
        session = self._make_session(messages=messages)
        renderables = render_session_detailed(session, verbose=False)
        buf = StringIO()
        c = Console(file=buf, force_terminal=False, width=200)
        for r in renderables:
            c.print(r)
        output = buf.getvalue()
        # Should NOT show "(empty response)" since there was tool activity
        assert "(empty response)" not in output


# --- Detailed JSON output ---

class TestGetDetailedJson:
    def test_returns_structured_data(self, tmp_project):
        record_prompt(tmp_project, "flow1", "step_a", "analyze",
                      "## Task Description\nDo something.", 0)
        ndjson = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Result"}]}
        })
        record_response(tmp_project, "flow1", "step_a", "analyze", ndjson, 0)

        result = get_detailed_json(tmp_project, "flow1")
        assert len(result) == 1
        step = result[0]
        assert step["step_type"] == "analyze"
        assert len(step["messages"]) == 2

        # User message should have segments
        user_msg = step["messages"][0]
        assert user_msg["role"] == "user"
        assert "segments" in user_msg
        assert any(s["title"] == "Task Description" for s in user_msg["segments"])

        # Assistant message should have content and raw_json
        asst_msg = step["messages"][1]
        assert asst_msg["role"] == "assistant"
        assert "raw_json" in asst_msg
