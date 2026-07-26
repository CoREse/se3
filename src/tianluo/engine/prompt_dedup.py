"""Prompt line-level deduplication for LLM calls.

Provides a generic utility to deduplicate repeated contiguous blocks
of lines within a prompt string, replacing subsequent occurrences with
a reference marker pointing to the first occurrence.
"""

from __future__ import annotations

import hashlib

_MARKER_PREFIX = "[DUPLICATED CONTENT:"


def deduplicate_prompt_lines(prompt: str, min_block_lines: int = 3) -> str:
    """Deduplicate repeated contiguous line blocks in a prompt.

    Uses fingerprint-based lookup for efficient matching (O(n) average case).
    Scans the prompt for contiguous blocks of >= ``min_block_lines`` identical
    lines that appear more than once.  The first occurrence is kept verbatim;
    subsequent occurrences are replaced with a marker referencing the original
    line range (1-indexed).

    Lines that are existing dedup markers (from prior dedup passes) and
    blocks consisting entirely of blank lines are excluded from dedup.

    Args:
        prompt: The full prompt text.
        min_block_lines: Minimum number of consecutive identical lines to
            qualify as a duplicated block.  Defaults to 3.

    Returns:
        The prompt with duplicate blocks replaced by markers.
    """
    if not prompt:
        return prompt

    if min_block_lines < 1:
        return prompt

    lines = prompt.split("\n")
    n = len(lines)

    if n < min_block_lines:
        return prompt

    # replaced[i] is True if line i has been replaced by a marker or removed
    replaced = [False] * n
    # insertions: mapping from line index -> marker text to insert
    insertions: dict[int, str] = {}

    # Fingerprint index: tuple of min_block_lines consecutive lines -> first position
    fingerprints: dict[tuple[str, ...], int] = {}

    i = 0
    while i <= n - min_block_lines:
        if replaced[i]:
            i += 1
            continue

        # Skip lines that are existing dedup markers (from prior passes)
        if lines[i].startswith(_MARKER_PREFIX):
            i += 1
            continue

        # Check if any line in the fingerprint window is already replaced
        window_valid = True
        for k in range(min_block_lines):
            if replaced[i + k]:
                window_valid = False
                break
        if not window_valid:
            i += 1
            continue

        # Build fingerprint from min_block_lines consecutive lines
        fp = tuple(lines[i + k] for k in range(min_block_lines))

        # Skip blocks consisting entirely of blank lines
        if all(line.strip() == "" for line in fp):
            i += 1
            continue

        if fp not in fingerprints:
            fingerprints[fp] = i
            i += 1
            continue

        src = fingerprints[fp]
        # Verify source lines haven't been replaced since registration
        source_valid = True
        for k in range(min_block_lines):
            if replaced[src + k]:
                source_valid = False
                break
        if not source_valid:
            # Update fingerprint to current position (new valid source)
            fingerprints[fp] = i
            i += 1
            continue

        # Extend match beyond the initial min_block_lines.
        # Guard: src + match_len < i prevents the source range from
        # overlapping with the duplicate range (needed for adjacent blocks).
        match_len = min_block_lines
        while (
            src + match_len < i
            and i + match_len < n
            and not replaced[src + match_len]
            and not replaced[i + match_len]
            and lines[src + match_len] == lines[i + match_len]
        ):
            match_len += 1

        # Replace lines[i : i + match_len] with a content-based marker.
        # Use first/last line + a short content hash for stable identification.
        # The hash disambiguates blocks that share the same first and last line
        # but differ in the middle (partial overlap scenario).
        # No positional reference — line numbers become stale across retries
        # when new history entries are prepended, which would confuse the LLM.
        block_content = "\n".join(lines[src : src + match_len])
        content_hash = hashlib.sha256(block_content.encode()).hexdigest()[:8]
        first_line = lines[src].strip()[:80]
        last_line = lines[src + match_len - 1].strip()[:80]
        marker = f"[DUPLICATED CONTENT: {match_len} lines #{content_hash}, from \"{first_line}\" to \"{last_line}\"]"
        insertions[i] = marker
        for k in range(i, i + match_len):
            replaced[k] = True

        # Note: advancing by match_len means fingerprints for windows that
        # partially overlap the replaced range are never registered.  This is
        # correct: any content within the replaced range is already eliminated
        # as part of a larger dedup, so there is nothing left to match against.
        # A different block whose first occurrence was entirely within the
        # replaced range has no visible first occurrence remaining, and its
        # duplicate (if any) will be the only visible copy — no dedup needed.
        #
        # The fingerprint for `fp` still points at `src` in the dict.  If a
        # third occurrence of the same block appears later, the source-validity
        # check (lines 89-98) will detect that `src` lines are still intact
        # and produce a valid replacement referencing the same source — no
        # fingerprint update is needed here.
        i += match_len

    # Build output
    result_lines: list[str] = []
    for idx in range(n):
        if idx in insertions:
            result_lines.append(insertions[idx])
        elif not replaced[idx]:
            result_lines.append(lines[idx])

    return "\n".join(result_lines)
