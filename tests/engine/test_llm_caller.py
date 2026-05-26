"""Tests for the LLM caller module, specifically tool preview formatters."""

import json
import re
import pytest

from se3.engine.llm_caller import (
    truncate_preview,
    format_tool_use_preview,
    format_tool_result_preview,
    StreamJSONTracker,
    LLMCaller,
)


class TestTruncatePreview:
    """Tests for the truncate_preview utility function."""

    def test_short_text_not_truncated(self):
        """Short text should be returned as-is."""
        text = "Short text"
        result = truncate_preview(text)
        assert result == "Short text"

    def test_exact_max_length_not_truncated(self):
        """Text exactly at max_length should not be truncated."""
        text = "x" * 60
        result = truncate_preview(text)
        assert result == text
        assert len(result) == 60

    def test_long_text_truncated(self):
        """Long text should be truncated with ellipsis."""
        text = "x" * 100
        result = truncate_preview(text)
        assert result == "x" * 57 + "..."
        assert len(result) == 60

    def test_newlines_replaced_with_spaces(self):
        """Newlines should be replaced with spaces."""
        text = "Line one\nLine two\nLine three"
        result = truncate_preview(text)
        assert "\n" not in result
        assert result == "Line one Line two Line three"

    def test_empty_string(self):
        """Empty string should return empty."""
        assert truncate_preview("") == ""

    def test_none_input(self):
        """None input should return empty string."""
        assert truncate_preview(None) == ""

    def test_custom_max_length(self):
        """Custom max_length should be respected."""
        text = "x" * 50
        result = truncate_preview(text, max_length=30)
        assert result == "x" * 27 + "..."
        assert len(result) == 30

    def test_custom_ellipsis(self):
        """Custom ellipsis string should be used."""
        text = "x" * 100
        result = truncate_preview(text, max_length=60, ellipsis_str=" [...]")
        assert result == "x" * 54 + " [...]"
        assert len(result) == 60

    def test_non_string_input(self):
        """Non-string input should be converted to string."""
        result = truncate_preview(12345)
        assert result == "12345"

    def test_number_input(self):
        """Number input should be converted to string."""
        result = truncate_preview(12345678901234567890, max_length=10)
        assert result == "1234567..."
        assert len(result) == 10


class TestFormatToolUsePreview:
    """Tests for the format_tool_use_preview function."""

    def test_known_tool_uses_per_tool_formatter(self):
        """Known tools (Read, Write, etc.) use per-tool formatters."""
        result = format_tool_use_preview("Read", {"file_path": "foo.py"})
        assert result.startswith("Read:")

    def test_known_tool_write(self):
        """Write tool shows file_path and line count."""
        result = format_tool_use_preview(
            "Write",
            {"file_path": "test.py", "content": "print('hello')"}
        )
        assert result.startswith("Write:")
        assert "test.py" in result

    def test_empty_input(self):
        """Tool call with empty input falls back to generic."""
        result = format_tool_use_preview("Read", {})
        # Per-tool formatter for Read with empty dict shows file_path=?
        assert "Read:" in result

    def test_none_input(self):
        """Tool call with None input falls back to generic."""
        result = format_tool_use_preview("Read", None)
        # Generic formatter used for None input
        assert "Read" in result

    def test_unknown_tool_generic_format(self):
        """Unknown tools use generic formatter."""
        result = format_tool_use_preview("Search", {"limit": 10, "offset": 5.5})
        assert "Tool: Search" in result
        assert "limit=10" in result
        assert "offset=5.5" in result

    def test_handles_boolean_values(self):
        """Boolean values should be formatted as True/False."""
        result = format_tool_use_preview("List", {"recursive": True, "hidden": False})
        assert "recursive=True" in result
        assert "hidden=False" in result

    def test_handles_list_values(self):
        """List values should be JSON-formatted and truncated if needed."""
        result = format_tool_use_preview("Batch", {"files": ["a.py", "b.py", "c.py"]})
        assert "files=" in result
        assert "[" in result
        assert "]" in result

    def test_handles_dict_values(self):
        """Dict values should be JSON-formatted and truncated if needed."""
        result = format_tool_use_preview("Config", {"settings": {"key": "value"}})
        assert "settings=" in result
        assert "{" in result
        assert "}" in result

    def test_limits_to_three_params(self):
        """Only first 3 parameters should be shown with ellipsis (generic formatter)."""
        result = format_tool_use_preview(
            "Complex",
            {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}
        )
        assert "a=1" in result
        assert "b=2" in result
        assert "c=3" in result
        assert "..." in result
        assert "d=4" not in result
        assert "e=5" not in result

    def test_nested_structure_truncation(self):
        """Nested structures should be truncated in preview."""
        nested = {"data": ["x" * 50, "y" * 50]}
        result = format_tool_use_preview("Process", nested)
        # The dict should be JSON-formatted and truncated
        assert "data=" in result
        assert "..." in result


class TestFormatToolResultPreview:
    """Tests for the format_tool_result_preview function."""

    def test_string_result_generic(self):
        """Simple string result with unknown tool (generic formatter)."""
        result = format_tool_result_preview("", "File contents here")
        assert result == "Result: File contents here"

    def test_empty_string_result(self):
        """Empty string result."""
        result = format_tool_result_preview("", "")
        assert result == "Result: (empty)"

    def test_whitespace_only_result(self):
        """Whitespace-only string result."""
        result = format_tool_result_preview("", "   \n\t  ")
        assert result == "Result: (empty)"

    def test_none_result(self):
        """None result."""
        result = format_tool_result_preview("", None)
        assert result == "Result: (empty)"

    def test_long_string_truncated(self):
        """Long string result should be truncated."""
        long_content = "x" * 100
        result = format_tool_result_preview("", long_content)
        assert "..." in result

    def test_dict_result(self):
        """Dict result should be JSON-formatted."""
        data = {"status": "ok", "count": 42}
        result = format_tool_result_preview("", data)
        assert "Result:" in result
        assert "status" in result

    def test_dict_with_error_flag(self):
        """Dict with isError flag should show error formatting."""
        data = {"isError": True, "content": "Something went wrong"}
        result = format_tool_result_preview("", data)
        assert "error" in result.lower()
        assert "Something went wrong" in result

    def test_dict_with_is_error_snake_case(self):
        """Dict with is_error flag (snake_case) should show error formatting."""
        data = {"is_error": True, "content": "An error occurred"}
        result = format_tool_result_preview("", data)
        assert "error" in result.lower()
        assert "An error occurred" in result

    def test_list_result(self):
        """List result should be JSON-formatted."""
        data = ["item1", "item2", "item3"]
        result = format_tool_result_preview("", data)
        assert "Result:" in result
        assert "item1" in result

    def test_number_result(self):
        """Number result should be converted to string."""
        result = format_tool_result_preview("", 42)
        assert result == "Result: 42"

    def test_boolean_result(self):
        """Boolean result should be converted to string."""
        result = format_tool_result_preview("", True)
        assert result == "Result: True"

    def test_known_tool_result(self):
        """Known tool (Read) uses per-tool formatter."""
        result = format_tool_result_preview("Read", "line1\nline2\nline3")
        assert "Read" in result
        assert "3 lines" in result

    def test_nested_dict_truncated(self):
        """Nested dict with long content should be truncated."""
        data = {"data": "x" * 100}
        result = format_tool_result_preview("", data)
        assert "..." in result


