"""Tests for tool_formatters module."""

from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

from tianluo.engine.tool_formatters import (
    TOOL_FORMATTERS,
    build_tool_detail_payload,
    build_tool_in_flight_detail_payload,
    format_tool_chip_header,
    format_tool_chip_in_flight_header,
    format_tool_diff,
    format_tool_result_preview,
    format_tool_use_preview,
    generate_edit_diff,
    truncate_preview,
)
from tianluo.engine.truncation import TOOL_DETAIL_PAYLOAD_MAX_CHARS


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
        # A missing "content" key means the upstream carried no content at all,
        # which is not the same as writing an empty file — only the path shows.
        data = {"file_path": "x.py"}
        result = format_tool_use_preview("Write", data)
        assert "Write:" in result
        assert "x.py" in result
        assert "empty" not in result.lower()

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
        from tianluo.engine.display import render_diff, set_console
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

    def test_added_line_has_new_line_number(self):
        diff_lines = ["@@ -1,2 +1,3 @@", " ctx", "+added", " ctx2"]
        output = self._capture_render(diff_lines)
        # +added is at new-file line 2
        assert "2" in output
        assert "added" in output

    def test_removed_line_has_old_line_number(self):
        diff_lines = ["@@ -5,2 +5,1 @@", "-removed", " ctx"]
        output = self._capture_render(diff_lines)
        # -removed is at old-file line 5
        assert "5" in output
        assert "removed" in output

    def test_context_line_has_line_number(self):
        diff_lines = ["@@ -10,3 +10,3 @@", " ctx_first", "-old", "+new"]
        output = self._capture_render(diff_lines)
        # context line at new-file line 10
        assert "10" in output

    def test_multi_hunk_line_numbers_reset(self):
        diff_lines = [
            "@@ -1,2 +1,2 @@", "-old1", "+new1", " ctx",
            "@@ -50,2 +50,2 @@", "-old50", "+new50", " ctx50",
        ]
        output = self._capture_render(diff_lines)
        # Second hunk starts at line 50
        assert "50" in output
        assert "new50" in output

    def test_hunk_header_has_no_line_number_prefix(self):
        # Hunk header lines should not have a numeric prefix before @@
        diff_lines = ["@@ -1 +1 @@", "+a"]
        output = self._capture_render(diff_lines)
        # The @@ line itself shouldn't be preceded by a line number.
        # We verify by checking that the first non-whitespace on the @@ line
        # is the @@ marker, not a digit.  Since Rich output is styled, we
        # look for the pattern more loosely: the output should NOT contain
        # a digit immediately before "@@" on the same segment.
        # Simplest check: line numbers are 4-char wide; hunk header should
        # not have "   0 @@" or similar.
        assert "0 @@" not in output


# ---------------------------------------------------------------------------
# format_tool_diff
# ---------------------------------------------------------------------------

class TestFormatToolDiff:
    def _capture_diff(self, tool_name, input_data, result_data, old_content=None):
        from tianluo.engine.display import set_console
        buf = io.StringIO()
        test_console = Console(file=buf, force_terminal=True, width=120)
        set_console(test_console)
        try:
            format_tool_diff(tool_name, input_data, result_data, old_content=old_content)
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

    def test_write_overwrite_no_diff_without_old_content(self):
        # Write overwriting existing file without old_content → "Created" summary
        # (old_content=None means new file, which is the default)
        input_data = {
            "file_path": "existing.py",
            "content": "new content\n",
        }
        output = self._capture_diff("Write", input_data, "Wrote existing.py")
        # Without old_content, treated as new file → "Created" summary
        assert "Created" in output

    def test_unknown_tool_no_output(self):
        output = self._capture_diff("Bash", {"command": "ls"}, "output")
        assert output == ""

    def test_write_overwrite_with_old_content_renders_diff(self):
        input_data = {
            "file_path": "existing.py",
            "content": "new content\nline2\n",
        }
        output = self._capture_diff(
            "Write", input_data, "Wrote existing.py",
            old_content="old content\nline2\n",
        )
        assert "old content" in output
        assert "new content" in output
        assert "existing.py" in output

    def test_write_overwrite_empty_file_renders_diff(self):
        # old_content="" means overwriting an empty file
        input_data = {
            "file_path": "empty.py",
            "content": "new line\n",
        }
        output = self._capture_diff(
            "Write", input_data, "Wrote empty.py",
            old_content="",
        )
        assert "new line" in output

    def test_write_overwrite_identical_no_diff(self):
        input_data = {
            "file_path": "same.py",
            "content": "same content\n",
        }
        output = self._capture_diff(
            "Write", input_data, "Wrote same.py",
            old_content="same content\n",
        )
        # Identical content → no diff rendered
        assert output == ""

    def test_write_new_file_old_content_none_shows_created(self):
        input_data = {
            "file_path": "brand_new.py",
            "content": "hello\n",
        }
        output = self._capture_diff(
            "Write", input_data, "Created brand_new.py",
            old_content=None,
        )
        assert "Created" in output

    def test_edit_missing_fields_no_crash(self):
        output = self._capture_diff("Edit", {}, "done")
        assert output == ""  # empty old == empty new → no diff


