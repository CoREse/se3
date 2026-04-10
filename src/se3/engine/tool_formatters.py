"""Tool formatting for LLM call output rendering.

Provides per-tool formatters for tool_use and tool_result previews.
Used by both llm_caller.py (stream output) and chat_history.py (history rendering)
to eliminate duplication and provide richer, tool-aware formatting.

Architecture: dictionary registry (TOOL_FORMATTERS) mapping tool names to
{use: fn, result: fn} pairs. Unknown tools fall back to generic formatters.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level project root for path truncation
# ---------------------------------------------------------------------------

_project_root: Optional[Path] = None


def set_project_root(root: Path) -> None:
    """Set the project root directory for path truncation."""
    global _project_root
    _project_root = root


def get_project_root() -> Optional[Path]:
    """Get the current project root directory."""
    return _project_root


# ---------------------------------------------------------------------------
# Core utility
# ---------------------------------------------------------------------------

def truncate_path(path: str, max_length: int = 160, project_root: Optional[Path] = None) -> str:
    """Truncate a file path, preserving the filename and first directory segment.

    1. Convert absolute path to relative (using project_root or module-level default).
    2. If still too long, abbreviate middle segments: first_dir/.../filename
    3. The filename (last segment) is never truncated.

    Args:
        path: The file path to truncate
        max_length: Maximum length (default 160, matching Claude Code)
        project_root: Optional project root for relative conversion;
                      falls back to module-level _project_root

    Returns:
        Truncated path string
    """
    if not path:
        return ""

    path = str(path)

    # Step 1: Convert absolute path to relative
    root = project_root or _project_root
    if root and os.path.isabs(path):
        try:
            rel = os.path.relpath(path, str(root))
            # Only use relpath if it doesn't go upward excessively
            if not rel.startswith('..'):
                path = rel
        except ValueError:
            # On Windows, relpath can fail across drives
            pass

    if len(path) <= max_length:
        return path

    # Step 2: Middle truncation — keep first segment and filename
    parts = path.replace('\\', '/').split('/')
    filename = parts[-1]

    # If only one segment (just a filename), return as-is (never truncate filename)
    if len(parts) <= 1:
        return path

    first_segment = parts[0]
    abbreviated = f"{first_segment}/.../{filename}"

    if len(abbreviated) <= max_length:
        return abbreviated

    # Even abbreviated form is too long — just return first_segment/.../filename
    # (filename is never truncated)
    return abbreviated


def truncate_preview(text: str, max_length: int = 60, ellipsis_str: str = '...') -> str:
    """Truncate text to a preview with ellipsis if too long.

    Args:
        text: The text to truncate
        max_length: Maximum length before truncation
        ellipsis_str: String to append when truncated

    Returns:
        Truncated text with ellipsis if needed
    """
    if not text:
        return ""
    text = str(text).replace('\n', ' ')
    if len(text) <= max_length:
        return text
    truncate_at = max_length - len(ellipsis_str)
    if truncate_at <= 0:
        return ellipsis_str[:max_length]
    return text[:truncate_at] + ellipsis_str


# ---------------------------------------------------------------------------
# Generic (fallback) formatters
# ---------------------------------------------------------------------------

def _generic_tool_use_preview(tool_name: str, input_data: dict) -> str:
    """Format a generic tool_use preview showing key parameters.

    Shows tool name and up to 3 key=value pairs.
    """
    if not input_data or not isinstance(input_data, dict):
        return f"Tool: {tool_name} | Input: (none)"

    params = []
    for i, (key, value) in enumerate(input_data.items()):
        if i >= 3:
            params.append("...")
            break

        if isinstance(value, str):
            val_preview = truncate_preview(value, max_length=30)
            params.append(f"{key}={val_preview}")
        elif isinstance(value, (int, float, bool)):
            params.append(f"{key}={value}")
        elif isinstance(value, (list, dict)):
            val_str = json.dumps(value, ensure_ascii=False)
            val_preview = truncate_preview(val_str, max_length=30)
            params.append(f"{key}={val_preview}")
        else:
            val_preview = truncate_preview(str(value), max_length=30)
            params.append(f"{key}={val_preview}")

    if not params:
        return f"Tool: {tool_name} | Input: (none)"

    return f"Tool: {tool_name} | Input: {', '.join(params)}"


def _generic_tool_result_preview(result_data: Any) -> str:
    """Format a generic tool_result preview."""
    if result_data is None:
        return "Result: (empty)"

    if isinstance(result_data, str):
        if not result_data.strip():
            return "Result: (empty)"
        return f"Result: {truncate_preview(result_data)}"

    if isinstance(result_data, dict):
        if result_data.get('isError') or result_data.get('is_error'):
            error_msg = result_data.get('content', 'Unknown error')
            return f"Result (error): {truncate_preview(str(error_msg))}"
        result_str = json.dumps(result_data, ensure_ascii=False)
        return f"Result: {truncate_preview(result_str)}"

    if isinstance(result_data, list):
        result_str = json.dumps(result_data, ensure_ascii=False)
        return f"Result: {truncate_preview(result_str)}"

    return f"Result: {truncate_preview(str(result_data))}"


# ---------------------------------------------------------------------------
# Edit formatter
# ---------------------------------------------------------------------------

def _format_edit_use(input_data: dict) -> str:
    file_path = input_data.get("file_path", "?")
    old_string = input_data.get("old_string", "")
    new_string = input_data.get("new_string", "")
    old_lines = len(old_string.splitlines()) if old_string else 0
    new_lines = len(new_string.splitlines()) if new_string else 0

    path_preview = truncate_path(file_path)
    if old_lines or new_lines:
        return f"Edit: {path_preview} ({old_lines} lines \u2192 {new_lines} lines)"
    return f"Edit: {path_preview}"


def _format_edit_result(result_data: Any) -> str:
    text = _extract_text(result_data)
    if text:
        # Look for file path in result text
        if "✓" in text or "success" in text.lower():
            return f"Edit \u2713 {truncate_preview(text, max_length=60)}"
        if "error" in text.lower() or "failed" in text.lower():
            return f"Edit \u2717 {truncate_preview(text, max_length=60)}"
    return f"Edit \u2713 done"


# ---------------------------------------------------------------------------
# Write formatter
# ---------------------------------------------------------------------------

def _format_write_use(input_data: dict) -> str:
    file_path = input_data.get("file_path", "?")
    content = input_data.get("content", "")
    n_lines = len(content.splitlines()) if content else 0
    path_preview = truncate_path(file_path)
    if n_lines:
        return f"Write: {path_preview} ({n_lines} lines)"
    return f"Write: {path_preview} (empty)"


def _format_write_result(result_data: Any) -> str:
    text = _extract_text(result_data)
    if text:
        return f"Write \u2713 {truncate_preview(text, max_length=60)}"
    return f"Write \u2713 done"


# ---------------------------------------------------------------------------
# Read formatter
# ---------------------------------------------------------------------------

def _format_read_use(input_data: dict) -> str:
    file_path = input_data.get("file_path", "?")
    path_preview = truncate_path(file_path)
    offset = input_data.get("offset")
    limit = input_data.get("limit")
    if offset is not None and limit is not None:
        end = offset + limit
        return f"Read: {path_preview}:{offset}-{end}"
    if offset is not None:
        return f"Read: {path_preview}:{offset}-"
    if limit is not None:
        return f"Read: {path_preview} ({limit} lines)"
    return f"Read: {path_preview}"


def _format_read_result(result_data: Any) -> str:
    text = _extract_text(result_data)
    if text:
        n_lines = len(text.splitlines())
        return f"Read \u2713 ({n_lines} lines)"
    return "Read \u2713 (empty)"


# ---------------------------------------------------------------------------
# Bash formatter
# ---------------------------------------------------------------------------

def _format_bash_use(input_data: dict) -> str:
    command = input_data.get("command", "")
    return f"Bash: {truncate_preview(command, max_length=50)}"


def _format_bash_result(result_data: Any) -> str:
    text = _extract_text(result_data)
    if text:
        n_lines = len(text.splitlines())
        return f"Bash \u2713 ({n_lines} lines output)"
    return "Bash \u2713 (no output)"


# ---------------------------------------------------------------------------
# Grep formatter
# ---------------------------------------------------------------------------

def _format_grep_use(input_data: dict) -> str:
    pattern = input_data.get("pattern", "?")
    path = input_data.get("path", ".")
    pattern_preview = truncate_preview(pattern, max_length=30)
    path_preview = truncate_path(path)
    return f"Grep: /{pattern_preview}/ in {path_preview}"


def _format_grep_result(result_data: Any) -> str:
    text = _extract_text(result_data)
    if text:
        n_lines = len(text.splitlines())
        return f"Grep \u2713 ({n_lines} matches)"
    return "Grep \u2713 (no matches)"


# ---------------------------------------------------------------------------
# Glob formatter
# ---------------------------------------------------------------------------

def _format_glob_use(input_data: dict) -> str:
    pattern = input_data.get("pattern", "?")
    path = input_data.get("path", ".")
    pattern_preview = truncate_preview(pattern, max_length=30)
    path_preview = truncate_path(path)
    return f"Glob: {pattern_preview} in {path_preview}"


def _format_glob_result(result_data: Any) -> str:
    text = _extract_text(result_data)
    if text:
        n_files = len(text.splitlines())
        return f"Glob \u2713 ({n_files} files)"
    return "Glob \u2713 (no files)"


# ---------------------------------------------------------------------------
# Helper: extract text from various result shapes
# ---------------------------------------------------------------------------

def _extract_text(result_data: Any) -> str:
    """Extract plain text content from a tool result.

    Tool results can arrive as a plain string, a dict with 'content' key,
    a list of content blocks, etc.
    """
    if result_data is None:
        return ""
    if isinstance(result_data, str):
        return result_data
    if isinstance(result_data, dict):
        # Error results
        if result_data.get("isError") or result_data.get("is_error"):
            return str(result_data.get("content", ""))
        # Content blocks list
        content = result_data.get("content")
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif isinstance(block, str):
                    texts.append(block)
            return "\n".join(texts)
        if isinstance(content, str):
            return content
        # Fall back to the whole dict as string
        return ""
    if isinstance(result_data, list):
        texts = []
        for block in result_data:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif isinstance(block, str):
                texts.append(block)
        return "\n".join(texts)
    return str(result_data)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Type alias for formatter functions
ToolUseFormatter = Callable[[dict], str]
ToolResultFormatter = Callable[[Any], str]

TOOL_FORMATTERS: Dict[str, Dict[str, Any]] = {
    "Edit": {"use": _format_edit_use, "result": _format_edit_result, "diff": "_edit"},
    "Write": {"use": _format_write_use, "result": _format_write_result, "diff": "_write"},
    "Read": {"use": _format_read_use, "result": _format_read_result},
    "Bash": {"use": _format_bash_use, "result": _format_bash_result},
    "Grep": {"use": _format_grep_use, "result": _format_grep_result},
    "Glob": {"use": _format_glob_use, "result": _format_glob_result},
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def format_tool_use_preview(tool_name: str, input_data: dict) -> str:
    """Format a tool_use event for human-readable preview.

    Routes to a per-tool formatter if registered, otherwise falls back
    to the generic formatter.

    Args:
        tool_name: Name of the tool (e.g. 'Edit', 'Bash')
        input_data: Tool input parameters dictionary

    Returns:
        Formatted preview string
    """
    entry = TOOL_FORMATTERS.get(tool_name)
    if entry and "use" in entry:
        try:
            return entry["use"](input_data or {})
        except Exception:
            logger.debug("Tool-specific formatter failed for %s, falling back", tool_name)
    return _generic_tool_use_preview(tool_name, input_data)


def format_tool_result_preview(tool_name: str, result_data: Any) -> str:
    """Format a tool_result event for human-readable preview.

    Routes to a per-tool formatter if registered, otherwise falls back
    to the generic formatter.

    Args:
        tool_name: Name of the tool (e.g. 'Edit', 'Bash')
        result_data: Tool result data

    Returns:
        Formatted preview string
    """
    entry = TOOL_FORMATTERS.get(tool_name)
    if entry and "result" in entry:
        try:
            return entry["result"](result_data)
        except Exception:
            logger.debug("Tool-specific result formatter failed for %s, falling back", tool_name)
    return _generic_tool_result_preview(result_data)


# ---------------------------------------------------------------------------
# Diff generation & rendering
# ---------------------------------------------------------------------------

def generate_edit_diff(old_string: str, new_string: str, file_path: str) -> list[str]:
    """Generate unified diff lines from Edit tool's old/new strings.

    Args:
        old_string: Original text being replaced
        new_string: Replacement text
        file_path: File path for diff header

    Returns:
        List of diff lines (empty if strings are identical)
    """
    if old_string == new_string:
        return []
    old_lines = old_string.splitlines(keepends=True)
    new_lines = new_string.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        n=3,
    )
    return [line.rstrip("\n") for line in diff]


def format_tool_diff(
    tool_name: str,
    input_data: dict,
    result_data: Any,
    old_content: Optional[str] = None,
) -> None:
    """Render diff output for Edit/Write tools. No-op for other tools.

    Args:
        tool_name: Tool name (e.g. 'Edit', 'Write')
        input_data: Cached tool input parameters
        result_data: Tool result data
        old_content: For Write overwrites, the original file content before write.
                     None means the file did not exist (new file).
    """
    from .display import render_diff

    entry = TOOL_FORMATTERS.get(tool_name)
    if not entry or "diff" not in entry:
        return

    try:
        if tool_name == "Edit":
            old_string = input_data.get("old_string", "")
            new_string = input_data.get("new_string", "")
            file_path = input_data.get("file_path", "?")
            diff_lines = generate_edit_diff(old_string, new_string, file_path)
            if diff_lines:
                render_diff(diff_lines, file_path)
        elif tool_name == "Write":
            file_path = input_data.get("file_path", "?")
            content = input_data.get("content", "")
            n_lines = len(content.splitlines()) if content else 0
            if old_content is not None:
                # Overwrite of existing file — generate unified diff
                diff_lines = generate_edit_diff(old_content, content, file_path)
                if diff_lines:
                    render_diff(diff_lines, file_path)
            else:
                # New file — show "Created" summary
                from .display import get_console
                get_console().print(
                    f"  [green]Created[/green] {file_path} ({n_lines} lines)"
                )
    except Exception:
        logger.debug("Diff rendering failed for %s", tool_name, exc_info=True)
