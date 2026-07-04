"""Standalone garbage collector for leaked ``se3 run --worktree`` runs.

Why this module exists
----------------------
The isolation-worktree cleanup path (branch deletion + worktree removal +
COMPLETED-state promotion) is wired ONLY into the normal end-of-flow
``_finalize_worktree_merge`` collect-and-merge step. Two real flows escape it:

* a flow that was ``pause``d and later ``resume``d completes WITHOUT
  re-triggering the finalize/merge step, so its worktree is never cleaned;
* a human who merges a branch back by hand never runs the finalize step at all.

Both leave the whole isolation worktree — including its multi-MB
``engine.json`` — stranded under ``se3/worktrees/`` indefinitely (observed:
a 50 MB ``engine.json`` sitting for 7 days, whose feature branch was never
merged back and which nobody knew about). Enough of them stacked up saturate
the daemon event loop and freeze the webui.

This GC is the safety net for that leak. It enumerates worktree runs whose
``engine.json`` is in a terminal state (COMPLETED / FAILED) and has been idle
long enough (mtime age ≥ ``max_age_seconds``), then per run: archives the
worktree, promotes its terminal state into the main archive, decides branch
safety, removes the worktree working directory, and — ONLY when the branch is
provably merged — deletes the branch. An unmerged branch's ref is ALWAYS kept
and surfaced in ``retained_unmerged`` so no unmerged work is ever silently lost
(the webui-discovery leak above was exactly such an unmerged-yet-completed
case). It reuses the already-hardened cleanup primitives (locale-pinned git,
hot/cold cold-partition promotion, archive-before-destroy gating) rather than
re-implementing them.

This is the single authoritative core shared by both trigger surfaces — the
``se3 worktree gc`` CLI command and the daemon's low-frequency periodic task.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..worktree import _run_git, has_new_commits, remove_worktree
from .cleanup import (
    _archive_worktree,
    _is_branch_ancestor_of_head,
    _promote_completed_engine_state,
    _run_git_locale,
)

logger = logging.getLogger(__name__)

# Only these two flow statuses are eligible for reclamation. A RUNNING /
# PAUSED run may still be resumed (or be mid-merge), so it is never touched;
# only a genuinely terminal run that has additionally been idle long enough
# is a candidate.
_TERMINAL_STATUSES = frozenset({"completed", "failed"})

# Default idle threshold: a terminal worktree run must have been untouched for
# at least this long before GC reclaims it, so a just-completed run that is
# waiting for a human merge or is briefly quiescent between resumes is left
# alone. mtime (a filesystem-level signal) is used rather than an in-JSON
# ``completed_at`` field because old / partially-written state may lack that
# field, whereas mtime is always present.
DEFAULT_MAX_AGE_SECONDS = 86400.0


@dataclass
class WorktreeGCReport:
    """Outcome of one ``gc_worktree_runs`` sweep.

    ``archived`` lists every worktree that was (or, under ``dry_run``, would be)
    copied into ``se3/worktrees/.archive/``; each entry is
    ``(name, archive_path, bytes)`` where ``archive_path`` is ``None`` for a
    dry run (nothing was written). ``retained_unmerged`` lists the branches
    whose refs were deliberately KEPT because they are not provably merged —
    each ``(branch, original_branch, reason)`` — and is what the daemon warns
    about ("存在 completed 但未 merge 的 worktree 分支"). ``reclaimed_bytes`` is
    the on-disk size of the removed (or, in dry run, removable) worktree
    directories. ``skipped`` / ``errors`` capture runs left untouched by choice
    or by failure respectively, each ``(name, reason)``.
    """

    archived: list[tuple[str, Optional[Path], int]] = field(default_factory=list)
    retained_unmerged: list[tuple[str, str, str]] = field(default_factory=list)
    reclaimed_bytes: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


def _dir_size(path: Path) -> int:
    """Return the total byte size of *path*'s contents (0 if it is missing).

    Symlinks are stat-ed but not followed (``lstat``) so a link's target is
    never double-counted and a broken link cannot raise. Any per-entry stat
    error is skipped rather than aborting the whole measurement — the size is
    only used for the reclaimed-space report, so a best-effort total is fine.
    """
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            fp = os.path.join(root, name)
            try:
                total += os.lstat(fp).st_size
            except OSError:
                continue
    return total


def _branch_is_merged(
    project_root: Path, branch: str, base: Optional[str]
) -> tuple[bool, str]:
    """Decide whether *branch* is safely merged into *base* (fail-closed).

    Returns ``(merged, reason)``. ``merged`` is ``True`` ONLY when we can
    positively prove the branch is contained in the base history; any
    ambiguity — git failure, missing ref, an undecidable ``None`` from the
    ancestor probe — yields ``False`` so an unmerged (or unknowable) branch is
    always retained rather than deleted. This is the core safety invariant of
    the GC: never delete a branch we cannot prove is merged.

    ``base`` defaults to ``HEAD`` when it is falsy or is not a resolvable ref
    (the worktree's recorded original branch may have since been renamed or
    deleted). Against ``HEAD`` the shared ``_is_branch_ancestor_of_head`` primitive
    is used; against any other base an equivalent ``merge-base --is-ancestor``
    probe is run through the same locale-pinned git wrapper.
    """
    resolved_base = base or "HEAD"
    if resolved_base != "HEAD":
        verify = _run_git_locale(
            project_root, "rev-parse", "--verify", "--quiet", resolved_base,
            check=False, timeout=15,
        )
        if verify.returncode != 0:
            # Original branch ref no longer resolvable — fall back to HEAD so we
            # can still make a positive merged-determination against the
            # currently checked-out integration branch instead of failing closed
            # purely because the recorded base name is stale.
            resolved_base = "HEAD"

    if resolved_base == "HEAD":
        status = _is_branch_ancestor_of_head(project_root, branch)
        if status is True:
            return True, "branch is an ancestor of HEAD (merged)"
        if status is False:
            # Corroborate with has_new_commits so the retained reason is precise
            # about whether the branch actually carries unmerged commits.
            if has_new_commits(project_root, branch, "HEAD"):
                return False, "branch has commits not in HEAD (unmerged)"
            return False, "branch is not an ancestor of HEAD (unmerged)"
        return False, "could not verify merge status against HEAD"

    result = _run_git_locale(
        project_root, "merge-base", "--is-ancestor", branch, resolved_base,
        check=False, timeout=15,
    )
    if result.returncode == 0:
        return True, f"branch is an ancestor of {resolved_base} (merged)"
    if result.returncode == 1:
        return False, f"branch is not an ancestor of {resolved_base} (unmerged)"
    # Any other exit (128 = bad ref, etc.) is undecidable → fail closed.
    return False, (
        f"could not verify merge status against {resolved_base} "
        f"(git exit {result.returncode})"
    )


def _load_worktree_header(engine_json: Path) -> Optional[dict]:
    """Read a worktree ``engine.json`` header, or ``None`` if unusable.

    A corrupt / unreadable / non-object engine.json is treated as "not a
    candidate" (returns ``None``) rather than raising — a single bad state file
    must never abort the whole sweep. The header carries the top-level identity
    keys GC needs (``status``, ``is_worktree_mode``, ``worktree_branch``,
    ``worktree_original_branch``, ``flow_id``).
    """
    try:
        raw = engine_json.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning(
            "worktree engine.json at %s is not valid JSON; skipping", engine_json,
        )
        return None
    return data if isinstance(data, dict) else None


def find_stale_worktree_runs(
    project_root: Path, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS
) -> list[dict]:
    """Enumerate terminal, over-age isolation-worktree runs eligible for GC.

    Globs ``se3/worktrees/*/se3/state/engine.json`` and keeps only entries that
    pass all three filters: the run is ``is_worktree_mode`` True, its ``status``
    is terminal (COMPLETED / FAILED), and the engine.json mtime is at least
    ``max_age_seconds`` old. Non-worktree runs, non-terminal runs, freshly
    completed runs, and corrupt/unreadable state files are excluded.

    Returns one record per eligible run with keys ``name`` (the worktree
    directory name), ``worktree_path`` (the physical directory — the glob
    parent, authoritative over any stale path recorded inside the JSON),
    ``engine_json``, ``worktree_branch``, ``worktree_original_branch``,
    ``flow_id`` and ``status``.
    """
    worktrees_root = project_root / "se3" / "worktrees"
    if not worktrees_root.is_dir():
        return []

    now = time.time()
    records: list[dict] = []
    for engine_json in sorted(worktrees_root.glob("*/se3/state/engine.json")):
        # The physical worktree directory is glob-parent[..] rather than the
        # JSON's recorded ``worktree_path`` field: a repo copied/moved on disk
        # keeps a stale absolute path in the JSON, but the file we just globbed
        # is by construction inside the real worktree dir.
        wt_path = engine_json.parents[2]

        # Never treat the ``.archive`` sibling (or any dotfile dir) as a run.
        if wt_path.name.startswith("."):
            continue

        data = _load_worktree_header(engine_json)
        if data is None:
            continue

        if not bool(data.get("is_worktree_mode", False)):
            continue

        status = str(data.get("status") or "").strip().lower()
        if status not in _TERMINAL_STATUSES:
            continue

        try:
            mtime = engine_json.stat().st_mtime
        except OSError:
            continue
        if now - mtime < max_age_seconds:
            continue

        records.append(
            {
                "name": wt_path.name,
                "worktree_path": wt_path,
                "engine_json": engine_json,
                "worktree_branch": data.get("worktree_branch"),
                "worktree_original_branch": data.get("worktree_original_branch"),
                "flow_id": data.get("flow_id"),
                "status": status,
            }
        )
    return records


