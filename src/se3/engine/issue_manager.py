"""SE3 Issue Manager — Manage project issues with YAML-based storage.

Provides IssueManager for creating, loading, listing, and updating issues
stored as YAML files in se3/issues/open/ and se3/issues/closed/ directories.
"""

from __future__ import annotations

import fcntl
import logging
import re
import shutil
from dataclasses import dataclass, field, replace
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
    title: Optional[str] = None
    description: str = ""
    status: IssueStatus = IssueStatus.OPEN
    priority: Optional[str] = None
    type: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    source: str = "system"
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    @property
    def display_title(self) -> str:
        """Derive a human-readable title from title or description.

        Priority: explicit title -> first non-empty line of description -> "untitled".
        """
        if self.title:
            return self.title
        if self.description:
            for line in self.description.splitlines():
                stripped = line.strip()
                if stripped:
                    return stripped
        return "untitled"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize issue to dictionary for YAML output."""
        data: Dict[str, Any] = {
            "id": self.id,
            "description": self.description,
            "status": self.status.value,
            "tags": self.tags,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        # Include optional fields only when set (preserves round-trip fidelity)
        if self.title is not None:
            data["title"] = self.title
        if self.priority is not None:
            data["priority"] = self.priority
        if self.type is not None:
            data["type"] = self.type
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Issue:
        """Deserialize issue from dictionary.

        Missing ``source`` defaults to ``"system"`` (backward compat with
        pre-source YAML files).  ``title``, ``priority`` and ``type`` are
        optional — absent means the user/programmer intentionally left them
        blank.  A legacy ``scope`` key (written before the field was retired)
        is tolerated and silently ignored.
        """
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at)
            except ValueError:
                created_at = datetime.now()
        elif isinstance(created_at, datetime):
            pass
        else:
            created_at = datetime.now()

        updated_at = data.get("updated_at")
        if isinstance(updated_at, str):
            try:
                updated_at = datetime.fromisoformat(updated_at)
            except ValueError:
                updated_at = datetime.now()
        elif isinstance(updated_at, datetime):
            pass
        else:
            updated_at = datetime.now()

        # title, priority, type are optional — None means "not specified"
        raw_title = data.get("title")
        title = str(raw_title) if raw_title is not None else None

        raw_priority = data.get("priority")
        priority = str(raw_priority) if raw_priority is not None else None

        raw_type = data.get("type")
        issue_type = str(raw_type) if raw_type is not None else None

        # description degrades gracefully for legacy data — missing or empty
        # defaults to "" so the issue remains listable, viewable, and editable.
        # Write paths (create / update_fields) enforce non-empty description.
        desc = data.get("description", "")
        if not desc or not str(desc).strip():
            desc = ""

        return cls(
            id=str(data["id"]),
            title=title,
            description=str(desc) if desc else "",
            status=IssueStatus(data.get("status", "open")),
            priority=priority,
            type=issue_type,
            tags=data.get("tags", []),
            source=data.get("source", "system"),
            created_at=created_at,
            updated_at=updated_at,
        )


def _make_slug(title: str) -> str:
    """Generate a URL-friendly slug from a title.

    Takes first 30 chars, replaces spaces/non-alphanum with hyphens, lowercases.
    Returns ``"untitled"`` when the resulting slug is empty.
    """
    slug = title[:30].strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "untitled"


def _derive_slug_for_issue(issue: Issue) -> str:
    """Derive a filesystem slug for an issue from its effective title source.

    Uses the same priority as ``Issue.display_title``: explicit ``title``
    first, then the first non-empty line of ``description``, then
    ``"untitled"``.
    """
    return _make_slug(issue.display_title)


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

        The read-modify-write on the counter file is serialized via
        ``fcntl.flock(LOCK_EX)`` so concurrent creators (CLI, webui,
        programmatic discovery) never allocate the same ID.
        """
        self._ensure_dirs()
        counter_file = self.issues_dir / ".next_id"

        # Open (or create) the counter file and acquire an exclusive lock
        # before reading/incrementing.  ``a+`` mode creates the file if it
        # does not exist and allows reading after seeking to 0.
        with open(counter_file, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                raw = fh.read().strip()

                next_val = None
                if raw:
                    try:
                        next_val = int(raw)
                    except ValueError:
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

                # Write the incremented counter (overwrite the file)
                fh.seek(0)
                fh.truncate()
                fh.write(str(next_val + 1))
                fh.flush()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

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
        description: str,
        title: Optional[str] = None,
        priority: Optional[str] = None,
        tags: Optional[List[str]] = None,
        type: Optional[str] = None,
        source: str = "system",
    ) -> Issue:
        """Create a new issue, write YAML to open/ directory.

        Only *description* is required.  ``title``, ``priority`` and ``type``
        are optional — when omitted the display title is derived from the
        description's first non-empty line.

        Args:
            description: The issue body (**required**, must be non-empty).
            title: Optional explicit title.
            priority: Optional priority (e.g. ``"high"``).
            tags: Tag list.
            type: Optional issue type (e.g. ``"bug"``).
            source: Origin of the issue — ``"human"`` for CLI/webui,
                ``"system"`` for programmatic discovery (default).

        Raises:
            ValueError: If *description* is empty or whitespace-only.
        """
        if not description or not description.strip():
            raise ValueError("Issue description must not be empty")

        self._ensure_dirs()

        issue_id = self._next_id()
        now = datetime.now()

        issue = Issue(
            id=issue_id,
            title=title if title and title.strip() else None,
            description=description,
            status=IssueStatus.OPEN,
            priority=priority,
            type=type,
            tags=tags or [],
            source=source,
            created_at=now,
            updated_at=now,
        )

        slug = _derive_slug_for_issue(issue)
        filename = f"{issue_id}_{slug}.yaml"
        filepath = self.open_dir / filename
        self._write_issue(filepath, issue)

        logger.info("Created issue %s: %s", issue_id, issue.display_title)
        return issue

    def adopt_issue(self, issue: Issue) -> Issue:
        """Adopt an externally-originated *issue* under a freshly-allocated ID.

        Unlike :meth:`create`, this preserves every field of *issue* except the
        ``id`` — status, timestamps, source, tags, priority, type — and writes
        the YAML file into ``open/`` or ``closed/`` according to the issue's
        status. It is used by ``se3 merge`` runtime-sync to fold a worktree-
        created issue back into the main project without colliding with the
        main project's existing IDs.

        The new ID is allocated via :meth:`_next_id`, whose read-modify-write on
        ``se3/issues/.next_id`` is serialized by ``fcntl.flock`` so concurrent
        allocators never collide. The original ``issue.id`` is discarded.

        Args:
            issue: The source issue (e.g. loaded from a worktree's
                ``se3/issues/``). Its ``description`` must be non-empty.

        Returns:
            The adopted :class:`Issue` with its new ID.

        Raises:
            ValueError: If *issue* has an empty/whitespace-only description.
        """
        if not issue.description or not issue.description.strip():
            raise ValueError("Cannot adopt an issue with an empty description")

        self._ensure_dirs()

        new_id = self._next_id()
        adopted = replace(issue, id=new_id)

        target_dir = (
            self.closed_dir
            if adopted.status in _CLOSED_DIR_STATUSES
            else self.open_dir
        )
        slug = _derive_slug_for_issue(adopted)
        filepath = target_dir / f"{new_id}_{slug}.yaml"
        self._write_issue(filepath, adopted)

        logger.info(
            "Adopted issue as %s (was %s): %s",
            new_id, issue.id, adopted.display_title,
        )
        return adopted

    def load(self, issue_id: str) -> Optional[Issue]:
        """Load an issue by ID from open/ or closed/ directory."""
        filepath = self._find_issue_file(issue_id)
        if not filepath:
            return None
        return self._read_issue(filepath)

    def list_issues(
        self,
        include_closed: bool = False,
        type_filter: Optional[str] = None,
        source_filter: Optional[str] = None,
    ) -> List[Issue]:
        """List issues. By default only open/, with include_closed=True also closed/.

        Args:
            include_closed: Include closed/resolved/won't-fix issues.
            type_filter: If provided, only return issues matching this type.
            source_filter: If provided (``"human"`` or ``"system"``), only
                return issues whose ``source`` field matches.
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
                    if source_filter and issue.source != source_filter:
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

        Issues with ``title=None`` are compared against ``display_title``.

        Args:
            title: Exact title to match against open issue titles.
        """
        title_lower = title.lower()
        if not title_lower or not self.open_dir.exists():
            return None
        for f in sorted(self.open_dir.glob("*.yaml")):
            issue = self._read_issue(f)
            if issue and title_lower == issue.display_title.lower():
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
                raise

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

    def update_fields(
        self,
        issue_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[str] = None,
        type: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Issue:
        """Update editable fields on an issue, renaming the YAML file when the
        derived slug changes.

        Only the fields that are explicitly passed (non-``None``) are updated;
        omitted fields retain their current value.  Pass an empty string to
        *clear* a field back to its default (``None`` for title/priority/type).

        Args:
            issue_id: Target issue ID.
            title: New title (empty string clears to ``None``).
            description: New description body.
            priority: New priority (empty string clears to ``None``).
            type: New issue type (empty string clears to ``None``).
            tags: New tag list.

        Returns:
            The updated :class:`Issue`.

        Raises:
            ValueError: If the issue is not found.
        """
        filepath = self._find_issue_file(issue_id)
        if not filepath:
            raise ValueError(f"Issue '{issue_id}' not found")

        issue = self._read_issue(filepath)
        if not issue:
            raise ValueError(f"Issue '{issue_id}' could not be read")

        # Use the canonical stored ID (e.g. "001") rather than the caller-supplied
        # potentially unpadded ID (e.g. "1") so the filename preserves zero-padding.
        canonical_id = issue.id

        # Extract the actual slug from the filename on disk (e.g. "001_untitled.yaml" → "untitled")
        actual_slug_on_disk = filepath.stem[len(canonical_id) + 1:] if filepath.stem.startswith(canonical_id + "_") else filepath.stem

        # Apply field changes — empty string means "clear to None"
        if title is not None:
            issue.title = title.strip() or None
        if description is not None:
            if not description.strip():
                raise ValueError("Issue description must not be empty")
            issue.description = description
        if priority is not None:
            issue.priority = priority.strip() or None
        if type is not None:
            issue.type = type.strip() or None
        if tags is not None:
            issue.tags = tags

        issue.updated_at = datetime.now()

        canonical_slug = _derive_slug_for_issue(issue)
        new_filename = f"{canonical_id}_{canonical_slug}.yaml"

        # Write to the canonical path (may be the same file or a rename)
        if canonical_slug != actual_slug_on_disk:
            target_path = filepath.parent / new_filename
            # Remove any stale file with the new slug that might linger
            if target_path != filepath and target_path.exists():
                target_path.unlink()
                logger.debug("Removed stale issue file %s before rename", target_path)
            self._write_issue(target_path, issue)
            if target_path != filepath:
                filepath.unlink()
                logger.info(
                    "Renamed issue %s file: %s -> %s",
                    canonical_id,
                    filepath.name,
                    new_filename,
                )
        else:
            self._write_issue(filepath, issue)

        return issue

    def reopen_issue(self, issue_id: str) -> Issue:
        """Reopen a closed issue back to OPEN, moving it to the open/ directory.

        This is a convenience wrapper that transitions from RESOLVED, WONT_FIX
        or CLOSED back to OPEN.

        Raises:
            ValueError: If the issue is not found or cannot be reopened.
        """
        filepath = self._find_issue_file(issue_id)
        if not filepath:
            raise ValueError(f"Issue '{issue_id}' not found")

        issue = self._read_issue(filepath)
        if not issue:
            raise ValueError(f"Issue '{issue_id}' could not be read")

        if issue.status == IssueStatus.OPEN:
            return issue  # already open, idempotent

        if issue.status not in _CLOSED_DIR_STATUSES:
            raise ValueError(
                f"Cannot reopen issue '{issue_id}' with status '{issue.status.value}'. "
                f"Only resolved, won't-fix, or closed issues can be reopened."
            )

        return self.update_status(issue_id, IssueStatus.OPEN)

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
