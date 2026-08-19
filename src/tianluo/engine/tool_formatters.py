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
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .truncation import TOOL_DETAIL_PAYLOAD_MAX_CHARS

logger = logging.getLogger(__name__)

__all__ = [
    "TOOL_FORMATTERS",
    "build_tool_detail_payload",
    "build_tool_in_flight_detail_payload",
    "format_tool_chip_header",
    "format_tool_chip_in_flight_header",
    "format_tool_diff",
    "format_tool_result_preview",
    "format_tool_use_preview",
    "generate_edit_diff",
    "get_project_root",
    "set_project_root",
    "truncate_path",
    "truncate_preview",
]


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
            # WHY: unregistered file tools (codex's "Delete", for one) carry only
            # a file_path; a blind mid-string truncation would cut the filename
            # off, so paths get path-aware shortening that always keeps the tail.
            if key == "file_path":
                val_preview = truncate_path(value)
            else:
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
    path_preview = truncate_path(file_path)
    # WHY: key absence and an empty value mean different things. Some upstreams
    # (e.g. codex file_change items) report *that* a file changed without ever
    # carrying its text, and omit the keys entirely — there is no line count to
    # show, so only the path is rendered. A present-but-empty value is a real
    # "edited to nothing" and keeps its existing rendering.
    if "old_string" not in input_data and "new_string" not in input_data:
        return f"Edit: {path_preview}"

    old_string = input_data.get("old_string") or ""
    new_string = input_data.get("new_string") or ""
    old_lines = len(old_string.splitlines()) if old_string else 0
    new_lines = len(new_string.splitlines()) if new_string else 0

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
    path_preview = truncate_path(file_path)
    # WHY: see _format_edit_use — a missing "content" key means the upstream has
    # no content information at all, so "(empty)" would be a false claim about
    # the file; a present-but-empty content really is an empty file.
    if "content" not in input_data:
        return f"Write: {path_preview}"

    content = input_data.get("content") or ""
    n_lines = len(content.splitlines()) if content else 0
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


# ---------------------------------------------------------------------------
# Chip header — single-chip in-flight / success / failure
# ---------------------------------------------------------------------------

def _generic_use_body(tool_name: str, use_input: dict) -> str:
    """Return the generic ``k=v, ...`` summary with the framing stripped.

    ``_generic_tool_use_preview`` frames its summary as
    ``Tool: <name> | Input: <body>`` because that string also feeds the CLI's
    ``🔧`` stdout preview and the chat_history text transcript. Chips draw the
    tool name in their own span, so only ``<body>`` belongs in the header.
    Unlike :func:`_failure_use_body` this peels the generic framing FIRST, so a
    tool literally named ``Tool`` does not lose half its framing to the
    ``"<name>: "`` prefix test.
    """
    full = format_tool_use_preview(tool_name, use_input or {})
    generic_prefix = f"Tool: {tool_name} | Input: "
    if full.startswith(generic_prefix):
        return full[len(generic_prefix):]
    prefix = f"{tool_name}: "
    if full.startswith(prefix):
        return full[len(prefix):]
    return full


def format_tool_chip_in_flight_header(tool_name: str, use_input: dict) -> str:
    """Return the chip header string for an in-flight tool call.

    Registered tools delegate to the ``_format_*_use`` registry, whose output
    already leads with the real tool name (``Read: path:0-200``). Unregistered
    tools — claude's ``Agent`` / ``Skill`` / ``ToolSearch``, codex's synthesized
    ``mcp__<server>__<tool>`` / ``unknown`` — would otherwise fall through to
    the generic ``Tool: <name> | Input: …`` framing, whose leading token is the
    literal word ``Tool`` rather than the tool's name; they get
    ``<tool_name>: <k=v summary>`` instead.

    WHY: both chip grammars — this in-flight one and the terminal
    ``<tool_name> ✓/✗ …`` from :func:`format_tool_chip_header` — MUST start
    with the real tool name, because the frontend parses the chip name
    generically as the first token inside the bracket rather than against a
    name whitelist. When the two grammars disagreed, the terminal fragment for
    an unregistered tool parsed as a different tool than its in-flight
    fragment, and upgrading the chip blanked its header.

    The returned string is the chip header *only* — it does NOT carry
    surrounding ``[`` / ``]`` brackets, since the chip frame is drawn by the
    frontend.
    """
    entry = TOOL_FORMATTERS.get(tool_name)
    if entry and "use" in entry:
        return format_tool_use_preview(tool_name, use_input or {})
    body = _generic_use_body(tool_name, use_input or {})
    return f"{tool_name}: {body}" if body else f"{tool_name}: (none)"


