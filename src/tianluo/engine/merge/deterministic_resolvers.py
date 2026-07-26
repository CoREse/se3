"""Deterministic, mechanical resolvers for merge conflicts that need no LLM.

Some tracked files conflict on *every* merge by construction — they are
regenerated on both sides (``tianluo/code-index.md``) or are monotonic counters
(``tianluo/issues/.next_id``). Feeding them to the LLM conflict resolver is both
wasteful and actively harmful: a 2.5MB regenerated index produced a ~10M-char
editor prompt that blew past every agent's input limit, so the whole conflict
chain failed without a single LLM call ever running.

Such files have a *mechanical* merge rule. This module owns a registry of
path-matched resolvers that compute the merged content in pure Python, write it,
and ``git add`` it before the LLM ever sees the conflict list.

**Why a registry here and not a git merge driver.** A ``merge.<name>.driver``
must be configured via ``git config`` in every clone *and* every worktree; SE3
creates worktrees on the fly, so that configuration would have to be
distributed and kept in sync, and a driver failure's fallback semantics are
git's to decide, not ours. The merge orchestrator is the single entry point for
every SE3 merge, so hooking in here is zero-config, unit-testable, and can fall
back to the LLM path per-file. A ``.gitattributes`` driver shell can still be
layered on top of these resolvers later if plain ``git merge`` ever needs them.

**The safety invariant.** A resolver only ever *proposes* content. This module —
never the resolver — decides whether to stage it: if ``resolve`` raises, or its
output still carries conflict markers, or staging fails, the path is left
exactly as git left it (unmerged index, marker-bearing working tree) and handed
back to the LLM path. A buggy resolver can therefore degrade merge quality but
can never corrupt a merge.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Tuple

from ..code_index import (
    _MD_BULLET_RE,
    _MD_DIR_HEADING_RE,
    _MD_FILE_HEADING_RE,
    _atomic_write_text,
    _dir_of,
    _fp,
    _sha256_prefix,
    _split_fp,
)
from ..worktree import _run_git
from .conflict_resolver import _has_conflict_markers

logger = logging.getLogger(__name__)

# ``git show :<stage>:<path>`` stage numbers.
STAGE_BASE = 1
STAGE_OURS = 2
STAGE_THEIRS = 3

CODE_INDEX_RELPATHS = frozenset({"tianluo/code-index.md", "se3/code-index.md"})
# Backwards-compat single canonical value (docs/tests); matching uses the set.
CODE_INDEX_RELPATH = "tianluo/code-index.md"
NEXT_ID_RELPATHS = frozenset({"tianluo/issues/.next_id", "se3/issues/.next_id"})
NEXT_ID_RELPATH = "tianluo/issues/.next_id"


# ---------------------------------------------------------------------------
# Protocol + outcome
# ---------------------------------------------------------------------------

class DeterministicResolver(Protocol):
    """A mechanical merge rule for one class of conflicting paths."""

    name: str

    def matches(self, relpath: str) -> bool:
        """Whether this resolver owns *relpath* (repo-relative, ``/``-separated)."""

    def resolve(self, project_root: Path, relpath: str) -> str:
        """Return the merged content for *relpath*.

        An empty string means "the merge result is that this file is deleted".
        Raising signals "I cannot resolve this" — the caller then leaves the
        path unmerged for the LLM.
        """


@dataclass(frozen=True)
class DeterministicOutcome:
    """The partition of a conflict list into mechanically-resolved and the rest."""

    resolved: List[str] = field(default_factory=list)
    remaining: List[str] = field(default_factory=list)
    failures: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------

def _git_show_stage(project_root: Path, stage: int, relpath: str) -> Optional[str]:
    """Text of *relpath* at merge *stage*, or ``None`` when that stage is absent.

    A missing stage is normal, not exceptional: an add/add conflict has no base,
    a modify/delete conflict has no ours- or theirs-side blob. Callers decide
    what an absent side means for their merge rule.

    A *failure to ask git* is a different thing entirely and propagates as an
    exception: swallowing a timeout or a missing git binary into ``None`` would
    make "this side does not exist" indistinguishable from "we could not read
    this side", and a resolver would then merge a side it never saw. Raising
    sends the path to ``failures``/``remaining`` and on to the LLM instead.

    Bytes are decoded here rather than relying on ``text=True``, whose locale
    encoding would mangle a UTF-8 index on a non-UTF-8 host.
    """
    result = subprocess.run(
        ["git", "-C", str(project_root), "show", f":{stage}:{relpath}"],
        capture_output=True,
        timeout=120,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _git_add(project_root: Path, relpath: str) -> None:
    _run_git(project_root, "add", "--", relpath, check=True)


def _git_rm(project_root: Path, relpath: str) -> None:
    _run_git(project_root, "rm", "-f", "--", relpath, check=True)


# ---------------------------------------------------------------------------
# code-index.md — entry-level fingerprint union
# ---------------------------------------------------------------------------

@dataclass
class FileBlock:
    """One md entry: a file heading plus every symbol bullet under it.

    The block is the atomic unit of this merge — heading and bullets are the
    product of a single extraction pass, and are never split or grafted across
    sides, not even when neither side's fingerprint matches the post-merge file.
    Interleaving two independently regenerated bullet lists would yield a hybrid
    entry whose list fingerprint matches neither side; taking one side whole
    keeps the entry internally consistent, and the index's own self-cleaning
    step regenerates whatever went stale.
    """

    relpath: str
    heading_line: str
    bullet_lines: List[str] = field(default_factory=list)
    content_fp: Optional[str] = None
    list_fp: Optional[str] = None
    summary: Optional[str] = None


def _parse_md_blocks(text: str) -> Tuple[Dict[str, str], Dict[str, FileBlock]]:
    """Parse a rendered code-index md into ``(dir_headings, file_blocks)``.

    Heading/bullet lines are retained verbatim so a parse→render round trip is
    byte-identical: this module rearranges entries, it never re-renders their
    content. The regexes and fp splitter are imported from ``code_index`` rather
    than re-derived, so that module's ``render_full`` stays the single authority
    on the md format — if the format moves, the import site is the sync point.
    """
    dirs: Dict[str, str] = {}
    files: Dict[str, FileBlock] = {}
    current: Optional[FileBlock] = None

    for raw in text.split("\n"):
        line, content_fp, list_fp = _split_fp(raw)

        dir_match = _MD_DIR_HEADING_RE.match(line)
        if dir_match:
            dirs[dir_match.group(1)] = raw
            current = None
            continue

        file_match = _MD_FILE_HEADING_RE.match(line)
        if file_match:
            relpath = file_match.group(1)
            current = FileBlock(
                relpath=relpath,
                heading_line=raw,
                content_fp=content_fp,
                list_fp=list_fp,
                summary=file_match.group(3),
            )
            files[relpath] = current
            continue

        if current is not None and _MD_BULLET_RE.match(line):
            current.bullet_lines.append(raw)

    return dirs, files


def _render(dirs: Dict[str, str], files: Dict[str, FileBlock]) -> str:
    """Rebuild a full md in ``render_full``'s deterministic order.

    Same shape as ``code_index.render_full``: sorted dir keys, files sorted by
    relpath within their dir, one blank line after each dir heading and after
    each file block. Because every emitted line came from a heading/bullet of a
    conflict-free stage blob, the product cannot contain conflict markers.
    """
    files_by_dir: Dict[str, List[str]] = {}
    for relpath in files:
        files_by_dir.setdefault(_dir_of(relpath), []).append(relpath)

    lines: List[str] = ["# Code Index", ""]
    for dir_key in sorted(set(dirs) | set(files_by_dir)):
        lines.append(dirs.get(dir_key, f"## `{dir_key}`"))
        lines.append("")
        for relpath in sorted(files_by_dir.get(dir_key, [])):
            block = files[relpath]
            lines.append(block.heading_line)
            lines.extend(block.bullet_lines)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _actual_content_fp(project_root: Path, relpath: str) -> Optional[str]:
    """The content fingerprint of the post-merge working-tree file, or ``None``.

    Mirrors ``code_index._index_file``: the fp is the head of the sha256 over
    the raw file bytes, so it can be recomputed offline. Symbol-level shas
    cannot — they depend on structure extraction, i.e. on rerunning the whole
    index — which is why adjudication uses the file-level fp alone.
    """
    try:
        return _fp(_sha256_prefix((project_root / relpath).read_bytes()))
    except OSError:
        return None


class CodeIndexResolver:
    """Entry-level union of the two regenerated ``tianluo/code-index.md`` sides.

    Both sides rebuild the index, so a textual three-way merge conflicts on
    nearly every entry while the *entries themselves* merge trivially. Each
    entry is adjudicated on its own, and ties are broken by hashing the actual
    post-merge file the entry describes.

    Merge is not the place to rebuild the index. Where the fingerprints cannot
    settle a disagreement this resolver deliberately keeps a possibly-stale
    entry (a fixed choice of theirs) instead of failing: the index's own
    self-cleaning/rebuild step regenerates stale entries on the next run, and a
    fixed choice makes the output reproducible.
    """

    name = "code-index"

    def matches(self, relpath: str) -> bool:
        return relpath in CODE_INDEX_RELPATHS

    def resolve(self, project_root: Path, relpath: str) -> str:
        ours_text = _git_show_stage(project_root, STAGE_OURS, relpath)
        theirs_text = _git_show_stage(project_root, STAGE_THEIRS, relpath)
        if ours_text is None and theirs_text is None:
            raise ValueError(f"neither :2: nor :3: stage exists for {relpath}")

        ours_dirs, ours_files = _parse_md_blocks(ours_text or "")
        theirs_dirs, theirs_files = _parse_md_blocks(theirs_text or "")

        merged_dirs = {**ours_dirs, **theirs_dirs}  # dir conflict → theirs
        merged_files: Dict[str, FileBlock] = {}

        for entry_path in set(ours_files) | set(theirs_files):
            if not (project_root / entry_path).exists():
                # The indexed file did not survive the merge; so must not its entry.
                continue
            ours = ours_files.get(entry_path)
            theirs = theirs_files.get(entry_path)

            # Hashing the working-tree file is only worth it when the two sides
            # actually disagree about that file's content.
            actual_fp = None
            if ours is not None and theirs is not None and ours.content_fp != theirs.content_fp:
                actual_fp = _actual_content_fp(project_root, entry_path)

            block, reason = self._pick(ours, theirs, actual_fp)
            logger.debug("code-index merge: %s → %s", entry_path, reason)
            merged_files[entry_path] = block

        return _render(merged_dirs, merged_files)

    @staticmethod
    def _pick(
        ours: Optional[FileBlock],
        theirs: Optional[FileBlock],
        actual_fp: Optional[str],
    ) -> Tuple[FileBlock, str]:
        """Adjudicate one entry, returning the winner and why it won.

        ``actual_fp`` is the working-tree file's real fingerprint, and is only
        consulted when the sides carry different content fingerprints. When one
        side matches it, that side's symbol list describes the real file exactly
        and is taken whole — grafting the other side's symbols in would add
        entries for symbols the file no longer has. Every undecidable case
        resolves to theirs — see the class docstring on why a fixed,
        possibly-stale choice beats failing the merge.
        """
        if theirs is None:
            return ours, "only-ours"
        if ours is None:
            return theirs, "only-theirs"
        if ours.content_fp != theirs.content_fp:
            if actual_fp is not None and ours.content_fp == actual_fp:
                return ours, "ours-matches-worktree"
            if actual_fp is not None and theirs.content_fp == actual_fp:
                return theirs, "theirs-matches-worktree"
            return theirs, "neither-matches-worktree"
        if (ours.list_fp, ours.summary) == (theirs.list_fp, theirs.summary):
            return theirs, "identical"
        return theirs, "same-fp-different-summary"


# ---------------------------------------------------------------------------
# .next_id — monotonic counter
# ---------------------------------------------------------------------------

def _parse_counter(text: Optional[str]) -> Optional[int]:
    """A counter side as an int, or ``None`` when that side is absent or malformed."""
    try:
        return int((text or "").strip())
    except ValueError:
        return None


class NextIdResolver:
    """``tianluo/issues/.next_id`` merges to the larger counter.

    Both sides allocated issue IDs below their own counter, so only a value
    above both can be handed out again without colliding.
    """

    name = "next-id"

    def matches(self, relpath: str) -> bool:
        return relpath in NEXT_ID_RELPATHS

    def resolve(self, project_root: Path, relpath: str) -> str:
        ours = _parse_counter(_git_show_stage(project_root, STAGE_OURS, relpath))
        theirs = _parse_counter(_git_show_stage(project_root, STAGE_THEIRS, relpath))
        usable = [value for value in (ours, theirs) if value is not None]
        if not usable:
            # The staged value must come from one of the sides. With neither side
            # readable there is no such value — 0 would be a counter this merge
            # invented, and reissuing already-allocated IDs is worse than falling
            # back to the LLM.
            raise ValueError(f"neither side of {relpath} holds a usable counter")
        # One unusable side counts as 0 rather than as a failure: the surviving
        # side's counter is still an upper bound on every ID either branch handed
        # out, so it is the correct merge result on its own.
        return f"{max(usable)}\n"


REGISTRY: List[DeterministicResolver] = [CodeIndexResolver(), NextIdResolver()]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _find_resolver(relpath: str) -> Optional[DeterministicResolver]:
    for resolver in REGISTRY:
        if resolver.matches(relpath):
            return resolver
    return None


def resolve_deterministic(
    project_root: Path, conflict_paths: List[str]
) -> DeterministicOutcome:
    """Mechanically resolve every conflict path a registered resolver owns.

    Resolved paths are written and staged; every other path — unmatched, or one
    whose resolver failed — is returned untouched in ``remaining`` for the LLM,
    with its index entry still unmerged and its working tree still marker-bearing.
    ``resolved`` and ``remaining`` partition ``conflict_paths`` in input order.
    """
    outcome = DeterministicOutcome()
    for relpath in conflict_paths:
        resolver = _find_resolver(relpath)
        if resolver is None:
            outcome.remaining.append(relpath)
            continue
        try:
            _apply(project_root, resolver, relpath)
        except Exception as exc:  # a resolver bug must never abort the merge
            logger.warning(
                "deterministic resolver %s failed on %s: %s", resolver.name, relpath, exc
            )
            outcome.failures[relpath] = f"{resolver.name}: {exc}"
            outcome.remaining.append(relpath)
            continue
        logger.info("deterministic resolver %s resolved %s", resolver.name, relpath)
        outcome.resolved.append(relpath)
    return outcome


def _apply(project_root: Path, resolver: DeterministicResolver, relpath: str) -> None:
    """Write and stage *resolver*'s output, or restore the conflict and raise.

    The pre-write working-tree state is restored when staging fails, so a
    half-applied resolution never reaches the LLM fallback: it must see the
    original marker-bearing file — or, where the conflict left no working-tree
    copy at all, no file — rather than a resolved-but-unstaged one, which reads
    as an already-handled conflict while the index entry is still unmerged.
    """
    merged = resolver.resolve(project_root, relpath)
    if not merged:
        _git_rm(project_root, relpath)
        return

    if _has_conflict_markers(merged):
        raise ValueError("resolver output still contains conflict markers")

    target = project_root / relpath
    # An unreadable existing file raises here, before anything is written: the
    # path then stays untouched and falls back to the LLM, which is the safe
    # outcome.  Only a genuinely absent file may be rolled back by unlinking.
    existed = target.exists()
    original = target.read_bytes() if existed else None

    _atomic_write_text(target, merged)
    try:
        _git_add(project_root, relpath)
    except Exception:
        if existed:
            target.write_bytes(original)  # type: ignore[arg-type]
        else:
            target.unlink(missing_ok=True)
        raise
