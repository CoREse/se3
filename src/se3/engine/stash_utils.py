"""Shared helpers for ``git stash pop`` conflict recovery.

These helpers are used by both the DAG implement step (when merging leaf
branches back into the parent branch's worktree) and the ``se3 merge``
robust strategy (when stashing dirty working-tree state around a merge).
They were originally defined in ``engine.steps.implement`` and extracted
here verbatim so the two call sites share a single implementation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .worktree import _run_git


def parse_stashpop_already_exists(
    pop_result: subprocess.CompletedProcess,
) -> list[str]:
    """Extract paths from ``git stash pop``'s "already exists" output.

    When ``--include-untracked`` is stashed and a subsequent merge
    repopulates one of those paths, ``git stash pop`` emits a line like
    ``<path>: already exists, no checkout`` per affected file. Git does
    NOT mark these paths as unmerged (they aren't 3-way conflicts), so
    ``get_conflicting_files`` returns an empty list — we have to parse
    the message to know what was dropped.
    """
    files: list[str] = []
    combined = (pop_result.stdout or "") + "\n" + (pop_result.stderr or "")
    for line in combined.splitlines():
        marker = "already exists"
        if marker in line and "no checkout" in line:
            # Format (git stash pop): ``<path> already exists, no checkout``
            # Path may contain spaces, so trim everything up to the marker.
            path = line[: line.index(marker)].rstrip(": ").strip()
            if path:
                files.append(path)
    return files


def take_ours_for_stashpop(
    project_root: Path,
    conflict_files: list[str],
) -> None:
    """Resolve stash-pop conflicts by keeping the merged (HEAD) version.

    In stash-pop terminology after a conflicted apply: ``--ours`` refers
    to HEAD (our post-merge state), ``--theirs`` to the stashed content.
    We keep ours because the merge result is the canonical state we just
    landed; the stash held pre-merge artefacts whose conflict-on-the-same-
    path means the merge has authoritatively overwritten them anyway.

    Best-effort: paths where ``--ours`` fails (e.g. stash pop refused
    due to an untracked-file collision, leaving no unmerged state) are
    skipped silently; the subsequent ``git stash drop`` finalizes the
    cleanup.
    """
    for filepath in conflict_files:
        _run_git(project_root, "checkout", "--ours", "--", filepath, check=False)
        _run_git(project_root, "add", filepath, check=False)