def gc_worktree_runs(
    project_root: Path,
    *,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    dry_run: bool = False,
) -> WorktreeGCReport:
    """Reclaim leaked terminal worktree runs; return a :class:`WorktreeGCReport`.

    For each stale run (see :func:`find_stale_worktree_runs`) the sequence is:
    measure on-disk size → archive the worktree (``_archive_worktree``) →
    promote its terminal engine.json into the main archive
    (``_promote_completed_engine_state(force=True)``) → decide branch safety
    (``_branch_is_merged``) → remove the worktree working directory → delete the
    branch ONLY when provably merged, otherwise keep the ref and record it in
    ``retained_unmerged`` → finally ``git worktree prune``.

    The archive is a hard gate before any destructive step: if the copy fails,
    the worktree and branch are preserved and the run is recorded in ``errors``.
    Each run is isolated in its own try/except so one failure never aborts the
    sweep. With ``dry_run`` True nothing on disk is touched — the report is the
    projected accounting (merged→archived, unmerged→archived+retained) only.
    """
    report = WorktreeGCReport()
    stale = find_stale_worktree_runs(project_root, max_age_seconds)

    for record in stale:
        name = record["name"]
        wt_path: Path = record["worktree_path"]
        branch = record.get("worktree_branch")
        original = record.get("worktree_original_branch")
        try:
            if not branch:
                # Without a recorded branch we cannot make a merge-safety
                # determination, so we must not delete anything. Skip rather
                # than risk it.
                report.skipped.append(
                    (name, "no worktree_branch recorded in engine.json")
                )
                continue

            size = _dir_size(wt_path)

            if dry_run:
                merged, reason = _branch_is_merged(project_root, branch, original)
                report.archived.append((name, None, size))
                report.reclaimed_bytes += size
                if not merged:
                    report.retained_unmerged.append(
                        (branch, str(original or ""), reason)
                    )
                continue

            # Archive BEFORE any destructive op. A failure here preserves the
            # worktree + branch so an operator can fix the cause and re-run.
            try:
                archive_path = _archive_worktree(project_root, branch, wt_path)
            except (OSError, shutil.Error) as exc:
                report.errors.append(
                    (
                        name,
                        f"archive to se3/worktrees/.archive/ failed: "
                        f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            # Promote the terminal engine.json (incl. hot/cold cold partition)
            # into the main archive so the flow still surfaces as a completed
            # run in history/webui after its worktree is gone. force=True: a
            # FAILED terminal run is promoted too. Non-fatal — a promotion
            # failure must not block reclamation.
            try:
                _promote_completed_engine_state(
                    project_root, wt_path, force=True
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "state promotion for worktree run '%s' raised "
                    "(continuing GC): %s", name, exc,
                )

            merged, reason = _branch_is_merged(project_root, branch, original)

            # Remove the worktree working directory (both merged and unmerged
            # runs free their disk here; the archive is the safety copy).
            remove_worktree(project_root, wt_path)
            report.archived.append((name, archive_path, size))
            report.reclaimed_bytes += size

            if merged:
                # Safe lowercase ``-d``: even having proven the branch merged,
                # git's own fully-merged gate is a second safety net. If git
                # still refuses, keep the ref and surface it as retained.
                delete_result = _run_git_locale(
                    project_root, "branch", "-d", branch,
                    check=False, timeout=15,
                )
                if delete_result.returncode != 0:
                    stderr = delete_result.stderr.strip()
                    logger.warning(
                        "branch -d refused for merged branch '%s' "
                        "(keeping ref): %s", branch, stderr,
                    )
                    report.retained_unmerged.append(
                        (branch, str(original or ""),
                         f"git branch -d refused: {stderr}")
                    )
                else:
                    logger.info(
                        "GC deleted merged worktree branch '%s'", branch,
                    )
            else:
                # Unmerged (or undecidable): keep the branch ref and warn.
                logger.warning(
                    "Retaining unmerged worktree branch '%s' (original: %s): %s",
                    branch, original, reason,
                )
                report.retained_unmerged.append(
                    (branch, str(original or ""), reason)
                )
        except Exception as exc:  # pragma: no cover - defensive isolation
            logger.warning(
                "GC of worktree run '%s' failed (continuing): %s", name, exc,
            )
            report.errors.append((name, f"{type(exc).__name__}: {exc}"))

    # A final prune sweeps any git metadata left dangling after the removals
    # (and any pre-existing stale entries). Skipped entirely under dry_run.
    if not dry_run and stale:
        _run_git(project_root, "worktree", "prune", check=False)

    return report
