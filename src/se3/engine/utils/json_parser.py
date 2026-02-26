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

    Args:
        response: Raw LLM response string
        required_keys: Optional list of keys that must be present

    Returns:
        Parsed dictionary or None if parsing fails
    """
    if not response:
        return None

    # 1. Try to find JSON block using regex for better extraction
    # This handles extra text before/after and markdown blocks
    json_str = _extract_json_string(response)
    if not json_str:
        logger.warning("No JSON-like structure found in response")
        return None

    # 2. Try standard parsing first
    try:
        data = json.loads(json_str)
        if _validate_keys(data, required_keys):
            return data
        else:
            logger.warning(f"JSON parsed but missing required keys: {required_keys}")
    except json.JSONDecodeError:
        pass

    # 3. If standard parsing fails, try to repair common LLM JSON mistakes
    repaired_json = _repair_json(json_str)
    try:
        data = json.loads(repaired_json)
        if _validate_keys(data, required_keys):
            logger.info("Successfully parsed JSON after repair")
            return data
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON even after repair: {e}")
        # Log a snippet of the failed JSON for debugging
        snippet = json_str[:200] + "..." if len(json_str) > 200 else json_str
        logger.debug(f"JSON snippet: {snippet}")

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
