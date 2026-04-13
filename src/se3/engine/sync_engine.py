"""SE3 Sync Engine — Data models and orchestration for spec-code synchronization.

Defines the core data structures for sync analysis results and provides
the SyncEngine class that orchestrates the full sync workflow.
"""

from __future__ import annotations

import json
import logging
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "diff_type": self.diff_type.value,
            "spec_name": self.spec_name,
            "description": self.description,
            "code_location": self.code_location,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SpecDiff:
        return cls(
            diff_type=DiffType(data["diff_type"]),
            spec_name=data["spec_name"],
            description=data["description"],
            code_location=data.get("code_location", ""),
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
    decision: ConflictDecision = ConflictDecision.PENDING

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spec_name": self.spec_name,
            "description": self.description,
            "spec_content": self.spec_content,
            "code_content": self.code_content,
            "code_location": self.code_location,
            "decision": self.decision.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Conflict:
        return cls(
            spec_name=data["spec_name"],
            description=data["description"],
            spec_content=data.get("spec_content", ""),
            code_content=data.get("code_content", ""),
            code_location=data.get("code_location", ""),
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

    def run(self) -> SyncResult:
        """Execute the full sync workflow.

        1. Load specs (generate base if missing)
        2. Load existing issues
        3. Analyze each spec
        4. Process results by diff type
        5. Manage issue lifecycle
        6. Generate call file for conflicts (if needed)

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

        for spec_name, spec_info in specs.items():
            analysis = analyzer.analyze_spec(
                spec_name, spec_info["content"], project_context
            )
            result.analyses.append(analysis)

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

        conflicts = self._collect_conflicts(result.analyses)
        result.conflicts = conflicts

        if conflicts and self.mode != "fast":
            call_path = self._generate_call_file(conflicts)
            result.call_file = str(call_path)

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

            Path(spec_path).write_text(updated_content, encoding="utf-8")
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
            except ValueError as e:
                logger.warning("Failed to close issue %s: %s", issue.id, e)

        return closed

    def _collect_conflicts(self, analyses: List[SpecAnalysis]) -> List[Conflict]:
        """Collect conflicts from all analyses based on mode.

        In 'fast' mode, no conflicts are collected (LLM handles all).
        In 'default' mode, only LLM-uncertain conflicts are collected.
        In 'strict' mode, all conflicts are collected.
        """
        conflicts: List[Conflict] = []

        for analysis in analyses:
            for diff in analysis.conflicts:
                if self.mode == "fast":
                    continue

                conflicts.append(Conflict(
                    spec_name=diff.spec_name,
                    description=diff.description,
                    code_location=diff.code_location,
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
        call_file = calls_dir / f"sync_conflicts_{timestamp}.json"

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
