"""Tests for json_parser unescaped quote repair."""

import json

from tianluo.engine.utils.json_parser import (
    _repair_unescaped_quotes,
    _extract_and_parse_json,
    parse_json_response,
    looks_like_json_object,
    looks_like_json,
    _try_parse_with_repairs,
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

    def test_trailing_json_with_escaped_quotes(self):
        """When text contains multiple JSONs and the trailing one has
        escaped quotes (\"), the backward walker must correctly identify
        the JSON boundary so the trailing JSON is extracted."""
        text = '{"a": "first"} then {"b": "say \\"hi\\""}'
        result = _extract_and_parse_json(text)
        assert result is not None
        assert result == {"b": 'say "hi"'}


class TestTryParseWithRepairs:
    """Tests for _try_parse_with_repairs — shared repair chain helper."""

    def test_valid_json_object(self):
        result = _try_parse_with_repairs('{"a": 1}')
        assert result == {"a": 1}

    def test_valid_json_array(self):
        result = _try_parse_with_repairs("[1, 2, 3]")
        assert result == [1, 2, 3]

    def test_unescaped_quotes_repaired(self):
        s = '{"content": "是否重写"discovery"步骤"}'
        result = _try_parse_with_repairs(s)
        assert result is not None
        assert "content" in result

    def test_trailing_comma_repaired(self):
        s = '{"a": 1, "b": 2,}'
        result = _try_parse_with_repairs(s)
        assert result == {"a": 1, "b": 2}

    def test_plain_text_returns_none(self):
        assert _try_parse_with_repairs("hello world") is None

    def test_empty_string_returns_none(self):
        assert _try_parse_with_repairs("") is None

    def test_none_input_returns_none(self):
        assert _try_parse_with_repairs(None) is None

    def test_consistency_with_extract_and_parse_json(self):
        """_try_parse_with_repairs should produce the same parsed result
        as _extract_and_parse_json for text that doesn't need extraction."""
        samples = [
            '{"key": "value"}',
            '{"content": "是否重写"discovery"步骤"}',
            '{"a": "x"y"z", "b": "ok"}',
            '{"a": 1, "b": 2,}',
        ]
        for s in samples:
            from_repair = _try_parse_with_repairs(s)
            from_extract = _extract_and_parse_json(s)
            assert from_repair == from_extract, f"mismatch for: {s[:60]}"


class TestLooksLikeJsonObject:
    """Tests for looks_like_json_object — lenient JSON detection helper."""

    def test_valid_json_object(self):
        assert looks_like_json_object('{"a": 1}') is True

    def test_valid_json_array_is_not_object(self):
        """Arrays are valid JSON but not dicts; parse_json_response only
        returns dicts because _validate_keys rejects non-dicts."""
        assert looks_like_json_object('[1, 2, 3]') is False

    def test_unescaped_ascii_quotes_inside_value(self):
        """Bug-repro: unescaped ASCII double quotes inside string values.

        This is the exact asymmetry that caused fenced JSON blocks with
        unescaped interior quotes to be treated as narrative (strict parse
        failed) while parse_json_response succeeded (lenient repair).
        """
        s = '{"content": "是否重写"discovery"步骤"}'
        assert looks_like_json_object(s) is True

    def test_unescaped_quotes_with_escaped_newlines(self):
        s = '{"content": "line1\\nhe said "hi" there\\nline3"}'
        assert looks_like_json_object(s) is True

    def test_multiple_unescaped_quotes(self):
        s = '{"a": "x"y"z", "b": "ok"}'
        assert looks_like_json_object(s) is True

    def test_chinese_punctuation_inside_json(self):
        """Chinese full-width quotes should not break JSON detection."""
        s = '{"key": "「中文内容」"}'
        assert looks_like_json_object(s) is True

    def test_trailing_comma(self):
        s = '{"a": 1, "b": 2,}'
        assert looks_like_json_object(s) is True

    def test_plain_text_is_not_json(self):
        assert looks_like_json_object("not json at all") is False

    def test_empty_string(self):
        assert looks_like_json_object("") is False

    def test_none(self):
        assert looks_like_json_object(None) is False

    def test_json_scalar_string_is_not_object(self):
        """Scalar strings are valid JSON but not objects/arrays."""
        assert looks_like_json_object('"just a string"') is False

    def test_json_scalar_number_is_not_object(self):
        assert looks_like_json_object("42") is False

    def test_json_scalar_bool_is_not_object(self):
        assert looks_like_json_object("true") is False

    def test_consistency_with_parse_json_response(self):
        """looks_like_json_object should agree with parse_json_response on
        samples that the latter can recover as dict.

        Bidirectional check: if parse_json_response succeeds, looks_like must
        say True; and if looks_like says True, parse_json_response must recover
        a dict.

        Arrays are intentionally excluded: looks_like_json_object is dict-only
        to match parse_json_response, which only returns dicts because
        _validate_keys rejects non-dicts.
        """
        samples = [
            '{"key": "value"}',
            '{"content": "是否重写"discovery"步骤"}',
            '{"a": "x"y"z", "b": "ok"}',
            '{"a": 1, "b": 2,}',
            '{"key": "「中文内容」"}',
        ]
        for s in samples:
            parsed = parse_json_response(s)
            looks_like = looks_like_json_object(s)
            # Forward: parse_json_response succeeds -> looks_like must say True
            if parsed is not None and isinstance(parsed, dict):
                assert looks_like is True, f"forward inconsistency for: {s[:60]}"
            # Reverse: looks_like says True -> parse_json_response must recover
            if looks_like is True:
                assert parsed is not None, f"reverse inconsistency for: {s[:60]}"
                assert isinstance(parsed, dict), f"reverse type mismatch for: {s[:60]}"

    def test_inconsistency_with_parse_json_response_negative(self):
        """Non-JSON text should fail both paths."""
        samples = ["hello world", '"just a string"', "42", ""]
        for s in samples:
            parsed = parse_json_response(s)
            looks_like = looks_like_json_object(s)
            # Both should fail
            assert parsed is None
            assert looks_like is False


class TestLooksLikeJson:
    """Tests for looks_like_json — accepts any valid JSON including scalars."""

    def test_valid_json_object(self):
        assert looks_like_json('{"a": 1}') is True

    def test_valid_json_array(self):
        assert looks_like_json('[1, 2, 3]') is True

    def test_json_scalar_string(self):
        """Scalar strings are valid JSON and should be recognized."""
        assert looks_like_json('"just a string"') is True

    def test_json_scalar_number(self):
        assert looks_like_json("42") is True

    def test_json_scalar_bool(self):
        assert looks_like_json("true") is True
        assert looks_like_json("false") is True

    def test_json_scalar_null(self):
        assert looks_like_json("null") is True

    def test_unescaped_quotes_recognized(self):
        """Should delegate to looks_like_json_object for composites."""
        s = '{"content": "是否重写"discovery"步骤"}'
        assert looks_like_json(s) is True

    def test_plain_text_is_not_json(self):
        assert looks_like_json("not json at all") is False

    def test_empty_string(self):
        assert looks_like_json("") is False

    def test_none(self):
        assert looks_like_json(None) is False

    def test_fenced_scalar_json_use_case(self):
        """The motivating use case: fenced blocks containing scalar JSON
        should be recognized as JSON and stripped from narrative."""
        # These are the scalars that looks_like_json_object would reject
        scalars = ['"hello"', "42", "true", "false", "null"]
        for scalar in scalars:
            assert looks_like_json(scalar) is True, f"scalar should be JSON: {scalar}"

    def test_array_with_trailing_comma(self):
        """Arrays needing repair (trailing comma) must be recognized."""
        assert looks_like_json("[1, 2, 3,]") is True

    def test_array_with_unescaped_quotes(self):
        """Arrays with unescaped interior quotes must be recognized."""
        assert looks_like_json('["a", "b", "x"y"z"]') is True

    def test_null_parsed_by_looks_like_json(self):
        """The literal JSON null value must be recognized."""
        assert looks_like_json("null") is True
