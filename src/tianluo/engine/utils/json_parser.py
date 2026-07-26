"""Robust JSON parsing for LLM responses.

Handles stream-json (NDJSON), single JSON, markdown blocks, and common LLM errors.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Dict[str, Any])


def parse_json_response(response: str, required_keys: Optional[list[str]] = None) -> Optional[Dict[str, Any]]:
    """Parse JSON from LLM response.

    For stream-json format, collects all text content from assistant messages,
    then tries to extract and parse JSON from it.

    Args:
        response: Raw LLM response string (may contain multiple NDJSON lines)
        required_keys: Optional list of keys that must be present

    Returns:
        Parsed dictionary or None if parsing fails
    """
    if not response:
        return None

    # Handle stream-json (NDJSON) format: parse line by line
    lines = response.strip().split('\n')
    if len(lines) > 1:
        # Collect text content from stream-json
        text_parts = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict) and data.get('type') == 'assistant':
                    message = data.get('message', {})
                    content = message.get('content', [])
                    for item in content:
                        if isinstance(item, dict) and item.get('type') == 'text':
                            text = item.get('text', '')
                            if text:
                                text_parts.append(text)
            except json.JSONDecodeError:
                continue
        
        if text_parts:
            combined_text = ''.join(text_parts)
            # Try to extract and parse JSON from combined text
            result = _extract_and_parse_json(combined_text, required_keys)
            if result:
                return result
    
    # Single line response: try direct JSON parsing
    return _extract_and_parse_json(response.strip(), required_keys)


def _extract_and_parse_json(text: str, required_keys: Optional[list[str]] = None) -> Optional[Dict[str, Any]]:
    """Extract JSON from text and parse it.

    Args:
        text: Text that may contain JSON (in markdown block or raw)
        required_keys: Keys that must be present

    Returns:
        Parsed dict or None
    """
    # Extract JSON string from text
    json_str = _extract_json_string(text)
    if not json_str:
        return None

    result = _try_parse_with_repairs(json_str)
    if result is not None and _validate_keys(result, required_keys):
        return result

    return None


def _extract_json_string(text: str) -> Optional[str]:
    """Extract JSON string from text (handles markdown code blocks)."""
    text = text.strip()
    
    # Remove tool call/result preview lines injected by extract_assistant_text.
    # Old format: "[Tool Call: ...]", "[Tool Result: ...]"
    # New format: "[Edit: ...]", "[Write: ...]", "[Read: ...]", "[Bash: ...]",
    #   "[Grep: ...]", "[Glob: ...]", "[Tool: ...]" (generic),
    #   "[Edit ✓", "[Write ✓", "[Read ✓", "[Bash ✓", "[Grep ✓", "[Glob ✓",
    #   "[Result:", "[Result (error):"
    _TOOL_PREVIEW_PREFIXES = (
        # Old format (backward compat)
        '[Tool Call:', '[Tool Result:',
        # New tool_use previews
        '[Edit:', '[Write:', '[Read:', '[Bash:', '[Grep:', '[Glob:', '[Tool:',
        # New tool_result previews
        '[Edit \u2713', '[Write \u2713', '[Read \u2713', '[Bash \u2713',
        '[Grep \u2713', '[Glob \u2713',
        '[Edit \u2717', '[Write \u2717',
        '[Result:', '[Result (error):',
    )
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(_TOOL_PREVIEW_PREFIXES):
            cleaned_lines.append(line)
    text = '\n'.join(cleaned_lines)
    text = text.strip()
    
    # Handle markdown code blocks - look for ```json specifically
    # If found, try to parse it as JSON
    md_match = re.search(r'```json\s*([\s\S]*?)\s*```', text)
    if md_match:
        json_str = md_match.group(1).strip()
        try:
            json.loads(json_str)
            return json_str
        except json.JSONDecodeError:
            pass  # Not valid JSON, fall through
    
    # Also try generic code blocks, but verify it's valid JSON
    md_match = re.search(r'```\s*([\s\S]*?)\s*```', text)
    if md_match:
        json_str = md_match.group(1).strip()
        try:
            json.loads(json_str)
            return json_str
        except json.JSONDecodeError:
            pass  # Not valid JSON, fall through
    
    # Find first { and last }
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end+1].strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

        # First-to-last failed (e.g. mixed text between multiple JSON objects).
        # Try extracting the last complete JSON object from the end.
        trailing_result = _extract_trailing_json_string(text)
        if trailing_result is not None:
            return trailing_result[0]

        # Fall back to raw first-to-last (repair will attempt to fix it)
        return candidate

    # Handle truncated JSON: if we have an opening brace but no closing brace,
    # return from opening brace to end (will be repaired later)
    if start != -1 and end == -1:
        return text[start:].strip()

    return None


def _extract_trailing_json_string(text: str) -> Optional[tuple[str, int]]:
    """Extract the rightmost JSON object from text.

    Walks backwards from the last '}' (anywhere in the text — trailing
    non-JSON characters are tolerated) to find its matching '{',
    properly handling strings and escaped quotes.

    Backward escape detection: when a '"' is encountered, count the
    consecutive run of backslashes immediately before it. An odd run
    means the quote is escaped (e.g. `\\"`), an even run means it is a
    literal quote (e.g. `\\\\"` is a backslash followed by a quote).

    Returns:
        Tuple of (raw JSON string, start index in *text*) if found and
        parseable (via the lenient repair chain), else None.
    """
    end = text.rfind('}')
    if end == -1:
        return None

    depth = 0
    in_string = False
    for i in range(end, -1, -1):
        c = text[i]
        if c == '"':
            bs_count = 0
            j = i - 1
            while j >= 0 and text[j] == '\\':
                bs_count += 1
                j -= 1
            if bs_count % 2 == 1:
                continue  # escaped quote, ignore
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '}':
            depth += 1
        elif c == '{':
            depth -= 1
            if depth == 0:
                candidate = text[i:end + 1]
                if _try_parse_with_repairs(candidate) is not None:
                    return candidate, i
                return None
    return None


def _count_unescaped_quotes(s: str) -> int:
    """Count the number of unescaped double quotes in *s*."""
    count = 0
    i = 0
    while i < len(s):
        if s[i] == '\\':
            i += 2
            continue
        if s[i] == '"':
            count += 1
        i += 1
    return count


def _repair_unescaped_quotes(json_str: str) -> str:
    """Repair unescaped double quotes inside JSON string values.

    LLMs sometimes produce JSON with unescaped ASCII double quotes inside
    string values (e.g., Chinese-style quoting like 变为"继续").  This
    function uses the error position reported by json.loads to iteratively
    escape the offending quote and retry.

    Args:
        json_str: JSON string that may contain unescaped interior quotes.

    Returns:
        Repaired JSON string (may be unchanged if no repair was needed or
        possible).
    """
    MAX_REPAIRS = 50  # safety limit
    current = json_str
    for _ in range(MAX_REPAIRS):
        try:
            json.loads(current)
            return current  # valid now
        except json.JSONDecodeError as e:
            pos = e.pos
            if pos is None or pos <= 0 or pos >= len(current):
                return current

            # Strategy 1: The error position is directly at an unescaped quote
            quote_pos = None
            if current[pos] == '"' and (pos == 0 or current[pos - 1] != '\\'):
                quote_pos = pos
            else:
                # Strategy 2: "Expecting ',' delimiter" or similar — the parser
                # saw a quote that prematurely closed a string, then hit an
                # unexpected char.  Walk backwards to find the quote.
                search_pos = pos - 1
                while search_pos >= 0 and current[search_pos] in ' \t\r\n':
                    search_pos -= 1
                if search_pos >= 0 and current[search_pos] == '"' and \
                   (search_pos == 0 or current[search_pos - 1] != '\\'):
                    quote_pos = search_pos

            if quote_pos is not None:
                # Verify this quote is inside a string value by counting
                # unescaped quotes before it — odd count means mid-string.
                preceding = current[:quote_pos]
                n_quotes = _count_unescaped_quotes(preceding)
                if n_quotes % 2 == 1:
                    # Inside a string — escape this quote
                    current = current[:quote_pos] + '\\"' + current[quote_pos + 1:]
                    continue

                # Even count means this quote looks structural.  But if it
                # is followed immediately by another quote (e.g., 继续""),
                # the *previous* quote prematurely closed the string.
                # Try escaping the quote one position earlier.
                if quote_pos > 0 and current[quote_pos - 1] == '"' and \
                   (quote_pos < 2 or current[quote_pos - 2] != '\\'):
                    alt_pos = quote_pos - 1
                    n_before_alt = _count_unescaped_quotes(current[:alt_pos])
                    if n_before_alt % 2 == 1:
                        current = current[:alt_pos] + '\\"' + current[alt_pos + 1:]
                        continue

            # Cannot repair at this position
            return current
    return current


def _repair_json(json_str: str) -> str:
    """Repair common JSON mistakes made by LLMs."""
    # Remove trailing commas
    json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
    
    # Replace single quotes with double quotes
    json_str = re.sub(r"'([^']*)'\s*:", r'"\1":', json_str)
    json_str = re.sub(r':\s*\'([^\']*)\'(?=\s*[,}\]])', r': "\1"', json_str)
    json_str = re.sub(r',\s*\'([^\']*)\'(?=\s*[,}\]])', r', "\1"', json_str)
    json_str = re.sub(r'\[\s*\'([^\']*)\'(?=\s*[,}\]])', r'["\1"', json_str)
    
    # Handle truncated JSON by closing open structures
    json_str = _close_truncated_json(json_str)
    
    return json_str


def _close_truncated_json(json_str: str) -> str:
    """Attempt to close truncated JSON structures.
    
    When LLM output is truncated due to length limits, this function
    attempts to close open strings, objects, and arrays to make the
    JSON parseable, even if incomplete.
    
    Args:
        json_str: Potentially truncated JSON string
        
    Returns:
        JSON string with structures closed
    """
    result = json_str
    
    # Check if we're inside a string (odd number of unescaped quotes)
    # Remove escaped quotes first for accurate counting
    temp = result.replace('\\"', '')
    quote_count = temp.count('"')
    in_string = quote_count % 2 == 1
    
    # Track what we need to close
    needs_string_close = False
    
    # If we're inside a string that's been truncated, we need to handle it carefully
    if in_string:
        # Find the last unescaped quote to find where the string started
        last_quote_idx = -1
        for i, c in enumerate(result):
            if c == '"' and (i == 0 or result[i-1] != '\\'):
                last_quote_idx = i
        
        if last_quote_idx > 0:
            # Look at what comes before the last quote to understand context
            before_quote = result[:last_quote_idx].rstrip()
            
            # Check if this looks like a "content": " pattern (common in file changes)
            if before_quote.endswith(':'):
                # This is a key string, not a value - just close it
                result += '"'
            else:
                # This is a value string that got truncated
                # Try to find a safe truncation point (last newline)
                # and add ellipsis to indicate truncation
                current_content = result[last_quote_idx+1:]
                last_newline = current_content.rfind('\\n')
                if last_newline > 0:
                    # Truncate to last complete line and add ellipsis
                    result = result[:last_quote_idx+1+last_newline+2] + '..."'
                else:
                    # No newline found, just close the string
                    result += '"'
            needs_string_close = True
    
    # Remove trailing comma if present
    result = result.rstrip()
    if result.endswith(','):
        result = result[:-1]
    
    # Count open/close braces and brackets after string handling
    open_braces = result.count('{')
    close_braces = result.count('}')
    open_brackets = result.count('[')
    close_brackets = result.count(']')
    
    # If we closed a string that was inside a value (like content field),
    # we need to close the enclosing object first, then array, then root
    # Typical structure: { "files_changed": [{ "content": "..." }] }
    if needs_string_close:
        # We just closed a value string, so we need to close:
        # 1. The enclosing object (if any unclosed braces after the last bracket)
        # 2. The enclosing array (if any unclosed brackets)
        # 3. The root object (if any remaining unclosed braces)
        
        # Find positions of last [ and {
        last_bracket = result.rfind('[')
        last_brace = result.rfind('{')
        
        # Check if there's an unclosed object inside the array
        # (this would be the file object containing our string value)
        if last_brace > last_bracket and close_braces < open_braces:
            # Close the file object first
            result += '}'
            close_braces += 1
    
    # Close arrays
    while close_brackets < open_brackets:
        result += ']'
        close_brackets += 1
    
    # Close remaining objects (including root)
    while close_braces < open_braces:
        result += '}'
        close_braces += 1
    
    return result


def _validate_keys(data: Any, required_keys: Optional[list[str]]) -> bool:
    """Validate that required keys are present in data."""
    if not isinstance(data, dict):
        return False
    if not required_keys:
        return True
    return all(key in data for key in required_keys)


def _try_parse_with_repairs(text: str) -> Optional[Any]:
    """Try to parse text as JSON, applying the full repair chain.

    This helper is shared between `_extract_and_parse_json` (content
    extraction) and `looks_like_json_object` (narrative extraction) so
    that both paths use identical repair semantics and stay aligned.

    Args:
        text: Candidate JSON text.

    Returns:
        The parsed Python object (dict/list/scalar) on success,
        or None if all repair attempts fail.
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip()
    if not text:
        return None

    # Try strict parse first
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try _repair_json
    try:
        return json.loads(_repair_json(text))
    except (json.JSONDecodeError, ValueError):
        pass

    # Try _repair_unescaped_quotes
    quote_repaired = _repair_unescaped_quotes(text)
    if quote_repaired != text:
        try:
            return json.loads(quote_repaired)
        except (json.JSONDecodeError, ValueError):
            pass

        # Try combining both repairs
        try:
            return json.loads(_repair_json(quote_repaired))
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def looks_like_json_object(text: str) -> bool:
    """Leniently determine if text is parseable as a JSON object (dict).

    Uses the same repair chain as `parse_json_response` / `_extract_and_parse_json`
    so that narrative extraction (strict JSON check) and content extraction
    (lenient JSON check) stay semantically aligned.

    Narrowed to dict-only (not list) to match `parse_json_response`, which
    only returns dicts because `_validate_keys` requires `isinstance(data, dict)`.

    Args:
        text: Candidate JSON text.

    Returns:
        True if the text can be parsed as a dict after the full repair chain,
        False otherwise.
    """
    result = _try_parse_with_repairs(text)
    return result is not None and isinstance(result, dict)


def looks_like_json(text: str) -> bool:
    """Leniently determine if text is parseable as any valid JSON value.

    Uses the full repair chain via `_try_parse_with_repairs` for all
    JSON value types (objects, arrays, scalars), with a strict-parse
    fallback only for the literal `null` value, which `_try_parse_with_repairs`
    returns as Python `None` — indistinguishable from parse failure.

    Args:
        text: Candidate JSON text.

    Returns:
        True if the text can be parseable as any valid JSON value
        (object, array, string, number, boolean, or null) — including
        composite values that need the lenient repair chain
        (e.g. arrays with trailing commas or unescaped quotes).
    """
    if not text or not isinstance(text, str):
        return False

    text = text.strip()
    if not text:
        return False

    # Use the lenient repair chain for all values — this also handles
    # arrays needing repair (trailing commas, unescaped quotes) so the
    # asymmetry between dicts and arrays is closed.
    if _try_parse_with_repairs(text) is not None:
        return True

    # `_try_parse_with_repairs` returns Python `None` for both parse failure
    # and valid JSON `null`. Distinguish via strict parse for the `null`
    # value (which never needs the repair chain).
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, ValueError, TypeError):
        return False
