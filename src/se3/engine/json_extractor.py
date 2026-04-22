"""Two-phase JSON extraction from LLM output.

Provides reliable JSON extraction by using a second LLM call to parse
and structure the output, avoiding prompt pollution in the primary call.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Two-phase extraction prompt.
#
# Phase 2 is invoked only after Phase 1's output has been judged unusable as
# JSON — either there is no JSON, the JSON is malformed, or the JSON is
# structurally valid but incomplete (missing required_keys). In every one of
# those cases the work is the same: re-express the content's meaning as a
# schema-complete JSON object. The prompt is therefore phrased as a
# summarization/structuring task over the raw content, not as a conditional
# "extract if JSON else synthesize" branch — that branching belongs to the
# upstream fast-path, not to the LLM.
EXTRACTION_PROMPT = """You are given a raw LLM output. Re-express its meaning as a single valid JSON object that matches the expected output schema shown below.

The content may contain prose, reasoning, tool-call traces, partial or complete JSON, code snippets, or any combination. Treat all of it as source material describing what the final JSON should say — not as something to copy verbatim.

Rules:
1. Return ONLY the JSON object — no markdown fences, no commentary, no preamble.
2. Produce a schema-complete JSON object that conveys everything the content describes, mapped onto the fields defined in the "Expected output schema" section below. Put the main user-facing message into the primary text field (typically `content`, `message`, or `summary` — whichever the schema defines); populate list fields (`questions`, `items`, etc.) from whatever enumeration the content implies.
3. Never return an empty object `{{}}`. If a field has no information in the source, use `null` or `[]` for that field — but at least one user-visible text field must be populated whenever the source has any meaningful content.
4. Preserve meaning faithfully. Do not invent facts or paraphrase away information.
5. JSON must be syntactically valid: double quotes on all keys and string values, no trailing commas, no unescaped newlines inside strings.

===== Source content =====
{content}
===== End of source content =====

===== Expected output schema =====
{schema_hint}
===== End of expected output schema =====

Respond with valid JSON only:"""


class JSONExtractor:
    """Extracts JSON from raw LLM output using a lightweight second pass."""

    def __init__(
        self,
        project_root: Optional[Any] = None,
        timeout: int = 60,
    ):
        """Initialize extractor.

        Args:
            project_root: Project root for LLM caller
            timeout: Timeout for extraction call
        """
        self.project_root = project_root
        self.timeout = timeout

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

    def _extract_text_from_stream_json(self, raw_output: str) -> str:
        """Extract assistant text content from stream-json (NDJSON) format.
        
        Parses the stream-json format and extracts text from assistant messages.
        Only extracts the final assistant message(s), filtering out tool calls
        and tool results.
        
        Args:
            raw_output: Raw NDJSON output from LLM
            
        Returns:
            Extracted text content from assistant messages
        """
        if not raw_output:
            return ""
        
        text_parts = []
        for line in raw_output.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if not isinstance(data, dict):
                    continue
                msg_type = data.get("type", "")
                
                # Only process assistant messages
                if msg_type == "assistant":
                    message = data.get("message", {})
                    content = message.get("content", [])
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text = item.get("text", "")
                            if text:
                                text_parts.append(text)
                                
            except json.JSONDecodeError:
                # Not valid JSON, might be a partial line
                continue
        
        return "\n".join(text_parts)

    def _extract_with_llm(
        self,
        raw_output: str,
        schema_hint: Optional[str],
        required_keys: Optional[list[str]],
    ) -> Optional[dict[str, Any]]:
        """Use LLM to extract JSON from raw output."""
        # Import here to avoid circular imports
        from .llm_caller import LLMCaller

        # First, extract text content from stream-json format
        # This filters out tool results and other non-assistant messages
        extracted_text = self._extract_text_from_stream_json(raw_output)
        
        # Use extracted text if available, otherwise fall back to raw output
        content = extracted_text if extracted_text else raw_output
        
        # Log if content was reduced
        if len(content) < len(raw_output):
            logger.info(f"Extracted {len(content)} chars of text from {len(raw_output)} chars of raw output")

        # The EXTRACTION_PROMPT already wraps this value in a labeled
        # "Expected output schema" section, so we pass the raw hint (or a
        # generic fallback) without any additional prefix here.
        schema_section = (
            schema_hint
            if schema_hint
            else "No specific schema provided. Structure the output to include every meaningful piece of information from the source content, using descriptive field names."
        )

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
