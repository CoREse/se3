"""Shared helpers for ``git stash pop`` conflict recovery.

These helpers are used by both the DAG implement step (when merging leaf
branches back into the parent branch's worktree) and the ``se3 merge``
robust strategy (when stashing dirty working-tree state around a merge).
They were originally defined in ``engine.steps.implement`` and extracted
here verbatim so the two call sites share a single implementation.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .worktree import _run_git, get_conflicting_files

logger = logging.getLogger(__name__)

# Archive sink for stash content that could not be cleanly popped back into
# the working tree. It lives under ``se3/worktrees/`` which is already
# ``.gitignore``d, so archived payloads never pollute the tree they were
# rescued from. Recovery is the highest-priority invariant: nothing is ever
# ``git stash drop``ped before its content is provably persisted here.
ARCHIVE_DIR = "se3/worktrees/.archive"


@dataclass
class ArchivedEntry:
    """One file's worth of stashed content persisted to :data:`ARCHIVE_DIR`.

    ``case`` records the structural origin of the content, which lines up
    with the two conflict classes the recovery flow distinguishes:

      * ``"b"`` — untracked stash content (from ``--include-untracked``).
        These are concurrent, unrelated new files; an untracked-collision
        on pop is not a semantic conflict.
      * ``"a"`` — tracked working-tree changes carried in the stash, the
        side that can land in a real 3-way conflict on pop.

    ``archive_path`` is project-root-relative so an operator reading the
    audit issue can locate the file directly; ``blob_sha`` lets them verify
    the recovered bytes against the original git object.

    ``side`` records *which version of a conflicting path* this entry holds,
    so that for a case-a 3-way conflict BOTH sides are independently
    recoverable no matter which one the resolver ends up discarding:

      * ``"stashed"`` — the content carried in the stash (theirs). This is
        the only side untracked (case-b) collisions ever have.
      * ``"head"`` — the merged working-tree / HEAD version (ours) of a
        case-a path, captured before resolution. Without this, a resolver
        that keeps the stashed side would silently discard the HEAD content
        with nothing to restore from.
    """

    rel_path: str
    archive_path: str
    blob_sha: str
    case: str
    side: str = "stashed"


def _resolve_stash_ref(project_root: Path, label: str) -> Optional[str]:
    """Resolve the live ``stash@{N}`` ref whose message equals ``label``.

    A failed ``git stash pop`` leaves the stash entry in place; to extract
    its content we need the exact ref. Indexing blindly at ``stash@{0}``
    is unsafe: concurrent merges (or any other code) may have pushed other
    stashes on top, so we match by the message we stored with ``-m`` rather
    than by position.

    ``git stash list`` prints ``stash@{N}: On <branch>: <message>`` (or
    ``WIP on <branch>: ...`` for unlabelled stashes). We isolate the
    message after the second ``": "`` and require an exact match so an
    unlabelled or differently-labelled concurrent stash never matches.
    Returns ``None`` when no entry matches — the caller must then refuse to
    drop and warn instead.
    """
    result = _run_git(project_root, "stash", "list", check=False)
    if result.returncode != 0:
        return None
    for line in (result.stdout or "").splitlines():
        ref, sep, after_ref = line.partition(": ")
        if not sep or not ref.startswith("stash@{"):
            continue
        # ``after_ref`` is ``On <branch>: <message>`` (or ``WIP on ...``);
        # the branch segment never contains ``": "``, so the second split
        # yields exactly the message we passed via ``-m``.
        _, msg_sep, message = after_ref.partition(": ")
        if msg_sep and message == label:
            return ref.strip()
    return None


def pop_stash_by_label(
    project_root: Path, stash_label: str,
) -> subprocess.CompletedProcess:
    """``git stash pop`` the entry whose message equals ``stash_label``.

    A bare ``git stash pop`` always applies ``stash@{0}``. If a concurrent
    process (or user) pushes an unrelated stash between SE3's pre-merge
    ``git stash push -m <label>`` and this pop, ``stash@{0}`` is no longer
    SE3's stash: a bare pop would apply — and, if clean, *drop* — the unrelated
    entry while SE3's labeled stash is silently never restored. Resolving the
    label to its exact ``stash@{N}`` first guarantees we pop precisely the
    stash we pushed, which is the same labeled entry the downstream recovery
    (:func:`resolve_stashpop_safely`) archives and drops.

    Returns the completed ``git stash pop <ref>`` process so callers branch on
    ``returncode`` exactly as they did for the bare pop. When the label does
    not resolve to a live stash (it was never pushed, or a concurrent process
    already removed it), returns a synthetic non-zero result instead of falling
    back to a bare pop — escalating into the no-data-loss recovery path rather
    than silently popping an unrelated stash.
    """
    ref = _resolve_stash_ref(project_root, stash_label)
    if ref is None:
        return subprocess.CompletedProcess(
            args=["git", "stash", "pop"],
            returncode=1,
            stdout="",
            stderr=(
                f"se3: no live stash matches label {stash_label!r}; refusing "
                f"to pop stash@{{0}} (it may be an unrelated concurrent stash)."
            ),
        )
    return _run_git(project_root, "stash", "pop", ref, check=False)


def _git_show_bytes(
    project_root: Path, spec: str, *, timeout: int = 30
) -> Optional[bytes]:
    """Return the raw bytes of ``git show <spec>`` (e.g. ``stash@{0}^3:p``).

    Bytes — not text — so binary blobs round-trip intact and the archived
    file hashes back to the original git object. Returns ``None`` if the
    object cannot be read.
    """
    cmd = ["git", "-C", str(project_root), "show", spec]
    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _sanitize_label(label: str) -> str:
    """Make ``label`` safe as a single filesystem path segment.

    Leaf-merge labels embed the branch name (e.g. ``impl/2026...``); the
    embedded ``/`` would otherwise spill the archive across directories.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", label)


