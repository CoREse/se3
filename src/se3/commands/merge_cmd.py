"""SE3 Merge command — Sequential merge of branches into current branch.

Usage:
    se3 merge <branch> [<branch> ...]
    se3 merge <branch> [<branch> ...] --strategy=default|strict|fast
    se3 merge <branch> [<branch> ...] --delete-merged
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

from ..engine.display import render_text
from ..engine.worktree import get_current_branch

logger = logging.getLogger(__name__)


def _run_git(
    project_root: Path, *args: str, check: bool = True, timeout: int = 30
) -> subprocess.CompletedProcess:
    """Run a git command in the given project root."""
    cmd = ["git", "-C", str(project_root)] + list(args)
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        stdin=subprocess.DEVNULL,
    )
    return result


def _resolve_git_dir(project_root: Path) -> Optional[Path]:
    """Resolve the actual git directory for the given working tree.

    In a linked worktree, ``<project_root>/.git`` is a regular file
    containing ``gitdir: <path>`` rather than a directory; the real
    in-progress markers (MERGE_HEAD, CHERRY_PICK_HEAD, …) live under that
    pointed-to directory. ``git rev-parse --git-dir`` reliably resolves
    this for both plain clones and worktrees. Returns ``None`` if git is
    unavailable or the directory cannot be resolved.
    """
    try:
        result = _run_git(project_root, "rev-parse", "--git-dir", check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    git_dir_str = result.stdout.strip()
    if not git_dir_str:
        return None
    git_dir = Path(git_dir_str)
    if not git_dir.is_absolute():
        git_dir = (project_root / git_dir).resolve()
    return git_dir


def _is_working_tree_clean(project_root: Path) -> bool:
    """Check if the working tree has no uncommitted tracked changes.

    Untracked files are ignored — they do not affect git merge operations.
    Also detects in-progress git states (merge, cherry-pick, revert, rebase).
    """
    git_dir = _resolve_git_dir(project_root)

    if git_dir is not None:
        # Detect in-progress git states that would interfere with merge.
        # In a linked worktree these markers live under the per-worktree
        # gitdir resolved above, not under <project_root>/.git.
        in_progress_markers = [
            git_dir / "MERGE_HEAD",
            git_dir / "CHERRY_PICK_HEAD",
            git_dir / "REVERT_HEAD",
            git_dir / "rebase-merge",
            git_dir / "rebase-apply",
        ]
        for marker in in_progress_markers:
            if marker.exists():
                return False

    result = _run_git(
        project_root, "status", "--porcelain", "--untracked-files=no", check=False
    )
    if result.returncode != 0:
        return False
    return not result.stdout.strip()


def _branch_exists(project_root: Path, branch: str) -> bool:
    """Check if a local branch exists."""
    result = _run_git(
        project_root, "show-ref", "--verify", f"refs/heads/{branch}",
        check=False,
    )
    return result.returncode == 0


def run_merge(
    branches: list[str],
    strategy: str = "default",
    delete_merged: bool = False,
    project_root: Optional[Path] = None,
) -> int:
    """Run the merge command.

    Args:
        branches: List of branch names to merge (in order).
        strategy: Conflict resolution strategy.
        delete_merged: Whether to delete merged branches afterward.
        project_root: Project root directory. Auto-detected if None.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    if project_root is None:
        from .run import get_project_root

        project_root = get_project_root()

    # Validate working tree is clean
    if not _is_working_tree_clean(project_root):
        render_text(
            "Working tree is not clean. Please commit or stash your changes before merging.",
            title="Merge Error",
        )
        return 1

    try:
        current_branch = get_current_branch(project_root)
    except RuntimeError as exc:
        render_text(
            f"Cannot merge in detached HEAD state: {exc}",
            title="Merge Error",
        )
        return 1

    # Deduplicate branch names while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for branch in branches:
        if branch not in seen:
            seen.add(branch)
            deduped.append(branch)
        else:
            logger.warning("Duplicate branch '%s' ignored", branch)
    branches = deduped

    # Validate branches
    for branch in branches:
        if branch == current_branch:
            render_text(
                f"Cannot merge the current branch ('{branch}') into itself.",
                title="Merge Error",
            )
            return 1
        if branch in ("main", "master"):
            render_text(
                f"Cannot merge '{branch}' — it is a protected base branch.",
                title="Merge Error",
            )
            return 1
        if not _branch_exists(project_root, branch):
            render_text(
                f"Branch '{branch}' does not exist.",
                title="Merge Error",
            )
            return 1

    # Run the orchestrator
    from ..engine.merge.orchestrator import MergeOrchestrator

    orchestrator = MergeOrchestrator(
        project_root=project_root,
        strategy=strategy,
        delete_merged=delete_merged,
    )
    report = orchestrator.execute(branches)

    if report.success:
        lines = [f"Successfully merged {len(report.merged_branches)} branch(es):", ""]
        for b in report.merged_branches:
            lines.append(f"  - {b}")
        if report.final_version:
            lines.append("")
            lines.append(f"Version: {report.pre_merge_version or '?'} -> {report.final_version}")
        if report.version_aggregation_error:
            lines.append("")
            lines.append(f"WARNING: Version aggregation failed: {report.version_aggregation_error}")
        if report.cleanup_report:
            cr = report.cleanup_report
            lines.append("")
            if cr.deleted:
                lines.append(f"Deleted branches: {', '.join(cr.deleted)}")
            if cr.skipped_dirty:
                lines.append("Skipped (dirty worktree):")
                for b, reason in cr.skipped_dirty:
                    lines.append(f"  - {b}: {reason}")
            if cr.skipped_protected:
                lines.append(f"Skipped (protected): {', '.join(cr.skipped_protected)}")
            if cr.skipped_unknown_state:
                lines.append("Skipped (unknown state):")
                for b, reason in cr.skipped_unknown_state:
                    lines.append(f"  - {b}: {reason}")
            if cr.skipped_worktree_remove_failed:
                lines.append("Skipped (worktree removal failed):")
                for b, reason in cr.skipped_worktree_remove_failed:
                    lines.append(f"  - {b}: {reason}")
            if cr.skipped_not_merged:
                lines.append("Skipped (not fully merged):")
                for b, reason in cr.skipped_not_merged:
                    lines.append(f"  - {b}: {reason}")
        render_text("\n".join(lines), title="Merge Complete")
        return 0
    elif report.rollback_failed:
        lines = [
            "CRITICAL: Git rollback failed after guardrail violation.",
            "",
            "The working tree is in an INCONSISTENT state. Manual intervention is required.",
            "",
            "Recovery commands:",
            "  git status          -- inspect the current state",
            "  git reflog          -- find a known-good commit to reset to",
            "  git reset --hard <known-good-sha>  -- force restore (DESTRUCTIVE)",
            "",
            f"Failed branch: {report.failed_branch}",
        ]
        if report.merged_branches:
            lines.append(f"Branches already merged: {', '.join(report.merged_branches)}")
        if report.log_file:
            lines.append(f"Log file: {report.log_file}")
        render_text("\n".join(lines), title="Merge Rollback Failed -- Repository May Be Corrupted")
        return 1
    elif report.pending_human:
        lines = ["Merge paused for human review.", ""]
        if report.merged_branches:
            lines.append(
                f"Branches already merged ({len(report.merged_branches)}):"
            )
            for b in report.merged_branches:
                lines.append(f"  - {b}")
            lines.append("")
        if report.human_call_file:
            lines.append(f"Call file: {report.human_call_file}")
        if report.log_file:
            lines.append(f"Log file: {report.log_file}")
        render_text("\n".join(lines), title="Merge Paused")
        return 130  # Interrupted by user / pending human
    else:
        title, first_line = _failure_title_and_summary(report.failure_reason)
        lines = [first_line, ""]
        if report.failed_branch:
            lines.append(f"Failed branch: {report.failed_branch}")
        if report.failure_reason and report.failure_reason not in (
            "merge_conflict",
            "guardrail_violation",
            "guardrail_repair_failed",
            "fast_abort",
            "llm_resolution_failed",
        ):
            lines.append(f"Reason: {report.failure_reason}")
        if report.merged_branches:
            lines.append(f"Branches already merged: {', '.join(report.merged_branches)}")
        if report.log_file:
            lines.append(f"Log file: {report.log_file}")
        render_text("\n".join(lines), title=title)
        return 1


def _failure_title_and_summary(
    failure_reason: Optional[str],
) -> tuple[str, str]:
    """Return (title, first_line) for a merge failure report.

    Distinguishes git merge conflicts from post-merge guardrail violations
    and fast-mode aborts so the user knows which category of failure
    occurred.
    """
    if failure_reason == "merge_conflict":
        return (
            "Merge failed",
            "Merge failed: git merge conflict (could not be resolved)",
        )
    if failure_reason == "guardrail_violation":
        return (
            "Merge failed",
            "Merge failed: post-merge guardrails violation",
        )
    if failure_reason == "guardrail_repair_failed":
        return (
            "Merge aborted",
            "Merge aborted: fast strategy could not auto-repair guardrails violation",
        )
    if failure_reason in ("fast_abort", "llm_resolution_failed"):
        return (
            "Merge aborted",
            "Merge aborted: fast strategy could not resolve conflict",
        )
    return ("Merge failed", "Merge failed.")
