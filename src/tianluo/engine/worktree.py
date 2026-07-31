"""Git worktree lifecycle management for SE3 branch isolation.

Provides generic primitives to create git worktrees for isolated execution,
clean them up resiliently, query repository state, and resolve merge conflicts
with full task context. Used by both the implement-step DAG parallel path and
the ``luo run --worktree`` isolation mode.
"""

from __future__ import annotations
from tianluo.runtime_paths import runtime_dir, uploads_dir

import logging
import os
import shutil
import subprocess
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


def _branch_safe_name(branch: str) -> str:
    """Convert branch name to filesystem-safe directory name."""
    return branch.replace("/", "-")


def seed_uploads(project_root: Path, worktree_path: Path) -> int:
    """Materialize *project_root*'s web-UI attachments inside *worktree_path*.

    INVARIANT: an attachment path sitting in a prompt must be openable from the
    working directory the flow actually runs in. The web UI stores a pasted file
    under the *main* root and leaves the project-relative path
    ``<runtime>/uploads/<hash>_<name>`` in the prompt text; a ``--worktree`` run
    then executes that prompt with the sandbox as its working directory. Since
    the uploads directory is gitignored, ``git worktree add`` produces a
    checkout without it, and that path would resolve to nothing. Seeding closes
    the gap for files attached *before* the fork; files attached to an
    already-running worktree flow land in the sandbox directly (see
    ``daemon/client.py``'s UPLOAD_COMMAND handler).

    Entries are hard-linked, not copied: the names are content-addressed, so a
    stored attachment is immutable and sharing one inode between the main root
    and every sandbox is safe — and it keeps forking cheap no matter how many
    (up to 20 MB each) attachments a project has accumulated. Copying is the
    fallback for filesystems that refuse the link.

    A real directory is created rather than a symlink to the main root's: the
    gitignore rule ``<runtime>/uploads/`` has a trailing slash and therefore
    matches directories only, so a symlink (which git sees as a file) would
    leave the sandbox permanently dirty and break its commit steps.

    Returns the number of attachments made available. Never raises: a sandbox
    without its attachments is a degraded run, but a worktree that fails to be
    created at all is a dead one.
    """
    source = uploads_dir(project_root)
    if not source.is_dir():
        return 0

    # The sandbox path is the main root's, transplanted — NOT re-resolved
    # against the sandbox. The prompt already carries a relative path spelled
    # with the main root's runtime directory name, and a freshly checked-out
    # sandbox whose runtime directory happens to hold nothing tracked would
    # resolve to the canonical name instead, landing the files beside the path
    # the agent is about to open rather than at it.
    target = worktree_path / source.relative_to(Path(project_root))
    seeded = 0
    try:
        target.mkdir(parents=True, exist_ok=True)
        for entry in sorted(source.iterdir()):
            if not entry.is_file():
                continue
            destination = target / entry.name
            if destination.exists():
                seeded += 1
                continue
            try:
                os.link(entry, destination)
            except OSError:
                shutil.copy2(entry, destination)
            seeded += 1
    except OSError as exc:
        logger.warning(
            "Could not seed uploads from %s into worktree %s: %s",
            source, worktree_path, exc,
        )
        return seeded

    if seeded:
        logger.info("Seeded %d upload(s) into worktree: %s", seeded, worktree_path)
    return seeded


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
    worktree_path = runtime_dir(project_root) / "worktrees" / safe_name

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
            seed_uploads(project_root, worktree_path)
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


def _cleanup_git_worktree_metadata(project_root: Path, branch_name: str) -> None:
    """Directly delete .git/worktrees/<safe_name> metadata directory as a last resort.

    This bypasses standard git commands and removes the internal metadata
    that git uses to track a worktree. Only use after all standard cleanup
    methods have failed.

    Args:
        project_root: Project root directory
        branch_name: The branch whose worktree metadata should be removed
    """
    safe_name = _branch_safe_name(branch_name)
    metadata_path = project_root / ".git" / "worktrees" / safe_name
    if not metadata_path.exists():
        return
    try:
        shutil.rmtree(metadata_path)
        logger.info(
            "Removed .git/worktrees metadata directory: %s", metadata_path,
        )
    except Exception as exc:
        logger.warning(
            "Failed to remove .git/worktrees metadata %s: %s",
            metadata_path, exc,
        )