def _archive_run_rel(stash_label: str, timestamp: str) -> str:
    """Project-root-relative *base* dir name for a recovery run's payload.

    Both the stashed-side enumeration and the case-a HEAD-side capture must
    land under the *same* run dir so an operator finds every recoverable
    version of a single recovery in one place; that shared dir is chosen once
    by :func:`_prepare_unique_archive_run` (which may add a ``-N`` suffix to
    this base on collision) and threaded into both.
    """
    return (Path(ARCHIVE_DIR)
            / f"{timestamp}_{_sanitize_label(stash_label)}").as_posix()


def _prepare_unique_archive_run(
    project_root: Path, stash_label: str, timestamp: str,
) -> str:
    """Claim a fresh, never-reused run dir for one recovery's archived payload.

    The base name is ``<timestamp>_<label>`` (second-resolution timestamp), so
    two recoveries of the same label within one wall-clock second — e.g.
    repeated leaf-merge attempts for the same branch label — would otherwise
    resolve to the *same* dir and :func:`_archive_blob`'s ``write_bytes`` would
    overwrite the first recovery's archived content, leaving the first audit
    issue pointing at bytes that no longer match its lost stash. To keep every
    recovery's payload independently recoverable we ``mkdir`` the run dir with
    ``exist_ok=False`` to atomically claim it, falling back to ``<base>-2``,
    ``<base>-3``, … on collision (the atomic create also closes the race
    between two concurrent recoveries probing the same name). The first
    recovery for a given second keeps the plain ``<base>`` name.
    """
    base = _archive_run_rel(stash_label, timestamp)
    candidate = base
    n = 2
    while True:
        try:
            (project_root / candidate).mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            candidate = f"{base}-{n}"
            n += 1


def _archive_blob(
    project_root: Path,
    *,
    spec: str,
    rel_path: str,
    dest_rel: str,
    archive_run_rel: str,
    case: str,
    side: str,
) -> ArchivedEntry:
    """Persist a single git object (``git show <spec>``) into the run dir.

    ``dest_rel`` is the path *within* the run dir to write to — usually the
    original ``rel_path`` (stashed side), but a side-namespaced variant for
    the HEAD side so the two versions of one conflicting path never collide
    on disk. Raises ``RuntimeError`` on any read/resolve/write failure so a
    caller relying on this as recovery proof refuses to drop.
    """
    content = _git_show_bytes(project_root, spec)
    if content is None:
        raise RuntimeError(
            f"Cannot archive stash payload: failed to read {spec} "
            f"(refusing to drop without recovery proof)."
        )
    sha_result = _run_git(project_root, "rev-parse", spec, check=False)
    if sha_result.returncode != 0:
        raise RuntimeError(
            f"Cannot archive stash payload: failed to resolve blob sha "
            f"for {spec}."
        )
    blob_sha = (sha_result.stdout or "").strip()

    dest = project_root / archive_run_rel / dest_rel
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot archive stash payload: failed to write {dest}: {exc} "
            f"(refusing to drop without recovery proof)."
        ) from exc

    return ArchivedEntry(
        rel_path=rel_path,
        archive_path=(Path(archive_run_rel) / dest_rel).as_posix(),
        blob_sha=blob_sha,
        case=case,
        side=side,
    )


