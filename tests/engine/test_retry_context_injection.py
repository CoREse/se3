"""Tests for retry context injection bugs.

Covers:
- implement step passes retry_count as external_attempt to LLMCaller
- _get_retry_context() logs warning on failure (not just debug)
- format_history_for_retry() handles malformed NDJSON gracefully
- All step handlers pass retry_count as external_attempt
"""

import json
import logging
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from se3.engine.llm_caller import LLMCaller
from se3.engine.chat_history import (
    ChatMessage,
    ChatSession,
    extract_conversation_from_ndjson,
    format_history_for_retry,
)
from se3.engine.models import FlowInstance, Step, StepStatus, StepType


# ---------------------------------------------------------------------------
# Task 1: implement step passes retry_count as external_attempt
# ---------------------------------------------------------------------------

class TestImplementPassesRetryCount:
    """Verify implement step reads retry_count and passes it to LLMCaller."""

    @patch("se3.engine.steps.implement.LLMCaller")
    def test_implement_passes_retry_count(self, mock_caller_cls):
        """On retry, implement handler should pass retry_count as external_attempt."""
        from se3.engine.steps.implement import implement_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({
            "files_changed": ["a.py"],
            "tests_added": [],
            "test_mapping": {},
            "summary": "done",
        })
        mock_caller_cls.return_value = mock_caller

        flow = FlowInstance(
            flow_id="test-flow",
            task_description="test task",
            task_type="bugfix",
        )
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.RETRYING,
            inputs={
                "task_description": "test task",
                "task_type": "bugfix",
                "task_groups": [{"group_id": "G1", "tasks": []}],
                "spec_content": {},
                "retry_count": 2,
            },
        )

        implement_handler(step, flow)

        # Verify LLMCaller was constructed with external_attempt=2
        call_kwargs = mock_caller_cls.call_args
        assert call_kwargs[1].get("external_attempt") == 2 or \
            (len(call_kwargs[0]) > 0 and call_kwargs[1].get("external_attempt") == 2)

    @patch("se3.engine.steps.implement.LLMCaller")
    def test_implement_defaults_retry_count_to_zero(self, mock_caller_cls):
        """Without retry_count in inputs, external_attempt should be 0."""
        from se3.engine.steps.implement import implement_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({
            "files_changed": [],
            "tests_added": [],
            "test_mapping": {},
            "summary": "done",
        })
        mock_caller_cls.return_value = mock_caller

        flow = FlowInstance(
            flow_id="test-flow",
            task_description="test",
            task_type="feature",
        )
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "test",
                "task_type": "feature",
                "task_groups": [],
                "spec_content": {},
            },
        )

        implement_handler(step, flow)

        call_kwargs = mock_caller_cls.call_args
        assert call_kwargs[1].get("external_attempt") == 0


# ---------------------------------------------------------------------------
# Task 2: _get_retry_context() logs warning on exception
# ---------------------------------------------------------------------------

class TestGetRetryContextWarning:
    """Verify _get_retry_context logs at WARNING level on failure."""

    def test_logs_warning_on_exception(self, caplog):
        """_get_retry_context should log warning (not debug) when format_history_for_retry fails."""
        caller = LLMCaller(
            project_root=Path("/tmp"),
            flow_id="test-flow",
            step_id="test-step",
            step_type="implement",
        )

        with patch("se3.engine.chat_history.format_history_for_retry",
                    side_effect=ValueError("malformed data")):
            import se3.engine.chat_history as ch_mod
            original_fn = ch_mod.format_history_for_retry

            # Patch at module level used by _get_retry_context
            ch_mod.format_history_for_retry = MagicMock(
                side_effect=ValueError("malformed data")
            )
            try:
                with caplog.at_level(logging.WARNING, logger="se3.engine.llm_caller"):
                    result = caller._get_retry_context()

                assert result is None
                assert any("Failed to get retry context" in r.message for r in caplog.records)
                assert any(r.levelno == logging.WARNING for r in caplog.records
                           if "Failed to get retry context" in r.message)
            finally:
                ch_mod.format_history_for_retry = original_fn


# ---------------------------------------------------------------------------
# Task 3: format_history_for_retry handles malformed NDJSON
# ---------------------------------------------------------------------------

def _make_session(messages):
    return ChatSession(
        flow_id="test-flow",
        step_id="test-step",
        step_type="implement",
        messages=messages,
    )


