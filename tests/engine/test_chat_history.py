"""Tests for chat_history module, focusing on format_history_for_retry modes."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from se3.engine.chat_history import (
    ChatMessage,
    ChatSession,
    format_history_for_retry,
    get_step_history,
)


def _make_user_message(content: str, attempt: int = 0) -> ChatMessage:
    return ChatMessage(
        role="user",
        content=content,
        raw_json=[],
        timestamp="2026-03-23T10:00:00",
        step_type="implement",
        attempt=attempt,
    )


def _make_assistant_message(
    content: str, attempt: int = 0, raw_json: list = None
) -> ChatMessage:
    return ChatMessage(
        role="assistant",
        content=content,
        raw_json=raw_json or [],
        timestamp="2026-03-23T10:01:00",
        step_type="implement",
        attempt=attempt,
    )


def _make_assistant_with_tool_calls(attempt: int = 0) -> ChatMessage:
    """Create an assistant message with tool calls in raw_json."""
    raw_json = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Let me read the file."},
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/test.py"},
                    },
                ]
            },
        },
        {
            "type": "tool_result",
            "result": {
                "toolUseId": "tool_1",
                "content": "file contents here " * 100,  # long result
                "isError": False,
            },
        },
    ]
    return ChatMessage(
        role="assistant",
        content="Let me read the file.\n[Tool Call: Read]",
        raw_json=raw_json,
        timestamp="2026-03-23T10:01:00",
        step_type="implement",
        attempt=attempt,
    )


def _make_session(messages: list) -> ChatSession:
    return ChatSession(
        flow_id="test-flow",
        step_id="test-step",
        step_type="implement",
        messages=messages,
    )


class TestFormatHistoryForRetryMode:
    """Tests for the mode parameter of format_history_for_retry."""

    @patch("se3.engine.chat_history.get_step_history")
    def test_default_mode_is_continue(self, mock_get):
        """Default mode should be 'continue'."""
        mock_get.return_value = _make_session([
            _make_user_message("Do X"),
            _make_assistant_message("Did X partially"),
        ])
        result = format_history_for_retry(Path("/tmp"), "flow", "step")
        assert result is not None
        assert "Continue from where the previous attempt stopped" in result
        assert "Do NOT redo completed work" in result

    @patch("se3.engine.chat_history.get_step_history")
    def test_retry_mode_uses_restart_instruction(self, mock_get):
        """Retry mode should use the restart instruction."""
        mock_get.return_value = _make_session([
            _make_user_message("Do X"),
            _make_assistant_message("Did X partially"),
        ])
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="retry")
        assert result is not None
        assert "Please try again with the same task" in result
        assert "Continue from where" not in result

    @patch("se3.engine.chat_history.get_step_history")
    def test_continue_mode_user_prompt_4000_limit(self, mock_get):
        """In continue mode, user prompts should be truncated at 4000 chars."""
        long_prompt = "x" * 5000
        mock_get.return_value = _make_session([
            _make_user_message(long_prompt),
            _make_assistant_message("response"),
        ])
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="continue")
        assert result is not None
        # The prompt should be truncated at 4000
        assert "x" * 4000 in result
        assert "x" * 4001 not in result
        assert "[truncated]" in result

    @patch("se3.engine.chat_history.get_step_history")
    def test_retry_mode_user_prompt_2000_limit(self, mock_get):
        """In retry mode, user prompts should be truncated at 2000 chars."""
        long_prompt = "x" * 3000
        mock_get.return_value = _make_session([
            _make_user_message(long_prompt),
            _make_assistant_message("response"),
        ])
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="retry")
        assert result is not None
        assert "x" * 2000 in result
        assert "x" * 2001 not in result
        assert "[truncated]" in result

    @patch("se3.engine.chat_history.get_step_history")
    def test_continue_mode_preserves_tool_call_responses(self, mock_get):
        """In continue mode, assistant responses with tool calls should not be truncated."""
        mock_get.return_value = _make_session([
            _make_user_message("Do X"),
            _make_assistant_with_tool_calls(),
        ])
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="continue")
        assert result is not None
        # Tool call content should be present (not truncated)
        assert "Read" in result
        assert "file contents here" in result

    @patch("se3.engine.chat_history.get_step_history")
    def test_retry_mode_truncates_long_assistant_responses(self, mock_get):
        """In retry mode, long assistant responses without raw_json should be truncated at 2000."""
        long_response = "y" * 3000
        mock_get.return_value = _make_session([
            _make_user_message("Do X"),
            _make_assistant_message(long_response),  # no raw_json
        ])
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="retry")
        assert result is not None
        assert "y" * 2000 in result
        assert "y" * 2001 not in result

    @patch("se3.engine.chat_history.get_step_history")
    def test_no_history_returns_none(self, mock_get):
        """Should return None if no history exists."""
        mock_get.return_value = None
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="continue")
        assert result is None

    @patch("se3.engine.chat_history.get_step_history")
    def test_empty_messages_returns_none(self, mock_get):
        """Should return None if session has empty messages."""
        mock_get.return_value = _make_session([])
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="continue")
        assert result is None

    @patch("se3.engine.chat_history.get_step_history")
    def test_continue_mode_short_prompts_not_truncated(self, mock_get):
        """Short prompts should not be truncated in either mode."""
        mock_get.return_value = _make_session([
            _make_user_message("Short prompt"),
            _make_assistant_message("Short response"),
        ])
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="continue")
        assert result is not None
        assert "Short prompt" in result
        assert "[truncated]" not in result
