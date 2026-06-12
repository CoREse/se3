"""Program-level (no-LLM) governance operations for ``se3 sync``.

This module implements the *mechanism* behind the spec volume-governance
refactors that ``se3 sync`` performs: moving module-level content out of the
``base`` spec, splitting an over-sized multi-topic spec into a parallel spec,
and backfilling missing ``<!-- domain: ... -->`` header metadata.

The split between *decision* and *mechanism* is deliberate:

- The **decision** (which Requirements violate the base admission standard and
  where they belong, or which cluster of Requirements should become a parallel
  spec) is a content judgement made by the LLM and confirmed by the user through
  ``se3 sync`` 's respond channel.
- The **mechanism** — cutting a Requirement block from one spec, pasting it into
  another, relinking the logical ``<spec>::<requirement>`` cross-references,
  constructing the new parallel spec body, and inserting the domain marker — is
  a set of **pure functions over spec text**. They never invoke an LLM, so they
  are deterministic and unit-testable, mirroring the navigation/render layer's
  "no LLM" guarantee.

The numeric thresholds live in :class:`se3.config.SpecGovernanceConfig`; the
normative prompt text lives in :mod:`se3.engine.spec_governance`. This module
carries only the deterministic transforms and the small dataclasses describing
a migration / split proposal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .spec_format import parse_spec
from .spec_governance import DOMAIN_MARKER_PREFIX, DOMAIN_MARKER_SUFFIX

_V1_MARKER = "<!-- spec-format: v1 -->"
_DOMAIN_RE = re.compile(r"<!--\s*domain:\s*(.*?)\s*-->", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Proposal dataclasses (carried in the respond-channel call files)
# ---------------------------------------------------------------------------

@dataclass
class BaseMigration:
    """A single ``base`` → module-spec relocation of one Requirement."""

    requirement_name: str
    target_spec: str
    item_id: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "item_id": self.item_id,
            "requirement_name": self.requirement_name,
            "target_spec": self.target_spec,
        }


@dataclass
class SplitProposal:
    """A proposal to split a cluster of Requirements into a parallel spec."""

    source_spec: str
    new_spec: str
    requirement_names: List[str] = field(default_factory=list)
    domain: Optional[str] = None
    purpose: str = ""
    rationale: str = ""
    item_id: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "item_id": self.item_id,
            "source_spec": self.source_spec,
            "new_spec": self.new_spec,
            "requirement_names": list(self.requirement_names),
            "domain": self.domain,
            "purpose": self.purpose,
            "rationale": self.rationale,
        }


# ---------------------------------------------------------------------------
# domain marker helpers
# ---------------------------------------------------------------------------

def domain_of(text: str) -> Optional[str]:
    """Return the ``<!-- domain: <path> -->`` value in *text*, or ``None``."""
    m = _DOMAIN_RE.search(text)
    if not m:
        return None
    value = m.group(1).strip()
    return value or None


def has_domain_marker(text: str) -> bool:
    """True when *text* declares a non-empty domain header marker."""
    return domain_of(text) is not None


def ensure_domain_marker(text: str, domain: str) -> str:
    """Return *text* with a ``<!-- domain: <domain> -->`` marker present.

    Replaces an existing domain marker in place; otherwise inserts the marker
    immediately after the ``<!-- spec-format: v1 -->`` line (or at the very top
    when no v1 marker exists). Pure — does not touch disk. An empty *domain*
    string is a no-op (returns *text* unchanged), so a caller that has no domain
    to assign leaves the spec un-marked (the renderer groups it under the
    "(未分类)" bucket).
    """
    domain = (domain or "").strip()
    if not domain:
        return text
    marker = f"{DOMAIN_MARKER_PREFIX} {domain} {DOMAIN_MARKER_SUFFIX}"

    if _DOMAIN_RE.search(text):
        return _DOMAIN_RE.sub(marker, text, count=1)

    if not text:
        return marker + "\n"

    lines = text.splitlines(keepends=True)
    insert_at = 0
    for i, line in enumerate(lines):
        if _V1_MARKER in line:
            insert_at = i + 1
            break
    # Preserve the surrounding newline style by reusing "\n".
    lines.insert(insert_at, marker + "\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# Requirement block cut / paste
# ---------------------------------------------------------------------------

def _requirement_intervals(text: str) -> List[Tuple[str, int, int]]:
    """Return ``(name, line_start, line_end)`` (1-based, inclusive) per Requirement.

    Mirrors the interval computation in :mod:`se3.engine.spec_index` so the
    sliced block text is byte-consistent with how the index addresses items.
    """
    parsed = parse_spec(text)
    reqs = parsed.requirements
    total_lines = len(text.splitlines())
    out: List[Tuple[str, int, int]] = []
    for i, req in enumerate(reqs):
        if i + 1 < len(reqs):
            end = max(req.line_start, reqs[i + 1].line_start - 1)
        else:
            end = max(req.line_start, total_lines)
        out.append((req.name, req.line_start, end))
    return out


def _normalize_blank_runs(text: str) -> str:
    """Collapse 3+ consecutive blank lines to a single blank line; trailing LF."""
    collapsed = re.sub(r"\n{3,}", "\n\n", text)
    return collapsed.rstrip("\n") + "\n"


def split_out_requirements(
    text: str, names: List[str]
) -> Tuple[str, Dict[str, str]]:
    """Cut the named ``### Requirement:`` blocks out of *text*.

    Returns ``(remaining_text, {name: block_text})``. The block text preserves
    the original formatting verbatim (heading + body), and ``remaining_text`` is
    the spec with those blocks removed and excess blank runs collapsed. Names not
    present in *text* are silently absent from the returned dict (no error).
    The header (everything before the first Requirement) and any trailing text
    are preserved.
    """
    name_set = set(names)
    intervals = _requirement_intervals(text)
    lines = text.splitlines()

    blocks: Dict[str, str] = {}
    remove: set[int] = set()
    for name, start, end in intervals:
        if name in name_set and name not in blocks:
            block = "\n".join(lines[start - 1:end]).strip("\n")
            blocks[name] = block
            for ln in range(start, end + 1):
                remove.add(ln)

    kept = [lines[i] for i in range(len(lines)) if (i + 1) not in remove]
    remaining = _normalize_blank_runs("\n".join(kept))
    return remaining, blocks


def append_requirements(text: str, blocks: List[str]) -> str:
    """Append Requirement *blocks* to the end of spec *text*, blank-separated."""
    parts = [text.rstrip("\n")]
    for block in blocks:
        b = block.strip("\n")
        if b:
            parts.append(b)
    return "\n\n".join(parts).rstrip("\n") + "\n"


def build_parallel_spec(
    spec_name: str,
    blocks: List[str],
    *,
    domain: Optional[str] = None,
    purpose: str = "",
) -> str:
    """Construct a brand-new, structurally valid ``spec.md`` body for a split.

    Emits the v1 marker, an optional domain marker, the ``# <spec_name>
    Specification`` title, a ``## Purpose`` section (with a one-sentence locator),
    and a ``## Requirements`` section carrying the moved Requirement *blocks*.
    The output passes ``validate_spec_structure`` when at least one non-empty
    block is supplied.
    """
    locator = (purpose or "").strip() or (
        f"{spec_name} — split out of a larger spec by `se3 sync` to keep each "
        f"spec single-topic."
    )
    out: List[str] = [_V1_MARKER]
    if domain and domain.strip():
        out.append(f"{DOMAIN_MARKER_PREFIX} {domain.strip()} {DOMAIN_MARKER_SUFFIX}")
    out.append("")
    out.append(f"# {spec_name} Specification")
    out.append("")
    out.append("## Purpose")
    out.append("")
    out.append(locator)
    out.append("")
    out.append("## Requirements")
    out.append("")
    for block in blocks:
        b = block.strip("\n")
        if b:
            out.append(b)
            out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def rewrite_moved_refs(
    text: str, moves: Dict[Tuple[str, str], Tuple[str, str]]
) -> str:
    """Relink inter-spec ``<spec>::<requirement>`` addresses after a move.

    *moves* maps ``(old_spec, requirement)`` → ``(new_spec, requirement)``. Every
    literal ``old_spec::requirement`` reference in *text* is rewritten to
    ``new_spec::requirement`` so cross-spec references survive the relocation.
    Pure literal replacement; the requirement name is the stable item identity.
    """
    for (old_spec, old_req), (new_spec, new_req) in moves.items():
        old_addr = f"{old_spec}::{old_req}"
        new_addr = f"{new_spec}::{new_req}"
        if old_addr != new_addr:
            text = text.replace(old_addr, new_addr)
    return text


def requirement_names(text: str) -> List[str]:
    """Return the ordered ``### Requirement:`` names declared in *text*."""
    return [name for name, _s, _e in _requirement_intervals(text)]