# ---------------------------------------------------------------------------
# StreamJSONTracker integration
# ---------------------------------------------------------------------------

class TestStreamJSONTrackerDiff:
    def _make_tracker(self):
        from tianluo.engine.llm_caller import StreamJSONTracker
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
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": is_error,
                }]
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

    def test_non_edit_write_also_cached(self):
        """Every tool's input is now cached so the single-chip terminal emit
        on tool_result can build a merged header (input summary + result
        summary) via format_tool_chip_header and a structured detail payload
        via build_tool_detail_payload. The Write-specific old_content snapshot
        still applies only to Write."""
        tracker = self._make_tracker()
        tracker.process_line(self._tool_use_event("Bash", {"command": "ls"}))
        assert tracker._tool_use_id_to_input["tu_1"] == {"command": "ls"}
        assert "tu_1" not in tracker._tool_use_id_to_old_content

    def test_cache_consumed_on_result(self):
        from tianluo.engine.display import set_console
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
        # On error, cache should be cleaned up to prevent leaks (#057)
        assert "tu_1" not in tracker._tool_use_id_to_input
        assert "tu_1" not in tracker._tool_use_id_to_name

    def test_full_edit_flow_renders_diff(self):
        from tianluo.engine.display import set_console
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

    def test_write_overwrite_caches_old_content(self, tmp_path):
        """Write tool_use for existing file caches old content."""
        target = tmp_path / "existing.py"
        target.write_text("old stuff\n", encoding="utf-8")
        tracker = self._make_tracker()
        input_data = {"file_path": str(target), "content": "new stuff\n"}
        tracker.process_line(self._tool_use_event("Write", input_data))
        assert "tu_1" in tracker._tool_use_id_to_old_content
        assert tracker._tool_use_id_to_old_content["tu_1"] == "old stuff\n"

    def test_write_new_file_caches_none(self):
        """Write tool_use for non-existent file caches None."""
        tracker = self._make_tracker()
        input_data = {"file_path": "/nonexistent/path/file.py", "content": "x"}
        tracker.process_line(self._tool_use_event("Write", input_data))
        assert "tu_1" in tracker._tool_use_id_to_old_content
        assert tracker._tool_use_id_to_old_content["tu_1"] is None

    def test_write_overwrite_full_flow(self, tmp_path):
        """Full Write overwrite flow: tool_use → read file → tool_result → diff rendered."""
        from tianluo.engine.display import set_console
        target = tmp_path / "overwrite.py"
        target.write_text("old line\n", encoding="utf-8")
        buf = io.StringIO()
        set_console(Console(file=buf, force_terminal=True, width=120))
        try:
            tracker = self._make_tracker()
            input_data = {"file_path": str(target), "content": "new line\n"}
            tracker.process_line(self._tool_use_event("Write", input_data))
            tracker.process_line(self._tool_result_event("tu_1", "Wrote overwrite.py"))
        finally:
            set_console(None)
        output = buf.getvalue()
        assert "old line" in output
        assert "new line" in output
        # old_content cache should be consumed
        assert "tu_1" not in tracker._tool_use_id_to_old_content

    def test_write_new_file_shows_created(self):
        """Write to non-existent file shows Created summary (old_content=None)."""
        from tianluo.engine.display import set_console
        buf = io.StringIO()
        set_console(Console(file=buf, force_terminal=True, width=120))
        try:
            tracker = self._make_tracker()
            input_data = {"file_path": "/tmp/new_file.py", "content": "hello\n"}
            tracker.process_line(self._tool_use_event("Write", input_data))
            tracker.process_line(self._tool_result_event("tu_1", "Created /tmp/new_file.py"))
        finally:
            set_console(None)
        output = buf.getvalue()
        assert "Created" in output

    def test_print_summary_clears_caches(self):
        """print_summary() should clear all caches."""
        tracker = self._make_tracker()
        input_data = {"file_path": "f.py", "old_string": "a", "new_string": "b"}
        tracker.process_line(self._tool_use_event("Edit", input_data))
        # Populate old_content cache manually
        tracker._tool_use_id_to_old_content["tu_1"] = "old"
        assert len(tracker._tool_use_id_to_input) > 0
        assert len(tracker._tool_use_id_to_name) > 0
        tracker.print_summary()
        assert len(tracker._tool_use_id_to_input) == 0
        assert len(tracker._tool_use_id_to_old_content) == 0
        assert len(tracker._tool_use_id_to_name) == 0


