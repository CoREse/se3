"""SE3 Sync Engine — Data models and orchestration for spec-code synchronization.

Defines the core data structures for sync analysis results and provides
the SyncEngine class that orchestrates the full sync workflow.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SYNC_TAGS = ["auto-discovered", "source:sync"]


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


class DiffType(Enum):
    """Type of difference between spec and code."""

    GAP = "gap"
    EXTENSION = "extension"
    CONFLICT = "conflict"


class ConflictDecision(Enum):
    """User decision for a conflict."""

    PENDING = "pending"
    UPDATE_SPEC = "update_spec"
    CREATE_ISSUE = "create_issue"


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
class PendingDecision:
    """A gap or conflict requiring human decision."""

    type: str  # "gap" or "conflict"
    item_id: str = ""
    spec_name: str = ""
    description: str = ""
    diff: str = ""
    confidence: str = ""
    decision: str = "pending"  # "pending", "update_spec", "create_issue"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "item_id": self.item_id,
            "spec_name": self.spec_name,
            "description": self.description,
            "diff": self.diff,
            "confidence": self.confidence,
            "decision": self.decision,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PendingDecision:
        return cls(
            type=data.get("type", "gap"),
            item_id=data.get("item_id", ""),
            spec_name=data.get("spec_name", ""),
            description=data.get("description", ""),
            diff=data.get("diff", ""),
            confidence=data.get("confidence", ""),
            decision=data.get("decision", "pending"),
        )


@dataclass
class Conflict:
    """A conflict requiring human decision."""

    spec_name: str
    description: str
    spec_content: str = ""
    code_content: str = ""
    code_location: str = ""
    confidence: str = ""
    decision: ConflictDecision = ConflictDecision.PENDING

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "spec_name": self.spec_name,
            "description": self.description,
            "spec_content": self.spec_content,
            "code_content": self.code_content,
            "code_location": self.code_location,
            "decision": self.decision.value,
        }
        if self.confidence:
            d["confidence"] = self.confidence
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Conflict:
        return cls(
            spec_name=data["spec_name"],
            description=data["description"],
            spec_content=data.get("spec_content", ""),
            code_content=data.get("code_content", ""),
            code_location=data.get("code_location", ""),
            confidence=data.get("confidence", ""),
            decision=ConflictDecision(data.get("decision", "pending")),
        )


@dataclass
class SyncResult:
    """Overall result of a sync operation."""

    analyses: List[SpecAnalysis] = field(default_factory=list)
    issues_created: int = 0
    issues_closed: int = 0
    specs_updated: int = 0
    conflicts: List[Conflict] = field(default_factory=list)
    call_file: Optional[str] = None
    completed_at: datetime = field(default_factory=datetime.now)
    specs_created: List[str] = field(default_factory=list)
    gap_resolutions: List[Dict[str, Any]] = field(default_factory=list)
    conflict_resolutions: List[Dict[str, Any]] = field(default_factory=list)
    detailed_changes: List[Dict[str, Any]] = field(default_factory=list)
    pending_decisions: List[PendingDecision] = field(default_factory=list)
    discovery_failed: bool = False

    @property
    def total_gaps(self) -> int:
        return sum(len(a.gaps) for a in self.analyses)

    @property
    def total_extensions(self) -> int:
        return sum(len(a.extensions) for a in self.analyses)

    @property
    def total_conflicts(self) -> int:
        return sum(len(a.conflicts) for a in self.analyses)

    @property
    def all_in_sync(self) -> bool:
        return all(a.is_in_sync for a in self.analyses)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "analyses": [a.to_dict() for a in self.analyses],
            "issues_created": self.issues_created,
            "issues_closed": self.issues_closed,
            "specs_updated": self.specs_updated,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "call_file": self.call_file,
            "completed_at": self.completed_at.isoformat(),
            "specs_created": self.specs_created,
            "gap_resolutions": self.gap_resolutions,
            "conflict_resolutions": self.conflict_resolutions,
            "detailed_changes": self.detailed_changes,
            "pending_decisions": [p.to_dict() for p in self.pending_decisions],
            "discovery_failed": self.discovery_failed,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SyncResult:
        completed_at = data.get("completed_at")
        if isinstance(completed_at, str):
            completed_at = datetime.fromisoformat(completed_at)
        elif not isinstance(completed_at, datetime):
            completed_at = datetime.now()

        return cls(
            analyses=[SpecAnalysis.from_dict(a) for a in data.get("analyses", [])],
            issues_created=data.get("issues_created", 0),
            issues_closed=data.get("issues_closed", 0),
            specs_updated=data.get("specs_updated", 0),
            conflicts=[Conflict.from_dict(c) for c in data.get("conflicts", [])],
            call_file=data.get("call_file"),
            completed_at=completed_at,
            specs_created=data.get("specs_created", []),
            gap_resolutions=data.get("gap_resolutions", []),
            conflict_resolutions=data.get("conflict_resolutions", []),
            detailed_changes=data.get("detailed_changes", []),
            pending_decisions=[
                PendingDecision.from_dict(p) for p in data.get("pending_decisions", [])
            ],
            discovery_failed=data.get("discovery_failed", False),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


_CONFLICT_RESOLUTION_PROMPT = """\
You are an expert software engineer resolving a spec-code conflict.

## Conflict Details

**Spec:** {spec_name}
**Description:** {description}
**Code location:** {code_location}

## Current Spec Content

{spec_content}

## Task

Decide how to resolve this conflict:

