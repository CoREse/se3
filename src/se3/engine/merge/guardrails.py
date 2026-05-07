"""MergeGuardrailsCheck — Check spec file integrity after a merge.

Runs guardrails on any spec files (se3/specs/**/spec.md) that changed
during the merge. Detects deleted requirements, weakened language,
and weakened quantifiers.

Also exposes ``check_spec_diff()`` as a reusable pure function so the
CLI ``se3 guardrails`` command can share the same logic.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..worktree import _run_git
from ...commands.merge.result_model import EvidenceRecord

logger = logging.getLogger(__name__)


def _evidence_dict(**kwargs: Any) -> dict:
    """Build an evidence dict with H4 typo-fail-fast validation.

    Constructs an :class:`EvidenceRecord` (which raises ``TypeError`` for
    unknown keyword arguments) and serialises to a dict so existing
    consumers that perform ``"key" in evidence`` checks keep working.
    """
    return EvidenceRecord(**kwargs).to_dict()

_SPEC_PATH_RE = re.compile(r"^se3/specs/.+/spec\.md$")

_WEAKEN_PATTERNS = [
    (r'\bMUST\b', r'\b(SHOULD|MAY)\b', "MUST weakened to SHOULD/MAY"),
    (r'\bSHALL\b', r'\b(SHOULD|MAY)\b', "SHALL weakened to SHOULD/MAY"),
    (r'\bREQUIRED\b', r'\b(RECOMMENDED|OPTIONAL)\b', "REQUIRED weakened to RECOMMENDED/OPTIONAL"),
]

_QUANTIFIER_PATTERNS = [
    (r'(?i)\ball\b', r'(?i)\bsome\b', "quantifier weakened: all → some"),
    (r'(?i)\bevery\b', r'(?i)\bsome\b', "quantifier weakened: every → some"),
]

# Token-set Jaccard similarity threshold for strong↔weak line pairing.
_PAIR_SIMILARITY_THRESHOLD = 0.5

# Higher threshold for Phase 2 (mixed lines) where a deleted strong line
# could coincidentally pair with an unrelated new line containing both
# strong and weak keywords. The stricter requirement reduces false-positive
# WEAKENING classifications for genuine DELETEs.
#
# Trade-off: 0.65 deliberately misses rare in-place mixed weakenings where a
# single strong line both gains a new strong clause AND has its original
# keyword weakened (e.g. "SHALL validate inputs." -> "SHOULD validate inputs.
# SHALL log requests."). The token sets would be {validate,inputs} vs
# {validate,inputs,log,requests} with Jaccard ~0.5, below threshold. Raising
# the threshold would catch these but increase false positives from genuine
# extensions that add unrelated tokens to an existing line.
_PAIR_SIMILARITY_THRESHOLD_MIXED = 0.65

# Role words (requirement strength keywords) to strip before pairing.
_ROLE_WORDS = frozenset({
    "shall", "should", "must", "may", "required", "recommended", "optional",
    "all", "some", "every",
})

# Common stop words to strip before pairing.
_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "to", "of", "in", "for", "on", "with", "at", "by", "from", "as",
    "and", "or", "but", "if", "then", "than", "that", "this", "it",
    "not", "no", "yes", "can", "cannot",
})


@dataclass
class GuardrailViolation:
    """A single guardrail violation.

    The ``evidence`` dict carries strong_line / weak_line / deleted_line /
    when_clauses for diagnostic purposes.  Optional fields such as
    ``branch_name``, ``trigger_branch``, and ``branch_kind`` are added by
    the orchestrator layer (via ``_violations_to_dicts``) so that the
    human call file shows which branch's merge produced the violation.
    """

    file_path: str
    violation_type: str
    message: str
    evidence: Optional[dict] = None


@dataclass
class GuardrailReport:
    """Result of a guardrails check.

    Attributes:
        passed: True when no violations were found AND the check completed.
        violations: List of detected guardrail violations.
        incomplete: True when the check could not finish (e.g. one or more
            spec files could not be read).  Callers SHOULD treat an
            incomplete report as a fail-closed condition.
    """

    passed: bool = True
    violations: list[GuardrailViolation] = field(default_factory=list)
    incomplete: bool = False


def _tokenize_for_pairing(line: str) -> set[str]:
    """Extract content tokens from a line for similarity comparison.

    Removes role words (SHALL/SHOULD/MUST/MAY/etc.) and common stop words,
    then returns the remaining lower-case word tokens as a set.
    """
    # Remove markdown punctuation, keep alphanumeric and spaces
    cleaned = re.sub(r"[^\w\s]", " ", line)
    tokens = cleaned.lower().split()
    return {
        t for t in tokens
        if t not in _ROLE_WORDS and t not in _STOP_WORDS and len(t) > 1
    }


def _tokenize_for_deduplication(line: str) -> set[str]:
    """Tokenize for exact-match deduplication, preserving role words.

    Removes common stop words and short tokens but keeps role words
    (SHALL/SHOULD/MUST/MAY/etc.) so that SHALL→SHOULD changes are
    not silently treated as whitespace-only rewrites.

    Why two tokenizers?
    - ``_tokenize_for_deduplication`` keeps role words because dedup must
      distinguish a strong line from its weakened variant (e.g. SHALL vs
      SHOULD). Stripping role words would make them identical and hide the
      weakening.
    - ``_tokenize_for_pairing`` strips role words because pairing must match
      a deleted strong line with a new weak line that expresses the same
      semantic content. Keeping role words would prevent "SHALL validate"
      from pairing with "SHOULD validate" even though they are clearly the
      same requirement at different strength levels.
    """
    cleaned = re.sub(r"[^\w\s]", " ", line)
    tokens = cleaned.lower().split()
    return {t for t in tokens if t not in _STOP_WORDS and len(t) > 1}


def _jaccard_similarity(set_a: set[str], set_b: set[str]) -> float:
    """Compute Jaccard similarity between two token sets.

    Returns 0.0 when either set is empty (no meaningful content to compare).
    """
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def _pair_strong_weak_lines(
    missing_strong_lines: list[str],
    weak_only_lines: list[str],
    threshold: float = _PAIR_SIMILARITY_THRESHOLD,
) -> list[tuple[int, int, str, str, float]]:
    """Pair missing strong lines with weak-only lines via token-set Jaccard.

    Computes all possible strong↔weak pairings, sorts by similarity score
    descending, and greedily assigns the highest-scoring pairs first. This
    avoids the "first-strong-wins" starvation problem of the naive
    per-strong greedy loop.

    Note:
        This is a greedy best-first assignment, not an optimal bipartite
        matching (e.g. Hungarian algorithm). For the small line counts
        typical in spec files the difference is negligible, but pathological
        inputs with many similar strong and weak lines could yield a
        sub-optimal pairing that blocks a globally better assignment.

    Returns:
        List of (strong_idx, weak_idx, strong_line, weak_line, score) tuples
        for all successful pairings. Each strong and weak line is paired at
        most once. ``strong_idx`` and ``weak_idx`` are the original list
        indices so callers can unambiguously map back to line numbers.
    """
    # Pre-tokenize all lines
    strong_tokens = [
        (s, _tokenize_for_pairing(s)) for s in missing_strong_lines
    ]
    weak_tokens = [(w, _tokenize_for_pairing(w)) for w in weak_only_lines]

    # Compute all candidate pairings above threshold
    candidates: list[tuple[float, int, int]] = []
    for si, (_, s_toks) in enumerate(strong_tokens):
        for wi, (_, w_toks) in enumerate(weak_tokens):
            score = _jaccard_similarity(s_toks, w_toks)
            if score >= threshold:
                candidates.append((score, si, wi))

    # Sort by score descending so best matches are claimed first
    candidates.sort(key=lambda x: x[0], reverse=True)

    pairings: list[tuple[int, int, str, str, float]] = []
    used_strong: set[int] = set()
    used_weak: set[int] = set()

    for score, si, wi in candidates:
        if si in used_strong or wi in used_weak:
            continue
        pairings.append((si, wi, strong_tokens[si][0], weak_tokens[wi][0], score))
        used_strong.add(si)
        used_weak.add(wi)

    return pairings


def _extract_weak_role_word(line: str) -> str | None:
    """Extract the weak role word from a line, or None if absent.

    Matches the same weak keywords used in ``_WEAKEN_PATTERNS`` and
    ``_QUANTIFIER_PATTERNS`` so that role-word changes (e.g. SHOULD→MAY)
    can be detected even when the semantic token set is identical.
    """
    match = re.search(r'\b(SHOULD|MAY|MIGHT|COULD|CAN|OPTIONAL|RECOMMENDED|SOME)\b', line, re.I)
    return match.group(1).upper() if match else None


def _weak_line_is_new(
    line: str,
    orig_stripped: set[str],
    orig_weak_token_sets: list[set[str]],
    orig_weak_lines: list[str] | None = None,
) -> bool:
    """Return True if a weak-only line is genuinely new (not pre-existing).

    Uses exact stripped string comparison first, then token-set normalization
    against pre-computed original weak-only token sets to catch
    whitespace/punctuation-only changes without conflating strong→weak
    conversions with pre-existing weak lines.

    If the new line shares the same token set as an original weak line but
    uses a different weak role word (e.g. SHOULD→MAY), it is considered
    "new" so that the pairing logic can flag the weakening.
    """
    stripped = line.strip()
    if stripped in orig_stripped:
        return False

    line_tokens = _tokenize_for_pairing(line)
    if not line_tokens:
        return True

    for i, orig_tokens in enumerate(orig_weak_token_sets):
        if line_tokens == orig_tokens:
            # Same semantic content — check if the weak role word changed
            if orig_weak_lines is not None:
                new_role = _extract_weak_role_word(line)
                orig_role = _extract_weak_role_word(orig_weak_lines[i])
                if new_role != orig_role:
                    return True  # Role word changed → genuine weakening
            return False
    return True


def _precompute_orig_data(orig_lines: list[str]) -> dict:
    """Precompute pattern-independent data for all original lines.

    Returns a dict with ``stripped_set``, ``pairing_tokens``, and
    ``dedup_tokens`` so that ``_compute_pairing_evidence`` can avoid
    re-tokenizing the same lines across multiple pattern checks.
    """
    return {
        "stripped_set": {l.strip() for l in orig_lines},
        "pairing_tokens": [_tokenize_for_pairing(l) for l in orig_lines],
        "dedup_tokens": [_tokenize_for_deduplication(l) for l in orig_lines],
    }


def _compute_pairing_evidence(
    orig_lines: list[str],
    new_lines: list[str],
    strong_re: str,
    weak_re: str,
    allow_mixed_lines: bool = False,
    _precomputed: Optional[dict] = None,
) -> Optional[dict]:
    """Compute pairing evidence for a strong→weak transition.

    Looks for a strong line that disappeared from the original and a
    new weak-only line that can be paired with it via token-set similarity.

    When ``allow_mixed_lines=True``, also considers lines that contain both
    the strong and weak keywords (in-place partial weakening, e.g. one of
    two SHALLs on the same line becoming SHOULD).

    Args:
        _precomputed: Optional dict from ``_precompute_orig_data``. When
            provided, tokenization of original lines is skipped.

    Returns:
        Evidence dict with strong_line, weak_line, pairing_score, and
        line numbers (strong_line_no, weak_line_no), or None if no
        pairing can be established.
    """
    orig_strong = [
        (line_no, line.strip())
        for line_no, line in enumerate(orig_lines, start=1)
        if re.search(strong_re, line)
    ]
    new_strong_set = {
        line.strip() for line in new_lines if re.search(strong_re, line)
    }
    missing_strong = [
        (ln, s) for ln, s in orig_strong if s not in new_strong_set
    ]
    if not missing_strong:
        return None

    # Pre-compute original data for O(W+O) filtering instead of O(W*O).
    if _precomputed is not None:
        orig_stripped = _precomputed["stripped_set"]
        orig_weak_token_sets = [
            tokens for line, tokens in zip(orig_lines, _precomputed["pairing_tokens"])
            if re.search(weak_re, line) and not re.search(strong_re, line)
        ]
        orig_weak_lines = [
            line for line in orig_lines
            if re.search(weak_re, line) and not re.search(strong_re, line)
        ]
    else:
        orig_stripped = {l.strip() for l in orig_lines}
        orig_weak_token_sets = [
            _tokenize_for_pairing(orig)
            for orig in orig_lines
            if re.search(weak_re, orig) and not re.search(strong_re, orig)
        ]
        orig_weak_lines = [
            orig for orig in orig_lines
            if re.search(weak_re, orig) and not re.search(strong_re, orig)
        ]

    # Phase 1: weak-only lines (strong→weak replacement)
    weak_only = [
        (line_no, line.strip())
        for line_no, line in enumerate(new_lines, start=1)
        if re.search(weak_re, line) and not re.search(strong_re, line)
        and _weak_line_is_new(line, orig_stripped, orig_weak_token_sets, orig_weak_lines)
    ]
    if weak_only:
        pairings = _pair_strong_weak_lines(
            [s for _, s in missing_strong],
            [w for _, w in weak_only],
        )
        if pairings:
            all_pairings = []
            for strong_idx, weak_idx, strong_text, weak_text, score in pairings:
                all_pairings.append({
                    "strong_line": strong_text,
                    "weak_line": weak_text,
                    "pairing_score": round(score, 3),
                    "strong_line_no": missing_strong[strong_idx][0],
                    "weak_line_no": weak_only[weak_idx][0],
                })
            best = pairings[0]
            strong_idx, weak_idx, strong_text, weak_text, score = best
            evidence = _evidence_dict(
                strong_line=strong_text,
                weak_line=weak_text,
                pairing_score=round(score, 3),
                strong_line_no=missing_strong[strong_idx][0],
                weak_line_no=weak_only[weak_idx][0],
            )
            if len(all_pairings) > 1:
                evidence["all_pairings"] = all_pairings
            return evidence

    if not allow_mixed_lines:
        return None

    # Phase 2: in-place partial weakening (line contains both strong and weak,
    # and is different from any original strong line).
    # Use token-set normalization for all original strong lines (both pure-strong
    # and mixed) so that whitespace/punctuation-only rewrites of any original
    # line are not treated as new and paired, producing a false positive.
    # Pure-strong lines that become mixed (e.g. SHALL X and SHALL Y →
    # SHALL X and SHOULD Y) must NOT be filtered out — they are genuine new
    # mixed lines, and token-set comparison naturally handles this because the
    # token set differs (one strong word replaced by a weak word).
    #
    # Note: orig_mixed (lines where original also matches weak_re) is a subset
    # of orig_strong, and both use the same _tokenize_for_deduplication tokenizer.
    # Any line filtered by an orig_mixed check would already be filtered by the
    # orig_strong check, so only one deduplication loop is needed.
    if _precomputed is not None:
        orig_strong_token_sets = [
            _precomputed["dedup_tokens"][line_no - 1]
            for line_no, _ in orig_strong
        ]
    else:
        orig_strong_token_sets = [
            _tokenize_for_deduplication(s)
            for _, s in orig_strong
        ]
    mixed_lines = []
    for line_no, line in enumerate(new_lines, start=1):
        if re.search(weak_re, line) and re.search(strong_re, line):
            stripped = line.strip()
            line_tokens = _tokenize_for_deduplication(line)
            # Skip token-equivalent rewrites of any original strong line
            if line_tokens and any(
                line_tokens == orig_tokens
                for orig_tokens in orig_strong_token_sets
            ):
                continue
            mixed_lines.append((line_no, stripped))
    if mixed_lines:
        missing_strong_texts = [s for _, s in missing_strong]
        mixed_texts = [w for _, w in mixed_lines]
        pairings = _pair_strong_weak_lines(
            missing_strong_texts,
            mixed_texts,
            threshold=_PAIR_SIMILARITY_THRESHOLD_MIXED,
        )
        # Verify the pairing actually represents a weakening: the mixed line
        # must contain fewer occurrences of the strong keyword than the
        # original strong line it replaces, OR the same count with the weak
        # keyword appearing in the same position as the original strong keyword
        # (indicating an in-place replacement, e.g. "SHALL validate inputs." ->
        # "SHOULD validate inputs and SHALL log requests.").
        # Without these guards, an extension that merely adds a weak keyword
        # (e.g. "SHALL validate inputs." -> "SHALL validate inputs and
        # SHOULD log.") can be falsely classified as a WEAKENING because the
        # token-set Jaccard exceeds the threshold.
        valid_pairings = []
        for strong_idx, weak_idx, strong_text, weak_text, score in pairings:
            strong_count = len(re.findall(strong_re, strong_text))
            weak_line_strong_count = len(re.findall(strong_re, weak_text))
            if weak_line_strong_count < strong_count:
                valid_pairings.append(
                    (strong_idx, weak_idx, strong_text, weak_text, score, None)
                )
            # Same strong count: check for in-place replacement where the weak
            # keyword appears at the same position as the original strong
            # keyword (same prefix text before the keyword).
            #
            # Edge case: a keyword-position swap like "SHALL log" -> "log SHOULD"
            # produces orig_prefix_tokens=[] and mixed_prefix_tokens=["log"].
            # The empty-vs-non-empty case falls through to prefix_score=0,
            # treating the swap as a structural rewrite rather than an in-place
            # weakening. This is intentional — position swaps are rare and
            # usually indicate a broader sentence restructuring.
            elif weak_line_strong_count == strong_count:
                weak_match = re.search(weak_re, weak_text)
                strong_match = re.search(strong_re, strong_text)
                if weak_match and strong_match:
                    mixed_prefix = weak_text[:weak_match.start()].strip()
                    orig_prefix = strong_text[:strong_match.start()].strip()
                    mixed_prefix_tokens = _tokenize_for_pairing(mixed_prefix)
                    orig_prefix_tokens = _tokenize_for_pairing(orig_prefix)
                    prefix_score = _jaccard_similarity(
                        mixed_prefix_tokens, orig_prefix_tokens,
                    )
                    # Treat empty prefixes as matching (both lines start with
                    # the keyword itself).
                    if (
                        not mixed_prefix_tokens and not orig_prefix_tokens
                    ) or prefix_score >= 0.8:
                        valid_pairings.append(
                            (strong_idx, weak_idx, strong_text, weak_text, score, prefix_score)
                        )

        if valid_pairings:
            all_pairings = []
            for si, wi, st, wt, sc, ps in valid_pairings:
                pd = {
                    "strong_line": st,
                    "weak_line": wt,
                    "pairing_score": round(sc, 3),
                    "strong_line_no": missing_strong[si][0],
                    "weak_line_no": mixed_lines[wi][0],
                }
                if ps is not None:
                    pd["prefix_score"] = round(ps, 3)
                all_pairings.append(pd)
            best = valid_pairings[0]
            evidence = _evidence_dict(
                strong_line=best[2],
                weak_line=best[3],
                pairing_score=round(best[4], 3),
                strong_line_no=missing_strong[best[0]][0],
                weak_line_no=mixed_lines[best[1]][0],
            )
            if best[5] is not None:
                evidence["prefix_score"] = round(best[5], 3)
            if len(all_pairings) > 1:
                evidence["all_pairings"] = all_pairings
            return evidence
        # If no pairing passed the weakening guard, log the best sub-threshold
        # score for diagnostic tracing of
        # near-miss in-place partial weakenings (see _PAIR_SIMILARITY_THRESHOLD_MIXED
        # documentation for the trade-off).
        highest_below = 0.0
        best_pair: tuple[str, str] = ("", "")
        for s_text in missing_strong_texts:
            s_toks = _tokenize_for_pairing(s_text)
            if not s_toks:
                continue
            for w_text in mixed_texts:
                w_toks = _tokenize_for_pairing(w_text)
                if not w_toks:
                    continue
                score = _jaccard_similarity(s_toks, w_toks)
                if score < _PAIR_SIMILARITY_THRESHOLD_MIXED and score > highest_below:
                    highest_below = score
                    best_pair = (s_text, w_text)
        if highest_below > 0:
            logger.debug(
                "Phase 2 mixed-line pairing: highest sub-threshold score "
                "%.3f (threshold %.2f) for '%s' ↔ '%s'",
                highest_below, _PAIR_SIMILARITY_THRESHOLD_MIXED,
                best_pair[0], best_pair[1],
            )

    return None


def _first_missing_strong_line(
    orig_lines: list[str],
    new_lines: list[str],
    strong_re: str,
) -> tuple[Optional[str], int]:
    """Return the first strong line that disappeared and its line number.

    Used to provide minimal evidence for DELETE violations when no
    weak-only or mixed-line pairing can be established.
    """
    orig_strong = [
        (line_no, line.strip())
        for line_no, line in enumerate(orig_lines, start=1)
        if re.search(strong_re, line)
    ]
    new_strong_set = {
        line.strip() for line in new_lines if re.search(strong_re, line)
    }
    for ln, s in orig_strong:
        if s not in new_strong_set:
            return s, ln
    return None, 0


def _normalize_message(message: str) -> str:
    """Normalize a violation message for stable hashing.

    Strips leading/trailing whitespace and removes line number
    references (e.g. "line 42", "at line 456"), attempt counters
    (e.g. "(attempt 3)"), and hex-like strings (e.g. SHA prefixes).
    Preserves count digits so that ``3 WHEN clause(s)`` and
    ``4 WHEN clause(s)`` remain distinguishable.
    """
    normalized = message.strip()
    # Remove line number patterns like "line 42", "at line 42",
    # "lines 42-45", "at lines 42-45"
    normalized = re.sub(
        r"\b(at\s+)?lines?\s+\d+(-\d+)?\b", "", normalized, flags=re.IGNORECASE
    )
    # Remove parenthesised numbers / attempt counters
    # (e.g. "(42)", "(attempt 3)", "(try 5)")
    normalized = re.sub(r"\(\d+\)", "", normalized)
    normalized = re.sub(r"\(attempt\s+\d+\)", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\(try\s+\d+\)", "", normalized, flags=re.IGNORECASE)
    # Also strip bare-word forms like "attempt 3 failed" or "iteration 4"
    normalized = re.sub(
        r"\battempt\s+\d+\b", "", normalized, flags=re.IGNORECASE
    )
    normalized = re.sub(
        r"\biteration\s+\d+\b", "", normalized, flags=re.IGNORECASE
    )
    normalized = re.sub(
        r"\btry\s+\d+\b", "", normalized, flags=re.IGNORECASE
    )
    # Remove hex-like strings (e.g. git SHA prefixes, object IDs).
    # Only context-anchored patterns are used — hex tokens must be preceded
    # by a SHA cue (sha:, commit:, hash:, ref:) — to avoid silently stripping
    # non-SHA hex-like identifiers that happen to be 7–40 chars long.
    # The evidence-derived stable key in _stable_key_from_violation is the
    # primary path for stable hashing; this message normalization is a
    # backward-compat fallback.
    normalized = re.sub(
        r"(?:sha|commit|hash|ref)[: ]*\s*[0-9a-fA-F]{7,40}\b",
        "", normalized, flags=re.IGNORECASE,
    )
    # Collapse multiple spaces
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _stable_hash_short(text: str) -> str:
    """Return a short, stable hex hash of *text* for sentinel keys.

    Uses SHA-256 instead of Python's built-in ``hash()`` so the value is
    deterministic across processes (``PYTHONHASHSEED`` does not affect it).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _stable_key_from_violation(v: GuardrailViolation) -> str:
    """Derive a stable hash key from violation evidence when available.

    Evidence-derived keys are preferred over normalized messages because
    they are insensitive to message-format drift (timestamps, attempt
    counters, line numbers).  Falls back to the normalized message when
    no usable evidence is present.

    Sentinel fallback: if token normalization strips a line to the empty
    string (e.g. a strong line whose content is all role/stop words such
    as "SHALL be required"), we append the violation type and the
    original text hash so distinct violations do not collide.
    """
    evidence = v.evidence
    if evidence is not None:
        # WEAKENING evidence carries strong_line + weak_line
        strong = evidence.get("strong_line")
        if strong and strong != "<unknown>":
            # Token-normalize the strong line so whitespace/punctuation
            # drift does not change the key.
            key = _normalize_for_comparison(strong)
            if key:
                return key
            # Sentinel: prevent empty-key collisions for lines that are
            # all role/stop words (e.g. "SHALL be required").
            return f"sentinel:weakening:{_stable_hash_short(strong)}"
        # DELETE evidence carries deleted_line
        deleted = evidence.get("deleted_line")
        if deleted and deleted != "<unknown>":
            key = _normalize_for_comparison(deleted)
            if key:
                return key
            return f"sentinel:delete:{_stable_hash_short(deleted)}"
        # WHEN-clause deletion evidence carries the list of deleted clauses.
        # Normalize each clause and join so the key is insensitive to count
        # drift (e.g. "1 WHEN clause(s)" vs "2 WHEN clause(s)").
        when_clauses = evidence.get("when_clauses")
        if when_clauses:
            normalized_clauses = sorted(
                _normalize_for_comparison(wc) for wc in when_clauses
            )
            if any(c for c in normalized_clauses):
                joined = "|".join(normalized_clauses)
                return joined
            return f"sentinel:when:{_stable_hash_short('|'.join(when_clauses))}"
    # Fallback: normalized message (kept for backward compat)
    msg_key = _normalize_message(v.message)
    if msg_key:
        return msg_key
    return f"sentinel:msg:{_stable_hash_short(v.message)}"