class TestFormatHistoryMalformedNDJSON:
    """Verify format_history_for_retry handles malformed raw_json gracefully."""

    @patch("se3.engine.chat_history.get_step_history")
    def test_malformed_raw_json_falls_back_to_content(self, mock_get):
        """If raw_json entries are malformed, should fall back to simplified content."""
        # Create a message with malformed raw_json (non-dict items)
        msg = ChatMessage(
            role="assistant",
            content="I did some work on the task",
            raw_json=[
                "not a dict",  # malformed
                42,            # malformed
                None,          # malformed
            ],
            timestamp="2026-03-24T10:00:00",
            step_type="implement",
            attempt=0,
        )
        user_msg = ChatMessage(
            role="user",
            content="Do the task",
            raw_json=[],
            timestamp="2026-03-24T09:59:00",
            step_type="implement",
            attempt=0,
        )
        mock_get.return_value = _make_session([user_msg, msg])

        result = format_history_for_retry(Path("/tmp"), "flow", "step")

        # Should not crash, should contain the fallback content
        assert result is not None
        assert "I did some work on the task" in result

    @patch("se3.engine.chat_history.get_step_history")
    def test_partial_valid_ndjson_returns_partial_context(self, mock_get):
        """If some raw_json entries are valid and some aren't, should return partial context."""
        raw_json = [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Reading the file now."}]
                },
            },
            "this is not valid json structure",  # malformed entry
            {
                "type": "tool_result",
                "result": {
                    "toolUseId": "t1",
                    "content": "file contents",
                    "isError": False,
                },
            },
        ]

        msg = ChatMessage(
            role="assistant",
            content="Reading the file now.",
            raw_json=raw_json,
            timestamp="2026-03-24T10:00:00",
            step_type="implement",
            attempt=0,
        )
        user_msg = ChatMessage(
            role="user",
            content="Implement the feature",
            raw_json=[],
            timestamp="2026-03-24T09:59:00",
            step_type="implement",
            attempt=0,
        )
        mock_get.return_value = _make_session([user_msg, msg])

        result = format_history_for_retry(Path("/tmp"), "flow", "step")

        assert result is not None
        # The valid entries should be present
        assert "Reading the file now" in result

    @patch("se3.engine.chat_history.get_step_history")
    def test_completely_broken_raw_json_falls_back(self, mock_get, caplog):
        """If extract_conversation_from_ndjson raises, should fall back and log warning."""
        msg = ChatMessage(
            role="assistant",
            content="Fallback content here",
            raw_json=[{"type": "assistant", "message": {"content": "not a list"}}],
            timestamp="2026-03-24T10:00:00",
            step_type="implement",
            attempt=0,
        )
        user_msg = ChatMessage(
            role="user",
            content="Do task",
            raw_json=[],
            timestamp="2026-03-24T09:59:00",
            step_type="implement",
            attempt=0,
        )
        mock_get.return_value = _make_session([user_msg, msg])

        # This should not raise - it should fall back gracefully
        result = format_history_for_retry(Path("/tmp"), "flow", "step")
        assert result is not None


class TestExtractConversationFromNDJSONResilience:
    """Verify extract_conversation_from_ndjson handles malformed entries."""

    def test_handles_non_dict_items_in_list(self):
        """Non-dict items in list input should be skipped gracefully."""
        raw = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
            "bad entry",
            42,
            None,
            {"type": "tool_result", "result": {"toolUseId": "t1", "content": "ok", "isError": False}},
        ]
        messages = extract_conversation_from_ndjson(raw)
        # Should have at least the valid assistant message
        assert len(messages) >= 1
        assert messages[0].role == "assistant"
        assert "hello" in messages[0].content

    def test_handles_empty_list(self):
        """Empty list should return empty list."""
        assert extract_conversation_from_ndjson([]) == []

    def test_handles_items_missing_type(self):
        """Items without 'type' key should be skipped."""
        raw = [
            {"no_type_key": "value"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "valid"}]}},
        ]
        messages = extract_conversation_from_ndjson(raw)
        assert len(messages) >= 1
        assert "valid" in messages[-1].content

    def test_handles_malformed_string_lines(self):
        """Malformed lines in string input should be skipped."""
        ndjson = (
            '{"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}\n'
            'this is not json\n'
            '{"truncated": true\n'  # incomplete JSON
        )
        messages = extract_conversation_from_ndjson(ndjson)
        assert len(messages) >= 1
        assert messages[0].content == "ok"