class TestStreamJSONTracker:
    """Tests for the StreamJSONTracker class."""

    def test_initialization(self):
        """Tracker should initialize with correct defaults."""
        tracker = StreamJSONTracker()
        assert tracker.message_count == 0
        assert tracker.tool_calls == []
        assert tracker.tool_results == []
        assert tracker.text_chunks == 0
        assert tracker.total_text_len == 0

    def test_process_empty_line(self):
        """Empty lines should be ignored."""
        tracker = StreamJSONTracker()
        tracker.process_line("")
        tracker.process_line("   ")
        assert tracker.message_count == 0

    def test_process_invalid_json(self):
        """Invalid JSON lines should be ignored."""
        tracker = StreamJSONTracker()
        tracker.process_line("not valid json")
        assert tracker.message_count == 0

    def test_process_assistant_text(self, capsys):
        """Assistant text messages should be tracked."""
        tracker = StreamJSONTracker()
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Hello world"}]
            }
        })
        tracker.process_line(line)
        assert tracker.message_count == 1
        assert tracker.text_chunks == 1
        assert tracker.total_text_len == 11

    def test_process_tool_use(self, capsys):
        """Tool use messages should be tracked and formatted with per-tool preview."""
        tracker = StreamJSONTracker()
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-read-1",
                    "name": "Read",
                    "input": {"file_path": "test.py"}
                }]
            }
        })
        tracker.process_line(line)
        assert tracker.message_count == 1
        assert len(tracker.tool_calls) == 1
        assert tracker.tool_calls[0] == "Read"
        captured = capsys.readouterr()
        # Per-tool formatter: "Read: test.py" not "Tool: Read | Input: ..."
        assert "Read:" in captured.out
        assert "test.py" in captured.out

    def test_process_tool_result_success(self, capsys):
        """Successful tool results should be tracked and use per-tool formatting."""
        tracker = StreamJSONTracker()
        # First, emit a tool_use so the id→name mapping is populated
        tracker.process_line(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tool-123",
                    "name": "Read",
                    "input": {"file_path": "test.py"}
                }]
            }
        }))
        # Now emit the result in CLI actual format (type='user' wrapper)
        tracker.process_line(json.dumps({
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool-123",
                    "content": "line1\nline2\nline3",
                    "is_error": False
                }]
            }
        }))
        assert len(tracker.tool_results) == 1
        assert tracker.tool_results[0] == "tool-123"
        captured = capsys.readouterr()
        # Per-tool result: "Read ✓ (3 lines)" instead of generic
        assert "Read" in captured.out
        assert "3 lines" in captured.out

    def test_process_tool_result_error(self, capsys):
        """Error tool results should be tracked and show error preview."""
        tracker = StreamJSONTracker()
        line = json.dumps({
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool-456",
                    "content": "Error message",
                    "is_error": True
                }]
            }
        })
        tracker.process_line(line)
        assert len(tracker.tool_results) == 1
        captured = capsys.readouterr()
        assert "Error message" in captured.out

    def test_process_error_message(self, capsys):
        """Error messages should be tracked."""
        tracker = StreamJSONTracker()
        line = json.dumps({
            "type": "error",
            "error": "Something went wrong"
        })
        tracker.process_line(line)
        # Error messages don't increment message_count but should be processed
        captured = capsys.readouterr()
        assert "error" in captured.out.lower() or "Error" in captured.out

    def test_print_summary(self, capsys):
        """Summary should be printed correctly."""
        tracker = StreamJSONTracker()
        # Add some activity
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Hello"}]
            }
        })
        tracker.process_line(line)

        tracker.print_summary()
        captured = capsys.readouterr()
        assert "Stream complete" in captured.out
        assert "1 messages" in captured.out
        assert "0 tool calls" in captured.out

    def test_multiple_text_chunks_batching(self, capsys):
        """Only certain text chunks should be printed."""
        tracker = StreamJSONTracker()

        # First 3 chunks should be printed
        for i in range(5):
            line = json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": f"Chunk {i}"}]
                }
            })
            tracker.process_line(line)

        # Chunks 1-3 should be printed, 4-5 should not
        captured = capsys.readouterr()
        assert "Chunk 0" in captured.out
        assert "Chunk 1" in captured.out
        assert "Chunk 2" in captured.out

    def test_tool_use_with_complex_input(self, capsys):
        """Tool use with complex nested input should be formatted."""
        tracker = StreamJSONTracker()
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": "Search",
                    "input": {
                        "pattern": "class.*Test",
                        "path": "/project",
                        "options": {"recursive": True, "max_depth": 5}
                    }
                }]
            }
        })
        tracker.process_line(line)
        captured = capsys.readouterr()
        assert "Tool: Search" in captured.out

    def test_edit_tool_use_shows_file_and_diff_info(self, capsys):
        """Edit tool_use should show file path and line change counts."""
        tracker = StreamJSONTracker()
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-edit-1",
                    "name": "Edit",
                    "input": {
                        "file_path": "src/main.py",
                        "old_string": "x = 1\ny = 2",
                        "new_string": "x = 10\ny = 20\nz = 30",
                    }
                }]
            }
        })
        tracker.process_line(line)
        captured = capsys.readouterr()
        assert "Edit:" in captured.out
        assert "src/main.py" in captured.out
        assert "2 lines" in captured.out
        assert "3 lines" in captured.out

    def test_edit_tool_result_shows_per_tool_format(self, capsys):
        """Edit tool_result should use per-tool formatter after id→name resolution."""
        tracker = StreamJSONTracker()
        # Emit tool_use to establish id→name mapping
        tracker.process_line(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-edit-2",
                    "name": "Edit",
                    "input": {"file_path": "a.py", "old_string": "a", "new_string": "b"}
                }]
            }
        }))
        # Emit result in CLI actual format
        tracker.process_line(json.dumps({
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu-edit-2",
                    "content": "✓ edited a.py",
                    "is_error": False
                }]
            }
        }))
        captured = capsys.readouterr()
        assert "Edit" in captured.out
        assert "\u2713" in captured.out

    def test_write_tool_use_shows_file_and_line_count(self, capsys):
        """Write tool_use should show file path and line count."""
        tracker = StreamJSONTracker()
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-write-1",
                    "name": "Write",
                    "input": {
                        "file_path": "output.py",
                        "content": "line1\nline2\nline3\nline4",
                    }
                }]
            }
        })
        tracker.process_line(line)
        captured = capsys.readouterr()
        assert "Write:" in captured.out
        assert "output.py" in captured.out
        assert "4 lines" in captured.out

    def test_write_tool_result_shows_per_tool_format(self, capsys):
        """Write tool_result should use per-tool formatter."""
        tracker = StreamJSONTracker()
        # Emit tool_use first
        tracker.process_line(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-write-2",
                    "name": "Write",
                    "input": {"file_path": "out.py", "content": "x"}
                }]
            }
        }))
        # Emit result in CLI actual format
        tracker.process_line(json.dumps({
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu-write-2",
                    "content": "Created out.py",
                    "is_error": False
                }]
            }
        }))
        captured = capsys.readouterr()
        assert "Write" in captured.out
        assert "\u2713" in captured.out

    def test_tool_result_without_prior_use_falls_back(self, capsys):
        """Tool result without prior tool_use should use generic formatter."""
        tracker = StreamJSONTracker()
        tracker.process_line(json.dumps({
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "orphan-id",
                    "content": "some result text",
                    "is_error": False
                }]
            }
        }))
        captured = capsys.readouterr()
        # Without id→name mapping, falls back to generic "Result: ..."
        assert "Result:" in captured.out

    def test_legacy_tool_result_format_backward_compat(self, capsys):
        """Legacy top-level tool_result format should still work (backward compat)."""
        tracker = StreamJSONTracker()
        # Emit tool_use first
        tracker.process_line(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "legacy-123",
                    "name": "Read",
                    "input": {"file_path": "test.py"}
                }]
            }
        }))
        # Emit result in legacy format (top-level tool_result with camelCase)
        tracker.process_line(json.dumps({
            "type": "tool_result",
            "result": {
                "toolUseId": "legacy-123",
                "content": "line1\nline2",
                "isError": False
            }
        }))
        assert len(tracker.tool_results) == 1
        captured = capsys.readouterr()
        assert "Read" in captured.out
        assert "2 lines" in captured.out

    def test_write_tool_use_caches_old_content(self, capsys, tmp_path):
        """Write tool_use for existing file should cache old content."""
        target = tmp_path / "existing.py"
        target.write_text("old content\n", encoding="utf-8")
        tracker = StreamJSONTracker()
        tracker.process_line(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-w-old",
                    "name": "Write",
                    "input": {"file_path": str(target), "content": "new content\n"}
                }]
            }
        }))
        assert "tu-w-old" in tracker._tool_use_id_to_old_content
        assert tracker._tool_use_id_to_old_content["tu-w-old"] == "old content\n"

    def test_write_tool_use_nonexistent_file_caches_none(self, capsys):
        """Write tool_use for non-existent file should cache None."""
        tracker = StreamJSONTracker()
        tracker.process_line(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-w-new",
                    "name": "Write",
                    "input": {"file_path": "/no/such/file.py", "content": "x"}
                }]
            }
        }))
        assert "tu-w-new" in tracker._tool_use_id_to_old_content
        assert tracker._tool_use_id_to_old_content["tu-w-new"] is None

    def test_print_summary_clears_caches(self, capsys):
        """print_summary should clear id-to-name, input, and old_content caches."""
        tracker = StreamJSONTracker()
        tracker.process_line(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-cache",
                    "name": "Edit",
                    "input": {"file_path": "f.py", "old_string": "a", "new_string": "b"}
                }]
            }
        }))
        tracker._tool_use_id_to_old_content["tu-cache"] = "old"
        assert len(tracker._tool_use_id_to_name) > 0
        assert len(tracker._tool_use_id_to_input) > 0
        assert len(tracker._tool_use_id_to_old_content) > 0

        tracker.print_summary()
        assert len(tracker._tool_use_id_to_name) == 0
        assert len(tracker._tool_use_id_to_input) == 0
        assert len(tracker._tool_use_id_to_old_content) == 0

    def test_cache_eviction_on_overflow(self, capsys):
        """Cache should evict oldest entry when exceeding _MAX_CACHE_SIZE."""
        tracker = StreamJSONTracker()
        # Fill cache to the limit
        for i in range(StreamJSONTracker._MAX_CACHE_SIZE + 1):
            tracker.process_line(json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "id": f"tu-{i}",
                        "name": "Edit",
                        "input": {"file_path": f"f{i}.py", "old_string": "a", "new_string": "b"}
                    }]
                }
            }))
        # First entry should have been evicted
        assert "tu-0" not in tracker._tool_use_id_to_input
        # Last entry should still be present
        assert f"tu-{StreamJSONTracker._MAX_CACHE_SIZE}" in tracker._tool_use_id_to_input
        # Total cache size should not exceed limit
        assert len(tracker._tool_use_id_to_input) <= StreamJSONTracker._MAX_CACHE_SIZE

    def test_error_result_cleans_up_caches(self, capsys):
        """Error tool_result should clean up all cached entries for that tool_use_id."""
        tracker = StreamJSONTracker()
        # Emit tool_use to populate caches
        tracker.process_line(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-err-cleanup",
                    "name": "Edit",
                    "input": {"file_path": "f.py", "old_string": "a", "new_string": "b"}
                }]
            }
        }))
        # Verify caches are populated
        assert "tu-err-cleanup" in tracker._tool_use_id_to_name
        assert "tu-err-cleanup" in tracker._tool_use_id_to_input

        # Emit error result
        tracker.process_line(json.dumps({
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu-err-cleanup",
                    "content": "File not found",
                    "is_error": True
                }]
            }
        }))
        # All caches for this id should be cleaned up
        assert "tu-err-cleanup" not in tracker._tool_use_id_to_name
        assert "tu-err-cleanup" not in tracker._tool_use_id_to_input
        assert "tu-err-cleanup" not in tracker._tool_use_id_to_old_content

    def test_error_result_does_not_affect_other_entries(self, capsys):
        """Error cleanup should only remove the specific tool_use_id, not others."""
        tracker = StreamJSONTracker()
        # Emit two tool_uses
        for tid in ("tu-keep", "tu-fail"):
            tracker.process_line(json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "id": tid,
                        "name": "Edit",
                        "input": {"file_path": f"{tid}.py", "old_string": "a", "new_string": "b"}
                    }]
                }
            }))
        # Emit error only for tu-fail
        tracker.process_line(json.dumps({
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu-fail",
                    "content": "Error",
                    "is_error": True
                }]
            }
        }))
        # tu-fail should be cleaned up
        assert "tu-fail" not in tracker._tool_use_id_to_input
        # tu-keep should still be in cache
        assert "tu-keep" in tracker._tool_use_id_to_input
        assert "tu-keep" in tracker._tool_use_id_to_name


