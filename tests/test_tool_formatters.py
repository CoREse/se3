"""Tests for tool_formatters module."""

from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

from se3.engine.tool_formatters import (
    TOOL_FORMATTERS,
    format_tool_diff,
    format_tool_result_preview,
    format_tool_use_preview,
    generate_edit_diff,
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

    def test_edit_and_write_have_diff_key(self):
        assert "diff" in TOOL_FORMATTERS["Edit"]
        assert "diff" in TOOL_FORMATTERS["Write"]


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


# ---------------------------------------------------------------------------
# generate_edit_diff
# ---------------------------------------------------------------------------

class TestGenerateEditDiff:
    def test_basic_replacement(self):
        diff = generate_edit_diff("hello\n", "world\n", "test.py")
        assert any(line.startswith("-hello") for line in diff)
        assert any(line.startswith("+world") for line in diff)

    def test_identical_strings_empty(self):
        assert generate_edit_diff("same", "same", "f.py") == []

    def test_empty_to_content(self):
        diff = generate_edit_diff("", "new line\n", "f.py")
        assert any(line.startswith("+new line") for line in diff)

    def test_content_to_empty(self):
        diff = generate_edit_diff("old line\n", "", "f.py")
        assert any(line.startswith("-old line") for line in diff)

    def test_both_empty(self):
        assert generate_edit_diff("", "", "f.py") == []

    def test_multiline_change(self):
        old = "line1\nline2\nline3\n"
        new = "line1\nchanged\nline3\n"
        diff = generate_edit_diff(old, new, "test.py")
        assert any(line.startswith("-line2") for line in diff)
        assert any(line.startswith("+changed") for line in diff)
        # Context lines should be present
        assert any("line1" in line and not line.startswith(("-", "+", "@@")) for line in diff)

    def test_has_hunk_header(self):
        diff = generate_edit_diff("a\n", "b\n", "f.py")
        assert any(line.startswith("@@") for line in diff)

    def test_has_file_header(self):
        diff = generate_edit_diff("a\n", "b\n", "src/main.py")
        assert any("a/src/main.py" in line for line in diff)
        assert any("b/src/main.py" in line for line in diff)


# ---------------------------------------------------------------------------
# render_diff (display.py)
# ---------------------------------------------------------------------------

class TestRenderDiff:
    def _capture_render(self, diff_lines, file_path="test.py", max_lines=50):
        """Render diff to a string buffer and return output."""
        from se3.engine.display import render_diff, set_console
        buf = io.StringIO()
        test_console = Console(file=buf, force_terminal=True, width=120)
        set_console(test_console)
        try:
            render_diff(diff_lines, file_path, max_lines=max_lines)
        finally:
            set_console(None)
        return buf.getvalue()

    def test_renders_added_lines_green(self):
        diff_lines = ["@@ -1 +1 @@", "-old", "+new"]
        output = self._capture_render(diff_lines)
        # Rich terminal output should contain the text
        assert "new" in output
        assert "old" in output

    def test_renders_hunk_header(self):
        diff_lines = ["@@ -1,3 +1,3 @@", " ctx", "-old", "+new"]
        output = self._capture_render(diff_lines)
        assert "@@" in output

    def test_file_path_in_panel_title(self):
        diff_lines = ["@@ -1 +1 @@", "-a", "+b"]
        output = self._capture_render(diff_lines, file_path="src/app.py")
        assert "src/app.py" in output

    def test_empty_diff_no_output(self):
        output = self._capture_render([])
        assert output == ""

    def test_truncation(self):
        # Generate more lines than max_lines
        diff_lines = ["@@ -1 +1 @@"] + [f"+line{i}" for i in range(60)]
        output = self._capture_render(diff_lines, max_lines=10)
        assert "more lines" in output

    def test_skips_file_headers(self):
        diff_lines = ["--- a/f.py", "+++ b/f.py", "@@ -1 +1 @@", "-old", "+new"]
        output = self._capture_render(diff_lines)
        # The --- / +++ headers should be skipped, but content should appear
        assert "old" in output
        assert "new" in output


# ---------------------------------------------------------------------------
# format_tool_diff
# ---------------------------------------------------------------------------

class TestFormatToolDiff:
    def _capture_diff(self, tool_name, input_data, result_data):
        from se3.engine.display import set_console
        buf = io.StringIO()
        test_console = Console(file=buf, force_terminal=True, width=120)
        set_console(test_console)
        try:
            format_tool_diff(tool_name, input_data, result_data)
        finally:
            set_console(None)
        return buf.getvalue()

    def test_edit_renders_diff(self):
        input_data = {
            "file_path": "src/main.py",
            "old_string": "hello\n",
            "new_string": "world\n",
        }
        output = self._capture_diff("Edit", input_data, "✓ edited src/main.py")
        assert "hello" in output
        assert "world" in output

    def test_edit_identical_no_output(self):
        input_data = {
            "file_path": "f.py",
            "old_string": "same",
            "new_string": "same",
        }
        output = self._capture_diff("Edit", input_data, "done")
        assert output == ""

    def test_write_created_shows_summary(self):
        input_data = {
            "file_path": "new_file.py",
            "content": "line1\nline2\nline3\n",
        }
        output = self._capture_diff("Write", input_data, "Created new_file.py")
        assert "Created" in output
        assert "new_file.py" in output
        assert "lines" in output

    def test_write_overwrite_no_diff(self):
        # Write overwriting existing file - no "Created" in result
        input_data = {
            "file_path": "existing.py",
            "content": "new content\n",
        }
        output = self._capture_diff("Write", input_data, "Wrote existing.py")
        # Should not render anything (no diff for overwrites)
        assert output == ""

    def test_unknown_tool_no_output(self):
        output = self._capture_diff("Bash", {"command": "ls"}, "output")
        assert output == ""

    def test_edit_missing_fields_no_crash(self):
        output = self._capture_diff("Edit", {}, "done")
        assert output == ""  # empty old == empty new → no diff


# ---------------------------------------------------------------------------
# StreamJSONTracker integration
# ---------------------------------------------------------------------------

class TestStreamJSONTrackerDiff:
    def _make_tracker(self):
        from se3.engine.llm_caller import StreamJSONTracker
        return StreamJSONTracker()

    def _tool_use_event(self, tool_name, tool_input, tool_use_id="tu_1"):
        return json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": tool_input,
                }]
            }
        })

    def _tool_result_event(self, tool_use_id="tu_1", content="done", is_error=False):
        return json.dumps({
            "type": "tool_result",
            "result": {
                "toolUseId": tool_use_id,
                "content": content,
                "isError": is_error,
            }
        })

    def test_edit_input_cached(self):
        tracker = self._make_tracker()
        input_data = {"file_path": "f.py", "old_string": "a", "new_string": "b"}
        tracker.process_line(self._tool_use_event("Edit", input_data))
        assert "tu_1" in tracker._tool_use_id_to_input
        assert tracker._tool_use_id_to_input["tu_1"] == input_data

    def test_write_input_cached(self):
        tracker = self._make_tracker()
        input_data = {"file_path": "f.py", "content": "hello"}
        tracker.process_line(self._tool_use_event("Write", input_data))
        assert "tu_1" in tracker._tool_use_id_to_input

    def test_non_edit_write_not_cached(self):
        tracker = self._make_tracker()
        tracker.process_line(self._tool_use_event("Bash", {"command": "ls"}))
        assert "tu_1" not in tracker._tool_use_id_to_input

    def test_cache_consumed_on_result(self):
        from se3.engine.display import set_console
        buf = io.StringIO()
        set_console(Console(file=buf, force_terminal=True, width=120))
        try:
            tracker = self._make_tracker()
            input_data = {"file_path": "f.py", "old_string": "a\n", "new_string": "b\n"}
            tracker.process_line(self._tool_use_event("Edit", input_data))
            assert "tu_1" in tracker._tool_use_id_to_input
            tracker.process_line(self._tool_result_event("tu_1", "✓ edited"))
            # Cache should be consumed
            assert "tu_1" not in tracker._tool_use_id_to_input
        finally:
            set_console(None)

    def test_error_result_no_diff(self):
        tracker = self._make_tracker()
        input_data = {"file_path": "f.py", "old_string": "a", "new_string": "b"}
        tracker.process_line(self._tool_use_event("Edit", input_data))
        tracker.process_line(self._tool_result_event("tu_1", "old_string not found", is_error=True))
        # On error, cache is NOT consumed by diff (error branch skips it)
        # but it stays in cache since the pop only happens in the else branch
        assert "tu_1" in tracker._tool_use_id_to_input

    def test_full_edit_flow_renders_diff(self):
        from se3.engine.display import set_console
        buf = io.StringIO()
        set_console(Console(file=buf, force_terminal=True, width=120))
        try:
            tracker = self._make_tracker()
            input_data = {
                "file_path": "src/app.py",
                "old_string": "def hello():\n    pass\n",
                "new_string": "def hello():\n    return 'world'\n",
            }
            tracker.process_line(self._tool_use_event("Edit", input_data))
            tracker.process_line(self._tool_result_event("tu_1", "✓ edited src/app.py"))
        finally:
            set_console(None)
        output = buf.getvalue()
        assert "src/app.py" in output
        assert "pass" in output
        assert "world" in output
