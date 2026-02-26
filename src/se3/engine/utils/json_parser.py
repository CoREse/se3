"""Robust JSON parsing for LLM responses.

Handles single JSON, NDJSON (stream-json), markdown blocks, and common LLM errors.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Dict[str, Any])


def parse_json_response(response: str, required_keys: Optional[list[str]] = None) -> Optional[Dict[str, Any]]:
    """Parse JSON from LLM response with robust error recovery.

    Handles:
    - Single JSON responses
    - NDJSON/stream-json format (multiple JSON lines)
    - Markdown code blocks
    - Common LLM JSON errors

    Args:
        response: Raw LLM response string
        required_keys: Optional list of keys that must be present

    Returns:
        Parsed dictionary or None if parsing fails
    """
    if not response:
        return None

    # First try to extract JSON from stream-json format
    stream_result = _extract_from_stream_json(response)
    if stream_result:
        # Try to parse the extracted content as JSON
        parsed = _try_parse_json(stream_result, required_keys)
        if parsed:
            return parsed
        # If extracted content isn't valid JSON, try the original response

    # Try NDJSON parsing (for stream-json format)
    ndjson_result = _parse_ndjson(response, required_keys)
    if ndjson_result:
        return ndjson_result

    # Fall back to single JSON parsing
    json_str = _extract_json_string(response)
    if not json_str:
        logger.warning("No JSON-like structure found in response")
        return None

    return _try_parse_json(json_str, required_keys)


def _extract_from_stream_json(response: str) -> Optional[str]:
    """Extract text content from Claude stream-json format.

    Stream-json outputs lines like:
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "..."}}

    Args:
        response: Raw stream-json response

    Returns:
        Extracted text content or None if not stream format
    """
    lines = response.strip().split('\n')
    text_parts = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                continue

            # Extract text from content_block_delta
            if data.get('type') == 'content_block_delta':
                delta = data.get('delta', {})
                if delta.get('type') == 'text_delta':
                    text = delta.get('text', '')
                    if text:
                        text_parts.append(text)

        except json.JSONDecodeError:
            continue

    if text_parts:
        return ''.join(text_parts)

    return None


def _parse_ndjson(response: str, required_keys: Optional[list[str]] = None) -> Optional[Dict[str, Any]]:
    """Parse NDJSON (newline-delimited JSON) format.

    Tries to find a JSON object matching required keys from the stream.

    Args:
        response: Raw response potentially in NDJSON format
        required_keys: Keys that must be present in the result

    Returns:
        Parsed dict or None if not valid NDJSON or no matching content
    """
    lines = response.strip().split('\n')
    if len(lines) < 2:
        return None

    # Collect all potential JSON objects from the stream
    candidates = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                continue
            candidates.append(data)
        except json.JSONDecodeError:
            continue

    if not candidates:
        return None

    # First try: look for objects with required keys
    if required_keys:
        for candidate in candidates:
            if all(key in candidate for key in required_keys):
                return candidate

    # Second try: look for objects in common nested fields
    for candidate in candidates:
        for key in ['content', 'output', 'message', 'result', 'response']:
            if key in candidate and isinstance(candidate[key], dict):
                nested = candidate[key]
                if not required_keys or all(k in nested for k in required_keys):
                    return nested

    # Third try: return the largest object (most content)
    largest = max(candidates, key=lambda x: len(str(x)))
    if not required_keys or _validate_keys(largest, required_keys):
        return largest

    return None


def _extract_json_string(text: str) -> Optional[str]:
    """Extract the JSON part of a string."""
    text = text.strip()

    # Handle markdown code blocks
    md_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
    if md_match:
        return md_match.group(1).strip()

    # Find the first { and the last }
    start = text.find('{')
    end = text.rfind('}')

    if start != -1 and end != -1 and end > start:
        return text[start:end+1].strip()

    return None


def _try_parse_json(json_str: str, required_keys: Optional[list[str]] = None) -> Optional[Dict[str, Any]]:
    """Try to parse JSON string with repair fallback.

    Args:
        json_str: JSON string to parse
        required_keys: Required keys in result

    Returns:
        Parsed dict or None
    """
    # Try standard parsing first
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
            logger.info("Successfully parsed JSON after repair")
            return data
    except json.JSONDecodeError:
        pass

    return None


def _repair_json(json_str: str) -> str:
    """Repair common JSON mistakes made by LLMs."""
    # Remove trailing commas before closing braces/brackets
    json_str = re.sub(r',\s*([}\]])', r'\1', json_str)

    # Replace 'key': with "key":
    json_str = re.sub(r"'([^']*)'\s*:", r'"\1":', json_str)

    # Replace : 'value' with : "value"
    json_str = re.sub(r':\s*\'([^\']*)\'(?=\s*[,}\]])', r': "\1"', json_str)

    # Replace , 'value' with , "value"
    json_str = re.sub(r',\s*\'([^\']*)\'(?=\s*[,}\]])', r', "\1"', json_str)

    # Replace [ 'value' with [ "value"
    json_str = re.sub(r'\[\s*\'([^\']*)\'(?=\s*[,}\]])', r'["\1"', json_str)

    return json_str


def _validate_keys(data: Any, required_keys: Optional[list[str]]) -> bool:
    """Validate that required keys are present in data."""
    if not isinstance(data, dict):
        return False
    if not required_keys:
        return True
    return all(key in data for key in required_keys)
