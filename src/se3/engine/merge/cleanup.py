"""Cleanup manager for `--delete-merged` flag in `se3 merge`.

Safely deletes merged branches and their bound worktrees. Uses `git branch -d`
(lowercase) so deletion only succeeds when the branch is fully merged. Skips
protected branches (main, master, current branch) and refuses to clean dirty
worktrees.

Defects fixed in this module:

* J1 — ``git worktree list --porcelain`` parsing now validates that each
  block has both a ``worktree`` line and either a ``branch`` line or a
  ``detached`` marker; malformed blocks are logged and skipped instead of
  silently ignored.

* J2 — git commands invoked from this module run with ``LC_ALL=C`` so that
  English-only error strings (``"checked out"``, ``"not fully merged"``) can
  be matched reliably regardless of the operator's user locale.

* J3 — When ``git worktree remove`` fails after the first ``branch -d``
  rejection, the manager now still attempts a final ``branch -d`` retry
  (which is no-op-safe via the ancestor pre-check, see J4) instead of
  bailing out without trying to delete the branch at all.

* J4 — Before invoking ``git branch -d`` we first verify that the branch is
  an ancestor of HEAD via ``git merge-base --is-ancestor``. ``branch -d``
  already enforces "fully merged" inside git, but the explicit check makes
  the safety boundary visible in the report (``skipped_not_merged`` is now
  populated proactively, before any ``branch -d`` is attempted, so
  operators can see the reason without parsing locale-dependent stderr).
"""

from __future__ import annotations

import logging
import os
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