# ---------------------------------------------------------------------------
# format_tool_chip_in_flight_header / format_tool_chip_header
# ---------------------------------------------------------------------------

class TestChipHeader:
    def test_in_flight_read(self):
        header = format_tool_chip_in_flight_header(
            "Read", {"file_path": "src/app.py", "offset": 0, "limit": 200}
        )
        assert header.startswith("Read:")
        assert "src/app.py" in header
        assert "0-200" in header
        # in-flight header does NOT contain ✓ or ✗
        assert "✓" not in header
        assert "✗" not in header

    def test_in_flight_unknown_tool(self):
        header = format_tool_chip_in_flight_header(
            "MysteryTool", {"foo": "bar"}
        )
        assert "MysteryTool" in header
        assert "foo=bar" in header

    def test_in_flight_unknown_tool_leads_with_its_own_name(self):
        # WHY this matters: the frontend reads a structured chip fragment's
        # tool name as the first token inside the bracket. The old generic
        # framing `Tool: MysteryTool | Input: ...` made that token "Tool", so
        # the terminal fragment `[MysteryTool ✓ ...]` looked like a different
        # tool and blanked the chip header when it upgraded it.
        header = format_tool_chip_in_flight_header(
            "MysteryTool", {"foo": "bar"}
        )
        assert header == "MysteryTool: foo=bar"

    def test_in_flight_agent_matches_terminal_leading_token(self):
        in_flight = format_tool_chip_in_flight_header(
            "Agent", {"description": "self check", "prompt": "look"}
        )
        terminal = format_tool_chip_header(
            "Agent",
            {"description": "self check", "prompt": "look"},
            "found nothing",
            is_error=False,
        )
        assert in_flight.split(":")[0] == terminal.split(" ")[0] == "Agent"

    def test_in_flight_codex_mcp_name_kept_whole(self):
        # codex_runner synthesizes `mcp__<server>__<tool>`; the double
        # underscores must survive into the chip name.
        header = format_tool_chip_in_flight_header(
            "mcp__ctx7__get-library-docs", {"library": "fastapi"}
        )
        assert header == "mcp__ctx7__get-library-docs: library=fastapi"

    def test_in_flight_codex_unknown_name(self):
        header = format_tool_chip_in_flight_header("unknown", {"raw": "x"})
        assert header == "unknown: raw=x"

    def test_in_flight_empty_input_is_none_body(self):
        assert format_tool_chip_in_flight_header("Agent", {}) == "Agent: (none)"

    def test_in_flight_registered_tools_byte_identical(self):
        """Registered tools keep the exact header they produced before."""
        assert format_tool_chip_in_flight_header(
            "Read", {"file_path": "src/app.py", "offset": 0, "limit": 200}
        ) == "Read: src/app.py:0-200"
        assert format_tool_chip_in_flight_header(
            "Bash", {"command": "pytest -q"}
        ) == "Bash: pytest -q"
        assert format_tool_chip_in_flight_header(
            "Grep", {"pattern": "def ", "path": "src"}
        ) == "Grep: /def / in src"
        assert format_tool_chip_in_flight_header(
            "Glob", {"pattern": "*.py", "path": "src"}
        ) == "Glob: *.py in src"

    def test_success_read_merges_use_and_result(self):
        header = format_tool_chip_header(
            "Read",
            {"file_path": "src/app.py", "offset": 0, "limit": 200},
            "line1\nline2\nline3",
            is_error=False,
        )
        assert header.startswith("Read ✓")
        assert "src/app.py" in header
        assert "0-200" in header
        assert "3 lines" in header
        assert "✗" not in header

    def test_success_edit_carries_line_counts(self):
        header = format_tool_chip_header(
            "Edit",
            {"file_path": "f.py", "old_string": "a\nb", "new_string": "a\nb\nc\nd"},
            "✓ edited",
            is_error=False,
        )
        assert header.startswith("Edit ✓")
        assert "f.py" in header
        assert "2 lines" in header
        assert "4 lines" in header

    def test_success_write(self):
        header = format_tool_chip_header(
            "Write",
            {"file_path": "out.txt", "content": "x\ny\nz"},
            "Wrote out.txt",
            is_error=False,
        )
        assert header.startswith("Write ✓")
        assert "out.txt" in header
        assert "3 lines" in header

    def test_success_bash(self):
        header = format_tool_chip_header(
            "Bash",
            {"command": "ls -la"},
            "a\nb\nc",
            is_error=False,
        )
        assert header.startswith("Bash ✓")
        assert "ls -la" in header
        assert "3 lines" in header

    def test_success_grep(self):
        header = format_tool_chip_header(
            "Grep",
            {"pattern": "TODO", "path": "src/"},
            "a.py\nb.py",
            is_error=False,
        )
        assert header.startswith("Grep ✓")
        assert "TODO" in header
        assert "2 matches" in header

    def test_success_glob(self):
        header = format_tool_chip_header(
            "Glob",
            {"pattern": "**/*.py", "path": "src/"},
            "x.py\ny.py\nz.py",
            is_error=False,
        )
        assert header.startswith("Glob ✓")
        assert "**/*.py" in header
        assert "3 files" in header

    def test_failure_read_includes_error_preview(self):
        header = format_tool_chip_header(
            "Read",
            {"file_path": "missing.py"},
            {"is_error": True, "content": "ENOENT: no such file or directory"},
            is_error=True,
        )
        assert header.startswith("Read ✗")
        assert "missing.py" in header
        assert "ENOENT" in header
        assert "✓" not in header

    def test_failure_truncates_long_error(self):
        long_err = "x" * 500
        header = format_tool_chip_header(
            "Read",
            {"file_path": "f.py"},
            {"is_error": True, "content": long_err},
            is_error=True,
        )
        assert header.startswith("Read ✗")
        # error preview must be truncated (not 500 chars of x)
        assert len(header) < 200

    def test_failure_unknown_tool(self):
        header = format_tool_chip_header(
            "MysteryTool",
            {"key": "val"},
            "boom",
            is_error=True,
        )
        assert header.startswith("MysteryTool ✗")
        assert "boom" in header

    def test_success_unknown_tool_falls_back(self):
        header = format_tool_chip_header(
            "MysteryTool",
            {"key": "val"},
            "ok",
            is_error=False,
        )
        assert header.startswith("MysteryTool ✓")

    def test_no_brackets_in_header(self):
        # Chip header must NOT carry the surrounding [ ] — that's the frontend's job
        header = format_tool_chip_header(
            "Read",
            {"file_path": "f.py"},
            "line",
            is_error=False,
        )
        assert not header.startswith("[")
        assert not header.endswith("]")


