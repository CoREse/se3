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


# ---------------------------------------------------------------------------
# Fix-iteration boundary filtering for format_history_for_retry
# ---------------------------------------------------------------------------

class TestFormatHistoryFixIterationBoundary:
    """Verify format_history_for_retry filters by current_fix_iteration so
    cross-iteration messages do not leak into retry context.

    Backward-compat: messages tagged with ``fix_iteration == 0`` are wildcard
    and always included (covers legacy jsonl predating this field).
    """

    def _msg(self, content, attempt=0, fix_iteration=0, role="user", ts="2026-05-10T10:00:00"):
        return ChatMessage(
            role=role,
            content=content,
            raw_json=[],
            timestamp=ts,
            step_type="implement",
            attempt=attempt,
            fix_iteration=fix_iteration,
        )

    @patch("se3.engine.chat_history.get_step_history")
    def test_filters_to_current_iteration_when_set(self, mock_get):
        """Messages with mismatching non-zero fix_iteration must be excluded."""
        sess = _make_session([
            self._msg("iter1 user", fix_iteration=1, role="user"),
            self._msg("iter1 reply", fix_iteration=1, role="assistant"),
            self._msg("iter2 user", fix_iteration=2, role="user"),
            self._msg("iter2 reply", fix_iteration=2, role="assistant"),
            self._msg("iter3 user", fix_iteration=3, role="user"),
        ])
        mock_get.return_value = sess

        result = format_history_for_retry(
            Path("/tmp"), "flow", "step", current_fix_iteration=2,
        )
        assert result is not None
        assert "iter2 user" in result
        assert "iter2 reply" in result
        assert "iter1 user" not in result
        assert "iter1 reply" not in result
        assert "iter3 user" not in result

    @patch("se3.engine.chat_history.get_step_history")
    def test_zero_fix_iteration_messages_act_as_wildcard(self, mock_get):
        """Legacy / unmarked messages (fix_iteration=0) are included regardless
        of current_fix_iteration so a chat_history written before the upgrade
        is not silently filtered out after deploy.
        """
        sess = _make_session([
            self._msg("legacy user", fix_iteration=0, role="user"),
            self._msg("legacy reply", fix_iteration=0, role="assistant"),
            self._msg("iter5 user", fix_iteration=5, role="user"),
        ])
        mock_get.return_value = sess

        result = format_history_for_retry(
            Path("/tmp"), "flow", "step", current_fix_iteration=5,
        )
        assert result is not None
        assert "legacy user" in result
        assert "legacy reply" in result
        assert "iter5 user" in result

    @patch("se3.engine.chat_history.get_step_history")
    def test_current_zero_means_no_filtering(self, mock_get):
        """When current_fix_iteration=0 (default / non-fix-loop callers), no
        iteration filter applies — all messages included.
        """
        sess = _make_session([
            self._msg("a", fix_iteration=1, role="user"),
            self._msg("b", fix_iteration=2, role="user"),
            self._msg("c", fix_iteration=3, role="user"),
        ])
        mock_get.return_value = sess

        result = format_history_for_retry(
            Path("/tmp"), "flow", "step", current_fix_iteration=0,
        )
        assert result is not None
        assert "a" in result
        assert "b" in result
        assert "c" in result

    @patch("se3.engine.chat_history.get_step_history")
    def test_returns_none_when_filter_drops_everything(self, mock_get):
        """If the iteration filter excludes every message, return None
        (caller treats None as 'no retry context')."""
        sess = _make_session([
            self._msg("a", fix_iteration=1, role="user"),
            self._msg("b", fix_iteration=2, role="user"),
        ])
        mock_get.return_value = sess

        result = format_history_for_retry(
            Path("/tmp"), "flow", "step", current_fix_iteration=99,
        )
        assert result is None

    @patch("se3.engine.chat_history.get_step_history")
    def test_default_argument_preserves_pre_upgrade_behavior(self, mock_get):
        """Callers that don't pass current_fix_iteration get all messages
        (default 0 acts as wildcard)."""
        sess = _make_session([
            self._msg("x", fix_iteration=1, role="user"),
            self._msg("y", fix_iteration=2, role="user"),
        ])
        mock_get.return_value = sess

        # No current_fix_iteration kwarg.
        result = format_history_for_retry(Path("/tmp"), "flow", "step")
        assert result is not None
        assert "x" in result
        assert "y" in result


# ---------------------------------------------------------------------------
# record_prompt / record_response accept fix_iteration
# ---------------------------------------------------------------------------

class TestRecordAPIAcceptsFixIteration:
    """Verify record_prompt / record_response persist fix_iteration."""

    def test_record_prompt_persists_fix_iteration(self, tmp_path):
        from se3.engine.chat_history import record_prompt, get_step_history

        record_prompt(
            tmp_path, "f1", "s1", "implement",
            prompt="hello", attempt=0, fix_iteration=7,
        )
        sess = get_step_history(tmp_path, "f1", "s1")
        assert sess is not None
        assert len(sess.messages) == 1
        assert sess.messages[0].fix_iteration == 7

    def test_record_response_persists_fix_iteration(self, tmp_path):
        from se3.engine.chat_history import record_response, get_step_history

        record_response(
            tmp_path, "f1", "s2", "implement",
            raw_ndjson='{"type":"assistant","message":{"content":[{"type":"text","text":"ok"}]}}',
            attempt=0, fix_iteration=4,
        )
        sess = get_step_history(tmp_path, "f1", "s2")
        assert sess is not None
        assert len(sess.messages) == 1
        assert sess.messages[0].fix_iteration == 4

    def test_record_prompt_default_fix_iteration_zero(self, tmp_path):
        """Old callers passing positional/kwargs without fix_iteration get 0."""
        from se3.engine.chat_history import record_prompt, get_step_history

        record_prompt(tmp_path, "f1", "s3", "analyze", prompt="x", attempt=0)
        sess = get_step_history(tmp_path, "f1", "s3")
        assert sess is not None
        assert sess.messages[0].fix_iteration == 0


# ---------------------------------------------------------------------------
# ChatMessage.from_dict backward-compat for legacy jsonl without fix_iteration
# ---------------------------------------------------------------------------

class TestChatMessageBackwardCompat:
    """Legacy ChatMessage records (no fix_iteration field) deserialize cleanly
    with default 0."""

    def test_from_dict_missing_fix_iteration_defaults_to_zero(self):
        legacy = {
            "role": "user",
            "content": "old prompt",
            "raw_json": [],
            "timestamp": "2026-01-01T00:00:00",
            "step_type": "analyze",
            "attempt": 0,
            # Note: no fix_iteration key
        }
        msg = ChatMessage.from_dict(legacy)
        assert msg.fix_iteration == 0
        assert msg.content == "old prompt"
