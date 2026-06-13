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
from typing import Dict, Iterable, List, Optional, Tuple

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

# A single domain path component may carry only these characters once
# normalized. The domain path is interpolated UNQUOTED into drill commands such
# as ``se3 spec index <group>`` and is split on ``/`` by the CLI, so a component
# must never contain whitespace or shell-significant characters — otherwise a
# domain like ``engine steps`` would render as ``se3 spec index engine steps``,
# which the CLI reads as two arguments and can never resolve back to the stored
# single-component group.
_DOMAIN_COMPONENT_ALLOWED_RE = re.compile(r"[^a-z0-9._-]+")


def normalize_domain(domain: object) -> str:
    """Normalize an LLM-supplied domain path into safe slash-separated parts.

    The result is lowercase, ``/``-separated, and every component is restricted
    to ``[a-z0-9._-]`` with internal whitespace / illegal characters collapsed to
    a single ``-``. Empty components (leading/trailing/duplicate slashes) are
    dropped. A value that normalizes to nothing usable (non-string, blank, or
    only illegal characters) returns ``""`` — the caller then leaves the spec
    un-marked and the renderer groups it under :data:`UNCLASSIFIED_GROUP`.

    Pure and deterministic; mirrors the navigation/render layer's "no LLM"
    guarantee so a malformed domain can never reach persistence or be
    interpolated unquoted into a drill command.
    """
    if not isinstance(domain, str):
        return ""
    parts: List[str] = []
    for raw in domain.strip().lower().split("/"):
        comp = _DOMAIN_COMPONENT_ALLOWED_RE.sub("-", raw.strip())
        comp = re.sub(r"-{2,}", "-", comp).strip("-.")
        if comp:
            parts.append(comp)
    return "/".join(parts)


def normalize_spec_name(name: object) -> str:
    """Normalize an LLM-supplied spec name into a safe, flat kebab directory name.

    A spec name addresses a single ``se3/specs/<name>/spec.md`` directory and is
    interpolated unquoted into that filesystem path and into ``<name>::<req>``
    logical addresses. It MUST therefore be a single flat component — no path
    separators, no ``..`` traversal, no whitespace, and only ``[a-z0-9._-]``.

    Unlike :func:`normalize_domain` (which preserves ``/`` to express a layered
    path), this collapses every illegal character — including ``/`` — to a single
    ``-`` and flattens the whole value to one component, so a value such as
    ``"engine/merge-internals"`` (the LLM confusing the spec name with a domain
    path) becomes ``"engine-merge-internals"`` and can only ever create a flat
    ``se3/specs/engine-merge-internals/spec.md`` directory that the one-level
    index and ``_all_spec_texts()`` probes can see. A value that normalizes to
    nothing usable (non-string, blank, only illegal characters, or only dots —
    e.g. ``".."``) returns ``""``; the caller then refuses the split.

    Pure and deterministic; mirrors the navigation/render layer's "no LLM"
    guarantee so a malformed name can never reach the filesystem or be
    interpolated into a path or address.
    """
    if not isinstance(name, str):
        return ""
    comp = _DOMAIN_COMPONENT_ALLOWED_RE.sub("-", name.strip().lower())
    comp = re.sub(r"-{2,}", "-", comp).strip("-.")
    return comp


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
    domain = normalize_domain(domain)
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

_H2_HEADING_RE = re.compile(r"^##\s+")


def _h2_heading_lines(text: str) -> List[int]:
    """1-based line numbers of level-2 (``## ``) headings, skipping code fences.

    A ``### Requirement:`` heading is level-3 (``###``) and is NOT matched, so the
    result contains only the genuine ``## `` shared / orphan / trailing section
    headings that bound a Requirement block per the spec-format v1 contract.
    """
    out: List[int] = []
    in_fence = False
    for idx, line in enumerate(text.splitlines()):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _H2_HEADING_RE.match(line):
            out.append(idx + 1)
    return out


