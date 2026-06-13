"""Spec format v1 parser and validator.

Provides `parse_spec()` to decompose a spec markdown file into a structured
`ParsedSpec` (shared header + list of `Requirement` items), and `validate()`
to check conformance to format v1 rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

SPEC_FORMAT_VERSION = "v1"
SPEC_FORMAT_VERSION_MARKER = "<!-- spec-format: v1 -->"

# Requirement boundary: exactly `### Requirement: <name>`
_REQUIREMENT_HEADER_RE = re.compile(r"^###\s+Requirement:\s*(.*)$", re.MULTILINE)

# Tags / keywords lines inside a Requirement block
# e.g. `**tags**: foo, bar` or `**Tags**: foo, bar`
_TAGS_RE = re.compile(r"^\*\*tags\*\*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_KEYWORDS_RE = re.compile(r"^\*\*keywords\*\*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)

# Reference patterns:
# 1. Intra-spec: `Requirement: <name>` followed by a terminator
#    (punctuation, closing bracket, or end-of-line).
#    Excludes boundary headers and prose like "`Requirement: <name>` — ..."
# 2. Inter-spec: `<spec>::<requirement>`
# Both name segments use Unicode-aware \w (includes CJK, etc.) plus space/hyphen.
_INTRA_REF_RE = re.compile(
    r"Requirement:\s+([\w\-]+(?:\s+[\w\-]+)*)"
    r"(?!\s*::)"  # negative lookahead: don't match if followed by :: (inter-spec form)
    r"(?:[\.,;:\)\]\n]|$)"
)
# Inter-spec reference: <spec>::<requirement>
# Spec name segment stays ASCII (it's a directory name).
# Requirement name uses Unicode-aware \w (includes CJK, etc.) plus space/hyphen.
# The leading ``(?<![a-zA-Z0-9_\-])`` anchors the spec-name token to the start of
# a contiguous run of spec-name characters. Without it, a long run of word
# characters NOT followed by ``::`` (e.g. a multi-KB Requirement body) makes the
# greedy ``[a-zA-Z0-9_\-]+`` re-consume the whole run at every starting offset,
# which is O(n^2) catastrophic backtracking (an 40 KB body costs ~8s). The
# look-behind lets a match start ONLY at a run boundary, so each run is scanned
# once — O(n) — while preserving identical matches (the leftmost greedy match
# always begins at a non-spec-char boundary anyway).
_INTER_REF_RE = re.compile(r"(?<![a-zA-Z0-9_\-])([a-zA-Z0-9_\-]+)::([\w\- ]+)")

# Common English stop-words that signal the end of a reference name
# when it appears inside prose (e.g. "see Requirement: Foo for details").
_REF_STOP_WORDS = frozenset(
    {"for", "and", "when", "the", "is", "to", "of", "in", "on", "at", "by",
     "with", "from", "as", "that", "this", "it", "or", "but", "not", "are",
     "was", "were", "be", "been", "have", "has", "had", "do", "does", "did"}
)
# Maximum words in a reference name (prevents runaway capture in prose)
_REF_MAX_WORDS = 8

# Illegal characters in Requirement name (control chars, newlines)
_ILLEGAL_NAME_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# Overly-deep headings (###### or more — ##### is allowed for Scenarios)
_DEEP_HEADING_RE = re.compile(r"^######+\s+", re.MULTILINE)

# Fenced code block delimiter
_CODE_FENCE_RE = re.compile(r"^```\w*\s*$", re.MULTILINE)


@dataclass
class Issue:
    """A validation issue found in a parsed spec."""

    severity: str  # "error" | "warning"
    message: str
    location: str  # e.g. "Requirement: Foo" or "header" or "line 42"


@dataclass
class Requirement:
    """A single Requirement item within a spec."""

    name: str
    body: str
    tags: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    refs: List[str] = field(default_factory=list)
    line_start: int = 0


@dataclass
class ParsedSpec:
    """Result of parsing a spec file."""

    header_text: str
    requirements: List[Requirement]
    has_v1_marker: bool = False
    # Orphan H2 headings found between Requirements (content-loss hazards).
    # Each tuple is (heading_text, line_number).
    orphan_h2s: List[tuple[str, int]] = field(default_factory=list)
    # Text that appears after the last Requirement body (orphan sections).
    # Preserved so loaders can include it even in items mode.
    trailing_text: str = ""
    # Line numbers of deep headings (###### or more) found in the original text.
    deep_heading_lines: List[int] = field(default_factory=list)


def _find_code_block_ranges(text: str) -> list[tuple[int, int]]:
    """Find (start, end) character ranges of fenced code blocks.

    Matches ``` delimiters. Returns empty list if no code blocks found.
    An unclosed block (odd number of fences) is treated as extending to EOF.
    """
    ranges: list[tuple[int, int]] = []
    starts = [m.start() for m in _CODE_FENCE_RE.finditer(text)]
    # Pair up start/end delimiters
    i = 0
    while i + 1 < len(starts):
        ranges.append((starts[i], starts[i + 1]))
        i += 2
    # Unclosed block: last fence opens a block to EOF
    if len(starts) % 2 == 1:
        ranges.append((starts[-1], len(text)))
    return ranges


def _is_inside_code_block(pos: int, ranges: list[tuple[int, int]]) -> bool:
    """Check if a character position falls inside any code block range."""
    for start, end in ranges:
        if start < pos < end:
            return True
    return False


def _is_inside_table_row(text: str, pos: int) -> bool:
    """Check if position falls inside a markdown table row.

    A table row starts with ``|`` and contains at least one more ``|``
    (so there are at least two columns).
    """
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end]
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _split_values(text: str) -> List[str]:
    """Split a comma-separated tag/keyword string, trimming whitespace.

    Returns an empty list for empty/whitespace-only input.
    """
    if not text or not text.strip():
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _clean_ref_name(name: str) -> str:
    """Truncate an extracted reference name at stop-words or word limit.

    Prevents over-capture when a reference appears inside prose
    (e.g. "see Requirement: Foo Bar for details" → "Foo Bar").

    Returns the cleaned name, or empty string if nothing remains.
    """
    words = name.split()
    if not words:
        return ""
    cleaned: list[str] = []
    for word in words[:_REF_MAX_WORDS]:
        # Stop at the first stop-word (case-insensitive)
        if word.lower().rstrip(".,;:)") in _REF_STOP_WORDS:
            break
        cleaned.append(word)
    return " ".join(cleaned)


def _extract_refs(body: str) -> List[str]:
    """Extract literal references from a Requirement body.

    Recognizes two forms:
    - Intra-spec: `Requirement: <name>` (excluding the boundary header)
    - Inter-spec: `<spec>::<requirement>`

    Code blocks (fenced ```) are skipped to avoid extracting references
    from examples/templates inside code fences.

    Returns a deduplicated list of reference strings.
    """
    refs: List[str] = []
    seen: set[str] = set()

    # Skip references inside fenced code blocks
    code_ranges = _find_code_block_ranges(body)

    # Intra-spec references: lines containing "Requirement: <name>"
    # We skip the first occurrence because that's the boundary header
    # itself, but since we operate on the body *after* stripping the
    # boundary, any "Requirement:" inside the body is a reference.
    for match in _INTRA_REF_RE.finditer(body):
        if _is_inside_code_block(match.start(), code_ranges):
            continue
        if _is_inside_table_row(body, match.start()):
            continue
        ref = _clean_ref_name(match.group(1))
        if ref and ref not in seen:
            seen.add(ref)
            refs.append(ref)

    # Inter-spec references: spec::requirement
    for match in _INTER_REF_RE.finditer(body):
        if _is_inside_code_block(match.start(), code_ranges):
            continue
        if _is_inside_table_row(body, match.start()):
            continue
        ref = f"{match.group(1)}::{_clean_ref_name(match.group(2))}"
        if ref.endswith("::"):
            continue
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)

    return refs


def _extract_tags_and_keywords(body: str) -> tuple[List[str], List[str]]:
    """Extract tags and keywords from a Requirement body.

    Looks for lines matching `**tags**:` and `**keywords**:`.
    Uses *findall* so that multiple occurrences (e.g. merge artifacts)
    are all captured and concatenated.

    Returns (tags, keywords).
    """
    tags: List[str] = []
    keywords: List[str] = []

    for m in _TAGS_RE.finditer(body):
        tags.extend(_split_values(m.group(1)))

    for m in _KEYWORDS_RE.finditer(body):
        keywords.extend(_split_values(m.group(1)))

    return tags, keywords


# H2 heading regex — exactly `## ` (two hashes + space), NOT `### ` or deeper.
_H2_HEADING_RE = re.compile(r"^##\s+", re.MULTILINE)


def parse_spec(text: str) -> ParsedSpec:
    """Parse a spec markdown file into structured components.

    Args:
        text: Raw markdown content of the spec file.

    Returns:
        ParsedSpec containing the shared header and a list of Requirements.
    """
    original_text = text

    # Detect v1 marker from first non-whitespace line
    stripped = text.lstrip()
    has_v1_marker = stripped.startswith(SPEC_FORMAT_VERSION_MARKER)

    # Track line offset caused by marker removal so line_start remains
    # accurate against the original file.
    header_line_offset = 0
    text_start_offset = 0  # Position in original_text where `text` begins
    if has_v1_marker:
        marker_pos = text.find(SPEC_FORMAT_VERSION_MARKER)
        after_marker_end = marker_pos + len(SPEC_FORMAT_VERSION_MARKER)
        after_marker = text[after_marker_end:]
        stripped_newlines = len(after_marker) - len(after_marker.lstrip("\r\n"))
        # Lines removed from the beginning = newlines before the first
        # remaining character in the original text.
        header_line_offset = original_text[
            : marker_pos + len(SPEC_FORMAT_VERSION_MARKER) + stripped_newlines
        ].count("\n")
        text_start_offset = after_marker_end + stripped_newlines

        # Slice past the marker and any immediately following newlines.
        # Slicing avoids replace() accidentally hitting a literal marker
        # occurrence inside code samples later in the document.
        text = text[text_start_offset:]

    # Split into header and requirement blocks using the boundary regex
    # Skip requirement headers that appear inside fenced code blocks
    code_ranges = _find_code_block_ranges(text)
    all_matches = list(_REQUIREMENT_HEADER_RE.finditer(text))
    matches = [m for m in all_matches if not _is_inside_code_block(m.start(), code_ranges)]

    # Find all H2 headings (## ) that are NOT inside code blocks.
    # These act as Requirement body terminators per the v1 spec.
    h2_positions: list[int] = []
    for m in _H2_HEADING_RE.finditer(text):
        if not _is_inside_code_block(m.start(), code_ranges):
            h2_positions.append(m.start())

    if not matches:
        # No requirements found — entire text is the header
        return ParsedSpec(
            header_text=text.strip(),
            requirements=[],
            has_v1_marker=has_v1_marker,
        )

    # Header is everything before the first Requirement boundary
    first_match = matches[0]
    header_text = text[: first_match.start()].strip()

    requirements: List[Requirement] = []
    body_ends: List[int] = []

    for i, match in enumerate(matches):
        name = match.group(1).strip()
        body_start = match.end() + 1  # skip past the newline after the header

        if i + 1 < len(matches):
            next_req_start = matches[i + 1].start()
        else:
            next_req_start = len(text)

        # Body terminates at the earlier of: next ### Requirement:, next ## heading, EOF
        body_end = next_req_start
        for h2_pos in h2_positions:
            if h2_pos > match.start() and h2_pos < body_end:
                body_end = h2_pos
                break  # h2_positions is sorted by finditer

        body = text[body_start:body_end].strip()
        line_start = text[: match.start()].count("\n") + 1 + header_line_offset

        tags, keywords = _extract_tags_and_keywords(body)
        refs = _extract_refs(body)

        requirements.append(
            Requirement(
                name=name,
                body=body,
                tags=tags,
                keywords=keywords,
                refs=refs,
                line_start=line_start,
            )
        )
        body_ends.append(body_end)

    # Detect orphan H2s: H2 headings that fall in the gap between a
    # Requirement's body end and the next Requirement (or EOF).
    # These sections are lost in items mode and are a content-loss hazard.
    orphan_h2s: List[tuple[str, int]] = []
    for i in range(len(matches)):
        if i + 1 < len(matches):
            gap_end = matches[i + 1].start()
        else:
            gap_end = len(text)
        gap_start = body_ends[i]
        for h2_pos in h2_positions:
            if h2_pos > gap_start and h2_pos < gap_end:
                line_end = text.find("\n", h2_pos)
                if line_end == -1:
                    line_end = len(text)
                heading_line = text[h2_pos:line_end].strip()
                # Compute line number relative to the original file text
                line_num = original_text[:h2_pos + text_start_offset].count("\n") + 1
                if heading_line not in [h for h, _ in orphan_h2s]:
                    orphan_h2s.append((heading_line, line_num))

    # Capture trailing text after the last requirement body
    trailing_text = ""
    if body_ends:
        trailing = text[body_ends[-1]:].strip()
        if trailing:
            trailing_text = trailing

    # Find deep heading (###### or more) line numbers in the original text
    deep_heading_lines: List[int] = []
    code_ranges_orig = _find_code_block_ranges(original_text)
    for m in _DEEP_HEADING_RE.finditer(original_text):
        if not _is_inside_code_block(m.start(), code_ranges_orig):
            line_num = original_text[:m.start()].count("\n") + 1
            deep_heading_lines.append(line_num)

    return ParsedSpec(
        header_text=header_text,
        requirements=requirements,
        has_v1_marker=has_v1_marker,
        orphan_h2s=orphan_h2s,
        trailing_text=trailing_text,
        deep_heading_lines=deep_heading_lines,
    )


def validate(parsed: ParsedSpec) -> List[Issue]:
    """Validate a parsed spec against format v1 rules.

    Checks performed:
    - error: Duplicate Requirement names within the spec
    - error: Requirement name contains illegal characters
    - error: Nesting level beyond v1 allowed range (##### or deeper)
    - warning: Missing ## Purpose section in header
    - warning: Missing v1 format marker

    Args:
        parsed: The ParsedSpec to validate.

    Returns:
        List of Issue objects, each with severity, message, and location.
    """
    issues: List[Issue] = []

    # Check for v1 marker
    if not parsed.has_v1_marker:
        issues.append(
            Issue(
                severity="warning",
                message="Spec does not declare a format version (missing v1 marker)",
                location="header",
            )
        )

    # Check for ## Purpose in header
    if "## Purpose" not in parsed.header_text and "## purpose" not in parsed.header_text.lower():
        issues.append(
            Issue(
                severity="warning",
                message="Header is missing a ## Purpose section",
                location="header",
            )
        )

    # Check for orphan H2 headings between Requirements (content-loss hazard)
    if parsed.orphan_h2s:
        for heading, line_num in parsed.orphan_h2s:
            issues.append(
                Issue(
                    severity="warning",
                    message=f"Orphan H2 heading between Requirements: '{heading}' — content under this heading is not attached to any Requirement and is lost in items mode",
                    location=f"line {line_num}",
                )
            )

    # Check for overly-deep headings anywhere in the spec
    if parsed.deep_heading_lines:
        line_num = parsed.deep_heading_lines[0]
        issues.append(
            Issue(
                severity="error",
                message="Heading nesting exceeds v1 allowed range (###### or deeper is not permitted)",
                location=f"line {line_num}",
            )
        )

    # Check Requirement names
    seen_names: set[str] = set()
    for req in parsed.requirements:
        # Empty name
        if not req.name or not req.name.strip():
            line_num = req.line_start if req.line_start else "?"
            issues.append(
                Issue(
                    severity="error",
                    message="Requirement name is empty (found '### Requirement:' with no name)",
                    location=f"line {line_num}",
                )
            )
            continue

        # Illegal characters
        if _ILLEGAL_NAME_CHARS_RE.search(req.name):
            issues.append(
                Issue(
                    severity="error",
                    message=f"Requirement name contains illegal characters: {repr(req.name)}",
                    location=f"Requirement: {req.name}",
                )
            )

        # Duplicate names
        if req.name in seen_names:
            issues.append(
                Issue(
                    severity="error",
                    message=f"Duplicate Requirement name: '{req.name}'",
                    location=f"Requirement: {req.name}",
                )
            )
        else:
            seen_names.add(req.name)

    return issues
