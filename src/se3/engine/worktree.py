"""Git worktree lifecycle management for SE3 loop mode branch isolation.

Provides functions to create loop branches, manage git worktrees for isolated
execution, merge results back, and clean up after loop completion.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

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

    On conflict: captures conflicting file list, displays with Rich formatting,
    aborts the merge to leave repo clean, and returns False.

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
            # Capture conflicting files before aborting
            conflict_files = get_conflicting_files(project_root)
            _display_merge_conflict(loop_branch, target_branch, conflict_files)
            # Abort the merge to leave repo in clean state
            _run_git(project_root, "merge", "--abort", check=False)
            return False
        else:
            logger.error("Merge failed: %s", result.stderr.strip())
            _run_git(project_root, "merge", "--abort", check=False)
            return False

    logger.info("Successfully merged %s into %s", loop_branch, target_branch)
    return True


def _display_merge_conflict(loop_branch: str, target_branch: str, conflict_files: list[str]) -> None:
    """Display merge conflict information with Rich formatting."""
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text

        console = Console()

        lines = Text()
        lines.append(f"Cannot merge ", style="bold red")
        lines.append(f"{loop_branch}", style="bold cyan")
        lines.append(f" into ", style="bold red")
        lines.append(f"{target_branch}", style="bold cyan")
        lines.append(f"\n\n", style="")

        if conflict_files:
            lines.append(f"Conflicting files ({len(conflict_files)}):\n", style="bold yellow")
            for f in conflict_files:
                lines.append(f"  • {f}\n", style="red")
        else:
            lines.append("Could not determine conflicting files.\n", style="dim")

        lines.append(f"\nTo resolve manually:\n", style="bold")
        lines.append(f"  git merge {loop_branch}\n", style="dim")
        lines.append(f"  # resolve conflicts\n", style="dim")
        lines.append(f"  git add . && git commit\n", style="dim")

        console.print(Panel(lines, title="Merge Conflict", border_style="red"))
    except ImportError:
        # Fallback without Rich
        print(f"\n--- Merge Conflict ---")
        print(f"Cannot merge {loop_branch} into {target_branch}")
        if conflict_files:
            print(f"\nConflicting files ({len(conflict_files)}):")
            for f in conflict_files:
                print(f"  • {f}")
        print(f"\nTo resolve: git merge {loop_branch}")
        print(f"---\n")


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


def exists_for_branch(project_root: Path, branch: str) -> bool:
    """Check if a worktree already exists for the given branch.

    Args:
        project_root: Project root directory
        branch: Branch name to check

    Returns:
        True if a worktree exists for this branch
    """
    result = _run_git(project_root, "worktree", "list", "--porcelain", check=False)
    if result.returncode != 0:
        return False

    # Parse porcelain output: each worktree block has "branch refs/heads/<name>"
    for line in result.stdout.splitlines():
        if line.startswith("branch "):
            wt_branch = line.split("refs/heads/", 1)[-1] if "refs/heads/" in line else ""
            if wt_branch == branch:
                return True
    return False


def get_conflicting_files(project_root: Path) -> list[str]:
    """Get list of files with merge conflicts.

    Args:
        project_root: Project root directory

    Returns:
        List of conflicting file paths
    """
    result = _run_git(
        project_root, "diff", "--name-only", "--diff-filter=U",
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    return [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]


def list_loop_branches(project_root: Path) -> list[dict[str, any]]:
    """List existing unmerged loop branches with commit counts.

    Args:
        project_root: Project root directory

    Returns:
        List of dicts with 'branch', 'commit_count', and 'base_branch' keys
    """
    result = _run_git(
        project_root, "branch", "--list", "se3-loop/*",
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []

    branches = []
    current_branch = get_current_branch(project_root)

    for line in result.stdout.strip().splitlines():
        branch_name = line.strip().lstrip("* ")
        if not branch_name:
            continue

        # Count commits ahead of current branch
        count_result = _run_git(
            project_root, "rev-list", "--count",
            f"{current_branch}..{branch_name}",
            check=False,
        )
        commit_count = 0
        if count_result.returncode == 0:
            commit_count = int(count_result.stdout.strip())

        branches.append({
            "branch": branch_name,
            "commit_count": commit_count,
            "base_branch": current_branch,
        })

    return branches


def get_diff_stat(project_root: Path, branch: str, base_branch: str) -> str:
    """Get diff stat summary between two branches.

    Args:
        project_root: Project root directory
        branch: Branch to compare
        base_branch: Base branch

    Returns:
        Diff stat string
    """
    result = _run_git(
        project_root, "diff", "--stat", f"{base_branch}..{branch}",
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


class WorktreeContext:
    """Context manager for exception-safe worktree lifecycle.

    On enter: validates no existing worktree for the branch, then creates one.
    On exit (including exceptions): removes the worktree but preserves the branch
    for recovery.

    Usage:
        with WorktreeContext(project_root, branch) as worktree_path:
            # work in worktree_path
    """

    def __init__(self, project_root: Path, branch: str) -> None:
        self.project_root = project_root
        self.branch = branch
        self.worktree_path: Optional[Path] = None

    def __enter__(self) -> Path:
        if exists_for_branch(self.project_root, self.branch):
            raise RuntimeError(
                f"Worktree already exists for branch {self.branch}. "
                f"Remove it first or use a different branch."
            )
        self.worktree_path = create_worktree(self.project_root, self.branch)
        return self.worktree_path

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.worktree_path is not None:
            remove_worktree(self.project_root, self.worktree_path)
            logger.info(
                "WorktreeContext: cleaned up worktree for %s (exception: %s)",
                self.branch,
                exc_type is not None,
            )