class TestStreamJSONTrackerPrefix:
    """Tests for StreamJSONTracker stream_prefix behavior."""

    def test_no_prefix_by_default(self, capsys):
        """Without stream_prefix, output should have no prefix (backward compat)."""
        tracker = StreamJSONTracker()
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-1",
                    "name": "Read",
                    "input": {"file_path": "test.py"}
                }]
            }
        })
        tracker.process_line(line)
        captured = capsys.readouterr()
        assert captured.out.strip().startswith("[llm-stream]")

    def test_prefix_on_tool_use(self, capsys):
        """With stream_prefix, tool_use lines should include the prefix."""
        tracker = StreamJSONTracker(stream_prefix='[G1] ')
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-1",
                    "name": "Read",
                    "input": {"file_path": "test.py"}
                }]
            }
        })
        tracker.process_line(line)
        captured = capsys.readouterr()
        assert "[G1] [llm-stream]" in captured.out

    def test_prefix_on_tool_error(self, capsys):
        """With stream_prefix, tool error lines should include the prefix."""
        tracker = StreamJSONTracker(stream_prefix='[G2] ')
        line = json.dumps({
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu-err",
                    "content": "Some error",
                    "is_error": True
                }]
            }
        })
        tracker.process_line(line)
        captured = capsys.readouterr()
        assert "[G2] [llm-stream]" in captured.out

    def test_prefix_on_tool_result(self, capsys):
        """With stream_prefix, tool result lines should include the prefix."""
        tracker = StreamJSONTracker(stream_prefix='[G3] ')
        # Emit tool_use first for id→name mapping
        tracker.process_line(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-r1",
                    "name": "Read",
                    "input": {"file_path": "a.py"}
                }]
            }
        }))
        # Emit result in CLI actual format
        tracker.process_line(json.dumps({
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu-r1",
                    "content": "line1\nline2",
                    "is_error": False
                }]
            }
        }))
        captured = capsys.readouterr()
        # Both tool_use and tool_result lines should have prefix
        assert captured.out.count("[G3] [llm-stream]") == 2

    def test_prefix_on_error(self, capsys):
        """With stream_prefix, error lines should include the prefix."""
        tracker = StreamJSONTracker(stream_prefix='[G1] ')
        line = json.dumps({
            "type": "error",
            "error": "Something went wrong"
        })
        tracker.process_line(line)
        captured = capsys.readouterr()
        assert "[G1] [llm-stream]" in captured.out

    def test_prefix_on_summary(self, capsys):
        """With stream_prefix, print_summary should include the prefix."""
        tracker = StreamJSONTracker(stream_prefix='[G2] ')
        tracker.print_summary()
        captured = capsys.readouterr()
        assert "[G2] [llm-stream]" in captured.out
        assert "Stream complete" in captured.out

    def test_merged_prefix(self, capsys):
        """Merged group prefix like [G1+G2+G3] should work correctly."""
        tracker = StreamJSONTracker(stream_prefix='[G1+G2+G3] ')
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-m1",
                    "name": "Edit",
                    "input": {"file_path": "x.py", "old_string": "a", "new_string": "b"}
                }]
            }
        })
        tracker.process_line(line)
        captured = capsys.readouterr()
        assert "[G1+G2+G3] [llm-stream]" in captured.out

    def test_empty_prefix_no_extra_space(self, capsys):
        """Empty stream_prefix should produce same output as no prefix."""
        tracker_no_prefix = StreamJSONTracker()
        tracker_empty = StreamJSONTracker(stream_prefix='')

        line = json.dumps({
            "type": "error",
            "error": "test error"
        })
        tracker_no_prefix.process_line(line)
        out_no_prefix = capsys.readouterr().out

        tracker_empty.process_line(line)
        out_empty = capsys.readouterr().out

        assert out_no_prefix == out_empty


