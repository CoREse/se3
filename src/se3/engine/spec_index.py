"""Spec Index - Item-level index for spec Requirements.

Builds and maintains an item-level index keyed by ``<spec>::<requirement>``.
Each entry tracks metadata (mtime, size, sha256 prefix) for incremental
cache invalidation, plus tags/keywords/refs for selector and loader use, and
the item's physical line interval (``line_start`` / ``line_end``) so the
``se3 spec show`` navigation command can read a single Requirement's body by
logical address without re-parsing the whole file.

The index also records, per spec, a small ``SpecMeta`` block (``domain`` parsed
from the ``<!-- domain: <path> -->`` header marker, a one-sentence ``locator``
parsed from the ``## Purpose`` first paragraph, and the ``item_count``). These
are program-derived navigation aids; they hold no authoritative content (the
spec file remains the storage layer).

Index file: ``se3/cache/spec-index.json`` (gitignored derived data).
"""

from __future__ import annotations

try:
    import fcntl
except ImportError:
    fcntl = None

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .spec_format import parse_spec

logger = logging.getLogger(__name__)

# Index schema version. Bumped 1 -> 2 when ItemMeta gained physical line
# numbers (line_start / line_end) and a per-spec ``specs`` metadata section
# (domain / locator / item_count); bumped 2 -> 3 when ItemMeta gained the
# enclosing ``## `` chapter ``section`` (the chapter-outline grouping the spec
# view renders before falling back to pagination); bumped 3 -> 4 when ItemMeta
# gained the enclosing ``#### `` ``subsection`` divider (the deeper sub-section
# grouping the spec view prefers before deterministic name-range pagination). A
# cached index whose ``version`` is not the current value is treated as a load
# miss and rebuilt from scratch, so an older cache is never mis-read against a
# newer schema.
INDEX_VERSION = 4

# Domain header marker: ``<!-- domain: <layered/path> -->`` placed alongside the
# ``<!-- spec-format: v1 -->`` marker at the top of a spec file. Parsed
# mechanically at index time (no LLM). See spec_governance.DOMAIN_MARKER_PREFIX.
_DOMAIN_MARKER_RE = re.compile(r"<!--\s*domain:\s*(.*?)\s*-->", re.IGNORECASE)

# ``## Purpose`` heading (locator source).
_PURPOSE_HEADING_RE = re.compile(r"^##\s+Purpose\s*$", re.IGNORECASE | re.MULTILINE)
_H2_ANY_RE = re.compile(r"^##\s+", re.MULTILINE)

# Sentinel requirement name used for specs that have zero Requirements, so the
# index can still record file metadata for incremental rebuild detection.
_NO_REQUIREMENTS_SENTINEL = "__no_requirements__"

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ItemMeta:
    """Metadata for a single Requirement item within a spec."""

    spec_name: str
    requirement_name: str
    spec_path: str
    mtime: float
    size: int
    sha256_prefix: str
    tags: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    refs: List[str] = field(default_factory=list)
    summary: str = ""
    # Physical line interval of this Requirement within its spec file, 1-based
    # and inclusive. ``line_start`` is the ``### Requirement:`` heading line;
    # ``line_end`` is the line just before the next Requirement boundary (or the
    # last line of the file for the final Requirement). Used by
    # ``resolve_item_location`` / ``se3 spec show`` to read a single item's body
    # without re-parsing the whole spec. ``0`` means "unknown" (legacy / sentinel).
    line_start: int = 0
    line_end: int = 0
    # Enclosing ``## `` chapter heading text (e.g. ``"Requirements"``) this
    # Requirement falls under, program-derived at index time (the nearest
    # preceding second-level heading). The spec-view renderer groups items by
    # this chapter outline *before* falling back to deterministic pagination,
    # so a multi-chapter spec drills down by semantic group rather than by an
    # anonymous page boundary. Empty string when no enclosing ``## `` heading
    # precedes the Requirement (degenerate / legacy).
    section: str = ""
    # Enclosing ``#### `` sub-section *divider* heading text this Requirement
    # falls under, program-derived at index time. A ``#### `` heading qualifies
    # as a divider only when the next non-blank line after it is a
    # ``### Requirement:`` heading (it introduces a run of Requirements rather
    # than being prose inside one Requirement's body); such a divider opens a
    # sub-section that every following Requirement belongs to until the next
    # divider or a new ``## `` chapter. The spec-view renderer prefers these
    # deeper sub-section dividers over deterministic name-range pagination when an
    # oversized chapter must be split, so meaningful ``#### `` boundaries are
    # honoured before anonymous pages appear. Empty string when no divider
    # precedes the Requirement within its chapter (the common flat layout).
    subsection: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ItemMeta":
        return cls(
            spec_name=data["spec_name"],
            requirement_name=data["requirement_name"],
            spec_path=data["spec_path"],
            mtime=data["mtime"],
            size=data["size"],
            sha256_prefix=data["sha256_prefix"],
            tags=list(data.get("tags", [])),
            keywords=list(data.get("keywords", [])),
            refs=list(data.get("refs", [])),
            summary=data.get("summary", ""),
            line_start=int(data.get("line_start", 0) or 0),
            line_end=int(data.get("line_end", 0) or 0),
            section=data.get("section", "") or "",
            subsection=data.get("subsection", "") or "",
        )

    @property
    def item_id(self) -> str:
        """Stable compound key: ``<spec>::<requirement>``."""
        return f"{self.spec_name}::{self.requirement_name}"