def _normalize_for_comparison(text: str) -> str:
    """Normalize text for identity comparison (not for display).

    Strips role words and stop words, collapses whitespace, and returns
    a lower-case token string.  Two lines that differ only in role words
    or punctuation will produce the same normalized string.
    """
    tokens = sorted(_tokenize_for_pairing(text))
    return " ".join(tokens)


def violation_set_hash(violations: list[GuardrailViolation]) -> str:
    """Compute a stable hash of a violation set.

    The hash is based on ``(file_path, violation_type, stable_key)``
    for each violation, where ``stable_key`` is derived from evidence
    (strong_line / deleted_line) when available, falling back to the
    normalized message.  Ordering of violations does not affect the hash.
    """
    tokens: list[str] = []
    for v in violations:
        stable_key = _stable_key_from_violation(v)
        token = f"{v.file_path}|{v.violation_type}|{stable_key}"
        tokens.append(token)
    tokens.sort()
    payload = "\n".join(tokens)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def check_spec_diff(original_text: str, new_text: str, file_path: str = "<unknown>") -> list[GuardrailViolation]:
    """Check for guardrail violations between two versions of a spec file.

    Args:
        original_text: The original spec content.
        new_text: The modified spec content.
        file_path: Optional path for violation reporting.

    Returns:
        List of GuardrailViolation objects. Empty list means no violations.
    """
    violations: list[GuardrailViolation] = []
    orig_lines = original_text.splitlines()
    new_lines = new_text.splitlines()

    # Precompute pattern-independent data once to avoid re-tokenizing the
    # same original lines across multiple pattern checks.
    orig_precomputed = _precompute_orig_data(orig_lines)

    # Check for weakened language patterns.
    # Two-layer detection:
    #   1. Occurrence counting: if total strong occurrences decrease and weak
    #      occurrences appear, it's a weakening.
    #   2. Line-content fallback: if a specific strong line disappears and weak
    #      lines appear (net-zero count corner case — e.g. one SHALL→SHOULD
    #      plus a brand-new SHALL elsewhere), still flag it.
    #      The fallback now requires token-set pairing between the missing
    #      strong line and a weak-only line to avoid false positives from
    #      unrelated weak lines elsewhere in the file.
    for strong, weak, message in _WEAKEN_PATTERNS:
        strong_orig = sum(len(re.findall(strong, line)) for line in orig_lines)
        strong_new = sum(len(re.findall(strong, line)) for line in new_lines)
        weak_new = sum(len(re.findall(weak, line)) for line in new_lines)
        detected = False
        evidence: Optional[dict] = None
        is_fast_path = False
        if strong_orig > 0 and strong_new < strong_orig and weak_new > 0:
            detected = True
            is_fast_path = True
        elif strong_orig > 0 and weak_new > 0:
            # Corner case: net-zero count but a specific line was weakened.
            # (e.g. one SHALL→SHOULD and a brand-new SHALL elsewhere).
            # Enable mixed-line pairing so that in-place partial weakenings
            # (same line contains both strong and weak) are still detected.
            evidence = _compute_pairing_evidence(
                orig_lines, new_lines, strong, weak, allow_mixed_lines=True,
                _precomputed=orig_precomputed,
            )
            if evidence is not None:
                detected = True
        # Fast path also deserves evidence when available.
        # Allow mixed lines (containing both strong and weak keywords) so that
        # in-place partial weakenings (e.g. one of two SHALLs on the same line
        # becoming SHOULD) are still detected.
        if detected and evidence is None:
            evidence = _compute_pairing_evidence(
                orig_lines, new_lines, strong, weak,
                allow_mixed_lines=is_fast_path,
                _precomputed=orig_precomputed,
            )
        if detected:
            if is_fast_path and evidence is None:
                # No new weak line paired with the deleted strong line → actual deletion
                keyword = message.split(" weakened")[0]
                del_line, del_ln = _first_missing_strong_line(
                    orig_lines, new_lines, strong,
                )
                violations.append(GuardrailViolation(
                    file_path=file_path,
                    violation_type="DELETE",
                    message=f"Requirement deleted: {keyword} line removed",
                    evidence=_evidence_dict(
                        deleted_line=del_line or "<unknown>",
                        deleted_line_no=del_ln,
                    ),
                ))
            else:
                violations.append(GuardrailViolation(
                    file_path=file_path,
                    violation_type="WEAKENING",
                    message=message,
                    evidence=evidence,
                ))

    # Check for weakened quantifiers (same two-layer approach)
    for strong, weak, message in _QUANTIFIER_PATTERNS:
        strong_orig = sum(len(re.findall(strong, line)) for line in orig_lines)
        strong_new = sum(len(re.findall(strong, line)) for line in new_lines)
        weak_new = sum(len(re.findall(weak, line)) for line in new_lines)
        detected = False
        evidence = None
        is_fast_path = False
        if strong_orig > 0 and strong_new < strong_orig and weak_new > 0:
            detected = True
            is_fast_path = True
        elif strong_orig > 0 and weak_new > 0:
            evidence = _compute_pairing_evidence(
                orig_lines, new_lines, strong, weak, allow_mixed_lines=True,
                _precomputed=orig_precomputed,
            )
            if evidence is not None:
                detected = True
        # Fast path also deserves evidence when available.
        if detected and evidence is None:
            evidence = _compute_pairing_evidence(
                orig_lines, new_lines, strong, weak,
                allow_mixed_lines=is_fast_path,
                _precomputed=orig_precomputed,
            )
        if detected:
            if is_fast_path and evidence is None:
                # No new weak line paired with the deleted strong quantifier line
                keyword = message.split(": ")[1].split(" →")[0]
                del_line, del_ln = _first_missing_strong_line(
                    orig_lines, new_lines, strong,
                )
                violations.append(GuardrailViolation(
                    file_path=file_path,
                    violation_type="DELETE",
                    message=f"Quantifier deleted: '{keyword}' line removed",
                    evidence=_evidence_dict(
                        deleted_line=del_line or "<unknown>",
                        deleted_line_no=del_ln,
                    ),
                ))
            else:
                violations.append(GuardrailViolation(
                    file_path=file_path,
                    violation_type="WEAKENING",
                    message=message,
                    evidence=evidence,
                ))

    # Check for deleted scenarios (WHEN clauses).
    # Compare by normalized token content rather than exact stripped string
    # so that whitespace/punctuation-only reformatting is not flagged.
    # Continuation lines (indented lines following a WHEN line) are joined
    # so that line-reflowed WHEN clauses are not reported as deletions.
    orig_when_lines = _extract_when_clauses(orig_lines)
    new_when_lines = _extract_when_clauses(new_lines)
    new_when_normalized = {_normalize_when_clause(w) for w in new_when_lines}
    missing_when = [
        w for w in orig_when_lines
        if _normalize_when_clause(w) not in new_when_normalized
    ]
    if missing_when:
        violations.append(GuardrailViolation(
            file_path=file_path,
            violation_type="DELETE",
            message=f"Scenarios deleted: {len(missing_when)} WHEN clause(s) removed",
            evidence=_evidence_dict(when_clauses=missing_when),
        ))

    return violations