class TestStreamJSONTrackerProgressBrackets:
    """Tool events recorded via _emit_progress MUST use the bracketed
    `[Name …]` marker format that the web UI's TOOL_MARKER_RE matches and
    `extract_assistant_text` produces for final-state history, while the
    CLI stdout emoji lines (🔧/✅/❌) remain byte-for-byte unchanged.

    The two surfaces are decoupled on purpose: emoji is the CLI human
    presentation; brackets are the structured marker the web frontend
    boxes as .tool-marker. This test asserts the dual-track invariant.
    """

    # Regex the frontend uses for matching (TOOL_MARKER_RE), restricted to
    # the registered names; matching the assertion in the design doc.
    BRACKET_RE = re.compile(
        r"^\[(Read|Bash|Edit|Write|Grep|Glob|Tool|MultiEdit|NotebookEdit|"
        r"WebFetch|WebSearch|LS|TodoWrite|Search|Fetch|Task)\b[^\]\n]*\]"
    )

    def _make_tracker(self, monkeypatch, tmp_path):
        """Build a tracker with flow context wired to a captured progress sink."""
        captured = []

        def fake_record_stream_progress(
            project_root, flow_id, step_id, step_type, content, raw_obj, attempt
        ):
            captured.append({
                "content": content,
                "raw_obj": raw_obj,
                "step_id": step_id,
                "attempt": attempt,
            })

        # The tracker imports record_stream_progress lazily inside
        # _emit_progress; patch on the chat_history module so the lazy
        # import picks up the fake.
        from se3.engine import chat_history
        monkeypatch.setattr(
            chat_history, "record_stream_progress", fake_record_stream_progress
        )

        tracker = StreamJSONTracker(
            project_root=tmp_path,
            flow_id="flow-x",
            step_id="step-y",
            step_type="implement",
            attempt=0,
        )
        return tracker, captured

    def test_tool_use_progress_content_is_bracketed(self, capsys, monkeypatch, tmp_path):
        tracker, captured = self._make_tracker(monkeypatch, tmp_path)
        tracker.process_line(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-r1",
                    "name": "Read",
                    "input": {"file_path": "src/foo.py"},
                }]
            }
        }))
        # The bracketed content lands in the recorded progress.
        progress_contents = [c["content"] for c in captured]
        assert any(
            self.BRACKET_RE.match(c) and c.startswith("[Read") for c in progress_contents
        ), f"no bracketed Read marker in {progress_contents!r}"
        # CLI stdout still carries the emoji line, byte-for-byte unchanged.
        out = capsys.readouterr().out
        assert "[llm-stream] 🔧 Read:" in out
        assert "src/foo.py" in out

    def test_tool_result_success_progress_content_is_bracketed(
        self, capsys, monkeypatch, tmp_path
    ):
        tracker, captured = self._make_tracker(monkeypatch, tmp_path)
        # tool_use to populate id→name mapping.
        tracker.process_line(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-r2",
                    "name": "Read",
                    "input": {"file_path": "a.py"},
                }]
            }
        }))
        captured.clear()  # focus on the result event
        tracker.process_line(json.dumps({
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu-r2",
                    "content": "line1\nline2\nline3",
                    "is_error": False,
                }]
            }
        }))
        progress_contents = [c["content"] for c in captured]
        assert any(
            self.BRACKET_RE.match(c) and c.startswith("[Read") for c in progress_contents
        ), f"no bracketed Read result marker in {progress_contents!r}"
        out = capsys.readouterr().out
        # ✅ line is byte-for-byte preserved.
        assert "[llm-stream] ✅ Read" in out

    def test_tool_result_error_with_known_tool_uses_bracketed_marker(
        self, capsys, monkeypatch, tmp_path
    ):
        tracker, captured = self._make_tracker(monkeypatch, tmp_path)
        # tool_use first so we have a tool_name available.
        tracker.process_line(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "id": "tu-e1",
                    "name": "Edit",
                    "input": {"file_path": "a.py", "old_string": "x", "new_string": "y"},
                }]
            }
        }))
        captured.clear()
        tracker.process_line(json.dumps({
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "tu-e1",
                    "content": "permission denied",
                    "is_error": True,
                }]
            }
        }))
        progress_contents = [c["content"] for c in captured]
        # Known tool: bracketed marker starts with the registered tool name.
        assert any(
            self.BRACKET_RE.match(c) and c.startswith("[Edit") for c in progress_contents
        ), f"no bracketed Edit error marker in {progress_contents!r}"
        out = capsys.readouterr().out
        assert "[llm-stream] ❌ Tool error: permission denied" in out

    def test_tool_result_error_with_unknown_tool_falls_back_to_tool_marker(
        self, capsys, monkeypatch, tmp_path
    ):
        tracker, captured = self._make_tracker(monkeypatch, tmp_path)
        # No prior tool_use → tool_name is empty; the error marker must fall
        # back to "[Tool error: ...]" so the frontend regex still matches.
        tracker.process_line(json.dumps({
            "type": "user",
            "message": {
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": "orphan",
                    "content": "boom",
                    "is_error": True,
                }]
            }
        }))
        progress_contents = [c["content"] for c in captured]
        assert any(
            self.BRACKET_RE.match(c) and c.startswith("[Tool") for c in progress_contents
        ), f"no bracketed Tool error marker in {progress_contents!r}"
        out = capsys.readouterr().out
        assert "[llm-stream] ❌ Tool error: boom" in out

    def test_stream_level_error_progress_content_is_bracketed(
        self, capsys, monkeypatch, tmp_path
    ):
        tracker, captured = self._make_tracker(monkeypatch, tmp_path)
        tracker.process_line(json.dumps({
            "type": "error",
            "error": "stream blew up",
        }))
        progress_contents = [c["content"] for c in captured]
        assert any(
            self.BRACKET_RE.match(c) and c.startswith("[Tool") for c in progress_contents
        ), f"no bracketed Tool error marker in {progress_contents!r}"
        out = capsys.readouterr().out
        # ❌ Error stdout line is byte-for-byte preserved.
        assert "[llm-stream] ❌ Error: stream blew up" in out

    def test_narrative_text_progress_not_bracketed(
        self, capsys, monkeypatch, tmp_path
    ):
        """Plain text/thinking chunks MUST remain unbracketed so the frontend
        renders them as ordinary markdown narrative, not as fake tool boxes."""
        tracker, captured = self._make_tracker(monkeypatch, tmp_path)
        # Feed enough text to cross _PROGRESS_FLUSH_CHARS so the buffer flushes.
        long_text = "x" * (StreamJSONTracker._PROGRESS_FLUSH_CHARS + 10)
        tracker.process_line(json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": long_text}]},
        }))
        # At least one progress write happened; none of them start with `[`.
        assert captured, "expected at least one flushed text progress"
        for c in captured:
            assert not c["content"].startswith("["), (
                f"narrative progress unexpectedly bracketed: {c['content']!r}"
            )