def archive_stash_payload(
    project_root: Path,
    stash_label: str,
    *,
    timestamp: str,
    archive_run_rel: Optional[str] = None,
) -> list[ArchivedEntry]:
    """Persist a live stash's full content to :data:`ARCHIVE_DIR`.

    Resolves the stash entry by ``stash_label`` (it must still exist — call
    this *before* ``git stash drop``), then enumerates everything it holds:

      * untracked files via ``git ls-tree -r --name-only <ref>^3``
        (the ``^3`` parent exists only for ``--include-untracked`` stashes);
      * tracked working-tree changes via
        ``git diff --name-only --diff-filter=d <ref>^1 <ref>``
        (``--diff-filter=d`` excludes paths the stash deleted — they hold
        no content to recover, so they must not be archived).

    Each file's complete content is read with ``git show`` (raw bytes) and
    written to ``ARCHIVE_DIR/<timestamp>_<label>/<original rel path>``,
    preserving the original directory structure. The returned manifest is
    the recovery proof the caller checks before it is allowed to drop.

    Raises:
        RuntimeError: if the stash ref cannot be resolved, or if any file's
            content cannot be read or written. Surfacing the failure (rather
            than silently skipping) is deliberate: the caller must NOT drop
            the stash when payload persistence is not provably complete.
    """
    ref = _resolve_stash_ref(project_root, stash_label)
    if ref is None:
        raise RuntimeError(
            f"Cannot archive stash payload: no live stash matches label "
            f"{stash_label!r} (refusing to proceed without recovery proof)."
        )

    # Claim a unique run dir (resolving the ref first so a missing label never
    # leaves a stray empty dir behind). The caller — ``resolve_stashpop_safely``
    # — passes the dir it already claimed so the stashed-side payload and the
    # case-a HEAD-side capture share one run dir; a standalone caller gets a
    # freshly-claimed, collision-free dir of its own.
    if archive_run_rel is None:
        archive_run_rel = _prepare_unique_archive_run(
            project_root, stash_label, timestamp,
        )

    # (source ref, case) per structural origin. ``^3`` (untracked) only
    # exists for ``--include-untracked`` stashes; both lookups are
    # best-effort (check=False) since either side may legitimately be empty.
    untracked = _run_git(
        project_root, "ls-tree", "-r", "--name-only", f"{ref}^3", check=False,
    )
    # ``--diff-filter=d`` (lowercase = exclude) drops paths the stash
    # *deleted* relative to its base: they have no content at ``<ref>:<path>``
    # to read back, so a deletion is nothing recoverable — archiving it would
    # spuriously fail (``git show`` returns no object) and wrongly block the
    # drop. We only archive paths that still have content in the stash.
    tracked = _run_git(
        project_root, "diff", "--name-only", "--diff-filter=d",
        f"{ref}^1", ref, check=False,
    )

    # Map rel_path -> (source spec ref, case). Untracked first; a path that
    # is somehow both keeps its untracked origin (the unrelated-new-file
    # semantics of case "b").
    sources: dict[str, tuple[str, str]] = {}
    if tracked.returncode == 0:
        for path in (tracked.stdout or "").splitlines():
            if path:
                sources[path] = (ref, "a")
    if untracked.returncode == 0:
        for path in (untracked.stdout or "").splitlines():
            if path:
                sources[path] = (f"{ref}^3", "b")

    entries: list[ArchivedEntry] = []
    for rel_path, (src_ref, case) in sorted(sources.items()):
        entries.append(
            _archive_blob(
                project_root,
                spec=f"{src_ref}:{rel_path}",
                rel_path=rel_path,
                dest_rel=rel_path,
                archive_run_rel=archive_run_rel,
                case=case,
                side="stashed",
            )
        )
    return entries


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

    This is now the *default* (no-resolver) fallback for case-a (real
    3-way tracked) conflicts inside :func:`resolve_stashpop_safely`. It is
    only safe because the discarded ``--theirs`` side has already been
    persisted to :data:`ARCHIVE_DIR` before this runs, so keeping ours is
    no longer a destructive, unrecoverable choice — it is a reversible
    default the operator can override from the archive.

    Best-effort: paths where ``--ours`` fails (e.g. stash pop refused
    due to an untracked-file collision, leaving no unmerged state) are
    skipped silently; the subsequent ``git stash drop`` finalizes the
    cleanup.
    """
    for filepath in conflict_files:
        _run_git(project_root, "checkout", "--ours", "--", filepath, check=False)
        _run_git(project_root, "add", filepath, check=False)


def _archive_case_a_head_side(
    project_root: Path,
    case_a_files: list[str],
    *,
    archive_run_rel: str,
) -> list[ArchivedEntry]:
    """Archive the merged HEAD (ours) version of each case-a conflict path.

    Called *before* a case-a resolution runs. ``archive_stash_payload`` only
    captures the stashed (theirs) side; if an injected resolver keeps the
    stashed version — or writes any resolution that drops the HEAD content —
    the merged side would be unrecoverable. Capturing ``:2:<path>`` (the
    ours/HEAD stage of the still-conflicted index) here guarantees *both*
    sides of a tracked 3-way conflict are in the manifest regardless of which
    one the resolver discards.

    A path with no stage-2 entry (HEAD has no version of it — e.g. the file
    exists only in the stash) is skipped: there is no HEAD content to lose.
    Any other read failure raises, so the no-data-loss contract is preserved.
    """
    entries: list[ArchivedEntry] = []
    for rel_path in case_a_files:
        spec = f":2:{rel_path}"
        # Probe the index stage first: absence (rc != 0) means HEAD simply
        # has no version of this path, so there is nothing to archive.
        probe = _run_git(project_root, "rev-parse", spec, check=False)
        if probe.returncode != 0:
            continue
        entries.append(
            _archive_blob(
                project_root,
                spec=spec,
                rel_path=rel_path,
                # Side-namespaced so the HEAD copy never overwrites the
                # stashed copy of the same rel_path on disk.
                dest_rel=str(Path(".head-side") / rel_path),
                archive_run_rel=archive_run_rel,
                case="a",
                side="head",
            )
        )
    return entries


@dataclass
class StashPopOutcome:
    """Result of :func:`resolve_stashpop_safely`.

    ``archived`` is the recovery manifest (every file persisted to
    :data:`ARCHIVE_DIR` before any disposition) — the content pointers the
    audit issue records so an operator can actually restore. ``dropped``
    says whether the stash entry was finalized; it is ``True`` only after
    archival is proven, so a ``False`` here means the live stash was kept
    on purpose for manual recovery. ``case_a_files``/``case_b_files`` split
    the two structurally distinct conflict classes (real 3-way tracked vs
    untracked-collision) so the caller never lumps them together again.
    ``archive_failed`` flags the refuse-to-drop path — the recovery was not
    finalized, so the live stash survives for manual recovery. It is set in
    two cases: archival could not be confirmed (nothing was archived), or a
    case-a conflict was archived but its resolution left paths still unmerged
    (in which case ``archived`` is populated even though ``dropped`` is False).

    ``unresolved_files`` names the case-a paths that remain *unmerged in the
    index* after recovery — the precise "the working tree is left conflicted"
    signal. Whenever it is non-empty the merge integration MUST NOT report a
    clean success: the index still has unmerged entries that would wedge the
    next merge (``git stash`` cannot package stage>0 entries) and silently
    advancing the flow over them is exactly the data-integrity hazard this
    recovery exists to prevent. ``archive_failed`` is always set alongside a
    non-empty ``unresolved_files`` (recovery was not finalized).
    """

    archived: list[ArchivedEntry] = field(default_factory=list)
    dropped: bool = False
    case_a_files: list[str] = field(default_factory=list)
    case_b_files: list[str] = field(default_factory=list)
    archive_failed: bool = False
    unresolved_files: list[str] = field(default_factory=list)


def resolve_stashpop_safely(
    project_root: Path,
    stash_label: str,
    pop_result: subprocess.CompletedProcess,
    *,
    timestamp: str,
    conflict_resolver: Optional[Callable[[Path, list[str]], None]] = None,
) -> StashPopOutcome:
    """Recover a stash-pop result without ever losing data.

    Single shared entry point for both merge paths (``se3 merge`` fast
    strategy and the implement-step leaf-back merge). The caller has
    already run ``git stash pop``; ``pop_result`` is that completed
    process. The invariant enforced here, in order:

      1. **Clean pop** (``returncode == 0``): git already dropped the stash
         and nothing conflicted — return an empty outcome, touch nothing.

      2. **Archive first, unconditionally.** On a non-clean pop the stash
         entry is still live, so its *entire* recoverable content (untracked
         collisions that were never checked out, plus tracked working-tree
         changes about to be take-ours'd over) is persisted to
         :data:`ARCHIVE_DIR` via :func:`archive_stash_payload` *before* any
         disposition. If that cannot be proven (it raises), we refuse to
         drop: the live stash is kept and a warning logged
         (``archive_failed=True``). Recovery beats tidiness.

      3. **Classify, don't lump.** ``get_conflicting_files`` yields case a
         (genuine 3-way tracked conflicts); :func:`parse_stashpop_already_exists`
         yields case b (untracked-collision — concurrent unrelated new
         files). The old code take-ours'd both and dropped; that destroyed
         case b's content. Here:

           * **case b** — no semantic conflict. The merged working-tree
             version stays exactly as the merge left it; the stashed version
             has already been set aside in the archive (its "give way").
             Deliberately NO take-ours, NO write-back, NO destruction.
           * **case a** — BOTH sides are archived first: the stashed
             (theirs) side from step 2, plus the merged HEAD (ours) side via
             :func:`_archive_case_a_head_side`, captured before any
             resolution. Then the conflict is handed to the injected
             ``conflict_resolver`` (e.g. the LLM, symmetric with the merge
             body) when present; otherwise it falls back to deterministic
             :func:`take_ours_for_stashpop`. Whichever side the resolver
             discards is already in ``archived``, so the choice is reversible.

      4. **Drop only after recovery is proven AND the conflict is resolved,
         and only the labeled stash.** Archival succeeding makes the drop
         non-destructive, but for case a the index must also be free of the
         conflict: if the resolver (or the take-ours fallback) leaves any
         case-a path unmerged, dropping would discard the only stash handle for
         re-applying it while the merge is half-resolved — so we keep the stash
         and signal failure (``archive_failed=True``) instead. The drop targets
         the stash resolved by *label* (never a bare ``stash drop``, which would
         blow away ``stash@{0}`` — possibly an unrelated, unarchived entry).

    Returns the :class:`StashPopOutcome` (manifest + classification + drop
    state) the caller threads into its audit issue.
    """
    # Clean pop: git's own ``stash pop`` already dropped the entry.
    if pop_result.returncode == 0:
        return StashPopOutcome()

    # The stash must still be live for any of its content to be recoverable;
    # resolve it up front so a missing label refuses-to-drop without first
    # claiming a stray run dir.
    if _resolve_stash_ref(project_root, stash_label) is None:
        logger.warning(
            "Refusing to drop stash %r: no live stash matches the label "
            "after a non-clean pop. Nothing archived; stash (if any) kept.",
            stash_label,
        )
        return StashPopOutcome(archive_failed=True)

    # Highest-priority invariant: persist everything recoverable before we
    # are allowed to consider any disposition. A failure here means we have
    # no recovery proof, so we must NOT drop — keep the live stash instead.
    # ``OSError`` is caught alongside ``RuntimeError`` because claiming the run
    # dir (``_prepare_unique_archive_run``'s ``mkdir``) and writing blobs can
    # fail at the OS level — most importantly ``ENOSPC`` (disk full), the very
    # condition under which silently crashing mid-merge would strand the live
    # stash with no recovery record. Degrading to "keep the stash, flag
    # archive_failed" preserves the no-data-loss contract instead of raising.
    try:
        # Claim ONE unique run dir for this whole recovery so both the
        # stashed-side payload and the case-a HEAD-side capture below land
        # together — and so a same-second repeat recovery of the same label
        # cannot overwrite an earlier recovery's archived bytes (see
        # _prepare_unique_archive_run).
        archive_run_rel = _prepare_unique_archive_run(
            project_root, stash_label, timestamp,
        )
        archived = archive_stash_payload(
            project_root, stash_label, timestamp=timestamp,
            archive_run_rel=archive_run_rel,
        )
    except (RuntimeError, OSError) as exc:
        logger.warning(
            "Refusing to drop stash %r: could not archive its content "
            "(%s). Live stash kept for manual recovery.",
            stash_label,
            exc,
        )
        return StashPopOutcome(archive_failed=True)

    case_a_files = get_conflicting_files(project_root)
    case_b_files = parse_stashpop_already_exists(pop_result)

    # case a only: case b is left untouched (merged tree wins, stashed copy
    # already archived). See the docstring for why this asymmetry is the
    # whole point of the fix.
    if case_a_files:
        # Capture the merged HEAD (ours) side of every case-a path BEFORE
        # resolving, so whichever side the resolver discards stays
        # recoverable. ``archive_stash_payload`` only held the stashed side.
        try:
            archived.extend(
                _archive_case_a_head_side(
                    project_root, case_a_files,
                    archive_run_rel=archive_run_rel,
                )
            )
        except RuntimeError as exc:
            logger.warning(
                "Refusing to drop stash %r: could not archive the HEAD side "
                "of a case-a conflict (%s). Live stash kept for manual "
                "recovery.",
                stash_label,
                exc,
            )
            # Resolution never ran, so the case-a paths are still unmerged in
            # the index — surface them so the caller fails the merge instead of
            # advancing over a conflicted tree.
            return StashPopOutcome(
                archived=archived,
                case_a_files=case_a_files,
                case_b_files=case_b_files,
                archive_failed=True,
                unresolved_files=list(case_a_files),
            )

        if conflict_resolver is not None:
            conflict_resolver(project_root, case_a_files)
        else:
            take_ours_for_stashpop(project_root, case_a_files)

        # Confirm the resolution actually cleared the conflict before we are
        # allowed to drop. A ``conflict_resolver`` can return without staging a
        # resolution, and ``take_ours_for_stashpop`` is best-effort — an
        # unmerged modify/delete (no ``--ours`` stage to check out) or any path
        # it could not ``add`` stays unmerged. Dropping the labeled stash while
        # the index is still conflicted would remove the only handle for
        # re-applying the stashed change and leave the merge half-resolved. The
        # content is already in ``archived``, but a stash is more directly
        # restorable, so we keep it and signal failure (reusing
        # ``archive_failed`` as the single "recovery not finalized — stash
        # kept" signal the callers already surface) rather than finalize a
        # conflicted state.
        unresolved = [
            p for p in get_conflicting_files(project_root)
            if p in set(case_a_files)
        ]
        if unresolved:
            logger.warning(
                "Refusing to drop stash %r: %d case-a path(s) remain unmerged "
                "after resolution (%s). Content was archived; live stash kept "
                "for manual recovery.",
                stash_label,
                len(unresolved),
                ", ".join(unresolved),
            )
            return StashPopOutcome(
                archived=archived,
                case_a_files=case_a_files,
                case_b_files=case_b_files,
                archive_failed=True,
                unresolved_files=unresolved,
            )

    # Archival above returned without raising -> full recovery proof exists
    # for every enumerated path, so dropping is now non-destructive. Drop the
    # EXACT labeled stash, never a bare ``stash drop`` (which targets
    # ``stash@{0}`` — a different, unarchived entry if anything was pushed
    # after the failed pop or the label resolved past index 0).
    drop_ref = _resolve_stash_ref(project_root, stash_label)
    if drop_ref is None:
        dropped = False
        logger.warning(
            "Stash drop skipped: label %r no longer resolves to a live stash "
            "after recovery; not dropping a possibly-unrelated entry.",
            stash_label,
        )
    else:
        drop = _run_git(project_root, "stash", "drop", drop_ref, check=False)
        dropped = drop.returncode == 0
        if not dropped:
            logger.warning(
                "Stash drop failed after safe recovery (label %r, ref %s, "
                "rc=%s): %s",
                stash_label,
                drop_ref,
                drop.returncode,
                (drop.stderr or drop.stdout or "").strip(),
            )

    return StashPopOutcome(
        archived=archived,
        dropped=dropped,
        case_a_files=case_a_files,
        case_b_files=case_b_files,
    )


def format_archived_manifest(entries: list[ArchivedEntry]) -> str:
    """Render an archive manifest as audit-issue body text.

    Shared by both merge paths (``se3 merge`` fast strategy and the
    implement-step leaf-back merge) so the recovery pointer an operator
    reads — archive path, verifiable blob sha, and conflict class — is
    byte-for-byte identical regardless of which path rescued the content.
    Recording only file *paths* (the original audit defect) left nothing to
    restore from; this records the content pointers instead.
    """
    if not entries:
        return "  (no recoverable content was archived)"
    case_labels = {"a": "tracked 3-way", "b": "untracked-collision"}
    side_labels = {"stashed": "stashed/theirs", "head": "merged/ours"}
    lines: list[str] = []
    for e in entries:
        case_label = case_labels.get(e.case, e.case)
        side_label = side_labels.get(e.side, e.side)
        lines.append(
            f"  - {e.rel_path} [{case_label}; {side_label}]\n"
            f"      archived: {e.archive_path}\n"
            f"      blob sha: {e.blob_sha}"
        )
    return "\n".join(lines)
