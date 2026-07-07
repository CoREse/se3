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
from typing import Callable, Optional

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

    Also renders committed-issue renumbers from the git three-way-merge
    channel: those land in a fix-up commit with no other user-visible trace,
    so a summary that omitted them would silently hide that an issue's number
    changed. They ride along in this helper (despite the "runtime sync" name)
    because it is the one rendering hook every CLI branch already calls —
    a renumber that happened before a later branch failed must stay visible
    on the failure paths too.

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
    # getattr guard: pre-typed-model report stubs in tests may lack the field.
    committed_renumbers = getattr(report, "committed_issue_renumbers", None)
    if committed_renumbers:
        lines.append("")
        lines.append(
            "Committed issue renumbers (a merged branch's issue shared its "
            "numeric ID with an existing issue; the incoming copy took a "
            "new ID and #old references were rewritten):"
        )
        for record in committed_renumbers:
            lines.append(
                f"  - #{record.old_id} -> #{record.new_id} "
                f"({record.status_dir})"
            )
    # getattr guard: pre-typed-model report stubs in tests may lack the field.
    ambiguous_refs = getattr(report, "ambiguous_issue_references", None)
    if ambiguous_refs:
        lines.append("")
        lines.append(
            "Ambiguous issue references (several merged issues shared one "
            "old ID, so these #old references could not be repointed to a "
            "single target; a note was recorded in each affected issue):"
        )
        for entry in ambiguous_refs:
            candidates = ", ".join(
                f"#{c}" for c in entry.get("candidates", [])
            )
            lines.append(
                f"  - {entry.get('file')}: #{entry.get('old_id')} "
                f"(candidates: {candidates})"
            )