@dataclass
class SpecMeta:
    """Spec-level navigation metadata, program-derived at index time.

    Holds no authoritative content — only navigation aids the renderer reads:

    - ``domain``: layered classification path parsed from the spec's
      ``<!-- domain: <path> -->`` header marker, or ``None`` when the marker is
      absent (the renderer groups such specs under ``(未分类)``).
    - ``locator``: one-sentence positioning parsed from the ``## Purpose`` first
      paragraph (truncated), shown beside the spec name in the root index view.
    - ``item_count``: number of indexed Requirements in the spec.
    """

    spec_name: str
    domain: Optional[str] = None
    locator: str = ""
    item_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        # Persisted under the ``specs`` section keyed by spec name, so the
        # name itself is the key and is not duplicated in the value.
        return {
            "domain": self.domain,
            "locator": self.locator,
            "item_count": self.item_count,
        }

    @classmethod
    def from_dict(cls, spec_name: str, data: Dict[str, Any]) -> "SpecMeta":
        return cls(
            spec_name=spec_name,
            domain=data.get("domain"),
            locator=data.get("locator", "") or "",
            item_count=int(data.get("item_count", 0) or 0),
        )


# ---------------------------------------------------------------------------
# Backwards-compat shim for consumers that still expect file-level objects
# ---------------------------------------------------------------------------

class _SimpleSpecInfo:
    """Minimal stand-in for the old SpecInfo used by sync_engine etc."""

    __slots__ = ("name", "path")

    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path


# ---------------------------------------------------------------------------
# SpecIndex
# ---------------------------------------------------------------------------

