"""SE3 Sync Engine — Single-round, one-directional spec update from code.

The engine treats specs as the documented snapshot of code (spec-assistant),
not as a forward-looking source of truth. Each call to ``run_once`` performs
one stateless pass: it analyzes every spec against the current code and
applies any drift (gap/extension/conflict) by updating the spec to match.

Cross-round orchestration (convergence detection, oscillation guarding,
report aggregation) lives in ``SyncLoop`` (see ``sync_loop.py``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SYNC_TAGS = ["auto-discovered", "source:sync"]

_REQUIREMENT_HEADING_RE = re.compile(r"^###\s+Requirement:\s*(.+?)\s*$", re.MULTILINE)


def strip_markdown_fences(text: str) -> str:
    """Strip outermost markdown code fences if the text is wholly wrapped in them."""
    stripped = text.strip()
    lines = stripped.split("\n")
    if len(lines) < 2:
        return text
    if not lines[0].startswith("```"):
        return text
    if lines[-1].strip() != "```":
        return text
    inner = lines[1:-1]
    fence_count = sum(1 for line in inner if line.startswith("```"))
    if fence_count % 2 != 0:
        return text
    return "\n".join(inner)


def _hash_spec_content(content: str) -> str:
    """SHA-256 of normalized spec content (rstripped lines, LF line endings)."""
    normalized = "\n".join(line.rstrip() for line in content.splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _requirement_names(content: str) -> set[str]:
    """Extract all ``### Requirement: <name>`` headings from a spec body."""
    return {m.group(1).strip() for m in _REQUIREMENT_HEADING_RE.finditer(content)}


class DiffType(Enum):
    """Type of difference between spec and code."""

    GAP = "gap"
    EXTENSION = "extension"
    CONFLICT = "conflict"


@dataclass
class SpecDiff:
    """A single difference found between a spec and project code."""

    diff_type: DiffType
    spec_name: str
    description: str
    code_location: str = ""
    confidence: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "diff_type": self.diff_type.value,
            "spec_name": self.spec_name,
            "description": self.description,
            "code_location": self.code_location,
        }
        if self.confidence:
            d["confidence"] = self.confidence
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SpecDiff:
        return cls(
            diff_type=DiffType(data["diff_type"]),
            spec_name=data["spec_name"],
            description=data["description"],
            code_location=data.get("code_location", ""),
            confidence=data.get("confidence", ""),
        )


@dataclass
class SpecAnalysis:
    """Analysis result for a single spec file."""

    spec_name: str
    diffs: List[SpecDiff] = field(default_factory=list)
    analyzed_at: datetime = field(default_factory=datetime.now)

    @property
    def gaps(self) -> List[SpecDiff]:
        return [d for d in self.diffs if d.diff_type == DiffType.GAP]

    @property
    def extensions(self) -> List[SpecDiff]:
        return [d for d in self.diffs if d.diff_type == DiffType.EXTENSION]

    @property
    def conflicts(self) -> List[SpecDiff]:
        return [d for d in self.diffs if d.diff_type == DiffType.CONFLICT]

    @property
    def is_in_sync(self) -> bool:
        return len(self.diffs) == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spec_name": self.spec_name,
            "diffs": [d.to_dict() for d in self.diffs],
            "analyzed_at": self.analyzed_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SpecAnalysis:
        analyzed_at = data.get("analyzed_at")
        if isinstance(analyzed_at, str):
            analyzed_at = datetime.fromisoformat(analyzed_at)
        elif not isinstance(analyzed_at, datetime):
            analyzed_at = datetime.now()

        return cls(
            spec_name=data["spec_name"],
            diffs=[SpecDiff.from_dict(d) for d in data.get("diffs", [])],
            analyzed_at=analyzed_at,
        )


