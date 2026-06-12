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

# Index schema version. Bumped from 1 -> 2 when ItemMeta gained physical line
# numbers (line_start / line_end) and a per-spec ``specs`` metadata section
# (domain / locator / item_count). A cached index whose ``version`` is not the
# current value is treated as a load miss and rebuilt from scratch, so an old
# v1 cache is never mis-read against the v2 schema.
INDEX_VERSION = 2

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

    def _index_spec(self, spec_name: str, spec_file: Path) -> None:
        """Parse a single spec file and add its Requirements to *items*."""
        self._specs = None
        try:
            text = spec_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read spec %s: %s", spec_name, exc)
            return

        try:
            parsed = parse_spec(text)
        except Exception as exc:
            logger.warning("Failed to parse spec %s: %s", spec_name, exc)
            return

        stat = spec_file.stat()
        mtime = stat.st_mtime
        size = stat.st_size
        sha256_prefix = self._compute_sha256_prefix(spec_file)

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

        first_item: Optional[ItemMeta] = None
        reqs = parsed.requirements
        for i, req in enumerate(reqs):
            # line_end is the line just before the next Requirement's heading,
            # or the last line of the file for the final Requirement (the
            # interval is 1-based and inclusive).
            if i + 1 < len(reqs):
                line_end = max(req.line_start, reqs[i + 1].line_start - 1)
            else:
                line_end = max(req.line_start, total_lines)
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

        Compares on-disk mtime + size with the cached values. If they differ,
        computes SHA-256 prefix for a definitive answer.
        """
        spec_file = self.specs_dir / spec_name / "spec.md"
        if not spec_file.exists():
            # Spec was deleted — any cached items for it are stale
            return any(
                k.startswith(f"{spec_name}::") for k in self.items
            )

        try:
            stat = spec_file.stat()
        except OSError:
            return True

        # O(1) lookup via _first_item_per_spec cache
        cached = self._first_item_per_spec.get(spec_name)
        if cached is None:
            return True

        if cached.mtime != stat.st_mtime or cached.size != stat.st_size:
            # mtime/size changed — verify with hash before declaring stale
            current_hash = self._compute_sha256_prefix(spec_file)
            return cached.sha256_prefix != current_hash

        return False

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

        spec_path = Path(item.spec_path)
        try:
            text = spec_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "resolve_item_location: failed to read %s: %s", spec_path, exc
            )
            return None

        lines = text.splitlines()
        total = len(lines)
        if total == 0:
            return None

        start = item.line_start
        # Clamp the end to the file's actual length (the file may have shrunk
        # since indexing; a stale cache would otherwise over-read).
        end = item.line_end if item.line_end >= start else start
        end = min(end, total)
        if start > total:
            return None

        body = "\n".join(lines[start - 1 : end])
        return (str(spec_path), start, end, body)

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

    # Determine which specs need rebuilding
    specs_to_rebuild: List[str] = []
    if index.specs_dir.exists():
        # Check existing specs on disk
        for spec_dir in index.specs_dir.iterdir():
            if not spec_dir.is_dir():
                continue
            spec_file = spec_dir / "spec.md"
            if not spec_file.exists():
                continue
            if index.needs_rebuild(spec_dir.name):
                specs_to_rebuild.append(spec_dir.name)

        # Check for specs that were indexed but have since been deleted
        indexed_specs = {item.spec_name for item in index.items.values()}
        disk_specs = {
            spec_dir.name
            for spec_dir in index.specs_dir.iterdir()
            if spec_dir.is_dir() and (spec_dir / "spec.md").exists()
        }
        for spec_name in indexed_specs - disk_specs:
            if spec_name not in specs_to_rebuild:
                specs_to_rebuild.append(spec_name)

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
                # Re-check after acquiring lock — another process may have
                # already rebuilt while we were waiting.
                still_stale: List[str] = []
                for spec_name in specs_to_rebuild:
                    if index.needs_rebuild(spec_name):
                        still_stale.append(spec_name)
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
