"""Core output formatting for the SE3 flow engine.

Provides utilities for formatting and displaying output from flow steps.
Delegates to display.py for user-facing content to ensure full content rendering.
All truncation logic has been removed - content is always displayed in full.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Union

from ..i18n import t

# Import display utilities for user-facing output
from .display import (
    get_console,
    render_full,
    render_usage_block,
)

logger = logging.getLogger(__name__)


def format_output(data: Any, truncate: bool = False) -> str:
    """Format data for output without truncation.

    This function converts any data type to a string representation.
    The truncate parameter is kept for API compatibility but is ignored -
    content is always rendered in full.

    Args:
        data: The data to format (string, dict, list, etc.)
        truncate: Deprecated, kept for API compatibility. Always False internally.

    Returns:
        Formatted string representation of the data
    """
    if data is None:
        return ""

    if isinstance(data, str):
        # Return string as-is - no truncation
        return data

    if isinstance(data, (dict, list)):
        # Pretty-print JSON structures
        try:
            return json.dumps(data, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(data)

    # Default to string representation
    return str(data)


def log_output(level: str, message: str, *args: Any, **kwargs: Any) -> None:
    """Log output at the specified level.

    This function is for internal/logging output that should not
    go through the display module.

    Args:
        level: Log level (debug, info, warning, error, critical)
        message: The message to log
        *args: Additional args for logging
        **kwargs: Additional kwargs for logging
    """
    log_func = getattr(logger, level.lower(), logger.info)
    log_func(message, *args, **kwargs)


def format_error(error: Union[str, Exception], context: Optional[Dict[str, Any]] = None) -> str:
    """Format an error message for display.

    Args:
        error: The error message or exception
        context: Optional context information

    Returns:
        Formatted error string
    """
    if isinstance(error, Exception):
        error_msg = f"{type(error).__name__}: {error}"
    else:
        error_msg = str(error)

    lines = [f"[bold red]{t('output.error.prefix')}[/bold red] {error_msg}"]

    if context:
        lines.append("")
        lines.append(f"[bold]{t('output.context.heading')}[/bold]")
        for key, value in context.items():
            lines.append(f"  {key}: {value}")

    return "\n".join(lines)


def display_error(error: Union[str, Exception], context: Optional[Dict[str, Any]] = None) -> None:
    """Display an error message with formatting.

    Args:
        error: The error message or exception
        context: Optional context information
    """
    content = format_error(error, context)
    render_full(content, title=t("output.panel.error"))


def display_success(message: str, details: Optional[Dict[str, Any]] = None) -> None:
    """Display a success message.

    Args:
        message: The success message
        details: Optional details to display
    """
    lines = [f"[bold green]{message}[/bold green]"]

    if details:
        lines.append("")
        for key, value in details.items():
            lines.append(f"[bold]{key}:[/bold] {value}")

    content = "\n".join(lines)
    render_full(content, title=t("output.panel.success"))
