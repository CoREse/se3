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
    if _git_operation_in_progress(project_root):
        return False

    result = _run_git(
        project_root, "status", "--porcelain", "--untracked-files=no", check=False
    )
    if result.returncode != 0:
        return False
    return not result.stdout.strip()


def _git_operation_in_progress(project_root: Path) -> bool:
    """True iff git is mid-merge/cherry-pick/revert/rebase.

    These states cannot be recovered by stashing; the caller (e.g. the
    fast strategy auto-stash path) must refuse to start.
    """
    git_dir = _resolve_git_dir(project_root)
    if git_dir is None:
        return False
    markers = [
        git_dir / "MERGE_HEAD",
        git_dir / "CHERRY_PICK_HEAD",
        git_dir / "REVERT_HEAD",
        git_dir / "rebase-merge",
        git_dir / "rebase-apply",
    ]
    return any(m.exists() for m in markers)


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
        "!",
        "\\",
        '"',
        "'",
        # NOTE: `=` is intentionally NOT in this set.  Git permits ``=``
        # in branch names (e.g. ``feature/v=1.2``), and our subprocess
        # calls always use list-form argv (never a shell), so an ``=``
        # cannot be misinterpreted as a key/value separator by argparse.
        # Rejecting it would produce a misleading "shell metacharacter"
        # error for legitimately-named branches.
        "\n",
        "\r",
        "\t",
    }
)

