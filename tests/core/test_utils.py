"""Tests for core utility functions."""

import pytest

from se3.core.utils import truncate_preview


class TestTruncatePreview:
    """Tests for truncate_preview function."""

    def test_empty_string_returns_empty(self):
        """Empty string should return empty string."""
        assert truncate_preview("") == ""

    def test_shorter_than_limit_returns_unchanged(self):
        """String shorter than max_length returns unchanged."""
        text = "Hello world"
        result = truncate_preview(text, max_length=100)
        assert result == "Hello world"
        assert result is text  # Same object, not copied

    def test_exact_length_returns_unchanged(self):
        """String at exact limit returns unchanged."""
        text = "Exactly 20 chars!!!"
        result = truncate_preview(text, max_length=len(text))
        assert result == text
        assert result is text

    def test_exceeding_limit_truncates_with_ellipsis(self):
        """String exceeding limit is truncated with ellipsis."""
        text = "This is a very long text that needs truncation"
        result = truncate_preview(text, max_length=20)
        assert result == "This is a very lo..."
        assert len(result) == 20

    def test_unicode_characters_handled_correctly(self):
        """Unicode characters are counted correctly."""
        # Emoji and CJK characters
        text = "你好世界🌍 This is a long message"
        result = truncate_preview(text, max_length=15)
        # Should truncate correctly (each char = 1)
        assert result.endswith("...")
        assert len(result) == 15

    def test_custom_max_length_parameter(self):
        """Custom max_length parameter works correctly."""
        text = "Hello world this is long"
        result = truncate_preview(text, max_length=10)
        assert result == "Hello w..."
        assert len(result) == 10

    def test_custom_ellipsis_string(self):
        """Custom ellipsis string is used correctly."""
        text = "Hello world"
        result = truncate_preview(text, max_length=10, ellipsis_str="..")
        assert result == "Hello w.."
        assert len(result) == 10

    def test_ellipsis_longer_than_max_length(self):
        """Edge case: ellipsis longer than max_length."""
        text = "Hello world"
        result = truncate_preview(text, max_length=2, ellipsis_str="...")
        # Should return just the truncated ellipsis
        assert result == ".."
        assert len(result) == 2

    def test_max_length_equals_ellipsis_length(self):
        """Edge case: max_length equals ellipsis length."""
        text = "Hello world"
        result = truncate_preview(text, max_length=3, ellipsis_str="...")
        assert result == "..."
        assert len(result) == 3

    def test_single_character_max_length(self):
        """Edge case: max_length of 1."""
        text = "Hello world"
        result = truncate_preview(text, max_length=1, ellipsis_str="...")
        assert result == "."
        assert len(result) == 1

    def test_newlines_in_text(self):
        """Text with newlines is truncated correctly."""
        text = "Line 1\nLine 2\nLine 3 is very long"
        result = truncate_preview(text, max_length=20)
        assert result.endswith("...")
        assert len(result) == 20

    def test_default_max_length_is_100(self):
        """Default max_length is 100 characters."""
        text = "x" * 150
        result = truncate_preview(text)
        assert len(result) == 100
        assert result.endswith("...")
        assert result == "x" * 97 + "..."
