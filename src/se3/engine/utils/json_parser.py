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
    
    # Try to parse
    try:
        data = json.loads(json_str)
        if _validate_keys(data, required_keys):
            return data
    except json.JSONDecodeError:
        pass
    
    # Try repair
    repaired = _repair_json(json_str)
    try:
        data = json.loads(repaired)
        if _validate_keys(data, required_keys):
            logger.debug("Successfully parsed JSON after repair")
            return data
    except json.JSONDecodeError:
        pass
    
    return None


def _extract_json_string(text: str) -> Optional[str]:
    """Extract JSON string from text (handles markdown code blocks)."""
    text = text.strip()
    
    # Remove tool call preview lines (e.g., "[Tool Call: Read]")
    # These are added by extract_assistant_text when LLM uses tools
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith('[Tool Call:') and not stripped.startswith('[Tool Result:'):
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
        trailing = _extract_trailing_json_string(text)
        if trailing is not None:
            return trailing

        # Fall back to raw first-to-last (repair will attempt to fix it)
        return candidate

    # Handle truncated JSON: if we have an opening brace but no closing brace,
    # return from opening brace to end (will be repaired later)
    if start != -1 and end == -1:
        return text[start:].strip()

    return None


def _extract_trailing_json_string(text: str) -> Optional[str]:
    """Extract the last complete JSON object from the end of text.

    Walks backwards from the final '}' to find its matching '{',
    properly handling strings (so braces inside quotes are ignored).

    Returns:
        Raw JSON string if found and valid, else None.
    """
    text = text.rstrip()
    if not text.endswith('}'):
        return None

    depth = 0
    in_string = False
    escape_next = False
    for i in range(len(text) - 1, -1, -1):
        c = text[i]
        if escape_next:
            escape_next = False
            continue
        # When scanning backwards, a backslash before the current char
        # means the *previous* char (at i+1) was escaped.  But since we
        # already processed i+1, we check if the char at i is a backslash
        # to mark that i-1 should skip.
        if in_string and c == '\\':
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
        if in_string:
            continue
        if c == '}':
            depth += 1
        elif c == '{':
            depth -= 1
            if depth == 0:
                candidate = text[i:]
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError:
                    return None
    return None


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
