"""SE3 Sync Engine — Data models and orchestration for spec-code synchronization.

Defines the core data structures for sync analysis results and provides
the SyncEngine class that orchestrates the full sync workflow.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SYNC_TAGS = ["auto-discovered", "source:sync"]


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

    def __init__(self, project_root: Path, mode: str = "default") -> None:
        self.project_root = Path(project_root)
        self.mode = mode
        self._specs: Dict[str, Any] = {}
        self._existing_issues: List[Any] = []
        self._sync_issues: List[Any] = []

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
        return self._existing_issues

    def run(self, progress_callback: Any = None) -> SyncResult:
        """Execute the full sync workflow.

        1. Load specs (generate base if missing)
        2. Load existing issues
        3. Analyze each spec
        4. Process results by diff type
        5. Manage issue lifecycle
        6. Handle conflicts based on mode

        Args:
            progress_callback: Optional callback(phase, spec_name, index, total, analysis).
                phase is "analyzing" (before) or "analyzed" (after).

        Returns:
            SyncResult with all analysis results and actions taken.
        """
        from .llm_caller import LLMCaller
        from .project_context import ProjectContextCollector
        from .sync_analyzer import SyncAnalyzer

        result = SyncResult()
        llm_caller = LLMCaller(project_root=self.project_root)
        analyzer = SyncAnalyzer(self.project_root, llm_caller)

        collector = ProjectContextCollector(self.project_root)
        context_dict = collector.collect()
        project_context = json.dumps(context_dict, indent=2, ensure_ascii=False, default=str)

        specs = self._load_specs()

        if not specs:
            logger.info("No specs found, generating base spec")
            analyzer.generate_base_spec(project_context)
            specs = self._load_specs()

        self._load_existing_issues()

        all_gap_titles: set[str] = set()
        spec_items = list(specs.items())
        total = len(spec_items)

        for i, (spec_name, spec_info) in enumerate(spec_items):
            if progress_callback:
                progress_callback("analyzing", spec_name, i, total, None)

            analysis = analyzer.analyze_spec(
                spec_name, spec_info["content"], project_context
            )
            result.analyses.append(analysis)

            if progress_callback:
                progress_callback("analyzed", spec_name, i, total, analysis)

            gaps_created = self._process_gaps(analysis.gaps)
            result.issues_created += gaps_created

            for gap in analysis.gaps:
                all_gap_titles.add(self._normalize_gap_title(gap))

            exts_updated = self._process_extensions(
                analysis.extensions, spec_info, llm_caller
            )
            result.specs_updated += exts_updated

        closed = self._manage_issue_lifecycle(all_gap_titles)
        result.issues_closed += closed

        all_conflicts = self._gather_all_conflicts(result.analyses)

        if self.mode == "fast":
            cr = self._handle_conflicts_fast(all_conflicts, llm_caller)
            result.specs_updated += cr["specs_updated"]
            result.issues_created += cr["issues_created"]
        elif self.mode == "strict":
            cr = self._handle_conflicts_strict(all_conflicts)
            result.conflicts = cr["conflicts"]
            if cr["call_file"]:
                result.call_file = cr["call_file"]
        else:
            cr = self._handle_conflicts_default(all_conflicts, llm_caller)
            result.specs_updated += cr["specs_updated"]
            result.issues_created += cr["issues_created"]
            result.conflicts = cr["unresolved"]
            if cr["call_file"]:
                result.call_file = cr["call_file"]

        return result

    def _normalize_gap_title(self, gap: SpecDiff) -> str:
        """Build a normalized title string for a gap issue."""
        return f"[sync] {gap.spec_name}: {gap.description}"

    def _process_gaps(self, gaps: List[SpecDiff]) -> int:
        """Create issues for spec-leads-code gaps with idempotency.

        Returns:
            Number of issues actually created.
        """
        from .issue_manager import IssueManager

        if not gaps:
            return 0

        mgr = IssueManager(self.project_root)
        created = 0

        for gap in gaps:
            title = self._normalize_gap_title(gap)

            existing = mgr.find_open_by_title(title)
            if existing:
                logger.info("Skipping duplicate issue for gap: %s", title)
                continue

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
            created += 1
            logger.info("Created issue for gap: %s", title)

        return created

    def _process_extensions(
        self,
        extensions: List[SpecDiff],
        spec_info: Dict[str, Any],
        llm_caller: Any,
    ) -> int:
        """Update spec files for code-extends-spec differences.

        Uses LLM to generate updated spec content that includes the
        extensions found in the code.

        Returns:
            Number of specs updated (0 or 1).
        """
        if not extensions:
            return 0

        spec_name = spec_info["name"]
        spec_content = spec_info["content"]
        spec_path = spec_info["path"]

        extensions_desc = "\n".join(
            f"- {ext.description}" + (f" (at {ext.code_location})" if ext.code_location else "")
            for ext in extensions
        )

        prompt = _SPEC_UPDATE_PROMPT_TEMPLATE.format(
            spec_name=spec_name,
            spec_content=spec_content,
            extensions_description=extensions_desc,
        )

        try:
            updated_content = llm_caller.call(prompt=prompt, json_mode="off")
            updated_content = updated_content.strip()

            if not updated_content:
                logger.warning("LLM returned empty content for spec '%s' update", spec_name)
                return 0

            if len(updated_content) < len(spec_content) * 0.5:
                logger.warning(
                    "LLM returned suspiciously short content for spec '%s' "
                    "(%d chars vs original %d chars), skipping update",
                    spec_name, len(updated_content), len(spec_content),
                )
                return 0

            Path(spec_path).write_text(updated_content, encoding="utf-8")
            if spec_name in self._specs:
                self._specs[spec_name]["content"] = updated_content
            logger.info("Updated spec '%s' with %d extensions", spec_name, len(extensions))
            return 1

        except Exception as e:
            logger.error("Failed to update spec '%s': %s", spec_name, e)
            return 0

    def _manage_issue_lifecycle(self, current_gap_titles: set[str]) -> int:
        """Auto-close sync issues whose gaps have disappeared.

        Compares existing sync-tagged open issues against current analysis
        results. If a sync issue's subject is no longer in the gap list,
        it means the gap has been resolved — close it.

        Returns:
            Number of issues closed.
        """
        from .issue_manager import IssueManager

        if not self._sync_issues:
            return 0

        mgr = IssueManager(self.project_root)
        closed = 0

        for issue in self._sync_issues:
            if issue.title in current_gap_titles:
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
                conflicts.append(Conflict(
                    spec_name=diff.spec_name,
                    description=diff.description,
                    code_location=diff.code_location,
                    confidence=diff.confidence,
                ))

        return conflicts

    def _generate_call_file(self, conflicts: List[Conflict]) -> Path:
        """Generate a single MCP call file for all pending conflicts.

        Creates a JSON file in se3/calls/ with all conflicts listed,
        awaiting human decision on each.

        Returns:
            Path to the generated call file.
        """
        calls_dir = self.project_root / "se3" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)

        timestamp = int(datetime.now().timestamp())
        unique_id = uuid.uuid4().hex[:8]
        call_file = calls_dir / f"sync_conflicts_{timestamp}_{unique_id}.json"

        call_data = {
            "type": "sync_conflicts",
            "mode": self.mode,
            "timestamp": timestamp,
            "conflicts": [
                {
                    "id": i + 1,
                    "spec_name": c.spec_name,
                    "description": c.description,
                    "code_location": c.code_location,
                    "options": ["update_spec", "create_issue"],
                    "decision": "pending",
                }
                for i, c in enumerate(conflicts)
            ],
        }

        call_file.write_text(
            json.dumps(call_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        logger.info("Generated sync call file: %s", call_file)
        return call_file

    def _resolve_conflict_via_llm(
        self, conflict: Conflict, llm_caller: Any
    ) -> str:
        """Call LLM to decide how to resolve a conflict.

        Returns:
            'update_spec' or 'create_issue'.
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

        try:
            response = llm_caller.call(prompt=prompt, json_mode="extract")
            data = json.loads(response)
            decision = data.get("decision", "create_issue")
            if decision in ("update_spec", "create_issue"):
                return decision
            logger.warning("Unknown LLM decision '%s', defaulting to create_issue", decision)
            return "create_issue"
        except Exception as e:
            logger.error("LLM conflict resolution failed for '%s': %s", conflict.spec_name, e)
            return "create_issue"

    def _apply_conflict_spec_update(
        self, conflict: Conflict, llm_caller: Any
    ) -> bool:
        """Use LLM to update a spec file based on a conflict resolution.

        Returns:
            True if the spec was updated successfully.
        """
        spec_info = self._specs.get(conflict.spec_name)
        if not spec_info:
            logger.warning("Spec '%s' not found for conflict update", conflict.spec_name)
            return False

        prompt = _CONFLICT_SPEC_UPDATE_PROMPT.format(
            spec_name=conflict.spec_name,
            description=conflict.description,
            code_location=conflict.code_location or "(not specified)",
            spec_content=spec_info["content"],
        )

        try:
            updated_content = llm_caller.call(prompt=prompt, json_mode="off")
            updated_content = updated_content.strip()

            if not updated_content:
                logger.warning("LLM returned empty content for conflict spec update '%s'", conflict.spec_name)
                return False

            Path(spec_info["path"]).write_text(updated_content, encoding="utf-8")
            spec_info["content"] = updated_content
            logger.info("Updated spec '%s' for conflict resolution", conflict.spec_name)
            return True
        except Exception as e:
            logger.error("Failed to update spec '%s' for conflict: %s", conflict.spec_name, e)
            return False

    def _apply_conflict_create_issue(self, conflict: Conflict) -> bool:
        """Create an issue for a conflict.

        Returns:
            True if the issue was created successfully.
        """
        from .issue_manager import IssueManager

        mgr = IssueManager(self.project_root)
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
            Dict with specs_updated and issues_created counts.
        """
        specs_updated = 0
        issues_created = 0

        for conflict in conflicts:
            decision = self._resolve_conflict_via_llm(conflict, llm_caller)

            if decision == "update_spec":
                if self._apply_conflict_spec_update(conflict, llm_caller):
                    specs_updated += 1
            else:
                if self._apply_conflict_create_issue(conflict):
                    issues_created += 1

        return {"specs_updated": specs_updated, "issues_created": issues_created}

    def _handle_conflicts_strict(
        self, conflicts: List[Conflict]
    ) -> Dict[str, Any]:
        """Strict mode: all conflicts collected into one MCP call file.

        Returns:
            Dict with conflicts list and optional call_file path.
        """
        if not conflicts:
            return {"conflicts": [], "call_file": None}

        call_path = self._generate_call_file(conflicts)
        return {"conflicts": conflicts, "call_file": str(call_path)}

    def _handle_conflicts_default(
        self, conflicts: List[Conflict], llm_caller: Any
    ) -> Dict[str, Any]:
        """Default mode: LLM auto-handles high-confidence, collects low-confidence.

        High-confidence conflicts are resolved automatically by LLM.
        Low-confidence conflicts are batched into an MCP call file for
        human decision.

        Returns:
            Dict with specs_updated, issues_created, unresolved list, and call_file.
        """
        specs_updated = 0
        issues_created = 0
        unresolved: List[Conflict] = []

        for conflict in conflicts:
            if conflict.confidence.lower() == "high":
                decision = self._resolve_conflict_via_llm(conflict, llm_caller)
                if decision == "update_spec":
                    if self._apply_conflict_spec_update(conflict, llm_caller):
                        specs_updated += 1
                else:
                    if self._apply_conflict_create_issue(conflict):
                        issues_created += 1
            else:
                unresolved.append(conflict)

        call_file: Optional[str] = None
        if unresolved:
            call_path = self._generate_call_file(unresolved)
            call_file = str(call_path)

        return {
            "specs_updated": specs_updated,
            "issues_created": issues_created,
            "unresolved": unresolved,
            "call_file": call_file,
        }

    def process_call_response(
        self, call_file_path: Path, llm_caller: Any = None
    ) -> Dict[str, Any]:
        """Process an MCP call response file for sync conflicts.

        Reads the response file, parses each conflict's decision, and
        executes the corresponding action (update spec or create issue).

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
            llm_caller = LLMCaller(project_root=self.project_root)

        call_data: Dict[str, Any] = {}
        try:
            call_data = json.loads(Path(call_file_path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to read call file '%s': %s", call_file_path, e)
            return {"specs_updated": 0, "issues_created": 0}

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
