"""Cleanup manager for `--delete-merged` flag in `se3 merge`.

Safely deletes merged branches and their bound worktrees. Uses `git branch -d`
(lowercase) so deletion only succeeds when the branch is fully merged. Skips
protected branches (main, master, current branch) and refuses to clean dirty
worktrees.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..worktree import (
    _cleanup_git_worktree_metadata,
    _run_git,
    exists_for_branch,
)

logger = logging.getLogger(__name__)

_PROTECTED_BRANCHES = frozenset({"main", "master"})


@dataclass
class CleanupReport:
    """Outcome of ``CleanupManager.delete_merged_branches``."""

    deleted: list[str] = field(default_factory=list)
    skipped_dirty: list[tuple[str, str]] = field(default_factory=list)
    skipped_worktree_remove_failed: list[tuple[str, str]] = field(
        default_factory=list,
    )
    skipped_protected: list[str] = field(default_factory=list)
    skipped_not_merged: list[tuple[str, str]] = field(default_factory=list)
    skipped_unknown_state: list[tuple[str, str]] = field(default_factory=list)


def _get_worktree_path_for_branch(project_root: Path, branch: str) -> Optional[Path]:
    """Return the filesystem path of the worktree bound to *branch*, if any.

    Parses ``git worktree list --porcelain`` output, which looks like::

        worktree /path/to/wt
        HEAD abc123
        branch refs/heads/my-branch
        detached

    Returns ``None`` when no worktree is registered for the branch.
    """
    result = _run_git(
        project_root, "worktree", "list", "--porcelain", check=False,
        timeout=15,
    )
    if result.returncode != 0:
        return None

    lines = result.stdout.splitlines()
    current_worktree: Optional[str] = None
    current_branch: Optional[str] = None

    for line in lines:
        if line.startswith("worktree "):
            # New block — flush previous
            if current_worktree and current_branch == branch:
                return Path(current_worktree)
            current_worktree = line[len("worktree "):]
            current_branch = None
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            if ref.startswith("refs/heads/"):
                current_branch = ref[len("refs/heads/"):]
            else:
                current_branch = ref

    # Flush last block
    if current_worktree and current_branch == branch:
        return Path(current_worktree)
    return None


def _is_worktree_clean(wt_path: Path) -> bool:
    """Return True when the worktree at *wt_path* has no uncommitted changes.

    Unlike ``merge_cmd._is_working_tree_clean``, untracked files ARE counted
    as dirty here because ``git worktree remove`` (without ``--force``)
    refuses to remove worktrees that contain untracked files.

    When the directory has already been removed externally, we treat it as
    clean so that the caller can proceed to branch deletion and metadata
    cleanup.
    """
    if not wt_path.exists():
        return True
    result = subprocess.run(
        ["git", "-C", str(wt_path), "status", "--porcelain"],
        capture_output=True, text=True, check=False,
        timeout=15,
    )
    if result.returncode != 0:
        # Treat unreadable worktree as dirty to be safe
        return False
    return not result.stdout.strip()


class CleanupManager:
    """Delete merged branches and their bound worktrees after a successful merge."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def delete_merged_branches(self, branches: list[str]) -> CleanupReport:
        """Safely delete *branches* that have been merged into the current branch.

        Steps per branch (in order):

        1. Skip if branch is in ``_PROTECTED_BRANCHES`` or equals the current
           checked-out branch.
        2. If a worktree is bound to the branch, verify the worktree working
           directory is clean (no uncommitted tracked changes). If dirty,
           record a skip and do NOT delete the branch.
        3. If a worktree exists and is clean, run ``git worktree remove <path>``
           (without ``--force`` — the clean check guarantees safety).
        4. Run ``git branch -d <branch>`` (lowercase *-d*). If the branch is not
           fully merged, git rejects the deletion and we record a skip.

        Returns:
            CleanupReport summarising which branches were deleted, skipped due
            to dirty worktrees, or skipped because they are protected.
        """
        report = CleanupReport()

        current_branch_result = _run_git(
            self.project_root, "rev-parse", "--abbrev-ref", "HEAD",
            check=False, timeout=15,
        )
        if current_branch_result.returncode != 0:
            # Fail closed: cannot determine current branch → abort cleanup
            reason = current_branch_result.stderr.strip()
            logger.error(
                "Failed to determine current branch: %s. "
                "Aborting cleanup to avoid deleting the checked-out branch.",
                reason,
            )
            for branch in branches:
                report.skipped_unknown_state.append(
                    (branch, f"current branch unknown: {reason}"),
                )
            return report

        current_branch = current_branch_result.stdout.strip()

        for branch in branches:
            if branch in _PROTECTED_BRANCHES:
                logger.info("Skipping protected branch '%s'", branch)
                report.skipped_protected.append(branch)
                continue

            if branch == current_branch:
                logger.info("Skipping current branch '%s'", branch)
                report.skipped_protected.append(branch)
                continue

            # Check for bound worktree and its cleanliness
            has_wt = exists_for_branch(self.project_root, branch)
            wt_path: Optional[Path] = None
            if has_wt:
                wt_path = _get_worktree_path_for_branch(self.project_root, branch)
                if wt_path is not None:
                    if not _is_worktree_clean(wt_path):
                        reason = f"worktree {wt_path} has uncommitted changes"
                        logger.warning("Skipping branch '%s': %s", branch, reason)
                        report.skipped_dirty.append((branch, reason))
                        continue

            # Try deleting branch first (safe with lowercase -d).
            # If the branch is checked out in a worktree, git will refuse;
            # we then remove the worktree and retry.
            delete_result = _run_git(
                self.project_root, "branch", "-d", branch,
                check=False, timeout=15,
            )
            if delete_result.returncode != 0:
                stderr = delete_result.stderr.strip()
                if has_wt and wt_path is not None and (
                    "checked out" in stderr.lower() or "worktree" in stderr.lower()
                ):
                    remove_result = _run_git(
                        self.project_root,
                        "worktree", "remove", str(wt_path),
                        check=False, timeout=30,
                    )
                    if remove_result.returncode != 0:
                        reason = (
                            f"worktree removal failed: {remove_result.stderr.strip()}"
                        )
                        logger.warning("Skipping branch '%s': %s", branch, reason)
                        report.skipped_worktree_remove_failed.append(
                            (branch, reason),
                        )
                        continue
                    logger.info(
                        "Removed worktree for branch '%s': %s", branch, wt_path
                    )
                    # Retry branch deletion
                    delete_result = _run_git(
                        self.project_root, "branch", "-d", branch,
                        check=False, timeout=15,
                    )
                    if delete_result.returncode == 0:
                        report.deleted.append(branch)
                        # Scrub stale metadata just like the success-first path
                        if has_wt:
                            _cleanup_git_worktree_metadata(self.project_root, branch)
                        continue
                    reason = delete_result.stderr.strip()
                else:
                    reason = stderr
                logger.warning("Failed to delete branch '%s': %s", branch, reason)
                report.skipped_not_merged.append((branch, reason))
                continue

            # Branch deleted successfully — clean up the worktree if it still exists
            if has_wt and wt_path is not None and wt_path.exists():
                remove_result = _run_git(
                    self.project_root,
                    "worktree", "remove", str(wt_path),
                    check=False, timeout=30,
                )
                if remove_result.returncode != 0:
                    logger.warning(
                        "Worktree removal after branch delete failed: %s",
                        remove_result.stderr.strip(),
                    )
            # Always scrub stale .git/worktrees metadata when a bound worktree
            # existed, even if the directory was already externally removed.
            if has_wt:
                _cleanup_git_worktree_metadata(self.project_root, branch)
            logger.info("Deleted branch '%s'", branch)
            report.deleted.append(branch)

        return report
