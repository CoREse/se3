"""JSON extraction modes for LLM calls.

Defines three strategies for obtaining structured JSON output from LLMs:

1. STRICT (require_json): Force LLM to output JSON, retry if invalid
   - Prompt wrapped with JSON constraints
   - Retries on parse failure
   - Best for: Simple, reliable outputs

2. EXTRACT (json_extract): Request JSON in prompt, extract if LLM fails
   - Prompt wrapped with JSON constraints  
   - No retries, uses LLM extraction on failure
   - Best for: Balanced reliability and efficiency

3. TWO_PHASE (two_phase_json): Natural generation + LLM extraction
   - Clean prompt without JSON constraints
   - LLM extracts JSON from natural output
   - Best for: Complex outputs, avoiding prompt pollution

Usage:
    # Mode 1: Strict with retry (default)
    response = caller.call(prompt, require_json=True)
    
    # Mode 2: Request JSON, extract on failure
    response = caller.call(prompt, json_mode=JsonMode.EXTRACT)
    
    # Mode 3: Two-phase extraction
    response = caller.call(prompt, json_mode=JsonMode.TWO_PHASE)
"""

# WHY: `get_json_mode` annotates parameters with PEP 604 unions
# (`JsonMode | None`), which signature evaluation resolves at import time and
# which `type.__or__` cannot support before 3.10. Deferring annotations keeps
# this module importable on the package's declared 3.9 floor without rewriting
# the annotations, and matches the charter's annotation style.
from __future__ import annotations

from enum import Enum, auto


class JsonMode(Enum):
    """JSON extraction strategies."""
    
    STRICT = "strict"
    """Force JSON output with retry on failure.
    
    Wraps prompt with strict JSON constraints.
    Retries up to max_retries if output is invalid.
    Best for simple outputs where reliability is critical.
    """
    
    EXTRACT = "extract"
    """Request JSON, use LLM extraction on failure.
    
    Wraps prompt with JSON constraints (like STRICT).
    On parse failure, uses second LLM call to extract JSON.
    No retries on the main call - extraction is the recovery.
    Best balance of reliability and token efficiency.
    """
    
    TWO_PHASE = "two_phase"
    """Natural generation followed by LLM extraction.
    
    Phase 1: Clean prompt without JSON constraints.
    Phase 2: LLM extracts JSON from natural output.
    Best for complex outputs, avoids prompt pollution.
    """
    
    OFF = "off"
    """No JSON requirement."""


def get_json_mode(
    require_json: bool = False,
    json_mode: JsonMode | None = None,
    two_phase_json: bool | None = None,
) -> JsonMode:
    """Determine JSON mode from various parameter combinations.
    
    Maintains backward compatibility with require_json parameter
    while supporting new json_mode parameter.
    
    Priority:
    1. Explicit json_mode parameter
    2. two_phase_json=True -> TWO_PHASE
    3. require_json=True -> STRICT
    4. Default -> OFF
    
    Args:
        require_json: Legacy flag for strict JSON mode
        json_mode: Explicit mode selection (preferred)
        two_phase_json: Legacy flag for two-phase mode
        
    Returns:
        Resolved JsonMode
    """
    if json_mode is not None:
        return json_mode
    
    if two_phase_json:
        return JsonMode.TWO_PHASE
    
    if require_json:
        return JsonMode.STRICT
    
    return JsonMode.OFF
