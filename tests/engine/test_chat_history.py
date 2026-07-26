"""Tests for chat_history module, focusing on format_history_for_retry modes."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from tianluo.engine.chat_history import (
    ChatMessage,
    ChatSession,
    format_history_for_retry,
    get_step_history,
    record_user_interjection,
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

    @patch("tianluo.engine.chat_history.get_step_history")
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

    @patch("tianluo.engine.chat_history.get_step_history")
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

    @patch("tianluo.engine.chat_history.get_step_history")
    def test_continue_mode_user_prompt_not_truncated(self, mock_get):
        """In continue mode, user prompts should NOT be truncated."""
        long_prompt = "x" * 5000
        mock_get.return_value = _make_session([
            _make_user_message(long_prompt),
            _make_assistant_message("response"),
        ])
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="continue")
        assert result is not None
        # Full prompt should be preserved
        assert "x" * 5000 in result
        assert "[truncated]" not in result

    @patch("tianluo.engine.chat_history.get_step_history")
    def test_retry_mode_user_prompt_not_truncated(self, mock_get):
        """In retry mode, user prompts should NOT be truncated."""
        long_prompt = "x" * 3000
        mock_get.return_value = _make_session([
            _make_user_message(long_prompt),
            _make_assistant_message("response"),
        ])
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="retry")
        assert result is not None
        # Full prompt should be preserved
        assert "x" * 3000 in result
        assert "[truncated]" not in result

    @patch("tianluo.engine.chat_history.get_step_history")
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

    @patch("tianluo.engine.chat_history.get_step_history")
    def test_retry_mode_truncates_long_assistant_responses(self, mock_get):
        """In retry mode, long assistant responses use head+tail truncation at 2000."""
        long_response = "y" * 3000
        mock_get.return_value = _make_session([
            _make_user_message("Do X"),
            _make_assistant_message(long_response),  # no raw_json
        ])
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="retry")
        assert result is not None
        # head=500, tail=1500 for retry mode (limit=2000)
        assert "y" * 500 in result
        assert "middle truncated, showing head+tail" in result
        assert "y" * 1500 in result
        assert "y" * 1501 not in result

    @patch("tianluo.engine.chat_history.get_step_history")
    def test_no_history_returns_none(self, mock_get):
        """Should return None if no history exists."""
        mock_get.return_value = None
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="continue")
        assert result is None

    @patch("tianluo.engine.chat_history.get_step_history")
    def test_empty_messages_returns_none(self, mock_get):
        """Should return None if session has empty messages."""
        mock_get.return_value = _make_session([])
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="continue")
        assert result is None

    @patch("tianluo.engine.chat_history.get_step_history")
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

    @patch("tianluo.engine.chat_history.get_step_history")
    def test_user_prompt_preserved_verbatim_regardless_of_size(self, mock_get):
        """User prompts are no longer truncated per-message in format_history_for_retry.

        The per-prompt 50K hard cap was removed because it fired before
        deduplicate_prompt_lines() had a chance to eliminate repeated spec text
        across attempts.  Post-dedup safety is enforced in LLMCaller.
        """
        huge_prompt = "x" * 60_000
        mock_get.return_value = _make_session([
            _make_user_message(huge_prompt),
            _make_assistant_message("response"),
        ])
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="continue")
        assert result is not None
        assert "x" * 60_000 in result
        assert "user prompt truncated for retry context safety" not in result
        assert "hit safety limit" not in result

    @patch("tianluo.engine.chat_history.get_step_history")
    def test_user_prompt_at_50k_not_truncated(self, mock_get):
        """User prompts at 50K chars pass through unchanged (regression guard)."""
        exact_prompt = "y" * 50_000
        mock_get.return_value = _make_session([
            _make_user_message(exact_prompt),
            _make_assistant_message("response"),
        ])
        result = format_history_for_retry(Path("/tmp"), "flow", "step", mode="retry")
        assert result is not None
        assert "y" * 50_000 in result
        assert "truncated for retry context safety" not in result


class TestRecordUserInterjection:
    """Tests for :func:`record_user_interjection`."""

    def test_writes_one_jsonl_line_with_expected_shape(self, tmp_path):
        record_user_interjection(
            tmp_path,
            "flow-1",
            "01_implement_abc",
            "implement",
            "please also rename the variable",
            attempt=2,
            source="webui",
        )
        path = tmp_path / "tianluo" / "history" / "flow-1" / "01_implement_abc.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["role"] == "user"
        assert record["kind"] == "interjection"
        assert record["content"] == "please also rename the variable"
        assert record["source"] == "webui"
        assert record["step_id"] == "01_implement_abc"
        assert record["step_type"] == "implement"
        assert record["attempt"] == 2
        assert "timestamp" in record and isinstance(record["timestamp"], str)

    def test_concurrent_appends_preserve_line_boundaries(self, tmp_path):
        """Each call appends exactly one whole line; no interleaving."""
        for i in range(5):
            record_user_interjection(
                tmp_path,
                "flow-2",
                "02_test_def",
                "test",
                f"interjection {i}",
            )
        path = tmp_path / "tianluo" / "history" / "flow-2" / "02_test_def.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5
        for i, line in enumerate(lines):
            record = json.loads(line)  # must be parseable on its own
            assert record["content"] == f"interjection {i}"
            assert record["kind"] == "interjection"

    def test_missing_flow_id_is_noop(self, tmp_path, caplog):
        """Empty flow_id is a warning + no-op, not an exception."""
        import logging

        with caplog.at_level(logging.WARNING, logger="tianluo.engine.chat_history"):
            record_user_interjection(tmp_path, "", "step-id", "implement", "hi")
        # No history dir / file should have been created
        assert not (tmp_path / "tianluo" / "history").exists() or not list(
            (tmp_path / "tianluo" / "history").iterdir()
        )
        assert any("missing flow_id" in rec.message for rec in caplog.records)

    def test_missing_step_id_is_noop(self, tmp_path, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="tianluo.engine.chat_history"):
            record_user_interjection(tmp_path, "flow", "", "implement", "hi")
        assert not (tmp_path / "tianluo" / "history" / "flow").exists()
        assert any("missing flow_id" in rec.message or "step_id" in rec.message
                   for rec in caplog.records)

    def test_interjection_visible_via_get_step_history(self, tmp_path):
        """``get_step_history`` rehydrates the interjection as a user
        ChatMessage with ``kind == "interjection"`` (rendering path keeps
        it visible)."""
        record_user_interjection(
            tmp_path, "flow-3", "03_implement_xyz", "implement",
            "tighten the error message", source="webui",
        )
        session = get_step_history(tmp_path, "flow-3", "03_implement_xyz")
        assert session is not None
        assert len(session.messages) == 1
        msg = session.messages[0]
        assert msg.role == "user"
        assert msg.kind == "interjection"
        assert msg.content == "tighten the error message"


class TestFormatHistorySkipsInterjections:
    """Interjection records are kept in jsonl for display but excluded
    from the retry context passed back to the LLM."""

    @patch("tianluo.engine.chat_history.get_step_history")
    def test_interjection_not_included_in_retry_context(self, mock_get):
        interjection = ChatMessage(
            role="user",
            content="please use shorter variable names",
            raw_json=[],
            timestamp="2026-03-23T10:00:30",
            step_type="implement",
            attempt=0,
            kind="interjection",
        )
        mock_get.return_value = _make_session([
            _make_user_message("Do X"),
            interjection,
            _make_assistant_message("Did X"),
        ])
        result = format_history_for_retry(Path("/tmp"), "flow", "step")
        assert result is not None
        # The original user prompt and assistant response remain
        assert "Do X" in result
        assert "Did X" in result
        # But the interjection text must NOT be retroactively fed back
        # to the LLM as another [User Prompt]: turn
        assert "please use shorter variable names" not in result

    @patch("tianluo.engine.chat_history.get_step_history")
    def test_only_interjections_returns_none(self, mock_get):
        """If filtering removes every message, return None (matches the
        existing `if not filtered: return None` contract)."""
        interjection = ChatMessage(
            role="user",
            content="ping",
            raw_json=[],
            timestamp="2026-03-23T10:00:30",
            step_type="implement",
            attempt=0,
            kind="interjection",
        )
        mock_get.return_value = _make_session([interjection])
        assert format_history_for_retry(Path("/tmp"), "flow", "step") is None