1. **update_spec** — The code is correct or represents a deliberate improvement; update the spec to match.
2. **create_issue** — The spec is correct; the code needs to be fixed.

Consider:
- If the code represents a deliberate improvement or natural evolution, choose update_spec.
- If the code appears to violate an intentional, important requirement, choose create_issue.

Return a JSON object:
{{"decision": "update_spec" or "create_issue", "reasoning": "Brief explanation"}}
"""

_CONFLICT_SPEC_UPDATE_PROMPT = """\
You are an expert software engineer updating a specification document.

## Task

A conflict was found between the spec and project code. The decision is to update
the spec to match the code's actual behavior.

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

_GAP_RESOLUTION_PROMPT = """\
You are an expert software engineer analyzing a spec-code gap.

## Gap Details

**Spec:** {spec_name}
**Gap description:** {description}
**Code location:** {code_location}

## Current Spec Content

{spec_content}

## Task

A gap was found: the spec describes a requirement that is NOT implemented in the code.
Determine whether this gap represents:

1. **update_spec** — The spec requirement is outdated or no longer relevant. The code has
   evolved past this requirement, or the requirement was intentionally not implemented because
   a better approach was taken. The spec should be updated to remove the outdated requirement.
2. **create_issue** — The spec requirement is still valid and important. The code genuinely
   needs to implement this requirement. Create an issue to track this work.

## Guiding Principle

Code is the implementation standard. If the code demonstrates a clear, progressive improvement
over what the spec describes, the spec should be updated to reflect reality. Only choose
create_issue when the missing implementation represents a genuine deficiency.

Return a JSON object:
{{"decision": "update_spec" or "create_issue", "confidence": "high" or "low", "reasoning": "Brief explanation"}}
"""

_GAP_SPEC_UPDATE_PROMPT = """\
You are an expert software engineer updating a specification document.

## Task

A gap was found between the spec and project code. The decision is to update the spec
by removing the outdated requirement, since the code has evolved past it.

## Gap

{description}
Code location: {code_location}

## Spec: {spec_name}

### Current Spec Content

{spec_content}

## Instructions

Precisely locate and remove the outdated requirement described in the gap.
Keep ALL other existing requirements intact — only remove the specific outdated content.
Return the complete updated spec content (the full markdown document).
Do NOT remove any requirements that are still valid.
"""

