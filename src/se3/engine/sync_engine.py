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
import os
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


def _governance_prompt_injection(spec_name: str) -> str:
    """Spec writing-discipline / split-criteria text appended to update prompts.

    Every spec-update prompt the sync engine builds carries the per-Requirement
    / per-spec writing discipline (a)-(d) and the cohesion-first split criteria,
    so that any spec body the sub-agent (re)writes already follows the rules that
    make the program-derived index views navigable. When the spec being updated
    is ``base``, the base admission standard is prepended too, so module-level
    detail is routed into the corresponding module spec rather than appended to
    ``base``. ``update_spec`` (not sync) must never create a parallel spec on its
    own; the split-criteria text states that responsibility split explicitly.
    """
    from .spec_governance import (
        BASE_ADMISSION_STANDARD,
        SPLIT_CRITERIA,
        WRITING_DISCIPLINE,
    )

    sections = []
    if spec_name == "base":
        sections.append(BASE_ADMISSION_STANDARD)
    sections.append(WRITING_DISCIPLINE)
    sections.append(SPLIT_CRITERIA)
    return "\n\n---\n\n" + "\n\n".join(sections) + "\n"


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
    failed_analysis_reason: Optional[str] = None
    touched_files: List[str] = field(default_factory=list)
    code_fully_absent: bool = False

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

    @property
    def analysis_failed(self) -> bool:
        return self.failed_analysis_reason is not None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "spec_name": self.spec_name,
            "diffs": [d.to_dict() for d in self.diffs],
            "analyzed_at": self.analyzed_at.isoformat(),
        }
        if self.failed_analysis_reason is not None:
            d["failed_analysis_reason"] = self.failed_analysis_reason
        if self.touched_files:
            d["touched_files"] = list(self.touched_files)
        if self.code_fully_absent:
            d["code_fully_absent"] = self.code_fully_absent
        return d

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
            failed_analysis_reason=data.get("failed_analysis_reason"),
            touched_files=list(data.get("touched_files") or []),
            code_fully_absent=bool(data.get("code_fully_absent", False)),
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
    new_subsystems_count: int = 0
    # Per-spec dependency files touched during this round (spec_name -> sorted list of relative paths).
    per_spec_deps: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def is_stable(self) -> bool:
        """Round is stable when nothing was changed AND no drift remains.

        A round is considered stable (i.e. eligible to count toward
        convergence) only when both:
          * ``specs_updated == 0`` — no spec content was written in this
            round, and
          * every analysis is in sync — the analyzer detected no drift.

        The second clause is essential: when the LLM proposes a fix that
        is rejected by a safety guard (e.g. the 50%-length floor) or that
        raises during application, ``specs_updated`` stays at 0 even
        though real drift was detected. Convergence in that case would be
        a lie — the next ``se3 sync`` invocation will rediscover the same
        drift.

        An analysis whose ``failed_analysis_reason`` is set (LLM output
        format error or infrastructure failure) does NOT block stability:
        it produced no diffs to act on this round and may recover on a
        future run, but it must not pin the loop to "still drifting" and
        burn rounds re-asking the same question. Such failures are
        reported separately as a partial-success line. When ``analyses``
        is empty (scripted RoundResults in tests, or pre-engine fixture
        data), ``all([]) is True`` falls back to the legacy
        ``specs_updated == 0`` semantics.
        """
        if self.specs_updated != 0:
            return False
        return all(a.is_in_sync or a.analysis_failed for a in self.analyses)

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
            "new_subsystems_count": self.new_subsystems_count,
            "per_spec_deps": {
                k: list(v) for k, v in self.per_spec_deps.items()
            },
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
    paused: bool = False
    checkpoint_path: Optional[str] = None
    completed_at: datetime = field(default_factory=datetime.now)
    # Spec names that produced llm_output_format_error during the run.
    # Accumulated across rounds; persisted so the final report can surface
    # them even though they are excluded from later rounds.
    format_error_specs: set = field(default_factory=set)
    obsolete_specs: List[str] = field(default_factory=list)
    obsolete_specs_deleted: List[str] = field(default_factory=list)
    obsolete_specs_kept: List[str] = field(default_factory=list)
    # Incremental-skip telemetry (G7 reporting).
    # Level 1: global shutter hit — code fingerprint unchanged, 0 LLM calls.
    level_1_cache_hit: bool = False
    # Level 2: per-spec gate cache hits skipped for the whole sync.
    level_2_skipped_specs: List[str] = field(default_factory=list)
    # Level 3: specs that reached per-spec convergence and exited early.
    level_3_early_exit_specs: List[str] = field(default_factory=list)
    # Spec volume-governance outcome (post-convergence): respond-channel call
    # files written for an over-limit base migration / multi-topic spec split,
    # and the specs still missing a ``<!-- domain: -->`` marker.
    governance: Dict[str, Any] = field(default_factory=dict)

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
            "paused": self.paused,
            "checkpoint_path": self.checkpoint_path,
            "completed_at": self.completed_at.isoformat(),
            "obsolete_specs": list(self.obsolete_specs),
            "obsolete_specs_deleted": list(self.obsolete_specs_deleted),
            "obsolete_specs_kept": list(self.obsolete_specs_kept),
            "level_1_cache_hit": self.level_1_cache_hit,
            "level_2_skipped_specs": list(self.level_2_skipped_specs),
            "level_3_early_exit_specs": list(self.level_3_early_exit_specs),
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
outdated content. Do NOT remove any requirements that are still valid.

You can update the spec in TWO ways (choose whichever fits best):

Way A (preferred for small/targeted changes):
  Use the Edit tool to modify se3/specs/{spec_name}/spec.md directly.
  Your reply can just describe what you changed.

Way B (for sweeping rewrites or new sections):
  Do NOT use Edit. Instead, output the COMPLETE new content of spec.md
  as a single markdown code block. The framework will write it for you.

In either case, the final spec.md MUST:
- Start with <!-- spec-format: v1 -->
- Followed by # {spec_name} Specification
- Contain ## Purpose
- Contain at least one ### Requirement: section
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

Keep all existing requirements intact. Add new sections or requirements
as needed to cover the extensions found.

You can update the spec in TWO ways (choose whichever fits best):

Way A (preferred for small/targeted changes):
  Use the Edit tool to modify se3/specs/{spec_name}/spec.md directly.
  Your reply can just describe what you changed.

Way B (for sweeping rewrites or new sections):
  Do NOT use Edit. Instead, output the COMPLETE new content of spec.md
  as a single markdown code block. The framework will write it for you.

In either case, the final spec.md MUST:
- Start with <!-- spec-format: v1 -->
- Followed by # {spec_name} Specification
- Contain ## Purpose
- Contain at least one ### Requirement: section
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

Modify only the parts that conflict with the code's behavior.
Keep all other existing requirements intact.

