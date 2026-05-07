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
from ..engine.merge.runtime_sync import DEST_HASH_UNAVAILABLE
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
    """Check if a local branch exists.

    Defect I4: ``git show-ref --verify`` is invoked with ``check=False`` so
    that "does not exist" reports cleanly via returncode rather than raising.
    We MUST inspect ``returncode`` (not just trust the call), and we MUST
    treat any infrastructure error (git missing, timeout, OS error) as
    "cannot determine" → ``False`` so the caller fails closed and refuses to
    merge an indeterminate ref. Otherwise a non-existent branch could slip
    past validation and surface as a misleading "merge failed" later.
    """
    try:
        result = _run_git(
            project_root, "show-ref", "--verify", "--quiet",
            f"refs/heads/{branch}",
            check=False,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning(
            "Cannot verify branch '%s' (treating as missing): %s", branch, exc,
        )
        return False
    return result.returncode == 0


# Shell metacharacters that could be misinterpreted by a downstream shell or
# git itself if a branch name ever leaked into a shell-interpreted context.
# Keeping this list explicit makes intent visible: branch names that contain
# any of these characters are rejected outright at CLI input. Subprocess
# invocations in this codebase use ``subprocess.run`` with a list argv, so
# these are about defense-in-depth and operator safety (avoiding misleading
# log lines, ANSI tricks via control chars, etc.) rather than literal shell
# injection.
_BRANCH_METACHARACTERS = frozenset(
    {
        "$",
        "`",
        ";",
        "&",
        "|",
        "<",
        ">",
        "(",
        ")",
        "{",
        "}",
        "[",
        "]",
        "*",
        "?",
        "!",
        "\\",
        '"',
        "'",
        "\n",
        "\r",
        "\t",
    }
)


def validate_branch_names(branches: list[str]) -> None:
    """Validate user-supplied branch names before any git command runs.

    Rejects:
      * empty list (defect I1)
      * empty string entry
      * leading-dash (so ``-rf`` cannot be passed to git as a flag) — defect I2
      * shell metacharacters (defense-in-depth — defect I2)
      * git-invalid characters: spaces, ``..``, ``~``, ``^``, ``:``,
        characters below ASCII 0x20, trailing ``.lock``
      * names ``HEAD`` or ``@`` which collide with git pseudo-refs

    Raises:
        ValueError: When at least one branch name is invalid. The message
            lists each rejected name and the rule it violated, so the CLI
            layer can wrap it in ``typer.BadParameter`` and the operator can
            see exactly which input is rejected.
    """
    if not branches:
        raise ValueError("At least one branch name is required.")

    rejected: list[str] = []
    for branch in branches:
        if not isinstance(branch, str):
            rejected.append(f"{branch!r}: not a string")
            continue
        if branch == "":
            rejected.append("'' (empty string): branch name must be non-empty")
            continue
        if branch.startswith("-"):
            rejected.append(
                f"{branch!r}: branch names must not start with '-' "
                "(could be misinterpreted as a CLI flag)"
            )
            continue
        if branch in ("HEAD", "@"):
            rejected.append(
                f"{branch!r}: reserved git pseudo-ref"
            )
            continue
        bad_chars = sorted({c for c in branch if c in _BRANCH_METACHARACTERS})
        if bad_chars:
            rejected.append(
                f"{branch!r}: contains shell metacharacter(s) "
                f"{''.join(repr(c) for c in bad_chars)}"
            )
            continue
        if any(ord(c) < 0x20 for c in branch):
            rejected.append(
                f"{branch!r}: contains control character(s) (ASCII < 0x20)"
            )
            continue
        if " " in branch:
            rejected.append(
                f"{branch!r}: branch names must not contain spaces"
            )
            continue
        # git ref-format rules — minimal subset most likely to bite users
        if (
            ".." in branch
            or branch.startswith(".")
            or branch.startswith("/")
            or branch.endswith("/")
            or branch.endswith(".lock")
            or "@{" in branch
        ):
            rejected.append(
                f"{branch!r}: violates git ref-format rules "
                "(see git check-ref-format)"
            )
            continue

    if rejected:
        message = (
            "Invalid branch name(s):\n  - "
            + "\n  - ".join(rejected)
        )
        raise ValueError(message)


def _append_runtime_sync_lines(lines: list[str], report) -> None:
    """Append runtime-sync rendering lines to *lines* in place.

    Renders the full set of runtime-sync signals (skipped branches, skipped
    files, idempotent bypasses, tier B discarded, tier A collisions) so that
    failure branches do not lose visibility of partial-sync state. Each
    section is gated by ``if`` so empty fields produce no output.

    Called from every CLI branch (success, rollback_failed, pending_human,
    generic-failure) to keep the rendered set consistent. A failure branch
    that completed some tier-A syncs before halting still surfaces
    idempotent-bypass and tier-B-discarded signals via this helper, rather
    than only when ``report.success`` is True.
    """
    if report.runtime_sync_skipped_branches:
        lines.append("")
        lines.append(
            "WARNING: Runtime data was not synced for these branches "
            "(no bound worktree found):"
        )
        for b in report.runtime_sync_skipped_branches:
            lines.append(f"  - {b}")
    if report.runtime_sync_skipped_files:
        lines.append("")
        lines.append(
            "WARNING: Runtime sync skipped files (some entries may "
            "indicate data loss — e.g. destination path is a directory "
            "or non-regular entry (FIFO/socket/device), sidecar name too "
            "long, sidecar disambiguation exhausted, or sidecar path is "
            "a directory; see log for details):"
        )
        for branch, files in report.runtime_sync_skipped_files:
            lines.append(f"  - {branch}: {', '.join(files)}")
    if report.runtime_sync_idempotent_bypasses:
        lines.append("")
        lines.append(
            "Runtime sync idempotent bypasses (sidecar already matched "
            "source content — possible stale sidecar leftovers from a "
            "prior aborted run that may mask a new collision):"
        )
        for branch, count in report.runtime_sync_idempotent_bypasses:
            lines.append(f"  - {branch}: {count} file(s)")
        # Per-file paths (parallel to the audit-only collision rendering):
        # without these, an operator investigating the stale-sidecar warning
        # had to cross-reference logs or programmatically read
        # ``report.runtime_sync_idempotent_records``.  Surface the rel_path
        # and sidecar_rel_path inline so the summary is self-contained.
        if report.runtime_sync_idempotent_records:
            for record in report.runtime_sync_idempotent_records:
                lines.append(
                    f"      {record.branch}: {record.original_rel_path} "
                    f"== {record.sidecar_rel_path}"
                )
    if report.runtime_sync_discarded:
        lines.append("")
        lines.append(
            "Runtime sync discarded (tier B branch-side state preserved "
            "by current branch):"
        )
        for branch, files in report.runtime_sync_discarded:
            lines.append(f"  - {branch}: {len(files)} file(s)")
    if report.runtime_sync_collisions:
        written_collisions = [
            c for c in report.runtime_sync_collisions if c.written
        ]
        audit_only_collisions = [
            c for c in report.runtime_sync_collisions if not c.written
        ]
        if written_collisions:
            lines.append("")
            lines.append("Runtime sync collisions (sidecar bypass):")
            for collision in written_collisions:
                lines.append(
                    f"  - {collision.branch}: {collision.original_rel_path} "
                    f"-> {collision.sidecar_rel_path} "
                    f"(src_hash={collision.src_hash[:8]}.. "
                    f"dest_hash={collision.dest_hash[:8]}..)"
                )
        if audit_only_collisions:
            lines.append("")
            lines.append(
                "Runtime sync collisions (audit-only — sidecar NOT written; "
                "source data is NOT recoverable from disk):"
            )
            for collision in audit_only_collisions:
                # dest_hash may be the DEST_HASH_UNAVAILABLE sentinel here
                # when the destination file was unhashable; render it
                # verbatim so operators can tell at a glance that the row
                # is bookkeeping, not data.
                dest_hash_render = (
                    collision.dest_hash
                    if collision.dest_hash == DEST_HASH_UNAVAILABLE
                    else f"{collision.dest_hash[:8]}.."
                )
                lines.append(
                    f"  - {collision.branch}: {collision.original_rel_path} "
                    f"-> {collision.sidecar_rel_path} "
                    f"(src_hash={collision.src_hash[:8]}.. "
                    f"dest_hash={dest_hash_render})"
                )


def _split_merged_buckets(report) -> tuple[list[str], list[str]]:
    """Return ``(newly_merged, already_ancestor)`` from a merge report.

    Defect I3: the legacy ``merged_branches`` aggregate erased the distinction
    between branches that produced a new merge commit and branches that were
    already reachable from HEAD (a no-op). The orchestrator now populates two
    parallel lists; this helper exposes them with a defensive fallback so that
    legacy callers that fail to populate the new buckets still render
    something useful instead of an empty section.
    """
    newly = list(getattr(report, "newly_merged_branches", []) or [])
    already = list(getattr(report, "already_ancestor_branches", []) or [])
    # Defensive fallback: if the orchestrator did not populate the new
    # buckets (older code path or test stub), fall back to the legacy
    # aggregate so we still render something useful rather than an empty
    # list. Treat the legacy aggregate as "newly merged" for the
    # fallback case — this matches the historical wording.
    if not newly and not already and getattr(report, "merged_branches", None):
        newly = list(report.merged_branches)
    return newly, already


def _append_split_branch_lines(
    lines: list[str],
    newly: list[str],
    already: list[str],
) -> None:
    """Append per-bucket branch listings to *lines* in place."""
    if newly:
        lines.append(f"Newly merged ({len(newly)}):")
        for b in newly:
            lines.append(f"  - {b}")
    if already:
        if newly:
            lines.append("")
        lines.append(
            f"Already an ancestor of HEAD — no new commit ({len(already)}):"
        )
        for b in already:
            lines.append(f"  - {b}")


def run_merge(
    branches: list[str],
    strategy: str = "default",
    delete_merged: bool = False,
    strict_runtime_sync: bool = False,
    project_root: Optional[Path] = None,
) -> int:
    """Run the merge command.

    Args:
        branches: List of branch names to merge (in order).
        strategy: Conflict resolution strategy.
        delete_merged: Whether to delete merged branches afterward.
        strict_runtime_sync: When True, tier A runtime sync collisions halt
            the merge sequence. When False (default), collisions are bypassed
            via sidecar files and the sequence continues.
        project_root: Project root directory. Auto-detected if None.

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    if project_root is None:
        from .run import get_project_root

        project_root = get_project_root()

    # Defense-in-depth: even when called programmatically (skipping the CLI
    # layer that already validates), reject obviously-bad branch names so
    # downstream code never sees ``-rf`` or shell metachars in a branch arg.
    try:
        validate_branch_names(branches)
    except ValueError as exc:
        render_text(str(exc), title="Merge Error")
        return 1

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
        strict_runtime_sync=strict_runtime_sync,
    )
    report = orchestrator.execute(branches)

    if report.success:
        # Defect I3: split rendering by newly-merged vs already-ancestor.
        # Operators care about the difference: a branch in "already" was
        # already reachable from HEAD (no-op for this run), while a branch
        # in "newly" produced a fresh merge commit. Older versions of the
        # CLI lumped them together and made it impossible to tell whether
        # a re-run actually made progress or was an idempotent no-op.
        newly, already = _split_merged_buckets(report)
        total = len(newly) + len(already)
        lines = [f"Successfully merged {total} branch(es):", ""]
        _append_split_branch_lines(lines, newly, already)
        if report.final_version:
            lines.append("")
            effective_base = report.effective_pre_merge_version or report.pre_merge_version or '?'
            lines.append(f"Version: {effective_base} -> {report.final_version}")
            if (
                report.effective_pre_merge_version
                and report.pre_merge_version
                and report.effective_pre_merge_version != report.pre_merge_version
            ):
                lines.append(
                    f"  (HEAD already at {report.pre_merge_version} from prior merges)"
                )
        if report.version_aggregation_error:
            lines.append("")
            lines.append(f"WARNING: Version aggregation failed: {report.version_aggregation_error}")
        _append_runtime_sync_lines(lines, report)
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
        reason_text = report.failure_reason or "unknown"
        lines = [
            f"CRITICAL: Git rollback failed (reason: {reason_text}).",
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
        if report.human_call_file:
            lines.append(f"Call file: {report.human_call_file}")
        if report.log_file:
            lines.append(f"Log file: {report.log_file}")
        # Defense-in-depth: runtime_sync_collisions / idempotent / discarded
        # are populated only by _sync_runtime in lenient mode after a
        # successful git merge, while rollback_failed only arises from
        # guardrail rollback errors before runtime sync runs. The two are
        # orthogonal in practice, but surfacing the full runtime-sync signal
        # set here ensures that if a future change ever makes them co-occur,
        # the output remains consistent across CLI branches.
        _append_runtime_sync_lines(lines, report)
        render_text("\n".join(lines), title="Merge Rollback Failed -- Repository May Be Corrupted")
        return 1
    elif report.pending_human:
        title, first_line = _failure_title_and_summary(
            report.failure_reason, report.pending_human
        )
        lines = [first_line, ""]
        # Defect I3: split the pre-failure merged-branches summary so
        # operators can tell which branches produced new merge commits
        # before the human-call escalation.
        newly, already = _split_merged_buckets(report)
        if newly or already:
            total = len(newly) + len(already)
            lines.append(
                f"Branches merged before pause ({total}):"
            )
            _append_split_branch_lines(lines, newly, already)
            lines.append("")
        if report.unattempted_branches:
            lines.append(
                f"Unattempted branches ({len(report.unattempted_branches)}):"
            )
            for b in report.unattempted_branches:
                lines.append(f"  - {b}")
            lines.append("")
        if report.human_call_file:
            lines.append(f"Call file: {report.human_call_file}")
        if report.log_file:
            lines.append(f"Log file: {report.log_file}")
        _append_runtime_sync_lines(lines, report)
        render_text("\n".join(lines), title=title)
        return 130  # Interrupted by user / pending human
    else:
        title, first_line = _failure_title_and_summary(
            report.failure_reason, report.pending_human
        )
        lines = [first_line, ""]
        if report.failed_branch:
            lines.append(f"Failed branch: {report.failed_branch}")
        if report.runtime_sync_collision_path:
            lines.append(
                f"Colliding path: se3/{report.runtime_sync_collision_path}"
            )
        # Only show the raw failure_reason when _failure_title_and_summary
        # fell back to the generic message (i.e. the reason has no dedicated
        # entry).  This removes the need to maintain a manual exclusion list.
        if (
            report.failure_reason
            and first_line == f"Merge failed: {report.failure_reason}."
        ):
            lines.append(f"Reason: {report.failure_reason}")
        if report.merged_branches:
            lines.append(f"Branches already merged: {', '.join(report.merged_branches)}")
        if report.unattempted_branches:
            lines.append(
                f"Unattempted branches: {', '.join(report.unattempted_branches)}"
            )
        if report.log_file:
            lines.append(f"Log file: {report.log_file}")
        _append_runtime_sync_lines(lines, report)
        render_text("\n".join(lines), title=title)
        return 1


def _failure_title_and_summary(
    failure_reason: Optional[str],
    pending_human: bool = False,
) -> tuple[str, str]:
    """Return (title, first_line) for a merge failure report.

    Distinguishes git merge conflicts from post-merge guardrail violations
    and fast-mode aborts so the user knows which category of failure
    occurred.

    Compound reasons such as ``"fast_abort: <stderr>"`` are matched by
    prefix so that diagnostic detail is not lost.
    """
    # Prefix matches first — compound reasons carry diagnostic detail
    if failure_reason and failure_reason.startswith("fast_failure"):
        detail = failure_reason[len("fast_failure"):].strip(": ")
        msg = "Merge aborted: fast strategy merge failed"
        if detail:
            msg += f" — {detail}"
        return ("Merge aborted", msg)

    if failure_reason and failure_reason.startswith("fast_abort"):
        detail = failure_reason[len("fast_abort"):].strip(": ")
        msg = "Merge aborted: fast strategy could not resolve conflict"
        if detail:
            msg += f" — {detail}"
        return ("Merge aborted", msg)

    if failure_reason and failure_reason.startswith("merge_failed"):
        detail = failure_reason[len("merge_failed"):].strip(": ")
        msg = "Merge failed: git merge operation failed"
        if detail:
            msg += f" — {detail}"
        return ("Merge failed", msg)

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
    if failure_reason == "guardrail_violation_no_rollback":
        return (
            "Merge failed",
            "Merge failed: post-merge guardrails violation (could not roll back — merge commit may still be in HEAD)",
        )
    if failure_reason == "merge_abort_failed":
        return (
            "Merge aborted",
            "Merge aborted: git merge --abort failed — working tree may still be mid-merge",
        )
    if failure_reason == "guardrail_violation_call_failed":
        return (
            "Merge failed",
            "Merge failed: post-merge guardrails violation (call file could not be written)",
        )
    if failure_reason == "guardrail_repair_stalled_call_failed":
        return (
            "Merge failed",
            "Merge failed: post-merge guardrails violation — repair stalled and call file could not be written",
        )
    if failure_reason == "guardrail_repair_exhausted_call_failed":
        return (
            "Merge failed",
            "Merge failed: post-merge guardrails violation — repair exhausted and call file could not be written",
        )
    if failure_reason == "guardrail_repair_stalled":
        return (
            "Merge paused for human review",
            "Merge paused: fast strategy could not auto-repair guardrails violation (repair stalled)",
        )
    if failure_reason == "guardrail_repair_exhausted":
        return (
            "Merge paused for human review",
            "Merge paused: fast strategy could not auto-repair guardrails violation (repair exhausted)",
        )
    if failure_reason == "human_call_write_failed":
        return (
            "Merge failed",
            "Merge failed: conflict resolution required human review, but the call file could not be written",
        )
    if failure_reason == "incomplete_resolution_call_failed":
        return (
            "Merge failed",
            "Merge failed: LLM resolution was incomplete and the call file could not be written",
        )
    if failure_reason == "guardrail_check_failed":
        return (
            "Merge aborted",
            "Merge aborted: guardrails check failed",
        )
    if failure_reason == "guardrail_check_failed_and_rollback_failed":
        return (
            "Merge aborted",
            "Merge aborted: guardrails check crashed and rollback also failed — working tree may be in an inconsistent state",
        )
    if failure_reason == "guardrail_repair_failed":
        return (
            "Merge aborted",
            "Merge aborted: fast strategy could not auto-repair guardrails violation",
        )
    if failure_reason == "conflict_context_failed":
        if pending_human:
            return (
                "Merge failed",
                "Merge failed: failed to build conflict context — paused for human review",
            )
        return (
            "Merge aborted",
            "Merge aborted: failed to build conflict context for conflict resolution",
        )
    if failure_reason == "conflict_context_failed_call_file_write_failed":
        return (
            "Merge failed",
            "Merge failed: failed to build conflict context and could not write human call file",
        )
    if failure_reason == "llm_resolution_failed":
        return (
            "Merge aborted",
            "Merge aborted: fast strategy LLM resolution failed",
        )
    if failure_reason == "incomplete_resolution":
        return (
            "Merge aborted",
            "Merge aborted: fast strategy — LLM resolution was incomplete",
        )
    if failure_reason == "resolution_rejected":
        return (
            "Merge aborted",
            "Merge aborted: fast strategy rejected the LLM resolution",
        )
    if failure_reason == "binary_file_conflict_fast_abort":
        return (
            "Merge aborted",
            "Merge aborted: fast strategy — binary file conflict cannot be auto-resolved",
        )
    if failure_reason == "binary_file_conflict":
        return (
            "Merge aborted",
            "Merge aborted: binary file conflict requires human review",
        )
    if failure_reason == "resolution_validation_failed":
        return (
            "Merge aborted",
            "Merge aborted: resolved content failed validation",
        )
    if failure_reason == "resolution_write_failed":
        return (
            "Merge aborted",
            "Merge aborted: failed to write or stage resolved files",
        )
    if failure_reason == "resolution_commit_failed":
        return (
            "Merge aborted",
            "Merge aborted: merge commit failed after resolution",
        )
    if failure_reason == "resolution_commit_timeout":
        return (
            "Merge aborted",
            "Merge aborted: conflict resolution succeeded but git commit timed out",
        )
    if failure_reason == "merge_timed_out":
        return (
            "Merge aborted",
            "Merge aborted: git merge timed out",
        )
    if failure_reason == "rollback_failed":
        return (
            "Merge failed",
            "Merge failed: git rollback failed after guardrail violation",
        )
    if failure_reason == "guardrail_missing_post_sha":
        return (
            "Merge aborted",
            "Merge aborted: guardrails check could not verify merge — post-merge commit SHA was unavailable",
        )
    if failure_reason == "guardrail_missing_pre_sha":
        return (
            "Merge aborted",
            "Merge aborted: guardrails check could not verify merge — pre-merge commit SHA was unavailable (merge commit may still be in HEAD)",
        )
    if failure_reason == "guardrail_missing_pre_and_post_sha":
        return (
            "Merge aborted",
            "Merge aborted: guardrails check could not verify merge — both pre-merge and post-merge commit SHAs were unavailable (merge commit may still be in HEAD)",
        )
    if failure_reason == "pending_human":
        return (
            "Merge paused for human review",
            "Merge paused: conflict resolution requires your decision",
        )
    if failure_reason == "runtime_sync_collision":
        return (
            "Merge failed",
            "Merge failed: runtime sync collision — a tier A file already exists in se3/. "
            "Check se3/ for the colliding file and resolve manually.",
        )
    if failure_reason == "runtime_sync_os_error":
        return (
            "Merge failed",
            "Merge failed: runtime sync OS error — check file permissions and disk space.",
        )
    if failure_reason == "runtime_sync_timeout":
        return (
            "Merge failed",
            "Merge failed: runtime sync timed out — the bound worktree may be unreachable.",
        )
    if failure_reason:
        return ("Merge failed", f"Merge failed: {failure_reason}.")
    return ("Merge failed", "Merge failed.")