class SpecIndex:
    """Item-level index of spec Requirements.

    Attributes:
        items: Mapping from ``<spec>::<requirement>`` to ``ItemMeta``.
        specs: Backwards-compatible dict of ``name -> _SimpleSpecInfo``.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.specs_dir = self._resolve_specs_dir(project_root)
        self.items: Dict[str, ItemMeta] = {}
        # Spec-level navigation metadata (domain / locator / item_count),
        # keyed by spec name. Program-derived; rebuilt alongside items.
        self.spec_metas: Dict[str, SpecMeta] = {}
        # Backwards-compat: derived on demand from items
        self._specs: Optional[Dict[str, _SimpleSpecInfo]] = None
        # O(1) lookup from spec_name -> any cached ItemMeta for that spec,
        # used by needs_rebuild() to avoid linear scans.
        self._first_item_per_spec: Dict[str, ItemMeta] = {}
        self._index_file = project_root / "se3" / "cache" / "spec-index.json"

    # -- internal helpers --------------------------------------------------

    @staticmethod
    def _resolve_specs_dir(project_root: Path) -> Path:
        """Resolve specs directory: se3/specs/ preferred, specs/ fallback, openspec/specs/ legacy."""
        primary = project_root / "se3" / "specs"
        fallback = project_root / "specs"
        legacy = project_root / "openspec" / "specs"
        if primary.exists():
            return primary
        if fallback.exists():
            return fallback
        return legacy

    @staticmethod
    def _sha256_prefix_of_bytes(data: bytes, prefix_bytes: int = 16) -> str:
        """Return hex-encoded SHA-256 of *data*, truncated to *prefix_bytes*."""
        return hashlib.sha256(data).hexdigest()[: prefix_bytes * 2]

    @staticmethod
    def _compute_sha256_prefix(path: Path, prefix_bytes: int = 16) -> str:
        """Return hex-encoded SHA-256 of the file, truncated to *prefix_bytes*."""
        h = hashlib.sha256()
        # Read in chunks to avoid loading huge files into memory
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()[: prefix_bytes * 2]

    @staticmethod
    def _hash_and_slice_lines(
        path: Path, start: int, end: int, prefix_bytes: int = 16,
    ) -> tuple[str, int, str]:
        """Single streaming pass over *path*: hash the full content AND slice it.

        Computes the sha256 prefix of the entire file (so the soundness check
        below still validates that on-disk content matches the indexed snapshot)
        while collecting only lines ``[start, end]`` (1-based, inclusive). The
        whole file is never held in memory: it is iterated line by line, the raw
        bytes feed the running hash, and only the target slice (typically a few
        KiB out of a multi-hundred-KiB spec) is retained and decoded.

        Line numbering counts ``\\n`` only, matching ``parse_spec``'s
        ``text.count("\\n")`` convention (so the recorded ``line_start`` lines up
        with the slice). Returns ``(sha256_prefix, total_lines, body)`` where
        ``body`` is the ``\\n``-joined slice. Raises ``OSError`` on read failure
        and ``UnicodeDecodeError`` if a sliced line is not valid UTF-8.
        """
        h = hashlib.sha256()
        collected: List[str] = []
        line_no = 0
        with open(path, "rb") as f:
            for raw in f:  # binary iteration splits on b"\n" only
                h.update(raw)  # reproduce exact bytes so the hash matches
                line_no += 1
                if start <= line_no <= end:
                    # Drop the trailing line terminator (\n, and a preceding \r
                    # for CRLF files) the way str.splitlines() does, so the
                    # joined body matches the prior whole-file slice behaviour.
                    stripped = raw[:-1] if raw.endswith(b"\n") else raw
                    if stripped.endswith(b"\r"):
                        stripped = stripped[:-1]
                    collected.append(stripped.decode("utf-8"))
        return (h.hexdigest()[: prefix_bytes * 2], line_no, "\n".join(collected))

    # Tags / keywords metadata lines that may appear at the top of a
    # Requirement body and should be skipped when forming a summary.
    _TAGS_KEYWORDS_LINE_RE = re.compile(
        r"^\*\*(tags|keywords)\*\*:",
        re.IGNORECASE | re.MULTILINE,
    )

    @classmethod
    def _make_summary(cls, body: str, max_chars: int = 200) -> str:
        """Extract the first paragraph of *body*, truncated to *max_chars*.

        Leading ``**tags**:`` / ``**keywords**:`` metadata lines are stripped
        so the summary reflects actual prose rather than structural markup.
        """
        # Strip leading metadata-only lines (tags / keywords).
        lines = body.splitlines()
        first_content = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not cls._TAGS_KEYWORDS_LINE_RE.match(stripped):
                first_content = i
                break

        cleaned = "\n".join(lines[first_content:])

        # Split on double newline — first "paragraph"
        para = cleaned.split("\n\n", 1)[0]
        # Collapse single newlines (wrapped lines)
        para = para.replace("\n", " ")
        if len(para) > max_chars:
            para = para[: max_chars - 3] + "..."
        return para.strip()

    @staticmethod
    def _extract_domain(text: str) -> Optional[str]:
        """Parse the ``<!-- domain: <path> -->`` header marker from *text*.

        Returns the layered path string, or ``None`` when the marker is absent
        or empty. Mechanical extraction (no LLM); the marker is expected near
        the top of the spec alongside the ``<!-- spec-format: v1 -->`` marker.
        """
        m = _DOMAIN_MARKER_RE.search(text)
        if not m:
            return None
        value = m.group(1).strip()
        return value or None

    @classmethod
    def _extract_locator(cls, header_text: str, max_chars: int = 200) -> str:
        """Extract a one-sentence locator from the ``## Purpose`` first paragraph.

        Reads the first paragraph beneath the ``## Purpose`` heading in the
        spec's shared header, collapses wrapped lines, and truncates to
        *max_chars*. Returns the empty string when no Purpose section exists.
        """
        m = _PURPOSE_HEADING_RE.search(header_text)
        if not m:
            return ""
        after = header_text[m.end():]
        # Cut at the next H2 heading so we stay within the Purpose section.
        nxt = _H2_ANY_RE.search(after)
        if nxt:
            after = after[: nxt.start()]
        para = after.strip().split("\n\n", 1)[0]
        para = para.replace("\n", " ").strip()
        if len(para) > max_chars:
            para = para[: max_chars - 3] + "..."
        return para

    # -- backwards-compat property -----------------------------------------

    @property
    def specs(self) -> Dict[str, _SimpleSpecInfo]:
        """File-level view for backwards compatibility (e.g. sync_engine)."""
        if self._specs is None:
            self._specs = {}
            seen: Set[str] = set()
            for item in self.items.values():
                if item.spec_name not in seen:
                    seen.add(item.spec_name)
                    self._specs[item.spec_name] = _SimpleSpecInfo(
                        name=item.spec_name,
                        path=Path(item.spec_path),
                    )
        return self._specs

    # -- build / rebuild ----------------------------------------------------

    def build(self) -> "SpecIndex":
        """Full scan of *specs_dir* and rebuild of the entire index."""
        self.items.clear()
        self.spec_metas.clear()
        self._specs = None
        self._first_item_per_spec.clear()

        if not self.specs_dir.exists():
            return self

        for spec_dir in self.specs_dir.iterdir():
            if not spec_dir.is_dir():
                continue
            spec_file = spec_dir / "spec.md"
            if not spec_file.exists():
                continue
            self._index_spec(spec_dir.name, spec_file)

        return self

    def rebuild_for(self, spec_name: str) -> None:
        """Rebuild items belonging to *spec_name* only."""
        # Remove existing items for this spec
        keys_to_remove = [
            k for k in self.items if k.startswith(f"{spec_name}::")
        ]
        for k in keys_to_remove:
            del self.items[k]
        self.spec_metas.pop(spec_name, None)
        self._specs = None
        self._first_item_per_spec.pop(spec_name, None)

        spec_file = self.specs_dir / spec_name / "spec.md"
        if spec_file.exists():
            self._index_spec(spec_name, spec_file)

    @staticmethod
    def _h2_heading_lines(text: str) -> List[int]:
        """1-based line numbers of ``## `` headings, excluding fenced code blocks.

        Mirrors the v1 parser's body-termination rule (a ``## `` second-level
        heading ends a Requirement body), so ``se3 spec show`` returns exactly
        the Requirement block and not any trailing ``## Appendix`` section.
        Only headings with exactly two leading hashes match (``###``/``####``
        Requirement/Scenario headings and deeper do not).
        """
        result: List[int] = []
        in_fence = False
        for i, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if re.match(r"^##\s+", line):
                result.append(i)
        return result

    @staticmethod
    def _h2_headings(text: str) -> List[tuple[int, str]]:
        """1-based ``(line, heading_text)`` of ``## `` headings outside fences.

        ``heading_text`` is the heading content with the leading ``## `` markers
        and surrounding whitespace stripped (e.g. ``"Requirements"``). Used to
        derive each Requirement's enclosing chapter ``section``. Only exactly
        two-hash headings match (``###``/deeper Requirement/Scenario headings do
        not), matching the v1 Requirement-body boundary rule.
        """
        result: List[tuple[int, str]] = []
        in_fence = False
        for i, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            m = re.match(r"^##\s+(.*\S)\s*$", line)
            if m:
                result.append((i, m.group(1).strip()))
        return result

    @staticmethod
    def _h4_dividers(text: str) -> List[tuple[int, str]]:
        """1-based ``(line, heading_text)`` of ``#### `` sub-section *divider* headings.

        A ``#### `` heading qualifies as a divider only when the next non-blank
        line after it is a ``### Requirement:`` heading — i.e. it introduces a run
        of Requirements rather than being prose inside a single Requirement's
        body. Fenced code blocks are skipped, and exactly four leading hashes
        match (``###``/``#####`` headings do not). Such a divider opens a
        sub-section that every following Requirement belongs to until the next
        divider or a new ``## `` chapter, giving the spec-view renderer a deeper
        semantic boundary to prefer before deterministic name-range pagination.
        """
        raw = text.splitlines()
        n = len(raw)
        # 1-based "is this line code (fence delimiter or inside a fence)?" map so
        # both the heading scan and the look-ahead respect code fences.
        in_fence: List[bool] = [False] * (n + 1)
        fence = False
        for i, line in enumerate(raw, start=1):
            if line.lstrip().startswith("```"):
                fence = not fence
                in_fence[i] = True
                continue
            in_fence[i] = fence

        req_re = re.compile(r"^###\s+Requirement:\s+\S")
        h4_re = re.compile(r"^####\s+(.*\S)\s*$")
        dividers: List[tuple[int, str]] = []
        for i, line in enumerate(raw, start=1):
            if in_fence[i]:
                continue
            m = h4_re.match(line)
            if not m:
                continue
            # Look ahead to the first non-blank line; a divider is one whose next
            # non-blank line is a (non-fenced) ``### Requirement:`` heading.
            j = i + 1
            while j <= n and not raw[j - 1].strip():
                j += 1
            if j <= n and not in_fence[j] and req_re.match(raw[j - 1]):
                dividers.append((i, m.group(1).strip()))
        return dividers

    def _index_spec(self, spec_name: str, spec_file: Path) -> None:
        """Parse a single spec file and add its Requirements to *items*."""
        self._specs = None
        # Read the raw bytes ONCE so the parsed content, the recorded size, and
        # the recorded content hash all describe a single consistent snapshot of
        # the file. Reading the text and then separately re-reading the file for
        # stat()/hash would create a race window: an atomic replacement of the
        # spec between the two reads would store the OLD items/line ranges keyed
        # to the NEW file's hash, after which ``needs_rebuild`` (which compares
        # the cached hash against the on-disk hash) would see a match and serve
        # the stale index indefinitely. Hashing the same bytes we parsed closes
        # that window — if the file is replaced after this read, the cached hash
        # is the old content's hash and the next reconciliation detects the drift.
        try:
            data = spec_file.read_bytes()
        except OSError as exc:
            logger.warning("Failed to read spec %s: %s", spec_name, exc)
            return

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            logger.warning("Failed to decode spec %s: %s", spec_name, exc)
            return

        try:
            parsed = parse_spec(text)
        except Exception as exc:
            logger.warning("Failed to parse spec %s: %s", spec_name, exc)
            return

        # Size and hash are derived from the in-memory snapshot, not a fresh
        # disk read. The mtime is non-authoritative (it is only a cheap hint;
        # the SHA-256 prefix is the authoritative change signal), so reading it
        # from a separate stat() is harmless even under a concurrent replace.
        try:
            mtime = spec_file.stat().st_mtime
        except OSError:
            mtime = 0.0
        size = len(data)
        sha256_prefix = self._sha256_prefix_of_bytes(data)

        # Total line count of the file, used to bound the last Requirement's
        # physical interval at EOF. ``splitlines()`` matches how
        # ``resolve_item_location`` slices the body (a trailing newline does
        # not create a phantom final line), so the stored interval stays
        # consistent with the readable body.
        total_lines = len(text.splitlines())

        # Spec-level navigation metadata (program-derived, no LLM).
        self.spec_metas[spec_name] = SpecMeta(
            spec_name=spec_name,
            domain=self._extract_domain(text),
            locator=self._extract_locator(parsed.header_text),
            item_count=len(parsed.requirements),
        )

        # A ``## `` heading (any second-level section: an orphan ``## Notes`` /
        # ``## Appendix`` between Requirements, or a trailing section after the
        # last one) terminates a Requirement's body per the v1 boundary rule. A
        # Requirement's physical interval must therefore stop at the first such
        # heading after it and never spill across a trailing section into EOF.
        # ``_h2_heading_lines`` returns 1-based line numbers against the file
        # text — the same coordinate system as ``req.line_start`` /
        # ``total_lines``.
        h2_lines = self._h2_heading_lines(text)

        # Headings with their text, used to derive each Requirement's enclosing
        # chapter ``section`` (the nearest preceding ``## `` heading).
        h2_headings = self._h2_headings(text)

        def _next_h2_after(line: int) -> Optional[int]:
            for ln in h2_lines:
                if ln > line:
                    return ln
            return None

        def _section_of(line: int) -> str:
            """The nearest ``## `` heading text at or before *line*."""
            sec = ""
            for ln, heading in h2_headings:
                if ln <= line:
                    sec = heading
                else:
                    break
            return sec

        # ``#### `` sub-section dividers, used to derive each Requirement's
        # enclosing ``subsection`` (the most recent divider at or before it,
        # scoped to its ``## `` chapter so a sub-section never leaks across a
        # chapter boundary).
        h4_dividers = self._h4_dividers(text)

        def _subsection_of(line: int) -> str:
            chapter_line = 0
            for ln in h2_lines:
                if ln <= line:
                    chapter_line = ln
                else:
                    break
            sub = ""
            for ln, heading in h4_dividers:
                if ln > line:
                    break
                if ln > chapter_line:
                    sub = heading
            return sub

        first_item: Optional[ItemMeta] = None
        reqs = parsed.requirements
        for i, req in enumerate(reqs):
            # line_end is the line just before the next Requirement's heading,
            # or the last line of the file for the final Requirement (the
            # interval is 1-based and inclusive).
            if i + 1 < len(reqs):
                hard_end = reqs[i + 1].line_start - 1
            else:
                hard_end = total_lines
            # Tighten the bound to the first intervening ``## `` heading so the
            # final Requirement (and any Requirement followed by a ``## ``
            # section before the next one) does not absorb that section.
            h2 = _next_h2_after(req.line_start)
            if h2 is not None and h2 - 1 < hard_end:
                hard_end = h2 - 1
            line_end = max(req.line_start, hard_end)
            item = ItemMeta(
                spec_name=spec_name,
                requirement_name=req.name,
                spec_path=str(spec_file),
                mtime=mtime,
                size=size,
                sha256_prefix=sha256_prefix,
                tags=req.tags,
                keywords=req.keywords,
                refs=req.refs,
                summary=self._make_summary(req.body),
                line_start=req.line_start,
                line_end=line_end,
                section=_section_of(req.line_start),
                subsection=_subsection_of(req.line_start),
            )
            self.items[item.item_id] = item
            if first_item is None:
                first_item = item

        if not parsed.requirements:
            # Spec has no Requirements — still record metadata so
            # needs_rebuild() can detect changes.
            item = ItemMeta(
                spec_name=spec_name,
                requirement_name=_NO_REQUIREMENTS_SENTINEL,
                spec_path=str(spec_file),
                mtime=mtime,
                size=size,
                sha256_prefix=sha256_prefix,
                summary="",
            )
            self.items[item.item_id] = item
            first_item = item

        if first_item is not None:
            self._first_item_per_spec[spec_name] = first_item

    # -- save / load --------------------------------------------------------

    def save(self) -> None:
        """Persist index to disk atomically.

        NOTE: This method does NOT acquire a file lock internally. Callers
        that require cross-process atomicity (e.g. ``load_or_build``) should
        hold the lock *before* calling ``save()``.
        """
        self._index_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": INDEX_VERSION,
            "items": {k: v.to_dict() for k, v in self.items.items()},
            "specs": {
                name: meta.to_dict() for name, meta in self.spec_metas.items()
            },
        }
        tmp = self._index_file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self._index_file)

    def load(self) -> bool:
        """Load index from disk. Returns ``True`` on success."""
        if not self._index_file.exists():
            return False
        try:
            with open(self._index_file, encoding="utf-8") as f:
                data = json.load(f)
            # Version check: if future versions add incompatible schema
            # changes, force a rebuild rather than silently misparse.
            version = data.get("version")
            if version is None or version != INDEX_VERSION:
                logger.info(
                    "Spec index version %s is incompatible (expected %s); rebuilding.",
                    version,
                    INDEX_VERSION,
                )
                return False
            self.items = {
                k: ItemMeta.from_dict(v)
                for k, v in data.get("items", {}).items()
            }
            self.spec_metas = {
                name: SpecMeta.from_dict(name, meta)
                for name, meta in data.get("specs", {}).items()
            }
            self._specs = None
            # Rebuild _first_item_per_spec from loaded items
            self._first_item_per_spec.clear()
            for item in self.items.values():
                if item.spec_name not in self._first_item_per_spec:
                    self._first_item_per_spec[item.spec_name] = item
            return True
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Failed to load spec index: %s", exc)
            return False

    # -- incremental validation / rebuild ----------------------------------

    def needs_rebuild(self, spec_name: str) -> bool:
        """Check whether *spec_name* needs re-indexing.

        Validates the cached entry against the on-disk **content hash** on every
        call, not merely against mtime + size. A spec edited so that its size is
        unchanged and its mtime is restored to the cached value (e.g. an
        in-place same-length rewrite followed by ``os.utime``) leaves both
        metadata values equal yet has different content; trusting mtime/size
        alone would then serve a stale index (wrong item names, summaries, and
        line locations) indefinitely. The SHA-256 prefix is the authoritative
        change signal, so it is always recomputed here — the spec files are
        small and this runs once per spec per CLI invocation.
        """
        spec_file = self.specs_dir / spec_name / "spec.md"
        if not spec_file.exists():
            # Spec was deleted — any cached items for it are stale
            return any(
                k.startswith(f"{spec_name}::") for k in self.items
            )

        try:
            spec_file.stat()
        except OSError:
            return True

        # O(1) lookup via _first_item_per_spec cache
        cached = self._first_item_per_spec.get(spec_name)
        if cached is None:
            return True

        # Always validate with the content hash. mtime/size are not trusted as
        # a sufficient currency proof, because a same-length edit with a
        # restored mtime would slip past a metadata-only check.
        try:
            current_hash = self._compute_sha256_prefix(spec_file)
        except OSError:
            return True
        return cached.sha256_prefix != current_hash

    # -- query API ----------------------------------------------------------

    def get_item(self, spec_name: str, requirement_name: str) -> Optional[ItemMeta]:
        """Look up a single item by its compound key."""
        return self.items.get(f"{spec_name}::{requirement_name}")

    def get_spec_meta(self, spec_name: str) -> Optional[SpecMeta]:
        """Return the spec-level navigation metadata for *spec_name*, if any."""
        return self.spec_metas.get(spec_name)

    def resolve_item_location(
        self,
        spec_name: str,
        requirement_name: str,
    ) -> Optional[tuple[str, int, int, str]]:
        """Resolve a logical item address to its physical location and body.

        Looks up ``<spec>::<requirement>`` in the index, reads the recorded
        physical line interval from the spec file, and returns
        ``(spec_path, line_start, line_end, body)`` where ``body`` is exactly
        the text of lines ``[line_start, line_end]`` (1-based, inclusive) — so
        the returned body and the physical interval are consistent by
        construction.

        Returns ``None`` for a non-item address (the no-requirements sentinel),
        an unknown address, an item lacking a recorded ``line_start``, or when
        the spec file cannot be read.
        """
        if requirement_name == _NO_REQUIREMENTS_SENTINEL:
            return None
        item = self.get_item(spec_name, requirement_name)
        if item is None:
            return None
        if item.line_start < 1:
            return None

        # Read the spec file and slice the recorded interval, but ONLY after
        # confirming the on-disk content still matches the indexed snapshot
        # hash. If the spec file was atomically replaced after the index was
        # loaded/built (mtime/size unchanged or not), slicing the recorded line
        # interval into the *new* file could return an unrelated Requirement
        # under the requested logical address. To stay sound we hash the full
        # content and slice the target lines in a SINGLE streaming pass (so the
        # body we slice and the hash we validate come from the same snapshot —
        # no read-vs-hash TOCTOU inside this method — without ever holding the
        # whole file in memory: only the requested line interval is retained).
        # On a hash mismatch we reconcile by rebuilding this spec's items ONCE
        # and re-resolving against the fresh interval before reading again. A
        # bounded two-attempt loop guarantees termination even if the file is
        # being rewritten in a tight loop.
        for reconcile_attempt in range(2):
            spec_path = Path(item.spec_path)
            start = item.line_start
            # Effective end: a stale/degenerate line_end below line_start
            # collapses to the single heading line.
            eff_end = item.line_end if item.line_end >= start else start
            try:
                current_hash, total, body = self._hash_and_slice_lines(
                    spec_path, start, eff_end
                )
            except OSError as exc:
                logger.warning(
                    "resolve_item_location: failed to read %s: %s", spec_path, exc
                )
                return None
            except UnicodeDecodeError as exc:
                logger.warning(
                    "resolve_item_location: failed to decode %s: %s", spec_path, exc
                )
                return None

            if current_hash != item.sha256_prefix:
                # The on-disk content drifted from the indexed snapshot whose
                # line interval we hold. Never slice stale coordinates into a
                # changed file.
                if reconcile_attempt == 0:
                    logger.info(
                        "resolve_item_location: %s changed since indexing "
                        "(hash %s != %s); rebuilding spec '%s' before slicing.",
                        spec_path, current_hash, item.sha256_prefix, spec_name,
                    )
                    self.rebuild_for(spec_name)
                    refreshed = self.get_item(spec_name, requirement_name)
                    if refreshed is None or refreshed.line_start < 1:
                        # The Requirement no longer exists under this logical
                        # address in the new content (renamed / removed).
                        return None
                    item = refreshed
                    continue
                # Still mismatched after one rebuild — refuse to return a
                # possibly inconsistent body rather than slice a stale interval.
                logger.warning(
                    "resolve_item_location: %s still drifting after rebuild; "
                    "refusing to slice a stale interval.", spec_path,
                )
                return None

            if total == 0 or start > total:
                # Empty file, or the recorded heading line is past EOF (the file
                # shrank since indexing). The hash matched, so this only happens
                # for a degenerate / out-of-range coordinate; refuse to return.
                return None

            # Clamp the reported end to the file's actual length (the file may
            # have shrunk; the streamed slice already stopped at EOF, so the
            # returned body and this clamped interval stay consistent).
            end = min(eff_end, total)
            return (str(spec_path), start, end, body)

        return None

    def list_all(self) -> List[ItemMeta]:
        """Return all indexed items, sorted by compound key."""
        return [self.items[k] for k in sorted(self.items)]

    def list_for_selector(self) -> List[Dict[str, Any]]:
        """Return a stable list of dicts for the analyze selector prompt.

        Each dict contains: ``spec``, ``requirement_name``, ``tags``, ``summary``.
        """
        result: List[Dict[str, Any]] = []
        for key in sorted(self.items):
            item = self.items[key]
            # Skip the sentinel for no-requirement specs
            if item.requirement_name == _NO_REQUIREMENTS_SENTINEL:
                continue
            result.append(
                {
                    "spec": item.spec_name,
                    "requirement_name": item.requirement_name,
                    "tags": item.tags,
                    "summary": item.summary,
                }
            )
        return result

    def resolve_refs(
        self,
        item_id: str,
        max_hops: int = 1,
    ) -> Set[str]:
        """Resolve literal references from *item_id*, expanding up to *max_hops*.

        Returns a set of additional item_ids that should be loaded alongside
        the original. Missing refs are silently ignored with a warning.
        """
        if max_hops < 1:
            return set()

        item = self.items.get(item_id)
        if item is None:
            logger.warning("resolve_refs: item %s not found", item_id)
            return set()

        extra: Set[str] = set()
        seen: Set[str] = {item_id}
        frontier: Set[str] = {item_id}

        for _hop in range(max_hops):
            next_frontier: Set[str] = set()
            for current_id in frontier:
                current = self.items.get(current_id)
                if current is None:
                    continue
                for ref in current.refs:
                    # ref may be:
                    #   - intra-spec: "Requirement Name"  (resolve against current spec)
                    #   - inter-spec: "spec::Requirement Name"
                    if "::" in ref:
                        target_id = ref
                    else:
                        # Intra-spec: prepend the current spec
                        target_id = f"{current.spec_name}::{ref}"

                    if target_id not in self.items:
                        # Try stripping trailing punctuation (refs extracted
                        # from sentences may include a period/comma)
                        target_id_stripped = target_id.rstrip(".,;:!?")
                        if target_id_stripped in self.items:
                            target_id = target_id_stripped
                        else:
                            # Downgrade to debug — unresolved refs from
                            # prose-level mentions are expected noise, not
                            # actionable warnings.
                            logger.debug(
                                "resolve_refs: ref %s from %s points to non-existent item",
                                ref,
                                current_id,
                            )
                            continue
                    if target_id not in seen:
                        seen.add(target_id)
                        next_frontier.add(target_id)
                        if target_id != item_id:
                            extra.add(target_id)
            frontier = next_frontier
            if not frontier:
                break

        return extra


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _compute_specs_to_rebuild(index: "SpecIndex") -> List[str]:
    """Specs that are stale on disk or were deleted, vs *index*'s current state.

    Pure read against ``index`` + the spec directory. Re-evaluable: callers run
    it both before acquiring the index lock (to decide whether any work is
    needed) and again after reloading the cache under the lock (so a spec a
    concurrent writer already refreshed drops out of the rebuild set).
    """
    specs_to_rebuild: List[str] = []

    # Every spec currently represented in the cache (item rows + metadata rows).
    indexed_specs = {item.spec_name for item in index.items.values()}
    indexed_specs.update(index.spec_metas.keys())

    if not index.specs_dir.exists():
        # The whole specs directory vanished: every cached spec is now deleted
        # on disk and MUST be purged, otherwise subsequent index commands keep
        # displaying stale specs and item locations from the cache. Rebuilding a
        # spec whose ``spec.md`` no longer exists removes its rows (see
        # ``SpecIndex.rebuild_for``). Sorted for deterministic logging.
        return sorted(indexed_specs)

    # Stale specs still present on disk.
    for spec_dir in index.specs_dir.iterdir():
        if not spec_dir.is_dir():
            continue
        spec_file = spec_dir / "spec.md"
        if not spec_file.exists():
            continue
        if index.needs_rebuild(spec_dir.name):
            specs_to_rebuild.append(spec_dir.name)

    # Specs that were indexed but have since been deleted from disk.
    disk_specs = {
        spec_dir.name
        for spec_dir in index.specs_dir.iterdir()
        if spec_dir.is_dir() and (spec_dir / "spec.md").exists()
    }
    for spec_name in indexed_specs - disk_specs:
        if spec_name not in specs_to_rebuild:
            specs_to_rebuild.append(spec_name)

    return specs_to_rebuild