def _error_preview(result_data: Any, max_length: int = 80) -> str:
    """Extract a short error description from a tool_result payload."""
    text = _extract_text(result_data) or ""
    if not text and isinstance(result_data, dict):
        text = str(result_data.get("error") or result_data.get("message") or "")
    return truncate_preview(text, max_length=max_length)


def _success_combined_edit(use_input: dict, result_data: Any) -> str:
    file_path = use_input.get("file_path", "?")
    # WHY: keys absent = upstream carries no text (see _format_edit_use); showing
    # "0 lines → 0 lines" there would assert a line count nobody measured.
    if "old_string" not in use_input and "new_string" not in use_input:
        return truncate_path(file_path)
    old_lines = len((use_input.get("old_string") or "").splitlines())
    new_lines = len((use_input.get("new_string") or "").splitlines())
    return f"{truncate_path(file_path)} ({old_lines} lines → {new_lines} lines)"


def _success_combined_write(use_input: dict, result_data: Any) -> str:
    file_path = use_input.get("file_path", "?")
    if "content" not in use_input:
        return truncate_path(file_path)
    n_lines = len((use_input.get("content") or "").splitlines())
    return f"{truncate_path(file_path)} ({n_lines} lines)"


def _success_combined_read(use_input: dict, result_data: Any) -> str:
    file_path = use_input.get("file_path", "?")
    text = _extract_text(result_data)
    n_lines = len(text.splitlines()) if text else 0
    offset = use_input.get("offset")
    limit = use_input.get("limit")
    range_part = ""
    if offset is not None and limit is not None:
        range_part = f":{offset}-{offset + limit}"
    elif offset is not None:
        range_part = f":{offset}-"
    return f"{truncate_path(file_path)}{range_part} · {n_lines} lines"


def _success_combined_bash(use_input: dict, result_data: Any) -> str:
    command = truncate_preview(use_input.get("command", ""), max_length=50)
    text = _extract_text(result_data)
    n_lines = len(text.splitlines()) if text else 0
    return f"{command} · {n_lines} lines output"


def _success_combined_grep(use_input: dict, result_data: Any) -> str:
    pattern = truncate_preview(use_input.get("pattern", "?"), max_length=30)
    path = truncate_path(use_input.get("path", "."))
    text = _extract_text(result_data)
    n = len(text.splitlines()) if text else 0
    return f"/{pattern}/ in {path} · {n} matches"


def _success_combined_glob(use_input: dict, result_data: Any) -> str:
    pattern = truncate_preview(use_input.get("pattern", "?"), max_length=30)
    path = truncate_path(use_input.get("path", "."))
    text = _extract_text(result_data)
    n = len(text.splitlines()) if text else 0
    return f"{pattern} in {path} · {n} files"


_SUCCESS_COMBINED: Dict[str, Callable[[dict, Any], str]] = {
    "Edit": _success_combined_edit,
    "Write": _success_combined_write,
    "Read": _success_combined_read,
    "Bash": _success_combined_bash,
    "Grep": _success_combined_grep,
    "Glob": _success_combined_glob,
}


def _failure_use_body(tool_name: str, use_input: dict) -> str:
    """Return the use-side summary body for a failure-state chip (no prefix)."""
    full = format_tool_use_preview(tool_name, use_input or {})
    prefix = f"{tool_name}: "
    if full.startswith(prefix):
        return full[len(prefix):]
    # Generic fallback: strip "Tool: <name> | Input: " framing
    generic_prefix = f"Tool: {tool_name} | Input: "
    if full.startswith(generic_prefix):
        return full[len(generic_prefix):]
    return full


