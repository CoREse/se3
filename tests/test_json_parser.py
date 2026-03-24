"""Tests for json_parser unescaped quote repair."""

import json

from se3.engine.utils.json_parser import (
    _repair_unescaped_quotes,
    _extract_and_parse_json,
    parse_json_response,
)


class TestRepairUnescapedQuotes:
    """Tests for _repair_unescaped_quotes."""

    def test_valid_json_unchanged(self):
        s = '{"key": "value", "num": 1}'
        assert _repair_unescaped_quotes(s) == s

    def test_already_escaped_quotes_unchanged(self):
        s = r'{"key": "he said \"hello\" there"}'
        result = _repair_unescaped_quotes(s)
        assert json.loads(result) == {"key": 'he said "hello" there'}

    def test_single_unescaped_quote(self):
        s = '{"key": "变为"继续"的内容"}'
        result = _repair_unescaped_quotes(s)
        parsed = json.loads(result)
        assert parsed["key"] == '变为"继续"的内容'

    def test_multiple_unescaped_quotes(self):
        s = '{"a": "x"y"z", "b": "ok"}'
        result = _repair_unescaped_quotes(s)
        parsed = json.loads(result)
        assert parsed["a"] == 'x"y"z'
        assert parsed["b"] == "ok"

    def test_mixed_escaped_and_unescaped(self):
        s = r'{"key": "escaped\"ok but "bad" here"}'
        result = _repair_unescaped_quotes(s)
        parsed = json.loads(result)
        assert '"bad"' in parsed["key"]

    def test_bug_report_json(self):
        """Test with the exact JSON pattern from the bug report."""
        s = r'''{
    "mode": "confirmation",
    "content": "提示语也是"重新开始"而非"继续"",
    "questions": [],
    "ready_to_proceed": false
}'''
        result = _repair_unescaped_quotes(s)
        parsed = json.loads(result)
        assert parsed["mode"] == "confirmation"
        assert "重新开始" in parsed["content"]
        assert parsed["questions"] == []
        assert parsed["ready_to_proceed"] is False

    def test_newlines_in_value_with_unescaped_quotes(self):
        """Unescaped quotes inside a value that also has escaped newlines."""
        s = '{"content": "line1\\nhe said "hi" there\\nline3"}'
        result = _repair_unescaped_quotes(s)
        parsed = json.loads(result)
        assert '"hi"' in parsed["content"]


class TestExtractAndParseJsonWithQuoteRepair:
    """Integration tests: _extract_and_parse_json with unescaped quote repair."""

    def test_markdown_block_with_unescaped_quotes(self):
        text = '```json\n{"key": "变为"继续"的"}\n```'
        result = _extract_and_parse_json(text)
        assert result is not None
        assert "继续" in result["key"]

    def test_raw_json_with_unescaped_quotes(self):
        text = '{"key": "but "bad" here"}'
        result = _extract_and_parse_json(text)
        assert result is not None
        assert '"bad"' in result["key"]

    def test_valid_json_passes_through(self):
        text = '{"mode": "work", "ready": true}'
        result = _extract_and_parse_json(text)
        assert result == {"mode": "work", "ready": True}

    def test_bug_report_full_json(self):
        """Test with a larger JSON resembling the actual bug report."""
        s = r'''{
    "mode": "confirmation",
    "content": "好的，我已经完全理解了两个场景。以下是整理后的需求：\n\n## 需求描述\n\n### 1. 失败重试 — 从断点继续而非重新开始\n\n**现状：** `format_history_for_retry()` 的结尾指令是 \"The above attempt(s) failed. Please try again with the same task.\"，导致 LLM 从头开始整个 step。\n\n**期望：** 修改提示语，让 LLM 从上次停下的地方继续。之前的所有对话记录（包括 tool calls/results）照常注入，但指引变为"继续未完成的工作"。\n\n### 2. Ctrl-C 注入 — 在断点处继续 + 新指令\n\n**现状两个问题：**\n- `_handle_step_interrupt` 将 step 重置为 PENDING，step handler 会创建新的 `LLMCaller(external_attempt=0)`，导致 `total_attempt == 0`，**历史上下文不会被注入**\n- 即使注入了，提示语也是"重新开始"而非"继续"\n\n**期望：** Ctrl-C 注入后，step 重新执行时应该：\n1. 自动检测到已有历史记录，注入之前的完整对话\n2. 指引 LLM 从断点处继续，上下文中多了用户注入的新指令",
    "questions": [],
    "refined_description": "修改 retry 和 Ctrl-C 注入的行为",
    "ready_to_proceed": false,
    "thinking": "key insight"
}'''
        result = _extract_and_parse_json(s)
        assert result is not None
        assert result["mode"] == "confirmation"
        assert result["ready_to_proceed"] is False
        assert "继续未完成的工作" in result["content"]
