"""Size-bounded, deterministic rendering of the spec index for LLM consumption.

This module is the **rendering layer** sitting on top of the navigation layer
(:mod:`se3.engine.spec_index`). It turns the flat, persisted index into
size-bounded *views* a single LLM context can hold:

- **root view** — every spec's name + one-sentence locator + item count. When the
  rendered output exceeds the threshold it is folded, ``domain`` path level by
  level, into navigation group handles; specs lacking a ``<!-- domain: -->``
  marker fold under the ``(未分类)`` group.
- **spec view** — the item index of one spec (``<spec>::<requirement>`` address,
  title, summary, tags, refs). Over threshold it is folded into deterministic
  ``<spec>/pN`` pagination handles (se3 specs carry no intermediate ``###``
  chapter grouping above their flat Requirement items, so pagination is the
  structural fallback the spec governance model calls for — *结构耗尽时以确定性
  分页兜底*).
- **group view** — drilling into any group handle (a deeper ``domain`` path or a
  ``pN`` page) via a multi-level group path.

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
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from .spec_governance import UNCLASSIFIED_GROUP
from .spec_index import SpecIndex, _NO_REQUIREMENTS_SENTINEL

try:
    from ..config import DEFAULT_INDEX_RENDER_THRESHOLD
except Exception:  # pragma: no cover - defensive; config should always import
    DEFAULT_INDEX_RENDER_THRESHOLD = 16384


# A page token used by the pagination fallback: ``p`` followed by a 1-based
# index, e.g. ``p1`` / ``p2``. Domain-path components never match this pattern
# in practice (a domain literally named ``p1`` is pathological and out of scope).
_PAGE_TOKEN_RE = re.compile(r"^p\d+$")

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


def _blen(text: str) -> int:
    """Byte length under UTF-8 (specs and group names may hold CJK text)."""
    return len(text.encode("utf-8"))


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
        cmd = " ".join(self.path)
        return (
            f"[page] {label} — {self.leaf_count} entries\n"
            f"    → se3 spec index {cmd}"
        )

    def display_lines(self, folded: bool = True) -> List[str]:
        return self.handle_line.split("\n")


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

def _header(base_path: Tuple[str, ...], label: str) -> str:
    loc = f" [{label}]" if label else ""
    path_hint = (" " + " ".join(base_path)) if base_path else ""
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


def _spec_entry_lines(index: SpecIndex, spec_name: str) -> List[str]:
    meta = index.spec_metas.get(spec_name)
    count = meta.item_count if meta else 0
    locator = (meta.locator if meta else "") or ""
    head = f"- {spec_name}  ({count} items)  → se3 spec index {spec_name}"
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
    spec_names = sorted(
        name for name in index.spec_metas if name != _NO_REQUIREMENTS_SENTINEL
    )
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
        cmd = " ".join(sub_path)
        handle = (
            f"[group] {label} — {n_specs} spec(s)\n"
            f"    → se3 spec index {cmd}"
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


def _spec_children(index: SpecIndex, spec_name: str) -> List[_Child]:
    children: List[_Child] = []
    for key in sorted(index.items):
        item = index.items[key]
        if item.spec_name != spec_name:
            continue
        if item.requirement_name == _NO_REQUIREMENTS_SENTINEL:
            continue
        children.append(
            _Child(
                token=item.requirement_name,
                sort_name=item.requirement_name,
                inline_lines=_item_entry_lines(index, item),
                foldable=False,
            )
        )
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
) -> str:
    header = _header(base_path, label)
    header_size = _blen(header)

    if not children:
        return header + "\n(no entries)\n"

    if page_tokens:
        # Navigating into a pagination handle: reproduce the deterministic page
        # layout, walk to the requested page, and render that slice.
        folded_children = _fold_all(children)
        pages = _build_pages(folded_children, threshold, base_path)
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
    pages = _build_pages(folded_children, threshold, base_path)
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

def _split_group_path(
    group_path: Sequence[str],
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Split *group_path* into a leading domain path and trailing page tokens."""
    domain: List[str] = []
    pages: List[str] = []
    in_pages = False
    for tok in group_path:
        if in_pages or _PAGE_TOKEN_RE.match(tok):
            in_pages = True
            pages.append(tok)
        else:
            domain.append(tok)
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
    domain_path, page_tokens = _split_group_path(group_path)
    children = _root_children(index, domain_path)
    label = "/".join(domain_path) if domain_path else "root"
    return _render_children(
        index, children, threshold, domain_path, page_tokens, label
    )


def render_spec_view(
    index: SpecIndex,
    spec: str,
    group_path: Sequence[str] = (),
    threshold: Optional[int] = None,
) -> str:
    """Render one spec's item index, optionally drilled into a ``pN`` page.

    Lists every item as ``<spec>::<requirement>`` plus title/summary/tags/refs.
    Over threshold the items fold into deterministic ``<spec>/pN`` pagination
    handles (se3 specs have no intermediate ``###`` chapter grouping above their
    flat Requirement items, so pagination is the structural fallback).
    """
    threshold = _resolve_threshold(threshold)
    # In a spec view every group component is a pagination token.
    page_tokens = tuple(group_path)
    children = _spec_children(index, spec)
    if not children and spec not in index.spec_metas:
        header = _header((spec,), spec)
        return header + f"\n(no such spec: {spec})\n"
    return _render_children(
        index, children, threshold, (spec,), page_tokens, spec
    )


def render_group_view(
    index: SpecIndex,
    spec: Optional[str] = None,
    group_path: Sequence[str] = (),
    threshold: Optional[int] = None,
) -> str:
    """Dispatch to the root or spec view for a multi-level group drill-down."""
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

    Disambiguates the first positional like the CLI does: when *spec* names a
    known spec it renders that spec's item view; otherwise *spec* is treated as
    the first component of a root-level domain/page drill path. ``spec is None``
    renders the root view.
    """
    threshold = _resolve_threshold(threshold)
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