@dataclass
class RoundResult:
    """Result of a single sync round (one stateless pass over all specs)."""

    round_index: int
    analyses: List[SpecAnalysis] = field(default_factory=list)
    # Spec name -> list of human-readable change descriptions (e.g. "removed requirement X").
    changes_by_spec: Dict[str, List[str]] = field(default_factory=dict)
    specs_updated: int = 0
    specs_created: List[str] = field(default_factory=list)
    # Spec name -> SHA-256 of normalized spec content after this round.
    spec_hashes_after: Dict[str, str] = field(default_factory=dict)
    duration_seconds: float = 0.0
    # Each entry: {"spec_name": str, "kind": "deletion", "decision": "approve"|"skip"|"auto",
    #              "description": str, "requirement_names": list[str]}
    high_impact_deletions: List[Dict[str, Any]] = field(default_factory=list)
    discovery_failed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "round_index": self.round_index,
            "analyses": [a.to_dict() for a in self.analyses],
            "changes_by_spec": dict(self.changes_by_spec),
            "specs_updated": self.specs_updated,
            "specs_created": list(self.specs_created),
            "spec_hashes_after": dict(self.spec_hashes_after),
            "duration_seconds": self.duration_seconds,
            "high_impact_deletions": list(self.high_impact_deletions),
            "discovery_failed": self.discovery_failed,
        }


