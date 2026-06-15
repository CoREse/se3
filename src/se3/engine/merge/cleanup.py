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

import json
import logging
import os
import re
import shutil
import subprocess
import time
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
    # Archive metadata: for each branch successfully archived before
    # deletion, the path of the resulting archive directory under
    # ``<project_root>/se3/worktrees/.archive/``. The archive happens BEFORE the
    # destructive worktree-remove / branch-delete steps so a failure
    # there preserves the branch + worktree (see ``skipped_archive_failed``).
    archived: list[tuple[str, Path]] = field(default_factory=list)
    # For each branch whose worktree carried a COMPLETED ``engine.json``,
    # the path of the promoted terminal-state snapshot written into the
    # *main* project's ``se3/state/archive/engine_<flow_id>.json`` BEFORE
    # the worktree was deleted. This lets the daemon aggregator / history
    # reader report the worktree flow as ``status=completed`` like a normal
    # run, so the webui shows the unified active→completed→history lifecycle
    # (a brief "Completed" before the session drops into history) instead of
    # the flow vanishing straight into history when ``--delete-merged``
    # removes the worktree's own ``engine.json``.
    promoted_states: list[tuple[str, Path]] = field(default_factory=list)
    # When the archive step itself failed (e.g. disk full, permission
    # denied, ``shutil.copytree`` raised), the worktree-remove and
    # branch-delete steps are skipped to preserve operator data, and
    # the reason is recorded here. Operators can re-run after fixing
    # the underlying issue.
    skipped_archive_failed: list[tuple[str, str]] = field(
        default_factory=list,
    )


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