# ---------------------------------------------------------------------------
# build_tool_detail_payload
# ---------------------------------------------------------------------------

class TestDetailPayloadEdit:
    def test_edit_diff_kind_and_fields(self):
        payload = build_tool_detail_payload(
            "Edit",
            {
                "file_path": "src/main.py",
                "old_string": "hello\nworld\n",
                "new_string": "hello\nthere\nworld\n",
            },
            "✓ edited",
        )
        assert payload["kind"] == "edit_diff"
        assert payload["file_path"] == "src/main.py"
        assert "hello" in payload["diff"]
        assert "+there" in payload["diff"]
        # Hunk start parsed from "@@ -<a>,<b> +<c>,<d> @@"
        assert payload["old_start_line"] is not None
        assert payload["new_start_line"] is not None
        assert payload["truncated"] is False

    def test_edit_diff_identical_strings(self):
        payload = build_tool_detail_payload(
            "Edit",
            {"file_path": "f.py", "old_string": "same", "new_string": "same"},
            "done",
        )
        assert payload["kind"] == "edit_diff"
        assert payload["diff"] == ""
        # No hunks → both line numbers None
        assert payload["old_start_line"] is None
        assert payload["new_start_line"] is None


class TestDetailPayloadWrite:
    def test_write_full_kind_when_old_content_none(self):
        payload = build_tool_detail_payload(
            "Write",
            {"file_path": "new.py", "content": "line1\nline2\n"},
            "Created new.py",
            old_content=None,
        )
        assert payload["kind"] == "write_full"
        assert payload["file_path"] == "new.py"
        assert "line1" in payload["content"]
        assert payload["start_line"] == 1
        assert payload["truncated"] is False

    def test_write_diff_kind_when_old_content_provided(self):
        payload = build_tool_detail_payload(
            "Write",
            {"file_path": "existing.py", "content": "new\nline\n"},
            "Wrote existing.py",
            old_content="old\nline\n",
        )
        assert payload["kind"] == "write_diff"
        assert "-old" in payload["diff"]
        assert "+new" in payload["diff"]
        assert payload["old_start_line"] is not None
        assert payload["new_start_line"] is not None