@dataclass
class LoopResult:
    """Aggregate result of a multi-round sync loop."""

    rounds: List[RoundResult] = field(default_factory=list)
    converged: bool = False
    oscillation_detected: bool = False
    oscillation_report: Optional[str] = None
    total_specs_updated: int = 0
    total_specs_created: List[str] = field(default_factory=list)
    final_round_index: int = 0
    discovery_failed: bool = False
    completed_at: datetime = field(default_factory=datetime.now)

    # --- Compatibility helpers ----------------------------------------
    # These properties expose a flattened view of the final round so legacy
    # callers (and the existing CLI render path) keep working until the
    # render layer is updated in a later group.

    @property
    def analyses(self) -> List[SpecAnalysis]:
        if not self.rounds:
            return []
        return self.rounds[-1].analyses

    @property
    def specs_updated(self) -> int:
        return self.total_specs_updated

    @property
    def specs_created(self) -> List[str]:
        return list(self.total_specs_created)

    @property
    def detailed_changes(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for r in self.rounds:
            for spec_name, changes in r.changes_by_spec.items():
                for desc in changes:
                    out.append(
                        {
                            "spec_name": spec_name,
                            "action": "spec_drift_resolved",
                            "description": desc,
                            "round": r.round_index,
                        }
                    )
        return out

    @property
    def all_in_sync(self) -> bool:
        return all(a.is_in_sync for a in self.analyses) if self.analyses else True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rounds": [r.to_dict() for r in self.rounds],
            "converged": self.converged,
            "oscillation_detected": self.oscillation_detected,
            "oscillation_report": self.oscillation_report,
            "total_specs_updated": self.total_specs_updated,
            "total_specs_created": list(self.total_specs_created),
            "final_round_index": self.final_round_index,
            "discovery_failed": self.discovery_failed,
            "completed_at": self.completed_at.isoformat(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


# ``SyncResult`` is retained as an alias so external imports keep working
# while the rest of the codebase migrates to ``LoopResult`` explicitly.
SyncResult = LoopResult


# -- Prompt templates -----------------------------------------------------

_GAP_SPEC_UPDATE_PROMPT = """\
You are an expert software engineer updating a specification document.

## Task

A gap was found between the spec and project code: the spec describes a
requirement that is NOT implemented in the code. Because the spec is a
documented snapshot of the code (not a forward-looking promise), the spec
must be updated to remove the outdated requirement.

## Gap

{description}
Code location: {code_location}

## Spec: {spec_name}

### Current Spec Content

{spec_content}

## Instructions

Precisely locate and remove the outdated requirement described in the gap.
Keep ALL other existing requirements intact — only remove the specific
outdated content. Return the complete updated spec content (the full
markdown document). Do NOT remove any requirements that are still valid.
"""

_SPEC_UPDATE_PROMPT_TEMPLATE = """\
You are an expert software engineer updating a specification document.

## Task

The project code contains functionality that is NOT described in the current
spec. Update the spec to reflect the code's actual behavior. Preserve the
existing spec structure and formatting. Only ADD content — do not remove or
weaken existing requirements.

## Spec: {spec_name}

### Current Spec Content

{spec_content}

## Extensions Found

{extensions_description}

## Instructions

Return the complete updated spec content (the full markdown document).
Keep all existing requirements intact. Add new sections or requirements
as needed to cover the extensions found.
"""

_CONFLICT_SPEC_UPDATE_PROMPT = """\
You are an expert software engineer updating a specification document.

## Task

A conflict was found between the spec and project code. Because the spec is
a documented snapshot of the code (not a forward-looking promise), update
the spec so it matches the code's actual behavior.

## Conflict

{description}
Code location: {code_location}

## Spec: {spec_name}

### Current Spec Content

{spec_content}

## Instructions

Return the complete updated spec content (the full markdown document).
Modify only the parts that conflict with the code's behavior.
Keep all other existing requirements intact.
"""


class SyncEngine:
    """Single-round, one-directional spec update engine.

    Each ``run_once`` call performs an independent, stateless pass:

    1. Optionally discover newly-uncovered subsystems and generate base specs.
    2. Analyze every spec against the code via ``SyncAnalyzer``.
    3. For every drift item (gap, extension, conflict), update the spec to
       reflect the code via the appropriate prompt.
    4. Optionally route ``high-impact deletions`` (whole-requirement removal)
       through ``SyncInteractionHandler`` when ``interactive=True``.
    5. Hash every spec and return a ``RoundResult``.

    All cross-round state (convergence, oscillation, aggregation) lives in
    ``SyncLoop`` — this class never reads or writes such state itself.
    """

    def __init__(
        self,
        project_root: Path,
        interactive: bool = False,
    ) -> None:
        self.project_root = Path(project_root)
        self.interactive = interactive
        self._specs: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Spec loading
    # ------------------------------------------------------------------

    def _load_specs(self) -> Dict[str, Any]:
        """Load all specs via SpecIndex, base spec first."""
        from .spec_index import SpecIndex

        index = SpecIndex(self.project_root).build()
        specs: Dict[str, Any] = {}

        if "base" in index.specs:
            info = index.specs["base"]
            try:
                content = info.path.read_text(encoding="utf-8")
                specs["base"] = {"name": "base", "path": info.path, "content": content}
            except OSError as e:
                logger.warning("Failed to read base spec: %s", e)

        for name, info in index.specs.items():
            if name == "base":
                continue
            try:
                content = info.path.read_text(encoding="utf-8")
                specs[name] = {"name": name, "path": info.path, "content": content}
            except OSError as e:
                logger.warning("Failed to read spec '%s': %s", name, e)

        self._specs = specs
        return specs

    # ------------------------------------------------------------------
    # Single-round entry point
    # ------------------------------------------------------------------

    def run_once(
        self,
        round_index: int,
        flow_ctx: Any,
        llm_caller: Any,
        project_context: str,
        specs: Optional[Dict[str, Any]] = None,
        do_discovery: bool = False,
        progress_callback: Optional[Callable[..., None]] = None,
    ) -> RoundResult:
        """Execute one stateless sync pass.

        Args:
            round_index: 1-based round index, used in step_id namespacing.
            flow_ctx: ``SyncFlowContext`` for step_id generation.
            llm_caller: Pre-constructed ``LLMCaller`` instance.
            project_context: Pre-collected project context string.
            specs: Optional pre-loaded spec dict; if None, ``_load_specs`` is called.
            do_discovery: If True, run ``SpecDiscovery`` once at the start of
                this round (typically only for round 1 of a loop).
            progress_callback: Optional ``(phase, spec_name, index, total, analysis)``
                callable. ``phase`` is one of ``analyzing``/``analyzed``/``discovering``.

        Returns:
            ``RoundResult`` capturing every change applied in this round.
        """
        from .sync_analyzer import SyncAnalyzer

        start = time.time()
        result = RoundResult(round_index=round_index)
        analyzer = SyncAnalyzer(self.project_root, llm_caller)

        if specs is None:
            specs = self._load_specs()
        else:
            self._specs = specs

        if not specs:
            logger.info("No specs found, generating base spec")
            try:
                analyzer.generate_base_spec(project_context)
            except Exception as e:
                logger.error("Base spec generation failed: %s", e)
                result.discovery_failed = True
                result.duration_seconds = time.time() - start
                return result
            specs = self._load_specs()

        if do_discovery:
            self._run_discovery(
                specs=specs,
                llm_caller=llm_caller,
                flow_ctx=flow_ctx,
                round_index=round_index,
                result=result,
                progress_callback=progress_callback,
            )

        pending_high_impact: List[Dict[str, Any]] = []

        spec_items = list(specs.items())
        total = len(spec_items)

        for i, (spec_name, spec_info) in enumerate(spec_items):
            if progress_callback:
                progress_callback("analyzing", spec_name, i, total, None)

            llm_caller.step_id = flow_ctx.make_round_step_id(
                round_index, "analyze", spec_name
            )
            llm_caller.step_type = "sync_analyze"

            analysis = analyzer.analyze_spec(
                spec_name, spec_info["content"], project_context
            )
            result.analyses.append(analysis)

            if progress_callback:
                progress_callback("analyzed", spec_name, i, total, analysis)

            if analysis.is_in_sync:
                continue

            llm_caller.step_type = "sync_resolve"
            for diff in analysis.diffs:
                llm_caller.step_id = flow_ctx.make_round_step_id(
                    round_index,
                    "resolve",
                    f"{spec_name}_{diff.diff_type.value}_{uuid.uuid4().hex[:6]}",
                )

                # Decide whether this drift is "high-impact" (full requirement removal).
                high_impact = self._is_high_impact_deletion(diff)

                if high_impact and self.interactive:
                    pending_high_impact.append(
                        self._build_high_impact_entry(diff, spec_info)
                    )
                    continue

                applied, label = self._apply_spec_drift_update(diff, llm_caller)
                if applied:
                    result.specs_updated += 1
                    result.changes_by_spec.setdefault(spec_name, []).append(label)
                    if high_impact:
                        result.high_impact_deletions.append(
                            {
                                "spec_name": spec_name,
                                "kind": "deletion",
                                "decision": "auto",
                                "description": diff.description,
                            }
                        )

        if pending_high_impact:
            self._handle_pending_high_impact(
                pending_items=pending_high_impact,
                llm_caller=llm_caller,
                result=result,
            )

        for name, info in self._specs.items():
            try:
                content = Path(info["path"]).read_text(encoding="utf-8")
            except OSError:
                content = info.get("content", "")
            result.spec_hashes_after[name] = _hash_spec_content(content)

        result.duration_seconds = time.time() - start
        return result

    # ------------------------------------------------------------------
    # Discovery (only run when caller requests; typically round 1)
    # ------------------------------------------------------------------

    def _run_discovery(
        self,
        specs: Dict[str, Any],
        llm_caller: Any,
        flow_ctx: Any,
        round_index: int,
        result: RoundResult,
        progress_callback: Optional[Callable[..., None]],
    ) -> None:
        from .sync_discovery import SpecDiscovery

        if progress_callback:
            progress_callback("discovering", None, 0, 0, None)

        try:
            llm_caller.step_id = flow_ctx.make_round_step_id(
                round_index, "scan", None
            )
            llm_caller.step_type = "sync_scan"
            discovery = SpecDiscovery(self.project_root, llm_caller)
            discovered = discovery.discover_missing_specs(specs)

            for subsystem in discovered:
                spec_path = discovery.generate_spec_for_subsystem(subsystem)
                if spec_path:
                    name = subsystem["name"]
                    result.specs_created.append(name)
                    try:
                        content = spec_path.read_text(encoding="utf-8")
                        specs[name] = {
                            "name": name,
                            "path": spec_path,
                            "content": content,
                        }
                        self._specs[name] = specs[name]
                    except OSError as e:
                        logger.warning(
                            "Failed to read newly created spec '%s': %s", name, e
                        )
        except Exception as e:
            logger.error("Spec discovery failed, continuing: %s", e)
            result.discovery_failed = True

    # ------------------------------------------------------------------
    # Drift update — single unified action
    # ------------------------------------------------------------------

    def _apply_spec_drift_update(
        self, diff: SpecDiff, llm_caller: Any
    ) -> Tuple[bool, str]:
        """Apply a single drift item by rewriting the affected spec.

        Returns ``(success, change_label)``. The label is a short
        human-readable string (e.g. ``"removed: <description>"``) suitable for
        inclusion in ``RoundResult.changes_by_spec``.
        """
        spec_info = self._specs.get(diff.spec_name)
        if not spec_info:
            logger.warning(
                "Spec '%s' not found while applying %s update",
                diff.spec_name, diff.diff_type.value,
            )
            return False, ""

        if diff.diff_type == DiffType.GAP:
            prompt = _GAP_SPEC_UPDATE_PROMPT.format(
                spec_name=diff.spec_name,
                description=diff.description,
                code_location=diff.code_location or "(not specified)",
                spec_content=spec_info.get("content", ""),
            )
            label_prefix = "removed"
            llm_label = "gap"
        elif diff.diff_type == DiffType.EXTENSION:
            ext_desc = f"- {diff.description}"
            if diff.code_location:
                ext_desc += f" (at {diff.code_location})"
            prompt = _SPEC_UPDATE_PROMPT_TEMPLATE.format(
                spec_name=diff.spec_name,
                spec_content=spec_info.get("content", ""),
                extensions_description=ext_desc,
            )
            label_prefix = "added"
            llm_label = "extension"
        else:  # CONFLICT
            prompt = _CONFLICT_SPEC_UPDATE_PROMPT.format(
                spec_name=diff.spec_name,
                description=diff.description,
                code_location=diff.code_location or "(not specified)",
                spec_content=spec_info.get("content", ""),
            )
            label_prefix = "modified"
            llm_label = "conflict"

        if self._update_spec_via_llm(diff.spec_name, prompt, llm_caller, llm_label):
            return True, f"{label_prefix}: {diff.description}"
        return False, ""

    def _update_spec_via_llm(
        self, spec_name: str, prompt: str, llm_caller: Any, label: str
    ) -> bool:
        """Shared helper: call LLM to rewrite a spec file with safety guards."""
        spec_info = self._specs.get(spec_name)
        if not spec_info:
            logger.warning("Spec '%s' not found for %s update", spec_name, label)
            return False

        try:
            updated_content = llm_caller.call(prompt=prompt, json_mode="off")
            updated_content = updated_content.strip()
            updated_content = strip_markdown_fences(updated_content)

            if not updated_content:
                logger.warning(
                    "LLM returned empty content for %s spec update '%s'",
                    label, spec_name,
                )
                return False

            if len(updated_content) < len(spec_info["content"]) * 0.5:
                logger.warning(
                    "LLM returned suspiciously short content for %s spec update '%s' "
                    "(%d chars vs original %d chars), skipping update",
                    label, spec_name,
                    len(updated_content), len(spec_info["content"]),
                )
                return False

            Path(spec_info["path"]).write_text(updated_content, encoding="utf-8")
            spec_info["content"] = updated_content
            logger.info("Updated spec '%s' for %s resolution", spec_name, label)
            return True
        except Exception as e:
            logger.error("Failed to update spec '%s' for %s: %s", spec_name, label, e)
            return False

    # ------------------------------------------------------------------
    # High-impact deletion handling
    # ------------------------------------------------------------------

    def _is_high_impact_deletion(self, diff: SpecDiff) -> bool:
        """Heuristic: a GAP whose description references a whole Requirement.

        The check is intentionally conservative — only gap-type drift
        qualifies, and only when the gap description mentions
        ``Requirement: <name>`` or quotes a heading-style phrase that maps to
        an existing top-level requirement of the spec. Other drift types are
        not classified as high-impact (extensions never delete, conflicts
        rewrite scoped sections).
        """
        if diff.diff_type != DiffType.GAP:
            return False
        spec_info = self._specs.get(diff.spec_name)
        if not spec_info:
            return False
        existing_reqs = _requirement_names(spec_info.get("content", ""))
        if not existing_reqs:
            return False
        desc = diff.description or ""
        desc_lower = desc.lower()
        for name in existing_reqs:
            if name.lower() and name.lower() in desc_lower:
                return True
        return False

    def _build_high_impact_entry(
        self, diff: SpecDiff, spec_info: Dict[str, Any]
    ) -> Dict[str, Any]:
        existing_reqs = _requirement_names(spec_info.get("content", ""))
        matching = [
            name for name in existing_reqs
            if name.lower() and name.lower() in (diff.description or "").lower()
        ]
        excerpt = (diff.description or "")[:500]
        return {
            "diff": diff,
            "item_id": f"del_{diff.spec_name}_{uuid.uuid4().hex[:8]}",
            "spec_name": diff.spec_name,
            "requirement_name": matching[0] if matching else "",
            "requirement_excerpt": excerpt,
        }

    def _handle_pending_high_impact(
        self,
        pending_items: List[Dict[str, Any]],
        llm_caller: Any,
        result: RoundResult,
    ) -> None:
        """Route pending high-impact deletions through SyncInteractionHandler."""
        from .sync_interaction import HighImpactDeletion, SyncInteractionHandler

        handler_items = [
            HighImpactDeletion(
                item_id=p["item_id"],
                spec_name=p["spec_name"],
                requirement_name=p["requirement_name"],
                requirement_excerpt=p["requirement_excerpt"],
            )
            for p in pending_items
        ]
        handler = SyncInteractionHandler(self.project_root, handler_items)

        try:
            decisions = handler.collect_decisions()
        except KeyboardInterrupt:
            logger.info("High-impact deletion approval interrupted")
            for p in pending_items:
                result.high_impact_deletions.append(
                    {
                        "spec_name": p["spec_name"],
                        "kind": "deletion",
                        "decision": "skip",
                        "description": p["diff"].description,
                        "requirement_name": p["requirement_name"],
                    }
                )
            return

        item_map = {p["item_id"]: p for p in pending_items}
        for item_id, decision in decisions.items():
            entry = item_map.get(item_id)
            if entry is None:
                continue
            if decision == "approve":
                applied, label = self._apply_spec_drift_update(entry["diff"], llm_caller)
                if applied:
                    result.specs_updated += 1
                    result.changes_by_spec.setdefault(entry["spec_name"], []).append(label)
                result.high_impact_deletions.append(
                    {
                        "spec_name": entry["spec_name"],
                        "kind": "deletion",
                        "decision": "approve",
                        "description": entry["diff"].description,
                        "requirement_name": entry["requirement_name"],
                    }
                )
            else:
                result.high_impact_deletions.append(
                    {
                        "spec_name": entry["spec_name"],
                        "kind": "deletion",
                        "decision": "skip",
                        "description": entry["diff"].description,
                        "requirement_name": entry["requirement_name"],
                    }
                )

    # ------------------------------------------------------------------
    # Call-response processing (sync-respond CLI)
    # ------------------------------------------------------------------

    def process_call_response(
        self, call_file_path: Path, llm_caller: Any = None
    ) -> Dict[str, Any]:
        """Process a ``sync_high_impact_deletion`` response file.

        Args:
            call_file_path: Path to the original call file (the response file
                is at ``{call_file_path}.response``).
            llm_caller: Optional pre-built LLMCaller. Constructed if None.

        Returns:
            Dict with ``specs_updated`` and ``skipped`` counts.

        Raises:
            ValueError: if the call file uses a legacy/unsupported format.
        """
        response_path = Path(str(call_file_path) + ".response")
        if not response_path.exists():
            logger.warning("Response file not found: %s", response_path)
            return {"specs_updated": 0, "skipped": 0}

        try:
            response_data = json.loads(response_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to read response file '%s': %s", response_path, e)
            return {"specs_updated": 0, "skipped": 0}

        try:
            call_data = json.loads(Path(call_file_path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to read call file '%s': %s", call_file_path, e)
            return {"specs_updated": 0, "skipped": 0}

        call_type = call_data.get("type")
        if call_type != "sync_high_impact_deletion":
            raise ValueError(
                "Unsupported sync call file type "
                f"'{call_type}'. The single-directional sync only produces "
                "'sync_high_impact_deletion' call files. Legacy formats "
                "(sync_pending_decisions, conflict-only) are no longer "
                "supported — re-run 'se3 sync --interactive' to generate "
                "a fresh call file."
            )

        if not self._specs:
            self._load_specs()

        if llm_caller is None:
            from .llm_caller import LLMCaller
            from .sync_history import SyncFlowContext
            flow_ctx = SyncFlowContext(self.project_root)
            llm_caller = LLMCaller(
                project_root=self.project_root,
                flow_id=flow_ctx.flow_id,
                step_id="sync_respond",
                step_type="sync_respond",
            )

        call_items_by_id = {
            item.get("item_id", ""): item
            for item in call_data.get("items", [])
            if item.get("item_id")
        }

        specs_updated = 0
        skipped = 0

        for resp in response_data.get("items", []):
            decision = (resp.get("decision") or "").lower()
            if decision not in ("approve", "skip"):
                continue

            item_id = resp.get("item_id", "")
            original = call_items_by_id.get(item_id)
            if not original:
                continue

            if decision == "skip":
                skipped += 1
                continue

            spec_name = original.get("spec_name", "")
            diff = SpecDiff(
                diff_type=DiffType.GAP,
                spec_name=spec_name,
                description=original.get(
                    "excerpt", original.get("requirement_excerpt", "")
                ),
                code_location="",
            )
            applied, _label = self._apply_spec_drift_update(diff, llm_caller)
            if applied:
                specs_updated += 1
            else:
                skipped += 1

        return {"specs_updated": specs_updated, "skipped": skipped}