def _append_human_call_lines(
    lines: list[str], report, suppress_human_call: bool
) -> None:
    """Render the human-escalation recovery artifact for a failure branch.

    In the normal (non-suppressed) mode the orchestrator wrote a real
    ``se3/calls/`` file and ``report.human_call_file`` points at it, so the
    operator can ``se3 merge respond`` against it — render that path.

    In library / suppress mode (change C: the ``se3 merge`` CLI drives no
    confirmation gate) NO call file is created — ``_RecordingNullHumanCallWriter``
    returns a phantom ``se3/calls/`` path without touching disk. Printing that
    path would tell the operator to respond to a file that does not exist. So
    here we instead render the recorded escalation payload and direct the
    operator to the actual recovery: rerun ``se3 merge`` (integrate is now a
    no-op; the failing step re-attempts). ``recorded_escalations`` is read
    defensively (a pre-typed-model report stub may lack it).
    """
    if not suppress_human_call:
        if report.human_call_file:
            lines.append(f"Call file: {report.human_call_file}")
        return

    escalations = getattr(report, "recorded_escalations", None) or []
    # Only claim a human escalation when one actually happened. A failure that
    # never escalated (postcondition/runtime-sync/branch-validation fault) has
    # no call file and no recorded escalation; printing the escalation block for
    # it would misrepresent what occurred, so we render nothing here and let the
    # caller's failure-reason lines stand alone.
    if not report.human_call_file and not escalations:
        return
    lines.append(
        "Merge escalated to human review (no call file — `se3 merge` runs "
        "without a confirmation gate)."
    )
    for esc in escalations:
        etype = esc.get("type", "escalation")
        branch = esc.get("branch", "")
        lines.append(f"  - {etype}: {branch}" if branch else f"  - {etype}")
        violations = esc.get("violations")
        if violations:
            for v in violations:
                lines.append(f"      violation: {v}")
    lines.append(
        "Recovery: resolve the reported issue, then rerun `se3 merge` "
        "(already-merged branches are skipped)."
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
) -> bool:
    """Pop the fast pre-merge stash through the shared no-data-loss path.

    Returns ``True`` when post-merge recovery did NOT finalize — the live
    stash was kept for manual recovery and/or the working tree is left with
    unmerged paths. The caller (:func:`run_merge`) must then refuse to report a
    clean success, because the merge result is layered over an unreconciled
    working tree. Returns ``False`` on a clean pop or a fully-recovered,
    dropped stash.


    Delegates the whole recovery to :func:`resolve_stashpop_safely`, the
    single implementation both merge paths (this fast strategy and the
    implement-step leaf-back merge) share so they cannot diverge. The
    invariant it enforces:

      1. ``git stash pop <ref>`` resolved by the stash's ``-m`` label (never a
         bare pop, which would target ``stash@{0}`` — possibly an unrelated
         concurrent stash). A clean pop drops the stash itself — nothing to do.
      2. On any non-clean pop the still-live stash's *entire* recoverable
         content is archived to ``se3/worktrees/.archive`` (full bytes, not
         just paths) BEFORE any disposition or drop. If that cannot be
         proven the stash is kept, not dropped.
      3. The two conflict classes are no longer lumped into one take-ours:
         case b (untracked-collision — concurrent unrelated new files such
         as the issue files discovery created in the main repo) gives way to
         the merged tree and is recovered from the archive, never destroyed;
         case a (real 3-way tracked conflict) is handed to an injected LLM
         resolver (symmetric with the merge body), falling back to take-ours
         only when the LLM is unavailable or leaves markers — with BOTH sides
         archived first so any choice is reversible. This replaces the earlier
         "LLM is intentionally NOT consulted" hard-coding, which was the very
         wrapper where data was being lost.
      4. The stash is dropped only after archival is proven.
      5. An audit issue is filed carrying the archive manifest (archive
         path + blob sha per file) so the operator can actually restore.
    """
    import time

    from ..engine.issue_manager import IssueManager
    from ..engine.stash_utils import (
        format_archived_manifest,
        pop_stash_by_label,
        resolve_stashpop_safely,
    )
    # The LLM-aware case-a resolver is injected from the merge integration
    # layer; stash_utils itself stays LLM-free and only invokes what we pass.
    from ..engine.stashpop_llm_resolver import make_llm_stashpop_resolver

    # Pop the EXACT labeled stash, never a bare ``stash pop`` (which targets
    # ``stash@{0}`` — possibly an unrelated stash pushed concurrently between
    # our pre-merge push and here). A clean pop drops the stash itself.
    pop = pop_stash_by_label(project_root, stash_label)
    if pop.returncode == 0:
        return False  # clean pop, nothing to do

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    # Case-a (real 3-way tracked) stash-pop conflicts get the same LLM-as-
    # editor treatment as the merge body; the resolver falls back to a safe
    # deterministic take-ours when the LLM is unavailable or unresolved, and
    # both discarded sides are archived first so any choice is reversible.
    outcome = resolve_stashpop_safely(
        project_root, stash_label, pop, timestamp=timestamp,
        conflict_resolver=make_llm_stashpop_resolver(
            context=(
                "`se3 merge` fast strategy: reconciling the user's "
                "pre-merge uncommitted changes with the just-merged result."
            ),
        ),
    )

    # Recovery did not finalize: the live stash was deliberately kept for
    # manual recovery (either archival could not be confirmed, or case-a
    # content WAS archived but its resolution left the index unmerged).
    # Surface that loudly AND signal the caller not to report a clean success
    # — pretending the working tree is reconciled is the very defect this path
    # guards against.
    if outcome.archive_failed:
        archived = outcome.archived
        msg = (
            f"Fast stash-pop did NOT finalize recovery (label: {stash_label}); "
            f"live stash kept for manual recovery"
            + (
                f"; {len(outcome.unresolved_files)} path(s) remain unmerged"
                if outcome.unresolved_files else ""
            )
            + "."
        )
        audit_messages.append(msg)
        render_text(msg, title="Fast Merge: Stash-Pop Recovery Incomplete")
        # Whenever content WAS archived (case-a sides captured before an
        # unresolved resolution), the audit issue must still carry the
        # manifest pointers so the operator can recover — not just be told to
        # poke at the live stash.
        unresolved_note = (
            "Unmerged paths still in the index (resolve manually):\n  - "
            + "\n  - ".join(outcome.unresolved_files)
            + "\n\n"
            if outcome.unresolved_files else ""
        )
        try:
            IssueManager(project_root).create(
                title=(
                    f"se3 merge: stash-pop recovery INCOMPLETE "
                    f"(label: {stash_label})"
                ),
                description=(
                    f"Fast strategy auto-stashed dirty working tree "
                    f"({stash_label}) before merge; on pop the recovery could "
                    f"not be finalized, so the stash was NOT dropped to avoid "
                    f"data loss. Recover it manually via `git stash list` / "
                    f"`git stash show -p`.\n\n"
                    f"{unresolved_note}"
                    f"git output:\n{pop.stdout}\n{pop.stderr}\n\n"
                    f"Content archived before the stash was kept "
                    f"(recoverable):\n"
                    + format_archived_manifest(archived)
                ),
                priority="high",
                type="task",
                tags=["merge-fallback", "stash-pop-fallback", "data-loss-risk"],
                source="system",
            )
        except Exception as exc:
            logger.warning(
                "Failed to file fast stash-pop recovery-incomplete issue: %s",
                exc,
            )
        return True

    archived = outcome.archived
    msg = (
        f"Fast stash-pop recovered safely (label: {stash_label}; "
        f"{len(outcome.case_a_files)} tracked / "
        f"{len(outcome.case_b_files)} untracked-collision; "
        f"{len(archived)} file(s) archived)."
    )
    audit_messages.append(msg)
    render_text(msg, title="Fast Merge: Stash-Pop Recovery")

    description = (
        f"Fast strategy auto-stashed dirty working tree ({stash_label}) "
        f"before merge; on pop, case-a (tracked 3-way) conflicts were "
        f"reconciled by the LLM resolver (or take-ours fallback) with BOTH "
        f"sides archived, and case-b (untracked-collision) files gave way to "
        f"the merged tree. Every discarded/colliding file's full content was "
        f"archived first and the stash dropped only after that was proven, so "
        f"each entry below is recoverable.\n\n"
        f"Recoverable archive manifest:\n"
        + format_archived_manifest(archived)
    )
    try:
        IssueManager(project_root).create(
            title=f"se3 merge: stash-pop recovery (label: {stash_label})",
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

    return False


def _map_branches_to_source_issues(
    project_root: Path, branches: list[str]
) -> dict[str, str]:
    """Capture ``{branch: source_issue_id}`` BEFORE the merge runs.

    A successful ``--delete-merged`` archives then removes each merged
    worktree, after which the ``source_issue_id`` recorded in its engine.json
    is no longer readable from a live worktree. The mapping must therefore be
    captured ahead of the orchestrator so the post-success backfill still has
    the branch→issue link. Uses a lazy import of the run.py scanner to avoid a
    circular import at module load (run.py imports from merge_cmd and vice
    versa). Best-effort: a scan failure yields no entry for that branch, never
    an exception.
    """
    from .run import find_worktree_source_issue_by_branch

    mapping: dict[str, str] = {}
    for branch in branches:
        try:
            issue_id = find_worktree_source_issue_by_branch(project_root, branch)
        except Exception:  # noqa: BLE001 - mapping capture must never fail the merge
            issue_id = None
        if issue_id:
            mapping[branch] = issue_id
    return mapping


def _backfill_resolved_source_issues(
    project_root: Path,
    merged_branches: list[str],
    branch_issue_map: dict[str, str],
) -> list[str]:
    """Resolve source issues of successfully-merged worktree branches (idempotent).

    This is the single choke point for "merge succeeded → source issue
    resolved". It serves both a worktree run's own finalizing merge (a
    ``--from-issue --worktree`` run merging back on COMPLETED) and a later
    manual ``se3 merge <leftover-branch>`` retry of a branch whose first merge
    failed — both reach a resolved issue through exactly this code.

    *merged_branches* MUST include both newly-merged AND already-ancestor
    branches. A branch classified as already-ancestor is, by definition,
    already reachable from the target branch — its commits ARE merged back.
    When such a branch's source issue is still IN_PROGRESS it means an earlier
    merge landed the commit but never ran this backfill (the merge returned
    non-zero via the stash-pop-incomplete recovery path, or the process died
    between the merge commit and the backfill). The guaranteed retry path is
    ``se3 merge <branch>``, on which the branch re-classifies as already-
    ancestor; excluding that bucket would strand the issue IN_PROGRESS with no
    automated route to RESOLVED. The IN_PROGRESS guard below keeps this safe:
    a normal repeat merge of an already-resolved branch is a no-op.

    Idempotent and best-effort: only an IN_PROGRESS issue is transitioned to
    RESOLVED (an already-resolved issue from a repeat merge, or one re-opened
    by hand, is left untouched so re-running merge never errors), and any
    IssueManager failure is swallowed so it can never change the merge exit
    code. Returns the ids actually resolved, for the caller to surface.
    """
    resolved: list[str] = []
    if not branch_issue_map:
        return resolved
    try:
        from ..engine.issue_manager import IssueManager, IssueStatus

        issue_mgr = IssueManager(project_root)
    except Exception:  # noqa: BLE001 - backfill must never fail the merge
        return resolved

    seen_issue_ids: set[str] = set()
    for branch in merged_branches:
        issue_id = branch_issue_map.get(branch)
        if not issue_id or issue_id in seen_issue_ids:
            continue
        seen_issue_ids.add(issue_id)
        try:
            issue = issue_mgr.load(issue_id)
            if issue is None or issue.status != IssueStatus.IN_PROGRESS:
                continue
            issue_mgr.update_status(issue_id, IssueStatus.RESOLVED)
            resolved.append(str(issue_id))
        except Exception:  # noqa: BLE001 - one bad issue must not fail the merge
            continue
    return resolved


def _acquire_merge_lock_with_callbacks(
    lock,
    on_lock_wait: Optional[Callable[[], None]],
    on_lock_acquired: Optional[Callable[[], None]],
) -> None:
    """Acquire *lock* (blocking), optionally surfacing a "waiting for lock" state.

    With no ``on_lock_wait`` callback this is exactly
    ``lock.acquire(blocking=True)`` — the legacy unconditional blocking acquire —
    so ``se3 merge`` (and any caller that passes no callbacks) is behaviourally
    unchanged, including the queue-and-wait semantics.

    When ``on_lock_wait`` IS provided, the lock is first probed non-blocking so
    the caller can distinguish "acquired immediately" (lock free / stale) from
    "must queue behind a live holder": only the latter fires ``on_lock_wait``
    (before blocking) and, once the block returns, ``on_lock_acquired``. A stale
    lock (dead holder) reclaims via the blocking acquire without a real wait, so
    it does NOT surface a wait state either. Callback exceptions are swallowed so
    a display/bookkeeping hook can never change the merge's exit code.
    """
    if on_lock_wait is None:
        lock.acquire(blocking=True)
        return

    from .merge.merge_lock import MergeLockBusy, MergeLockStale

    try:
        lock.acquire(blocking=False)
        return  # lock was free — acquired immediately, no wait to surface
    except MergeLockStale:
        # Holder PID is dead: the blocking acquire reclaims it without a real
        # wait, so we do not fire on_lock_wait for this path.
        lock.acquire(blocking=True)
        return
    except MergeLockBusy:
        pass  # a live holder owns it — fall through to the wait+block path

    try:
        on_lock_wait()
    except Exception:  # noqa: BLE001 - callback must never break the merge
        logger.debug("on_lock_wait callback failed", exc_info=True)
    lock.acquire(blocking=True)
    if on_lock_acquired is not None:
        try:
            on_lock_acquired()
        except Exception:  # noqa: BLE001 - callback must never break the merge
            logger.debug("on_lock_acquired callback failed", exc_info=True)


def _run_deferred_branch_cleanup(project_root: Path, report, branches: list[str]) -> None:
    """Delete the merged source branches AFTER the reconcile half has settled.

    Change B/C recovery contract: the ``se3 merge`` CLI drives
    ``integrate() -> reconcile()`` back-to-back, so branch deletion must NOT
    happen inside ``integrate()`` (as it does for a flow step, whose resume
    boundary re-runs only reconcile). Deferring it to here — the last thing a
    fully clean CLI run does — keeps the documented whole-command rerun
    recoverable: a reconcile fault leaves the source branch intact so
    ``se3 merge <branch>`` can re-attempt the version decision instead of
    failing branch validation against a branch that ``--delete-merged`` already
    removed.

    Mirrors the orchestrator's own cleanup error handling: on a partial failure
    the in-progress report is surfaced (so the operator sees which branches were
    deleted before the raise) and a synthetic entry records the aborting
    exception, rather than raising out of the CLI's success path.
    """
    from ..engine.merge.cleanup import CleanupManager, CleanupReport

    cleanup = CleanupManager(project_root)
    try:
        report.cleanup_report = cleanup.delete_merged_branches(report.merged_branches)
        report.cleanup_skipped = False
    except Exception as exc:  # noqa: BLE001 - cleanup must not break the success path
        logger.warning("Deferred branch cleanup failed: %s", exc, exc_info=True)
        partial = getattr(cleanup, "_current_report", None)
        if partial is not None:
            report.cleanup_report = partial
            partial.skipped_unknown_state.append((
                "<cleanup-aborted>",
                f"Cleanup raised {type(exc).__name__}: {exc}",
            ))
        else:
            report.cleanup_report = CleanupReport(
                skipped_not_merged=[(
                    "<cleanup-aborted>",
                    f"Cleanup raised {type(exc).__name__}: {exc}",
                )],
            )


def run_merge(
    branches: list[str],
    strategy: str = "fast",
    delete_merged: bool = True,
    strict_runtime_sync: bool = False,
    project_root: Optional[Path] = None,
    on_lock_wait: Optional[Callable[[], None]] = None,
    on_lock_acquired: Optional[Callable[[], None]] = None,
    suppress_human_call: bool = False,
) -> int:
    """Run the merge command: integrate the branches, then reconcile the version.

    This is the back-to-back ``integrate() -> reconcile()`` sequence that the
    ``se3 merge`` CLI drives (change B/C). ``integrate`` owns the merge
    invariants (lock, runtime sync, issue renumber, post-conditions);
    ``reconcile`` is the merge-side version release point — it runs
    UNCONDITIONALLY on a clean integrate, in-lock on the main checkout, and
    derives the final version from the merged-in session intents against
    master's *current* on-disk version rather than any version a session
    guessed. A plain branch merge that carries no session intents reconciles
    to a clean no-op, so behaviour is unchanged until de-versioned commits
    start emitting intents.

    Args:
        branches: List of branch names to merge (in order).
        strategy: Conflict resolution strategy.
        delete_merged: Whether to delete merged branches afterward.
        strict_runtime_sync: When True, tier A runtime sync collisions halt
            the merge sequence. When False (default), collisions are bypassed
            via sidecar files and the sequence continues.
        project_root: Project root directory. Auto-detected if None.
        suppress_human_call: When True, run the integrate half in library mode
            — the orchestrator records escalations on the returned result
            instead of writing ``se3/calls/`` files or printing terminal
            instructions. The ``se3 merge`` CLI passes True (no confirmation
            gate, failure is expressed via the exit code and the operator
            reruns the whole command). Defaults to False so the worktree
            merge-back keeps its existing call-file behaviour.
        on_lock_wait: Optional callback fired once, BEFORE blocking, when the
            main-worktree merge lock is already held by a live holder and this
            merge must queue for it. Lets a caller (the worktree merge-back)
            surface a "等待主分支锁" sub-state while it waits. Not called when the
            lock is free or stale (acquired without a real wait). Defaults to
            None, in which case the lock is acquired with the legacy
            unconditional blocking acquire and behaviour is unchanged.
        on_lock_acquired: Optional callback fired once, AFTER a contended
            blocking acquire returns (i.e. only on the path that fired
            ``on_lock_wait``), so the caller can clear the wait sub-state.

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

    # Capture {branch: source_issue_id} BEFORE the orchestrator runs: a
    # successful --delete-merged archives then removes each merged worktree,
    # after which its engine.json (holding source_issue_id) is gone. The
    # post-success backfill uses this map to resolve the source issue.
    branch_issue_map = _map_branches_to_source_issues(project_root, branches)

    # Run the orchestrator under the main-worktree mutex so that two
    # `se3 merge` invocations (and any synchronous `se3 run` holding the
    # same lock) cannot mutate the same working tree, index, and runtime
    # sync targets simultaneously (K1 / G1). The lock is acquired in
    # BLOCKING mode: a second invocation queues until the in-progress
    # holder releases it rather than failing fast — the legacy non-blocking
    # `MergeLockBusy` / `MergeLockStale` rendering below is kept only as a
    # defensive fallback (blocking acquisition does not raise them).
    from ..engine.merge import integrate
    from ..engine.merge.reconcile import (
        ReconcileError,
        ReconcileResult,
        VersionRegressionError,
        reconcile,
    )
    from ..engine.version_intent import (
        IntentReadError,
        intent_flow_ids_introduced,
    )
    from .merge.merge_lock import MergeLock, MergeLockBusy, MergeLockStale
    from .run import _resolve_main_lock_root

    # The main-worktree mutex always lives at the *main repository's*
    # ``se3/state/merge.lock``. When ``se3 merge`` runs with cwd inside a
    # linked worktree (``se3/`` is gitignored and therefore per-worktree),
    # resolve ``project_root`` back to the main repo — the same way a
    # synchronous ``se3 run`` does in ``run_flow`` — so all three
    # main-worktree-mutex acquirers contend on a single lock file. The
    # orchestrator and stash logic still operate on the original
    # ``project_root`` (the worktree being mutated); only the lock target
    # is resolved to the main repo.
    lock_root = _resolve_main_lock_root(project_root)

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
    # Set by the post-merge stash-pop below when recovery did not finalize
    # (live stash kept / index left with unmerged paths). When True the merge
    # must NOT report a clean success even if the orchestrator's report does.
    stash_pop_incomplete = False
    # Populated inside the lock by the reconcile half below. ``reconcile_result``
    # carries the final version landed at merge; ``reconcile_error`` holds a
    # version-decision fault (regression / collision / write failure) that the
    # post-lock rendering surfaces as a non-zero exit. Kept as locals so the
    # reconcile runs in-lock but is rendered after the lock releases, alongside
    # the integrate report.
    reconcile_result: Optional[ReconcileResult] = None
    reconcile_error: Optional[str] = None
    try:
        # Blocking acquisition (queue-and-wait) is unchanged when no callbacks
        # are supplied; ``_acquire_merge_lock_with_callbacks`` only adds a
        # non-blocking probe so a worktree merge-back can surface "等待主分支锁"
        # before it queues. We manage the lock explicitly (acquire + finally
        # release) instead of a ``with`` block so the acquisition can run the
        # probe-then-callback path while keeping the same release guarantee.
        lock = MergeLock(lock_root, blocking=True)
        _acquire_merge_lock_with_callbacks(lock, on_lock_wait, on_lock_acquired)
        try:
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

            # Scope the reconcile half to exactly the intents the branches THIS
            # invocation is merging INTRODUCE. Read from each branch's committed
            # tree BEFORE integrate (so ``HEAD`` is still master's pre-merge tip
            # and the branches still exist), and subtract the intents already on
            # that tip: an intent present at both the branch and pre-merge master
            # was merely INHERITED (a concurrent worktree flow that finished
            # merge_integrate but has not yet run its own version_reconcile step —
            # Flow A paused at its confirmation gate — leaves its intent
            # outstanding on master, and a branch cut from that master carries a
            # copy). Consuming Flow A's inherited intent here would commit its
            # version decision outside its step lifecycle, bypass its
            # confirmation/resume boundary, and collapse two independent releases
            # into one max bump — the exact accident the self-check flags. The
            # branch's OWN intent is never already on master (unique flow_id, its
            # reconcile has not run), so it survives the subtraction.
            #
            # Scoping by the BRANCH tree (not a pre/post-merge diff of master)
            # also preserves the documented whole-command rerun recovery: after a
            # reconcile fault the branch's own intent is still on master AND still
            # on the branch, so a rerun re-derives it (reconcile is idempotent via
            # the git-durable reconcile-commit trailer) instead of no-op'ing — a
            # pre/post diff would see the intent as "already present" on the rerun
            # and skip it.
            #
            # A git INFRASTRUCTURE fault while reading the scope must NOT degrade
            # to an empty contribution: an empty scope makes reconcile a clean
            # no-op AND lets ``--delete-merged`` remove the branch, publishing the
            # merge with no version bump / changelog for a branch that actually
            # carried an intent. So an unreadable scope is treated exactly like a
            # reconcile fault — the branches integrate, but the command reports a
            # non-zero exit and preserves the source branch so the whole-command
            # rerun re-derives the version once git recovers.
            merged_intent_flow_ids: set[str] = set()
            intent_scope_unreadable = False
            try:
                for _branch in branches:
                    merged_intent_flow_ids.update(
                        intent_flow_ids_introduced(project_root, _branch, "HEAD")
                    )
            except IntentReadError as exc:
                intent_scope_unreadable = True
                reconcile_error = (
                    "could not determine the version-intent scope for the "
                    f"merged branch(es): {exc}"
                )
                logger.error("version-intent scope determination failed: %s", exc)

            try:
                # Thin adapter: drive the SAME integrate() library entry the
                # merge_integrate step uses so the two entry points stay
                # equivalent — any invariant, return-shape, or recovery change
                # to integrate() applies to `se3 merge` too (a test patching
                # se3.engine.merge.integrate now affects both paths).
                #
                # The CLI wrapper already holds the merge lock via the
                # surrounding acquire above, so integrate must NOT re-acquire it
                # (acquire_lock=False) — fcntl.flock on a second fd of the same
                # file from the same process would surface as ``MergeLockBusy``.
                #
                # Change C: the CLI drives no confirmation gate and expresses
                # failure via the exit code (rerun the whole command). In that
                # mode the integrate half must not self-write se3/calls files or
                # print terminal instructions — an escalation surfaces on the
                # returned report instead. The worktree merge-back leaves
                # ``suppress_human_call`` False to keep its call-file behaviour.
                #
                # Branch deletion is deferred: integrate runs with
                # ``delete_merged=False`` even when the operator requested it, so
                # the source branches survive until AFTER the reconcile half
                # settles below. This preserves the whole-command rerun recovery
                # contract — a reconcile fault must not have already deleted the
                # branch out from under the documented ``se3 merge <branch>``
                # retry (which would then fail branch validation / capture an
                # empty intent scope). The actual cleanup runs post-reconcile via
                # ``_run_deferred_branch_cleanup``.
                report = integrate(
                    project_root,
                    branches,
                    strategy=strategy,
                    delete_merged=False,
                    strict_runtime_sync=strict_runtime_sync,
                    acquire_lock=False,
                    suppress_human_call=suppress_human_call,
                )
            finally:
                # Always attempt to pop the stash, even on orchestrator
                # exception, so a fast run never leaves a dangling stash
                # entry for the operator to clean up. ``_fast_stash_pop``
                # itself is best-effort — failures surface as audit
                # messages in ``stash_audit_messages`` and via the issue
                # tracker; they do not raise.
                if stash_label is not None:
                    stash_pop_incomplete = _fast_stash_pop(
                        project_root, stash_label, stash_audit_messages,
                    )

            # Reconcile the version at merge — the second half of the
            # integrate -> reconcile sequence, run in-lock on the main checkout
            # so a concurrent merge cannot bump between the two. It is
            # UNCONDITIONAL by design (it decides from the outstanding session
            # intents, not from a merge-shape heuristic), and idempotent —
            # consumed intents are marked, so rerunning the whole command after
            # a failure re-collects only outstanding intents and never
            # double-bumps. Skipped only when the integrate itself did not land
            # cleanly (a failed merge or an unfinalised stash-pop): there is no
            # settled tree to reconcile a version against.
            if report.success and not stash_pop_incomplete and not intent_scope_unreadable:
                # Restrict the reconcile to the intents INTRODUCED by THESE
                # branches (captured above from their committed trees, minus what
                # they inherited from master). An empty scope is meaningful and
                # preserved: a pure-legacy branch carrying no intent reconciles to
                # a clean no-op instead of sweeping up an unrelated in-flight
                # flow's outstanding intent. When the scope could NOT be read
                # (``intent_scope_unreadable``) this block is skipped entirely and
                # ``reconcile_error`` is already set, so the merge is NOT published
                # as clean and the branch is preserved for a rerun.
                try:
                    reconcile_result = reconcile(
                        project_root,
                        flow_ids=sorted(merged_intent_flow_ids),
                        commit=True,
                    )
                except (
                    ReconcileError,
                    VersionRegressionError,
                    IntentReadError,
                ) as exc:
                    # A version-decision fault (regression, collision, a
                    # write/commit failure) or an idempotency-probe fault that
                    # persisted across a retry (git contention on this host is a
                    # known condition). The branches are already integrated; only
                    # the version decision is unsettled, so we surface a non-zero
                    # exit and let the operator rerun rather than fail open into a
                    # possible double-bump.
                    reconcile_error = str(exc)
                    logger.error("version reconcile failed: %s", exc)

            # Deferred --delete-merged cleanup, still inside the merge lock.
            # Runs only on a fully clean outcome: the integrate landed
            # (``report.success``), the reconcile half actually RAN
            # (``not stash_pop_incomplete``), and it did not fault
            # (``reconcile_error is None``). A version-decision fault therefore
            # preserves the source branches so the documented whole-command
            # rerun can re-attempt the version decision against them — matching
            # integrate()'s own "skip cleanup unless success" rule while moving
            # the delete point past reconcile.
            #
            # The stash-pop-incomplete path must ALSO preserve the branch even
            # though its fault concerns the operator's unrelated WIP, not the
            # merged branch: that path SKIPS reconcile entirely (guard at 1295),
            # so no version-reconcile commit was created and the command returns
            # non-zero (below) advertising a whole-command rerun. Deleting the
            # branch here would make that rerun fail branch validation, stranding
            # the merged intent on master with no reconcile bump/changelog. The
            # branch survives so the operator can resolve the stash-pop, rerun
            # the command, and let reconcile settle the version against it.
            if (
                delete_merged
                and report.success
                and not stash_pop_incomplete
                and reconcile_error is None
            ):
                _run_deferred_branch_cleanup(project_root, report, branches)
        finally:
            # Mirror the previous ``with MergeLock(...)`` __exit__ — release the
            # lock on every exit path, including an orchestrator exception.
            lock.release()
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

    if report.success and stash_pop_incomplete:
        # The branch merge itself landed, but restoring the user's pre-merge
        # working tree did not finalize: the live stash was kept for manual
        # recovery and/or the index is left with unmerged paths. Reporting a
        # clean success here would advance over an unreconciled tree (the very
        # data-integrity hazard the no-data-loss recovery exists to prevent),
        # so we surface a failure that points the operator at the kept stash
        # and the archived content. ``_fast_stash_pop`` already filed the
        # detailed audit issue and archive manifest.
        lines = [
            "Branches merged, but post-merge stash-pop recovery did NOT "
            "finalize — manual intervention required.",
            "",
            "Your pre-merge uncommitted changes are preserved in the live git "
            "stash and archived under se3/worktrees/.archive; the working tree "
            "may still contain unmerged paths.",
            "",
            "Inspect and recover:",
            "  git status                 -- check for unmerged paths",
            "  git stash list             -- the kept pre-merge stash",
            "  git stash show -p stash@{0}",
        ]
        # Back-fill source-issue resolution HERE too, before the non-zero
        # return. The branch merge itself landed (``report.success``) — the
        # commits are now ancestors of the target branch — so a
        # ``--from-issue --worktree`` finalizing merge has, for source-issue
        # purposes, "merged back". The failure being reported is purely the
        # stash-pop recovery of the OPERATOR's unrelated WIP, not of the merged
        # branch. If we skipped backfill on this path the issue would strand
        # IN_PROGRESS with no automated route to RESOLVED: delete_merged has by
        # now archived the worktree and deleted the isolation branch, so the
        # documented ``se3 merge <branch>`` retry would fail at
        # ``_branch_exists`` ("branch does not exist"). Backfill is idempotent
        # and best-effort (only IN_PROGRESS issues transition; failures are
        # swallowed) so it cannot alter this path's non-zero exit code.
        newly, already = _split_merged_buckets(report, project_root)
        resolved_issue_ids = _backfill_resolved_source_issues(
            project_root, newly + already, branch_issue_map
        )
        if resolved_issue_ids:
            lines.append("")
            for issue_id in resolved_issue_ids:
                lines.append(f"Resolved source issue #{issue_id}")
        render_text(
            "\n".join(lines),
            title="Merge: Stash-Pop Recovery Incomplete",
        )
        return 1

    if report.success and reconcile_error is not None:
        # Branches integrated cleanly, but the merge-side version decision
        # failed (a regression, a collision with an already-released version,
        # or a write/commit fault). Reporting success here would land the
        # merges with a wrong or missing version — the exact class of accident
        # the reconcile redesign exists to prevent — so surface a non-zero exit.
        # Recovery is a whole-command rerun: integrate is now a no-op (the
        # branches are already ancestors) and reconcile re-collects only the
        # still-outstanding intents (idempotent, never double-bumps), so the
        # rerun re-attempts just the version decision.
        lines = [
            "Branches integrated, but version reconcile FAILED — the final "
            "version was NOT landed.",
            "",
            f"Reason: {reconcile_error}",
            "",
            "The branch merges are committed; only the version decision is "
            "unsettled. Fix the cause, then rerun `se3 merge` to re-attempt "
            "the reconcile.",
        ]
        if report.log_file:
            lines.append(f"Log file: {report.log_file}")
        render_text("\n".join(lines), title="Merge: Version Reconcile Failed")
        return 1

    if report.success:
        # The reconcile half is the authoritative version landed at merge:
        # fold its result onto the report so the "Version:" line below reflects
        # the version actually written on disk (change B/C — the merge side,
        # not any session's guess, owns the number). A no-op reconcile (no
        # session intents) leaves ``final_version`` empty and the line is
        # simply omitted, matching pre-reconcile behaviour.
        if reconcile_result is not None and reconcile_result.final_version:
            report.final_version = reconcile_result.final_version
            if not (report.effective_pre_merge_version or report.pre_merge_version):
                report.effective_pre_merge_version = reconcile_result.base_version
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
        # Backfill "merge succeeded → source issue resolved" for any branch
        # this run merged back — BOTH freshly-committed (``newly``) and
        # already-ancestor (``already``) — that carried a source_issue_id
        # (captured pre-merge above). Already-ancestor branches must be
        # included: on a retry of a leftover branch whose first merge landed
        # the commit but died before this backfill, the branch re-classifies
        # as already-ancestor, and skipping that bucket would strand its issue
        # IN_PROGRESS with no automated path to RESOLVED. Only IN_PROGRESS
        # issues transition, so this stays idempotent across repeat merges.
        resolved_issue_ids = _backfill_resolved_source_issues(
            project_root, newly + already, branch_issue_map
        )
        if resolved_issue_ids:
            lines.append("")
            for issue_id in resolved_issue_ids:
                lines.append(f"Resolved source issue #{issue_id}")
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
        _append_human_call_lines(lines, report, suppress_human_call)
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
        _append_human_call_lines(lines, report, suppress_human_call)
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
