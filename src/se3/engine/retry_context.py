"""Shared constants for retry-context formatting and capping.

Consumed by both the producer (``chat_history.format_history_for_retry``)
and the consumer (``llm_caller._post_dedup_safety_cap``). Extracting the
shared API to a neutral module avoids cross-module import of private
underscore-prefixed names.

Invariant: ``format_history_for_retry`` emits exactly one
``RETRY_HISTORY_MARKER`` and exactly one ``RETRY_HISTORY_SEPARATOR`` per
retry-context block. The post-dedup safety cap depends on this invariant
to locate the tail of the retry history and truncate the head. A future
edit that adds a second marker/separator, removes the separator under
some branch, or reuses the separator as a section divider inside the
``[User Prompt]:`` body will silently break the cap's anchoring.
"""

from __future__ import annotations

import os

# Marker prefixing the retry-history block. The cap locates the block by
# finding this marker at position 0 of the effective prompt.
RETRY_HISTORY_MARKER = "[Previous conversation context for this step]:"

# Unique sentinel terminating the retry-history block. Distinctive tokens
# (``SE3-RETRY-CAP-ANCHOR``) make collisions with markdown horizontal rules
# or spec content containing 40-char ``=`` runs structurally impossible.
# The cap additionally uses ``rfind`` for defense in depth so retry-of-retry
# chains (where a prior retry's ``effective_prompt`` containing an inner
# anchor is stored as a user message and replayed verbatim) still resolve
# to the OUTER anchor.
RETRY_HISTORY_SEPARATOR = "=== END RETRY HISTORY [SE3-RETRY-CAP-ANCHOR] ==="


_DEFAULT_POST_DEDUP_SAFETY_LIMIT = 500_000
_ENV_VAR = "SE3_POST_DEDUP_SAFETY_LIMIT"


def _load_safety_limit() -> int:
    """Resolve the cap from ``SE3_POST_DEDUP_SAFETY_LIMIT`` env var, falling
    back to the built-in default on missing/invalid values.
    """
    raw = os.environ.get(_ENV_VAR)
    if raw is None:
        return _DEFAULT_POST_DEDUP_SAFETY_LIMIT
    try:
        val = int(raw)
    except ValueError:
        return _DEFAULT_POST_DEDUP_SAFETY_LIMIT
    return val if val > 0 else _DEFAULT_POST_DEDUP_SAFETY_LIMIT


# Post-dedup safety cap for retry-context prompts. Primary dedup happens in
# ``LLMCaller._call_with_retry()`` via ``deduplicate_prompt_lines()`` after
# combining retry context with the new prompt; this limit is a defensive
# fallback evaluated *after* dedup on the whole effective_prompt, not per
# historical user prompt. It prevents unbounded growth in the degenerate
# case where dedup has no effect (e.g. every old prompt is unique).
POST_DEDUP_SAFETY_LIMIT = _load_safety_limit()
