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
    
    # Handle markdown code blocks
    md_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
    if md_match:
        return md_match.group(1).strip()
    
    # Find first { and last }
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        return text[start:end+1].strip()
    
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
    
    return json_str


def _validate_keys(data: Any, required_keys: Optional[list[str]]) -> bool:
    """Validate that required keys are present in data."""
    if not isinstance(data, dict):
        return False
    if not required_keys:
        return True
    return all(key in data for key in required_keys)