def _requirement_intervals(text: str) -> List[Tuple[str, int, int]]:
    """Return ``(name, line_start, line_end)`` (1-based, inclusive) per Requirement.

    Mirrors the interval computation in :mod:`se3.engine.spec_index` and the
    spec-format v1 body-boundary rule: a Requirement block extends from its
    ``### Requirement:`` heading up to (but not including) the next
    ``### Requirement:`` heading, the next ``## `` (level-2) heading, OR EOF —
    whichever comes first. Bounding at an intervening ``## `` heading is what
    keeps an orphan / trailing section (e.g. a final ``## Appendix``, or a
    ``## Notes`` block between two Requirements) OUT of the moved block, so
    relocating a Requirement never silently drags an unrelated shared section
    with it (and never deletes it from the source spec).
    """
    parsed = parse_spec(text)
    reqs = parsed.requirements
    total_lines = len(text.splitlines())
    h2_lines = _h2_heading_lines(text)
    out: List[Tuple[str, int, int]] = []
    for i, req in enumerate(reqs):
        if i + 1 < len(reqs):
            next_bound = reqs[i + 1].line_start - 1
        else:
            next_bound = total_lines
        # A ``## `` heading appearing after this Requirement's heading terminates
        # its block (the level-2 heading is not part of any Requirement).
        for h2_line in h2_lines:
            if h2_line > req.line_start:
                next_bound = min(next_bound, h2_line - 1)
                break
        end = max(req.line_start, next_bound)
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
    norm_domain = normalize_domain(domain)
    if norm_domain:
        out.append(f"{DOMAIN_MARKER_PREFIX} {norm_domain} {DOMAIN_MARKER_SUFFIX}")
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


def _continuation_is_longer_name(
    req_name: str, tail: str, known_names: Optional[Iterable[str]]
) -> bool:
    """Decide whether a matched address/name is a strict prefix of a longer name.

    *tail* is the text immediately following the matched ``req_name`` (the caller's
    regex has already excluded an immediate name-char / hyphen continuation, so
    *tail* begins with a non-name character — a space, punctuation, or EOL).

    A longer name is recognised two ways, and a match by EITHER means the shorter
    rewrite MUST NOT fire:

    1. **Capitalization heuristic** — a space followed by a Title-Case / numeric
       continuation word (``base::Auth`` is a prefix of ``base::Auth Token``).
       This protects a longer name that is not in *known_names* (e.g. a referenced
       requirement that lives in another spec).
    2. **Known-name match (capitalization-independent)** — *tail* spells out the
       remainder of a longer requirement name actually present in *known_names*
       (the indexed requirement names). This catches a lowercase continuation such
       as ``base::Foo bar`` (where ``Foo bar`` is a distinct indexed requirement)
       that the capitalization heuristic alone would miss and corrupt.
    """
    if re.match(r" [A-Z0-9]", tail):
        return True
    for k in (known_names or ()):
        # A longer name must extend ``req_name`` across a space-delimited word
        # boundary; ``k.startswith(req_name + " ")`` excludes both an equal name
        # and a same-prefix-no-space collision (``Foo``/``Foobar``).
        if k == req_name or not k.startswith(req_name + " "):
            continue
        if tail.startswith(k[len(req_name):]):
            return True
    return False


def rewrite_moved_refs(
    text: str,
    moves: Dict[Tuple[str, str], Tuple[str, str]],
    known_reqs: Optional[Dict[str, Iterable[str]]] = None,
) -> str:
    """Relink inter-spec ``<spec>::<requirement>`` addresses after a move.

    *moves* maps ``(old_spec, requirement)`` → ``(new_spec, requirement)``. Every
    literal ``old_spec::requirement`` reference in *text* is rewritten to
    ``new_spec::requirement`` so cross-spec references survive the relocation.
    The requirement name is the stable item identity, so relinking is by logical
    address and tolerates ordinary prose following the address.

    The match is substring-by-address but **prefix-collision guarded**: a move
    fires only when ``old_addr`` is NOT a strict prefix of a *longer* requirement
    name at the same address. A longer name is detected by the character that
    immediately follows the address — a name character / hyphen
    (``Auth`` → ``Authentication``) is excluded at the regex level. A space-led
    continuation is then evaluated by :func:`_continuation_is_longer_name`, which
    distinguishes a complete logical address from a prefix **using the actual
    indexed requirement names** in *known_reqs* (a per-spec map ``{spec_name:
    [requirement names]}``) regardless of capitalization — so a distinct
    ``base::Foo bar`` is preserved when only ``Foo`` is moved — falling back to a
    Title-Case / numeric heuristic when *known_reqs* is absent. Without this guard
    a blind ``str.replace`` of ``base::Auth`` would silently corrupt a distinct
    ``base::Auth Token`` reference. Prose following an address (``... lives here``,
    ``... and ...``) begins with a lowercase word that is not a known longer name,
    so legitimate relinks still fire.
    """
    for (old_spec, old_req), (new_spec, new_req) in moves.items():
        old_addr = f"{old_spec}::{old_req}"
        new_addr = f"{new_spec}::{new_req}"
        if old_addr == new_addr:
            continue
        names = None if known_reqs is None else known_reqs.get(old_spec)
        # (?![\w\-]) — not extended by a name char / hyphen. The space-led
        # continuation is evaluated in the callback against the known names.
        pattern = re.compile(re.escape(old_addr) + r"(?![\w\-])")

        def _repl(m: "re.Match[str]", _new=new_addr, _req=old_req, _names=names) -> str:
            tail = m.string[m.end():]
            if _continuation_is_longer_name(_req, tail, _names):
                return m.group(0)
            return _new

        text = pattern.sub(_repl, text)
    return text