def load_or_build(project_root: Path) -> SpecIndex:
    """Load an existing index or build a fresh one.

    Performs incremental validation:
    - If the index file does not exist → full build.
    - Otherwise load the cached index, then scan all spec files.
    - For each spec whose mtime/size/hash differs, rebuild that spec only.
    """
    index = SpecIndex(project_root)

    loaded = index.load()
    if not loaded:
        # Acquire lock before initial build to prevent redundant work
        # when multiple processes start with no index file.
        lock_file = Path(str(index._index_file) + ".lock")
        lock_acquired = False
        try:
            with open(lock_file, "w") as lock_fd:
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    lock_acquired = True
                # Double-check: another process may have built while waiting
                if index.load():
                    logger.debug("Spec index built by another process; skipping.")
                    if fcntl is not None:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                        lock_acquired = False
                    return index
                logger.info("Spec index not found; building from scratch.")
                index.build()
                index.save()
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_acquired = False
        except OSError:
            logger.warning(
                "File lock not available; building spec index without coordination."
            )
            index.build()
            index.save()
            if lock_acquired and fcntl is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        return index

    # Determine which specs need rebuilding (against the just-loaded cache).
    specs_to_rebuild = _compute_specs_to_rebuild(index)

    if specs_to_rebuild:
        # Advisory exclusive lock around rebuild+save to prevent a second
        # writer from overwriting fresher data with a stale snapshot.
        lock_file = Path(str(index._index_file) + ".lock")
        lock_acquired = False
        try:
            with open(lock_file, "w") as lock_fd:
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    lock_acquired = True
                # Now that we hold the lock, RELOAD the latest on-disk index:
                # another process may have rebuilt and saved a fresher snapshot
                # (for a different spec) while we computed the stale set or
                # waited for the lock. Without this reload we would save our own
                # stale copy of that spec and clobber the other writer's update.
                # ``load()`` only mutates state on success, so a vanished /
                # corrupt file leaves our in-memory cache intact (we then
                # proceed with the originally-computed set).
                index.load()
                # Re-check against the freshly loaded cache — a spec another
                # process already rebuilt is no longer stale and is skipped,
                # while genuinely stale / deleted specs are rebuilt here.
                still_stale = _compute_specs_to_rebuild(index)
                if still_stale:
                    logger.info(
                        "Rebuilding spec index for: %s",
                        ", ".join(still_stale),
                    )
                    for spec_name in still_stale:
                        index.rebuild_for(spec_name)
                    index.save()
                else:
                    logger.debug("Spec index rebuilt by another process; skipping.")
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_acquired = False
        except OSError:
            # Fallback: rebuild without lock (e.g. filesystem doesn't support flock)
            logger.warning(
                "File lock not available; rebuilding spec index without coordination: %s",
                ", ".join(specs_to_rebuild),
            )
            for spec_name in specs_to_rebuild:
                index.rebuild_for(spec_name)
            index.save()
            if lock_acquired and fcntl is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
    else:
        logger.debug("Spec index is up to date.")

    return index


# ---------------------------------------------------------------------------
# Deprecated helpers — kept for API stability
# ---------------------------------------------------------------------------

def get_or_build_index(project_root: Path) -> SpecIndex:
    """Deprecated: use ``load_or_build`` instead."""
    import warnings
    warnings.warn(
        "get_or_build_index is deprecated; use load_or_build instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return load_or_build(project_root)