def format_tool_chip_header(
    tool_name: str,
    use_input: Optional[dict],
    result_data: Any,
    is_error: bool,
) -> str:
    """Return the merged chip header for a settled tool call.

    Success path produces ``"<tool> ✓ <input-summary> · <result-summary>"``
    (e.g. ``"Read ✓ src/app.py:0-200 · 87 lines"``); failure path
    produces ``"<tool> ✗ <input-summary> · <error-preview>"`` (e.g.
    ``"Read ✗ src/missing.py · ENOENT: no such file"``).

    The returned string is the chip header *only* — no surrounding
    ``[`` / ``]`` brackets are added.
    """
    use_input = use_input or {}
    if is_error:
        body = _failure_use_body(tool_name, use_input)
        err = _error_preview(result_data)
        if body and err:
            return f"{tool_name} ✗ {body} · {err}"
        if body:
            return f"{tool_name} ✗ {body}"
        if err:
            return f"{tool_name} ✗ {err}"
        return f"{tool_name} ✗"
    combiner = _SUCCESS_COMBINED.get(tool_name)
    if combiner is not None:
        try:
            body = combiner(use_input, result_data)
        except Exception:
            logger.debug("Chip header combiner failed for %s", tool_name, exc_info=True)
            body = _failure_use_body(tool_name, use_input)
    else:
        # Unregistered tool — use generic body + result summary
        body_use = _failure_use_body(tool_name, use_input)
        result_summary = truncate_preview(_extract_text(result_data) or "", max_length=60)
        body = f"{body_use} · {result_summary}" if body_use and result_summary else (body_use or result_summary)
    return f"{tool_name} ✓ {body}" if body else f"{tool_name} ✓"


