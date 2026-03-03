"""Core output formatting for the SE3 flow engine.

Provides utilities for formatting and displaying output from flow steps.
Delegates to display.py for user-facing content to ensure full content rendering.
All truncation logic has been removed - content is always displayed in full.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Union

# Import display utilities for user-facing output
from .display import (
    get_console,
    render_code,
    render_design,
    render_full,
    render_markdown,
    render_proposal,
    render_spec_content,
    render_text,
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


def print_step_output(step: str, content: Any) -> None:
    """Print output from a step execution.

    Uses display utilities for formatted, full-content output.

    Args:
        step: The step name/type
        content: The content to display
    """
    formatted = format_output(content)

    if not formatted:
        logger.debug(f"Step '{step}' produced no output")
        return

    # Use display module for full-content rendering
    render_full(formatted, title=f"Step: {step}")


def display_step_result(step_result: Dict[str, Any]) -> None:
    """Display a step result with appropriate formatting.

    Args:
        step_result: Dictionary containing step execution results
    """
    step_type = step_result.get("step_type", "Unknown")
    status = step_result.get("status", "unknown")
    output = step_result.get("output", {})

    # Build content based on result structure
    lines = [f"[bold]Status:[/bold] {status}", ""]

    if isinstance(output, dict):
        for key, value in output.items():
            if isinstance(value, (dict, list)):
                lines.append(f"[bold]{key}:[/bold]")
                lines.append(format_output(value))
            else:
                lines.append(f"[bold]{key}:[/bold] {value}")
            lines.append("")
    else:
        lines.append(format_output(output))

    content = "\n".join(lines)
    render_full(content, title=f"Result: {step_type}")


def display_proposal(proposal: Dict[str, Any]) -> None:
    """Display a proposal with full content rendering.

    Args:
        proposal: Proposal dictionary with summary, files, rationale, etc.
    """
    render_proposal(proposal)


def display_design(design: Dict[str, Any]) -> None:
    """Display a design document with full content rendering.

    Args:
        design: Design document dictionary with components, interfaces, etc.
    """
    render_design(design)


def display_spec(spec: Dict[str, Any]) -> None:
    """Display spec content with full rendering.

    Args:
        spec: Spec dictionary with requirements, description, etc.
    """
    render_spec_content(spec)


def display_analysis(analysis: Dict[str, Any]) -> None:
    """Display analysis results with appropriate formatting.

    Args:
        analysis: Analysis result dictionary
    """
    lines = []

    # Summary section
    summary = analysis.get("summary", "")
    if summary:
        lines.append("[bold cyan]Analysis Summary[/bold cyan]")
        lines.append(summary)
        lines.append("")

    # Findings/insights
    findings = analysis.get("findings", analysis.get("insights", []))
    if findings:
        lines.append("[bold yellow]Findings[/bold yellow]")
        for finding in findings:
            if isinstance(finding, dict):
                title = finding.get("title", finding.get("name", ""))
                desc = finding.get("description", finding.get("detail", ""))
                if title:
                    lines.append(f"\n[bold]{title}[/bold]")
                if desc:
                    lines.append(desc)
            else:
                lines.append(f"  • {finding}")
        lines.append("")

    # Recommendations
    recommendations = analysis.get("recommendations", [])
    if recommendations:
        lines.append("[bold green]Recommendations[/bold green]")
        for rec in recommendations:
            if isinstance(rec, dict):
                action = rec.get("action", rec.get("recommendation", ""))
                priority = rec.get("priority", "")
                if priority:
                    lines.append(f"\n• [{priority}] {action}")
                else:
                    lines.append(f"• {action}")
            else:
                lines.append(f"  • {rec}")
        lines.append("")

    # Additional fields
    for key, value in analysis.items():
        if key not in ("summary", "findings", "insights", "recommendations"):
            if value:
                lines.append(f"[bold]{key.replace('_', ' ').title()}[/bold]")
                if isinstance(value, list):
                    for item in value:
                        lines.append(f"  • {item}")
                elif isinstance(value, dict):
                    for k, v in value.items():
                        lines.append(f"  [bold]{k}:[/bold] {v}")
                else:
                    lines.append(str(value))
                lines.append("")

    content = "\n".join(lines)
    render_full(content, title="Analysis Results")


def display_code(content: str, language: str = "python", title: Optional[str] = None) -> None:
    """Display code content with syntax highlighting.

    Args:
        content: The code content to display
        language: Programming language for syntax highlighting
        title: Optional title for the panel
    """
    render_code(content, language=language, title=title)


def display_markdown(content: str, title: Optional[str] = None) -> None:
    """Display markdown content with rendering.

    Args:
        content: The markdown content to display
        title: Optional title for the panel
    """
    render_markdown(content, title=title)


def display_text(content: str, title: Optional[str] = None, style: Optional[str] = None) -> None:
    """Display plain text content.

    Args:
        content: The text content to display
        title: Optional title for the panel
        style: Optional Rich style string for the content
    """
    render_text(content, title=title, style=style)


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

    lines = [f"[bold red]Error:[/bold red] {error_msg}"]

    if context:
        lines.append("")
        lines.append("[bold]Context:[/bold]")
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
    render_full(content, title="Error")


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
    render_full(content, title="Success")
