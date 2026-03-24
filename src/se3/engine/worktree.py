"""Git worktree lifecycle management for SE3 loop mode branch isolation.

Provides functions to create loop branches, manage git worktrees for isolated
execution, merge results back, and clean up after loop completion.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _run_git(project_root: Path, *args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a git command in the given project root.

    Args:
        project_root: Directory to run git in
        *args: Git subcommand and arguments
        check: Whether to raise on non-zero exit
        timeout: Command timeout in seconds

    Returns:
        CompletedProcess result

    Raises:
        subprocess.CalledProcessError: If check=True and command fails
        subprocess.TimeoutExpired: If command exceeds timeout
    """
    cmd = ["git", "-C", str(project_root)] + list(args)
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )
    if result.returncode != 0 and not check:
        logger.debug("Git command returned %d: %s", result.returncode, result.stderr.strip())
    return result


def get_current_branch(project_root: Path) -> str:
    """Get the current branch name.

    Args:
        project_root: Project root directory

    Returns:
        Current branch name

    Raises:
        subprocess.CalledProcessError: If not in a git repo or detached HEAD
    """
    result = _run_git(project_root, "rev-parse", "--abbrev-ref", "HEAD")
    branch = result.stdout.strip()
    if branch == "HEAD":
        raise RuntimeError("Detached HEAD state — cannot create loop branch")
    return branch


def create_loop_branch(project_root: Path, timestamp: str | None = None) -> tuple[str, str]:
    """Create a new loop branch from current HEAD.

    Args:
        project_root: Project root directory
        timestamp: Optional timestamp string; defaults to now

    Returns:
        Tuple of (loop_branch_name, original_branch_name)

    Raises:
        subprocess.CalledProcessError: If branch creation fails
    """
    original_branch = get_current_branch(project_root)

    if timestamp is None:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    branch_name = f"se3-loop/{timestamp}"

    _run_git(project_root, "branch", branch_name)
    logger.info("Created loop branch: %s (from %s)", branch_name, original_branch)

    return branch_name, original_branch


def _branch_safe_name(branch: str) -> str:
    """Convert branch name to filesystem-safe directory name."""
    return branch.replace("/", "-")


def create_worktree(project_root: Path, branch: str) -> Path:
    """Create a git worktree for the given branch.

    Args:
        project_root: Project root directory
        branch: Branch name to checkout in the worktree

    Returns:
        Path to the created worktree directory

    Raises:
        subprocess.CalledProcessError: If worktree creation fails
    """
    safe_name = _branch_safe_name(branch)
    worktree_path = project_root / "se3" / "worktrees" / safe_name

    # Ensure parent directory exists
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    _run_git(project_root, "worktree", "add", str(worktree_path), branch)
    logger.info("Created worktree at: %s (branch: %s)", worktree_path, branch)

    return worktree_path


def remove_worktree(project_root: Path, worktree_path: Path) -> None:
    """Remove a git worktree.

    Handles the case where the worktree directory has already been removed.

    Args:
        project_root: Project root directory
        worktree_path: Path to the worktree to remove
    """
    if not worktree_path.exists():
        # Worktree directory already gone; prune stale entries
        _run_git(project_root, "worktree", "prune", check=False)
        logger.info("Worktree directory already removed: %s", worktree_path)
        return

    result = _run_git(project_root, "worktree", "remove", str(worktree_path), "--force", check=False)
    if result.returncode != 0:
        logger.warning("Failed to remove worktree %s: %s", worktree_path, result.stderr.strip())
        # Try prune as fallback
        _run_git(project_root, "worktree", "prune", check=False)
    else:
        logger.info("Removed worktree: %s", worktree_path)


def merge_loop_branch(project_root: Path, loop_branch: str, target_branch: str) -> bool:
    """Merge a loop branch into the target branch.

    Args:
        project_root: Project root directory
        loop_branch: Name of the loop branch to merge
        target_branch: Name of the branch to merge into

    Returns:
        True if merge succeeded, False if there were conflicts
    """
    # Ensure we're on the target branch
    current = get_current_branch(project_root)
    if current != target_branch:
        _run_git(project_root, "checkout", target_branch)

    result = _run_git(
        project_root, "merge", loop_branch,
        "--no-edit",
        "-m", f"Merge loop branch {loop_branch}",
        check=False,
    )

    if result.returncode != 0:
        if "CONFLICT" in result.stdout or "CONFLICT" in result.stderr:
            logger.error("Merge conflict merging %s into %s", loop_branch, target_branch)
            # Abort the merge to leave repo in clean state
            _run_git(project_root, "merge", "--abort", check=False)
            return False
        else:
            logger.error("Merge failed: %s", result.stderr.strip())
            _run_git(project_root, "merge", "--abort", check=False)
            return False

    logger.info("Successfully merged %s into %s", loop_branch, target_branch)
    return True


def delete_branch(project_root: Path, branch: str) -> None:
    """Delete a local branch.

    Args:
        project_root: Project root directory
        branch: Branch name to delete
    """
    result = _run_git(project_root, "branch", "-D", branch, check=False)
    if result.returncode != 0:
        logger.warning("Failed to delete branch %s: %s", branch, result.stderr.strip())
    else:
        logger.info("Deleted branch: %s", branch)


def cleanup_loop(
    project_root: Path,
    loop_branch: str,
    worktree_path: Path,
    delete_branch_flag: bool = False,
) -> None:
    """Full cleanup: remove worktree and optionally delete the loop branch.

    Args:
        project_root: Project root directory
        loop_branch: Name of the loop branch
        worktree_path: Path to the worktree directory
        delete_branch_flag: If True, also delete the branch
    """
    remove_worktree(project_root, worktree_path)

    if delete_branch_flag:
        delete_branch(project_root, loop_branch)

    logger.info("Loop cleanup complete (branch deleted: %s)", delete_branch_flag)


def has_new_commits(project_root: Path, branch: str, base_branch: str) -> bool:
    """Check if a branch has commits ahead of the base branch.

    Args:
        project_root: Project root directory
        branch: Branch to check
        base_branch: Base branch to compare against

    Returns:
        True if branch has commits not in base_branch
    """
    result = _run_git(
        project_root, "rev-list", "--count", f"{base_branch}..{branch}",
        check=False,
    )
    if result.returncode != 0:
        return False
    count = int(result.stdout.strip())
    return count > 0
