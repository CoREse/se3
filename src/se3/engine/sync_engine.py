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
