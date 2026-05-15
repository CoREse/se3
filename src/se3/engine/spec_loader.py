"""Spec loader — item-level and full-spec loading for LLM prompts.

Provides ``load_for_step()`` which assembles spec text for downstream steps.
Two modes:
- ``items``: base spec full text + each involved spec's header + selected items + 1-hop refs
- ``full_spec``: base spec full text + each involved spec's full text

The loader delegates to :func:`spec_index.load_or_build` for index management
and :func:`spec_format.parse_spec` for parsing individual spec files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set

from .spec_format import parse_spec, SPEC_FORMAT_VERSION_MARKER
from .spec_index import load_or_build, SpecIndex

logger = logging.getLogger(__name__)


@dataclass
class LoadResult:
    """Result of loading specs for a step."""

    text: str
    """Assembled spec text ready for LLM prompt injection."""

    relevant_specs: List[str]
    """Distinct spec names that contributed content (includes 'base')."""

    loaded_items: List[str]
    """Item IDs actually loaded (selected items + refs expansion), deduplicated."""


# Module-level cache for spec text: (specs_dir_str, spec_name) -> (text, mtime, size)
# Guards against repeated disk reads when _build_step_inputs is called
# multiple times (transitions, retries, fix loops).
# LRU cap of 64 entries prevents unbounded growth in long-running processes.
_SPEC_TEXT_CACHE: dict[tuple[str, str], tuple[str, float, int]] = {}
_SPEC_TEXT_CACHE_ORDER: list[tuple[str, str]] = []
_MAX_CACHE_SIZE = 64


def _read_spec_text(specs_dir: Path, spec_name: str) -> Optional[str]:
    """Read raw spec markdown text for a spec by name.

    Results are cached by (directory, spec_name) keyed on (mtime, size)
    to avoid repeated disk reads across multiple calls in the same process.
    The cache is capped at ``_MAX_CACHE_SIZE`` entries (LRU eviction).
    """
    spec_file = specs_dir / spec_name / "spec.md"
    if not spec_file.exists():
        return None

    cache_key = (str(specs_dir), spec_name)
    try:
        stat = spec_file.stat()
        mtime = stat.st_mtime
        size = stat.st_size
    except OSError:
        mtime = 0.0
        size = 0

    cached = _SPEC_TEXT_CACHE.get(cache_key)
    if cached is not None:
        text, cached_mtime, cached_size = cached
        if mtime == cached_mtime and size == cached_size:
            # Touch for LRU ordering
            if cache_key in _SPEC_TEXT_CACHE_ORDER:
                _SPEC_TEXT_CACHE_ORDER.remove(cache_key)
            _SPEC_TEXT_CACHE_ORDER.append(cache_key)
            return text

    try:
        text = spec_file.read_text(encoding="utf-8")
        # LRU eviction
        if cache_key in _SPEC_TEXT_CACHE_ORDER:
            _SPEC_TEXT_CACHE_ORDER.remove(cache_key)
        _SPEC_TEXT_CACHE_ORDER.append(cache_key)
        if len(_SPEC_TEXT_CACHE_ORDER) > _MAX_CACHE_SIZE:
            oldest = _SPEC_TEXT_CACHE_ORDER.pop(0)
            _SPEC_TEXT_CACHE.pop(oldest, None)
        _SPEC_TEXT_CACHE[cache_key] = (text, mtime, size)
        return text
    except OSError as exc:
        logger.warning("Failed to read spec %s: %s", spec_name, exc)
    return None


def _assemble_items_text(
    index: SpecIndex,
    specs_dir: Path,
    selected_items: List[Dict[str, str]],
) -> tuple[str, List[str], List[str]]:
    """Assemble spec text in 'items' mode.

    Returns (text, relevant_specs, loaded_item_ids).
    """
    # Collect selected item IDs.
    # Expand * wildcard: spec::* means "all items in this spec".
    selected_ids: Set[str] = set()
    for item in selected_items:
        spec = item.get("spec", "").strip()
        req = item.get("requirement_name", "").strip()
        if spec and req:
            if req == _ALL_ITEMS_WILDCARD:
                # Expand to every known item in this spec
                prefix = f"{spec}::"
                for key in sorted(index.items):
                    if key.startswith(prefix):
                        selected_ids.add(key)
            else:
                selected_ids.add(f"{spec}::{req}")

    # Resolve 1-hop refs
    extra_ids: Set[str] = set()
    for item_id in selected_ids:
        extra = index.resolve_refs(item_id, max_hops=1)
        extra_ids.update(extra)

    all_ids = selected_ids | extra_ids

    # Group by spec name
    spec_to_reqs: Dict[str, Set[str]] = {}
    for item_id in all_ids:
        if "::" not in item_id:
            continue
        spec_name, req_name = item_id.split("::", 1)
        spec_to_reqs.setdefault(spec_name, set()).add(req_name)

    # Determine relevant specs (ordered: base first, then alphabetically)
    involved_specs = sorted(spec_to_reqs.keys())
    relevant_specs: List[str] = []

    parts: List[str] = []

    # --- Base spec: always full text ---
    base_text = _read_spec_text(specs_dir, "base")
    if base_text:
        parts.append(base_text)
        parts.append("")
        relevant_specs.append("base")
    else:
        logger.debug("Base spec not found at %s", specs_dir / "base" / "spec.md")

    # Track which items were actually found in parsed specs (for hallucination detection)
    found_item_ids: Set[str] = set()

    # --- Other specs: header + selected items ---
    for spec_name in involved_specs:
        if spec_name == "base":
            # Base is already loaded fully above; skip individual items
            continue

        raw_text = _read_spec_text(specs_dir, spec_name)
        if not raw_text:
            continue

        try:
            parsed = parse_spec(raw_text)
        except Exception as exc:
            logger.warning("Failed to parse spec %s: %s", spec_name, exc)
            continue

        # Build a requirement name -> body map for fast lookup
        req_map: Dict[str, str] = {}
        req_line_start: Dict[str, int] = {}
        for req in parsed.requirements:
            req_map[req.name] = req.body
            req_line_start[req.name] = req.line_start

        # Determine which requirements to include for this spec
        req_names = spec_to_reqs.get(spec_name, set())
        if not req_names:
            continue

        # Assemble: header + selected requirements + trailing text
        spec_parts: List[str] = []

        # Header (title + shared sections)
        header = parsed.header_text.strip()
        if header:
            spec_parts.append(header)

        # Each selected requirement — preserve file order via line_start
        included_reqs = 0
        for req_name in sorted(req_names, key=lambda n: req_line_start.get(n, float("inf"))):
            body = req_map.get(req_name)
            if body is None:
                logger.warning(
                    "Requirement '%s' not found in spec '%s'", req_name, spec_name
                )
                continue
            spec_parts.append(f"\n### Requirement: {req_name}\n")
            spec_parts.append(body)
            included_reqs += 1
            found_item_ids.add(f"{spec_name}::{req_name}")

        # Preserve trailing text (orphan H2 sections after last requirement)
        if parsed.trailing_text:
            spec_parts.append("")
            spec_parts.append(parsed.trailing_text)

        # Include the spec if at least one requirement was found, or if
        # this spec was explicitly targeted by selected_ids (so the header
        # is preserved even when all selected requirements are hallucinated).
        had_selected_target = any(
            item_id.startswith(f"{spec_name}::") for item_id in selected_ids
        )
        if included_reqs == 0 and not parsed.trailing_text and not had_selected_target:
            continue

        parts.append("\n".join(spec_parts))
        parts.append("")
        relevant_specs.append(spec_name)

    full_text = "\n".join(parts).strip()
    # loaded_items = items that were actually found in parsed specs
    # (excludes hallucinated requirements and base spec items)
    loaded_items = sorted(id for id in found_item_ids if not id.startswith("base::"))

    return full_text, relevant_specs, loaded_items


# Wildcard requirement_name that means "all items in this spec".
# When the LLM outputs {"spec": "base", "requirement_name": "*"}, it means
# "no non-base items are relevant" — base is always loaded in full anyway.
# When applied to a non-base spec, it selects every requirement in that spec.
_ALL_ITEMS_WILDCARD = "*"


def _assemble_full_text(
    specs_dir: Path,
    selected_items: List[Dict[str, str]],
) -> tuple[str, List[str], List[str]]:
    """Assemble spec text in 'full_spec' mode.

    Returns (text, relevant_specs, loaded_item_ids derived from all items in involved specs).

    Raises ValueError if *selected_items* is empty — by this point the
    analyze step should have guaranteed a non-empty list (at minimum
    ``base::*``).
    """
    if not selected_items:
        raise ValueError(
            "full_spec mode requires non-empty selected_items; got [] — "
            "this usually means the analyze step failed to select any spec "
            "items relevant to the task. Check ANALYZE outputs."
        )

    # Determine involved specs from selected items + base.
    # The * wildcard works naturally here: base::* adds "base" (already in
    # the set), other-spec::* adds that spec's full text.
    involved_specs: Set[str] = {"base"}
    for item in selected_items:
        spec = item.get("spec", "").strip()
        if spec:
            involved_specs.add(spec)

    parts: List[str] = []
    relevant_specs: List[str] = []
    loaded_items: List[str] = []

    # Order: base first, then alphabetically
    for spec_name in sorted(involved_specs, key=lambda s: (s != "base", s)):
        raw_text = _read_spec_text(specs_dir, spec_name)
        if raw_text:
            parts.append(raw_text)
            parts.append("")
            relevant_specs.append(spec_name)

            # Also collect all item IDs from this spec for loaded_items
            try:
                parsed = parse_spec(raw_text)
                for req in parsed.requirements:
                    loaded_items.append(f"{spec_name}::{req.name}")
            except Exception:
                pass

    return "\n".join(parts).strip(), relevant_specs, sorted(loaded_items)


def load_for_step(
    step_type: str,
    selected_items: Optional[List[Dict[str, str]]],
    project_root: Path,
    mode: Literal["items", "full_spec"] = "items",
) -> LoadResult:
    """Load spec content for a given step.

    Args:
        step_type: Current step type (for logging / future extensibility).
        selected_items: List of ``{"spec": str, "requirement_name": str}`` dicts
            chosen by the analyze step selector. May be empty or None.
        project_root: Project root directory.
        mode: ``"items"`` for header+items assembly (default), or
            ``"full_spec"`` for full text of each involved spec.

    Returns:
        LoadResult with assembled text, relevant spec names, and loaded item IDs.
    """
    project_root = Path(project_root)
    specs_dir = project_root / "se3" / "specs"

    # Fallback to empty list
    if selected_items is None:
        selected_items = []

    # Ensure selected_items is a list of dicts
    valid_items: List[Dict[str, str]] = []
    for item in selected_items:
        if isinstance(item, dict) and "spec" in item and "requirement_name" in item:
            valid_items.append(item)
    selected_items = valid_items

    if mode == "items":
        # Build/load the index
        index = load_or_build(project_root)
        text, relevant_specs, loaded_items = _assemble_items_text(
            index, specs_dir, selected_items
        )
    elif not selected_items:
        # full_spec mode with empty selected_items.  This is a safety net:
        # the analyze step should guarantee non-empty (at minimum base::*),
        # but pre-existing persisted flows may have empty lists.
        logger.warning(
            "full_spec mode with empty selected_items for step=%s; "
            "loading base spec only (analyze should have produced at "
            "least base::*)",
            step_type,
        )
        raw_text = _read_spec_text(specs_dir, "base")
        if raw_text:
            text = raw_text
            relevant_specs = ["base"]
            loaded_items = []
            try:
                parsed = parse_spec(raw_text)
                loaded_items = sorted(
                    f"base::{req.name}" for req in parsed.requirements
                )
            except Exception:
                pass
        else:
            text = ""
            relevant_specs = []
            loaded_items = []
    else:
        # full_spec mode — no need for the index
        text, relevant_specs, loaded_items = _assemble_full_text(
            specs_dir, selected_items
        )

    logger.info(
        "Loaded specs for step=%s mode=%s specs=%s items=%d",
        step_type, mode, relevant_specs, len(loaded_items),
    )

    return LoadResult(text=text, relevant_specs=relevant_specs, loaded_items=loaded_items)


def load_full(
    spec_names: List[str],
    project_root: Path,
) -> str:
    """Load the full text of one or more spec files.

    Convenience helper for callers that don't need item-level selection.
    Always returns full spec text (not item-filtered).

    Args:
        spec_names: List of spec names to load (e.g. ``["base"]``).
        project_root: Project root directory.

    Returns:
        Concatenated spec text.
    """
    project_root = Path(project_root)
    specs_dir = project_root / "se3" / "specs"

    parts: List[str] = []
    for spec_name in spec_names:
        text = _read_spec_text(specs_dir, spec_name)
        if text:
            parts.append(text)
            parts.append("")

    return "\n".join(parts).strip()