def _read_init_default_branch(project_root: Path) -> Optional[str]:
    """Return ``init.defaultBranch`` from git config, or ``None``.

    Used to dynamically widen the protected-branch set so projects that
    set ``init.defaultBranch`` to a non-default name (``develop``,
    ``trunk``, ``main-line``) automatically protect their integration
    branch from accidental ``--delete-merged`` removal.
    """
    try:
        result = _run_git_locale(
            project_root, "config", "--get", "init.defaultBranch",
            check=False, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def _load_protected_branches(project_root: Path) -> frozenset[str]:
    """Return the effective protected-branch set for this project.

    Combines:
      * The hardcoded baseline (``main``, ``master``) for repos that
        rely on git defaults without overriding them.
      * ``init.defaultBranch`` from git config (auto-detected so a repo
        that uses ``develop`` or ``trunk`` as its integration branch is
        protected without extra configuration).
      * ``merge.protected_branches`` from ``se3.yaml`` (a list of
        additional branch names operators want to protect).

    Loader failures fall back to the hardcoded baseline rather than
    failing closed: protection is defense-in-depth, but a config-load
    error MUST NOT block the cleanup (operators can always remove
    branches manually with ``git branch -d``).
    """
    protected = set(_PROTECTED_BRANCHES)
    default_branch = _read_init_default_branch(project_root)
    if default_branch:
        protected.add(default_branch)
    try:
        from ...config import load_project_yaml  # local import: avoid cycles
        data, _src = load_project_yaml(project_root)
    except (ImportError, OSError, ValueError, TypeError, AttributeError) as exc:
        logger.debug(
            "load_project_yaml failed for protected-branch lookup: %s — "
            "using baseline {main, master%s}",
            exc, f", {default_branch}" if default_branch else "",
        )
        return frozenset(protected)
    if isinstance(data, dict):
        merge_data = data.get("merge", {})
        if isinstance(merge_data, dict):
            extra = merge_data.get("protected_branches", [])
            if isinstance(extra, list):
                for entry in extra:
                    if isinstance(entry, str) and entry:
                        protected.add(entry)
    return frozenset(protected)


# Locale-independent environment for git invocations. Required for J2:
# git's user-facing stderr (e.g. "Cannot delete branch 'X' checked out at
# /path") is translated to the user's LANG/LC_MESSAGES locale, which makes
# substring matching against English keywords ("checked out", "fully
# merged") wrong on systems where these messages are localized. Forcing
# ``LC_ALL=C`` keeps stderr predictable.
def _build_locale_env() -> dict[str, str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    # LANGUAGE wins over LC_ALL on some systems; clear it explicitly.
    env.pop("LANGUAGE", None)
    return env


def _run_git_locale(
    project_root: Path,
    *args: str,
    check: bool = False,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Run a git command in the cleanup module's locale-pinned environment.

    Defect J2: cleanup parses git stderr looking for English keywords
    ("checked out", "not fully merged"). Calling git through
    ``_run_git`` which inherits the parent process's locale would let
    a localized git build emit messages we cannot match. This wrapper
    re-runs the same call with ``LC_ALL=C`` so message matching is
    deterministic.
    """
    cmd = ["git", "-C", str(project_root)] + list(args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
        stdin=subprocess.DEVNULL,
        env=_build_locale_env(),
    )


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


@dataclass
class _WorktreeRecord:
    """Parsed entry from ``git worktree list --porcelain``."""

    path: Optional[str] = None
    head: Optional[str] = None
    branch: Optional[str] = None
    detached: bool = False
    bare: bool = False
    # J7: True when the parsed ``branch`` field came from a non
    # ``refs/heads/`` reference (e.g. ``refs/remotes/origin/foo``,
    # ``refs/tags/foo``).  The cleanup machinery only operates on local
    # branches, so a record with this flag set must NOT be matched
    # against a target branch — otherwise a user-provided ref like
    # ``refs/remotes/origin/foo`` could collide with a remote-tracking
    # porcelain row and be silently treated as a bound worktree.  The
    # ``branch`` field still preserves the verbatim ref so callers can
    # render it for diagnostics.
    branch_is_non_local: bool = False


def _parse_worktree_porcelain(stdout: str) -> list[_WorktreeRecord]:
    """Parse ``git worktree list --porcelain`` output into validated records.

    Defect J1: the previous parser tracked only the current ``worktree``/
    ``branch`` pair in two ad-hoc variables and silently dropped any block
    that did not match the pattern. That means a malformed or future-version
    git porcelain layout could cause cleanup to skip a worktree without
    surfacing the parse failure. This rewrite walks block-by-block, builds a
    typed record per block, and validates that each block carries either a
    branch ref or an explicit ``detached``/``bare`` marker. Malformed blocks
    (missing ``worktree`` line, missing both ``branch`` and ``detached``,
    etc.) are logged at WARNING level and dropped — but the rest of the
    output is still returned, so a single bad row cannot mask the whole
    cleanup pass.

    The format is:

        worktree /path/to/wt
        HEAD <sha>
        branch refs/heads/<name>     OR    detached     OR    bare
        [locked [reason]]
        [prunable [reason]]
        <blank line separating records>
    """
    records: list[_WorktreeRecord] = []
    current: Optional[_WorktreeRecord] = None

    def _flush() -> None:
        nonlocal current
        if current is None:
            return
        # Validate the record: at minimum we need a path, and either a
        # branch, a detached marker, or a bare marker.
        if not current.path:
            logger.warning(
                "Dropping worktree porcelain block with no 'worktree' line: %r",
                current,
            )
            current = None
            return
        if not (current.branch or current.detached or current.bare):
            logger.warning(
                "Dropping worktree porcelain block at %r — neither 'branch' "
                "nor 'detached'/'bare' marker present (git output may have "
                "changed format)",
                current.path,
            )
            current = None
            return
        records.append(current)
        current = None

    for raw in stdout.splitlines():
        line = raw.rstrip("\r")
        if line == "":
            _flush()
            continue
        if line.startswith("worktree "):
            # Starting a new block — flush whatever was being assembled.
            _flush()
            current = _WorktreeRecord(path=line[len("worktree "):])
            continue
        if current is None:
            # Stray line outside a block; tolerate but log so format
            # changes are visible in test logs.
            logger.warning(
                "Skipping stray worktree porcelain line outside any block: %r",
                line,
            )
            continue
        if line.startswith("HEAD "):
            current.head = line[len("HEAD "):]
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            if ref.startswith("refs/heads/"):
                current.branch = ref[len("refs/heads/"):]
            else:
                # Non-``refs/heads/`` shape (e.g. ``refs/remotes/origin/foo``,
                # ``refs/tags/foo``). J7: preserve the verbatim ref for
                # diagnostic rendering, but flag the record so the
                # matching path in ``_get_worktree_path_for_branch`` skips
                # it. Cleanup only operates on local branches; without
                # the flag, a user-provided ref could collide with a
                # remote-tracking porcelain row and be silently treated
                # as a bound worktree.
                logger.warning(
                    "Worktree porcelain record points to non-`refs/heads/` "
                    "ref %r; flagging as non-local (cleanup operates only "
                    "on local branches)",
                    ref,
                )
                current.branch = ref
                current.branch_is_non_local = True
        elif line == "detached":
            current.detached = True
        elif line == "bare":
            current.bare = True
        # locked / prunable / other annotations: not needed for our use,
        # but they are part of the porcelain stream so we don't warn.
    _flush()
    return records


def _get_worktree_path_for_branch(
    project_root: Path, branch: str
) -> Optional[Path]:
    """Return the filesystem path of the worktree bound to *branch*, if any.

    Returns ``None`` when no worktree is registered for the branch.
    """
    try:
        result = _run_git_locale(
            project_root, "worktree", "list", "--porcelain",
            check=False, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning(
            "git worktree list --porcelain failed (%s) — assuming no worktree "
            "for '%s'", exc, branch,
        )
        return None
    if result.returncode != 0:
        logger.warning(
            "git worktree list --porcelain returned %d: %s",
            result.returncode, result.stderr.strip(),
        )
        return None

    records = _parse_worktree_porcelain(result.stdout)
    for record in records:
        # J7: skip records whose ``branch`` field came from a
        # non-``refs/heads/`` ref. Cleanup only operates on local
        # branches; treating a remote-tracking record as a bound
        # worktree for a user-provided branch would silently match a
        # ref the operator never intended.
        if record.branch_is_non_local:
            continue
        if record.branch == branch and record.path:
            return Path(record.path)
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
    try:
        result = subprocess.run(
            ["git", "-C", str(wt_path), "status", "--porcelain"],
            capture_output=True, text=True, check=False,
            timeout=15,
            stdin=subprocess.DEVNULL,
            env=_build_locale_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning(
            "Cannot determine cleanliness of worktree %s (%s) — treating as dirty",
            wt_path, exc,
        )
        return False
    if result.returncode != 0:
        # Treat unreadable worktree as dirty to be safe
        return False
    return not result.stdout.strip()


def _is_branch_ancestor_of_head(
    project_root: Path, branch: str
) -> Optional[bool]:
    """Return ``True`` when *branch* is an ancestor of HEAD (i.e., merged).

    Returns ``None`` when the check could not be performed (git missing,
    timeout, ref unavailable). The caller MUST treat ``None`` as
    "do not delete" — it means we cannot prove the branch is merged.

    Defect J4: ``git branch -d`` already refuses to delete unmerged
    branches, but git's stderr ("not fully merged") is locale-dependent.
    Performing the explicit ancestor check up front lets the cleanup
    report record the reason in a locale-independent way and avoids
    relying on ``"not fully merged"`` substring matching when git is
    translated.
    """
    try:
        result = _run_git_locale(
            project_root, "merge-base", "--is-ancestor", branch, "HEAD",
            check=False, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning(
            "merge-base --is-ancestor for '%s' failed (%s)", branch, exc,
        )
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        # Definitive "not an ancestor" — branch is not merged.
        return False
    # Other non-zero exit codes (128 = invalid ref, etc.) → cannot decide.
    logger.warning(
        "merge-base --is-ancestor for '%s' returned %d: %s",
        branch, result.returncode, result.stderr.strip(),
    )
    return None


class CleanupManager:
    """Delete merged branches and their bound worktrees after a successful merge."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        # Exposed so callers (orchestrator's `except Exception` handler)
        # can read the partial report when ``delete_merged_branches``
        # raises mid-way.  Without this, the report-being-built would
        # be local to the method and lost on exception, leaving the
        # operator with no record of which branches were deleted before
        # the failure.
        self._current_report: Optional[CleanupReport] = None

    def delete_merged_branches(self, branches: list[str]) -> CleanupReport:
        """Safely delete *branches* that have been merged into the current branch.

        Steps per branch (in order):

        1. Skip if branch is in ``_PROTECTED_BRANCHES`` or equals the current
           checked-out branch.
        2. Verify the branch is an ancestor of HEAD via ``merge-base
           --is-ancestor`` (J4). If the branch is NOT merged into HEAD,
           record ``skipped_not_merged`` and bail before any destructive
           ``branch -d`` attempt.
        3. If a worktree is bound to the branch, verify the worktree
           working directory is clean (no uncommitted tracked changes).
           If dirty, record a skip and do NOT delete the branch.
        4. If a worktree exists and is clean, run ``git worktree remove
           <path>`` (without ``--force`` — the clean check guarantees
           safety).
        5. Run ``git branch -d <branch>`` (lowercase ``-d``). If the branch
           is not fully merged, git rejects the deletion and we record a
           skip.

        Returns:
            CleanupReport summarising which branches were deleted, skipped
            due to dirty worktrees, or skipped because they are protected.

        Note: the in-progress report is also kept on
        ``self._current_report`` so callers can recover partial progress
        if this method raises.
        """
        report = CleanupReport()
        # Publish the in-progress report so an exception handler in the
        # caller can still see what was deleted before the raise.  The
        # cleanup loop appends to ``report`` as it goes, so reading
        # ``self._current_report`` after a partial failure yields the
        # correct snapshot of completed work.
        self._current_report = report

        try:
            current_branch_result = _run_git_locale(
                self.project_root, "rev-parse", "--abbrev-ref", "HEAD",
                check=False, timeout=15,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            # Fail closed: cannot determine current branch → abort cleanup.
            logger.error(
                "Failed to invoke git for current-branch lookup (%s). "
                "Aborting cleanup to avoid deleting the checked-out branch.",
                exc,
            )
            for branch in branches:
                report.skipped_unknown_state.append(
                    (branch, f"current branch unknown: {exc}"),
                )
            return report

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

        # G3 fix (low): widen the protected-branch set beyond the
        # hardcoded ('main', 'master') tuple. Includes git's
        # ``init.defaultBranch`` plus any names the operator listed in
        # ``merge.protected_branches`` in se3.yaml. Repositories using
        # ``develop`` / ``trunk`` / a custom default branch are now
        # protected from accidental deletion without needing extra
        # configuration when the default is set in git config.
        effective_protected = _load_protected_branches(self.project_root)

        for branch in branches:
            if branch in effective_protected:
                logger.info(
                    "Skipping protected branch '%s' (effective set: %s)",
                    branch, sorted(effective_protected),
                )
                report.skipped_protected.append(branch)
                continue

            if branch == current_branch:
                logger.info("Skipping current branch '%s'", branch)
                report.skipped_protected.append(branch)
                continue

            # Defect J4: explicit ancestor check before any destructive
            # operation. ``git branch -d`` would also catch a non-ancestor
            # branch, but the rejection is locale-dependent. Doing the
            # check here keeps the rejection reason consistent and
            # eliminates a class of "delete an unmerged branch by
            # accident" bugs that could arise from misreading git's
            # localized stderr.
            ancestor_status = _is_branch_ancestor_of_head(
                self.project_root, branch,
            )
            if ancestor_status is None:
                reason = (
                    "could not verify that branch is merged into HEAD; "
                    "refusing to delete"
                )
                logger.warning(
                    "Skipping branch '%s': %s", branch, reason,
                )
                report.skipped_unknown_state.append((branch, reason))
                continue
            if ancestor_status is False:
                reason = "branch is not an ancestor of HEAD (unmerged)"
                logger.warning(
                    "Skipping branch '%s': %s", branch, reason,
                )
                report.skipped_not_merged.append((branch, reason))
                continue

            # Check for bound worktree and its cleanliness
            has_wt = exists_for_branch(self.project_root, branch)
            wt_path: Optional[Path] = None
            if has_wt:
                wt_path = _get_worktree_path_for_branch(
                    self.project_root, branch,
                )
                if wt_path is not None:
                    if not _is_worktree_clean(wt_path):
                        reason = (
                            f"worktree {wt_path} has uncommitted changes"
                        )
                        logger.warning(
                            "Skipping branch '%s': %s", branch, reason,
                        )
                        report.skipped_dirty.append((branch, reason))
                        continue

            # Try deleting branch first (safe with lowercase -d).
            # If the branch is checked out in a worktree, git will refuse;
            # we then remove the worktree and retry.
            delete_result = _run_git_locale(
                self.project_root, "branch", "-d", branch,
                check=False, timeout=15,
            )
            if delete_result.returncode != 0:
                stderr = delete_result.stderr.strip()
                # J2: the substring matches below run under ``LC_ALL=C``,
                # so they are deterministic. We still match permissively
                # ("checked out" or "worktree") in case future git versions
                # tweak the wording slightly.
                stderr_lower = stderr.lower()
                checked_out = (
                    "checked out" in stderr_lower
                    or "worktree" in stderr_lower
                )
                if has_wt and wt_path is not None and checked_out:
                    remove_result = _run_git_locale(
                        self.project_root,
                        "worktree", "remove", str(wt_path),
                        check=False, timeout=30,
                    )
                    if remove_result.returncode != 0:
                        # Defect J3: the previous behaviour bailed out
                        # here without trying ``branch -d`` again. But
                        # ``branch -d`` is safe (only deletes merged
                        # branches), and a worktree-remove failure can
                        # be transient or driven by a stale lock. Try
                        # one more ``branch -d`` to give cleanup the
                        # best chance of success; if even that fails,
                        # surface the worktree error to the operator.
                        retry_result = _run_git_locale(
                            self.project_root, "branch", "-d", branch,
                            check=False, timeout=15,
                        )
                        if retry_result.returncode == 0:
                            logger.info(
                                "Deleted branch '%s' even though worktree "
                                "removal at %s failed: %s",
                                branch, wt_path,
                                remove_result.stderr.strip(),
                            )
                            report.deleted.append(branch)
                            if has_wt:
                                _cleanup_git_worktree_metadata(
                                    self.project_root, branch,
                                )
                            # J3 follow-up: branch deleted and metadata
                            # scrubbed, but the orphaned worktree
                            # directory on disk remains. Emit an explicit
                            # WARNING so the operator knows manual
                            # ``rm -rf`` is needed before a future
                            # ``git worktree add`` at the same path will
                            # succeed; without this, that future failure
                            # presents with no obvious cause.
                            if wt_path is not None and wt_path.exists():
                                logger.warning(
                                    "Orphan worktree directory left on disk "
                                    "for branch '%s': %s. Branch was deleted "
                                    "and .git/worktrees metadata scrubbed, "
                                    "but the directory itself could not be "
                                    "removed (worktree-remove returned: %s). "
                                    "Run `rm -rf %s` manually to reclaim the "
                                    "path; otherwise a future "
                                    "`git worktree add` at the same location "
                                    "will fail.",
                                    branch, wt_path,
                                    remove_result.stderr.strip(),
                                    wt_path,
                                )
                            continue
                        # Both worktree removal AND the retry failed —
                        # report the worktree-removal failure since it
                        # is the root cause.
                        reason = (
                            f"worktree removal failed: "
                            f"{remove_result.stderr.strip()}"
                        )
                        logger.warning(
                            "Skipping branch '%s': %s", branch, reason,
                        )
                        report.skipped_worktree_remove_failed.append(
                            (branch, reason),
                        )
                        continue
                    logger.info(
                        "Removed worktree for branch '%s': %s",
                        branch, wt_path,
                    )
                    # Retry branch deletion after successful worktree remove.
                    delete_result = _run_git_locale(
                        self.project_root, "branch", "-d", branch,
                        check=False, timeout=15,
                    )
                    if delete_result.returncode == 0:
                        report.deleted.append(branch)
                        # Scrub stale metadata just like the success-first path
                        if has_wt:
                            _cleanup_git_worktree_metadata(
                                self.project_root, branch,
                            )
                        continue
                    reason = delete_result.stderr.strip()
                else:
                    reason = stderr
                logger.warning("Failed to delete branch '%s': %s", branch, reason)
                report.skipped_not_merged.append((branch, reason))
                continue

            # Branch deleted successfully — clean up the worktree if it still exists
            if has_wt and wt_path is not None and wt_path.exists():
                remove_result = _run_git_locale(
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
