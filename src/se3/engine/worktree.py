"""Git worktree lifecycle management for SE3 loop mode branch isolation.

Provides functions to create loop branches, manage git worktrees for isolated
execution, merge results back, and clean up after loop completion.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

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
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0 and not check:
        logger.debug("Git command returned %d: %s", result.returncode, result.stderr.strip())
    return result


def has_commits(project_root: Path) -> bool:
    """Check whether the repository has at least one commit.

    Args:
        project_root: Project root directory

    Returns:
        True if the repo has at least one commit, False otherwise
    """
    result = _run_git(project_root, "rev-parse", "HEAD", check=False)
    return result.returncode == 0


def get_current_branch(project_root: Path) -> str:
    """Get the current branch name.

    Works on both normal repos and empty repos (git init with no commits)
    by trying ``git symbolic-ref`` first, then falling back to ``rev-parse``.

    Args:
        project_root: Project root directory

    Returns:
        Current branch name

    Raises:
        RuntimeError: If in detached HEAD state or no branch can be determined
    """
    # symbolic-ref works even on repos with no commits (orphan branch)
    sym_result = _run_git(project_root, "symbolic-ref", "--short", "HEAD", check=False)
    if sym_result.returncode == 0:
        branch = sym_result.stdout.strip()
        if branch:
            return branch

    # Fallback: rev-parse works on repos with commits
    rp_result = _run_git(project_root, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if rp_result.returncode == 0:
        branch = rp_result.stdout.strip()
        if branch == "HEAD":
            raise RuntimeError("Detached HEAD state — cannot determine branch")
        if branch:
            return branch

    raise RuntimeError("Cannot determine current branch")


def _slugify_task_id(task: str) -> str:
    """Convert a task description into a branch-name-safe slug.

    Lowercases, replaces non-alphanumeric chars with hyphens, strips leading/
    trailing hyphens, collapses consecutive hyphens, and truncates to 30 chars.
    """
    slug = task.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:30].rstrip("-")


def create_loop_branch(
    project_root: Path,
    timestamp: str | None = None,
    task_id: str | None = None,
    iteration: int | None = None,
) -> tuple[str, str]:
    """Create a new loop branch from current HEAD.

    When *task_id* and *iteration* are provided the branch is named
    ``loop/{slugified_task_id}-{iteration}`` (new convention).  Otherwise
    falls back to the legacy ``se3-loop/{timestamp}`` format.

    Args:
        project_root: Project root directory
        timestamp: Optional timestamp string; defaults to now (legacy)
        task_id: Task identifier for the new naming convention
        iteration: Iteration number for the new naming convention

    Returns:
        Tuple of (loop_branch_name, original_branch_name)

    Raises:
        subprocess.CalledProcessError: If branch creation fails
    """
    original_branch = get_current_branch(project_root)

    if task_id and iteration is not None:
        slug = _slugify_task_id(task_id)
        if not slug:
            slug = "task"
        branch_name = f"loop/{slug}-{iteration}"
    else:
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

    # Prune stale worktree entries to avoid lock contention
    _run_git(project_root, "worktree", "prune", check=False)

    # Ensure parent directory exists
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    max_retries = 2
    timeout = 120
    last_error: subprocess.TimeoutExpired | None = None

    for attempt in range(max_retries + 1):
        try:
            _run_git(project_root, "worktree", "add", str(worktree_path), branch, timeout=timeout)
            logger.info("Created worktree at: %s (branch: %s)", worktree_path, branch)
            return worktree_path
        except subprocess.TimeoutExpired as e:
            last_error = e
            if attempt < max_retries:
                logger.warning(
                    "Worktree creation timed out after %ds (attempt %d/%d). "
                    "Pruning stale worktrees and retrying with %ds timeout...",
                    timeout, attempt + 1, max_retries + 1, timeout * 2,
                )
                # Clean up partial worktree directory
                if worktree_path.exists():
                    shutil.rmtree(worktree_path, ignore_errors=True)
                # Prune stale worktrees that may cause lock contention
                _run_git(project_root, "worktree", "prune", check=False)
                # Double timeout for next attempt
                timeout *= 2
            else:
                logger.error(
                    "Worktree creation failed after %d attempts for branch '%s' at '%s'",
                    max_retries + 1, branch, worktree_path,
                )

    # All retries exhausted — re-raise with context
    raise subprocess.TimeoutExpired(
        last_error.cmd,
        last_error.timeout,
        output=last_error.output,
        stderr=last_error.stderr,
    )


def fork_worktree(
    project_root: Path,
    source_branch: str,
    new_branch: str,
) -> Path:
    """Create a new branch from source_branch and set up a worktree for it.

    Used by the relay strategy when a fork point's non-primary downstream
    needs its own worktree starting from the fork point's state.

    Args:
        project_root: Project root directory
        source_branch: Existing branch to fork from
        new_branch: Name for the new branch

    Returns:
        Path to the newly created worktree directory

    Raises:
        subprocess.CalledProcessError: If branch creation or worktree setup fails
    """
    _run_git(project_root, "branch", new_branch, source_branch)
    logger.info("Created branch '%s' from '%s'", new_branch, source_branch)
    return create_worktree(project_root, new_branch)


def remove_worktree(project_root: Path, worktree_path: Path) -> None:
    """Remove a git worktree.

    Handles the case where the worktree directory has already been removed
    (pruning stale git metadata) and locked worktrees (using double-force).

    Args:
        project_root: Project root directory
        worktree_path: Path to the worktree to remove
    """
    if not worktree_path.exists():
        # Worktree directory already gone; prune stale entries
        _run_git(project_root, "worktree", "prune", check=False)
        # After pruning, check if git metadata still references this path.
        # If so, force-remove the stale entry.
        _force_remove_if_still_registered(project_root, worktree_path)
        logger.info("Worktree directory already removed: %s", worktree_path)
        return

    result = _run_git(project_root, "worktree", "remove", str(worktree_path), "--force", check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "locked" in stderr.lower():
            # Locked worktree requires double-force to override
            logger.info("Worktree is locked, retrying with double-force: %s", worktree_path)
            retry = _run_git(
                project_root, "worktree", "remove", "-f", "-f", str(worktree_path), check=False,
            )
            if retry.returncode == 0:
                logger.info("Removed locked worktree: %s", worktree_path)
                return
            logger.warning(
                "Failed to remove locked worktree %s even with double-force: %s",
                worktree_path, retry.stderr.strip(),
            )
        else:
            logger.warning("Failed to remove worktree %s: %s", worktree_path, stderr)
        # Fallback: prune stale entries
        _run_git(project_root, "worktree", "prune", check=False)
    else:
        logger.info("Removed worktree: %s", worktree_path)


def _force_remove_if_still_registered(project_root: Path, worktree_path: Path) -> None:
    """Force-remove a worktree entry if git still tracks it after pruning.

    This handles the case where the worktree directory is gone but git
    metadata (e.g. a lock file) prevents pruning from cleaning it up.
    """
    result = _run_git(project_root, "worktree", "list", "--porcelain", check=False)
    if result.returncode != 0:
        return

    wt_str = str(worktree_path)
    for line in result.stdout.splitlines():
        if line.startswith("worktree ") and line.split(" ", 1)[1] == wt_str:
            logger.info("Stale git metadata found for %s, force-removing", worktree_path)
            _run_git(
                project_root, "worktree", "remove", "-f", "-f", str(worktree_path), check=False,
            )
            return


def force_cleanup_worktree(project_root: Path, branch_name: str) -> None:
    """Forcefully clean up a worktree for the given branch regardless of state.

    Combines unlock, directory removal, pruning, and verification to ensure
    the worktree is fully removed. Suitable for resume scenarios where the
    worktree may be in an inconsistent state (locked, partially created, etc.).

    Args:
        project_root: Project root directory
        branch_name: The branch whose worktree should be cleaned up
    """
    safe_name = _branch_safe_name(branch_name)
    worktree_path = project_root / "se3" / "worktrees" / safe_name

    # Step 1: Unlock the worktree if locked (ignore errors if not locked)
    _run_git(project_root, "worktree", "unlock", str(worktree_path), check=False)

    # Step 2: Try git worktree remove with double-force
    _run_git(project_root, "worktree", "remove", "-f", "-f", str(worktree_path), check=False)

    # Step 3: Remove the directory if it still exists
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
        logger.info("Removed worktree directory: %s", worktree_path)

    # Step 4: Prune stale worktree entries
    _run_git(project_root, "worktree", "prune", check=False)

    # Step 5: Verify cleanup
    if exists_for_branch(project_root, branch_name):
        logger.warning(
            "Worktree for branch %s still registered after force cleanup", branch_name,
        )
    else:
        logger.info("Force cleanup complete for branch %s", branch_name)


def merge_loop_branch(
    project_root: Path,
    loop_branch: str,
    target_branch: str,
    conflict_strategy: str = "human",
) -> Union[bool, str]:
    """Merge a loop branch into the target branch.

    Args:
        project_root: Project root directory
        loop_branch: Name of the loop branch to merge
        target_branch: Name of the branch to merge into
        conflict_strategy: How to handle conflicts — ``'human'`` (preserve
            conflict state + create call file) or ``'llm'`` (attempt
            per-file LLM resolution, fallback to human).

    Returns:
        ``True`` if merge succeeded, ``False`` if merge failed (non-conflict),
        or ``'pending_human'`` when *conflict_strategy='human'* and conflicts
        are detected.
    """
    # Ensure we're on the target branch
    current = get_current_branch(project_root)
    if current != target_branch:
        _run_git(project_root, "checkout", target_branch)

    # Stash uncommitted changes before merging (they would block git merge)
    stash_result = _run_git(project_root, "stash", "--include-untracked", check=False)
    stashed = stash_result.returncode == 0 and "No local changes" not in stash_result.stdout

    result = _run_git(
        project_root, "merge", loop_branch,
        "--no-edit",
        "-m", f"Merge loop branch {loop_branch}",
        check=False,
    )

    if result.returncode != 0:
        is_conflict = "CONFLICT" in result.stdout or "CONFLICT" in result.stderr
        if not is_conflict:
            logger.error("Merge failed: %s", result.stderr.strip())
            _run_git(project_root, "merge", "--abort", check=False)
            if stashed:
                _run_git(project_root, "stash", "pop", check=False)
            return False

        logger.error("Merge conflict merging %s into %s", loop_branch, target_branch)
        conflict_files = get_conflicting_files(project_root)

        if conflict_strategy == "llm":
            resolved = _resolve_conflicts_with_llm(project_root, conflict_files)
            if resolved:
                logger.info("LLM resolved all conflicts — completing merge")
                if stashed:
                    _run_git(project_root, "stash", "pop", check=False)
                return True
            # LLM failed — fall through to human mode
            logger.warning("LLM conflict resolution failed, falling back to human mode")

        if conflict_strategy == "human" or conflict_strategy == "llm":
            # For human mode: preserve conflict state and create call file
            # Note: stash is NOT popped here — human needs clean state to resolve conflicts
            # Stash will be available via `git stash pop` after conflict resolution
            if stashed:
                logger.info("Uncommitted changes stashed — run 'git stash pop' after resolving conflicts")
            _display_merge_conflict(loop_branch, target_branch, conflict_files)
            _create_merge_conflict_call(project_root, loop_branch, target_branch, conflict_files)
            return "pending_human"

    # Merge succeeded — restore stashed changes
    if stashed:
        pop_result = _run_git(project_root, "stash", "pop", check=False)
        if pop_result.returncode != 0:
            logger.warning("Stash pop had conflicts after merge — resolve manually with 'git stash pop'")

    logger.info("Successfully merged %s into %s", loop_branch, target_branch)
    return True


def _resolve_conflicts_with_llm(project_root: Path, conflict_files: list[str]) -> bool:
    """Attempt to resolve merge conflicts using LLM, one file at a time.

    Returns True if all conflicts were resolved, False otherwise.
    """
    try:
        from .llm_caller import LLMCaller
    except ImportError:
        logger.warning("LLMCaller not available for conflict resolution")
        return False

    for filepath in conflict_files:
        full_path = project_root / filepath
        if not full_path.exists():
            logger.warning("Conflict file not found: %s", filepath)
            return False

        try:
            content = full_path.read_text(encoding="utf-8")
        except Exception:
            logger.warning("Could not read conflict file: %s", filepath)
            return False

        prompt = (
            "You are resolving a git merge conflict. Below is the file content "
            "with conflict markers (<<<<<<< HEAD, =======, >>>>>>>). "
            "Output ONLY the fully resolved file content with no conflict markers. "
            "Do not add any explanation.\n\n"
            f"File: {filepath}\n\n```\n{content}\n```"
        )

        try:
            caller = LLMCaller(project_root, step_type="merge_conflict")
            resolved_content = caller.call(prompt=prompt)
        except Exception as e:
            logger.warning("LLM conflict resolution failed for %s: %s", filepath, e)
            return False

        # Verify no conflict markers remain
        if "<<<<<<<" in resolved_content or ">>>>>>>" in resolved_content:
            logger.warning("LLM output still contains conflict markers for %s", filepath)
            return False

        # Write resolved content and stage
        full_path.write_text(resolved_content, encoding="utf-8")
        _run_git(project_root, "add", filepath)

    # All files resolved — complete the merge
    commit_result = _run_git(
        project_root, "commit", "--no-edit", check=False,
    )
    if commit_result.returncode != 0:
        logger.warning("Failed to complete merge commit: %s", commit_result.stderr.strip())
        return False

    return True


def _create_merge_conflict_call(
    project_root: Path,
    loop_branch: str,
    target_branch: str,
    conflict_files: list[str],
) -> None:
    """Create a call file for human merge conflict resolution."""
    calls_dir = project_root / "se3" / "calls"
    calls_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    call_file = calls_dir / f"merge_conflict_{ts}.json"

    call_data = {
        "type": "merge_conflict",
        "created_at": datetime.now().isoformat(),
        "loop_branch": loop_branch,
        "target_branch": target_branch,
        "conflict_files": conflict_files,
        "instructions": (
            f"Merge conflict detected merging {loop_branch} into {target_branch}. "
            f"Resolve the {len(conflict_files)} conflicting file(s), then run "
            f"'git add' and 'git commit' to complete the merge."
        ),
    }

    call_file.write_text(json.dumps(call_data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Created merge conflict call file: %s", call_file)


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

    Matches both the new ``loop/*`` and legacy ``se3-loop/*`` patterns.
    Legacy branches are flagged with ``is_legacy=True``.

    Args:
        project_root: Project root directory

    Returns:
        List of dicts with 'branch', 'commit_count', 'base_branch', and
        'is_legacy' keys.
    """
    branches = []
    current_branch = get_current_branch(project_root)

    for pattern, is_legacy in [("loop/*", False), ("se3-loop/*", True)]:
        result = _run_git(
            project_root, "branch", "--list", pattern,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            continue

        for line in result.stdout.strip().splitlines():
            branch_name = line.strip().lstrip("* ")
            if not branch_name:
                continue

            if is_legacy:
                logger.warning(
                    "Legacy loop branch detected: %s — new format is loop/{task}-{iter}",
                    branch_name,
                )

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
                "is_legacy": is_legacy,
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


def resolve_merge_conflicts_with_context(
    project_root: Path,
    conflict_files: list[str],
    task_description: str,
    group_summaries: list[dict],
    spec_content: str,
    max_retries: int = 3,
    flow_id: str | None = None,
    step_id: str | None = None,
) -> bool:
    """Resolve merge conflicts using LLM with full task context.

    For each conflicting file, sends the file content (with conflict markers)
    to the LLM along with task description, group summaries, and spec context.
    Verifies the output contains no conflict markers before writing.

    Retries up to *max_retries* times.  Does NOT fall back to ``--theirs``.

    Args:
        project_root: Project root directory (where the merge is happening)
        conflict_files: List of conflicting file paths (relative to project_root)
        task_description: Overall task description for LLM context
        group_summaries: ``[{group_id, summary, files_changed}, ...]``
        spec_content: Spec summary text for LLM context
        max_retries: Maximum resolution attempts
        flow_id: Optional flow ID for history recording
        step_id: Optional step ID for history recording

    Returns:
        True if all conflicts resolved and merge committed, False otherwise.
    """
    try:
        from .llm_caller import LLMCaller
    except ImportError:
        logger.warning("LLMCaller not available for conflict resolution")
        return False

    summaries_text = "\n".join(
        f"- Group {gs['group_id']}: {gs.get('summary', '')} "
        f"(files: {', '.join(gs.get('files_changed', []))})"
        for gs in group_summaries
    ) if group_summaries else "No group context available."

    for attempt in range(1, max_retries + 1):
        resolved_contents: dict[str, str] = {}
        all_ok = True

        for filepath in conflict_files:
            full_path = project_root / filepath
            if not full_path.exists():
                logger.warning("Conflict file not found: %s", filepath)
                all_ok = False
                break

            try:
                content = full_path.read_text(encoding="utf-8")
            except Exception:
                logger.warning("Could not read conflict file: %s", filepath)
                all_ok = False
                break

            if "<<<<<<<" not in content:
                continue  # Already resolved or not a text conflict

            prompt = (
                "You are resolving a git merge conflict. The conflict occurred "
                "while merging parallel implementation branches back to the "
                "main branch.\n\n"
                f"## Task Description\n{task_description}\n\n"
                f"## What Each Group Did\n{summaries_text}\n\n"
                f"## Project Conventions\n{spec_content}\n\n"
                f"## Conflicting File: {filepath}\n\n"
                f"```\n{content}\n```\n\n"
                "Output ONLY the fully resolved file content. "
                "Do NOT include any conflict markers (<<<<<<< / ======= / >>>>>>>). "
                "Do NOT add any explanation or code fences."
            )

            try:
                caller = LLMCaller(
                    project_root,
                    flow_id=flow_id,
                    step_id=step_id,
                    step_type="merge_conflict",
                    external_attempt=attempt - 1,
                )
                resolved = caller.call(prompt=prompt)
            except Exception as e:
                logger.warning(
                    "LLM conflict resolution failed for %s (attempt %d/%d): %s",
                    filepath, attempt, max_retries, e,
                )
                all_ok = False
                break

            if "<<<<<<<" in resolved or ">>>>>>>" in resolved:
                logger.warning(
                    "LLM output still has conflict markers for %s (attempt %d/%d)",
                    filepath, attempt, max_retries,
                )
                all_ok = False
                break

            resolved_contents[filepath] = resolved

        if all_ok:
            # Write all resolved files and complete the merge
            for filepath, content in resolved_contents.items():
                (project_root / filepath).write_text(content, encoding="utf-8")
                _run_git(project_root, "add", filepath)

            commit_result = _run_git(
                project_root, "commit", "--no-edit", check=False,
            )
            if commit_result.returncode == 0:
                logger.info(
                    "Merge conflicts resolved on attempt %d/%d",
                    attempt, max_retries,
                )
                return True
            logger.warning(
                "Merge commit failed (attempt %d/%d): %s",
                attempt, max_retries, commit_result.stderr.strip(),
            )

        # Reset conflict state for retry
        if attempt < max_retries:
            for filepath in resolved_contents:
                _run_git(
                    project_root, "checkout", "--merge", "--", filepath,
                    check=False,
                )
            logger.info(
                "Retrying conflict resolution (attempt %d/%d)",
                attempt + 1, max_retries,
            )

    return False


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