# Glob-pattern characters that git-check-ref-format ALSO rejects ("?",
# "*", "[", "]" — see https://git-scm.com/docs/git-check-ref-format).
# Kept in a separate set from ``_BRANCH_METACHARACTERS`` so the rejection
# message says "git-ref-invalid" rather than "shell metacharacter": both
# would technically be true, but the user-facing surprise (the issue
# being addressed) is that an operator typing a git-style branch name
# gets an error mentioning shells.
_BRANCH_GIT_REF_GLOB_CHARS = frozenset({"*", "?", "[", "]"})


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
        glob_chars = sorted(
            {c for c in branch if c in _BRANCH_GIT_REF_GLOB_CHARS}
        )
        if glob_chars:
            rejected.append(
                f"{branch!r}: contains git-ref-invalid character(s) "
                f"{''.join(repr(c) for c in glob_chars)} "
                "(see git check-ref-format — `?`, `*`, `[`, `]` are not "
                "permitted in branch names)"
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
        # I4 fix: ``:``, ``~``, ``^`` are git-ref-invalid AND
        # interpreted by ``git show-ref --verify refs/heads/<name>`` as
        # revision expressions (e.g. ``foo~1`` walks parents). Without
        # rejecting them, a branch name slips past validation and
        # produces unpredictable behaviour on the existence check
        # downstream. ``?``, ``*``, ``[``, ``]`` are caught above by
        # the dedicated git-ref-invalid set so the user-facing message
        # cleanly distinguishes shell metachars from ref-invalid chars.
        if (
            ".." in branch
            or branch.startswith(".")
            or branch.startswith("/")
            or branch.endswith("/")
            or branch.endswith(".lock")
            or "@{" in branch
            or ":" in branch
            or "~" in branch
            or "^" in branch
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


def _split_merged_buckets(
    report,
    project_root: Optional[Path] = None,
) -> tuple[list[str], list[str]]:
    """Return ``(newly_merged, already_ancestor)`` from a merge report.

    Defect I3: the legacy ``merged_branches`` aggregate erased the distinction
    between branches that produced a new merge commit and branches that were
    already reachable from HEAD (a no-op). The orchestrator now populates two
    parallel lists; this helper exposes them with a defensive fallback so that
    legacy callers that fail to populate the new buckets still render
    something useful instead of an empty section.

    When *project_root* is provided, the fallback queries git ancestry so
    that already-ancestor branches are not misclassified as newly merged.

    Branches in ``merged_with_warnings`` (fast-mode guardrail repair ran)
    are included in the ``newly_merged`` bucket here because they DID
    produce a new merge commit — the bucket separation in the report is
    for downstream consumers that want to filter on repaired-vs-clean.
    """
    newly = list(getattr(report, "newly_merged_branches", []) or [])
    already = list(getattr(report, "already_ancestor_branches", []) or [])
    warnings = list(getattr(report, "merged_with_warnings", []) or [])
    # Append warnings-repaired branches to the newly-merged bucket so the
    # CLI rendering reports the correct total. The orchestrator splits
    # these out into a dedicated list so structured consumers (e.g.
    # ``to_legacy_dict``) can render the distinction; the textual CLI
    # output keeps them in the same line block to match existing UX.
    for branch in warnings:
        if branch not in newly and branch not in already:
            newly.append(branch)
    # Defensive fallback: if the orchestrator did not populate the new
    # buckets (older code path or test stub), fall back to the legacy
    # aggregate so we still render something useful rather than an empty
    # list. When project_root is available, use git merge-base to
    # correctly classify each branch instead of treating all as newly
    # merged.
    if not newly and not already and getattr(report, "merged_branches", None):
        if project_root is not None:
            for branch in report.merged_branches:
                result = subprocess.run(
                    [
                        "git", "-C", str(project_root),
                        "merge-base", "--is-ancestor", branch, "HEAD",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                )
                if result.returncode == 0:
                    already.append(branch)
                elif result.returncode == 1:
                    # rc=1 is the documented "branch is not an ancestor"
                    # signal — bucket as newly merged.
                    newly.append(branch)
                else:
                    # rc=128 (or any other unexpected rc) means git could
                    # not evaluate the relationship — typically because
                    # the branch ref no longer exists (e.g. after
                    # ``--delete-merged`` removed it). Re-classifying a
                    # missing branch as "newly merged" would mis-describe
                    # the CLI summary; leave it unbucketed instead so the
                    # caller can still surface the merged_branches list
                    # without a misleading "newly merged" label.
                    logger.debug(
                        "merge-base --is-ancestor for '%s' returned rc=%d "
                        "(branch likely missing, e.g. after --delete-merged). "
                        "Skipping bucket classification.",
                        branch, result.returncode,
                    )
        else:
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


def _has_user_uncommitted_changes(project_root: Path) -> bool:
    """True iff the user's working tree has tracked changes or untracked
    files that they themselves authored.

    Used by the fast strategy to decide whether a pre-merge stash is
    needed. Called BEFORE acquiring the merge lock so that SE3's own
    runtime files (e.g. the lock file) — which we are about to write —
    don't count as "user WIP". In production these paths are gitignored
    and ``--porcelain`` skips them; in test fixtures that omit
    ``.gitignore`` they appear untracked, so we filter explicitly.
    """
    result = _run_git(
        project_root, "status", "--porcelain", check=False,
    )
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        line = line.rstrip()
        if not line:
            continue
        # Porcelain v1: XY <space> <path>. Status code is two chars,
        # then a space, then the path.
        path = line[3:] if len(line) > 3 else ""
        if path.startswith("se3/state/") or path.startswith("se3/cache/"):
            # SE3 runtime artifacts we own; ignore for stash purposes.
            continue
        return True
    return False


def _fast_stash_dirty(
    project_root: Path,
    audit_messages: list[str],
) -> Optional[str]:
    """Stash any dirty working-tree state under the fast strategy.

    Returns the stash label string when something was actually stashed,
    or ``None`` if the tree was already clean (this should not happen
    in practice because callers gate on ``_has_user_uncommitted_changes``
    first, but the guard is kept here to make the helper safe to call
    standalone). Failures to stash surface as ``None`` plus a logged
    warning — the caller proceeds with the merge anyway (best-effort).
    """
    import time

    label = f"se3-pre-fast-merge-{int(time.time())}"
    stash = _run_git(
        project_root,
        "stash", "push", "--include-untracked",
        "-m", label,
        check=False,
    )
    if stash.returncode != 0:
        logger.warning(
            "Fast auto-stash failed (rc=%s): %s",
            stash.returncode,
            (stash.stderr or stash.stdout or "").strip(),
        )
        return None
    if "No local changes" in (stash.stdout or ""):
        # ``git stash push`` returns rc=0 even when there's nothing to
        # stash. Catch that case here so the caller doesn't try to pop
        # a stash entry that never got created. Reachable when this
        # helper is called standalone (production callers gate on
        # ``_has_user_uncommitted_changes`` first, but tests may not).
        return None

    audit_messages.append(
        f"Auto-stashed dirty working tree before fast merge "
        f"(label: {label}). Will pop after merge."
    )
    render_text(
        f"Auto-stashed dirty working tree (label: {label}).",
        title="Fast Merge: Pre-Stash",
    )
    return label


def _fast_stash_pop(
    project_root: Path,
    stash_label: str,
    audit_messages: list[str],
) -> None:
    """Pop the fast pre-merge stash, resolving conflicts deterministically.

    Steps:
      1. ``git stash pop`` (no --index; merge-style application).
      2. On 3-way conflicts (paths reported by ``get_conflicting_files``):
         take-ours (HEAD/merged version wins) — symmetric with the
         implement-step stash-pop policy. LLM is intentionally NOT
         consulted: the merged HEAD is the canonical post-merge state,
         while the stashed content was authored against an older tree
         that didn't know about the incoming branch — letting an LLM
         try to combine them risks generating incoherent output.
      3. On untracked-collision (``<path>: already exists`` lines): parse
         via ``parse_stashpop_already_exists`` and add them to the
         take-ours set.
      4. ``git stash drop`` — the working tree is by now reconciled,
         keeping the stash entry around would only confuse a future run.
      5. Audit-issue file the take-ours event so the operator can review
         what was dropped.
    """
    from ..engine.issue_manager import IssueManager
    from ..engine.stash_utils import (
        parse_stashpop_already_exists,
        take_ours_for_stashpop,
    )
    from ..engine.worktree import get_conflicting_files

    pop = _run_git(project_root, "stash", "pop", check=False)
    if pop.returncode == 0:
        return  # clean pop, nothing to do

    pop_conflict_files = get_conflicting_files(project_root)
    collision_files = parse_stashpop_already_exists(pop)
    # Union preserving order: 3-way conflicts first (their paths often
    # carry semantic value), then untracked collisions.
    seen: set[str] = set()
    affected: list[str] = []
    for f in list(pop_conflict_files) + list(collision_files):
        if f not in seen:
            seen.add(f)
            affected.append(f)

    if affected:
        take_ours_for_stashpop(project_root, affected)

    # Drop the stash regardless — leaving it behind on partial recovery
    # is worse than dropping (the operator can still see the audit issue).
    drop = _run_git(project_root, "stash", "drop", check=False)
    if drop.returncode != 0:
        logger.warning(
            "Fast stash drop failed after pop conflict (rc=%s): %s",
            drop.returncode,
            (drop.stderr or drop.stdout or "").strip(),
        )

    msg = (
        f"Fast stash-pop conflict resolved via take-ours "
        f"(label: {stash_label}; {len(affected)} affected file(s))."
    )
    audit_messages.append(msg)
    render_text(msg, title="Fast Merge: Stash-Pop Fallback")

    if affected:
        description = (
            f"Fast strategy auto-stashed dirty working tree "
            f"({stash_label}) before merge; on pop, conflicts/"
            f"collisions were resolved by keeping the merged (HEAD) "
            f"version.\n\nAffected files:\n  - "
            + "\n  - ".join(affected)
        )
    else:
        description = (
            f"Fast strategy auto-stashed dirty working tree "
            f"({stash_label}) and pop failed with no detectable "
            f"conflicts. Stash has been dropped.\n\n"
            f"git output:\n{pop.stdout}\n{pop.stderr}"
        )

    try:
        IssueManager(project_root).create(
            title=f"se3 merge: stash-pop fallback (label: {stash_label})",
            description=description,
            priority="medium",
            type="task",
            tags=["merge-fallback", "stash-pop-fallback"],
            source="system",
        )
    except Exception as exc:
        logger.warning(
            "Failed to file fast stash-pop audit issue: %s", exc,
        )


def run_merge(
    branches: list[str],
    strategy: str = "fast",
    delete_merged: bool = True,
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

    # Validate working tree is clean — non-fast strategies refuse to
    # merge a dirty tree. The fast strategy auto-stashes tracked +
    # untracked changes inside the merge lock below, restoring them
    # after the orchestrator returns. The in-progress git marker check
    # ALWAYS applies — we never want to layer a merge on top of an
    # unfinished one.
    if strategy != "fast":
        if not _is_working_tree_clean(project_root):
            render_text(
                "Working tree is not clean. Please commit or stash your "
                "changes before merging, or use --strategy=fast to "
                "auto-stash.",
                title="Merge Error",
            )
            return 1
    else:
        # Even under fast, refuse to start if a git operation (another
        # merge, cherry-pick, rebase) is already in progress — stashing
        # cannot recover from that. The marker check is a strict subset
        # of _is_working_tree_clean, but we replicate it here so fast
        # callers still get a clear error before touching git.
        if _git_operation_in_progress(project_root):
            render_text(
                "A git operation (merge/cherry-pick/rebase) is already "
                "in progress. Resolve it before running `se3 merge`.",
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

    # Run the orchestrator under the main-worktree mutex so that two
    # `se3 merge` invocations (and any synchronous `se3 run` holding the
    # same lock) cannot mutate the same working tree, index, and runtime
    # sync targets simultaneously (K1 / G1). The lock is acquired in
    # BLOCKING mode: a second invocation queues until the in-progress
    # holder releases it rather than failing fast — the legacy non-blocking
    # `MergeLockBusy` / `MergeLockStale` rendering below is kept only as a
    # defensive fallback (blocking acquisition does not raise them).
    from ..engine.merge.orchestrator import MergeOrchestrator
    from .merge.merge_lock import MergeLock, MergeLockBusy, MergeLockStale

    # Capture whether the user's tree has WIP BEFORE entering the lock.
    # The lock context writes ``se3/state/merge.lock`` (gitignored in
    # production, but not in fixture repos), so a porcelain check
    # AFTER lock acquisition would observe our own lock file as
    # untracked dirty state and spuriously trigger a stash. Doing the
    # check pre-lock observes the user's actual intent.
    needs_stash_under_fast = (
        strategy == "fast"
        and _has_user_uncommitted_changes(project_root)
    )

    stash_audit_messages: list[str] = []
    try:
        with MergeLock(project_root, blocking=True):
            # Stashing happens INSIDE the lock so two racing ``se3 merge``
            # invocations cannot interleave; the second blocks at lock
            # acquisition above and only proceeds once the first has
            # popped and released. The pre-lock dirty check is therefore
            # safe even if the second process sees a different state on
            # entry — it captures the user's pre-merge intent, not the
            # post-merge state.
            stash_label: Optional[str] = None
            if needs_stash_under_fast:
                stash_label = _fast_stash_dirty(
                    project_root, stash_audit_messages,
                )

            orchestrator = MergeOrchestrator(
                project_root=project_root,
                strategy=strategy,
                delete_merged=delete_merged,
                strict_runtime_sync=strict_runtime_sync,
                # The CLI wrapper already holds the merge lock via the
                # surrounding ``with`` block, so the orchestrator must
                # NOT re-acquire — fcntl.flock on a second fd of the
                # same file from the same process would surface as
                # ``MergeLockBusy`` (the legacy contract here was that
                # lock acquisition lived only in this wrapper).
                acquire_lock=False,
            )
            try:
                report = orchestrator.execute(branches)
            finally:
                # Always attempt to pop the stash, even on orchestrator
                # exception, so a fast run never leaves a dangling stash
                # entry for the operator to clean up. ``_fast_stash_pop``
                # itself is best-effort — failures surface as audit
                # messages in ``stash_audit_messages`` and via the issue
                # tracker; they do not raise.
                if stash_label is not None:
                    _fast_stash_pop(
                        project_root, stash_label, stash_audit_messages,
                    )
    except MergeLockBusy as exc:
        render_text(
            f"Another `se3 merge` is in progress (lock held by pid={exc.holder_pid}).\n"
            f"Lock file: {exc.lock_file}\n\n"
            f"Wait for the in-progress merge to finish before retrying.",
            title="Merge Already In Progress",
        )
        return 1
    except MergeLockStale as exc:
        if exc.holder_pid is None:
            pid_msg = "(unparseable pid)"
        else:
            pid_msg = f"(holder pid={exc.holder_pid} no longer exists)"
        render_text(
            f"Merge lock appears stale {pid_msg}.\n"
            f"Lock file: {exc.lock_file}\n\n"
            f"Remove the stale lock file and retry:\n"
            f"  rm {exc.lock_file}",
            title="Merge Lock Stale",
        )
        return 1

    if report.success:
        # Defect I3: split rendering by newly-merged vs already-ancestor.
        # Operators care about the difference: a branch in "already" was
        # already reachable from HEAD (no-op for this run), while a branch
        # in "newly" produced a fresh merge commit. Older versions of the
        # CLI lumped them together and made it impossible to tell whether
        # a re-run actually made progress or was an idempotent no-op.
        newly, already = _split_merged_buckets(report, project_root)
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
            if getattr(report, "version_higher_than_target", False):
                lines.append(
                    "  WARNING: On-disk version is HIGHER than the aggregated target. "
                    "Possible manual bump or anomalous state."
                )
        if report.version_aggregation_error:
            lines.append("")
            lines.append(f"WARNING: Version aggregation failed: {report.version_aggregation_error}")
        _append_runtime_sync_lines(lines, report)
        if report.cleanup_report:
            cr = report.cleanup_report
            lines.append("")
            if cr.archived:
                lines.append("Archived worktrees (before delete):")
                for b, archive_path in cr.archived:
                    lines.append(f"  - {b} -> {archive_path}")
            if cr.deleted:
                lines.append(f"Deleted branches: {', '.join(cr.deleted)}")
            if cr.skipped_dirty:
                lines.append("Skipped (dirty worktree):")
                for b, reason in cr.skipped_dirty:
                    lines.append(f"  - {b}: {reason}")
            if cr.skipped_archive_failed:
                lines.append(
                    "Skipped (archive failed — preserving worktree + branch):"
                )
                for b, reason in cr.skipped_archive_failed:
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
        newly, already = _split_merged_buckets(report, project_root)
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
    if failure_reason == "version_higher_than_target":
        return (
            "Merge failed",
            "Merge failed: on-disk version is higher than the aggregated target — "
            "possible manual bump or stale pre-merge version",
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