class TestLLMCallerStreamPrefix:
    """Tests for LLMCaller stream_prefix parameter."""

    def test_default_stream_prefix_is_empty(self):
        """LLMCaller should default to empty stream_prefix."""
        caller = LLMCaller()
        assert caller.stream_prefix == ''

    def test_stream_prefix_stored(self):
        """LLMCaller should store the stream_prefix."""
        caller = LLMCaller(stream_prefix='[G1] ')
        assert caller.stream_prefix == '[G1] '


class TestExtractTextFromNDJSON:
    """Tests for LLMCaller._extract_text_from_ndjson()."""

    def test_extracts_text_from_assistant_messages(self):
        """Should extract text content from NDJSON assistant messages."""
        ndjson = '\n'.join([
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Hello "}]
                }
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "world"}]
                }
            }),
        ])
        result = LLMCaller._extract_text_from_ndjson(ndjson)
        assert result == "Hello world"

    def test_strips_command_prefix_line(self):
        """Should strip '=== Command: ... ===' prefix lines."""
        ndjson = '\n'.join([
            '=== Command: claude -p "do something" ===',
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "result text"}]
                }
            }),
        ])
        result = LLMCaller._extract_text_from_ndjson(ndjson)
        assert result == "result text"
        assert "Command" not in result

    def test_fallback_when_no_text_extractable(self):
        """Should return None when no text content can be extracted."""
        # NDJSON with only tool_use, no text
        ndjson = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": "Write",
                    "input": {"file_path": "foo.py", "content": "print('hi')"}
                }]
            }
        })
        result = LLMCaller._extract_text_from_ndjson(ndjson)
        assert result is None

    def test_fallback_on_empty_output(self):
        """Should return None for empty or whitespace output."""
        assert LLMCaller._extract_text_from_ndjson("") is None
        assert LLMCaller._extract_text_from_ndjson("   \n  ") is None

    def test_ignores_non_assistant_types(self):
        """Should only extract from assistant messages, not tool_result etc."""
        ndjson = '\n'.join([
            json.dumps({
                "type": "tool_result",
                "result": {"toolUseId": "123", "content": "should be ignored"}
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "extracted"}]
                }
            }),
        ])
        result = LLMCaller._extract_text_from_ndjson(ndjson)
        assert result == "extracted"
        assert "ignored" not in result

    def test_handles_mixed_content_types(self):
        """Should extract text but skip tool_use items in same message."""
        ndjson = json.dumps({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I'll write the file"},
                    {"type": "tool_use", "name": "Write", "input": {}},
                    {"type": "text", "text": " now."},
                ]
            }
        })
        result = LLMCaller._extract_text_from_ndjson(ndjson)
        assert result == "I'll write the file now."

    def test_ignores_invalid_json_lines(self):
        """Should skip lines that aren't valid JSON."""
        ndjson = '\n'.join([
            'not json at all',
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "valid"}]
                }
            }),
            'also not json',
        ])
        result = LLMCaller._extract_text_from_ndjson(ndjson)
        assert result == "valid"

    def test_require_json_true_path_unchanged(self):
        """The require_json=True code path should not use NDJSON extraction.

        This is a design verification — _extract_text_from_ndjson is only
        called when require_json=False. The strict/extract/two_phase modes
        use parse_json_response or JSONExtractor instead.
        """
        # Verify the method exists and is static (can be called without instance)
        assert callable(LLMCaller._extract_text_from_ndjson)
        # Verify _contains_valid_json still uses parse_json_response
        ndjson_with_json = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": '{"key": "value"}'}]
            }
        })
        assert LLMCaller._contains_valid_json(ndjson_with_json) is True


