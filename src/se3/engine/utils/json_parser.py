"""Robust JSON parsing for LLM responses.

Handles common LLM output issues like markdown blocks, extra text,
trailing commas, and single quotes.
"""

import json
import logging
import re
from typing import Any, Dict, Optional, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Dict[str, Any])

def parse_json_response(response: str, required_keys: Optional[list[str]] = None) -> Optional[Dict[str, Any]]:
    """Parse JSON from LLM response with robust error recovery.

    Handles both single JSON responses and NDJSON (newline-delimited JSON)
    from stream-json format.

    Args:
        response: Raw LLM response string
        required_keys: Optional list of keys that must be present

    Returns:
        Parsed dictionary or None if parsing fails
    """
    if not response:
        return None

    # First, try NDJSON format (stream-json output)
    # Each line is a separate JSON object, we need to find the one with content
    ndjson_result = _parse_ndjson(response, required_keys)
    if ndjson_result:
        return ndjson_result

    # Fall back to single JSON parsing
    json_str = _extract_json_string(response)
    if not json_str:
        logger.warning("No JSON-like structure found in response")
        return None

    # Try standard parsing
    try:
        data = json.loads(json_str)
        if _validate_keys(data, required_keys):
            return data
        else:
            logger.warning(f"JSON parsed but missing required keys: {required_keys}")
    except json.JSONDecodeError:
        pass

    # If standard parsing fails, try to repair common LLM JSON mistakes
    repaired_json = _repair_json(json_str)
    try:
        data = json.loads(repaired_json)
        if _validate_keys(data, required_keys):
            logger.info("Successfully parsed JSON after repair")
            return data
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON even after repair: {e}")
        snippet = json_str[:200] + "..." if len(json_str) > 200 else json_str
        logger.debug(f"JSON snippet: {snippet}")

    return None


def _parse_ndjson(response: str, required_keys: Optional[list[str]] = None) -> Optional[Dict[str, Any]]:
    """Parse NDJSON (newline-delimited JSON) format.

    Stream-json format outputs one JSON object per line.
    We look for the line containing the actual response content.

    Args:
        response: Raw response potentially in NDJSON format
        required_keys: Keys that must be present in the result

    Returns:
        Parsed dict from the content line, or None if not NDJSON or no valid content found
    """
    lines = response.strip().split('\n')
    if len(lines) < 2:
        # Not NDJSON format (single line)
        return None

    # Try to parse each line as JSON
    for line in lines:
        line = line.strip()
        if not line:
            continue

        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                continue

            # Check for stream-json format indicators
            # Claude stream-json outputs objects with 'type' field
            # The actual content is usually in a 'content' or 'message' field
            # or we look for objects that have our required keys

            # If this line has the required keys, use it
            if _validate_keys(data, required_keys):
                logger.debug(f"Found valid JSON in NDJSON line with keys: {list(data.keys())}")
                return data

            # Also check nested 'content' or 'output' fields
            for key in ['content', 'output', 'message', 'result']:
                if key in data and isinstance(data[key], dict):
                    if _validate_keys(data[key], required_keys):
                        logger.debug(f"Found valid JSON in NDJSON line['{key}']")
                        return data[key]

        except json.JSONDecodeError:
            # This line isn't valid JSON, skip it
            continue

    return None

def _extract_json_string(text: str) -> Optional[str]:
    """Extract the JSON part of a string."""
    text = text.strip()

    # Handle markdown code blocks
    # Look for ```json ... ``` or just ``` ... ```
    md_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
    if md_match:
        return md_match.group(1).strip()

    # Find the first { and the last }
    start = text.find('{')
    end = text.rfind('}')

    if start != -1 and end != -1 and end > start:
        return text[start:end+1].strip()

    return None

def _repair_json(json_str: str) -> str:
    """Repair common JSON mistakes made by LLMs."""
    # Remove trailing commas before closing braces/brackets
    # e.g., {"a": 1,} -> {"a": 1}
    json_str = re.sub(r',\s*([}\]])', r'\1', json_str)

    # Replace 'key': with "key":
    json_str = re.sub(r"'([^']*)'\s*:", r'"\1":', json_str)
    
    # Replace : 'value' with : "value" (careful not to match inside already double-quoted strings)
    # This is a heuristic and might fail for complex strings, but helps for simple LLM errors
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