_SPEC_UPDATE_PROMPT_TEMPLATE = """\
You are an expert software engineer updating a specification document.

## Task

The project code contains functionality that is NOT described in the current spec.
Update the spec to reflect the code's actual behavior. Preserve the existing spec
structure and formatting. Only ADD content — do not remove or weaken existing
requirements.

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


class SyncEngine:
    """Orchestrates the full spec-code synchronization workflow.

    Loads specs via SpecIndex, dispatches per-spec LLM analysis via
    SyncAnalyzer, and processes the results: creates issues for gaps,
    updates specs for extensions, and collects conflicts for human
    decision.
    """

    VALID_MODES = {"default", "strict", "fast"}

    def __init__(
        self,
        project_root: Path,
        mode: str = "default",
        interactive: bool = True,
    ) -> None:
        self.project_root = Path(project_root)
        if mode not in self.VALID_MODES:
            raise ValueError(
                f"Invalid sync mode '{mode}'. Must be one of: {', '.join(sorted(self.VALID_MODES))}"
            )
        self.mode = mode
        self.interactive = interactive
        self._specs: Dict[str, Any] = {}
        self._existing_issues: List[Any] = []
        self._sync_issues: List[Any] = []
        self._issue_manager: Optional[Any] = None
        self._normalized_issue_titles: Optional[set] = None

    def _get_issue_manager(self) -> Any:
        if self._issue_manager is None:
            from .issue_manager import IssueManager
            self._issue_manager = IssueManager(self.project_root)
        return self._issue_manager

    def _load_specs(self) -> Dict[str, Any]:
        """Load all specs via SpecIndex, base spec first.

        Returns:
            Dict mapping spec name to dict with 'name', 'path', 'content'.
        """
        from .spec_index import SpecIndex

        index = SpecIndex(self.project_root).build_index()
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

    def _load_existing_issues(self) -> List[Any]:
        """Load all open issues once, for idempotency and lifecycle management."""
        from .issue_manager import IssueManager

        mgr = IssueManager(self.project_root)
        self._existing_issues = mgr.list_issues(include_closed=False)
        self._sync_issues = mgr.list_by_tags(SYNC_TAGS, include_closed=False)
        self._normalized_issue_titles = {
            self._normalize_for_matching(issue.title)
            for issue in self._sync_issues
        }
        return self._existing_issues

    def run(self, progress_callback: Any = None) -> SyncResult:
        """Execute the full sync workflow.

        1. Discover missing specs (scan codebase for uncovered subsystems)
        2. Load specs (generate base if missing)
        3. Load existing issues
        4. Analyze each spec
        5. Process results by diff type
        6. Manage issue lifecycle
        7. Handle conflicts based on mode

        Args:
            progress_callback: Optional callback(phase, spec_name, index, total, analysis).
                phase is "analyzing"/"analyzed" or "discovering".

        Returns:
            SyncResult with all analysis results and actions taken.
        """
        from .llm_caller import LLMCaller
        from .project_context import ProjectContextCollector
        from .sync_analyzer import SyncAnalyzer
        from .sync_discovery import SpecDiscovery
        from .sync_history import SyncFlowContext

        result = SyncResult()

        flow_ctx = SyncFlowContext(self.project_root)
        flow_ctx.write_meta()

        llm_caller = LLMCaller(
            project_root=self.project_root,
            flow_id=flow_ctx.flow_id,
        )
        analyzer = SyncAnalyzer(self.project_root, llm_caller)

        collector = ProjectContextCollector(self.project_root)
        context_dict = collector.collect()
        project_context = json.dumps(context_dict, indent=2, ensure_ascii=False, default=str)

        specs = self._load_specs()

        if not specs:
            logger.info("No specs found, generating base spec")
            analyzer.generate_base_spec(project_context)
            specs = self._load_specs()

        if progress_callback:
            progress_callback("discovering", None, 0, 0, None)

        try:
            step_id = flow_ctx.make_step_id("sync_scan")
            llm_caller.step_id = step_id
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
                        specs[name] = {"name": name, "path": spec_path, "content": content}
                        self._specs[name] = specs[name]
                    except OSError as e:
                        logger.warning("Failed to read newly created spec '%s': %s", name, e)

            if result.specs_created:
                context_dict = collector.collect()
                project_context = json.dumps(
                    context_dict, indent=2, ensure_ascii=False, default=str
                )
        except Exception as e:
            logger.error("Spec discovery failed, continuing with existing specs: %s", e)
            result.discovery_failed = True

        self._load_existing_issues()

        all_gap_titles: set[str] = set()
        spec_items = list(specs.items())
        total = len(spec_items)

        for i, (spec_name, spec_info) in enumerate(spec_items):
            if progress_callback:
                progress_callback("analyzing", spec_name, i, total, None)

            step_id = flow_ctx.make_step_id("sync_analyze", spec_name)
            llm_caller.step_id = step_id
            llm_caller.step_type = "sync_analyze"

            analysis = analyzer.analyze_spec(
                spec_name, spec_info["content"], project_context
            )
            result.analyses.append(analysis)

            if progress_callback:
                progress_callback("analyzed", spec_name, i, total, analysis)

            gap_result = self._process_gaps(analysis.gaps, llm_caller)
            result.issues_created += gap_result["issues_created"]
            result.specs_updated += gap_result["specs_updated"]
            result.pending_decisions.extend(gap_result["pending_decisions"])
            result.gap_resolutions.extend(gap_result["gap_resolutions"])

            for gap in analysis.gaps:
                all_gap_titles.add(self._normalize_gap_title(gap))

            exts_updated = self._process_extensions(
                analysis.extensions, spec_info, llm_caller
            )
            result.specs_updated += exts_updated
            if exts_updated > 0:
                for ext in analysis.extensions:
                    result.detailed_changes.append({
                        "spec_name": spec_name,
                        "action": "extension_added",
                        "description": ext.description,
                    })

        closed = self._manage_issue_lifecycle(all_gap_titles)
        result.issues_closed += closed

        all_conflicts = self._gather_all_conflicts(result.analyses)

        llm_caller.step_type = "sync_resolve"

        if self.mode == "fast":
            llm_caller.step_id = flow_ctx.make_step_id("sync_resolve", "conflicts_fast")
            cr = self._handle_conflicts_fast(all_conflicts, llm_caller)
            result.specs_updated += cr["specs_updated"]
            result.issues_created += cr["issues_created"]
            result.conflict_resolutions.extend(cr.get("conflict_resolutions", []))
        elif self.mode == "strict":
            llm_caller.step_id = flow_ctx.make_step_id("sync_resolve", "conflicts_strict")
            cr = self._handle_conflicts_strict(all_conflicts)
            result.conflicts = cr["conflicts"]
            result.pending_decisions.extend(cr["pending_decisions"])
        else:
            llm_caller.step_id = flow_ctx.make_step_id("sync_resolve", "conflicts_default")
            cr = self._handle_conflicts_default(all_conflicts, llm_caller)
            result.specs_updated += cr["specs_updated"]
            result.issues_created += cr["issues_created"]
            result.conflicts = cr["unresolved"]
            result.pending_decisions.extend(cr["pending_decisions"])
            result.conflict_resolutions.extend(cr.get("conflict_resolutions", []))

        if result.pending_decisions and self.interactive:
            llm_caller.step_id = flow_ctx.make_step_id("sync_resolve", "interactive")
            resolved = self._interact_for_decisions(result.pending_decisions, llm_caller)
            result.specs_updated += resolved.get("specs_updated", 0)
            result.issues_created += resolved.get("issues_created", 0)

        for cr_item in result.conflict_resolutions:
            result.detailed_changes.append({
                "spec_name": cr_item.get("spec_name", ""),
                "action": f"conflict_{cr_item.get('action', '')}",
                "description": cr_item.get("description", ""),
            })

        return result

    def _normalize_gap_title(self, gap: SpecDiff) -> str:
        """Build a normalized title string for a gap issue."""
        return f"[sync] {gap.spec_name}: {gap.description}"

    _SYNC_TITLE_RE = re.compile(
        r"^\[sync(?:-conflict)?\]\s*([^:]+):\s*(.+)$", re.IGNORECASE
    )

    def _normalize_for_matching(self, title: str) -> str:
        """Normalize a sync issue title for stable comparison.

        For titles matching ``[sync] {spec_name}: {description}``:
        - Extracts spec_name (lowered, stripped) and description.
        - Description: lowered, articles (a/an/the) removed, punctuation
          stripped, whitespace collapsed.
        - Returns ``[sync] {spec_name}: {normalized_description}``.

        Non-sync titles fall back to ``title.lower().strip()``.
        """
        if not title:
            return ""
        m = self._SYNC_TITLE_RE.match(title.strip())
        if not m:
            return title.lower().strip()
        spec_name = m.group(1).strip().lower()
        desc = m.group(2).strip().lower()
        desc = re.sub(r"\b(?:a|an|the)\b", "", desc)
        desc = re.sub(r"[^a-z0-9\s]", "", desc)
        desc = re.sub(r"\s+", " ", desc).strip()
        return f"[sync] {spec_name}: {desc}"

    def _extract_spec_name_from_title(self, title: str) -> Optional[str]:
        """Extract spec_name from a ``[sync] spec_name: ...`` title."""
        m = self._SYNC_TITLE_RE.match(title.strip())
        if m:
            return m.group(1).strip().lower()
        return None

    def _process_gaps(
        self,
        gaps: List[SpecDiff],
        llm_caller: Any = None,
    ) -> Dict[str, Any]:
        """Process spec-leads-code gaps based on the current mode.

        - **fast**: LLM decides every gap (update_spec or create_issue), auto-executes.
        - **default**: LLM decides; high-confidence auto-executes, low-confidence
          marks as PendingDecision.
        - **strict**: All gaps marked as PendingDecision, no auto-execution.

        Falls back to legacy behavior (create issues for all gaps) when
        llm_caller is not provided.

        Returns:
            Dict with issues_created, specs_updated, pending_decisions, and
            gap_resolutions counts/lists.
        """
        empty = {"issues_created": 0, "specs_updated": 0,
                 "pending_decisions": [], "gap_resolutions": []}
        if not gaps:
            return empty

        if llm_caller is None:
            return {"issues_created": self._process_gaps_legacy(gaps),
                    "specs_updated": 0, "pending_decisions": [],
                    "gap_resolutions": []}

        if self.mode == "strict":
            pending = []
            for gap in gaps:
                pd = PendingDecision(
                    type="gap",
                    item_id=f"gap_{gap.spec_name}_{uuid.uuid4().hex[:8]}",
                    spec_name=gap.spec_name,
                    description=gap.description,
                    diff=gap.code_location,
                    decision="pending",
                )
                pending.append(pd)
            return {"issues_created": 0, "specs_updated": 0,
                    "pending_decisions": pending, "gap_resolutions": []}

        created = 0
        updated = 0
        pending = []
        resolutions = []
        for gap in gaps:
            resolution = self._resolve_gap_via_llm(gap, llm_caller)
            decision = resolution.get("decision", "create_issue")
            confidence = resolution.get("confidence", "low")

            if self.mode == "fast" or (self.mode == "default" and confidence == "high"):
                if decision == "update_spec":
                    if self._apply_gap_spec_update(gap, llm_caller):
                        updated += 1
                        resolutions.append({
                            "spec_name": gap.spec_name,
                            "action": "update_spec",
                            "description": gap.description,
                            "reasoning": resolution.get("reasoning", ""),
                        })
                    else:
                        logger.warning(
                            "Gap update_spec failed for '%s', falling back to create_issue",
                            gap.spec_name,
                        )
                        if self._create_gap_issue(gap):
                            created += 1
                        resolutions.append({
                            "spec_name": gap.spec_name,
                            "action": "create_issue",
                            "description": gap.description,
                            "reasoning": "update_spec failed, fell back to create_issue",
                        })
                else:
                    if self._create_gap_issue(gap):
                        created += 1
                    resolutions.append({
                        "spec_name": gap.spec_name,
                        "action": "create_issue",
                        "description": gap.description,
                        "reasoning": resolution.get("reasoning", ""),
                    })
            else:
                pd = PendingDecision(
                    type="gap",
                    item_id=f"gap_{gap.spec_name}_{uuid.uuid4().hex[:8]}",
                    spec_name=gap.spec_name,
                    description=gap.description,
                    diff=gap.code_location,
                    confidence=confidence,
                    decision="pending",
                )
                pending.append(pd)

        return {"issues_created": created, "specs_updated": updated,
                "pending_decisions": pending, "gap_resolutions": resolutions}

    def _process_gaps_legacy(self, gaps: List[SpecDiff]) -> int:
        """Legacy gap processing: create issues for all gaps with idempotency."""
        if not gaps:
            return 0

        created = 0
        for gap in gaps:
            if self._create_gap_issue(gap):
                created += 1
        return created

    def _create_gap_issue(self, gap: SpecDiff) -> bool:
        """Create a single gap issue with idempotency. Returns True if created."""
        mgr = self._get_issue_manager()

        if self._normalized_issue_titles is None:
            self._normalized_issue_titles = {
                self._normalize_for_matching(issue.title)
                for issue in self._sync_issues
            }

        title = self._normalize_gap_title(gap)
        norm_title = self._normalize_for_matching(title)

        if norm_title in self._normalized_issue_titles:
            logger.info("Skipping duplicate issue for gap: %s", title)
            return False

        existing = mgr.find_open_by_title(title)
        if existing:
            logger.info("Skipping duplicate issue for gap: %s", title)
            return False

        description = (
            f"Spec '{gap.spec_name}' describes a requirement that is not "
            f"implemented in the code.\n\n"
            f"**Gap:** {gap.description}\n"
        )
        if gap.code_location:
            description += f"**Expected location:** {gap.code_location}\n"

        mgr.create(
            title=title,
            description=description,
            priority="medium",
            scope="in_scope",
            tags=list(SYNC_TAGS),
            type="task",
        )
        self._normalized_issue_titles.add(norm_title)
        logger.info("Created issue for gap: %s", title)
        return True

    def _resolve_via_llm(
        self,
        llm_caller: Any,
        prompt: str,
        label: str,
        spec_name: str,
        default_confidence: str = "low",
    ) -> Dict[str, str]:
        """Shared LLM resolution for gaps and conflicts.

        Returns:
            Dict with 'decision' ('update_spec' or 'create_issue'),
            'confidence', and 'reasoning'.
        """
        try:
            response = llm_caller.call(prompt=prompt, json_mode="extract")
            data = json.loads(response)
            decision = data.get("decision", "create_issue")
            if decision not in ("update_spec", "create_issue"):
                logger.warning("Unknown LLM decision '%s' for %s, defaulting to create_issue", decision, label)
                decision = "create_issue"
            return {
                "decision": decision,
                "confidence": data.get("confidence", default_confidence).lower(),
                "reasoning": data.get("reasoning", ""),
            }
        except Exception as e:
            logger.error("LLM %s resolution failed for '%s': %s", label, spec_name, e)
            return {"decision": "create_issue", "confidence": "low", "reasoning": str(e)}

    def _resolve_gap_via_llm(
        self, gap: SpecDiff, llm_caller: Any
    ) -> Dict[str, str]:
        """Call LLM to decide how to handle a gap.

        Returns:
            Dict with 'decision' ('update_spec' or 'create_issue'),
            'confidence' ('high' or 'low'), and 'reasoning'.
        """
        spec_content = self._specs.get(gap.spec_name, {}).get("content", "")

        prompt = _GAP_RESOLUTION_PROMPT.format(
            spec_name=gap.spec_name,
            description=gap.description,
            code_location=gap.code_location or "(not specified)",
            spec_content=spec_content or "(not available)",
        )

        return self._resolve_via_llm(llm_caller, prompt, "gap", gap.spec_name)

    def _update_spec_via_llm(
        self, spec_name: str, prompt: str, llm_caller: Any, label: str
    ) -> bool:
        """Shared helper: call LLM to update a spec file with safety guards.

        Performs: LLM call -> strip -> strip_markdown_fences -> empty check
        -> 50% length check -> write to disk -> update in-memory cache.

        Args:
            spec_name: Name of the spec to update.
            prompt: Full prompt for the LLM call.
            llm_caller: LLMCaller instance.
            label: Human-readable label for log messages (e.g. "gap", "extension", "conflict").

        Returns:
            True if the spec was updated successfully.
        """
        spec_info = self._specs.get(spec_name)
        if not spec_info:
            logger.warning("Spec '%s' not found for %s update", spec_name, label)
            return False

        try:
            updated_content = llm_caller.call(prompt=prompt, json_mode="off")
            updated_content = updated_content.strip()
            updated_content = strip_markdown_fences(updated_content)

            if not updated_content:
                logger.warning("LLM returned empty content for %s spec update '%s'", label, spec_name)
                return False

            if len(updated_content) < len(spec_info["content"]) * 0.5:
                logger.warning(
                    "LLM returned suspiciously short content for %s spec update '%s' "
                    "(%d chars vs original %d chars), skipping update",
                    label, spec_name, len(updated_content), len(spec_info["content"]),
                )
                return False

            Path(spec_info["path"]).write_text(updated_content, encoding="utf-8")
            spec_info["content"] = updated_content
            logger.info("Updated spec '%s' for %s resolution", spec_name, label)
            return True
        except Exception as e:
            logger.error("Failed to update spec '%s' for %s: %s", spec_name, label, e)
            return False

    def _apply_gap_spec_update(
        self, gap: SpecDiff, llm_caller: Any
    ) -> bool:
        """Use LLM to remove an outdated requirement from a spec."""
        if gap.spec_name not in self._specs:
            logger.warning("Spec '%s' not found for gap update", gap.spec_name)
            return False
        prompt = _GAP_SPEC_UPDATE_PROMPT.format(
            spec_name=gap.spec_name,
            description=gap.description,
            code_location=gap.code_location or "(not specified)",
            spec_content=self._specs[gap.spec_name].get("content", ""),
        )
        return self._update_spec_via_llm(gap.spec_name, prompt, llm_caller, "gap")

    def _process_extensions(
        self,
        extensions: List[SpecDiff],
        spec_info: Dict[str, Any],
        llm_caller: Any,
    ) -> int:
        """Update spec files for code-extends-spec differences.

        Returns:
            Number of specs updated (0 or 1).
        """
        if not extensions:
            return 0

        spec_name = spec_info["name"]

        if spec_name not in self._specs:
            self._specs[spec_name] = spec_info

        extensions_desc = "\n".join(
            f"- {ext.description}" + (f" (at {ext.code_location})" if ext.code_location else "")
            for ext in extensions
        )

        prompt = _SPEC_UPDATE_PROMPT_TEMPLATE.format(
            spec_name=spec_name,
            spec_content=spec_info["content"],
            extensions_description=extensions_desc,
        )

        return 1 if self._update_spec_via_llm(spec_name, prompt, llm_caller, "extension") else 0

    def _manage_issue_lifecycle(self, current_gap_titles: set[str]) -> int:
        """Auto-close sync gap issues whose gaps have disappeared.

        Only processes gap issues (excludes conflict issues which have
        their own lifecycle via the conflict resolution flow).

        Uses a three-layer matching strategy to avoid false closures:
        1. Normalized match: issue title normalizes to a current gap title.
        2. Prefix fallback: the issue's spec still has gaps (conservative).
        3. Only close when neither condition holds.

        Returns:
            Number of issues closed.
        """
        from .issue_manager import IssueManager

        if not self._sync_issues:
            return 0

        gap_issues = [
            issue for issue in self._sync_issues
            if "conflict" not in issue.tags
        ]

        if not gap_issues:
            return 0

        normalized_current = {
            self._normalize_for_matching(t) for t in current_gap_titles
        }

        current_spec_names: set[str] = set()
        for t in current_gap_titles:
            sn = self._extract_spec_name_from_title(t)
            if sn:
                current_spec_names.add(sn)

        mgr = IssueManager(self.project_root)
        closed = 0

        for issue in gap_issues:
            norm_issue = self._normalize_for_matching(issue.title)
            if norm_issue in normalized_current:
                continue

            issue_spec = self._extract_spec_name_from_title(issue.title)
            if issue_spec and issue_spec in current_spec_names:
                logger.debug(
                    "Keeping issue %s (spec '%s' still has gaps): %s",
                    issue.id, issue_spec, issue.title,
                )
                continue

            try:
                mgr.close_issue(
                    issue.id,
                    reason="Gap resolved: sync check confirmed this requirement is now implemented",
                )
                closed += 1
                logger.info("Auto-closed issue %s: %s", issue.id, issue.title)
            except (ValueError, OSError) as e:
                logger.warning("Failed to close issue %s: %s", issue.id, e)

        return closed

    def _gather_all_conflicts(self, analyses: List[SpecAnalysis]) -> List[Conflict]:
        """Gather all conflict diffs from analyses into Conflict objects."""
        conflicts: List[Conflict] = []

        for analysis in analyses:
            for diff in analysis.conflicts:
                spec_content = self._specs.get(diff.spec_name, {}).get("content", "")
                conflicts.append(Conflict(
                    spec_name=diff.spec_name,
                    description=diff.description,
                    spec_content=spec_content,
                    code_location=diff.code_location,
                    confidence=diff.confidence,
                ))

        return conflicts

    def _generate_pending_call_file(
        self, pending: List[PendingDecision]
    ) -> Path:
        """Generate an MCP call file for pending decisions (non-interactive fallback)."""
        from .sync_interaction import SyncInteractionHandler

        handler = SyncInteractionHandler(self.project_root, pending)
        return handler.generate_pending_call_file()

    def _interact_for_decisions(
        self,
        pending: List[PendingDecision],
        llm_caller: Any,
    ) -> Dict[str, int]:
        """Launch SyncInteractionHandler and execute resolved decisions.

        Returns:
            Dict with specs_updated and issues_created counts.
        """
        from .sync_interaction import SyncInteractionHandler

        handler = SyncInteractionHandler(self.project_root, pending)
        try:
            decisions = handler.collect_decisions()
        except KeyboardInterrupt:
            logger.info("Decision collection interrupted")
            return {"specs_updated": 0, "issues_created": 0}

        return self._execute_decisions(pending, decisions, llm_caller)

    def _execute_decisions(
        self,
        pending: List[PendingDecision],
        decisions: Dict[str, str],
        llm_caller: Any,
    ) -> Dict[str, int]:
        """Execute resolved decisions (update_spec or create_issue).

        Returns:
            Dict with specs_updated and issues_created counts.
        """
        specs_updated = 0
        issues_created = 0

        item_map = {pd.item_id: pd for pd in pending}

        for item_id, decision in decisions.items():
            pd = item_map.get(item_id)
            if pd is None:
                continue

            pd.decision = decision

            if decision == "update_spec":
                if pd.type == "gap":
                    gap_diff = SpecDiff(
                        diff_type=DiffType.GAP,
                        spec_name=pd.spec_name,
                        description=pd.description,
                        code_location=pd.diff,
                    )
                    if self._apply_gap_spec_update(gap_diff, llm_caller):
                        specs_updated += 1
                else:
                    conflict = Conflict(
                        spec_name=pd.spec_name,
                        description=pd.description,
                        code_location=pd.diff,
                    )
                    if self._apply_conflict_spec_update(conflict, llm_caller):
                        specs_updated += 1
            elif decision == "create_issue":
                if pd.type == "gap":
                    gap_diff = SpecDiff(
                        diff_type=DiffType.GAP,
                        spec_name=pd.spec_name,
                        description=pd.description,
                        code_location=pd.diff,
                    )
                    if self._create_gap_issue(gap_diff):
                        issues_created += 1
                else:
                    conflict = Conflict(
                        spec_name=pd.spec_name,
                        description=pd.description,
                        code_location=pd.diff,
                    )
                    if self._apply_conflict_create_issue(conflict):
                        issues_created += 1

        return {"specs_updated": specs_updated, "issues_created": issues_created}

    def _resolve_conflict_via_llm(
        self, conflict: Conflict, llm_caller: Any
    ) -> Dict[str, str]:
        """Call LLM to decide how to resolve a conflict.

        Returns:
            Dict with 'decision' ('update_spec' or 'create_issue'),
            'reasoning', and 'confidence'.
        """
        spec_content = ""
        if conflict.spec_name in self._specs:
            spec_content = self._specs[conflict.spec_name].get("content", "")

        prompt = _CONFLICT_RESOLUTION_PROMPT.format(
            spec_name=conflict.spec_name,
            description=conflict.description,
            code_location=conflict.code_location or "(not specified)",
            spec_content=spec_content or "(not available)",
        )

        result = self._resolve_via_llm(
            llm_caller, prompt, "conflict", conflict.spec_name,
            default_confidence=conflict.confidence or "medium",
        )
        # Pre-assigned confidence from the analyzer takes precedence;
        # unset (None/"") defers to the LLM's assessment.
        if conflict.confidence:
            result["confidence"] = conflict.confidence
        return result

    def _apply_conflict_spec_update(
        self, conflict: Conflict, llm_caller: Any
    ) -> bool:
        """Use LLM to update a spec file based on a conflict resolution."""
        prompt = _CONFLICT_SPEC_UPDATE_PROMPT.format(
            spec_name=conflict.spec_name,
            description=conflict.description,
            code_location=conflict.code_location or "(not specified)",
            spec_content=self._specs.get(conflict.spec_name, {}).get("content", ""),
        )
        return self._update_spec_via_llm(conflict.spec_name, prompt, llm_caller, "conflict")

    def _apply_conflict_create_issue(self, conflict: Conflict) -> bool:
        """Create an issue for a conflict.

        Returns:
            True if the issue was created successfully.
        """
        mgr = self._get_issue_manager()
        title = f"[sync-conflict] {conflict.spec_name}: {conflict.description}"

        existing = mgr.find_open_by_title(title)
        if existing:
            logger.info("Skipping duplicate issue for conflict: %s", title)
            return False

        description = (
            f"Spec '{conflict.spec_name}' conflicts with the code implementation.\n\n"
            f"**Conflict:** {conflict.description}\n"
        )
        if conflict.code_location:
            description += f"**Code location:** {conflict.code_location}\n"

        mgr.create(
            title=title,
            description=description,
            priority="high",
            scope="in_scope",
            tags=list(SYNC_TAGS) + ["conflict"],
            type="task",
        )
        logger.info("Created issue for conflict: %s", title)
        return True

    def _handle_conflicts_fast(
        self, conflicts: List[Conflict], llm_caller: Any
    ) -> Dict[str, Any]:
        """Fast mode: LLM auto-handles all conflicts.

        For each conflict, calls LLM to decide whether to update the spec
        or create an issue, then executes the decision.

        Returns:
            Dict with specs_updated, issues_created, and conflict_resolutions.
        """
        specs_updated = 0
        issues_created = 0
        resolutions: List[Dict[str, Any]] = []

        for conflict in conflicts:
            resolution = self._resolve_conflict_via_llm(conflict, llm_caller)
            decision = resolution["decision"]
            logger.info(
                "Auto-resolved conflict '%s': %s (confidence=%s, reasoning=%s)",
                conflict.spec_name, decision,
                resolution.get("confidence", ""),
                resolution.get("reasoning", "")[:200],
            )

            actual_action = decision
            if decision == "update_spec":
                if self._apply_conflict_spec_update(conflict, llm_caller):
                    specs_updated += 1
                else:
                    logger.warning(
                        "Conflict update_spec failed for '%s', falling back to create_issue",
                        conflict.spec_name,
                    )
                    actual_action = "create_issue"
                    if self._apply_conflict_create_issue(conflict):
                        issues_created += 1
            else:
                if self._apply_conflict_create_issue(conflict):
                    issues_created += 1

            resolutions.append({
                "spec_name": conflict.spec_name,
                "action": actual_action,
                "description": conflict.description,
                "reasoning": resolution.get("reasoning", ""),
            })

        return {
            "specs_updated": specs_updated,
            "issues_created": issues_created,
            "conflict_resolutions": resolutions,
        }

    def _handle_conflicts_strict(
        self, conflicts: List[Conflict],
    ) -> Dict[str, Any]:
        """Strict mode: all conflicts marked as PendingDecision.

        Returns:
            Dict with conflicts list and pending_decisions.
        """
        if not conflicts:
            return {"conflicts": [], "pending_decisions": []}

        pending: List[PendingDecision] = []
        for conflict in conflicts:
            pd = PendingDecision(
                type="conflict",
                item_id=f"conflict_{conflict.spec_name}_{uuid.uuid4().hex[:8]}",
                spec_name=conflict.spec_name,
                description=conflict.description,
                diff=conflict.code_location,
                confidence=conflict.confidence,
                decision="pending",
            )
            pending.append(pd)
        return {"conflicts": conflicts, "pending_decisions": pending}

    def _handle_conflicts_default(
        self, conflicts: List[Conflict], llm_caller: Any,
    ) -> Dict[str, Any]:
        """Default mode: LLM auto-handles high-confidence, collects low-confidence.

        High-confidence conflicts are resolved automatically by LLM.
        Low-confidence conflicts are marked as PendingDecision.

        Returns:
            Dict with specs_updated, issues_created, unresolved list, and pending_decisions.
        """
        specs_updated = 0
        issues_created = 0
        unresolved: List[Conflict] = []
        pending: List[PendingDecision] = []
        resolutions: List[Dict[str, Any]] = []

        for conflict in conflicts:
            if conflict.confidence and conflict.confidence.lower() == "high":
                resolution = self._resolve_conflict_via_llm(conflict, llm_caller)
                decision = resolution["decision"]
                logger.info(
                    "Auto-resolved conflict '%s': %s (confidence=%s, reasoning=%s)",
                    conflict.spec_name, decision,
                    resolution.get("confidence", ""),
                    resolution.get("reasoning", "")[:200],
                )
                actual_action = decision
                if decision == "update_spec":
                    if self._apply_conflict_spec_update(conflict, llm_caller):
                        specs_updated += 1
                    else:
                        logger.warning(
                            "Conflict update_spec failed for '%s', falling back to create_issue",
                            conflict.spec_name,
                        )
                        actual_action = "create_issue"
                        if self._apply_conflict_create_issue(conflict):
                            issues_created += 1
                else:
                    if self._apply_conflict_create_issue(conflict):
                        issues_created += 1
                resolutions.append({
                    "spec_name": conflict.spec_name,
                    "action": actual_action,
                    "description": conflict.description,
                    "reasoning": resolution.get("reasoning", ""),
                })
            else:
                unresolved.append(conflict)
                pd = PendingDecision(
                    type="conflict",
                    item_id=f"conflict_{conflict.spec_name}_{uuid.uuid4().hex[:8]}",
                    spec_name=conflict.spec_name,
                    description=conflict.description,
                    diff=conflict.code_location,
                    confidence=conflict.confidence,
                    decision="pending",
                )
                pending.append(pd)

        return {
            "specs_updated": specs_updated,
            "issues_created": issues_created,
            "unresolved": unresolved,
            "pending_decisions": pending,
            "conflict_resolutions": resolutions,
        }

    def process_call_response(
        self, call_file_path: Path, llm_caller: Any = None
    ) -> Dict[str, Any]:
        """Process an MCP call response file for sync decisions.

        Supports both the legacy conflict-only format (``"conflicts"`` key)
        and the new unified pending-decisions format (``"items"`` key with
        ``type: "sync_pending_decisions"``).

        Args:
            call_file_path: Path to the original call file (response file
                is at {call_file_path}.response).
            llm_caller: LLMCaller instance. Created if None.

        Returns:
            Dict with specs_updated and issues_created counts.
        """
        response_path = Path(str(call_file_path) + ".response")

        if not response_path.exists():
            logger.warning("Response file not found: %s", response_path)
            return {"specs_updated": 0, "issues_created": 0}

        try:
            response_data = json.loads(response_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to read response file '%s': %s", response_path, e)
            return {"specs_updated": 0, "issues_created": 0}

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

        call_data: Dict[str, Any] = {}
        try:
            call_data = json.loads(Path(call_file_path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to read call file '%s': %s", call_file_path, e)
            return {"specs_updated": 0, "issues_created": 0}

        if call_data.get("type") == "sync_pending_decisions":
            return self._process_pending_call_response(
                call_data, response_data, llm_caller
            )

        return self._process_legacy_call_response(
            call_data, response_data, llm_caller
        )

    def _process_pending_call_response(
        self,
        call_data: Dict[str, Any],
        response_data: Dict[str, Any],
        llm_caller: Any,
    ) -> Dict[str, Any]:
        """Handle the ``sync_pending_decisions`` call file format."""
        call_items = {
            item.get("item_id", ""): item
            for item in call_data.get("items", [])
            if item.get("item_id")
        }

        pending = []
        for item in call_data.get("items", []):
            item_id = item.get("item_id", "")
            if item_id:
                pending.append(PendingDecision(
                    type=item.get("type", "gap"),
                    item_id=item_id,
                    spec_name=item.get("spec_name", ""),
                    description=item.get("description", ""),
                    diff=item.get("diff", ""),
                    confidence=item.get("confidence", ""),
                ))

        decisions: Dict[str, str] = {}
        for resp_item in response_data.get("items", []):
            decision = resp_item.get("decision", "")
            if decision not in ("update_spec", "create_issue"):
                continue

            item_id = resp_item.get("item_id", "")
            if item_id and item_id in call_items:
                decisions[item_id] = decision
            else:
                num_id = resp_item.get("id")
                call_list = call_data.get("items", [])
                if isinstance(num_id, int) and 1 <= num_id <= len(call_list):
                    resolved_id = call_list[num_id - 1].get("item_id", "")
                    if resolved_id:
                        decisions[resolved_id] = decision

        return self._execute_decisions(pending, decisions, llm_caller)

    def _process_legacy_call_response(
        self,
        call_data: Dict[str, Any],
        response_data: Dict[str, Any],
        llm_caller: Any,
    ) -> Dict[str, Any]:
        """Handle the legacy conflict-only call file format."""
        call_conflicts = {
            c["id"]: c for c in call_data.get("conflicts", [])
        }

        specs_updated = 0
        issues_created = 0

        for item in response_data.get("conflicts", []):
            conflict_id = item.get("id")
            decision = item.get("decision", "")

            if decision not in ("update_spec", "create_issue"):
                logger.warning("Skipping conflict %s with invalid decision '%s'", conflict_id, decision)
                continue

            original = call_conflicts.get(conflict_id, {})
            if not original:
                logger.warning("Skipping unknown conflict_id %s in response", conflict_id)
                continue

            conflict = Conflict(
                spec_name=original.get("spec_name", ""),
                description=original.get("description", ""),
                code_location=original.get("code_location", ""),
            )

            if decision == "update_spec":
                if self._apply_conflict_spec_update(conflict, llm_caller):
                    specs_updated += 1
            else:
                if self._apply_conflict_create_issue(conflict):
                    issues_created += 1

        return {"specs_updated": specs_updated, "issues_created": issues_created}