class TestDetailPayloadRead:
    def test_read_text_kind(self):
        payload = build_tool_detail_payload(
            "Read",
            {"file_path": "f.py", "offset": 100, "limit": 50},
            "a\nb\nc\n",
        )
        assert payload["kind"] == "read_text"
        assert payload["file_path"] == "f.py"
        assert payload["text"] == "a\nb\nc\n"
        # offset → start_line is 1-based
        assert payload["start_line"] == 101
        assert payload["truncated"] is False

    def test_read_zero_offset_starts_at_line_1(self):
        payload = build_tool_detail_payload(
            "Read",
            {"file_path": "f.py"},
            "line\n",
        )
        assert payload["start_line"] == 1


class TestDetailPayloadBash:
    def test_bash_output_kind_carries_command_and_stdout(self):
        payload = build_tool_detail_payload(
            "Bash",
            {"command": "ls -la"},
            "a\nb\nc",
        )
        assert payload["kind"] == "bash_output"
        assert payload["command"] == "ls -la"
        assert payload["stdout"] == "a\nb\nc"
        assert payload["stderr"] == ""
        assert payload["truncated"] is False

    def test_bash_split_stdout_stderr_when_dict(self):
        payload = build_tool_detail_payload(
            "Bash",
            {"command": "false"},
            {"stdout": "ok", "stderr": "boom"},
        )
        assert payload["stdout"] == "ok"
        assert payload["stderr"] == "boom"


class TestDetailPayloadGrep:
    def test_grep_matches_list(self):
        payload = build_tool_detail_payload(
            "Grep",
            {"pattern": "TODO", "path": "src/"},
            "file1.py\nfile2.py\nfile3.py",
        )
        assert payload["kind"] == "grep_matches"
        assert payload["pattern"] == "TODO"
        assert payload["path"] == "src/"
        assert payload["matches"] == ["file1.py", "file2.py", "file3.py"]
        assert payload["truncated"] is False


class TestDetailPayloadGlob:
    def test_glob_matches_list(self):
        payload = build_tool_detail_payload(
            "Glob",
            {"pattern": "**/*.py", "path": "src/"},
            "a.py\nb.py",
        )
        assert payload["kind"] == "glob_matches"
        assert payload["files"] == ["a.py", "b.py"]
        assert payload["truncated"] is False


class TestDetailPayloadTextFallback:
    def test_unregistered_tool_returns_text_kind(self):
        payload = build_tool_detail_payload(
            "MysteryTool",
            {"prompt": "do something"},
            "the result",
        )
        assert payload["kind"] == "text"
        assert payload["text"] == "the result"
        assert payload["truncated"] is False

    def test_text_payload_handles_dict_result(self):
        payload = build_tool_detail_payload(
            "MysteryTool",
            {},
            {"content": [{"type": "text", "text": "nested"}]},
        )
        assert payload["kind"] == "text"
        assert "nested" in payload["text"]


class TestDetailPayloadTruncation:
    def test_long_read_text_truncated(self):
        long_text = "x" * (TOOL_DETAIL_PAYLOAD_MAX_CHARS + 1000)
        payload = build_tool_detail_payload(
            "Read",
            {"file_path": "big.txt"},
            long_text,
        )
        assert payload["truncated"] is True
        assert len(payload["text"]) == TOOL_DETAIL_PAYLOAD_MAX_CHARS

    def test_long_write_full_truncated(self):
        long_content = "y" * (TOOL_DETAIL_PAYLOAD_MAX_CHARS + 500)
        payload = build_tool_detail_payload(
            "Write",
            {"file_path": "big.txt", "content": long_content},
            "Created big.txt",
            old_content=None,
        )
        assert payload["truncated"] is True
        assert len(payload["content"]) == TOOL_DETAIL_PAYLOAD_MAX_CHARS

    def test_too_many_grep_matches_truncated(self):
        text = "\n".join(f"match{i}" for i in range(2000))
        payload = build_tool_detail_payload(
            "Grep",
            {"pattern": "x", "path": "."},
            text,
        )
        assert payload["truncated"] is True
        assert len(payload["matches"]) == 1000

    def test_too_many_glob_files_truncated(self):
        text = "\n".join(f"file{i}.py" for i in range(2000))
        payload = build_tool_detail_payload(
            "Glob",
            {"pattern": "**/*.py", "path": "."},
            text,
        )
        assert payload["truncated"] is True
        assert len(payload["files"]) == 1000