def _archive_worktree(
    project_root: Path,
    branch: str,
    wt_path: Path,
) -> Path:
    """Copy a worktree directory to ``se3/worktrees/.archive/<slug>-<ts>/``
    before it is removed by ``delete_merged_branches``.

    The archive lands inside the project's sole ignored runtime root,
    ``se3/`` (no leading dot, covered by the ``/se3/*`` gitignore rule),
    as a hidden ``.archive`` subdirectory of the existing
    ``se3/worktrees/`` workspace — so a worktree archive can never leak
    into git (the prior ``.se3/archive``落点 had no ignore rule covering
    it and is the root cause this落点 change fixes).

    ``.git`` is intentionally excluded: in linked worktrees it is a
    file containing a gitdir pointer that would not be useful in the
    archive, and the branch ref in the parent repo remains valid until
    the destructive deletion step runs (which happens only AFTER this
    function returns successfully). Untracked and gitignored files
    ARE included so any operator WIP is preserved.

    Args:
        project_root: The merge command's project root (parent of
            ``se3/worktrees/.archive/``).
        branch: The branch whose worktree is being archived (used for
            the slug + recorded in ``.se3-archive-meta.json``).
        wt_path: Absolute path to the worktree directory on disk.

    Returns:
        Path to the resulting archive directory.

    Raises:
        OSError / shutil.Error: Filesystem operation failed; the caller
            MUST treat this as a hard archive failure and refuse to
            run the destructive worktree-remove / branch-delete step.
    """
    archive_root = project_root / "se3" / "worktrees" / ".archive"
    archive_root.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^A-Za-z0-9._-]", "_", branch)
    ts = int(time.time())
    base = f"{slug}-{ts}"
    dest = archive_root / base
    seq = 0
    while dest.exists():
        seq += 1
        dest = archive_root / f"{base}-{seq}"

    shutil.copytree(
        wt_path,
        dest,
        ignore=shutil.ignore_patterns(".git"),
        dirs_exist_ok=False,
        symlinks=True,
    )

    # Capture HEAD SHA from the worktree itself so the archive metadata
    # has a self-contained recovery pointer (the branch ref in the
    # parent repo is the authoritative recovery target but is deleted
    # by the next step). ``rev-parse HEAD`` from within the worktree
    # gives the tip commit even after we have copied the files out.
    head_sha = ""
    try:
        rev = subprocess.run(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=15,
        )
        if rev.returncode == 0:
            head_sha = rev.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        # Non-fatal — record what we have.
        head_sha = ""

    meta = {
        "branch": branch,
        "worktree_path": str(wt_path),
        "head_sha": head_sha,
        "ts": ts,
    }
    try:
        (dest / ".se3-archive-meta.json").write_text(
            json.dumps(meta, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        # Meta is best-effort; the directory contents are the
        # authoritative archive. Log and continue.
        logger.warning(
            "Failed to write archive metadata for %s at %s: %s",
            branch, dest, exc,
        )

    logger.info(
        "Archived worktree for branch '%s' to %s before delete",
        branch, dest,
    )
    return dest


def _promote_completed_engine_state(
    project_root: Path,
    wt_path: Path,
) -> Optional[Path]:
    """Promote a worktree's COMPLETED ``engine.json`` into the main archive.

    A ``se3 run --worktree`` flow persists its terminal ``COMPLETED`` state
    only inside the isolation worktree at
    ``<wt_path>/se3/state/engine.json``. Once ``--delete-merged`` removes the
    worktree, that file is gone and the main project never recorded the flow's
    completion — so the daemon aggregator / history reader would only ever see
    the flow as a history-only directory (after Tier A history sync), never as
    a ``status=completed`` run, and the webui would skip the brief "Completed"
    state and drop the session straight into history.

    To restore the unified ``active → completed → history`` lifecycle this
    copies the worktree's engine.json — only when it describes a genuinely
    ``COMPLETED`` flow — into the *main* project's
    ``se3/state/archive/engine_<flow_id>.json`` (atomic write), stamping the
    main ``project_root`` so the history enumeration attributes it correctly.
    The daemon then reports it exactly like an archived normal run.

    This MUST be called BEFORE the destructive worktree-remove / branch-delete
    step so the source engine.json still exists. Returns the promoted archive
    path on success, or ``None`` when there is nothing to promote (no
    engine.json, unreadable, missing flow_id, or status is not COMPLETED).

    Failures are non-fatal: the caller treats a ``None`` / raised error as
    "nothing promoted" and proceeds with cleanup — losing the brief Completed
    chip is far less bad than blocking a merge-back cleanup.
    """
    engine_json = wt_path / "se3" / "state" / "engine.json"
    try:
        raw = engine_json.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning(
            "Worktree engine.json at %s is not valid JSON; skipping "
            "completed-state promotion", engine_json,
        )
        return None
    if not isinstance(data, dict):
        return None

    status = str(data.get("status") or "").strip().lower()
    if status != "completed":
        # Only a genuinely COMPLETED flow is promoted. A FAILED / PAUSED
        # worktree run keeps its worktree (cleanup never reaches a non-merged
        # branch), so there is nothing to promote here.
        return None

    flow_id = data.get("flow_id")
    if not flow_id:
        logger.warning(
            "Worktree engine.json at %s has no flow_id; skipping "
            "completed-state promotion", engine_json,
        )
        return None
    flow_id_str = str(flow_id)

    # Stamp the main project root so the daemon's historical-root enumeration
    # (which reads engine_*.json's ``project_root`` field) attributes the
    # promoted state to the main project, not the now-deleted worktree.
    try:
        data["project_root"] = os.path.realpath(str(project_root))
    except OSError:  # pragma: no cover - defensive
        data["project_root"] = str(project_root)

    archive_dir = project_root / "se3" / "state" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", flow_id_str)
    dest = archive_dir / f"engine_{slug}.json"

    # Atomic write: write to a temp file in the same directory, then replace.
    tmp = archive_dir / f".engine_{slug}.json.tmp"
    try:
        tmp.write_text(
            json.dumps(data, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, dest)
    except OSError as exc:
        logger.warning(
            "Failed to promote completed worktree state for flow %s to %s: %s",
            flow_id_str, dest, exc,
        )
        try:
            tmp.unlink()
        except OSError:
            pass
        return None

    logger.info(
        "Promoted COMPLETED worktree flow %s to main archive %s",
        flow_id_str, dest,
    )
    return dest


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

            # Archive the worktree BEFORE the destructive ops. A failure
            # here preserves the worktree + branch (the destructive ops
            # are skipped), so an operator can fix the underlying issue
            # (e.g. disk full) and re-run cleanup. The archive lives at
            # ``<project_root>/se3/worktrees/.archive/<slug>-<ts>/`` and includes
            # tracked + untracked + ignored files (but not ``.git`` —
            # see ``_archive_worktree``).
            if has_wt and wt_path is not None and wt_path.exists():
                try:
                    archive_path = _archive_worktree(
                        self.project_root, branch, wt_path,
                    )
                    report.archived.append((branch, archive_path))
                except (OSError, shutil.Error) as exc:
                    reason = (
                        f"archive to se3/worktrees/.archive/ failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    logger.warning(
                        "Skipping branch '%s' (preserving worktree + "
                        "branch): %s", branch, reason,
                    )
                    report.skipped_archive_failed.append((branch, reason))
                    continue

                # Promote the worktree's COMPLETED engine.json into the main
                # project's archive BEFORE the destructive worktree-remove /
                # branch-delete step below. This is what lets the daemon report
                # the worktree flow as ``status=completed`` (the brief
                # "Completed" state) instead of it vanishing straight into
                # history once ``--delete-merged`` removes the worktree. A
                # failure here is non-fatal — cleanup still proceeds.
                try:
                    promoted = _promote_completed_engine_state(
                        self.project_root, wt_path,
                    )
                    if promoted is not None:
                        report.promoted_states.append((branch, promoted))
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "Completed-state promotion for branch '%s' raised "
                        "(continuing cleanup): %s", branch, exc,
                    )

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
