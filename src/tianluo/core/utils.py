"""Core utility functions for SE3.

Shared utility functions used across the SE3 framework.
"""


def truncate_preview(text: str, max_length: int = 100, ellipsis_str: str = "...") -> str:
    """Truncate text to a maximum length with ellipsis indicator.

    Provides consistent preview formatting for displaying text content
    that may be too long for console output. Returns the original text
    if it fits within the limit, otherwise truncates and appends ellipsis.

    Args:
        text: The text to truncate
        max_length: Maximum length for the preview (default: 100)
        ellipsis_str: String to append when truncating (default: "...")

    Returns:
        Truncated text with ellipsis if exceeded, or original text if within limit

    Examples:
        >>> truncate_preview("Hello world", max_length=20)
        'Hello world'
        >>> truncate_preview("This is a very long text", max_length=10)
        'This is a...'
    """
    if not text:
        return text

    if len(text) <= max_length:
        return text

    # Account for ellipsis length in the truncation
    truncate_at = max_length - len(ellipsis_str)
    if truncate_at <= 0:
        # Edge case: max_length is smaller than ellipsis
        return ellipsis_str[:max_length]

    return text[:truncate_at] + ellipsis_str