class TestExtraPromptThreadSafety:
    """Test thread safety of module-level extra prompt globals."""

    def setup_method(self):
        """Clear extra prompt state before each test."""
        from se3.engine.llm_caller import clear_extra_prompt
        clear_extra_prompt()

    def teardown_method(self):
        """Clear extra prompt state after each test."""
        from se3.engine.llm_caller import clear_extra_prompt
        clear_extra_prompt()

    def test_concurrent_set_and_get(self):
        """Multiple threads setting/getting extra prompt should not corrupt state."""
        import threading
        from se3.engine.llm_caller import set_extra_prompt, get_extra_prompt

        errors = []
        barrier = threading.Barrier(4)

        def writer(value):
            try:
                barrier.wait(timeout=5)
                for _ in range(100):
                    set_extra_prompt(value)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                barrier.wait(timeout=5)
                for _ in range(100):
                    result = get_extra_prompt()
                    # Result should be None or a string — never a partial/corrupt value
                    assert result is None or isinstance(result, str)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=("prompt_A",)),
            threading.Thread(target=writer, args=("prompt_B",)),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread safety errors: {errors}"

    def test_concurrent_set_persistent_and_transient(self):
        """Concurrent persistent and transient writes should not interfere."""
        import threading
        from se3.engine.llm_caller import set_extra_prompt, get_extra_prompt

        errors = []
        barrier = threading.Barrier(2)

        def set_transient():
            try:
                barrier.wait(timeout=5)
                for _ in range(100):
                    set_extra_prompt("transient", persistent=False)
            except Exception as e:
                errors.append(e)

        def set_persistent():
            try:
                barrier.wait(timeout=5)
                for _ in range(100):
                    set_extra_prompt("persistent", persistent=True)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=set_transient)
        t2 = threading.Thread(target=set_persistent)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors
        # After both threads complete, both should be set
        result = get_extra_prompt()
        assert result is not None
        assert "persistent" in result
        assert "transient" in result

    def test_concurrent_clear_and_set(self):
        """Concurrent clearing and setting should not raise exceptions."""
        import threading
        from se3.engine.llm_caller import (
            set_extra_prompt, clear_extra_prompt, clear_persistent_extra_prompt
        )

        errors = []
        barrier = threading.Barrier(3)

        def setter():
            try:
                barrier.wait(timeout=5)
                for _ in range(100):
                    set_extra_prompt("value", persistent=True)
                    set_extra_prompt("transient")
            except Exception as e:
                errors.append(e)

        def clearer():
            try:
                barrier.wait(timeout=5)
                for _ in range(100):
                    clear_extra_prompt()
            except Exception as e:
                errors.append(e)

        def partial_clearer():
            try:
                barrier.wait(timeout=5)
                for _ in range(100):
                    clear_persistent_extra_prompt()
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=setter),
            threading.Thread(target=clearer),
            threading.Thread(target=partial_clearer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread safety errors: {errors}"


class TestCallWithRetryDedup:
    """Integration test: _call_with_retry invokes deduplicate_prompt_lines."""

    def test_call_with_retry_skips_dedup_on_first_call(self, tmp_path):
        """Verify that _call_with_retry does NOT call deduplicate_prompt_lines
        on the first call (total_attempt == 0) since there is no internal repetition."""
        from unittest.mock import patch, MagicMock
        from se3.engine.prompt_dedup import deduplicate_prompt_lines as real_dedup

        caller = LLMCaller(
            project_root=tmp_path,
            flow_id="test-flow",
            step_id="test-step",
            step_type="implement",
            agents=[{"name": "test", "type": "claude-code", "cmd": "echo"}],
        )

        with patch("se3.engine.llm_caller.deduplicate_prompt_lines", wraps=real_dedup) as mock_dedup, \
             patch.object(caller, "_get_current_runner") as mock_get_runner:
            runner_inst = MagicMock()
            result_obj = MagicMock()
            result_obj.success = True
            result_obj.output = "test output"
            result_obj.interrupted = False
            runner_inst.run_with_monitor.return_value = result_obj
            mock_get_runner.return_value = runner_inst

            # Use a simple on_output callback to avoid StreamJSONTracker setup
            output_lines = []
            caller._call_with_retry(
                prompt="test prompt",
                timeout=30,
                context_files=None,
                on_output=lambda line: output_lines.append(line),
                require_json=False,
                json_retry_count=0,
            )

            # Dedup should NOT be called on first call (no repetition possible)
            mock_dedup.assert_not_called()

            # Verify the original prompt was passed to run_with_monitor unchanged
            call_kwargs = runner_inst.run_with_monitor.call_args
            passed_args = call_kwargs[1]["args"] if "args" in (call_kwargs[1] or {}) else call_kwargs[0][0]
            prompt_idx = passed_args.index("-p")
            assert passed_args[prompt_idx + 1] == "test prompt"

    def test_record_prompt_records_original_runner_gets_deduped(self, tmp_path):
        """On retry, _record_prompt receives the ORIGINAL prompt (so the persistent
        chat history stays free of retry-context bloat), but the runner still gets
        the deduped effective_prompt."""
        from unittest.mock import patch, MagicMock

        # A prompt that will actually change after dedup
        spec_block = "spec A\nspec B\nspec C"
        original_prompt = f"header\n{spec_block}\nmiddle\n{spec_block}\nfooter"

        caller = LLMCaller(
            project_root=tmp_path,
            flow_id="test-flow",
            step_id="test-step",
            step_type="implement",
            agents=[{"name": "test", "type": "claude-code", "cmd": "echo"}],
        )
        # Simulate a retry so total_attempt > 0 and dedup is invoked
        caller.external_attempt = 1

        with patch.object(caller, "_record_prompt") as mock_record, \
             patch.object(caller, "_get_current_runner") as mock_get_runner, \
             patch.object(caller, "_get_retry_context", return_value=None):
            runner_inst = MagicMock()
            result_obj = MagicMock()
            result_obj.success = True
            result_obj.output = "test output"
            result_obj.interrupted = False
            runner_inst.run_with_monitor.return_value = result_obj
            mock_get_runner.return_value = runner_inst

            output_lines = []
            caller._call_with_retry(
                prompt=original_prompt,
                timeout=30,
                context_files=None,
                on_output=lambda line: output_lines.append(line),
                require_json=False,
                json_retry_count=0,
            )

            # Recorded prompt is the raw original — no dedup applied to history
            mock_record.assert_called_once()
            recorded_prompt = mock_record.call_args[0][0]
            assert recorded_prompt == original_prompt
            assert "DUPLICATED CONTENT" not in recorded_prompt

            # Runner's -p argument received the deduped prompt
            run_args = runner_inst.run_with_monitor.call_args.kwargs["args"]
            sent_prompt = run_args[run_args.index("-p") + 1]
            assert "DUPLICATED CONTENT" in sent_prompt
            standalone_count = sum(1 for l in sent_prompt.split("\n") if l == "spec A")
            assert standalone_count == 1

    def test_dedup_with_continue_mode_retry_context(self, tmp_path):
        """Verify dedup works correctly in continue mode where retry context
        is combined with a continuation instruction (not the full original prompt)."""
        from unittest.mock import patch, MagicMock

        spec_block = "spec line 1\nspec line 2\nspec line 3"
        original_prompt = f"Task: implement\n{spec_block}\nBegin work."

        # Simulate retry context containing the same spec block
        retry_context = (
            "[Previous conversation context for this step]:\n"
            "\n=== Attempt 1 ===\n"
            "\n[User Prompt]:\n"
            f"Task: implement\n{spec_block}\nBegin work.\n"
            "\n[Assistant Response]:\n"
            "I started but hit an error.\n"
            "\n========================================\n"
            "[Continue from where the previous attempt stopped. "
            "Do NOT redo completed work — pick up from the breakpoint.]"
        )

        caller = LLMCaller(
            project_root=tmp_path,
            flow_id="test-flow",
            step_id="test-step",
            step_type="implement",
            agents=[{"name": "test", "type": "claude-code", "cmd": "echo"}],
        )
        # Simulate external_attempt > 0 to trigger retry path
        caller.external_attempt = 1
        caller.retry_mode = "continue"

        with patch.object(caller, "_get_retry_context", return_value=retry_context), \
             patch.object(caller, "_record_prompt") as mock_record, \
             patch.object(caller, "_get_current_runner") as mock_get_runner:
            runner_inst = MagicMock()
            result_obj = MagicMock()
            result_obj.success = True
            result_obj.output = "test output"
            result_obj.interrupted = False
            runner_inst.run_with_monitor.return_value = result_obj
            mock_get_runner.return_value = runner_inst

            output_lines = []
            caller._call_with_retry(
                prompt=original_prompt,
                timeout=30,
                context_files=None,
                on_output=lambda line: output_lines.append(line),
                require_json=False,
                json_retry_count=0,
            )

            mock_record.assert_called_once()
            recorded_prompt = mock_record.call_args[0][0]
            # Recorded prompt is the raw original_prompt — retry context lives only in
            # what's sent to the runner, not in the persistent history record.
            assert recorded_prompt == original_prompt
            assert "[Previous conversation context for this step]:" not in recorded_prompt

            # Runner-bound effective_prompt carries the continue-mode structure.
            run_args = runner_inst.run_with_monitor.call_args.kwargs["args"]
            sent_prompt = run_args[run_args.index("-p") + 1]
            assert "[Previous conversation context for this step]:" in sent_prompt
            assert "Continue the task from where you left off" in sent_prompt
            # The original prompt is NOT re-appended after the retry context in continue mode.
            assert sent_prompt.count("Begin work.") == 1
            assert sent_prompt.count("spec line 1") == 1
            # Continue mode does not re-append the original prompt, so there is no
            # duplicated content to deduplicate — no dedup markers in sent prompt either.
            assert "DUPLICATED CONTENT" not in sent_prompt

    def test_dedup_with_retry_mode_deduplicates_spec(self, tmp_path):
        """Verify dedup removes repeated spec content in retry mode where
        retry context + original prompt produces duplication."""
        from unittest.mock import patch, MagicMock

        spec_block = "spec line 1\nspec line 2\nspec line 3"
        original_prompt = f"Task: implement\n{spec_block}\nBegin work."

        # Simulate retry context containing the same spec block (retry mode re-prepends original)
        retry_context = (
            "[Previous conversation context for this step]:\n"
            "\n=== Attempt 1 ===\n"
            "\n[User Prompt]:\n"
            f"Task: implement\n{spec_block}\nBegin work.\n"
            "\n[Assistant Response]:\n"
            "I started but hit an error.\n"
            "\n========================================\n"
            "[The above attempt(s) failed. Please try again with the same task.]"
        )

        caller = LLMCaller(
            project_root=tmp_path,
            flow_id="test-flow",
            step_id="test-step",
            step_type="implement",
            agents=[{"name": "test", "type": "claude-code", "cmd": "echo"}],
        )
        caller.external_attempt = 1
        caller.retry_mode = "retry"

        with patch.object(caller, "_get_retry_context", return_value=retry_context), \
             patch.object(caller, "_record_prompt") as mock_record, \
             patch.object(caller, "_get_current_runner") as mock_get_runner:
            runner_inst = MagicMock()
            result_obj = MagicMock()
            result_obj.success = True
            result_obj.output = "test output"
            result_obj.interrupted = False
            runner_inst.run_with_monitor.return_value = result_obj
            mock_get_runner.return_value = runner_inst

            output_lines = []
            caller._call_with_retry(
                prompt=original_prompt,
                timeout=30,
                context_files=None,
                on_output=lambda line: output_lines.append(line),
                require_json=False,
                json_retry_count=0,
            )

            mock_record.assert_called_once()
            recorded_prompt = mock_record.call_args[0][0]
            # Recorded prompt is the raw original_prompt — clean of retry context
            assert recorded_prompt == original_prompt

            # In retry mode, effective_prompt = retry_context + "\n" + original_prompt;
            # the spec block appears twice in the runner-bound prompt, and dedup replaces the second.
            run_args = runner_inst.run_with_monitor.call_args.kwargs["args"]
            sent_prompt = run_args[run_args.index("-p") + 1]
            assert "DUPLICATED CONTENT" in sent_prompt
            assert sent_prompt.count("spec line 1") == 1

    def test_internal_retry_triggers_dedup(self, tmp_path):
        """Verify dedup runs on internal retry (external_attempt=0, internal_attempt=1).

        When a network-level retry happens within the first external call,
        total_attempt = 0 * max_retries + 1 = 1, which triggers dedup.
        This tests the interaction between dedup and retry context generated
        from only the current call's first attempt (just recorded moments before).
        """
        from unittest.mock import patch, MagicMock

        spec_block = "spec line 1\nspec line 2\nspec line 3"
        original_prompt = f"Task: implement\n{spec_block}\nBegin work."

        # Retry context simulating what would be recorded from the first internal attempt
        retry_context = (
            "[Previous conversation context for this step]:\n"
            "\n=== Attempt 1 ===\n"
            "\n[User Prompt]:\n"
            f"Task: implement\n{spec_block}\nBegin work.\n"
            "\n[Assistant Response]:\n"
            "Started but network error.\n"
            "\n========================================\n"
            "[The above attempt(s) failed. Please try again with the same task.]"
        )

        caller = LLMCaller(
            project_root=tmp_path,
            flow_id="test-flow",
            step_id="test-step",
            step_type="implement",
            agents=[{"name": "test", "type": "claude-code", "cmd": "echo"}],
        )
        # external_attempt=0: this is the first external call
        caller.external_attempt = 0
        caller.retry_mode = "retry"

        # We need to simulate the internal retry loop: first attempt fails, second succeeds.
        # On internal_attempt=0: total_attempt=0, no dedup (first call)
        # On internal_attempt=1: total_attempt=1, dedup runs
        call_count = [0]
        recorded_prompts = []

        def fake_record_prompt(prompt_text, attempt):
            recorded_prompts.append(prompt_text)

        def fake_get_retry_context():
            # Only return retry context on the second internal attempt
            if call_count[0] > 0:
                return retry_context
            return None

        sent_prompts = []

        def fake_run_with_monitor(**kwargs):
            call_count[0] += 1
            args = kwargs["args"]
            sent_prompts.append(args[args.index("-p") + 1])
            result_obj = MagicMock()
            if call_count[0] == 1:
                # First internal attempt fails
                result_obj.success = False
                result_obj.output = ""
                result_obj.interrupted = False
                raise RuntimeError("Network error")
            # Second internal attempt succeeds
            result_obj.success = True
            result_obj.output = "test output"
            result_obj.interrupted = False
            return result_obj

        with patch.object(caller, "_record_prompt", side_effect=fake_record_prompt), \
             patch.object(caller, "_get_retry_context", side_effect=fake_get_retry_context), \
             patch.object(caller, "_get_current_runner") as mock_get_runner, \
             patch.object(caller, "_rotate_agent"):
            runner_inst = MagicMock()
            runner_inst.run_with_monitor.side_effect = fake_run_with_monitor
            mock_get_runner.return_value = runner_inst

            output_lines = []
            caller._call_with_retry(
                prompt=original_prompt,
                timeout=30,
                context_files=None,
                on_output=lambda line: output_lines.append(line),
                require_json=False,
                json_retry_count=0,
            )

            # Both internal attempts record the raw original_prompt (no retry-context bloat
            # leaked into history). Dedup behavior is verified on the runner-bound prompts.
            assert len(recorded_prompts) == 2
            assert recorded_prompts[0] == original_prompt
            assert recorded_prompts[1] == original_prompt

            assert len(sent_prompts) == 2
            # First call: no dedup (total_attempt=0)
            assert "DUPLICATED CONTENT" not in sent_prompts[0]
            # Second call: dedup applied (total_attempt=1), spec block deduplicated
            assert "DUPLICATED CONTENT" in sent_prompts[1]
            assert sent_prompts[1].count("spec line 1") == 1

    def test_dedup_runs_as_noop_when_retry_context_is_none(self, tmp_path):
        """When total_attempt > 0 but _get_retry_context() returns None,
        dedup still runs on the original prompt (as a no-op since there
        is no internal repetition). Verifies the code path is exercised."""
        from unittest.mock import patch, MagicMock
        from se3.engine.prompt_dedup import deduplicate_prompt_lines as real_dedup

        caller = LLMCaller(
            project_root=tmp_path,
            flow_id="test-flow",
            step_id="test-step",
            step_type="implement",
            agents=[{"name": "test", "type": "claude-code", "cmd": "echo"}],
        )
        # Simulate a retry (external_attempt=1) so total_attempt > 0
        caller.external_attempt = 1

        with patch("se3.engine.llm_caller.deduplicate_prompt_lines", wraps=real_dedup) as mock_dedup, \
             patch.object(caller, "_get_current_runner") as mock_get_runner, \
             patch.object(caller, "_get_retry_context", return_value=None):
            runner_inst = MagicMock()
            result_obj = MagicMock()
            result_obj.success = True
            result_obj.output = "test output"
            result_obj.raw_lines = []
            runner_inst.run_with_monitor.return_value = result_obj
            mock_get_runner.return_value = runner_inst

            caller._call_with_retry(
                prompt="unique prompt without repetition",
                timeout=30,
                context_files=None,
                on_output=lambda x: None,
                require_json=False,
                json_retry_count=0,
            )

            # Dedup should be called (total_attempt > 0) even though retry context is None
            mock_dedup.assert_called_once()
            # The prompt has no internal repetition, so dedup is a no-op
            passed_args = runner_inst.run_with_monitor.call_args
            call_args = passed_args[1]["args"] if "args" in (passed_args[1] or {}) else passed_args[0][0]
            prompt_idx = call_args.index("-p")
            assert call_args[prompt_idx + 1] == "unique prompt without repetition"
