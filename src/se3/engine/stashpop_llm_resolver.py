"""LLM-aware ``conflict_resolver`` for case-a stash-pop conflicts.

This is the LLM integration layer that sits *above* the generic, no-data-loss
:mod:`se3.engine.stash_utils` recovery engine. ``stash_utils`` itself must stay
LLM-free — it archives every stash payload before any disposition and only ever
*invokes* an injected ``conflict_resolver``; it never constructs one. This
module builds that resolver, so the LLM stack coupling (``LLMCaller``) lives
here, at the merge/implement integration boundary, and both merge paths
(``se3 merge`` fast strategy and the implement-step leaf-back merge) import the
same factory from one place rather than each wiring their own.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from .stash_utils import take_ours_for_stashpop
from .worktree import _run_git

logger = logging.getLogger(__name__)


def _path_is_unmerged(project_root: Path, rel_path: str) -> bool:
    """True while ``rel_path`` still carries an unmerged index entry.

    ``git ls-files --unmerged -- <path>`` prints one line per surviving
    conflict stage; empty output means the path is fully staged/resolved.
    The "resolved" bookkeeping is gated on THIS — not on ``git add``'s exit
    code — because a path reported resolved while still unmerged would let
    :func:`stash_utils.resolve_stashpop_safely` drop the archived stash on
    top of an unresolved conflict, reporting merge success over a still-dirty
    index. A non-zero ``ls-files`` (we cannot tell) is treated as still
    unmerged so the path falls back rather than being optimistically dropped.
    """
    result = _run_git(
        project_root, "ls-files", "--unmerged", "--", rel_path, check=False,
    )
    if result.returncode != 0:
        return True
    return bool((result.stdout or "").strip())


def llm_resolve_stashpop_conflicts(
    project_root: Path,
    conflict_files: list[str],
    *,
    flow_id: Optional[str] = None,
    step_id: Optional[str] = None,
    context: str = "",
) -> set[str]:
    """Reconcile case-a stash-pop conflicts with the LLM, WITHOUT committing.

    A stash pop restores *uncommitted* work, so — unlike the merge body — a
    resolved conflict must stay an uncommitted (staged) working-tree change,
    never a commit. For each conflicted file this reads the conflict-markered
    buffer, asks the LLM for the reconciled content, verifies the markers are
    gone, writes it back and stages it. This is the stash-pop analogue of the
    merge body's :func:`worktree.resolve_merge_conflicts_with_context`, giving
    case-a the same LLM-as-editor treatment the merge body already uses.

    Returns the set of paths actually resolved. A path is left out — handed to
    the caller's safe deterministic fallback — whenever the LLM cannot give a
    trustworthy edit: LLM unavailable, an exception, markers still present in
    the output, the markerless modify/delete shape (no buffer to edit, so the
    keep-vs-delete decision must not be made mechanically here), or staging
    that failed to clear the unmerged index entry. A path is reported resolved
    ONLY after its conflict entry is provably gone, so the caller never drops
    the stash over a still-unmerged index. Every left-out path's content is
    already archived by :func:`stash_utils.resolve_stashpop_safely`, so the
    fallback loses nothing.
    """
    # Imported lazily so the LLM stack is touched only when a case-a conflict
    # actually needs reconciling (and so tests can monkeypatch
    # ``se3.engine.llm_caller.LLMCaller``); stash_utils stays import-clean of it.
    try:
        from .llm_caller import LLMCaller
    except ImportError:
        logger.warning("LLMCaller unavailable; case-a stash-pop falls back.")
        return set()

    resolved: set[str] = set()
    for rel_path in conflict_files:
        full_path = project_root / rel_path
        try:
            content = full_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Binary or unreadable conflict — leave for deterministic fallback.
            continue
        if "<<<<<<<" not in content:
            # No textual conflict markers, yet this path is unmerged (git
            # handed it to us): the modify/delete shape, where one side deleted
            # the file and the other modified it. There is no markered buffer to
            # hand the LLM-as-editor, and staging whichever side git happened to
            # leave in the working tree would settle the keep-vs-delete decision
            # mechanically — precisely the one-size-fits-all disposition this
            # case-a path exists to avoid. Leave it UNRESOLVED so it falls back
            # through the resolver policy (take_ours_for_stashpop), which makes
            # one consistent disposition AND clears the unmerged index entry; the
            # discarded side is already archived, so the fallback loses nothing.
            continue

        prompt = (
            "You are reconciling a `git stash pop` conflict. The conflict "
            "is between the just-merged HEAD version (ours) and uncommitted "
            "local changes that were stashed before the merge (theirs). "
            "Produce the correct reconciled file that preserves both intents "
            "where possible.\n\n"
            + (f"## Context\n{context}\n\n" if context else "")
            + f"## Conflicting File: {rel_path}\n\n"
            f"```\n{content}\n```\n\n"
            "Output ONLY the fully resolved file content. Do NOT include any "
            "conflict markers (<<<<<<< / ======= / >>>>>>>). Do NOT add any "
            "explanation or code fences."
        )
        try:
            caller = LLMCaller(
                project_root,
                flow_id=flow_id,
                step_id=step_id,
                step_type="stashpop_conflict",
            )
            out = caller.call(prompt=prompt)
        except Exception as exc:
            logger.warning(
                "LLM stash-pop resolution failed for %s: %s", rel_path, exc,
            )
            continue
        if "<<<<<<<" in out or ">>>>>>>" in out:
            logger.warning(
                "LLM stash-pop output still has conflict markers for %s",
                rel_path,
            )
            continue
        try:
            full_path.write_text(out, encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Could not write LLM-resolved %s: %s", rel_path, exc,
            )
            continue
        # Count the path resolved only once its unmerged index entry is
        # provably gone. ``git add`` runs check=False, but a zero exit alone is
        # not proof the conflict cleared; reporting "resolved" while the index
        # is still unmerged would let resolve_stashpop_safely drop the archived
        # stash over an unresolved conflict. If staging did not clear the entry,
        # leave the path for the deterministic fallback (its sides are archived).
        add = _run_git(project_root, "add", "--", rel_path, check=False)
        if add.returncode == 0 and not _path_is_unmerged(project_root, rel_path):
            resolved.add(rel_path)
        else:
            logger.warning(
                "Staging LLM-resolved %s did not clear its unmerged index "
                "entry; leaving it for the deterministic fallback.", rel_path,
            )
    return resolved


def make_llm_stashpop_resolver(
    *,
    flow_id: Optional[str] = None,
    step_id: Optional[str] = None,
    context: str = "",
) -> Callable[[Path, list[str]], None]:
    """Build the case-a ``conflict_resolver`` both merge paths inject.

    The returned callable matches the ``resolve_stashpop_safely`` resolver
    contract: it must leave the working tree free of conflict markers for
    every file handed to it. It tries the LLM first (symmetric with the merge
    body) and, for whatever the LLM could not resolve, falls back to the
    deterministic :func:`stash_utils.take_ours_for_stashpop`. The fallback is
    non-destructive because the discarded side is already in the archive
    manifest ``resolve_stashpop_safely`` captured before calling this resolver.
    """

    def _resolver(project_root: Path, files: list[str]) -> None:
        resolved = llm_resolve_stashpop_conflicts(
            project_root, files, flow_id=flow_id, step_id=step_id,
            context=context,
        )
        remaining = [f for f in files if f not in resolved]
        if remaining:
            take_ours_for_stashpop(project_root, remaining)

    return _resolver
