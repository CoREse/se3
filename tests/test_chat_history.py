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
    get_flow_history,
    get_step_history,
    list_flows,
    record_prompt,
    record_response,
    render_session_text,
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
            raw_ndjson="",
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
            "raw_ndjson": '{"type":"assistant"}',
            "timestamp": "2026-01-01T00:00:00",
            "step_type": "propose",
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
            raw_ndjson="",
            timestamp="2026-02-27T12:00:00",
            step_type="design",
            attempt=0,
        )
        d = msg.to_dict()
        msg2 = ChatMessage.from_dict(d)
        assert msg == msg2


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
                    {"type": "tool_use", "name": "Read", "input": {"path": "foo.py"}},
                ]
            }
        })
        result = extract_assistant_text(ndjson)
        assert "Let me check" in result
        assert "[Tool Call: Read]" in result

    def test_tool_result(self):
        ndjson = json.dumps({
            "type": "tool_result",
            "result": {"toolUseId": "123", "content": "file contents here"}
        })
        result = extract_assistant_text(ndjson)
        assert "[Tool Result:" in result
        assert "file contents" in result

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
        ndjson = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "It is a test"}]}
        })
        record_response(tmp_project, "flow1", "step1", "analyze", ndjson, 0)

        session = get_step_history(tmp_project, "flow1", "step1")
        assert session is not None
        assert len(session.messages) == 1
        assert session.messages[0].role == "assistant"
        assert session.messages[0].content == "It is a test"
        assert session.messages[0].raw_ndjson == ndjson

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


# --- Flow history ---

class TestFlowHistory:
    def test_get_flow_history(self, tmp_project):
        record_prompt(tmp_project, "flow1", "step_a", "analyze", "P1", 0)
        record_prompt(tmp_project, "flow1", "step_b", "propose", "P2", 0)

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
        assert "failed" in context.lower()

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
                    raw_ndjson="",
                    timestamp="2026-01-01T12:00:00",
                    step_type="analyze",
                    attempt=0,
                ),
                ChatMessage(
                    role="assistant",
                    content="Analysis complete",
                    raw_ndjson="",
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
                    raw_ndjson="",
                    timestamp="2026-01-01T12:00:00",
                    step_type="analyze",
                    attempt=0,
                ),
            ],
        )
        text = render_session_text(session, truncate_prompt=100)
        assert "[truncated]" in text

    def test_render_with_ndjson(self):
        ndjson = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Here is the analysis"},
                    {"type": "tool_use", "name": "Read", "input": {"path": "foo.py"}},
                ]
            }
        })
        session = ChatSession(
            flow_id="flow1",
            step_id="step1",
            step_type="analyze",
            messages=[
                ChatMessage(
                    role="assistant",
                    content="Here is the analysis",
                    raw_ndjson=ndjson,
                    timestamp="2026-01-01T12:00:00",
                    step_type="analyze",
                    attempt=0,
                ),
            ],
        )
        text = render_session_text(session)
        assert "Here is the analysis" in text
        assert "Tool: Read" in text
