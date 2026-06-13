"""Size-bounded, deterministic rendering of the spec index for LLM consumption.

This module is the **rendering layer** sitting on top of the navigation layer
(:mod:`se3.engine.spec_index`). It turns the flat, persisted index into
size-bounded *views* a single LLM context can hold:

- **root view** — every spec's name + one-sentence locator + item count. When the
  rendered output exceeds the threshold it is folded, ``domain`` path level by
  level, into navigation group handles; specs lacking a ``<!-- domain: -->``
  marker fold under the ``(未分类)`` group.
- **spec view** — the item index of one spec (``<spec>::<requirement>`` address,
  title, summary, tags, refs). Over threshold it folds first by the spec's
  ``## `` chapter outline: a spec organised into two or more chapters collapses
  its largest chapter into a ``[group] <spec>/<chapter>`` handle drilled via
  ``se3 spec index <spec> sN``. A single-chapter spec — or a chapter still too
  large once drilled — falls back to deterministic ``<spec>[ sN]/pN`` pagination
  (*结构耗尽时以确定性分页兜底*), so semantic structure is always exhausted before
  pages appear. Even those pages stay self-describing: each ``[page]`` handle
  carries the requirement-name span (first … last) of the items behind it,
  derived mechanically from the ``###`` requirement outline, so no page is
  opaque.
- **group view** — drilling into any group handle (a deeper ``domain`` path, a
  ``sN`` chapter, or a ``pN`` page) via a multi-level group path.

Two hard invariants:

1. **Everything is mechanical.** Summaries, locators, domain grouping and byte
   counting are all read straight from the index — **no LLM is ever invoked**
   from this module. Given the same index and threshold, the same input always
   produces byte-identical output (deterministic greedy folding: largest
   foldable unit first, ties broken by lexicographic name).
2. **item identity is preserved.** An item entry always carries its full
   ``<spec>::<requirement>`` address; a group/page handle never carries a ``::``
   address — it carries only the exact ``se3 spec index`` command that drills
   into it. This is the rendering half of the *item 标识不变式* (machine
   guarantee a): the renderer makes items and navigation handles
   unmistakably distinct.

Every view is self-describing: the header states the command to read one item
(``se3 spec show``) and the command to drill a handle (``se3 spec index``), so an
LLM needs no external knowledge to navigate.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .spec_governance import UNCLASSIFIED_GROUP
from .spec_index import SpecIndex, _NO_REQUIREMENTS_SENTINEL

try:
    from ..config import DEFAULT_INDEX_RENDER_THRESHOLD
except Exception:  # pragma: no cover - defensive; config should always import
    DEFAULT_INDEX_RENDER_THRESHOLD = 16384


# NOTE on page tokens (``p`` + 1-based index, e.g. ``p1`` / ``p2``): whether a
# *root* path component is a domain or a page is NOT decided by token shape — the
# domain namespace is authoritative there and is resolved against the actual
# stored domain tree by :func:`_split_root_group_path`, so a domain literally
# named like a page handle (``p1``, or a nested ``engine/p1``) stays navigable.
# Because the domain namespace wins, a generated root ``pN`` page handle whose
# token coincides with a real sibling domain (paginating domain ``engine``'s
# direct specs into ``p1`` while a ``engine/p1`` subgroup also exists) would be
# shadowed by that domain when the page command is parsed back. Root pagination
# therefore emits the :data:`_ROOT_PAGE_SENTINEL` just before its ``pN`` tokens
# so the page namespace stays reachable regardless of any same-named domain.

# A chapter-group token used by the spec view: ``s`` followed by a 1-based
# index, e.g. ``s1`` / ``s2``. It selects one ``## `` chapter group; trailing
# ``pN`` tokens then paginate within that chapter.
_SECTION_TOKEN_RE = re.compile(r"^s\d+$")

# Extra margin (bytes) added to the header reserve when packing pages, covering
# the slightly longer header a deeper drill path produces, so a page's rendered
# output (header + slice) stays under the threshold once drilled.
_HEADER_MARGIN = 160
# Never shrink a page budget below this, so a tiny threshold still makes progress.
_MIN_BUDGET = 120
# Hard cap on super-pagination grouping rounds (a runaway-loop backstop set far
# above any realistic spec size).
_MAX_PAGE_LEVELS = 24
# Fixed fan-out used to guarantee the page count shrinks each super-pagination
# round even when the threshold is too small to pack 2+ handles by size (an
# infeasible-threshold safety valve that keeps the handle list log-bounded).
_PAGE_FANOUT = 8
# A ``## `` chapter holding more than this many Requirements is split into
# deterministic name-range subgroups derived from the ``### `` requirement
# outline, so a flat spec (every Requirement under a single ``## Requirements``
# chapter — the common se3 layout) still yields semantic, name-labelled groups
# before anonymous ``pN`` pagination. Structure (the requirement outline) is
# exhausted first; pagination is the fallback within an over-threshold subgroup.
_SECTION_SUBDIVIDE_SIZE = 12

# Reserved sentinel that prefixes a root-level *domain* drill path whenever the
# domain's first component collides with a spec name. At the first positional of
# ``se3 spec index``, the spec namespace shadows the domain namespace
# (:func:`render_index` routes a known spec name to its item view), so a domain
# literally named after a spec — e.g. the ``server`` domain alongside the
# ``server`` spec — would be unreachable by the bare ``se3 spec index server``
# command (that opens the spec). Prefixing such a domain's drill command with
# this sentinel keeps the domain group reachable; :func:`render_index` and
# :func:`render_root_view` strip it before resolving the domain path. It carries
# ``@`` so it can never collide with a kebab-case spec/domain component, and it
# matches neither the ``pN`` nor the ``sN`` token patterns.
_ROOT_DOMAIN_SENTINEL = "@domain"

# Reserved sentinel that switches a root drill path from the *domain* namespace
# to the *page* namespace. The domain namespace is authoritative at root
# (:func:`_split_root_group_path` consumes domain prefixes greedily), so a
# generated ``pN`` pagination handle whose token coincides with a real domain
# component — e.g. paginating domain ``engine``'s direct specs into ``p1`` while
# a sibling subgroup ``engine/p1`` also exists — would otherwise be shadowed by
# that domain when the page command is parsed back. Root pagination therefore
# emits this sentinel just before its ``pN`` tokens; :func:`_split_root_group_path`
# treats it as an explicit "everything after me is a page token" switch and drops
# the sentinel itself. It carries ``@`` so it can never collide with a kebab-case
# domain component, and matches neither the ``pN`` nor the ``sN`` token patterns.
_ROOT_PAGE_SENTINEL = "@page"


def _blen(text: str) -> int:
    """Byte length under UTF-8 (specs and group names may hold CJK text)."""
    return len(text.encode("utf-8"))


def _byte_truncate(text: str, limit: int) -> str:
    """Truncate *text* to at most *limit* UTF-8 bytes without splitting a char.

    Used as the last-resort guarantee that even a degenerate output (a threshold
    smaller than the irreducible navigation header) still honours the configured
    positive byte bound. Backs off to the nearest valid UTF-8 boundary so a
    multibyte character is never cut in half.
    """
    if limit <= 0:
        return ""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    cut = data[:limit]
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""


# Notice appended when a rendered view is hard-truncated to honour the byte
# threshold. This is the last-resort guarantee that *every* output — root,
# group, drilled page, or a single oversized leaf item — stays within the
# configured threshold even when no further folding/pagination can shrink it
# (e.g. one item whose tags/refs alone exceed the budget, or a threshold
# configured smaller than the fixed self-describing header).
_TRUNCATION_NOTICE = "\n# ... [output truncated to fit the byte threshold]\n"

# The minimal *complete navigation path*: just the two essential commands (read
# one item / drill a handle). When the full self-describing header would itself
# blow the configured threshold, the renderer falls back to this compact header
# so a tiny threshold still yields output near the irreducible navigation floor
# instead of the full multi-line help. It deliberately keeps both command
# strings verbatim so the LLM can always navigate; the floor is the byte size of
# these two lines — a threshold below it cannot be honoured without severing a
# navigation command, so the floor wins over the cap in that degenerate case.
_COMPACT_NAV_HEADER = (
    "#   se3 spec show <spec>::<requirement>\n"
    "#   se3 spec index <spec> [<group>...]\n"
)

# The irreducible navigation floor: the byte size of the compact navigation
# header. A render threshold below this cannot be honoured without severing one
# of the two essential commands mid-line, leaving a view that is bounded but no
# longer self-describing. Configuration clamps ``index_render_threshold`` up to
# this floor so every emitted view stays BOTH bounded and navigable; this module
# is the single source of truth for the value (config imports it).
MIN_RENDER_THRESHOLD = _blen(_COMPACT_NAV_HEADER)


def _split_header_body(text: str) -> Tuple[str, str]:
    """Split a rendered view into its ``#``-prefixed header and the body.

    The self-describing header is a run of leading lines that all start with
    ``#`` (see :func:`_header`); the body (item entries / group / page handles)
    never starts a line with ``#``. Returns ``(header, body)`` where *body* may
    be empty.
    """
    lines = text.split("\n")
    idx = 0
    while idx < len(lines) and lines[idx].startswith("#"):
        idx += 1
    header = "\n".join(lines[:idx])
    body = "\n".join(lines[idx:])
    return header, body


def _body_units(body: str) -> List[str]:
    """Group *body* lines into navigable units (one entry or one handle each).

    A unit begins at a line that does not start with whitespace (an item
    address ``- spec::req``, a ``[group]``/``[page]`` handle, or a ``(...)``
    placeholder) and absorbs the following indented continuation lines (the
    ``    → se3 spec index …`` drill command, an item summary, tags/refs). Whole
    units are kept or dropped together so truncation can never sever a handle's
    drill command or an item's address from its body.
    """
    units: List[str] = []
    cur: List[str] = []
    for line in body.split("\n"):
        starts_unit = bool(line) and not line[0].isspace()
        if starts_unit and cur:
            units.append("\n".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        units.append("\n".join(cur))
    return units


def _is_item_unit(unit: str) -> bool:
    """Whether *unit*'s first line is a leaf item address (``- <spec>::<req>``).

    Distinguishes a terminal, selectable item entry from a navigation handle
    (``[group]`` / ``[page]``) or a root spec entry (``- <spec>  (N items)``,
    which carries no ``::`` address). Only true item units are force-compacted by
    :func:`_enforce_threshold` so a drilled leaf never loses its address.
    """
    first = unit.split("\n", 1)[0]
    return first.startswith("- ") and "::" in first


def _compact_unit(unit: str, budget: int) -> str:
    """Compact a single navigation *unit* toward *budget* bytes, never losing its
    anchor.

    Used when no whole unit fits the residual byte budget yet content must still
    be shown — most commonly a single oversized leaf item whose summary / tags /
    refs (or a very long requirement name) alone blow the threshold. Dropping the
    unit wholesale would strip the only selectable ``<spec>::<requirement>``
    address (or a handle's drill command), breaking drill-down to that leaf.

    The *essential* lines are always emitted intact even when they alone exceed
    *budget* (navigability wins over the byte cap in this degenerate case): the
    unit's first line (an item address ``- spec::req`` or a ``[group]``/``[page]``
    label) plus any ``→ se3 spec index …`` drill-command line, so an item keeps
    its address and a handle keeps both its label and its command. Remaining
    metadata lines (summary, tags/refs) are appended in document order only while
    they fit, so the result is deterministic.
    """
    lines = unit.split("\n")
    if not lines:
        return unit
    essential = {0}
    for i, line in enumerate(lines):
        if line.lstrip().startswith("→"):
            essential.add(i)
    kept = set(essential)
    # Newline glue counted between the essential lines already chosen.
    size = sum(_blen(lines[i]) for i in essential) + max(0, len(essential) - 1)
    for i, line in enumerate(lines):
        if i in kept:
            continue
        add = _blen(line) + 1  # newline glue
        if size + add > budget:
            continue
        kept.add(i)
        size += add
    return "\n".join(lines[i] for i in sorted(kept))


def _oversized_leaf_substitute(address_line: str) -> str:
    """A bounded, NON-selectable placeholder for a single leaf whose
    ``<spec>::<requirement>`` address alone exceeds the residual byte budget.

    The bounded-context invariant requires every rendered view to stay within the
    threshold, so an over-long literal address line cannot be emitted intact. But
    the item-identity invariant equally forbids substituting a *different*
    selection: the previous behaviour offered ``<spec>::*`` (the whole-spec
    wildcard), which silently turned an exact single-item selection into a
    select-everything-in-this-spec selection — a broader, different-semantics
    route that could load unrelated or collectively oversized content. We must
    NOT do that.

    Since a Requirement *name* longer than the entire threshold cannot be exposed
    by any threshold-bounded textual view, this placeholder is deliberately
    **non-selectable** (it starts with ``(`` and carries no ``- <spec>::…`` leaf
    line and no ``<spec>::*`` wildcard). It states the truth — the exact item
    exists but cannot be addressed at the current threshold — and gives the
    bounded resolution route: split the over-long Requirement (this is a
    ≤8 KiB-per-Requirement writing-discipline violation that ``se3 guardrails``
    flags) or raise ``spec_governance.index_render_threshold`` and re-drill. A
    truncated, explicitly non-selectable preview names which item it is. Because
    the placeholder is non-selectable, the engine-side out-of-line validation
    (item-identity machine guarantee c) correctly rejects any attempt to select
    it, instead of admitting a broadened whole-spec selection.
    """
    raw = address_line[2:] if address_line.startswith("- ") else address_line
    spec = raw.split("::", 1)[0].strip()
    preview = raw if _blen(raw) <= 80 else _byte_truncate(raw, 80) + "…"
    return (
        f"(unselectable: an oversized Requirement in '{spec}' whose full "
        f"<spec>::<requirement> address exceeds the byte threshold and so cannot "
        f"be rendered as a selectable leaf at the current "
        f"spec_governance.index_render_threshold. It is NOT selected by "
        f"'{spec}::*' (that would select the entire spec, not this item). "
        f"Resolution: split this Requirement (it violates the ≤8 KiB-per-"
        f"Requirement writing discipline and is flagged by se3 guardrails), or "
        f"raise spec_governance.index_render_threshold, then re-run se3 spec "
        f"index to drill again. preview (NOT a selectable address): {preview})"
    )


def _enforce_threshold(text: str, threshold: int) -> str:
    """Cap *text* near *threshold* bytes while keeping the view navigable.

    Folding and pagination keep output bounded in the common case; this is the
    final mechanical guarantee for the residual cases they cannot shrink (a
    threshold smaller than the fixed self-describing header, or an irreducible
    oversized leaf). Unlike a raw byte chop, this NEVER cuts the header or a
    navigation unit in half where avoidable: a self-describing header (carrying
    the ``se3 spec show`` / ``se3 spec index`` commands) is always emitted intact,
    and only whole trailing body units are dropped, so whatever survives is
    still structurally complete and navigable.

    A single leaf whose ``<spec>::<requirement>`` address alone overruns the
    residual budget is NOT force-emitted over the threshold, and is NOT replaced
    by a broader ``<spec>::*`` whole-spec selector (which would change an exact
    single-item selection into selecting every Requirement in the spec). Instead
    it is replaced by a bounded, NON-selectable placeholder (see
    :func:`_oversized_leaf_substitute`) that previews the item and points to the
    resolution, so the view stays within the byte bound while never broadening
    the selection semantics. The context-bound invariant — every single output
    stays ``<= threshold`` for any positive threshold — therefore holds for every
    view without exception.

    Threshold compliance: when the full multi-line header would blow the
    threshold, the renderer drops to the compact navigation header
    (:data:`_COMPACT_NAV_HEADER`, the two essential commands only), which keeps
    the output as close to the threshold as possible. The invariant is honoured
    even below the irreducible navigation floor: when even the compact header
    overflows the threshold it is hard-truncated to the byte bound (on a valid
    UTF-8 boundary), so a tiny configured threshold can never produce an
    over-budget view.
    """
    if threshold <= 0 or _blen(text) <= threshold:
        return text

    header, body = _split_header_body(text)
    notice = _TRUNCATION_NOTICE

    # If the full self-describing header alone (plus the truncation notice)
    # cannot fit, fall back to the compact navigation header so we don't emit
    # the full multi-line help just to overshoot a tiny threshold.
    if _blen(header) + _blen(notice) > threshold:
        if _blen(_COMPACT_NAV_HEADER) + _blen(notice) <= threshold:
            header = _COMPACT_NAV_HEADER
        elif _blen(_COMPACT_NAV_HEADER) <= threshold:
            # The compact header fits without the truncation notice: emit it
            # alone (no notice, no body), the smallest navigable output that
            # still stays within the byte bound.
            return _COMPACT_NAV_HEADER
        else:
            # Even the compact header overflows the threshold: hard-truncate it
            # to the byte bound. The context-bound invariant (every output stays
            # within the configured positive threshold) wins over preserving the
            # full navigation commands in this degenerate sub-floor case.
            return _byte_truncate(_COMPACT_NAV_HEADER, threshold)

    base = _blen(header) + _blen(notice)
    body_units = [u for u in _body_units(body) if u]
    kept: List[str] = []
    size = base
    for unit in body_units:
        add = _blen(unit) + 1  # newline glue
        if size + add > threshold:
            break
        kept.append(unit)
        size += add

    result = header
    if kept:
        result += "\n" + "\n".join(kept)
    elif body_units and _is_item_unit(body_units[0]):
        # No whole unit fits, yet the (only) content is a leaf *item* — typically
        # a single oversized Requirement (large tags/refs/name) reached by
        # drilling into a page handle. Emitting only the header + notice would
        # leave the view with no selectable address, so analyze could never
        # finish drilling to that leaf.
        leaf = body_units[0]
        address_line = leaf.split("\n", 1)[0]
        leaf_budget = max(0, threshold - base)
        if _blen(address_line) + 1 <= leaf_budget:
            # The full ``<spec>::<requirement>`` address fits: compact the unit,
            # always preserving its address line, so the leaf stays selectable
            # and the view stays bounded. (Navigation handles, by contrast, are
            # kept bounded by the fold/pagination machinery, so when none fits
            # they are dropped rather than force-emitted here.)
            result += "\n" + _compact_unit(leaf, leaf_budget)
        else:
            # Irreducible: the ``<spec>::<requirement>`` address ALONE overruns
            # the residual budget (a Requirement name long enough to trigger this
            # is itself a writing-discipline violation surfaced by guardrails).
            # The bounded-context invariant wins — every single output entering
            # LLM context MUST stay within the byte threshold — so we do NOT
            # force-emit the over-long literal address. We also do NOT substitute
            # the broader ``<spec>::*`` whole-spec selector (that would silently
            # change an exact single-item selection into selecting every
            # Requirement in the spec). Instead emit a bounded, NON-selectable
            # placeholder that names the spec, previews the item, and points to
            # the resolution (split the Requirement / raise the threshold). The
            # item is intentionally not selectable here, because no
            # threshold-bounded view can expose its over-long exact address.
            result += "\n" + _oversized_leaf_substitute(address_line)
    result += notice

    # Final byte-bound guarantee: the context-bound invariant always wins. Every
    # view — root, group, drilled page, or a single oversized leaf — is
    # hard-truncated to the byte bound on a valid UTF-8 boundary so no individual
    # index response can exceed the configured threshold. An over-long leaf
    # address never escapes here: it was replaced above by a bounded,
    # NON-selectable placeholder; even if that placeholder is itself truncated it
    # can never become a valid broadened selector, so the selection semantics are
    # preserved.
    if _blen(result) > threshold:
        return _byte_truncate(result, threshold)
    return result


def _lines_size(lines: Sequence[str]) -> int:
    """Byte size of *lines* once joined with newlines."""
    if not lines:
        return 0
    return _blen("\n".join(lines)) + 1  # trailing newline glue


# ---------------------------------------------------------------------------
# Display model
# ---------------------------------------------------------------------------

@dataclass
class _Child:
    """A renderable unit at one navigation level.

    A *leaf* (``foldable=False``) is an item entry (spec view) or a spec entry
    (root view): it renders as ``inline_lines`` and is never compacted to a
    handle (only bundled into a page). A *group* (``foldable=True``) is a domain
    subgroup: it renders flat as ``inline_lines`` (its members) in the most-
    expanded form, or collapses to the single ``handle_line`` when folded;
    ``sub_children`` are the children reached by drilling.
    """

    token: str
    sort_name: str
    inline_lines: List[str]
    foldable: bool = False
    handle_line: str = ""
    sub_children: List["_Child"] = field(default_factory=list)
    is_page: bool = False

    def display_lines(self, folded: bool) -> List[str]:
        if folded and self.handle_line:
            return [self.handle_line]
        return self.inline_lines


@dataclass
class _Page:
    """A pagination handle produced by the size-bounded fallback."""

    path: Tuple[str, ...]
    sub_children: List[_Child]
    is_page: bool = True
    foldable: bool = True

    @property
    def token(self) -> str:
        return self.path[-1]

    @property
    def leaf_count(self) -> int:
        return _count_leaves(self.sub_children)

    @property
    def handle_line(self) -> str:
        label = "/".join(self.path)
        cmd = _index_cmd(self.path)
        # Pages are the *structure-exhausted* fallback, but they are still made
        # self-describing by carrying the requirement-name span (first … last)
        # of the items they hold — derived mechanically from the ``###``
        # requirement outline — so an LLM sees which Requirements live behind a
        # page handle instead of an opaque ``pN``. The span never contains a
        # ``::`` (item-identity invariant: handles carry no item address).
        span = _leaf_name_span(self.sub_children)
        span_txt = f"{span} — " if span else ""
        return (
            f"[page] {label} — {span_txt}{self.leaf_count} entries\n"
            f"    → {cmd}"
        )

    def display_lines(self, folded: bool = True) -> List[str]:
        return self.handle_line.split("\n")


# Cap a span name so a page handle stays compact even with long requirement names.
_SPAN_NAME_MAX = 40


def _leaf_node_name(node) -> str:
    """The displayable requirement name of a leaf ``_Child`` (handle-safe)."""
    name = getattr(node, "token", "") or getattr(node, "sort_name", "") or ""
    # A handle must never carry a ``::`` address (item-identity invariant); a
    # requirement name should not contain ``::`` but defensively neutralise it.
    name = name.replace("::", ":")
    if len(name) > _SPAN_NAME_MAX:
        name = name[: _SPAN_NAME_MAX - 1] + "…"
    return name


def _first_leaf(children: Sequence):
    """The first non-page, non-group leaf reachable by descending *children*."""
    for c in children:
        if getattr(c, "is_page", False) or (
            getattr(c, "foldable", False) and getattr(c, "sub_children", None)
        ):
            found = _first_leaf(c.sub_children)
            if found is not None:
                return found
        else:
            return c
    return None


def _last_leaf(children: Sequence):
    """The last non-page, non-group leaf reachable by descending *children*."""
    for c in reversed(list(children)):
        if getattr(c, "is_page", False) or (
            getattr(c, "foldable", False) and getattr(c, "sub_children", None)
        ):
            found = _last_leaf(c.sub_children)
            if found is not None:
                return found
        else:
            return c
    return None


def _leaf_name_span(children: Sequence) -> str:
    """``"First … Last"`` span of the requirement names a page covers.

    Returns a single name when the page holds one leaf (or the endpoints match),
    or ``""`` when no leaf is reachable. Deterministic and program-derived (the
    ``###`` requirement names already in the index) — no LLM.
    """
    first = _first_leaf(children)
    if first is None:
        return ""
    last = _last_leaf(children)
    first_name = _leaf_node_name(first)
    if not first_name:
        return ""
    last_name = _leaf_node_name(last) if last is not None else first_name
    if last is first or last_name == first_name:
        return first_name
    return f"{first_name} … {last_name}"


def _count_leaves(children: Sequence) -> int:
    total = 0
    for c in children:
        if getattr(c, "is_page", False):
            total += _count_leaves(c.sub_children)
        elif getattr(c, "foldable", False) and c.sub_children:
            total += _count_leaves(c.sub_children)
        else:
            total += 1
    return total


# ---------------------------------------------------------------------------
# Header (self-describing command help)
# ---------------------------------------------------------------------------

def _index_cmd(tokens: Sequence[str]) -> str:
    """Render a copy-paste-safe ``se3 spec index`` command for *tokens*.

    Every positional is shell-quoted (``shlex.quote``) so a handle whose domain
    component carries shell-significant characters — most notably the built-in
    ``(未分类)`` unclassified group, whose parentheses bash would otherwise parse
    as a subshell — stays executable when pasted into Bash. ASCII-safe tokens
    (spec names, ``sN`` / ``pN`` handles, ``@domain`` sentinel) pass through
    unchanged, so existing command strings are byte-identical.
    """
    parts = " ".join(shlex.quote(t) for t in tokens)
    return "se3 spec index" + (f" {parts}" if parts else "")


def _header(base_path: Tuple[str, ...], label: str) -> str:
    loc = f" [{label}]" if label else ""
    path_hint = (
        (" " + " ".join(shlex.quote(p) for p in base_path)) if base_path else ""
    )
    return (
        f"# se3 spec index{path_hint}{loc}\n"
        f"# Items are lines like '- <spec>::<requirement>'. "
        f"Read one item's full text with:\n"
        f"#     se3 spec show <spec>::<requirement>\n"
        f"# Lines tagged [group]/[page] are navigation handles (no '::' address). "
        f"Drill into one with the command it shows:\n"
        f"#     se3 spec index <spec> [<group>...]\n"
    )


# ---------------------------------------------------------------------------
# Semantic children builders
# ---------------------------------------------------------------------------

def _effective_domain(index: SpecIndex, spec_name: str) -> Tuple[str, ...]:
    """Domain path for *spec_name*; specs with no marker fall under ``(未分类)``."""
    meta = index.spec_metas.get(spec_name)
    raw = meta.domain if meta else None
    if not raw:
        return (UNCLASSIFIED_GROUP,)
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    return tuple(parts) if parts else (UNCLASSIFIED_GROUP,)


def _root_spec_names(index: SpecIndex) -> set:
    """The set of real spec names (excluding the no-requirements sentinel)."""
    return {
        name for name in index.spec_metas if name != _NO_REQUIREMENTS_SENTINEL
    }


def _root_domain_needs_sentinel(
    spec_name_set: set, domain_path: Tuple[str, ...]
) -> bool:
    """Whether a root domain path is shadowed by a spec of the same first name.

    The first positional of ``se3 spec index`` is resolved as a spec name when
    one exists (the spec namespace wins), so a domain path whose leading
    component equals a spec name needs the :data:`_ROOT_DOMAIN_SENTINEL` prefix
    to stay reachable as a domain group rather than opening that spec.
    """
    return bool(domain_path) and domain_path[0] in spec_name_set


def _root_drill_path(
    spec_name_set: set, domain_path: Tuple[str, ...]
) -> Tuple[str, ...]:
    """The ``se3 spec index`` argument tail (token tuple) that drills
    *domain_path* at root, sentinel-prefixed when its leading component collides
    with a spec name. Returned as a tuple so the caller can shell-quote each
    component when rendering the copy-paste command."""
    path = domain_path
    if _root_domain_needs_sentinel(spec_name_set, domain_path):
        path = (_ROOT_DOMAIN_SENTINEL,) + domain_path
    return path


def _spec_entry_lines(index: SpecIndex, spec_name: str) -> List[str]:
    meta = index.spec_metas.get(spec_name)
    count = meta.item_count if meta else 0
    locator = (meta.locator if meta else "") or ""
    head = f"- {spec_name}  ({count} items)  → {_index_cmd((spec_name,))}"
    lines = [head]
    if locator:
        lines.append(f"      {locator}")
    return lines


def _root_children(index: SpecIndex, domain_path: Tuple[str, ...]) -> List[_Child]:
    """Build the semantic children of the root view at *domain_path*.

    Specs whose effective domain equals *domain_path* exactly become direct
    spec-entry leaves; the rest are partitioned by their next domain component
    into foldable subgroup children (recursively built), so folding descends the
    domain path one level at a time.
    """
    spec_name_set = _root_spec_names(index)
    spec_names = sorted(spec_name_set)
    plen = len(domain_path)

    direct: List[str] = []
    groups: dict = {}
    for name in spec_names:
        eff = _effective_domain(index, name)
        if eff[:plen] != domain_path:
            continue
        if len(eff) == plen:
            direct.append(name)
        else:
            comp = eff[plen]
            groups.setdefault(comp, []).append(name)

    children: List[_Child] = []
    for comp in sorted(groups):
        sub_path = domain_path + (comp,)
        sub = _root_children(index, sub_path)
        inline: List[str] = []
        for c in sub:
            inline.extend(c.inline_lines)
        n_specs = len(groups[comp])
        label = "/".join(sub_path)
        # The drill command is sentinel-prefixed when this domain path's leading
        # component collides with a spec name, so the domain group stays
        # reachable instead of being shadowed by that spec's item view.
        drill_path = _root_drill_path(spec_name_set, sub_path)
        handle = (
            f"[group] {label} — {n_specs} spec(s)\n"
            f"    → {_index_cmd(drill_path)}"
        )
        children.append(
            _Child(
                token=comp,
                sort_name=comp,
                inline_lines=inline,
                foldable=True,
                handle_line=handle,
                sub_children=sub,
            )
        )

    for name in sorted(direct):
        children.append(
            _Child(
                token=name,
                sort_name=name,
                inline_lines=_spec_entry_lines(index, name),
                foldable=False,
            )
        )

    return children


def _item_entry_lines(index: SpecIndex, item) -> List[str]:
    addr = f"{item.spec_name}::{item.requirement_name}"
    lines = [f"- {addr}"]
    if item.summary:
        lines.append(f"      {item.summary}")
    meta_bits = []
    if item.tags:
        meta_bits.append("tags: " + ", ".join(item.tags))
    if item.refs:
        meta_bits.append("refs: " + ", ".join(item.refs))
    if meta_bits:
        lines.append("      " + " | ".join(meta_bits))
    return lines


def _item_leaf(index: SpecIndex, item) -> _Child:
    """A single Requirement rendered as a (never-folded) leaf entry."""
    return _Child(
        token=item.requirement_name,
        sort_name=item.requirement_name,
        inline_lines=_item_entry_lines(index, item),
        foldable=False,
    )


def _spec_sections(index: SpecIndex, spec_name: str):
    """Ordered ``[(section_name, [items]), ...]`` for *spec_name*.

    Items are grouped by their stored enclosing ``## `` chapter ``section`` and
    returned in document order: sections by the earliest item ``line_start``
    they contain, items within a section by ``line_start`` (ties broken by
    name) — all deterministic. The no-requirements sentinel is excluded.
    """
    buckets: dict = {}
    for key in index.items:
        item = index.items[key]
        if item.spec_name != spec_name:
            continue
        if item.requirement_name == _NO_REQUIREMENTS_SENTINEL:
            continue
        sec = getattr(item, "section", "") or ""
        buckets.setdefault(sec, []).append(item)

    ordered_sections = sorted(
        buckets.keys(),
        key=lambda s: (min(i.line_start for i in buckets[s]), s),
    )
    result = []
    for sec in ordered_sections:
        items = sorted(
            buckets[sec], key=lambda i: (i.line_start, i.requirement_name)
        )
        result.append((sec, items))
    return result


def _section_group_child(
    index: SpecIndex,
    spec_name: str,
    token: str,
    section_name: str,
    items: List,
) -> _Child:
    """A foldable chapter group: items inline when expanded, a handle when folded."""
    inline: List[str] = []
    leaves: List[_Child] = []
    for item in items:
        inline.extend(_item_entry_lines(index, item))
        leaves.append(_item_leaf(index, item))
    label = section_name or "(unsectioned)"
    handle = (
        f"[group] {spec_name}/{label} — {len(items)} item(s)\n"
        f"    → {_index_cmd((spec_name, token))}"
    )
    return _Child(
        token=token,
        sort_name=token,
        inline_lines=inline,
        foldable=True,
        handle_line=handle,
        sub_children=leaves,
    )


def _trim_req_name(name: str) -> str:
    """Handle-safe, length-capped requirement name for a subgroup label."""
    name = (name or "").replace("::", ":")
    if len(name) > _SPAN_NAME_MAX:
        name = name[: _SPAN_NAME_MAX - 1] + "…"
    return name


def _section_name_span(items: Sequence) -> str:
    """``"First … Last"`` requirement-name span for a name-range subgroup.

    Derived purely from the ``### `` requirement names already in the index (no
    LLM); single-item subgroups collapse to one name. Handles never carry a
    ``::`` address, so the names are neutralised by :func:`_trim_req_name`.
    """
    if not items:
        return "(empty)"
    first = _trim_req_name(getattr(items[0], "requirement_name", "")) or "(unnamed)"
    if len(items) == 1:
        return first
    last = _trim_req_name(getattr(items[-1], "requirement_name", "")) or "(unnamed)"
    if last == first:
        return first
    return f"{first} … {last}"


def _chunk_items(items: Sequence, size: int) -> List[list]:
    """Split *items* into deterministic contiguous slices of at most *size*."""
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def _subsection_runs(items: Sequence) -> List[Tuple[str, list]]:
    """Group *items* into contiguous runs by their stored ``#### `` ``subsection``.

    Runs preserve document order: consecutive items sharing the same divider form
    one run. A spec with no ``#### `` dividers yields a single ``("",[…])`` run, so
    callers can cheaply detect "no deeper structure" as ``len(runs) < 2``.
    """
    runs: List[Tuple[str, list]] = []
    for item in items:
        sub = getattr(item, "subsection", "") or ""
        if runs and runs[-1][0] == sub:
            runs[-1][1].append(item)
        else:
            runs.append((sub, [item]))
    return runs


def _compose_unit_label(sec: str, sub: str, span: str, multi: bool) -> str:
    """Compose a subgroup label from chapter / sub-section / name-range parts.

    The chapter is included only when more than one real ``## `` chapter exists
    (so a single-chapter spec is not redundantly prefixed); the ``#### ``
    sub-section divider is included when present; the name-range span is always
    appended for precision. Empty parts are dropped.
    """
    parts: List[str] = []
    if multi and sec:
        parts.append(sec)
    if sub:
        parts.append(sub)
    parts.append(span)
    return " · ".join(parts)


def _spec_section_units(
    index: SpecIndex, spec_name: str
) -> List[Tuple[str, str, list]]:
    """Ordered ``[(token, label, items)]`` of a spec's navigable chapter units.

    Built from the stored ``## `` chapter outline. A chapter holding more than
    :data:`_SECTION_SUBDIVIDE_SIZE` Requirements is subdivided, and the
    subdivision exhausts *deeper semantic structure before deterministic
    pagination*: if the oversized chapter is organised into two or more ``#### ``
    sub-section dividers (the stored ``subsection``), it is split by those
    contiguous sub-section runs first; a single sub-section run that is itself
    still over the cap is then name-range chunked from the ``### `` requirement
    outline. Only a chapter with no deeper ``#### `` structure collapses straight
    to deterministic name-range chunks. This gives a flat spec — every
    Requirement under one ``## Requirements`` chapter, the common se3 layout —
    semantic, name-labelled groups instead of anonymous ``pN`` pages, while a
    chapter organised with meaningful ``#### `` subsections is split along those
    boundaries rather than every N Requirements by raw document order. ``sN``
    tokens are numbered globally in document order and are exactly the tokens the
    ``sN`` drill resolver consumes, so the top-level grouping and the drill-down
    can never drift.
    """
    sections = _spec_sections(index, spec_name)
    multi = len(sections) >= 2
    units: List[Tuple[str, str, list]] = []
    n = 1
    for sec, items in sections:
        if len(items) > _SECTION_SUBDIVIDE_SIZE:
            runs = _subsection_runs(items)
            if len(runs) >= 2:
                # Prefer the deeper ``#### `` sub-section boundaries: each run is a
                # unit, and only a run that is itself oversized is further
                # name-range chunked (pagination is the last resort).
                for sub, run_items in runs:
                    if len(run_items) > _SECTION_SUBDIVIDE_SIZE:
                        for chunk in _chunk_items(run_items, _SECTION_SUBDIVIDE_SIZE):
                            span = _section_name_span(chunk)
                            label = _compose_unit_label(sec, sub, span, multi)
                            units.append((f"s{n}", label, chunk))
                            n += 1
                    else:
                        span = _section_name_span(run_items)
                        label = _compose_unit_label(sec, sub, span, multi)
                        units.append((f"s{n}", label, list(run_items)))
                        n += 1
            else:
                # No deeper ``#### `` structure — fall back to deterministic
                # name-range chunks derived from the ``### `` requirement outline.
                for chunk in _chunk_items(items, _SECTION_SUBDIVIDE_SIZE):
                    span = _section_name_span(chunk)
                    # Qualify the name-range with the chapter when more than one
                    # real ``## `` chapter exists, so the subgroup label stays
                    # unambiguous across chapters.
                    label = f"{sec} · {span}" if (multi and sec) else span
                    units.append((f"s{n}", label, chunk))
                    n += 1
        else:
            label = sec or "(unsectioned)"
            units.append((f"s{n}", label, list(items)))
            n += 1
    return units


def _spec_children(index: SpecIndex, spec_name: str) -> List[_Child]:
    """Top-level children of a spec view.

    The spec's Requirements are organised into chapter *units* — the ``## ``
    chapter outline, with any chapter holding many Requirements further split
    into name-range subgroups (see :func:`_spec_section_units`). Two or more
    units become foldable semantic group handles drilled via ``se3 spec index
    <spec> sN``; a single small unit is returned as flat leaves and the
    size-bounded fallback paginates them. Semantic structure (chapters then the
    requirement-name outline) is always exhausted before deterministic ``pN``
    pagination appears.
    """
    units = _spec_section_units(index, spec_name)
    if len(units) >= 2:
        return [
            _section_group_child(index, spec_name, tok, label, items)
            for tok, label, items in units
        ]
    children: List[_Child] = []
    for _tok, _label, items in units:
        for item in items:
            children.append(_item_leaf(index, item))
    return children


# ---------------------------------------------------------------------------
# Greedy folding + pagination core
# ---------------------------------------------------------------------------

def _greedy_fold(
    children: Sequence[_Child],
    threshold: int,
    header_size: int,
) -> Tuple[List[bool], bool]:
    """Greedily fold the largest foldable child until the view fits.

    Returns ``(folded_flags, fits)``. The largest foldable (by inline byte size)
    is folded first; ties are broken by lexicographic ``sort_name`` so the same
    input always yields the same output.
    """
    folded = [False] * len(children)

    def total() -> int:
        size = header_size
        for i, c in enumerate(children):
            size += _lines_size(c.display_lines(folded[i]))
        return size

    while total() > threshold:
        candidates = [
            i for i, c in enumerate(children)
            if c.foldable and not folded[i] and c.handle_line
        ]
        if not candidates:
            return folded, False
        # Largest inline size first; lexicographic name breaks ties.
        candidates.sort(key=lambda i: (-_lines_size(children[i].inline_lines),
                                       children[i].sort_name))
        folded[candidates[0]] = True

    return folded, True


def _bundle(units: Sequence, budget: int, folded: bool) -> List[list]:
    """Greedily pack *units* into bins whose display size stays within *budget*."""
    bundles: List[list] = []
    cur: list = []
    cur_size = 0
    for u in units:
        size = _lines_size(u.display_lines(folded))
        if cur and cur_size + size > budget:
            bundles.append(cur)
            cur = []
            cur_size = 0
        cur.append(u)
        cur_size += size
    if cur:
        bundles.append(cur)
    return bundles


def _reparent(page: _Page, new_path: Tuple[str, ...]) -> None:
    """Re-path *page* (and any nested page descendants) under *new_path*."""
    page.path = new_path
    for child in page.sub_children:
        if isinstance(child, _Page):
            _reparent(child, new_path + (child.token,))


def _build_pages(
    children: Sequence[_Child],
    threshold: int,
    base_path: Tuple[str, ...],
) -> List[_Page]:
    """Deterministically paginate *children* into size-bounded page handles.

    Children are first packed into pages whose drilled content fits the
    threshold. If the resulting list of page handles is itself too large, the
    pages are grouped into super-pages (re-pathed so each command nests
    correctly), repeated until the top-level handle list fits.
    """
    header_size = _blen(_header(base_path, ""))
    budget = max(_MIN_BUDGET, threshold - header_size - _HEADER_MARGIN)
    bundles = _bundle(children, budget, folded=True)
    pages: List[_Page] = [
        _Page(path=base_path + (f"p{i + 1}",), sub_children=list(b))
        for i, b in enumerate(bundles)
    ]

    levels = 0
    while len(pages) > 1 and not _handles_fit(pages, threshold, header_size):
        levels += 1
        if levels > _MAX_PAGE_LEVELS:
            break
        groups = _bundle(pages, budget, folded=True)
        if len(groups) >= len(pages):
            # Threshold too small to pack 2+ handles per super-page by size:
            # fall back to a fixed fan-out so the count still shrinks each round
            # (an infeasible threshold then degrades to a log-depth handle list
            # rather than dumping every flat page).
            groups = [
                list(pages[i:i + _PAGE_FANOUT])
                for i in range(0, len(pages), _PAGE_FANOUT)
            ]
            if len(groups) >= len(pages):
                break
        supers: List[_Page] = []
        for i, grp in enumerate(groups):
            sp_path = base_path + (f"p{i + 1}",)
            members: List[_Page] = []
            for j, member in enumerate(grp):
                _reparent(member, sp_path + (f"p{j + 1}",))
                members.append(member)
            supers.append(_Page(path=sp_path, sub_children=members))
        pages = supers

    return pages


def _handles_fit(pages: Sequence[_Page], threshold: int, header_size: int) -> bool:
    size = header_size
    for p in pages:
        size += _lines_size(p.display_lines(True))
    return size <= threshold


def _walk_pages(pages: Sequence, tokens: Sequence[str]):
    """Walk *tokens* through a (possibly nested) page list; return the node."""
    cur: Sequence = pages
    node = None
    for t in tokens:
        match = next((p for p in cur if getattr(p, "is_page", False)
                      and p.token == t), None)
        if match is None:
            return None
        node = match
        cur = [c for c in match.sub_children if getattr(c, "is_page", False)]
    return node


# ---------------------------------------------------------------------------
# Level renderer
# ---------------------------------------------------------------------------

def _render_children(
    index: SpecIndex,
    children: List[_Child],
    threshold: int,
    base_path: Tuple[str, ...],
    page_tokens: Sequence[str],
    label: str,
    page_base: Optional[Tuple[str, ...]] = None,
) -> str:
    """Render one navigation level.

    *base_path* is the breadcrumb path shown in the header. *page_base* is the
    command prefix that generated ``pN`` handles carry; it defaults to
    *base_path* but the root view passes a :data:`_ROOT_PAGE_SENTINEL`-suffixed
    path so root pagination commands stay in the page namespace and are never
    shadowed by a same-named domain (e.g. a ``engine/p1`` subgroup).
    """
    header = _header(base_path, label)
    header_size = _blen(header)
    if page_base is None:
        page_base = base_path

    if not children:
        return header + "\n(no entries)\n"

    if page_tokens:
        # Navigating into a pagination handle: reproduce the deterministic page
        # layout, walk to the requested page, and render that slice.
        folded_children = _fold_all(children)
        pages = _build_pages(folded_children, threshold, page_base)
        node = _walk_pages(pages, page_tokens)
        if node is None:
            return (
                header
                + f"\n(no such group: {' '.join(page_tokens)})\n"
            )
        return _render_plain(node.sub_children, threshold, node.path, label)

    folded, fits = _greedy_fold(children, threshold, header_size)
    if fits:
        body_lines: List[str] = []
        for i, c in enumerate(children):
            body_lines.extend(c.display_lines(folded[i]))
        return header + "\n" + "\n".join(body_lines) + "\n"

    # Could not fit even with every foldable group collapsed → paginate.
    folded_children = _fold_all(children)
    pages = _build_pages(folded_children, threshold, page_base)
    body_lines = []
    for p in pages:
        body_lines.extend(p.display_lines(True))
    return header + "\n" + "\n".join(body_lines) + "\n"


def _render_plain(
    children: Sequence,
    threshold: int,
    base_path: Tuple[str, ...],
    label: str,
) -> str:
    """Render an already-bounded slice (a drilled page's content)."""
    header = _header(base_path, label)
    if not children:
        return header + "\n(no entries)\n"
    body_lines: List[str] = []
    for c in children:
        body_lines.extend(c.display_lines(True))
    return header + "\n" + "\n".join(body_lines) + "\n"


def _fold_all(children: Sequence[_Child]) -> List[_Child]:
    """Return *children* with every foldable group collapsed to its handle."""
    out: List[_Child] = []
    for c in children:
        if c.foldable and c.handle_line:
            out.append(
                _Child(
                    token=c.token,
                    sort_name=c.sort_name,
                    inline_lines=[c.handle_line],
                    foldable=True,
                    handle_line=c.handle_line,
                    sub_children=c.sub_children,
                )
            )
        else:
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _domain_prefixes(index: SpecIndex) -> set:
    """Every valid domain-path prefix across all specs' effective domains.

    A token tuple is a navigable domain group iff it is a prefix of some spec's
    effective domain (the ``<!-- domain: -->`` path, or the ``(未分类)`` bucket
    for specs without a marker). Used to resolve a root group path against the
    real domain tree rather than the token's shape.
    """
    prefixes = set()
    for name in _root_spec_names(index):
        eff = _effective_domain(index, name)
        for i in range(1, len(eff) + 1):
            prefixes.add(eff[:i])
    return prefixes


def _split_root_group_path(
    index: SpecIndex, group_path: Sequence[str]
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Split a root *group_path* into a domain path and trailing page tokens.

    Domain components are resolved greedily against the actual stored domain
    tree (:func:`_domain_prefixes`), NOT by a regex on the token shape: a token
    is taken as a domain component for as long as it extends a real domain
    prefix; the first token that does not extend any stored domain switches the
    remainder to deterministic ``pN`` pagination tokens. This makes the domain
    namespace authoritative over the renderer's own pagination namespace, so a
    domain literally named like a page handle (e.g. ``p1`` or a nested
    ``engine/p1``) is reachable instead of being misread as a page token.
    """
    prefixes = _domain_prefixes(index)
    domain: List[str] = []
    pages: List[str] = []
    in_pages = False
    for tok in group_path:
        if not in_pages:
            if tok == _ROOT_PAGE_SENTINEL:
                # Explicit switch emitted by root pagination: everything after
                # this marker is a page token. Drop the sentinel itself (it is a
                # routing marker, not a navigable path component), so a generated
                # ``pN`` handle stays reachable even when a same-named domain
                # (``engine/p1``) exists.
                in_pages = True
                continue
            candidate = tuple(domain) + (tok,)
            if candidate in prefixes:
                domain.append(tok)
                continue
            in_pages = True
        pages.append(tok)
    return tuple(domain), tuple(pages)


def render_root_view(
    index: SpecIndex,
    group_path: Sequence[str] = (),
    threshold: Optional[int] = None,
) -> str:
    """Render the root view (all specs), optionally drilled into a domain group.

    With no ``group_path`` this lists every spec with its one-sentence locator
    and item count. Over threshold the view greedily folds the largest domain
    group into a navigation handle (ties broken lexicographically), descending
    the domain path level by level; specs with no ``<!-- domain: -->`` marker
    fold under ``(未分类)``. ``group_path`` drills into a folded domain group or
    a ``pN`` pagination handle.
    """
    threshold = _resolve_threshold(threshold)
    gp = tuple(group_path)
    # A leading sentinel marks an explicit root *domain* drill (used when the
    # domain's first component collides with a spec name). Strip it before
    # resolving the real domain/page path.
    if gp and gp[0] == _ROOT_DOMAIN_SENTINEL:
        gp = gp[1:]
    domain_path, page_tokens = _split_root_group_path(index, gp)
    children = _root_children(index, domain_path)
    label = "/".join(domain_path) if domain_path else "root"
    # When the resolved domain path itself collides with a spec name, the
    # command base (header breadcrumb + generated ``pN`` page commands) must
    # carry the sentinel too, so every drill command emitted from this view
    # routes back to the domain group rather than the colliding spec.
    cmd_base = domain_path
    if _root_domain_needs_sentinel(_root_spec_names(index), domain_path):
        cmd_base = (_ROOT_DOMAIN_SENTINEL,) + domain_path
    # Root pagination commands carry the page sentinel so a generated ``pN``
    # handle is parsed back as a page even when a same-named domain (e.g. a
    # ``<domain>/p1`` subgroup) exists; the breadcrumb header keeps the bare
    # ``cmd_base`` so it shows the honest current location.
    page_base = cmd_base + (_ROOT_PAGE_SENTINEL,)
    out = _render_children(
        index, children, threshold, cmd_base, page_tokens, label,
        page_base=page_base,
    )
    return _enforce_threshold(out, threshold)


def render_spec_view(
    index: SpecIndex,
    spec: str,
    group_path: Sequence[str] = (),
    threshold: Optional[int] = None,
) -> str:
    """Render one spec's item index, optionally drilled into a ``pN`` page.

    Lists every item as ``<spec>::<requirement>`` plus title/summary/tags/refs.
    Over threshold the view folds by the spec's chapter *units*: the ``## ``
    chapter outline, with any chapter holding many Requirements further split
    into name-range subgroups derived from the ``### `` requirement outline. The
    largest unit collapses to a ``[group] <spec>/<chapter-or-name-range>`` handle
    drilled via ``se3 spec index <spec> sN``. Only when a single subgroup is
    still too large once drilled does it fall back to deterministic
    ``<spec> sN/pN`` pagination, so semantic structure is exhausted before
    anonymous pages appear.
    """
    threshold = _resolve_threshold(threshold)

    if spec not in index.spec_metas and not _spec_sections(index, spec):
        header = _header((spec,), spec)
        return _enforce_threshold(
            header + f"\n(no such spec: {spec})\n", threshold
        )

    tokens = list(group_path)
    # A leading ``sN`` token selects a chapter group; the remainder are ``pN``
    # pagination tokens. Without a leading section token everything is a page
    # token (single-chapter / flat spec, or a page of folded chapter handles).
    if tokens and _SECTION_TOKEN_RE.match(tokens[0]):
        section_token = tokens[0]
        page_tokens = tuple(tokens[1:])
        units = _spec_section_units(index, spec)
        unit = next((u for u in units if u[0] == section_token), None)
        if unit is None:
            header = _header((spec, section_token), spec)
            return _enforce_threshold(
                header + f"\n(no such group: {section_token})\n", threshold
            )
        _tok, sec_name, items = unit
        children = [_item_leaf(index, item) for item in items]
        base_path = (spec, section_token)
        label = f"{spec}/{sec_name or '(unsectioned)'}"
        out = _render_children(
            index, children, threshold, base_path, page_tokens, label
        )
        return _enforce_threshold(out, threshold)

    page_tokens = tuple(tokens)
    children = _spec_children(index, spec)
    out = _render_children(
        index, children, threshold, (spec,), page_tokens, spec
    )
    return _enforce_threshold(out, threshold)


def render_group_view(
    index: SpecIndex,
    spec: Optional[str] = None,
    group_path: Sequence[str] = (),
    threshold: Optional[int] = None,
) -> str:
    """Dispatch to the root or spec view for a multi-level group drill-down."""
    if spec == _ROOT_DOMAIN_SENTINEL:
        return render_root_view(index, tuple(group_path), threshold)
    if spec is None:
        return render_root_view(index, group_path, threshold)
    return render_spec_view(index, spec, group_path, threshold)


def render_index(
    index: SpecIndex,
    spec: Optional[str] = None,
    group_path: Sequence[str] = (),
    threshold: Optional[int] = None,
) -> str:
    """Unified entry shared by the CLI and analyze's programmatic root injection.

    Disambiguates the first positional like the CLI does: the reserved
    :data:`_ROOT_DOMAIN_SENTINEL` forces root-level domain navigation (used when
    a domain component collides with a spec name); otherwise when *spec* names a
    known spec it renders that spec's item view; otherwise *spec* is treated as
    the first component of a root-level domain/page drill path. ``spec is None``
    renders the root view.
    """
    threshold = _resolve_threshold(threshold)
    if spec == _ROOT_DOMAIN_SENTINEL:
        # Explicit root-domain drill: the remaining path is the domain/page
        # path, never resolved against the spec namespace.
        return render_root_view(index, tuple(group_path), threshold)
    if spec is not None and spec in index.spec_metas \
            and spec != _NO_REQUIREMENTS_SENTINEL:
        return render_spec_view(index, spec, group_path, threshold)
    # Treat the first positional as part of the root domain/page path.
    root_path: Tuple[str, ...] = tuple(group_path)
    if spec is not None:
        root_path = (spec,) + root_path
    return render_root_view(index, root_path, threshold)


def _resolve_threshold(threshold: Optional[int]) -> int:
    if threshold is None or threshold <= 0:
        return DEFAULT_INDEX_RENDER_THRESHOLD
    return threshold