def _next_non_blank_line(
    lines: list[str], start: int,
) -> tuple[int, str]:
    """Advance from ``start`` to the next non-blank line.

    Returns a ``(index, line)`` tuple.  Raises :class:`StopIteration` when
    no further non-blank line exists.

    A line is "blank" when it is empty *or* contains only whitespace.
    Using ``StopIteration`` (rather than returning a sentinel like ``-1``)
    lets callers wrap the call in a single ``try/except`` block instead
    of guarding every access to ``lines[idx]``.
    """
    for idx in range(start, len(lines)):
        if lines[idx].strip():
            return idx, lines[idx]
    raise StopIteration


def _extract_when_clauses(lines: list[str]) -> list[str]:
    """Extract WHEN clauses, joining continuation lines that were reflowed.

    A continuation line is one that follows a WHEN-containing line and starts
    with whitespace (indented continuation of a markdown list item).  This
    prevents spurious DELETE violations when a WHEN clause is wrapped across
    multiple lines during a merge.

    Blank lines between the WHEN line and its indented continuations are
    skipped so that reflowed clauses that include paragraph breaks are still
    joined correctly.

    H3 — Bounds protection: blank lines are skipped via
    :func:`_next_non_blank_line` which uses ``StopIteration`` to signal
    end-of-file, removing any chance that ``lines[j][0]`` is indexed on
    an empty string.  An additional ``if not cur:`` guard protects against
    pathological inputs (e.g. external tools that leave zero-length lines
    that are not detected by ``.strip()``).
    """
    clauses: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.search(r'\bWHEN\b', line):
            parts = [line.strip()]
            j = i + 1
            while True:
                try:
                    j, cur = _next_non_blank_line(lines, j)
                except StopIteration:
                    break
                # Defensive: skip any zero-length residual lines that
                # somehow survived strip() (should not happen, but a
                # defence-in-depth guard against external corruption).
                if not cur:
                    j += 1
                    continue
                if cur[0] not in ' \t':
                    break
                # A whitespace-indented line that starts its own WHEN clause
                # (e.g. nested list item) is a new clause, not a continuation.
                if re.search(r'\bWHEN\b', cur):
                    break
                parts.append(cur.strip())
                j += 1
            clauses.append(' '.join(parts))
            i = j
        else:
            i += 1
    return clauses