class TestDetailPayloadJsonSafe:
    def test_all_payloads_are_json_serializable(self):
        cases = [
            ("Edit", {"file_path": "f.py", "old_string": "a", "new_string": "b"}, "ok", None),
            ("Write", {"file_path": "f.py", "content": "x"}, "ok", None),
            ("Write", {"file_path": "f.py", "content": "x"}, "ok", "y"),
            ("Read", {"file_path": "f.py", "offset": 0}, "line", None),
            ("Bash", {"command": "ls"}, "out", None),
            ("Grep", {"pattern": "p", "path": "."}, "a\nb", None),
            ("Glob", {"pattern": "*", "path": "."}, "x", None),
            ("MysteryTool", {"k": "v"}, "ok", None),
        ]
        for tool, use, result, old in cases:
            payload = build_tool_detail_payload(tool, use, result, old_content=old)
            # Must round-trip through JSON
            blob = json.dumps(payload)
            restored = json.loads(blob)
            assert restored["kind"] == payload["kind"]


# ---------------------------------------------------------------------------
# Missing content keys vs. present-but-empty content
#
# Upstreams that only report *that* a file changed (codex's file_change items)
# omit the content keys entirely; upstreams that really wrote an empty file
# send the key with "". These must render differently — the first has no line
# count to show, the second does.
# ---------------------------------------------------------------------------

class TestMissingContentKeyRendering:
    def test_write_use_key_missing_shows_path_only(self):
        result = format_tool_use_preview("Write", {"file_path": "src/new.py"})
        assert result == "Write: src/new.py"
        assert "lines" not in result
        assert "empty" not in result.lower()

    def test_write_use_key_empty_string_unchanged(self):
        result = format_tool_use_preview("Write", {"file_path": "src/new.py", "content": ""})
        assert result == "Write: src/new.py (empty)"

    def test_write_use_missing_and_empty_differ(self):
        missing = format_tool_use_preview("Write", {"file_path": "a.py"})
        empty = format_tool_use_preview("Write", {"file_path": "a.py", "content": ""})
        assert missing != empty

    def test_write_use_with_content_unchanged(self):
        result = format_tool_use_preview("Write", {"file_path": "a.py", "content": "x\ny"})
        assert result == "Write: a.py (2 lines)"

    def test_edit_use_both_keys_missing_shows_path_only(self):
        result = format_tool_use_preview("Edit", {"file_path": "src/mod.py"})
        assert result == "Edit: src/mod.py"
        assert "lines" not in result

    def test_edit_use_keys_empty_strings_unchanged(self):
        result = format_tool_use_preview("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""})
        assert result == "Edit: a.py"

    def test_edit_use_one_key_present_keeps_line_counts(self):
        result = format_tool_use_preview("Edit", {"file_path": "a.py", "new_string": "x\ny"})
        assert result == "Edit: a.py (0 lines → 2 lines)"

    def test_edit_use_both_keys_present_unchanged(self):
        result = format_tool_use_preview(
            "Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y\nz"}
        )
        assert result == "Edit: a.py (1 lines → 2 lines)"

    def test_combined_write_key_missing_shows_path_only(self):
        header = format_tool_chip_header("Write", {"file_path": "src/new.py"}, "ok", is_error=False)
        assert header == "Write ✓ src/new.py"
        assert "0 lines" not in header

    def test_combined_write_key_empty_string_unchanged(self):
        header = format_tool_chip_header(
            "Write", {"file_path": "src/new.py", "content": ""}, "ok", is_error=False
        )
        assert header == "Write ✓ src/new.py (0 lines)"

    def test_combined_write_missing_and_empty_differ(self):
        missing = format_tool_chip_header("Write", {"file_path": "a.py"}, "ok", is_error=False)
        empty = format_tool_chip_header(
            "Write", {"file_path": "a.py", "content": ""}, "ok", is_error=False
        )
        assert missing != empty

    def test_combined_edit_keys_missing_shows_path_only(self):
        header = format_tool_chip_header("Edit", {"file_path": "src/mod.py"}, "ok", is_error=False)
        assert header == "Edit ✓ src/mod.py"
        assert "0 lines" not in header

    def test_combined_edit_keys_empty_strings_unchanged(self):
        header = format_tool_chip_header(
            "Edit",
            {"file_path": "src/mod.py", "old_string": "", "new_string": ""},
            "ok",
            is_error=False,
        )
        assert header == "Edit ✓ src/mod.py (0 lines → 0 lines)"

    def test_combined_edit_missing_and_empty_differ(self):
        missing = format_tool_chip_header("Edit", {"file_path": "a.py"}, "ok", is_error=False)
        empty = format_tool_chip_header(
            "Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "ok", is_error=False
        )
        assert missing != empty

    def test_combined_edit_with_strings_unchanged(self):
        header = format_tool_chip_header(
            "Edit",
            {"file_path": "a.py", "old_string": "x", "new_string": "y\nz"},
            "ok",
            is_error=False,
        )
        assert header == "Edit ✓ a.py (1 lines → 2 lines)"