You can update the spec in TWO ways (choose whichever fits best):

Way A (preferred for small/targeted changes):
  Use the Edit tool to modify se3/specs/{spec_name}/spec.md directly.
  Your reply can just describe what you changed.

Way B (for sweeping rewrites or new sections):
  Do NOT use Edit. Instead, output the COMPLETE new content of spec.md
  as a single markdown code block. The framework will write it for you.

In either case, the final spec.md MUST:
- Start with <!-- spec-format: v1 -->
- Followed by # {spec_name} Specification
- Contain ## Purpose
- Contain at least one ### Requirement: section
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
        skip_specs: Optional[set[str]] = None,
        spec_deps: Optional[Dict[str, List[str]]] = None,
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
            skip_specs: Optional set of spec names to treat as already in-sync.
                Used on resume to avoid re-analyzing specs that the previous
                run already confirmed unchanged. Each skipped spec is recorded
                with an empty-diffs ``SpecAnalysis`` so its hash still flows
                into ``RoundResult.spec_hashes_after`` for oscillation detection.
            spec_deps: Optional dict mapping spec_name -> list of dependency
                file paths (accumulated across rounds). Passed to the analyzer
                so it can detect when all deps are missing and inject the code
                absence confirmation prompt.

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

        skip = set(skip_specs or ())

        for i, (spec_name, spec_info) in enumerate(spec_items):
            if progress_callback:
                progress_callback("analyzing", spec_name, i, total, None)

            if spec_name in skip:
                # Resume optimization: the previous run already confirmed
                # this spec was in-sync and its sha256 has not changed on
                # disk. Skip the LLM round-trip but still record an
                # in-sync analysis so the rest of the pipeline behaves
                # identically to a normal "no drift" pass.
                analysis = SpecAnalysis(spec_name=spec_name, diffs=[])
                result.analyses.append(analysis)
                if progress_callback:
                    progress_callback("analyzed", spec_name, i, total, analysis)
                continue

            llm_caller.step_id = flow_ctx.make_round_step_id(
                round_index, "analyze", spec_name
            )
            llm_caller.step_type = "sync_analyze"

            spec_dep_list = None
            if spec_deps:
                spec_dep_list = spec_deps.get(spec_name)

            analysis = analyzer.analyze_spec(
                spec_name, spec_info["content"], project_context,
                deps=spec_dep_list,
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

        # Accumulate per-spec deps from all analyses in this round.
        for analysis in result.analyses:
            if getattr(analysis, "touched_files", None):
                result.per_spec_deps[analysis.spec_name] = sorted(
                    set(analysis.touched_files)
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
            result.new_subsystems_count = len(discovered)

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

        # Inject the spec-language instruction (Way A edits + Way B rewrites
        # both produce spec body text). No-op when spec_language is unset.
        from .context_builder import get_spec_language_instruction
        prompt += get_spec_language_instruction(self.project_root)

        # Inject the spec writing discipline + split criteria (and the base
        # admission standard when editing base) so the rewritten body follows
        # the volume-governance rules.
        prompt += _governance_prompt_injection(diff.spec_name)

        if self._update_spec_via_llm(diff.spec_name, prompt, llm_caller, llm_label):
            return True, f"{label_prefix}: {diff.description}"
        return False, ""

    @staticmethod
    def _snapshot_spec_disk(path: Path) -> Tuple[Optional[float], Optional[str]]:
        """Return (mtime, sha256) of the file at ``path``.

        Returns ``(None, None)`` when the file does not exist or cannot be
        read. The sha256 is computed over the raw file bytes so it can be
        used to detect any byte-level modification by a sub-agent.
        """
        try:
            stat = path.stat()
        except (OSError, FileNotFoundError):
            return None, None
        try:
            data = path.read_bytes()
        except OSError:
            return stat.st_mtime, None
        return stat.st_mtime, hashlib.sha256(data).hexdigest()

    @staticmethod
    def _stdout_contains_spec_body(text: str) -> bool:
        """Heuristic: does ``text`` (after fence stripping) carry a full spec body?

        We accept a stdout payload as a Way-B rewrite when the cleaned text
        contains either the v1 marker line or a ``# <name> Specification``
        heading. The check is intentionally loose — final structural
        validity is enforced by ``validate_spec_structure`` downstream.
        """
        if not text:
            return False
        stripped = strip_markdown_fences(text.strip()).strip()
        if not stripped:
            return False
        if "<!-- spec-format: v1 -->" in stripped:
            return True
        for line in stripped.splitlines():
            line_stripped = line.strip()
            if line_stripped.startswith("# ") and line_stripped.lower().rstrip().endswith(
                "specification"
            ):
                return True
        return False

    def _git_checkout_rollback(self, spec_path: Path) -> bool:
        """Run ``git checkout HEAD -- <spec_path>`` to revert the file.

        Returns True on success, False on any failure. Failures are logged
        but never raised — callers decide how to react.
        """
        import subprocess

        try:
            rel = spec_path.relative_to(self.project_root)
        except ValueError:
            rel = spec_path
        try:
            result = subprocess.run(
                ["git", "checkout", "HEAD", "--", str(rel)],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, FileNotFoundError) as exc:
            logger.error(
                "git checkout rollback failed for '%s': %s", spec_path, exc
            )
            return False
        if result.returncode != 0:
            logger.error(
                "git checkout rollback for '%s' returned non-zero (%d): %s",
                spec_path, result.returncode, result.stderr.strip(),
            )
            return False
        return True

    def _update_spec_via_llm(
        self, spec_name: str, prompt: str, llm_caller: Any, label: str
    ) -> bool:
        """Call LLM to update a spec, detecting Way A (Edit) vs Way B (rewrite).

        Flow:
          1. Snapshot the on-disk spec (mtime, sha256) BEFORE the LLM call.
          2. Invoke the LLM. The sub-agent may either edit the file directly
             (Way A) or return the full new spec content as its stdout
             (Way B).
          3. Snapshot the on-disk spec AFTER the call.
          4. If the disk changed → Way A: read disk, validate. On failure
             roll back via ``git checkout HEAD -- <path>``.
          5. If the disk did not change but stdout looks like a complete
             spec body → Way B: write stdout to disk, validate. On failure
             restore the original content and report.
          6. If neither — log an error and return False.
        """
        from .spec_validator import extract_spec_body, validate_spec_structure

        spec_info = self._specs.get(spec_name)
        if not spec_info:
            logger.warning("Spec '%s' not found for %s update", spec_name, label)
            return False

        spec_path = Path(spec_info["path"])
        original_content = spec_info.get("content", "")
        try:
            pre_disk_content = spec_path.read_text(encoding="utf-8")
        except OSError:
            pre_disk_content = original_content
        _, pre_sha = self._snapshot_spec_disk(spec_path)

        try:
            raw_stdout = llm_caller.call(prompt=prompt, json_mode="off")
        except Exception as exc:
            logger.error(
                "LLM call failed for %s spec update '%s': %s",
                label, spec_name, exc,
            )
            # The sub-agent may have used the Edit tool (Way A) to mutate the
            # spec file before the call errored out (e.g. network drop after
            # a successful file write).  Snapshot disk and validate or roll
            # back so the next round does not treat half-written content as
            # authoritative.
            _, post_sha = self._snapshot_spec_disk(spec_path)
            disk_changed_on_error = (
                pre_sha is not None and post_sha is not None
                and pre_sha != post_sha
            )
            if disk_changed_on_error:
                logger.warning(
                    "LLM call errored but disk changed for spec '%s'; "
                    "checking for unintended Way-A edit.", spec_name,
                )
                try:
                    new_content = spec_path.read_text(encoding="utf-8")
                except OSError:
                    self._git_checkout_rollback(spec_path)
                    spec_info["content"] = pre_disk_content
                    return False

                validation = validate_spec_structure(new_content, spec_name)
                if validation.passed:
                    spec_info["content"] = new_content
                    logger.info(
                        "Accepted Way-A edit for spec '%s' despite LLM call "
                        "error; content passed structural validation.", spec_name,
                    )
                    return True
                else:
                    logger.error(
                        "Way-A edit for spec '%s' during LLM error failed "
                        "validation: %s",
                        spec_name, "; ".join(validation.errors),
                    )
                    self._git_checkout_rollback(spec_path)
                    try:
                        spec_info["content"] = spec_path.read_text(encoding="utf-8")
                    except OSError:
                        spec_info["content"] = pre_disk_content
                    return False
            return False

        _, post_sha = self._snapshot_spec_disk(spec_path)
        disk_changed = (pre_sha is not None and post_sha is not None
                        and pre_sha != post_sha)

        # --- Way A: sub-agent edited the file directly --------------------
        if disk_changed:
            try:
                new_content = spec_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.error(
                    "Failed to re-read spec '%s' after Way-A edit: %s",
                    spec_name, exc,
                )
                self._git_checkout_rollback(spec_path)
                spec_info["content"] = pre_disk_content
                return False

            if len(new_content) < len(original_content) * 0.5:
                logger.warning(
                    "Way-A edit for %s spec '%s' is much shorter than "
                    "original (%d vs %d chars); accepting but flagging.",
                    label, spec_name, len(new_content), len(original_content),
                )

            validation = validate_spec_structure(new_content, spec_name)
            if not validation.passed:
                logger.error(
                    "Way-A edit for %s spec '%s' failed structural validation: %s",
                    label, spec_name, "; ".join(validation.errors),
                )
                if self._git_checkout_rollback(spec_path):
                    try:
                        restored = spec_path.read_text(encoding="utf-8")
                    except OSError:
                        restored = pre_disk_content
                    spec_info["content"] = restored
                else:
                    # Best-effort: write the original content back to disk
                    # so we are not left with an invalid spec.
                    try:
                        spec_path.write_text(pre_disk_content, encoding="utf-8")
                        spec_info["content"] = pre_disk_content
                    except OSError as exc:
                        logger.error(
                            "Failed to restore spec '%s' after rollback "
                            "failure: %s", spec_name, exc,
                        )
                return False

            spec_info["content"] = new_content
            logger.info(
                "Updated spec '%s' for %s resolution via Way-A edit", spec_name, label
            )
            return True

        # --- Way B: full-rewrite via stdout -------------------------------
        # Purify the stdout the same way sync_discovery does: strip the
        # outer markdown fences, then slice out the spec body from its first
        # structural anchor so any agentic narrative preamble is dropped
        # before validation. Without this an off-mode response that leads
        # with prose would fail validation even though a valid spec body
        # sits at its tail.
        cleaned_stdout = ""
        if isinstance(raw_stdout, str):
            cleaned_stdout = extract_spec_body(
                strip_markdown_fences(raw_stdout.strip()).strip(), spec_name
            ).strip()

        if self._stdout_contains_spec_body(raw_stdout or ""):
            if len(cleaned_stdout) < len(original_content) * 0.5:
                logger.warning(
                    "Way-B rewrite for %s spec '%s' is much shorter than "
                    "original (%d vs %d chars); accepting but flagging.",
                    label, spec_name,
                    len(cleaned_stdout), len(original_content),
                )

            validation = validate_spec_structure(cleaned_stdout, spec_name)
            if not validation.passed:
                logger.error(
                    "Way-B rewrite for %s spec '%s' failed structural "
                    "validation: %s",
                    label, spec_name, "; ".join(validation.errors),
                )
                return False

            try:
                spec_path.write_text(cleaned_stdout, encoding="utf-8")
            except OSError as exc:
                logger.error(
                    "Failed to write Way-B rewrite for spec '%s': %s",
                    spec_name, exc,
                )
                return False
            spec_info["content"] = cleaned_stdout
            logger.info(
                "Updated spec '%s' for %s resolution via Way-B rewrite",
                spec_name, label,
            )
            return True

        # --- Way C: no disk change AND stdout has no spec body ------------
        logger.error(
            "LLM call for %s spec '%s' produced neither a disk edit nor a "
            "complete spec body in stdout; skipping this update.",
            label, spec_name,
        )
        return False

    # ------------------------------------------------------------------
    # High-impact deletion handling
    # ------------------------------------------------------------------

    def _is_high_impact_deletion(self, diff: SpecDiff) -> bool:
        """Heuristic: a GAP whose description indicates a whole-Requirement removal.

        A bare substring match between a Requirement heading and the gap
        description is far too lax — a description like "the project
        identity section's listed language is outdated" mentions an
        existing Requirement name but only proposes a small in-place
        edit, not a deletion of the entire ``### Requirement: ...``
        block. Treating that as high-impact would block every round on
        a needless approval call file.

        The stricter contract this method enforces:

        * Only ``GAP`` drift is ever high-impact (extensions never
          delete; conflicts rewrite scoped sections).
        * The description MUST mention at least one existing Requirement
          name (case-insensitive substring).
        * AND one of the following must hold:

            1. The description contains the heading-style phrase
               ``Requirement: <something>`` (with the colon), which the
               LLM uses when it intends to reference / drop a whole
               Requirement block. OR
            2. The description contains the word ``requirement`` AND a
               clear "whole-absence" indicator
               (``not implemented``, ``lacks``, ``missing``,
               ``removed``, ``no longer``, ``never implemented``,
               ``does not exist``, ``no code``, ...).
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
        if not desc:
            return False
        desc_lower = desc.lower()

        matched_names = [
            name for name in existing_reqs
            if name and name.lower() in desc_lower
        ]
        if not matched_names:
            return False

        if re.search(r"\brequirement\s*:\s*\S", desc_lower):
            return True

        if not re.search(r"\brequirements?\b", desc_lower):
            return False

        absence_pattern = re.compile(
            r"\b("
            r"not\s+implemented|"
            r"not\s+present|"
            r"un[-\s]?implemented|"
            r"lacks?|lacking|"
            r"missing|absent|"
            r"removed?|deleted?|dropped?|"
            r"no\s+longer|"
            r"never\s+implemented|"
            r"does\s+not\s+(exist|implement)|"
            r"doesn['’]?t\s+(exist|implement)|"
            r"no\s+(code|implementation)"
            r")\b"
        )
        return bool(absence_pattern.search(desc_lower))

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
            logger.info("High-impact deletion approval interrupted — aborting sync")
            for p in pending_items:
                result.high_impact_deletions.append(
                    {
                        "spec_name": p["spec_name"],
                        "kind": "deletion",
                        "decision": "interrupted",
                        "description": p["diff"].description,
                        "requirement_name": p["requirement_name"],
                    }
                )
            raise

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
    # Spec volume governance: base migration / parallel split / domain backfill
    #
    # All three are semantic-level refactors performed ONLY by sync. The
    # *decision* (which Requirements, which target / new spec) is made by the
    # LLM and confirmed by the user through the respond channel; the *mechanism*
    # below is deterministic and never invokes an LLM (it only reads + rewrites
    # spec text via the pure helpers in ``sync_governance``).
    # ------------------------------------------------------------------

    def _specs_dir(self) -> Path:
        from .spec_index import SpecIndex

        return SpecIndex._resolve_specs_dir(self.project_root)

    def _all_spec_texts(self) -> Dict[str, Tuple[Path, str]]:
        """Read every ``se3/specs/<name>/spec.md`` into ``{name: (path, text)}``."""
        out: Dict[str, Tuple[Path, str]] = {}
        specs_dir = self._specs_dir()
        if not specs_dir.exists():
            return out
        for sub in sorted(specs_dir.iterdir()):
            if not sub.is_dir():
                continue
            spec_file = sub / "spec.md"
            if not spec_file.exists():
                continue
            try:
                out[sub.name] = (spec_file, spec_file.read_text(encoding="utf-8"))
            except OSError as exc:
                logger.warning("Failed to read spec '%s': %s", sub.name, exc)
        return out

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write *content* to *path* atomically via a temp file + ``os.replace``.

        The new bytes land in a sibling ``*.tmp`` file first and are then swapped
        into place with ``os.replace`` (an atomic rename on POSIX/Windows). The
        destination is therefore **never** left half-written: a failure while
        writing the temp file (disk full, mid-write I/O error) leaves the
        original file untouched, and the stray temp file is removed. This is the
        property ``_write_all_or_restore`` relies on so a write that raises can
        never corrupt the spec it was targeting. Raises ``OSError`` on failure.
        """
        tmp = path.with_name(path.name + ".sync-tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:  # pragma: no cover - defensive
                pass
            raise

    def _write_all_or_restore(
        self,
        edits: List[Tuple[Path, str, str]],
        creates: List[Tuple[Path, str]],
    ) -> Optional[str]:
        """Write *edits* and *creates* atomically; roll back on any failure.

        ``edits`` is a list of ``(path, new_content, original_content)`` for
        existing files, ``creates`` a list of ``(new_file_path, content)`` for
        brand-new files. Every individual write goes through ``_atomic_write``
        (temp file + ``os.replace``), so a write that raises mid-flight never
        leaves its own destination truncated — the file is either the old
        content or the new content, never a partial. On the first ``OSError``
        the edits that *did* commit are restored to their original content (also
        atomically) and every freshly created file (and any directory created
        for it) is removed, so a partial migration / split never leaves source
        requirements deleted while their new home failed to materialise. Returns
        ``None`` on success or the error string on failure.
        """
        written_edits: List[Tuple[Path, str]] = []
        created_files: List[Path] = []
        created_dirs: List[Path] = []
        try:
            for path, new_content, original in edits:
                self._atomic_write(path, new_content)
                written_edits.append((path, original))
            for new_path, content in creates:
                parent = new_path.parent
                if not parent.exists():
                    parent.mkdir(parents=True, exist_ok=True)
                    created_dirs.append(parent)
                self._atomic_write(new_path, content)
                created_files.append(new_path)
            return None
        except OSError as exc:
            logger.error("Atomic spec write failed (%s) — rolling back", exc)
            # The failing write left its own destination intact (atomic replace),
            # so only the edits that already committed need restoring.
            for path, original in written_edits:
                try:
                    self._atomic_write(path, original)
                except OSError as rb_exc:  # pragma: no cover - defensive
                    logger.error(
                        "Rollback could not restore %s: %s", path, rb_exc
                    )
            for f in created_files:
                try:
                    f.unlink()
                except OSError:  # pragma: no cover - defensive
                    pass
            for d in created_dirs:
                try:
                    d.rmdir()
                except OSError:  # pragma: no cover - defensive
                    pass
            return str(exc)

    def _rebuild_index(self) -> None:
        """Full-rebuild the spec index so moved items get their new addresses."""
        try:
            from .spec_index import SpecIndex

            idx = SpecIndex(self.project_root)
            idx.build()
            idx.save()
        except Exception as exc:  # best-effort — index is derived data
            logger.warning("Spec index rebuild after governance op failed: %s", exc)

    def base_exceeds_limit(self) -> bool:
        """True when the on-disk ``base`` spec is over its configured byte limit."""
        from ..config import load_spec_governance_config

        cfg = load_spec_governance_config(self.project_root)
        specs = self._all_spec_texts()
        base = specs.get("base")
        if base is None:
            return False
        return len(base[1].encode("utf-8")) > cfg.base_max_bytes

    def migrate_requirements(
        self, migrations: List[Any]
    ) -> Dict[str, Any]:
        """Relocate the given ``base`` Requirements into their module specs.

        ``migrations`` is a list of ``BaseMigration`` (``requirement_name`` +
        ``target_spec``). Each Requirement block is cut from ``base`` and appended
        to the target module spec; inter-spec ``base::<requirement>`` references
        across every spec are relinked to ``<target>::<requirement>``. Every
        changed spec is validated against the v1 structural contract BEFORE any
        write — the whole migration aborts (writes nothing) if any changed spec
        would become invalid, so the operation is atomic. On success the spec
        index is rebuilt so moved items resolve at their new address.

        Returns ``{"specs_updated": int, "migrated": [...], "skipped": [...]}``.
        """
        from .spec_validator import validate_spec_structure
        from .sync_governance import (
            append_requirements,
            relink_intra_spec_refs,
            requirement_names,
            rewrite_moved_refs,
            split_out_requirements,
        )

        specs = self._all_spec_texts()
        result: Dict[str, Any] = {"specs_updated": 0, "migrated": [], "skipped": []}
        if "base" not in specs:
            logger.warning("migrate_requirements: no base spec on disk")
            result["skipped"] = [getattr(m, "requirement_name", "") for m in migrations]
            return result

        base_path, base_text = specs["base"]
        names = [getattr(m, "requirement_name", "") for m in migrations]
        # First pass: discover which Requirement blocks actually exist in base.
        # This is parse-only; the returned ``remaining_base`` (which would strip
        # *every* requested name) is intentionally discarded — base must only
        # lose the Requirements that are actually relocated below.
        _, blocks = split_out_requirements(base_text, names)

        target_to_blocks: Dict[str, List[str]] = {}
        moves: Dict[Tuple[str, str], Tuple[str, str]] = {}
        valid_names: List[str] = []
        for mig in migrations:
            name = getattr(mig, "requirement_name", "")
            target = getattr(mig, "target_spec", "")
            if name not in blocks or target not in specs or target == "base":
                result["skipped"].append(name)
                continue
            target_to_blocks.setdefault(target, []).append(blocks[name])
            moves[("base", name)] = (target, name)
            valid_names.append(name)
            result["migrated"].append({"requirement_name": name, "target_spec": target})

        if not moves:
            return result

        # Recompute base removing ONLY the validly-migrated Requirements, so a
        # skipped migration's Requirement is left intact in base rather than
        # being silently dropped without a destination.
        remaining_base, _ = split_out_requirements(base_text, valid_names)

        # Post-move location of every Requirement originally in base: a migrated
        # name lands in its target spec, every other name stays in base. This
        # drives intra-spec `Requirement: <name>` relinking so the documented
        # primary reference form survives the move alongside the inter-spec form.
        base_names = requirement_names(base_text)
        final_location: Dict[str, str] = {n: "base" for n in base_names}
        for (_old_spec, mname), (mtarget, _new_req) in moves.items():
            final_location[mname] = mtarget

        # Build proposed new contents (base + targets), then relink refs across
        # every spec so addresses stay consistent. Intra-spec references crossing
        # the relocation boundary are relinked per-text first (the remaining base
        # pointing at moved Requirements, and each moved block pointing back at
        # Requirements that stayed in base or moved to a different target).
        remaining_base = relink_intra_spec_refs(
            remaining_base, "base", final_location, known_reqs=base_names
        )
        proposed: Dict[str, str] = {"base": remaining_base}
        for target, blks in target_to_blocks.items():
            relinked_blks = [
                relink_intra_spec_refs(b, target, final_location, known_reqs=base_names)
                for b in blks
            ]
            proposed[target] = append_requirements(specs[target][1], relinked_blks)

        # The moved addresses all reference ``base`` requirements, so the known-name
        # guard for inter-spec relinking is the base spec's indexed requirement set
        # (distinguishes ``base::Foo`` from a distinct ``base::Foo bar`` that did
        # not move, regardless of capitalization).
        moved_known: Dict[str, list] = {"base": base_names}
        changed: Dict[str, str] = {}
        for name, (path, original) in specs.items():
            candidate = proposed.get(name, original)
            relinked = rewrite_moved_refs(candidate, moves, known_reqs=moved_known)
            if relinked != original:
                changed[name] = relinked

        # Atomic validation gate: any structural failure aborts the migration.
        for name, content in changed.items():
            validation = validate_spec_structure(content, name)
            if not validation.passed:
                logger.error(
                    "Base migration aborted — '%s' would fail validation: %s",
                    name, "; ".join(validation.errors),
                )
                return {
                    "specs_updated": 0,
                    "migrated": [],
                    "skipped": names,
                    "error": f"validation failed for {name}",
                }

        edits = [
            (specs[name][0], content, specs[name][1])
            for name, content in changed.items()
        ]
        error = self._write_all_or_restore(edits, [])
        if error is not None:
            return {
                "specs_updated": 0,
                "migrated": [],
                "skipped": names,
                "error": f"write failed: {error}",
            }

        result["specs_updated"] = len(changed)
        self._rebuild_index()
        return result

    def apply_split(self, proposal: Any) -> Dict[str, Any]:
        """Split a cluster of Requirements out of a spec into a parallel spec.

        ``proposal`` is a ``SplitProposal`` (``source_spec`` / ``new_spec`` /
        ``requirement_names`` / ``domain`` / ``purpose``). The named Requirements
        are cut from the source spec and assembled into a brand-new parallel spec
        carrying its own ``<!-- domain: ... -->`` marker; inter-spec
        ``<source>::<requirement>`` references across every spec are relinked to
        ``<new_spec>::<requirement>``. Both the trimmed source and the new spec
        are validated before any write (atomic). On success the index is rebuilt
        so the moved items resolve at their new ``<new_spec>::<requirement>``
        addresses. Returns ``{"created": bool, "new_spec": str, ...}``.
        """
        from .spec_validator import validate_spec_structure
        from .sync_governance import (
            build_parallel_spec,
            normalize_spec_name,
            relink_intra_spec_refs,
            requirement_names,
            rewrite_moved_refs,
            split_out_requirements,
        )

        source_spec = getattr(proposal, "source_spec", "")
        raw_new_spec = getattr(proposal, "new_spec", "")
        req_names = list(getattr(proposal, "requirement_names", []) or [])
        domain = getattr(proposal, "domain", None)
        purpose = getattr(proposal, "purpose", "") or ""

        # The new spec name is interpolated unquoted into the filesystem path
        # (``se3/specs/<new_spec>/spec.md``) and into every relinked
        # ``<new_spec>::<req>`` logical address, so it MUST be a flat, safe
        # kebab component. An LLM that confuses the name with the layered
        # ``domain`` field (e.g. ``"engine/merge-internals"``) or emits ``..``
        # would otherwise create a nested or out-of-tree directory the
        # one-level index and ``_all_spec_texts()`` cannot see, silently
        # losing the moved Requirements from the navigation layer.
        new_spec = normalize_spec_name(raw_new_spec)
        if not new_spec:
            return {
                "created": False,
                "error": f"invalid new spec name '{raw_new_spec}'",
            }

        specs = self._all_spec_texts()
        if source_spec not in specs:
            return {"created": False, "error": f"source spec '{source_spec}' not found"}
        if new_spec == source_spec:
            return {
                "created": False,
                "error": f"new spec name '{new_spec}' collides with source spec",
            }
        if new_spec in specs:
            return {"created": False, "error": f"target spec '{new_spec}' already exists"}
        if not req_names:
            return {"created": False, "error": "no requirements to split"}

        source_path, source_text = specs[source_spec]
        remaining_source, blocks = split_out_requirements(source_text, req_names)
        if not blocks:
            return {"created": False, "error": "none of the named requirements found"}

        # Post-move location of every Requirement originally in the source spec:
        # a split-out name lands in the new parallel spec, every other name stays
        # in the source. This drives intra-spec `Requirement: <name>` relinking so
        # the documented primary reference form survives the split — the trimmed
        # source pointing at moved Requirements, and each moved block pointing
        # back at Requirements that stayed in the source.
        source_names = requirement_names(source_text)
        final_location: Dict[str, str] = {
            n: source_spec for n in source_names
        }
        for n in blocks:
            final_location[n] = new_spec

        remaining_source = relink_intra_spec_refs(
            remaining_source, source_spec, final_location, known_reqs=source_names
        )
        ordered_blocks = [
            relink_intra_spec_refs(
                blocks[n], new_spec, final_location, known_reqs=source_names
            )
            for n in req_names
            if n in blocks
        ]

        new_spec_text = build_parallel_spec(
            new_spec, ordered_blocks, domain=domain, purpose=purpose
        )
        moves: Dict[Tuple[str, str], Tuple[str, str]] = {
            (source_spec, n): (new_spec, n) for n in blocks
        }

        # A moved Requirement block may carry an explicit inter-spec
        # `<source>::<other-moved-req>` reference pointing at a *sibling* that
        # moved into the new spec alongside it. `relink_intra_spec_refs` only
        # rewrites the intra-spec `Requirement: <name>` prose form, so without
        # this the new spec would retain a `<source>::<name>` address that no
        # longer resolves after the split. Apply the same move map used for the
        # trimmed source and every other spec so the new spec's own inter-spec
        # references to relocated siblings relink to `<new_spec>::<name>`.
        # The moved addresses all reference the source spec, so the known-name
        # guard for inter-spec relinking is the source spec's indexed requirement
        # set (capitalization-independent prefix disambiguation).
        moved_known: Dict[str, list] = {source_spec: source_names}
        new_spec_text = rewrite_moved_refs(new_spec_text, moves, known_reqs=moved_known)

        # Validate the trimmed source + new spec before writing anything.
        for name, content in ((source_spec, remaining_source), (new_spec, new_spec_text)):
            validation = validate_spec_structure(content, name)
            if not validation.passed:
                logger.error(
                    "Spec split aborted — '%s' would fail validation: %s",
                    name, "; ".join(validation.errors),
                )
                return {
                    "created": False,
                    "error": f"validation failed for {name}",
                }

        # Relink refs in every other spec.
        changed: Dict[str, str] = {
            source_spec: rewrite_moved_refs(remaining_source, moves, known_reqs=moved_known)
        }
        for name, (path, original) in specs.items():
            if name == source_spec:
                continue
            relinked = rewrite_moved_refs(original, moves, known_reqs=moved_known)
            if relinked != original:
                changed[name] = relinked

        # Write the trimmed source + relinked specs and create the new spec
        # atomically: if creating the parallel spec fails, the source/ref edits
        # are rolled back so requirements never vanish from the source while the
        # new spec is missing (and no relinked ref points at a nonexistent spec).
        edits = [
            (specs[name][0], content, specs[name][1])
            for name, content in changed.items()
        ]
        new_path = self._specs_dir() / new_spec / "spec.md"
        error = self._write_all_or_restore(edits, [(new_path, new_spec_text)])
        if error is not None:
            return {"created": False, "error": f"write failed: {error}"}

        self._rebuild_index()
        return {
            "created": True,
            "new_spec": new_spec,
            "source_spec": source_spec,
            "moved_requirements": list(blocks.keys()),
            "relinked_specs": [n for n in changed if n != source_spec],
        }

    def backfill_domains(self, domains: Dict[str, str]) -> List[str]:
        """Backfill ``<!-- domain: ... -->`` markers for specs missing one.

        ``domains`` maps spec name → domain path. Only specs that (a) are listed
        in *domains*, (b) exist on disk, and (c) currently lack a domain marker
        are touched. A spec absent from *domains* or already carrying a marker is
        left unchanged — a missing domain never blocks sync; such specs simply
        render under the "(未分类)" group. Returns the list of specs updated.
        """
        from .sync_governance import ensure_domain_marker, has_domain_marker

        specs = self._all_spec_texts()
        edits: List[Tuple[Path, str, str]] = []
        updated: List[str] = []
        for name, domain in domains.items():
            if not domain or not domain.strip():
                continue
            entry = specs.get(name)
            if entry is None:
                continue
            path, text = entry
            if has_domain_marker(text):
                continue
            new_text = ensure_domain_marker(text, domain)
            if new_text != text:
                edits.append((path, new_text, text))
                updated.append(name)
        if not edits:
            return []
        # Write every backfilled spec through the same atomic write-and-restore
        # mechanism the split/migration paths use: each individual write goes via
        # ``_atomic_write`` (temp file + ``os.replace``) so a write that raises
        # never truncates an authoritative ``spec.md``, and on the first failure
        # every already-committed marker is rolled back to its original content.
        # A partial / interrupted / disk-full write therefore never leaves one
        # spec truncated while earlier specs retain their new marker.
        error = self._write_all_or_restore(edits, [])
        if error is not None:
            logger.warning("Failed to backfill domains atomically: %s", error)
            return []
        self._rebuild_index()
        return updated

    def specs_missing_domain(self) -> List[str]:
        """Return the names of on-disk specs that declare no domain marker."""
        from .sync_governance import has_domain_marker

        missing: List[str] = []
        for name, (path, text) in self._all_spec_texts().items():
            if not has_domain_marker(text):
                missing.append(name)
        return sorted(missing)

    def oversized_specs(self) -> List[str]:
        """Return non-base spec names whose file exceeds the warn threshold.

        These are split-evaluation candidates: a spec over the configured
        ``spec_file_warn_bytes`` *may* be multi-topic and warrant a parallel
        split (the LLM judges cohesion in ``propose_spec_split``). ``base`` is
        excluded — its over-limit treatment is content migration, not split.
        """
        from ..config import load_spec_governance_config

        cfg = load_spec_governance_config(self.project_root)
        limit = cfg.spec_file_warn_bytes
        out: List[str] = []
        for name, (path, text) in self._all_spec_texts().items():
            if name == "base":
                continue
            if len(text.encode("utf-8")) > limit:
                out.append(name)
        return sorted(out)

    def propose_domain_backfill(self, llm_caller: Any) -> List[str]:
        """LLM-assisted: assign + persist a ``<!-- domain: -->`` marker for every
        spec that currently lacks one, so existing domain-less specs stop
        rendering under ``(未分类)`` forever.

        This is the restructuring-time domain maintenance the governance model
        promises (``se3 sync`` *维护与补全* domains): it asks the LLM to place
        each domain-less spec in the hierarchical domain taxonomy already used by
        the project's other specs, validates the proposal, and applies it via the
        pure :meth:`backfill_domains`. No-ops (zero LLM calls) when every spec
        already declares a domain. The single LLM use here is proposal
        generation; the actual write is pure and never blocks sync. Returns the
        list of specs that received a marker.
        """
        missing = self.specs_missing_domain()
        if not missing:
            return []

        from .sync_governance import domain_of, requirement_names

        specs = self._all_spec_texts()
        # Existing domain taxonomy (examples that steer the LLM toward a
        # consistent hierarchy rather than inventing a parallel one).
        existing: List[str] = []
        for name, (_p, text) in specs.items():
            dom = domain_of(text)
            if dom:
                existing.append(f"- {name}: {dom}")

        def _spec_brief(name: str) -> str:
            entry = specs.get(name)
            if entry is None:
                return f"- {name}"
            reqs = requirement_names(entry[1])[:6]
            req_hint = ("; ".join(reqs)) if reqs else "(no requirements)"
            return f"- {name} — Requirements: {req_hint}"

        prompt = (
            "You are assigning a hierarchical `domain` path to each spec that "
            "currently lacks a `<!-- domain: <path> -->` header marker.\n\n"
            "A domain is a layered path (e.g. `engine/steps`, `server`, "
            "`engine/merge`) that classifies the spec ABOVE the spec level so "
            "the navigation index can group related specs. Reuse the existing "
            "taxonomy below where a spec fits under it; otherwise introduce a "
            "concise new path.\n\n"
            "## Existing domain taxonomy\n"
            + ("\n".join(sorted(existing)) if existing else "(none yet)")
            + "\n\n## Specs needing a domain\n"
            + "\n".join(_spec_brief(n) for n in missing)
            + "\n\n## Instructions\n"
            "Output a JSON array; each entry is {\"spec_name\": <one of the "
            "specs needing a domain above>, \"domain\": <hierarchical path, "
            "lowercase, '/'-separated>}. Assign a domain to EVERY listed spec. "
            "Output ONLY the JSON array."
        )
        try:
            raw = llm_caller.call(prompt=prompt, json_mode="off")
        except Exception as exc:
            logger.error("propose_domain_backfill LLM call failed: %s", exc)
            return []

        entries = self._parse_json_array(raw)
        valid_specs = set(missing)
        domains: Dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("spec_name", "")
            domain = entry.get("domain", "")
            if (
                isinstance(name, str)
                and isinstance(domain, str)
                and name in valid_specs
                and domain.strip()
            ):
                domains[name] = domain.strip()
        if not domains:
            return []
        return self.backfill_domains(domains)

    def run_governance(self, llm_caller: Any) -> Dict[str, Any]:
        """Post-convergence spec volume-governance detection + proposal.

        Deterministically detects governance work — an over-limit ``base`` and
        over-sized (potentially multi-topic) module specs — and generates the
        respond-channel confirmation call files the user approves via ``se3
        sync-respond``. The actual content move (migration / split) stays a pure
        operation gated behind that human confirmation; the only LLM use here is
        proposal generation, and each proposal step no-ops when its threshold is
        not exceeded (so a compliant project incurs zero extra LLM calls). Also
        reports specs still missing a ``<!-- domain: -->`` marker so the caller
        can surface the backfill backlog. Never raises — each sub-step is
        independently fault-tolerant so a governance hiccup cannot fail an
        otherwise-converged sync.
        """
        result: Dict[str, Any] = {
            "base_migration_call": None,
            "split_calls": [],
            "domains_backfilled": [],
            "specs_missing_domain": [],
        }
        try:
            if self.base_exceeds_limit():
                call = self.propose_base_migration(llm_caller)
                if call is not None:
                    result["base_migration_call"] = str(call)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Governance: base migration proposal failed: %s", exc)
        try:
            for spec_name in self.oversized_specs():
                call = self.propose_spec_split(spec_name, llm_caller)
                if call is not None:
                    result["split_calls"].append(str(call))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Governance: spec split proposal failed: %s", exc)
        # Restructuring-time domain maintenance: actually ASSIGN and PERSIST a
        # domain marker for specs that lack one, rather than only reporting them
        # — otherwise a domain-less spec renders under "(未分类)" on every sync
        # forever. Runs before the audit below so the reported backlog reflects
        # what remains after the backfill.
        try:
            result["domains_backfilled"] = self.propose_domain_backfill(llm_caller)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Governance: domain backfill failed: %s", exc)
        try:
            result["specs_missing_domain"] = self.specs_missing_domain()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Governance: domain audit failed: %s", exc)
        return result

    @staticmethod
    def _parse_json_array(raw: Any) -> List[Dict[str, Any]]:
        """Parse a JSON array from LLM stdout (fence-tolerant). Returns ``[]`` on failure."""
        if not isinstance(raw, str) or not raw.strip():
            return []
        cleaned = strip_markdown_fences(raw.strip()).strip()
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            return []
        if isinstance(data, dict):
            for key in ("migrations", "proposals", "items", "result"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        return data if isinstance(data, list) else []

    def propose_base_migration(self, llm_caller: Any) -> Optional[Path]:
        """LLM-assisted: propose which over-budget ``base`` Requirements to relocate.

        Injects the base admission standard plus the list of base Requirements
        and the available module specs, asks the LLM for a JSON array of
        ``{"requirement_name", "target_spec"}`` entries, and writes a
        ``sync_base_migration`` respond-channel call file. Returns the call-file
        path, or ``None`` when base is within limit or the LLM proposes nothing.

        This is the only LLM step in the base-migration flow; the actual content
        move (``migrate_requirements`` via ``se3 sync-respond``) is pure.
        """
        from .spec_governance import BASE_ADMISSION_STANDARD
        from .sync_governance import BaseMigration, requirement_names
        from .sync_interaction import write_base_migration_call

        specs = self._all_spec_texts()
        base = specs.get("base")
        if base is None:
            return None
        base_reqs = requirement_names(base[1])
        module_specs = [n for n in specs if n != "base"]
        if not base_reqs or not module_specs:
            return None

        prompt = (
            "You are auditing the `base` spec against its admission standard.\n\n"
            f"{BASE_ADMISSION_STANDARD}\n\n"
            "## base Requirements\n"
            + "\n".join(f"- {n}" for n in base_reqs)
            + "\n\n## Available module specs (migration targets)\n"
            + "\n".join(f"- {n}" for n in module_specs)
            + "\n\n## Instructions\n"
            "List ONLY the base Requirements that violate the admission standard "
            "(module-specific detail that belongs in a module spec). Output a JSON "
            "array; each entry is {\"requirement_name\": <exact base Requirement "
            "name>, \"target_spec\": <one of the module specs above>}. Output an "
            "empty array [] if base is already compliant. Output ONLY the JSON."
        )
        try:
            raw = llm_caller.call(prompt=prompt, json_mode="off")
        except Exception as exc:
            logger.error("propose_base_migration LLM call failed: %s", exc)
            return None

        entries = self._parse_json_array(raw)
        valid_reqs = set(base_reqs)
        valid_targets = set(module_specs)
        migrations: List[BaseMigration] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            req = entry.get("requirement_name", "")
            target = entry.get("target_spec", "")
            if req in valid_reqs and target in valid_targets:
                migrations.append(
                    BaseMigration(requirement_name=req, target_spec=target)
                )
        if not migrations:
            return None
        return write_base_migration_call(self.project_root, migrations)

    def propose_spec_split(
        self, spec_name: str, llm_caller: Any
    ) -> Optional[Path]:
        """LLM-assisted: propose splitting a multi-topic spec into a parallel spec.

        Asks the LLM to cluster the spec's Requirements and, only when the spec
        is genuinely multi-topic (sparse cross-cluster references), name a cluster
        to move into a new parallel spec. Writes a ``sync_spec_split`` call file
        and returns its path, or ``None`` when the spec is cohesive (no split) or
        the LLM proposes nothing. ``update_spec`` never reaches this path — only
        sync may create a parallel spec, and only after respond-channel approval.
        """
        from .spec_governance import SPLIT_CRITERIA
        from .sync_governance import SplitProposal, requirement_names
        from .sync_interaction import write_spec_split_call

        specs = self._all_spec_texts()
        entry = specs.get(spec_name)
        if entry is None:
            return None
        reqs = requirement_names(entry[1])
        if len(reqs) < 2:
            return None

        prompt = (
            f"You are evaluating whether the `{spec_name}` spec should be split.\n\n"
            f"{SPLIT_CRITERIA}\n\n"
            f"## `{spec_name}` Requirements\n"
            + "\n".join(f"- {n}" for n in reqs)
            + "\n\n## Instructions\n"
            "Apply cohesion before size. If `" + spec_name + "` is internally "
            "cohesive, output an empty array []. Only if it is genuinely "
            "multi-topic, output a JSON array with a SINGLE entry: "
            "{\"new_spec\": <kebab-case new spec name>, \"requirement_names\": "
            "[<exact Requirement names to move>], \"domain\": <layered/path or "
            "null>, \"purpose\": <one-sentence locator>, \"rationale\": <why>}. "
            "Output ONLY the JSON."
        )
        try:
            raw = llm_caller.call(prompt=prompt, json_mode="off")
        except Exception as exc:
            logger.error("propose_spec_split LLM call failed: %s", exc)
            return None

        entries = self._parse_json_array(raw)
        valid_reqs = set(reqs)
        proposals: List[SplitProposal] = []
        for entry_d in entries:
            if not isinstance(entry_d, dict):
                continue
            new_spec = (entry_d.get("new_spec") or "").strip()
            move = [
                n for n in (entry_d.get("requirement_names") or [])
                if n in valid_reqs
            ]
            # Refuse a degenerate "split" that moves everything (or nothing).
            if not new_spec or not move or len(move) >= len(reqs):
                continue
            proposals.append(
                SplitProposal(
                    source_spec=spec_name,
                    new_spec=new_spec,
                    requirement_names=move,
                    domain=entry_d.get("domain"),
                    purpose=entry_d.get("purpose", "") or "",
                    rationale=entry_d.get("rationale", "") or "",
                )
            )
        if not proposals:
            return None
        return write_spec_split_call(self.project_root, proposals)

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
        if call_type == "sync_base_migration":
            return self._process_base_migration_response(call_data, response_data)
        if call_type == "sync_spec_split":
            return self._process_split_response(call_data, response_data)
        if call_type != "sync_high_impact_deletion":
            raise ValueError(
                "Unsupported sync call file type "
                f"'{call_type}'. Supported sync call files are "
                "'sync_high_impact_deletion', 'sync_base_migration', and "
                "'sync_spec_split'. Legacy formats (sync_pending_decisions, "
                "conflict-only) are no longer supported — re-run "
                "'se3 sync --interactive' to generate a fresh call file."
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

    # ------------------------------------------------------------------
    # base migration / parallel split — respond-channel handlers
    # ------------------------------------------------------------------

    def _process_base_migration_response(
        self, call_data: Dict[str, Any], response_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Apply the approved entries of a ``sync_base_migration`` call file."""
        from .sync_governance import BaseMigration
        from .sync_interaction import parse_decisions

        decisions = parse_decisions(call_data, response_data)
        by_id = {
            item.get("item_id", ""): item for item in call_data.get("items", [])
        }
        approved: List[BaseMigration] = []
        skipped = 0
        for item_id, decision in decisions.items():
            item = by_id.get(item_id)
            if not item:
                continue
            if decision != "approve":
                skipped += 1
                continue
            approved.append(
                BaseMigration(
                    requirement_name=item.get("requirement_name", ""),
                    target_spec=item.get("target_spec", ""),
                    item_id=item_id,
                )
            )

        if not approved:
            return {"specs_updated": 0, "skipped": skipped}

        result = self.migrate_requirements(approved)
        skipped += len(result.get("skipped", []))
        response: Dict[str, Any] = {
            "specs_updated": result.get("specs_updated", 0),
            "skipped": skipped,
            "migrated": result.get("migrated", []),
        }
        if result.get("error"):
            response["error"] = result["error"]
        return response

    def _process_split_response(
        self, call_data: Dict[str, Any], response_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Apply the approved entries of a ``sync_spec_split`` call file."""
        from .sync_governance import SplitProposal
        from .sync_interaction import parse_decisions

        decisions = parse_decisions(call_data, response_data)
        by_id = {
            item.get("item_id", ""): item for item in call_data.get("items", [])
        }
        specs_created: List[str] = []
        errors: List[str] = []
        skipped = 0
        for item_id, decision in decisions.items():
            item = by_id.get(item_id)
            if not item:
                continue
            if decision != "approve":
                skipped += 1
                continue
            proposal = SplitProposal(
                source_spec=item.get("source_spec", ""),
                new_spec=item.get("new_spec", ""),
                requirement_names=list(item.get("requirement_names", []) or []),
                domain=item.get("domain"),
                purpose=item.get("purpose", "") or "",
                rationale=item.get("rationale", "") or "",
                item_id=item_id,
            )
            outcome = self.apply_split(proposal)
            if outcome.get("created"):
                specs_created.append(outcome.get("new_spec", ""))
            else:
                skipped += 1
                if outcome.get("error"):
                    new_spec = item.get("new_spec", "") or "?"
                    errors.append(f"{new_spec}: {outcome['error']}")

        response: Dict[str, Any] = {
            "specs_created": specs_created,
            "specs_updated": len(specs_created),
            "skipped": skipped,
        }
        if errors:
            response["error"] = "; ".join(errors)
        return response
