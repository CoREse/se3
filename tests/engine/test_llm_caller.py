"""Tests for the LLM caller module, specifically tool preview formatters."""

import json
import pytest

from se3.engine.llm_caller import (
    truncate_preview,
    format_tool_use_preview,
    format_tool_result_preview,
    StreamJSONTracker,
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

    def test_simple_tool_call(self):
        """Simple tool call with basic parameters."""
        result = format_tool_use_preview("Read", {"path": "foo.py"})
        assert result == "Tool: Read | Input: path=foo.py"

    def test_multiple_parameters(self):
        """Tool call with multiple parameters."""
        result = format_tool_use_preview(
            "Write",
            {"file_path": "test.py", "content": "print('hello')"}
        )
        assert "Tool: Write" in result
        assert "file_path=test.py" in result
        assert "content=print('hello')" in result

    def test_empty_input(self):
        """Tool call with empty input."""
        result = format_tool_use_preview("Read", {})
        assert result == "Tool: Read | Input: (none)"

    def test_none_input(self):
        """Tool call with None input."""
        result = format_tool_use_preview("Read", None)
        assert result == "Tool: Read | Input: (none)"

    def test_truncates_long_string_value(self):
        """Long string values should be truncated."""
        long_path = "/very/long/path/to/the/file/that/exceeds/limit.txt"
        result = format_tool_use_preview("Read", {"path": long_path})
        assert "..." in result
        assert len(result) < len(long_path) + 30

    def test_handles_numeric_values(self):
        """Numeric values should be formatted without quotes."""
        result = format_tool_use_preview("Search", {"limit": 10, "offset": 5.5})
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
        """Only first 3 parameters should be shown with ellipsis."""
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

    def test_string_result(self):
        """Simple string result."""
        result = format_tool_result_preview("File contents here")
        assert result == "Result: File contents here"

    def test_empty_string_result(self):
        """Empty string result."""
        result = format_tool_result_preview("")
        assert result == "Result: (empty)"

    def test_whitespace_only_result(self):
        """Whitespace-only string result."""
        result = format_tool_result_preview("   \n\t  ")
        assert result == "Result: (empty)"

    def test_none_result(self):
        """None result."""
        result = format_tool_result_preview(None)
        assert result == "Result: (empty)"

    def test_long_string_truncated(self):
        """Long string result should be truncated."""
        long_content = "x" * 100
        result = format_tool_result_preview(long_content)
        assert "..." in result
        assert len(result) <= 68  # "Result: " + 60 + "..."

    def test_dict_result(self):
        """Dict result should be JSON-formatted."""
        data = {"status": "ok", "count": 42}
        result = format_tool_result_preview(data)
        assert result.startswith("Result: {")
        assert "status" in result
        assert "count" in result

    def test_dict_with_error_flag(self):
        """Dict with isError flag should show error formatting."""
        data = {"isError": True, "content": "Something went wrong"}
        result = format_tool_result_preview(data)
        assert "error" in result.lower()
        assert "Something went wrong" in result

    def test_dict_with_is_error_snake_case(self):
        """Dict with is_error flag (snake_case) should show error formatting."""
        data = {"is_error": True, "content": "An error occurred"}
        result = format_tool_result_preview(data)
        assert "error" in result.lower()
        assert "An error occurred" in result

    def test_list_result(self):
        """List result should be JSON-formatted."""
        data = ["item1", "item2", "item3"]
        result = format_tool_result_preview(data)
        assert result.startswith("Result: [")
        assert "item1" in result

    def test_number_result(self):
        """Number result should be converted to string."""
        result = format_tool_result_preview(42)
        assert result == "Result: 42"

    def test_boolean_result(self):
        """Boolean result should be converted to string."""
        result = format_tool_result_preview(True)
        assert result == "Result: True"

    def test_nested_dict_truncated(self):
        """Nested dict with long content should be truncated."""
        data = {"data": "x" * 100}
        result = format_tool_result_preview(data)
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
        """Tool use messages should be tracked and formatted."""
        tracker = StreamJSONTracker()
        line = json.dumps({
            "type": "assistant",
            "message": {
                "content": [{
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"path": "test.py"}
                }]
            }
        })
        tracker.process_line(line)
        assert tracker.message_count == 1
        assert len(tracker.tool_calls) == 1
        assert tracker.tool_calls[0] == "Read"

    def test_process_tool_result_success(self, capsys):
        """Successful tool results should be tracked."""
        tracker = StreamJSONTracker()
        line = json.dumps({
            "type": "tool_result",
            "result": {
                "toolUseId": "tool-123",
                "content": "File content here",
                "isError": False
            }
        })
        tracker.process_line(line)
        assert len(tracker.tool_results) == 1
        assert tracker.tool_results[0] == "tool-123"

    def test_process_tool_result_error(self, capsys):
        """Error tool results should be tracked."""
        tracker = StreamJSONTracker()
        line = json.dumps({
            "type": "tool_result",
            "result": {
                "toolUseId": "tool-456",
                "content": "Error message",
                "isError": True
            }
        })
        tracker.process_line(line)
        assert len(tracker.tool_results) == 1

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