# ---------------------------------------------------------------------------
# Missing content keys — detail payload and CLI diff
#
# The old-content snapshot is taken when the tool_use arrives, i.e. AFTER an
# upstream like codex already wrote the file. Diffing that snapshot against an
# absent "content" would render the freshly written file as fully deleted, so
# both the detail payload and the CLI diff must report "no content info".
# ---------------------------------------------------------------------------

class TestMissingContentKeyDetail:
    def test_write_detail_key_missing_is_path_only(self):
        payload = build_tool_detail_payload(
            "Write",
            {"file_path": "src/b.py"},
            "ok",
            old_content="line1\nline2\nline3",
        )
        assert payload["kind"] == "file_path_only"
        assert payload["file_path"] == "src/b.py"
        assert "diff" not in payload
        assert "content" not in payload

    def test_write_detail_key_missing_no_old_content_is_path_only(self):
        payload = build_tool_detail_payload("Write", {"file_path": "src/b.py"}, "ok")
        assert payload["kind"] == "file_path_only"

    def test_write_detail_key_empty_string_unchanged(self):
        payload = build_tool_detail_payload(
            "Write", {"file_path": "src/b.py", "content": ""}, "ok"
        )
        assert payload["kind"] == "write_full"
        assert payload["content"] == ""

    def test_write_detail_with_content_still_diffs(self):
        payload = build_tool_detail_payload(
            "Write",
            {"file_path": "src/b.py", "content": "line1\nline2"},
            "ok",
            old_content="line1",
        )
        assert payload["kind"] == "write_diff"
        assert "+line2" in payload["diff"]

    def test_edit_detail_keys_missing_is_path_only(self):
        payload = build_tool_detail_payload("Edit", {"file_path": "src/b.py"}, "ok")
        assert payload["kind"] == "file_path_only"
        assert payload["file_path"] == "src/b.py"
        assert "diff" not in payload

    def test_edit_detail_keys_empty_strings_unchanged(self):
        payload = build_tool_detail_payload(
            "Edit", {"file_path": "src/b.py", "old_string": "", "new_string": ""}, "ok"
        )
        assert payload["kind"] == "edit_diff"

    def test_edit_detail_one_key_present_still_diffs(self):
        payload = build_tool_detail_payload(
            "Edit", {"file_path": "src/b.py", "new_string": "x"}, "ok"
        )
        assert payload["kind"] == "edit_diff"
        assert "+x" in payload["diff"]

    def test_path_only_payload_is_json_safe(self):
        payload = build_tool_detail_payload("Write", {"file_path": "src/b.py"}, "ok")
        assert json.loads(json.dumps(payload))["kind"] == "file_path_only"