def force_cleanup_worktree(project_root: Path, branch_name: str) -> None:
    """Forcefully clean up a worktree for the given branch regardless of state.

    Combines unlock, directory removal, pruning, metadata cleanup, and
    verification to ensure the worktree is fully removed. Each step is
    independently fault-tolerant — a single step timing out or failing will
    not block subsequent cleanup steps.

    Suitable for resume scenarios where the worktree may be in an
    inconsistent state (locked, partially created, etc.).

    Args:
        project_root: Project root directory
        branch_name: The branch whose worktree should be cleaned up
    """
    safe_name = _branch_safe_name(branch_name)
    worktree_path = runtime_dir(project_root) / "worktrees" / safe_name

    # Step 1: Unlock the worktree if locked (ignore errors if not locked)
    try:
        _run_git(
            project_root, "worktree", "unlock", str(worktree_path),
            check=False, timeout=60,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Step 1 (unlock) timed out for branch %s", branch_name)
    except Exception as exc:
        logger.warning("Step 1 (unlock) failed for branch %s: %s", branch_name, exc)

    # Step 2: Try git worktree remove with double-force
    try:
        _run_git(
            project_root, "worktree", "remove", "-f", "-f", str(worktree_path),
            check=False, timeout=60,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Step 2 (remove) timed out for branch %s", branch_name)
    except Exception as exc:
        logger.warning("Step 2 (remove) failed for branch %s: %s", branch_name, exc)

    # Step 3: Remove the directory if it still exists
    try:
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
            logger.info("Removed worktree directory: %s", worktree_path)
    except Exception as exc:
        logger.warning(
            "Step 3 (rmtree) failed for branch %s: %s", branch_name, exc,
        )

    # Step 4: Prune stale worktree entries
    try:
        _run_git(
            project_root, "worktree", "prune",
            check=False, timeout=60,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Step 4 (prune) timed out for branch %s", branch_name)
    except Exception as exc:
        logger.warning("Step 4 (prune) failed for branch %s: %s", branch_name, exc)

    # Step 5: Clean up .git/worktrees metadata as last resort
    try:
        _cleanup_git_worktree_metadata(project_root, branch_name)
    except Exception as exc:
        logger.warning(
            "Step 5 (metadata cleanup) failed for branch %s: %s",
            branch_name, exc,
        )

    # Step 6: Verify cleanup
    try:
        if exists_for_branch(project_root, branch_name):
            logger.warning(
                "Worktree for branch %s still registered after force cleanup",
                branch_name,
            )
        else:
            logger.info("Force cleanup complete for branch %s", branch_name)
    except Exception as exc:
        logger.warning(
            "Step 6 (verify) failed for branch %s: %s", branch_name, exc,
        )


def delete_branch(project_root: Path, branch: str) -> None:
    """Delete a local branch.

    Before deleting, checks whether a worktree is still registered for the
    branch. If so, runs ``force_cleanup_worktree`` to remove it first, then
    re-checks. If the worktree persists after cleanup, logs a warning but
    still attempts the branch deletion.

    Args:
        project_root: Project root directory
        branch: Branch name to delete
    """
    # Pre-delete: ensure no worktree is registered for this branch
    if exists_for_branch(project_root, branch):
        logger.info(
            "Worktree still registered for branch %s — running force cleanup before delete",
            branch,
        )
        force_cleanup_worktree(project_root, branch)

        if exists_for_branch(project_root, branch):
            logger.warning(
                "Worktree for branch %s still registered after retry cleanup; "
                "proceeding with branch deletion anyway",
                branch,
            )

    result = _run_git(project_root, "branch", "-D", branch, check=False)
    if result.returncode != 0:
        logger.warning("Failed to delete branch %s: %s", branch, result.stderr.strip())
    else:
        logger.info("Deleted branch: %s", branch)


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


def detect_unmerged_paths(project_root: Path) -> list[str]:
    """Return paths currently in unmerged-index state, deduped.

    Unlike ``get_conflicting_files`` which reads worktree-vs-index diff and
    can miss modify/delete combinations, this reads the index directly via
    ``git ls-files --unmerged`` and surfaces every stage>0 path regardless
    of working-tree state.
    """
    result = _run_git(project_root, "ls-files", "--unmerged", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if "\t" in line:
            paths.add(line.split("\t", 1)[1])
    return sorted(paths)


def merge_in_progress(project_root: Path) -> bool:
    """True iff a real git merge/cherry-pick/rebase/revert is mid-flight.

    Distinguishes "user has an active git operation they need to finish"
    from "the index is dirty but no operation is active" (stale leftover).
    Works for both regular clones and linked worktrees by resolving the
    real ``.git`` directory via ``git rev-parse --git-dir``.
    """
    result = _run_git(project_root, "rev-parse", "--git-dir", check=False)
    if result.returncode != 0:
        return False
    git_dir_str = result.stdout.strip()
    if not git_dir_str:
        return False
    git_dir = Path(git_dir_str)
    if not git_dir.is_absolute():
        git_dir = (project_root / git_dir).resolve()
    return any(
        (git_dir / m).exists()
        for m in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                  "rebase-merge", "rebase-apply")
    )


def recover_stale_unmerged_paths(
    project_root: Path,
) -> tuple[list[str], list[str]]:
    """Auto-resolve stale unmerged-index entries left over from a prior failure.

    A path is safe to auto-resolve when no active merge marker exists (see
    ``merge_in_progress``) AND the working-tree content already matches
    what HEAD has for that path — i.e. resolving as "keep HEAD" is a no-op
    on actual file content, only the stale stage entries get cleared.

    The two safe shapes:

    * working-tree file present, blob == HEAD blob → ``git add`` to mark
      resolved (the case produced by a modify/delete merge that was
      abandoned without ``--abort``).
    * working-tree path absent AND HEAD has no entry for it → ``git rm``
      (path was never on either resolved side; entries are pure index
      garbage).

    Returns ``(resolved, unresolved)``. ``resolved`` lists paths cleared by
    this call; ``unresolved`` lists paths whose working-tree content
    diverges from HEAD and therefore needs human attention. Callers should
    fail-fast when ``unresolved`` is non-empty.

    Does NOT touch the index when ``merge_in_progress`` is true — caller
    must check that separately. The split mirrors the data-vs-policy
    boundary: this function only handles the stale-leftover case; an
    in-progress merge is the user's call to continue or abort.
    """
    paths = detect_unmerged_paths(project_root)
    if not paths:
        return ([], [])

    resolved: list[str] = []
    unresolved: list[str] = []
    for path in paths:
        head_obj = _run_git(
            project_root, "ls-tree", "HEAD", "--", path, check=False,
        )
        head_blob: str | None = None
        if head_obj.returncode == 0 and head_obj.stdout.strip():
            parts = head_obj.stdout.strip().split()
            if len(parts) >= 3 and parts[1] == "blob":
                head_blob = parts[2]

        wt_path = project_root / path
        wt_blob: str | None = None
        if wt_path.is_file():
            wt_obj = _run_git(
                project_root, "hash-object", "--", str(wt_path), check=False,
            )
            if wt_obj.returncode == 0:
                wt_blob = wt_obj.stdout.strip() or None

        if wt_blob is not None and wt_blob == head_blob:
            add_res = _run_git(project_root, "add", "--", path, check=False)
            if add_res.returncode == 0:
                resolved.append(path)
            else:
                unresolved.append(path)
        elif wt_blob is None and head_blob is None:
            rm_res = _run_git(
                project_root, "rm", "--cached", "--", path, check=False,
            )
            if rm_res.returncode == 0:
                resolved.append(path)
            else:
                unresolved.append(path)
        else:
            unresolved.append(path)
    return (resolved, unresolved)


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
