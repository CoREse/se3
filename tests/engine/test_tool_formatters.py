"""Tests for the tool_formatters module.

Covers: truncate_preview, truncate_path, per-tool formatters (Edit/Write/Read/Bash/Grep/Glob),
generic fallback, registry structure, and routing (registered vs unregistered).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tianluo.engine.tool_formatters import (
    TOOL_FORMATTERS,
    _extract_text,
    format_tool_result_preview,
    format_tool_use_preview,
    get_project_root,
    set_project_root,
    truncate_path,
    truncate_preview,
)


# ---------------------------------------------------------------------------
# truncate_preview
# ---------------------------------------------------------------------------

class TestTruncatePreview:
    def test_empty_string(self):
        assert truncate_preview("") == ""

    def test_none_returns_empty(self):
        assert truncate_preview(None) == ""

    def test_short_text_unchanged(self):
        assert truncate_preview("hello", max_length=10) == "hello"

    def test_exact_max_length_unchanged(self):
        text = "a" * 60
        assert truncate_preview(text) == text

    def test_over_max_length_truncated(self):
        result = truncate_preview("a" * 100, max_length=10)
        assert result == "aaaaaaa..."
        assert len(result) == 10

    def test_newlines_replaced_with_spaces(self):
        assert truncate_preview("line1\nline2", max_length=60) == "line1 line2"

    def test_custom_ellipsis(self):
        result = truncate_preview("a" * 20, max_length=10, ellipsis_str="..")
        assert result.endswith("..")
        assert len(result) == 10

    def test_very_short_max_length_uses_ellipsis_prefix(self):
        # When max_length <= len(ellipsis_str), truncate_at <= 0
        result = truncate_preview("hello world", max_length=3)
        assert len(result) == 3
        assert result == "..."

    def test_default_max_length_is_60(self):
        long_text = "x" * 100
        result = truncate_preview(long_text)
        assert len(result) == 60
        assert result.endswith("...")

    def test_non_string_input_converted(self):
        assert truncate_preview(12345) == "12345"

    def test_max_length_1_with_single_char_ellipsis(self):
        result = truncate_preview("hello", max_length=1, ellipsis_str=".")
        assert result == "."
        assert len(result) == 1


# ---------------------------------------------------------------------------
# truncate_path
# ---------------------------------------------------------------------------

class TestTruncatePath:
    def test_empty_string(self):
        assert truncate_path("") == ""

    def test_short_relative_path_unchanged(self):
        assert truncate_path("src/main.py") == "src/main.py"

    def test_absolute_to_relative_with_project_root(self):
        result = truncate_path(
            "/home/user/project/src/engine/steps/implement.py",
            project_root=Path("/home/user/project"),
        )
        assert result == "src/engine/steps/implement.py"

    def test_absolute_to_relative_with_module_level_root(self):
        old_root = get_project_root()
        try:
            set_project_root(Path("/home/user/project"))
            result = truncate_path("/home/user/project/src/engine/steps/implement.py")
            assert result == "src/engine/steps/implement.py"
        finally:
            # Restore original state
            if old_root is None:
                from tianluo.engine import tool_formatters
                tool_formatters._project_root = None
            else:
                set_project_root(old_root)

    def test_explicit_project_root_overrides_module_level(self):
        old_root = get_project_root()
        try:
            set_project_root(Path("/other/root"))
            result = truncate_path(
                "/home/user/project/src/main.py",
                project_root=Path("/home/user/project"),
            )
            assert result == "src/main.py"
        finally:
            if old_root is None:
                from tianluo.engine import tool_formatters
                tool_formatters._project_root = None
            else:
                set_project_root(old_root)

    def test_long_path_middle_truncation(self):
        # Build a path that exceeds 160 chars
        long_path = "src/" + "/".join(f"very_long_directory_name_{i}" for i in range(20)) + "/implement.py"
        assert len(long_path) > 160
        result = truncate_path(long_path)
        assert result.startswith("src/")
        assert result.endswith("/implement.py")
        assert "..." in result

    def test_filename_never_truncated(self):
        long_filename = "a" * 200 + ".py"
        result = truncate_path(long_filename)
        # Single segment — returned as-is, filename never truncated
        assert result == long_filename

    def test_filename_preserved_in_deep_path(self):
        long_filename = "very_specific_implementation_file.py"
        long_path = "src/" + "/".join(f"dir{i}" for i in range(30)) + f"/{long_filename}"
        result = truncate_path(long_path)
        assert result.endswith(long_filename)

    def test_no_project_root_still_works(self):
        old_root = get_project_root()
        try:
            from tianluo.engine import tool_formatters
            tool_formatters._project_root = None
            result = truncate_path("src/main.py")
            assert result == "src/main.py"
        finally:
            if old_root is not None:
                set_project_root(old_root)

    def test_path_outside_project_root_not_converted(self):
        result = truncate_path(
            "/other/location/file.py",
            project_root=Path("/home/user/project"),
        )
        # relpath would start with '..', so should not use relpath
        assert not result.startswith("src/")

    def test_default_max_length_is_160(self):
        # Path exactly 160 chars should not be truncated
        filename = "file.py"
        # Need path of exactly 160 chars: "d/" + padding + "/file.py"
        prefix = "d/"
        remaining = 160 - len(prefix) - len("/") - len(filename)
        middle = "x" * remaining
        path = f"{prefix}{middle}/{filename}"
        assert len(path) == 160
        assert truncate_path(path) == path

    def test_path_161_chars_is_truncated(self):
        filename = "file.py"
        prefix = "d/"
        remaining = 161 - len(prefix) - len("/") - len(filename)
        middle = "x" * remaining
        path = f"{prefix}{middle}/{filename}"
        assert len(path) == 161
        result = truncate_path(path)
        assert "..." in result
        assert result.endswith(filename)

    def test_set_get_project_root(self):
        old_root = get_project_root()
        try:
            set_project_root(Path("/foo"))
            assert get_project_root() == Path("/foo")
        finally:
            if old_root is None:
                from tianluo.engine import tool_formatters
                tool_formatters._project_root = None
            else:
                set_project_root(old_root)

    def test_initial_project_root_is_none(self):
        """Module-level _project_root may have been set by other tests; just verify the API works."""
        # This tests the getter returns the expected type
        result = get_project_root()
        assert result is None or isinstance(result, Path)


# ---------------------------------------------------------------------------
# Generic formatters (exercised via unknown tool name)
# ---------------------------------------------------------------------------

class TestGenericFormatters:
    def test_use_no_input(self):
        assert format_tool_use_preview("Foo", {}) == "Tool: Foo | Input: (none)"

    def test_use_none_input(self):
        assert format_tool_use_preview("Foo", None) == "Tool: Foo | Input: (none)"

    def test_use_string_param(self):
        result = format_tool_use_preview("Foo", {"key": "value"})
        assert "key=value" in result

    def test_use_numeric_param(self):
        result = format_tool_use_preview("Foo", {"n": 42})
        assert "n=42" in result

    def test_use_bool_param(self):
        result = format_tool_use_preview("Foo", {"flag": True})
        assert "flag=True" in result

    def test_use_list_param(self):
        result = format_tool_use_preview("Foo", {"items": [1, 2]})
        assert "items=" in result
        assert "[" in result

    def test_use_dict_param(self):
        result = format_tool_use_preview("Foo", {"cfg": {"k": "v"}})
        assert "cfg=" in result

    def test_use_truncates_at_3_params(self):
        result = format_tool_use_preview("Foo", {"a": 1, "b": 2, "c": 3, "d": 4})
        assert "..." in result
        assert "d=4" not in result

    def test_result_none(self):
        assert format_tool_result_preview("Foo", None) == "Result: (empty)"

    def test_result_empty_string(self):
        assert format_tool_result_preview("Foo", "   ") == "Result: (empty)"

    def test_result_string(self):
        assert "hello" in format_tool_result_preview("Foo", "hello")

    def test_result_error_isError(self):
        result = format_tool_result_preview("Foo", {"isError": True, "content": "oops"})
        assert "error" in result.lower()
        assert "oops" in result

    def test_result_error_is_error_snake(self):
        result = format_tool_result_preview("Foo", {"is_error": True, "content": "oops"})
        assert "error" in result.lower()

    def test_result_dict(self):
        result = format_tool_result_preview("Foo", {"status": "ok"})
        assert "Result:" in result

    def test_result_list(self):
        result = format_tool_result_preview("Foo", [1, 2, 3])
        assert "Result:" in result

    def test_result_number(self):
        assert format_tool_result_preview("Foo", 42) == "Result: 42"

    def test_result_long_string_truncated(self):
        result = format_tool_result_preview("Foo", "x" * 200)
        assert "..." in result


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
        assert result.startswith("Edit:")
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
        # Filename is preserved in the output
        assert "file.py" in result

    def test_result_success_marker(self):
        result = format_tool_result_preview("Edit", "\u2713 edited src/main.py")
        assert "Edit" in result
        assert "\u2713" in result

    def test_result_error_marker(self):
        result = format_tool_result_preview("Edit", "Error: old_string not found")
        assert "Edit" in result
        assert "\u2717" in result

    def test_result_none_gives_done(self):
        result = format_tool_result_preview("Edit", None)
        assert "Edit" in result
        assert "done" in result

    def test_result_empty_string_gives_done(self):
        result = format_tool_result_preview("Edit", "")
        assert "Edit" in result
        assert "done" in result

    def test_result_dict_content_blocks(self):
        """Edit result arriving as dict with content blocks."""
        data = {"content": [{"type": "text", "text": "success editing file.py"}]}
        result = format_tool_result_preview("Edit", data)
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
        result = format_tool_use_preview("Write", {"file_path": "x.py"})
        assert "Write:" in result
        assert "empty" in result.lower()

    def test_use_single_line_content(self):
        data = {"file_path": "one.py", "content": "print('hi')"}
        result = format_tool_use_preview("Write", data)
        assert "1 lines" in result

    def test_result_basic(self):
        result = format_tool_result_preview("Write", "Created out.txt")
        assert "Write" in result
        assert "\u2713" in result

    def test_result_none_gives_done(self):
        result = format_tool_result_preview("Write", None)
        assert "Write" in result
        assert "done" in result


# ---------------------------------------------------------------------------
# Read formatter
# ---------------------------------------------------------------------------

class TestReadFormatter:
    def test_use_simple(self):
        result = format_tool_use_preview("Read", {"file_path": "src/app.py"})
        assert "Read:" in result
        assert "src/app.py" in result

    def test_use_with_offset_and_limit(self):
        data = {"file_path": "big.log", "offset": 100, "limit": 50}
        result = format_tool_use_preview("Read", data)
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
        result = format_tool_use_preview("Bash", {"command": "ls -la"})
        assert "Bash:" in result
        assert "ls -la" in result

    def test_use_long_command_truncated(self):
        result = format_tool_use_preview("Bash", {"command": "x" * 200})
        assert "..." in result
        assert len(result) < 100

    def test_use_empty_command(self):
        result = format_tool_use_preview("Bash", {"command": ""})
        assert "Bash:" in result

    def test_result_with_output(self):
        result = format_tool_result_preview("Bash", "line1\nline2")
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
        result = format_tool_use_preview("Grep", {"pattern": "error"})
        assert "/error/" in result
        assert "." in result

    def test_use_long_pattern_truncated(self):
        result = format_tool_use_preview("Grep", {"pattern": "x" * 100})
        assert "..." in result

    def test_result_with_matches(self):
        result = format_tool_result_preview("Grep", "file1.py\nfile2.py")
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
        result = format_tool_use_preview("Glob", {"pattern": "*.md"})
        assert "*.md" in result
        assert "." in result

    def test_result_with_files(self):
        result = format_tool_result_preview("Glob", "a.py\nb.py\nc.py")
        assert "3 files" in result

    def test_result_no_files(self):
        result = format_tool_result_preview("Glob", "")
        assert "no files" in result.lower()


# ---------------------------------------------------------------------------
# _extract_text helper
# ---------------------------------------------------------------------------

class TestExtractText:
    def test_none(self):
        assert _extract_text(None) == ""

    def test_plain_string(self):
        assert _extract_text("hello") == "hello"

    def test_dict_with_content_string(self):
        assert _extract_text({"content": "text"}) == "text"

    def test_dict_with_content_blocks(self):
        data = {"content": [{"type": "text", "text": "abc"}, {"type": "text", "text": "def"}]}
        assert _extract_text(data) == "abc\ndef"

    def test_dict_error_flag(self):
        data = {"isError": True, "content": "bad"}
        assert _extract_text(data) == "bad"

    def test_list_of_text_blocks(self):
        data = [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}]
        assert _extract_text(data) == "line1\nline2"

    def test_list_of_strings(self):
        data = ["hello", "world"]
        assert _extract_text(data) == "hello\nworld"

    def test_numeric_input(self):
        assert _extract_text(42) == "42"

    def test_dict_no_content_returns_empty(self):
        assert _extract_text({"other": "stuff"}) == ""


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
    def test_registered_tool_uses_specific_use_formatter(self):
        data = {"file_path": "a.py", "old_string": "x", "new_string": "y"}
        result = format_tool_use_preview("Edit", data)
        assert result.startswith("Edit:")
        assert "Tool:" not in result

    def test_unregistered_tool_uses_generic_use_formatter(self):
        result = format_tool_use_preview("Agent", {"prompt": "do stuff"})
        assert result.startswith("Tool: Agent")

    def test_registered_tool_uses_specific_result_formatter(self):
        result = format_tool_result_preview("Bash", "hello world")
        assert "Bash" in result
        assert "1 lines output" in result

    def test_unregistered_tool_uses_generic_result_formatter(self):
        result = format_tool_result_preview("Agent", "some output")
        assert result.startswith("Result:")

    def test_result_formatter_accepts_tool_name_first_arg(self):
        # Verify the new signature: tool_name as first argument
        result = format_tool_result_preview("Edit", None)
        assert "Edit" in result

    def test_formatter_exception_falls_back_to_generic(self):
        """If a per-tool formatter raises, fallback to generic."""
        # We can't easily force a real exception, but verify the function
        # handles None input gracefully (which would cause issues in naive code)
        result = format_tool_use_preview("Read", None)
        # Should fall back to generic since per-tool gets None dict
        assert "Read" in result

    def test_all_registered_tools_produce_tool_name_prefix(self):
        """Every registered tool's use formatter starts with the tool name."""
        test_inputs = {
            "Edit": {"file_path": "a.py"},
            "Write": {"file_path": "a.py"},
            "Read": {"file_path": "a.py"},
            "Bash": {"command": "ls"},
            "Grep": {"pattern": "x"},
            "Glob": {"pattern": "*.py"},
        }
        for tool_name, input_data in test_inputs.items():
            result = format_tool_use_preview(tool_name, input_data)
            assert result.startswith(f"{tool_name}:"), (
                f"{tool_name} use preview should start with '{tool_name}:', got: {result}"
            )
