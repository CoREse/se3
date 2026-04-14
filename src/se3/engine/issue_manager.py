"""SE3 Issue Manager — Manage project issues with YAML-based storage.

Provides IssueManager for creating, loading, listing, and updating issues
stored as YAML files in se3/issues/open/ and se3/issues/closed/ directories.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Recommended issue types — not enforced, free-form strings allowed
KNOWN_TYPES = ["bug", "feature", "enhancement", "idea", "task"]


class IssueStatus(Enum):
    """Status of an issue."""

    OPEN = "open"
    IN_PROGRESS = "in-progress"
    RESOLVED = "resolved"
    WONT_FIX = "won't-fix"
    CLOSED = "closed"


@dataclass
class Issue:
    """A single issue record."""

    id: str
    title: str
    description: str
    status: IssueStatus = IssueStatus.OPEN
    priority: str = "medium"
    scope: str = "in_scope"
    type: str = "bug"
    tags: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize issue to dictionary for YAML output."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "priority": self.priority,
            "scope": self.scope,
            "type": self.type,
            "tags": self.tags,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Issue:
        """Deserialize issue from dictionary."""
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif isinstance(created_at, datetime):
            pass
        else:
            created_at = datetime.now()

        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)
        elif isinstance(updated_at, datetime):
            pass
        else:
            updated_at = datetime.now()

        return cls(
            id=str(data["id"]),
            title=data["title"],
            description=data.get("description", ""),
            status=IssueStatus(data.get("status", "open")),
            priority=data.get("priority", "medium"),
            scope=data.get("scope", "in_scope"),
            type=data.get("type", "bug"),
            tags=data.get("tags", []),
            created_at=created_at,
            updated_at=updated_at,
        )


def _make_slug(title: str) -> str:
    """Generate a URL-friendly slug from a title.

    Takes first 30 chars, replaces spaces/non-alphanum with hyphens, lowercases.
    """
    slug = title[:30].strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "untitled"


# Valid state transitions
_VALID_TRANSITIONS: Dict[IssueStatus, List[IssueStatus]] = {
    IssueStatus.OPEN: [IssueStatus.IN_PROGRESS, IssueStatus.WONT_FIX, IssueStatus.CLOSED],
    IssueStatus.IN_PROGRESS: [IssueStatus.OPEN, IssueStatus.RESOLVED, IssueStatus.WONT_FIX],
    IssueStatus.RESOLVED: [IssueStatus.CLOSED, IssueStatus.OPEN],
    IssueStatus.WONT_FIX: [IssueStatus.OPEN, IssueStatus.CLOSED],
    IssueStatus.CLOSED: [IssueStatus.OPEN],
}

# Statuses that belong in the closed/ directory
_CLOSED_DIR_STATUSES = {IssueStatus.RESOLVED, IssueStatus.WONT_FIX, IssueStatus.CLOSED}


class IssueManager:
    """Manages issue lifecycle: create, load, list, update status, file movement."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.issues_dir = project_root / "se3" / "issues"
        self.open_dir = self.issues_dir / "open"
        self.closed_dir = self.issues_dir / "closed"

    def _ensure_dirs(self) -> None:
        """Create issue directories if they don't exist."""
        self.open_dir.mkdir(parents=True, exist_ok=True)
        self.closed_dir.mkdir(parents=True, exist_ok=True)

    def _next_id(self) -> str:
        """Return the next sequential issue ID (zero-padded 3 digits).

        Uses a counter file (se3/issues/.next_id) for monotonic IDs.
        Falls back to scanning existing files if the counter file doesn't
        exist yet (first run or migration).
        """
        counter_file = self.issues_dir / ".next_id"

        # Read current counter
        next_val = None
        if counter_file.exists():
            try:
                next_val = int(counter_file.read_text().strip())
            except (ValueError, OSError):
                pass

        if next_val is None:
            # Bootstrap: scan existing files to find the max
            max_id = 0
            for directory in [self.open_dir, self.closed_dir]:
                if not directory.exists():
                    continue
                for f in directory.glob("*.yaml"):
                    match = re.match(r"^(\d+)_", f.name)
                    if match:
                        num = int(match.group(1))
                        if num > max_id:
                            max_id = num
            next_val = max_id + 1

        # Atomically write the incremented counter
        self._ensure_dirs()
        counter_file.write_text(str(next_val + 1))

        return f"{next_val:03d}"

    def _find_issue_file(self, issue_id: str) -> Optional[Path]:
        """Find an issue file by ID across open/ and closed/ directories."""
        # Normalize: strip leading zeros for matching, but also try exact
        issue_id_stripped = issue_id.lstrip("0") or "0"
        for directory in [self.open_dir, self.closed_dir]:
            if not directory.exists():
                continue
            for f in directory.glob("*.yaml"):
                match = re.match(r"^(\d+)_", f.name)
                if match:
                    file_id = match.group(1)
                    file_id_stripped = file_id.lstrip("0") or "0"
                    if file_id == issue_id or file_id_stripped == issue_id_stripped:
                        return f
        return None

    def create(
        self,
        title: str,
        description: str,
        priority: str = "medium",
        scope: str = "in_scope",
        tags: Optional[List[str]] = None,
        type: str = "bug",
    ) -> Issue:
        """Create a new issue, write YAML to open/ directory."""
        self._ensure_dirs()

        issue_id = self._next_id()
        slug = _make_slug(title)
        now = datetime.now()

        issue = Issue(
            id=issue_id,
            title=title,
            description=description,
            status=IssueStatus.OPEN,
            priority=priority,
            scope=scope,
            type=type,
            tags=tags or [],
            created_at=now,
            updated_at=now,
        )

        filename = f"{issue_id}_{slug}.yaml"
        filepath = self.open_dir / filename
        self._write_issue(filepath, issue)

        logger.info(f"Created issue {issue_id}: {title}")
        return issue

    def load(self, issue_id: str) -> Optional[Issue]:
        """Load an issue by ID from open/ or closed/ directory."""
        filepath = self._find_issue_file(issue_id)
        if not filepath:
            return None
        return self._read_issue(filepath)

    def list_issues(
        self, include_closed: bool = False, type_filter: Optional[str] = None
    ) -> List[Issue]:
        """List issues. By default only open/, with include_closed=True also closed/.

        Args:
            include_closed: Include closed/resolved/won't-fix issues.
            type_filter: If provided, only return issues matching this type.
        """
        issues = []
        dirs = [self.open_dir]
        if include_closed:
            dirs.append(self.closed_dir)

        for directory in dirs:
            if not directory.exists():
                continue
            for f in sorted(directory.glob("*.yaml")):
                issue = self._read_issue(f)
                if issue:
                    if type_filter and issue.type != type_filter:
                        continue
                    issues.append(issue)

        # Sort by ID
        issues.sort(key=lambda i: i.id)
        return issues

    def update_status(self, issue_id: str, new_status: IssueStatus) -> Issue:
        """Update issue status and move file between directories as needed.

        Raises:
            ValueError: If the issue is not found or the transition is invalid.
        """
        filepath = self._find_issue_file(issue_id)
        if not filepath:
            raise ValueError(f"Issue '{issue_id}' not found")

        issue = self._read_issue(filepath)
        if not issue:
            raise ValueError(f"Issue '{issue_id}' could not be read")

        # Validate transition
        valid = _VALID_TRANSITIONS.get(issue.status, [])
        if new_status not in valid:
            raise ValueError(
                f"Invalid status transition: {issue.status.value} -> {new_status.value}. "
                f"Valid transitions: {[s.value for s in valid]}"
            )

        # Update fields
        issue.status = new_status
        issue.updated_at = datetime.now()

        # Write updated YAML first (status correctness > file location)
        self._write_issue(filepath, issue)

        # Move file if needed
        target_dir = self.closed_dir if new_status in _CLOSED_DIR_STATUSES else self.open_dir
        if filepath.parent != target_dir:
            target_path = target_dir / filepath.name
            try:
                self._ensure_dirs()
                shutil.move(str(filepath), str(target_path))
                logger.info(f"Moved issue {issue_id} to {target_dir.name}/")
            except OSError as e:
                logger.warning(f"Failed to move issue file {filepath} -> {target_path}: {e}")

        return issue

    def reset_to_open(self, issue_id: str) -> Issue:
        """Reset an in-progress issue back to open.

        Raises:
            ValueError: If the issue is not found or not in-progress.
        """
        filepath = self._find_issue_file(issue_id)
        if not filepath:
            raise ValueError(f"Issue '{issue_id}' not found")

        issue = self._read_issue(filepath)
        if not issue:
            raise ValueError(f"Issue '{issue_id}' could not be read")

        if issue.status != IssueStatus.IN_PROGRESS:
            raise ValueError(
                f"Can only reset in-progress issues. Issue '{issue_id}' is '{issue.status.value}'"
            )

        return self.update_status(issue_id, IssueStatus.OPEN)

    def find_open_by_title(self, title: str) -> Optional[Issue]:
        """Find an open issue whose title exactly matches (case-insensitive).

        Args:
            title: Exact title to match against open issue titles.
        """
        title_lower = title.lower()
        if not title_lower or not self.open_dir.exists():
            return None
        for f in sorted(self.open_dir.glob("*.yaml")):
            issue = self._read_issue(f)
            if issue and title_lower == issue.title.lower():
                return issue
        return None

    def close_issue(self, issue_id: str, reason: str = "") -> Issue:
        """Close an open issue, moving it to the closed/ directory.

        Args:
            issue_id: ID of the issue to close.
            reason: Optional reason for closing.

        Raises:
            ValueError: If the issue is not found or cannot be closed.
        """
        filepath = self._find_issue_file(issue_id)
        if not filepath:
            raise ValueError(f"Issue '{issue_id}' not found")

        issue = self._read_issue(filepath)
        if not issue:
            raise ValueError(f"Issue '{issue_id}' could not be read")

        if issue.status in _CLOSED_DIR_STATUSES:
            return issue

        target_status = IssueStatus.CLOSED
        valid = _VALID_TRANSITIONS.get(issue.status, [])
        if target_status not in valid:
            if IssueStatus.RESOLVED in valid:
                target_status = IssueStatus.RESOLVED
            else:
                raise ValueError(
                    f"Cannot close issue '{issue_id}' with status '{issue.status.value}'. "
                    f"Valid transitions: {[s.value for s in valid]}"
                )

        issue.status = target_status
        issue.updated_at = datetime.now()

        self._write_issue(filepath, issue)

        target_dir = self.closed_dir
        if filepath.parent != target_dir:
            target_path = target_dir / filepath.name
            try:
                self._ensure_dirs()
                shutil.move(str(filepath), str(target_path))
                logger.info("Closed issue %s: %s", issue_id, reason)
            except OSError as e:
                logger.warning("Failed to move issue file %s -> %s: %s", filepath, target_path, e)

        return issue

    def list_by_tags(self, tags: List[str], include_closed: bool = False) -> List[Issue]:
        """List issues that contain all specified tags.

        Args:
            tags: Tags to filter by. An issue must have all of these tags.
            include_closed: Whether to include closed issues.
        """
        if not tags:
            return self.list_issues(include_closed=include_closed)

        tags_set = set(tags)
        result = []
        dirs = [self.open_dir]
        if include_closed:
            dirs.append(self.closed_dir)

        for directory in dirs:
            if not directory.exists():
                continue
            for f in sorted(directory.glob("*.yaml")):
                issue = self._read_issue(f)
                if issue and tags_set.issubset(set(issue.tags)):
                    result.append(issue)

        result.sort(key=lambda i: i.id)
        return result

    def _read_issue(self, filepath: Path) -> Optional[Issue]:
        """Read and parse an issue YAML file."""
        try:
            content = filepath.read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            if not data or not isinstance(data, dict):
                return None
            return Issue.from_dict(data)
        except (yaml.YAMLError, KeyError, ValueError) as e:
            logger.warning(f"Failed to read issue file {filepath}: {e}")
            return None

    def _write_issue(self, filepath: Path, issue: Issue) -> None:
        """Write issue data to a YAML file."""
        data = issue.to_dict()
        content = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
        filepath.write_text(content, encoding="utf-8")
