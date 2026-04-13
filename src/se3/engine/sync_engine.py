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
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


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
