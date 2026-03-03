"""Two-phase JSON extraction from LLM output.

Provides reliable JSON extraction by using a second LLM call to parse
and structure the output, avoiding prompt pollution in the primary call.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Lightweight extraction prompt - minimal, clear instructions
EXTRACTION_PROMPT = """Extract valid JSON from the following content.

Rules:
1. Return ONLY the JSON object/array, no markdown, no explanations
2. If content contains markdown code blocks (```json), extract from them
3. If content is truncated or incomplete, complete it reasonably
4. Ensure valid JSON syntax: double quotes, no trailing commas
5. Preserve all data faithfully, don't modify values

Content to extract from:
---
{content}
---

{schema_hint}

Respond with valid JSON only:"""


class JSONExtractor:
    """Extracts JSON from raw LLM output using a lightweight second pass."""

    def __init__(
        self,
        project_root: Optional[Any] = None,
        timeout: int = 60,
        max_content_length: int = 200000,  # ~200KB for large code outputs
    ):
        """Initialize extractor.

        Args:
            project_root: Project root for LLM caller
            timeout: Timeout for extraction call (short, as this is fast)
            max_content_length: Truncate content if too long
        """
        self.project_root = project_root
        self.timeout = timeout
        self.max_content_length = max_content_length

    def extract(
        self,
        raw_output: str,
        schema_hint: Optional[str] = None,
        required_keys: Optional[list[str]] = None,
    ) -> Optional[dict[str, Any]]:
        """Extract JSON from raw LLM output.

        Args:
            raw_output: The raw output from phase 1 LLM call
            schema_hint: Optional description of expected schema
            required_keys: Keys that must be present in result

        Returns:
            Parsed dict or None if extraction fails
        """
        # Fast path: already valid JSON
        result = self._try_direct_parse(raw_output, required_keys)
        if result is not None:
            logger.debug("JSON extraction: direct parse succeeded")
            return result

        # Phase 2: Use LLM to extract
        logger.debug("JSON extraction: using LLM extraction")
        return self._extract_with_llm(raw_output, schema_hint, required_keys)

    def _try_direct_parse(
        self,
        raw_output: str,
        required_keys: Optional[list[str]],
    ) -> Optional[dict[str, Any]]:
        """Try to parse directly without LLM extraction."""
        from .utils.json_parser import parse_json_response

        return parse_json_response(raw_output, required_keys=required_keys)

    def _extract_with_llm(
        self,
        raw_output: str,
        schema_hint: Optional[str],
        required_keys: Optional[list[str]],
    ) -> Optional[dict[str, Any]]:
        """Use LLM to extract JSON from raw output."""
        # Import here to avoid circular imports
        from .llm_caller import LLMCaller

        # Truncate if too long to avoid overwhelming the extractor
        content = raw_output
        if len(content) > self.max_content_length:
            # Try to truncate at a reasonable boundary
            truncate_point = content.rfind("\n", 0, self.max_content_length)
            if truncate_point < self.max_content_length * 0.8:
                truncate_point = self.max_content_length
            content = content[:truncate_point]
            logger.warning(f"Content truncated from {len(raw_output)} to {len(content)} for extraction")

        schema_section = f"Expected schema: {schema_hint}" if schema_hint else "Ensure all relevant data is included in the JSON."

        prompt = EXTRACTION_PROMPT.format(
            content=content,
            schema_hint=schema_section,
        )

        try:
            # Use LLMCaller but without JSON requirement to avoid recursion
            caller = LLMCaller(
                project_root=self.project_root,
                max_retries=2,  # Fewer retries for extraction
                retry_delay=1.0,
            )

            # Important: Don't use require_json=True here to avoid infinite recursion
            response = caller.call(
                prompt=prompt,
                require_json=False,
                timeout=self.timeout,
            )

            # Parse the extraction result
            from .utils.json_parser import parse_json_response
            result = parse_json_response(response, required_keys=required_keys)

            if result:
                logger.info("JSON extraction succeeded via LLM")
                return result
            else:
                logger.warning("LLM extraction returned invalid JSON")
                return None

        except Exception as e:
            logger.warning(f"LLM extraction failed: {e}")
            return None


def extract_json_two_phase(
    raw_output: str,
    project_root: Optional[Any] = None,
    schema_hint: Optional[str] = None,
    required_keys: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """Convenience function for two-phase JSON extraction.

    Args:
        raw_output: Raw output from phase 1
        project_root: Project root path
        schema_hint: Expected schema description
        required_keys: Required keys in result

    Returns:
        Parsed dict or None
    """
    extractor = JSONExtractor(project_root=project_root)
    return extractor.extract(raw_output, schema_hint, required_keys)
