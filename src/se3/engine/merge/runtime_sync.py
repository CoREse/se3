"""Runtime content synchronization for se3 merge.

After a successful git merge, git-ignored runtime data under ``se3/`` is not
automatically merged. This module copies tier A runtime content from the
source branch's bound worktree into the current branch's ``se3/`` directory.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .cleanup import _get_worktree_path_for_branch

logger = logging.getLogger(__name__)

# Tier A: directories to recursively scan (relative to se3/)
TIER_A_DIRS = [
    "history",
    "logs",
    "state/archive",
    "collab/tasks",
]

# Tier A: glob patterns (relative to se3/)
TIER_A_GLOBS = [
    "state/summary-*",
    "calls/confirm_*",
]

# Tier B: specific files to discard from source (relative to se3/)
TIER_B_FILES = [
    "state/engine.json",
    "state/known_test_failures.json",
]

# Tier B: directories to discard from source (relative to se3/)
TIER_B_DIRS = [
    "calls/active",
]

# Tier C: directories completely skipped (relative to se3/)
TIER_C_DIRS = [
    "cache",
    "tmp",
    "worktrees",
]


class RuntimeSyncCollision(RuntimeError):
    """Raised when a tier A file exists in both source and target at the same relative path."""

    def __init__(self, rel_path: str) -> None:
        super().__init__(f"Runtime sync collision: {rel_path}")
        self.rel_path = rel_path


@dataclass
class SyncReport:
    """Outcome of ``sync_branch_runtime``."""

    copied: list[str] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)
    skipped: bool = False


def _collect_files_under(path: Path) -> list[Path]:
    """Return all files recursively under *path*, or empty list if path does not exist."""
    if not path.exists():
        return []
    return [p for p in path.rglob("*") if p.is_file()]


def _collect_glob_files(base: Path, pattern: str) -> list[Path]:
    """Return files matching glob *pattern* relative to *base*."""
    if not base.exists():
        return []
    return [p for p in base.glob(pattern) if p.is_file()]


def _rel_path_str(path: Path, base: Path) -> str:
    """Return POSIX-style relative path string of *path* from *base*."""
    return str(path.relative_to(base)).replace("\\", "/")


def sync_branch_runtime(project_root: Path, branch: str) -> SyncReport:
    """Sync runtime content from *branch*'s bound worktree into current branch's se3/.

    Tier A files (``history/``, ``logs/``, ``state/summary-*``,
    ``state/archive/``, ``calls/confirm_*``, ``collab/tasks/``) are copied
    from the source worktree's ``se3/`` to the current branch's ``se3/`` if
    the target does not already have a file at the same relative path. If a
    collision is detected, ``RuntimeSyncCollision`` is raised.

    Tier B files (``state/engine.json``, ``state/known_test_failures.json``,
    ``calls/active/``) are recorded as discarded but not copied.

    Tier C directories (``cache/``, ``tmp/``, ``worktrees/``) are completely
    ignored.

    Args:
        project_root: Root of the project (current branch).
        branch: Branch name whose bound worktree is the source.

    Returns:
        SyncReport describing what was copied and discarded. When the source
        worktree does not exist, returns ``SyncReport(skipped=True)``.

    Raises:
        RuntimeSyncCollision: When a tier A file exists at the same
            relative path in both source and target.
    """
    source_wt = _get_worktree_path_for_branch(project_root, branch)
    if source_wt is None:
        logger.warning(
            "No bound worktree for branch '%s', skipping runtime sync", branch
        )
        return SyncReport(skipped=True)

    source_se3 = source_wt / "se3"
    target_se3 = project_root / "se3"

    report = SyncReport()

    # --- Tier A: copy non-colliding files ---
    for dir_name in TIER_A_DIRS:
        source_dir = source_se3 / dir_name
        for src_file in _collect_files_under(source_dir):
            rel_str = _rel_path_str(src_file, source_se3)
            dest_file = target_se3 / rel_str
            if dest_file.exists():
                raise RuntimeSyncCollision(rel_str)
            report.copied.append(rel_str)
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)

    for glob_pattern in TIER_A_GLOBS:
        for src_file in _collect_glob_files(source_se3, glob_pattern):
            rel_str = _rel_path_str(src_file, source_se3)
            dest_file = target_se3 / rel_str
            if dest_file.exists():
                raise RuntimeSyncCollision(rel_str)
            report.copied.append(rel_str)
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)

    # --- Tier B: record as discarded ---
    for file_name in TIER_B_FILES:
        source_file = source_se3 / file_name
        if source_file.exists() and source_file.is_file():
            report.discarded.append(file_name)

    for dir_name in TIER_B_DIRS:
        source_dir = source_se3 / dir_name
        for src_file in _collect_files_under(source_dir):
            rel_str = _rel_path_str(src_file, source_se3)
            report.discarded.append(rel_str)

    return report