# ---------------------------------------------------------------------------
# Structured detail payload — feeds the web chip's collapsible panel
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _parse_first_hunk_start(diff_lines: list[str]) -> Tuple[Optional[int], Optional[int]]:
    for line in diff_lines:
        m = _HUNK_RE.match(line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None, None


def _maybe_truncate_text(
    text: str, max_chars: int = TOOL_DETAIL_PAYLOAD_MAX_CHARS
) -> Tuple[str, bool]:
    if not text:
        return text or "", False
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _maybe_truncate_lines(
    lines: list[str], max_items: int = 1000
) -> Tuple[list[str], bool]:
    if len(lines) <= max_items:
        return lines, False
    return lines[:max_items], True


def _json_safe_value(value: Any) -> Any:
    """Coerce one tool-input value into something ``json.dumps`` can encode.

    WHY: the detail payload is written verbatim into the step jsonl and pushed
    to the browser, so a value the stream carried but json cannot encode would
    poison the whole record rather than just its own key. Falling back to
    ``str(value)`` keeps the key visible instead of losing the record.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
    return value


def _build_input_payload(use_input: Optional[dict]) -> Tuple[Dict[str, Any], bool]:
    """Return the tool's own input, JSON-safe and per-value tail-truncated.

    Unlike the 60-char chip-header summary, this keeps every key and the FULL
    value up to ``TOOL_DETAIL_PAYLOAD_MAX_CHARS`` — the panel exists precisely
    so a Bash ``command`` or an Agent ``prompt`` can be read in full. Returns
    ``(payload, truncated)`` where ``truncated`` is True if any string value
    was cut.
    """
    if not isinstance(use_input, dict):
        return {}, False
    payload: Dict[str, Any] = {}
    truncated_any = False
    for key, value in use_input.items():
        name = key if isinstance(key, str) else str(key)
        if isinstance(value, str):
            text, truncated = _maybe_truncate_text(value)
            payload[name] = text
            truncated_any = truncated_any or truncated
        else:
            payload[name] = _json_safe_value(value)
    return payload, truncated_any


def build_tool_in_flight_detail_payload(
    tool_name: str, use_input: Optional[dict]
) -> dict:
    """Build the detail payload for an *in-flight* (result-pending) tool call.

    Returns ``{"kind": "tool_input", "tool_name": …, "input": {…},
    "truncated": bool}``. The frontend's ``tool_input`` renderer draws it as a
    ``$ command`` line for Bash and as a key/value list otherwise, so a running
    call can be expanded to read its full arguments instead of only the
    truncated chip header.

    WHY a separate ``kind`` rather than reusing ``text``: the terminal payload
    keeps its own discriminator table, and adding a new kind leaves every
    registered tool's settled payload structure untouched.

    INVARIANT: emitting this payload does NOT make a record terminal. Both the
    backend (``llm_caller._emit_progress``) and the frontend distinguish
    in-flight from settled solely by ``is_error`` being absent — never by
    ``tool_detail`` being empty.
    """
    payload, truncated = _build_input_payload(use_input)
    return {
        "kind": "tool_input",
        "tool_name": tool_name,
        "input": payload,
        "truncated": truncated,
    }


def _build_file_path_only_detail(file_path: str) -> dict:
    """Detail payload for "a file changed, but no text was reported".

    WHY: synthesising a diff from an absent key would claim something nobody
    measured — with the post-write file on disk as the old side, an absent
    ``content`` renders as *every line deleted*, i.e. the exact opposite of
    what happened. Carrying only the path lets the renderer say "no content
    information" instead of inventing one.
    """
    return {
        "kind": "file_path_only",
        "file_path": file_path,
        "truncated": False,
    }


def _build_edit_detail(use_input: dict, result_data: Any, old_content: Optional[str]) -> dict:
    file_path = use_input.get("file_path", "?")
    # WHY: key absence and an empty value differ (see _format_edit_use); both
    # keys absent means the upstream carries no text at all, so there is no
    # diff to show. A present-but-empty value is a real "edited to nothing".
    if "old_string" not in use_input and "new_string" not in use_input:
        return _build_file_path_only_detail(file_path)

    old_string = use_input.get("old_string", "")
    new_string = use_input.get("new_string", "")
    diff_lines = generate_edit_diff(old_string, new_string, file_path)
    diff_text = "\n".join(diff_lines)
    old_start, new_start = _parse_first_hunk_start(diff_lines)
    truncated_text, truncated = _maybe_truncate_text(diff_text)
    return {
        "kind": "edit_diff",
        "file_path": file_path,
        "diff": truncated_text,
        "old_start_line": old_start,
        "new_start_line": new_start,
        "truncated": truncated,
    }


def _build_write_detail(use_input: dict, result_data: Any, old_content: Optional[str]) -> dict:
    file_path = use_input.get("file_path", "?")
    # WHY: same rule as _build_edit_detail — an absent "content" key carries no
    # file text, and the tracker's old-content snapshot is taken *after* the
    # upstream already wrote the file, so diffing against "" would render the
    # freshly created file as fully deleted.
    if "content" not in use_input:
        return _build_file_path_only_detail(file_path)

    content = use_input.get("content", "")
    if old_content is None:
        truncated_text, truncated = _maybe_truncate_text(content)
        return {
            "kind": "write_full",
            "file_path": file_path,
            "content": truncated_text,
            "start_line": 1,
            "truncated": truncated,
        }
    diff_lines = generate_edit_diff(old_content, content, file_path)
    diff_text = "\n".join(diff_lines)
    old_start, new_start = _parse_first_hunk_start(diff_lines)
    truncated_text, truncated = _maybe_truncate_text(diff_text)
    return {
        "kind": "write_diff",
        "file_path": file_path,
        "diff": truncated_text,
        "old_start_line": old_start,
        "new_start_line": new_start,
        "truncated": truncated,
    }


def _build_read_detail(use_input: dict, result_data: Any, old_content: Optional[str]) -> dict:
    file_path = use_input.get("file_path", "?")
    offset = use_input.get("offset") or 0
    try:
        offset_int = int(offset)
    except (TypeError, ValueError):
        offset_int = 0
    text = _extract_text(result_data) or ""
    truncated_text, truncated = _maybe_truncate_text(text)
    return {
        "kind": "read_text",
        "file_path": file_path,
        "text": truncated_text,
        "start_line": offset_int + 1 if offset_int else 1,
        "truncated": truncated,
    }


def _build_bash_detail(use_input: dict, result_data: Any, old_content: Optional[str]) -> dict:
    command = use_input.get("command", "")
    # Claude CLI bash tool returns combined output in result_data text content.
    # If the result is a dict with explicit stdout/stderr fields, prefer those.
    stdout = ""
    stderr = ""
    if isinstance(result_data, dict) and (
        "stdout" in result_data or "stderr" in result_data
    ):
        stdout = str(result_data.get("stdout", "") or "")
        stderr = str(result_data.get("stderr", "") or "")
    else:
        stdout = _extract_text(result_data) or ""
    truncated_stdout, t1 = _maybe_truncate_text(stdout)
    truncated_stderr, t2 = _maybe_truncate_text(stderr)
    return {
        "kind": "bash_output",
        "command": command,
        "stdout": truncated_stdout,
        "stderr": truncated_stderr,
        "truncated": t1 or t2,
    }


def _build_grep_detail(use_input: dict, result_data: Any, old_content: Optional[str]) -> dict:
    text = _extract_text(result_data) or ""
    matches = text.splitlines() if text else []
    matches, items_truncated = _maybe_truncate_lines(matches)
    # Also guard against an individual match line being absurdly long
    joined_truncated = sum(len(m) for m in matches) > TOOL_DETAIL_PAYLOAD_MAX_CHARS
    return {
        "kind": "grep_matches",
        "pattern": use_input.get("pattern", ""),
        "path": use_input.get("path", "."),
        "matches": matches,
        "truncated": items_truncated or joined_truncated,
    }


def _build_glob_detail(use_input: dict, result_data: Any, old_content: Optional[str]) -> dict:
    text = _extract_text(result_data) or ""
    files = text.splitlines() if text else []
    files, items_truncated = _maybe_truncate_lines(files)
    return {
        "kind": "glob_matches",
        "pattern": use_input.get("pattern", ""),
        "path": use_input.get("path", "."),
        "files": files,
        "truncated": items_truncated,
    }


def _build_generic_text_detail(use_input: dict, result_data: Any, old_content: Optional[str]) -> dict:
    """Settled payload for a tool with no registered detail builder.

    Carries the call's own ``input`` alongside the result text: an unregistered
    tool (claude's ``Agent`` / ``Skill``, codex's ``mcp__<server>__<tool>``) has
    no per-tool renderer to reconstruct its arguments from, so dropping the
    input would make the completed chip strictly less informative than the
    in-flight one it replaced.
    """
    text = _extract_text(result_data) or ""
    truncated_text, truncated = _maybe_truncate_text(text)
    input_payload, input_truncated = _build_input_payload(use_input)
    return {
        "kind": "text",
        "text": truncated_text,
        "input": input_payload,
        "truncated": truncated or input_truncated,
    }


_DETAIL_BUILDERS: Dict[str, Callable[[dict, Any, Optional[str]], dict]] = {
    "Edit": _build_edit_detail,
    "Write": _build_write_detail,
    "Read": _build_read_detail,
    "Bash": _build_bash_detail,
    "Grep": _build_grep_detail,
    "Glob": _build_glob_detail,
}


def build_tool_detail_payload(
    tool_name: str,
    use_input: Optional[dict],
    result_data: Any,
    old_content: Optional[str] = None,
) -> dict:
    """Build a JSON-safe structured detail dict for the web chip's panel.

    The returned dict always carries a ``kind`` discriminator drawn from:
    ``edit_diff`` / ``write_full`` / ``write_diff`` / ``file_path_only`` /
    ``read_text`` / ``bash_output`` / ``grep_matches`` / ``glob_matches`` /
    ``text``.

    ``file_path_only`` is the Edit/Write payload for an upstream that reports
    *that* a file changed without carrying its text (codex ``file_change``
    items omit the ``content`` / ``old_string`` / ``new_string`` keys entirely).

    Per-tool builders return a richer shape than the 60-char preview so the
    frontend can render diffs with line numbers, full file content, command
    output, or match lists without re-running the tool. Oversize bodies are
    tail-truncated to ``TOOL_DETAIL_PAYLOAD_MAX_CHARS`` and flagged via
    ``truncated: True``.

    Unregistered tools fall back to ``kind="text"`` carrying the extracted
    result text **plus** the call's own ``input`` dict, so a settled Agent /
    MCP chip shows what was asked as well as what came back. The in-flight
    counterpart is :func:`build_tool_in_flight_detail_payload`.
    """
    use_input = use_input or {}
    builder = _DETAIL_BUILDERS.get(tool_name)
    if builder is None:
        return _build_generic_text_detail(use_input, result_data, old_content)
    try:
        return builder(use_input, result_data, old_content)
    except Exception:
        logger.debug("Detail builder failed for %s", tool_name, exc_info=True)
        return _build_generic_text_detail(use_input, result_data, old_content)


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
        # WHY: keys absent = the upstream never reported the file text (codex
        # file_change items). There is nothing to diff, and old_content here is
        # the *post-write* file, so diffing would print the new file as fully
        # deleted — and the "Created … (N lines)" fallback would print a line
        # count nobody measured. Rendering nothing is the only truthful option.
        if tool_name == "Write" and "content" not in input_data:
            return
        if (
            tool_name == "Edit"
            and "old_string" not in input_data
            and "new_string" not in input_data
        ):
            return

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