def _rewrite_one_intra_ref(
    text: str,
    req_name: str,
    new_spec: str,
    known_names: Optional[Iterable[str]] = None,
) -> str:
    """Rewrite intra-spec ``Requirement: <req_name>`` refs to ``<new_spec>::<req_name>``.

    Matches the same intra-spec reference shape the parser recognizes
    (``Requirement:`` followed by the exact requirement name) while being
    **boundary-guarded** identically to :func:`rewrite_moved_refs`: a match fires
    only when the name is not extended by a further name char / hyphen or by a
    longer requirement name. The longer-name decision uses the actual indexed
    *known_names* regardless of capitalization (falling back to a Title-Case /
    numeric heuristic when absent), so ``Requirement: Foo`` is never rewritten when
    the real reference is a longer ``Requirement: Foo Bar`` or ``Requirement: Foo
    bar``.

    The ``### Requirement: <name>`` boundary heading is left untouched: a match
    whose line prefix (the text before ``Requirement:`` on its line) begins with
    ``#`` is a heading, not a prose reference, and is skipped.
    """
    pattern = re.compile(
        r"Requirement:\s+" + re.escape(req_name) + r"(?![\w\-])"
    )

    def _repl(m: "re.Match[str]") -> str:
        src = m.string
        tail = src[m.end():]
        if _continuation_is_longer_name(req_name, tail, known_names):
            return m.group(0)
        line_start = src.rfind("\n", 0, m.start()) + 1
        prefix = src[line_start:m.start()]
        if prefix.lstrip().startswith("#"):
            # This is a `### Requirement:` boundary header, not a reference.
            return m.group(0)
        return f"{new_spec}::{req_name}"

    return pattern.sub(_repl, text)


def relink_intra_spec_refs(
    text: str,
    new_home: str,
    final_location: Dict[str, str],
    known_reqs: Optional[Iterable[str]] = None,
) -> str:
    """Relink intra-spec ``Requirement: <name>`` refs that cross a relocation boundary.

    *text* is content authored as part of a single source spec whose post-move
    home spec is *new_home* (either the trimmed source spec itself, or a moved
    Requirement block now living in a different spec). *final_location* maps every
    Requirement name originally declared in the source spec to the spec where it
    lives **after** the relocation.

    For each name whose ``final_location[name] != new_home``, the intra-spec
    reference ``Requirement: <name>`` no longer resolves inside *new_home* (the
    target is now in another spec), so it is rewritten to the inter-spec
    ``<final_location[name]>::<name>`` form. This keeps 1-hop reference expansion
    resolving after a migration / split for both directions:

    - a reference in the trimmed source spec pointing at a Requirement that moved
      out (``new_home == source``, ``final_location[name] == target``), and
    - a reference inside a moved block pointing back at a Requirement that stayed
      (``new_home == target``, ``final_location[name] == source``).

    Names whose final location equals *new_home* (they stayed local, or moved in
    alongside this block) keep the intra-spec form. Pure and deterministic.

    *known_reqs* is the set of indexed requirement names of the source spec, used
    to distinguish a complete intra-spec reference from a prefix of a longer name
    regardless of capitalization (see :func:`_continuation_is_longer_name`); it
    defaults to the keys of *final_location* (every source-spec requirement name)
    when not supplied.
    """
    known = known_reqs if known_reqs is not None else list(final_location.keys())
    for name, loc in final_location.items():
        if loc == new_home or not name:
            continue
        text = _rewrite_one_intra_ref(text, name, loc, known_names=known)
    return text


def requirement_names(text: str) -> List[str]:
    """Return the ordered ``### Requirement:`` names declared in *text*."""
    return [name for name, _s, _e in _requirement_intervals(text)]