def _normalize_when_clause(line: str) -> str:
    """Normalize a WHEN clause for identity comparison.

    Strips markdown punctuation, collapses whitespace, and lower-cases.
    Unlike ``_normalize_for_comparison``, this preserves short tokens
    (e.g. 'x', 'z') and does NOT strip role words or stop words.
    """
    cleaned = re.sub(r"[^\w\s]", " ", line)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip().lower()


def _is_spec_path(path: str) -> bool:
    """Return True when path matches se3/specs/**/spec.md."""
    normalized = path.replace("\\", "/")
    return bool(_SPEC_PATH_RE.match(normalized))


def _get_changed_spec_files(project_root: Path, base_ref: str, head_ref: str) -> list[str]:
    """Get list of spec files changed between base_ref and head_ref."""
    if not base_ref or not head_ref:
        raise ValueError(
            f"Cannot diff spec files: empty ref "
            f"(base_ref={base_ref!r}, head_ref={head_ref!r})"
        )
    result = _run_git(
        project_root, "diff", "--name-only", f"{base_ref}..{head_ref}",
        check=False, timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    changed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [p for p in changed if _is_spec_path(p)]


def _read_file_from_ref(project_root: Path, rel_path: str, ref: str) -> str | None:
    """Read file content from a git ref. Returns None if unavailable."""
    result = _run_git(
        project_root, "show", f"{ref}:{rel_path}",
        check=False, timeout=15,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _check_spec_file_against_ref(
    project_root: Path,
    rel_path: str,
    old_ref: str,
    new_ref: str,
) -> list[GuardrailViolation]:
    """Check a single spec file comparing two git refs.

    Args:
        project_root: Path to the project root.
        rel_path: Relative path to the spec file.
        old_ref: Git ref for the original version.
        new_ref: Git ref for the new version (or "WORKTREE" for working tree).

    Returns:
        List of guardrail violations.
    """
    # Get original content
    original_content = _read_file_from_ref(project_root, rel_path, old_ref)
    if original_content is None:
        # File didn't exist in old ref — no guardrail to enforce
        return []

    # Get new content
    if new_ref == "WORKTREE":
        full_path = project_root / rel_path
        if not full_path.exists():
            return [GuardrailViolation(
                file_path=rel_path,
                violation_type="DELETE",
                message="Spec file was deleted in merge",
            )]
        try:
            new_content = full_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # H5: Don't silently absorb file-read failures.  Log the
            # specific exception type and return an INCOMPLETE marker so
            # the report is flagged as not fully verified.
            logger.warning(
                "Could not read spec file %s (%s): %s",
                rel_path, type(exc).__name__, exc,
            )
            return [GuardrailViolation(
                file_path=rel_path,
                violation_type="CHECK_INCOMPLETE",
                message=(
                    f"Spec file could not be read for guardrails check: "
                    f"{type(exc).__name__}: {exc}"
                ),
                evidence=_evidence_dict(
                    exception_type=type(exc).__name__,
                    exception_msg=str(exc),
                ),
            )]
    else:
        new_content = _read_file_from_ref(project_root, rel_path, new_ref)
        if new_content is None:
            # File was deleted in new ref
            return [GuardrailViolation(
                file_path=rel_path,
                violation_type="DELETE",
                message="Spec file was deleted in merge",
            )]

    return check_spec_diff(original_content, new_content, file_path=rel_path)


def _check_merge_topology(
    project_root: Path,
    pre_sha: str,
    post_sha: str,
    *,
    min_parents: int = 2,
    timeout: int = 15,
) -> list[GuardrailViolation]:
    """Verify that ``post_sha`` is a valid merge result built on ``pre_sha``.

    This catches a class of disasters where the merge commit was silently
    dropped (e.g. ``git reset --soft HEAD~1`` on an amended merge) — the
    spec content might match the expected post-merge text, but the
    underlying commit is no longer a merge or is no longer a descendant
    of the pre-merge HEAD.

    Two checks:

    1. **Ancestry**: ``pre_sha`` must be an ancestor of ``post_sha`` (i.e.
       ``post_sha`` is a descendant of ``pre_sha``).  If not, the merge
       commit was lost.

    2. **Parent count**: ``post_sha`` must have at least ``min_parents``
       parents (defaults to 2).  Octopus merges (>2 parents) are accepted
       — a 3-parent merge has parents >= 2.

    The no-op already-ancestor case (``pre_sha == post_sha``) is handled
    by the caller; this function still returns a violation in that case
    if invoked, because a same-commit pair cannot satisfy the merge-commit
    requirement.

    Returns:
        List of CHECK_FAILURE violations.  Empty when the topology is
        valid.
    """
    violations: list[GuardrailViolation] = []
    if not pre_sha or not post_sha:
        return violations  # Caller already validated; nothing to check.

    # Check 1: ancestry — pre must be an ancestor of post.
    try:
        ancestry_result = _run_git(
            project_root, "merge-base", "--is-ancestor", pre_sha, post_sha,
            check=False, timeout=timeout,
        )
    except Exception as exc:
        violations.append(GuardrailViolation(
            file_path="N/A",
            violation_type="CHECK_FAILURE",
            message=(
                f"Topology check failed: could not run "
                f"`git merge-base --is-ancestor {pre_sha[:8]} {post_sha[:8]}`: {exc}"
            ),
        ))
        return violations

    if ancestry_result.returncode != 0:
        violations.append(GuardrailViolation(
            file_path="N/A",
            violation_type="CHECK_FAILURE",
            message=(
                f"Merge topology violation: pre-merge SHA {pre_sha[:8]} is "
                f"NOT an ancestor of post-merge SHA {post_sha[:8]}. "
                f"The merge commit may have been lost (e.g. `git reset --soft HEAD~1` "
                f"after amending the merge)."
            ),
            evidence=_evidence_dict(
                pre_sha=pre_sha,
                post_sha=post_sha,
                topology_check="ancestry",
            ),
        ))
        # If ancestry fails, the parent-count check is meaningless because
        # we are likely on a disconnected commit graph.  Return early.
        return violations

    # Check 2: parent count — post must be a merge commit (>= min_parents).
    try:
        parents_result = _run_git(
            project_root, "rev-list", "--parents", "-n", "1", post_sha,
            check=False, timeout=timeout,
        )
    except Exception as exc:
        violations.append(GuardrailViolation(
            file_path="N/A",
            violation_type="CHECK_FAILURE",
            message=(
                f"Topology check failed: could not run "
                f"`git rev-list --parents -n 1 {post_sha[:8]}`: {exc}"
            ),
        ))
        return violations

    if parents_result.returncode != 0:
        violations.append(GuardrailViolation(
            file_path="N/A",
            violation_type="CHECK_FAILURE",
            message=(
                f"Topology check failed: `git rev-list --parents -n 1 "
                f"{post_sha[:8]}` returned {parents_result.returncode}: "
                f"{parents_result.stderr.strip()}"
            ),
        ))
        return violations

    parts = parents_result.stdout.strip().split()
    # parts[0] is post_sha itself; parts[1:] are the parents.
    parent_count = max(0, len(parts) - 1)
    if parent_count < min_parents:
        violations.append(GuardrailViolation(
            file_path="N/A",
            violation_type="CHECK_FAILURE",
            message=(
                f"Merge topology violation: post-merge commit {post_sha[:8]} "
                f"has {parent_count} parent(s), expected >= {min_parents}. "
                f"HEAD is not a merge commit — the merge may have been "
                f"squashed, fast-forwarded, or replaced by a single-parent commit."
            ),
            evidence=_evidence_dict(
                post_sha=post_sha,
                parent_count=parent_count,
                min_parents=min_parents,
                topology_check="parent_count",
            ),
        ))

    return violations


class MergeGuardrailsCheck:
    """Check merged spec files against guardrails."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def check(
        self,
        ours_before_sha: str,
        merge_commit_sha: str,
    ) -> GuardrailReport:
        """Check spec files changed in the merge for guardrail violations.

        .. deprecated::
            Use :meth:`check_merge_result` instead for proper ref-based
            comparison. This method is kept for backward compatibility
            and compares against the working tree.

        Args:
            ours_before_sha: The SHA of HEAD before the merge started.
            merge_commit_sha: The SHA of the merge commit (or current HEAD).

        Returns:
            GuardrailReport with pass/fail status and any violations.
        """
        return self.check_merge_result(ours_before_sha, merge_commit_sha)

    def check_merge_result(
        self,
        ours_before_sha: str,
        merge_commit_sha: str,
        *,
        enforce_topology: bool = True,
    ) -> GuardrailReport:
        """Check spec files changed between two commits for violations.

        Lists the merge commit's touched ``se3/specs/**/spec.md`` files,
        fetches the pre-merge HEAD version and the merge-commit version,
        and runs :func:`check_spec_diff` on each.

        Also performs **merge topology validation** (H1/H2):

          * ``merge_commit_sha`` must be a descendant of ``ours_before_sha``;
          * ``merge_commit_sha`` must have at least 2 parents (octopus merges
            with more parents are accepted).

        The topology check is skipped when ``ours_before_sha ==
        merge_commit_sha`` (already-ancestor no-op path); the orchestrator
        normally filters that case out before calling us.  Tests that want
        to exercise only the spec-diff logic (without setting up a real
        merge commit) can pass ``enforce_topology=False`` to skip the
        topology assertions.

        Args:
            ours_before_sha: The SHA of HEAD before the merge started.
            merge_commit_sha: The SHA of the merge commit.
            enforce_topology: When True (default) verify the merge
                topology.  When False, skip the topology check — only
                the spec-diff content checks run.  Tests that exercise
                the spec-diff path with non-merge commits SHOULD set
                this to False to avoid spurious CHECK_FAILURE
                violations.

        Returns:
            GuardrailReport with pass/fail status and any violations.
        """
        if not ours_before_sha or not merge_commit_sha:
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="N/A",
                        violation_type="CHECK_FAILURE",
                        message=(
                            f"Guardrails check failed: missing ref "
                            f"(ours_before_sha={ours_before_sha!r}, "
                            f"merge_commit_sha={merge_commit_sha!r})"
                        ),
                    ),
                ],
            )

        violations: list[GuardrailViolation] = []
        incomplete = False

        # H1/H2 — merge topology check.  Skip when pre == post (already-ancestor
        # no-op path); the orchestrator filters that case out before calling us.
        if enforce_topology and ours_before_sha != merge_commit_sha:
            topology_violations = _check_merge_topology(
                self.project_root, ours_before_sha, merge_commit_sha,
            )
            violations.extend(topology_violations)

        spec_files = _get_changed_spec_files(
            self.project_root, ours_before_sha, merge_commit_sha,
        )
        if not spec_files and not violations:
            return GuardrailReport(passed=True)

        for rel_path in spec_files:
            try:
                file_violations = _check_spec_file_against_ref(
                    self.project_root, rel_path, ours_before_sha, merge_commit_sha,
                )
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                # H5: Per-file iteration error must NOT silently abort the
                # overall check.  Log, mark the report incomplete, and
                # continue with the remaining files.
                logger.warning(
                    "Per-file guardrails check failed for %s (%s): %s",
                    rel_path, type(exc).__name__, exc,
                )
                violations.append(GuardrailViolation(
                    file_path=rel_path,
                    violation_type="CHECK_INCOMPLETE",
                    message=(
                        f"Spec file iteration error: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    evidence=_evidence_dict(
                        exception_type=type(exc).__name__,
                        exception_msg=str(exc),
                    ),
                ))
                incomplete = True
                continue
            for v in file_violations:
                if v.violation_type == "CHECK_INCOMPLETE":
                    incomplete = True
            violations.extend(file_violations)

        return GuardrailReport(
            passed=len(violations) == 0,
            violations=violations,
            incomplete=incomplete,
        )
