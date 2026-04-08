"""Tests for tool_formatters module."""

from __future__ import annotations

import pytest

from se3.engine.tool_formatters import (
    TOOL_FORMATTERS,
    format_tool_result_preview,
    format_tool_use_preview,
    truncate_preview,
)


# ---------------------------------------------------------------------------
# truncate_preview
# ---------------------------------------------------------------------------

class TestTruncatePreview:
    def test_empty_string(self):
        assert truncate_preview("") == ""

    def test_none_is_falsy(self):
        # None is falsy, should return ""
        assert truncate_preview(None) == ""

    def test_short_text_unchanged(self):
        assert truncate_preview("hello", max_length=10) == "hello"

    def test_exact_length_unchanged(self):
        assert truncate_preview("abcde", max_length=5) == "abcde"

    def test_long_text_truncated(self):
        result = truncate_preview("a" * 100, max_length=10)
        assert result == "aaaaaaa..."
        assert len(result) == 10

    def test_newlines_replaced(self):
        assert truncate_preview("line1\nline2", max_length=60) == "line1 line2"

    def test_custom_ellipsis(self):
        result = truncate_preview("a" * 20, max_length=10, ellipsis_str="..")
        assert result.endswith("..")
        assert len(result) == 10

    def test_very_short_max_length(self):
        result = truncate_preview("hello world", max_length=3)
        assert len(result) == 3

    def test_default_max_length_is_60(self):
        long_text = "x" * 100
        result = truncate_preview(long_text)
        assert len(result) == 60


# ---------------------------------------------------------------------------
# Generic formatters (via unknown tool name)
# ---------------------------------------------------------------------------

class TestGenericFormatters:
    def test_use_no_input(self):
        result = format_tool_use_preview("UnknownTool", {})
        assert result == "Tool: UnknownTool | Input: (none)"

    def test_use_none_input(self):
        result = format_tool_use_preview("UnknownTool", None)
        assert result == "Tool: UnknownTool | Input: (none)"

    def test_use_string_params(self):
        result = format_tool_use_preview("UnknownTool", {"key": "value"})
        assert "key=value" in result

    def test_use_numeric_params(self):
        result = format_tool_use_preview("UnknownTool", {"count": 42})
        assert "count=42" in result

    def test_use_truncates_at_3_params(self):
        data = {"a": 1, "b": 2, "c": 3, "d": 4}
        result = format_tool_use_preview("UnknownTool", data)
        assert "..." in result

    def test_result_none(self):
        result = format_tool_result_preview("UnknownTool", None)
        assert result == "Result: (empty)"

    def test_result_empty_string(self):
        result = format_tool_result_preview("UnknownTool", "   ")
        assert result == "Result: (empty)"

    def test_result_string(self):
        result = format_tool_result_preview("UnknownTool", "some output")
        assert "some output" in result

    def test_result_error_dict(self):
        data = {"isError": True, "content": "bad stuff"}
        result = format_tool_result_preview("UnknownTool", data)
        assert "error" in result.lower()
        assert "bad stuff" in result

    def test_result_dict(self):
        result = format_tool_result_preview("UnknownTool", {"key": "val"})
        assert "Result:" in result

    def test_result_list(self):
        result = format_tool_result_preview("UnknownTool", [1, 2, 3])
        assert "Result:" in result


# ---------------------------------------------------------------------------
# Edit formatter
# ---------------------------------------------------------------------------

class TestEditFormatter:
    def test_use_basic(self):
        data = {
            "file_path": "src/main.py",
            "old_string": "line1\nline2",
            "new_string": "line1\nline2\nline3",
        }
        result = format_tool_use_preview("Edit", data)
        assert "Edit:" in result
        assert "src/main.py" in result
        assert "2 lines" in result
        assert "3 lines" in result
        assert "\u2192" in result  # arrow

    def test_use_empty_strings(self):
        data = {"file_path": "a.py", "old_string": "", "new_string": ""}
        result = format_tool_use_preview("Edit", data)
        assert "Edit:" in result
        assert "a.py" in result

    def test_use_missing_file_path(self):
        result = format_tool_use_preview("Edit", {})
        assert "Edit:" in result
        assert "?" in result

    def test_use_long_path_truncated(self):
        data = {"file_path": "a/" * 40 + "file.py", "old_string": "x", "new_string": "y"}
        result = format_tool_use_preview("Edit", data)
        assert len(result) < 200

    def test_result_success(self):
        result = format_tool_result_preview("Edit", "✓ edited src/main.py")
        assert "Edit" in result
        assert "\u2713" in result

    def test_result_error(self):
        result = format_tool_result_preview("Edit", "Error: old_string not found")
        assert "Edit" in result
        assert "\u2717" in result

    def test_result_none(self):
        result = format_tool_result_preview("Edit", None)
        assert "Edit" in result


# ---------------------------------------------------------------------------
# Write formatter
# ---------------------------------------------------------------------------

class TestWriteFormatter:
    def test_use_with_content(self):
        data = {"file_path": "out.txt", "content": "line1\nline2\nline3"}
        result = format_tool_use_preview("Write", data)
        assert "Write:" in result
        assert "out.txt" in result
        assert "3 lines" in result

    def test_use_empty_content(self):
        data = {"file_path": "empty.txt", "content": ""}
        result = format_tool_use_preview("Write", data)
        assert "empty" in result.lower()

    def test_use_no_content_key(self):
        data = {"file_path": "x.py"}
        result = format_tool_use_preview("Write", data)
        assert "Write:" in result
        assert "empty" in result.lower()

    def test_result(self):
        result = format_tool_result_preview("Write", "Created out.txt")
        assert "Write" in result
        assert "\u2713" in result


