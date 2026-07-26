"""SE3 spec structural validator.

Pure-function module that validates whether a spec.md file conforms to
the spec-format v1 contract. Used by:

* ``sync_discovery`` — reject newly generated specs that are actually
  sub-agent meta summaries instead of real spec content.
* ``sync_engine`` — verify written-back content from sub-agent
  responses before refreshing the in-memory cache.
* ``luo sync --validate-only`` CLI — manual audit of all on-disk specs.

The module has no external dependencies (stdlib only) so it stays
cheap to import from any layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


V1_MARKER = "<!-- spec-format: v1 -->"

# Narrative-prefix patterns indicating sub-agent prose rather than a
# spec body. Lowercased before comparison; both English and common
# Chinese variants are listed.
_NARRATIVE_PREFIXES = (
    "i ",
    "i'",
    "i,",
    "i.",
    "created ",
    "here ",
    "here's",
    "here is",
    "let me ",
    "the spec",
    "this spec",
    "我",
    "已经",
    "让我",
)


@dataclass
class ValidationResult:
    """Outcome of :func:`validate_spec_structure`.

    Attributes:
        passed: True iff every check passed.
        errors: Human-readable error strings; empty when ``passed`` is
            True. Order matches the order checks are performed.
    """

    passed: bool
    errors: List[str] = field(default_factory=list)


def validate_spec_structure(content: str, spec_name: str) -> ValidationResult:
    """Validate a spec.md body against the spec-format v1 structural rules.

    Checks (in order; all are reported, the function never short-circuits):

    1. First non-blank line MUST be the ``<!-- spec-format: v1 -->`` marker.
    2. The next non-blank line after the marker MUST be a level-1 heading
       ``# <spec_name> Specification`` (spec_name comparison is
       case-insensitive on the spec name token).
    3. The body MUST contain a ``## Purpose`` section whose body is non-empty.
    4. The body MUST contain at least one ``### Requirement:`` section.
    5. The first non-comment, non-heading, non-blank line MUST NOT start
       with a narrative-prose phrase (``I ``/``Created``/``Here``/
       ``Let me``/``The spec``/Chinese ``我``/``已经``/``让我`` …).

    The function never raises; malformed input degrades to a result with
    one or more error strings.
    """
    errors: List[str] = []

    if not isinstance(content, str):
        return ValidationResult(passed=False, errors=["content is not a string"])

    raw_lines = content.splitlines()

    # ------------------------------------------------------------------
    # Rule 1: first non-blank line is the v1 marker
    # ------------------------------------------------------------------
    first_idx = _first_non_blank(raw_lines, 0)
    if first_idx is None:
        return ValidationResult(passed=False, errors=["spec is empty"])

    if raw_lines[first_idx].strip() != V1_MARKER:
        errors.append(
            f"first non-blank line is not the v1 marker '{V1_MARKER}' "
            f"(found: {raw_lines[first_idx].strip()!r})"
        )
        # Treat the whole file as the body for rules 2-5.
        body_start = first_idx
    else:
        body_start = first_idx + 1

    # ------------------------------------------------------------------
    # Rule 2: '# <spec_name> Specification' heading follows the marker
    # ------------------------------------------------------------------
    # Skip leading header comment lines (e.g. the ``<!-- domain: <path> -->``
    # metadata marker that lives alongside the v1 marker) so a spec that
    # declares a domain in its header is not mistaken for a missing title.
    title_idx = _first_non_blank_noncomment(raw_lines, body_start)
    if title_idx is None:
        errors.append(
            f"no '# {spec_name} Specification' heading found"
        )
    else:
        title_line = raw_lines[title_idx].strip()
        if not _is_valid_title(title_line, spec_name):
            errors.append(
                f"first heading after the v1 marker is not "
                f"'# {spec_name} Specification' (found: {title_line!r})"
            )

    # ------------------------------------------------------------------
    # Rule 3: '## Purpose' section with non-empty body
    # ------------------------------------------------------------------
    purpose_idx = _find_heading(raw_lines, "## Purpose")
    if purpose_idx is None:
        errors.append("missing '## Purpose' section")
    else:
        if not _section_has_body(raw_lines, purpose_idx):
            errors.append("'## Purpose' section is empty")

    # ------------------------------------------------------------------
    # Rule 4: at least one '### Requirement:' section
    # ------------------------------------------------------------------
    if not _has_requirement(raw_lines):
        errors.append("no '### Requirement:' section found")

    # ------------------------------------------------------------------
    # Rule 5: first non-comment, non-heading line is not narrative prose
    # ------------------------------------------------------------------
    first_prose = _first_prose_line(raw_lines)
    if first_prose is not None and _looks_narrative(first_prose):
        errors.append(
            f"spec body starts with a narrative phrase rather than "
            f"structured content (line: {first_prose!r})"
        )

    return ValidationResult(passed=not errors, errors=errors)


def extract_spec_body(text: str, spec_name: str) -> str:
    """Slice the markdown spec body out of an agentic sub-agent output.

    Sub-agent stdout frequently carries narrative preamble ("I have enough
    context…"), tool-process chatter, and only then the actual spec
    document. This helper drops everything before the first structural
    anchor so the downstream :func:`validate_spec_structure` gate sees a
    clean spec body instead of prose.

    Anchor precedence:

    1. The v1 marker line ``<!-- spec-format: v1 -->``.
    2. A ``# <spec_name> Specification`` level-1 heading (case-insensitive,
       token-tolerant — same rule as :func:`_is_valid_title`).
    3. The first level-1 ``# `` heading of any kind (fallback).

    When no anchor is found the text is returned unchanged so the validator
    can reject it on its own terms. The function never raises and performs
    no I/O — it is a pure transform over ``(text, spec_name)``.
    """
    if not isinstance(text, str) or not text:
        return text

    # Keep line endings so a matched anchor's character offset can slice the
    # original string verbatim (no trailing-newline loss, no line-ending
    # normalization).
    lines = text.splitlines(keepends=True)

    offset = _line_offset(lines, lambda s: s == V1_MARKER)
    if offset is None:
        offset = _line_offset(
            lines,
            lambda s: s.startswith("# ") and _is_valid_title(s, spec_name),
        )
    if offset is None:
        offset = _line_offset(lines, lambda s: s.startswith("# "))

    if offset is None:
        # No structural anchor — leave the text for the validator to reject.
        return text
    return text[offset:]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _line_offset(lines_keepends: List[str], predicate) -> int | None:
    """Return the character offset of the first line whose stripped form
    satisfies ``predicate``, or ``None`` if no line matches.

    ``lines_keepends`` must be produced by ``str.splitlines(keepends=True)``
    so the cumulative length of preceding lines is the exact byte/char offset
    into the original string.
    """
    offset = 0
    for line in lines_keepends:
        if predicate(line.strip()):
            return offset
        offset += len(line)
    return None


def _first_non_blank(lines: List[str], start: int) -> int | None:
    for idx in range(start, len(lines)):
        if lines[idx].strip():
            return idx
    return None


def _first_non_blank_noncomment(lines: List[str], start: int) -> int | None:
    """First non-blank line that is also not a self-contained HTML comment.

    Header markers such as ``<!-- spec-format: v1 -->`` and
    ``<!-- domain: <path> -->`` are single-line HTML comments that may precede
    the spec title; this helper skips them (and blank lines) so the title search
    lands on the real ``# <name> Specification`` heading.
    """
    for idx in range(start, len(lines)):
        stripped = lines[idx].strip()
        if not stripped:
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        return idx
    return None


def _is_valid_title(line: str, spec_name: str) -> bool:
    """Return True if ``line`` is a valid ``# <spec_name> Specification``.

    Comparison is case-insensitive. The trailing word "Specification" is
    required. The spec_name match is tolerant: any single dash-separated
    token of ``spec_name`` appearing in the title (case-insensitive) is
    accepted, so legacy human-authored titles such as
    ``# SE3 Version Management Specification`` for the
    ``se3-versioning`` spec still validate.
    """
    if not line.startswith("# "):
        return False
    inner = line[2:].strip()
    if not inner.lower().endswith("specification"):
        return False
    inner_lower = inner.lower()
    if spec_name.lower() in inner_lower:
        return True
    # Token-level fallback for legacy titles.
    tokens = [t for t in spec_name.lower().split("-") if t]
    if not tokens:
        return True
    return any(t in inner_lower for t in tokens)


def _find_heading(lines: List[str], heading: str) -> int | None:
    target = heading.strip()
    for idx, line in enumerate(lines):
        if line.strip() == target:
            return idx
    return None


def _section_has_body(lines: List[str], heading_idx: int) -> bool:
    """Return True if the section starting at ``heading_idx`` has any
    non-blank, non-heading content before the next heading or EOF."""
    for line in lines[heading_idx + 1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return False
        return True
    return False


def _has_requirement(lines: List[str]) -> bool:
    for line in lines:
        if line.strip().startswith("### Requirement:"):
            return True
    return False


def _first_prose_line(lines: List[str]) -> str | None:
    """Return the first non-blank, non-comment, non-heading line content."""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("<!--"):
            continue
        if stripped.startswith("#"):
            continue
        return stripped
    return None


def _looks_narrative(line: str) -> bool:
    lowered = line.lstrip().lower()
    for prefix in _NARRATIVE_PREFIXES:
        if lowered.startswith(prefix):
            return True
    # Bare "I" word (e.g. "I will...", "I'll...") — already covered by
    # ``i ``/``i'`` prefixes above. We additionally guard "I" with no
    # following character.
    if lowered.rstrip() == "i":
        return True
    return False