class TestMissingContentKeyDiffRendering:
    def _capture_diff(self, tool_name, input_data, result_data, old_content=None):
        from tianluo.engine.display import set_console
        buf = io.StringIO()
        set_console(Console(file=buf, force_terminal=True, width=120))
        try:
            format_tool_diff(tool_name, input_data, result_data, old_content=old_content)
        finally:
            set_console(None)
        return buf.getvalue()

    def test_write_key_missing_renders_nothing(self):
        # codex file_change: the file already exists on disk with 3 lines.
        output = self._capture_diff(
            "Write", {"file_path": "src/b.py"}, "ok", old_content="line1\nline2\nline3"
        )
        assert output == ""

    def test_write_key_missing_no_old_content_has_no_line_count(self):
        output = self._capture_diff("Write", {"file_path": "src/b.py"}, "ok")
        assert output == ""
        assert "0 lines" not in output

    def test_write_key_empty_string_still_reports_created(self):
        output = self._capture_diff("Write", {"file_path": "src/b.py", "content": ""}, "ok")
        # Rich wraps the count in style codes, so match the pieces.
        assert "Created" in output
        assert "lines" in output

    def test_edit_keys_missing_renders_nothing(self):
        output = self._capture_diff("Edit", {"file_path": "src/b.py"}, "ok")
        assert output == ""


# ---------------------------------------------------------------------------
# Unregistered file tools (e.g. codex's "Delete") through the generic formatter
# ---------------------------------------------------------------------------

class TestDeleteViaGenericFormatter:
    def test_use_preview_contains_file_path(self):
        result = format_tool_use_preview("Delete", {"file_path": "src/gone.py"})
        assert "Delete" in result
        assert "src/gone.py" in result

    def test_in_flight_chip_contains_file_path(self):
        header = format_tool_chip_in_flight_header("Delete", {"file_path": "src/gone.py"})
        assert "Delete" in header
        assert "src/gone.py" in header

    def test_success_chip_contains_file_path(self):
        header = format_tool_chip_header("Delete", {"file_path": "src/gone.py"}, "", is_error=False)
        assert header.startswith("Delete ✓")
        assert "src/gone.py" in header

    def test_long_file_path_keeps_filename(self):
        long_path = "src/" + "deeply/" * 10 + "gone.py"
        result = format_tool_use_preview("Delete", {"file_path": long_path})
        # path-aware shortening never truncates the filename itself
        assert "gone.py" in result


# ---------------------------------------------------------------------------
# In-flight detail payload — the running chip's expandable input panel
# ---------------------------------------------------------------------------

class TestBuildToolInFlightDetailPayload:
    def test_kind_and_tool_name(self):
        payload = build_tool_in_flight_detail_payload("Agent", {"prompt": "p"})
        assert payload["kind"] == "tool_input"
        assert payload["tool_name"] == "Agent"

    def test_bash_command_is_present_in_full(self):
        command = "for f in *.py; do echo $f; done  # " + "c" * 500
        payload = build_tool_in_flight_detail_payload("Bash", {"command": command})
        assert payload["input"] == {"command": command}

    def test_registered_tool_also_gets_a_payload(self):
        """The in-flight panel is not limited to unregistered tools."""
        payload = build_tool_in_flight_detail_payload(
            "Read", {"file_path": "src/a.py", "offset": 0, "limit": 200}
        )
        assert payload["tool_name"] == "Read"
        assert payload["input"]["file_path"] == "src/a.py"
        assert payload["input"]["limit"] == 200

    def test_truncation_boundary(self):
        over = "x" * (TOOL_DETAIL_PAYLOAD_MAX_CHARS + 1)
        payload = build_tool_in_flight_detail_payload("Agent", {"prompt": over})
        assert payload["truncated"] is True
        assert len(payload["input"]["prompt"]) == TOOL_DETAIL_PAYLOAD_MAX_CHARS

    def test_json_round_trip(self):
        payload = build_tool_in_flight_detail_payload(
            "mcp__srv__tool", {"args": {"a": [1, 2]}, "n": 3, "flag": False}
        )
        assert json.loads(json.dumps(payload)) == payload


class TestGenericDetailPayloadCarriesInput:
    def test_unregistered_settled_payload_has_input_and_text(self):
        detail = build_tool_detail_payload(
            "Agent",
            {"description": "self check", "prompt": "look at the diff"},
            "No findings reported.",
        )
        assert detail["kind"] == "text"
        assert detail["text"] == "No findings reported."
        assert detail["input"]["description"] == "self check"

    def test_bash_payload_keeps_its_own_shape(self):
        detail = build_tool_detail_payload("Bash", {"command": "ls"}, "a\nb")
        assert detail == {
            "kind": "bash_output",
            "command": "ls",
            "stdout": "a\nb",
            "stderr": "",
            "truncated": False,
        }