# ---------------------------------------------------------------------------
# Read formatter
# ---------------------------------------------------------------------------

class TestReadFormatter:
    def test_use_simple(self):
        data = {"file_path": "src/app.py"}
        result = format_tool_use_preview("Read", data)
        assert "Read:" in result
        assert "src/app.py" in result

    def test_use_with_offset_and_limit(self):
        data = {"file_path": "big.log", "offset": 100, "limit": 50}
        result = format_tool_use_preview("Read", data)
        assert "Read:" in result
        assert "100-150" in result

    def test_use_with_offset_only(self):
        data = {"file_path": "x.py", "offset": 10}
        result = format_tool_use_preview("Read", data)
        assert "10-" in result

    def test_use_with_limit_only(self):
        data = {"file_path": "x.py", "limit": 20}
        result = format_tool_use_preview("Read", data)
        assert "20 lines" in result

    def test_result_with_content(self):
        result = format_tool_result_preview("Read", "line1\nline2\nline3")
        assert "Read" in result
        assert "3 lines" in result

    def test_result_empty(self):
        result = format_tool_result_preview("Read", "")
        assert "empty" in result.lower()


# ---------------------------------------------------------------------------
# Bash formatter
# ---------------------------------------------------------------------------

class TestBashFormatter:
    def test_use_short_command(self):
        data = {"command": "ls -la"}
        result = format_tool_use_preview("Bash", data)
        assert "Bash:" in result
        assert "ls -la" in result

    def test_use_long_command_truncated(self):
        data = {"command": "x" * 200}
        result = format_tool_use_preview("Bash", data)
        assert "..." in result
        assert len(result) < 100

    def test_use_empty_command(self):
        data = {"command": ""}
        result = format_tool_use_preview("Bash", data)
        assert "Bash:" in result

    def test_result_with_output(self):
        result = format_tool_result_preview("Bash", "output line 1\noutput line 2")
        assert "Bash" in result
        assert "2 lines" in result

    def test_result_no_output(self):
        result = format_tool_result_preview("Bash", "")
        assert "no output" in result.lower()


# ---------------------------------------------------------------------------
# Grep formatter
# ---------------------------------------------------------------------------

class TestGrepFormatter:
    def test_use_basic(self):
        data = {"pattern": "TODO", "path": "src/"}
        result = format_tool_use_preview("Grep", data)
        assert "Grep:" in result
        assert "/TODO/" in result
        assert "src/" in result

    def test_use_default_path(self):
        data = {"pattern": "error"}
        result = format_tool_use_preview("Grep", data)
        assert "/error/" in result
        assert "." in result  # default path

    def test_result_with_matches(self):
        result = format_tool_result_preview("Grep", "file1.py\nfile2.py")
        assert "Grep" in result
        assert "2 matches" in result

    def test_result_no_matches(self):
        result = format_tool_result_preview("Grep", "")
        assert "no matches" in result.lower()


# ---------------------------------------------------------------------------
# Glob formatter
# ---------------------------------------------------------------------------

class TestGlobFormatter:
    def test_use_basic(self):
        data = {"pattern": "**/*.py", "path": "src/"}
        result = format_tool_use_preview("Glob", data)
        assert "Glob:" in result
        assert "**/*.py" in result
        assert "src/" in result

    def test_use_default_path(self):
        data = {"pattern": "*.md"}
        result = format_tool_use_preview("Glob", data)
        assert "*.md" in result
        assert "." in result

    def test_result_with_files(self):
        result = format_tool_result_preview("Glob", "a.py\nb.py\nc.py")
        assert "Glob" in result
        assert "3 files" in result

    def test_result_no_files(self):
        result = format_tool_result_preview("Glob", "")
        assert "no files" in result.lower()


# ---------------------------------------------------------------------------
# Registry structure
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_all_expected_tools_registered(self):
        expected = {"Edit", "Write", "Read", "Bash", "Grep", "Glob"}
        assert set(TOOL_FORMATTERS.keys()) == expected

    def test_each_entry_has_use_and_result(self):
        for name, entry in TOOL_FORMATTERS.items():
            assert "use" in entry, f"{name} missing 'use'"
            assert "result" in entry, f"{name} missing 'result'"
            assert callable(entry["use"]), f"{name}['use'] not callable"
            assert callable(entry["result"]), f"{name}['result'] not callable"


# ---------------------------------------------------------------------------
# Routing: registered vs unregistered
# ---------------------------------------------------------------------------

class TestRouting:
    def test_registered_tool_uses_specific_formatter(self):
        # Edit should produce "Edit: ..." not "Tool: Edit | Input: ..."
        data = {"file_path": "a.py", "old_string": "x", "new_string": "y"}
        result = format_tool_use_preview("Edit", data)
        assert result.startswith("Edit:")

    def test_unregistered_tool_uses_generic(self):
        result = format_tool_use_preview("Agent", {"prompt": "do stuff"})
        assert result.startswith("Tool: Agent")

    def test_result_registered_tool(self):
        result = format_tool_result_preview("Bash", "hello world")
        assert "Bash" in result

    def test_result_unregistered_tool(self):
        result = format_tool_result_preview("Agent", "some output")
        assert "Result:" in result

    def test_format_tool_result_preview_requires_tool_name(self):
        # The new signature has tool_name as first arg
        result = format_tool_result_preview("Edit", None)
        assert "Edit" in result
