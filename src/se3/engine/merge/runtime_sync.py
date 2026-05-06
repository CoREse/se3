"""Runtime content synchronization for se3 merge.

After a successful git merge, git-ignored runtime data under ``se3/`` is not
automatically merged. This module copies tier A runtime content from the
source branch's bound worktree into the current branch's ``se3/`` directory.
"""

from __future__ import annotations

import hashlib
import errno
import logging
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Optional

from .cleanup import _get_worktree_path_for_branch

logger = logging.getLogger(__name__)


# Sentinel used in BypassedCollision.dest_hash when the destination file
# could not be hashed (deleted, permission denied, became a directory,
# etc.) at audit-recording time. Exposed as a named constant rather than
# bare string literal so downstream consumers comparing dest_hash
# semantically (forensic recovery, audit-trail tooling) have a stable
# symbol to import. Operators should still branch primarily on the
# ``written`` flag, which is the authoritative "source bytes recoverable
# from disk" indicator; this constant is a secondary signal.
DEST_HASH_UNAVAILABLE: Final[str] = "unavailable"


def _file_hash(path: Path, source_se3: Path | None = None) -> str:
    """Return a SHA-256 hex digest of the file at *path* (streaming, 64 KiB chunks).

    Opens the file with ``O_NOFOLLOW`` and verifies via ``fstat`` that the
    descriptor refers to a regular file before reading. Non-regular entries
    (FIFOs, sockets, device files) raise ``OSError`` rather than potentially
    blocking on a FIFO read or returning garbage bytes from a device.

    When *source_se3* is provided, the read path follows the same internal
    symlink fallback as :func:`_safe_read_and_stat` (bounded depth, target
    must stay within *source_se3*). This keeps hash and read aligned for
    source-side files where internal symlinks are legitimate. When
    *source_se3* is ``None``, symlinks raise ``OSError(ELOOP)`` — the
    intended mode for destination/sidecar files which must be regular.
    """
    if source_se3 is not None:
        content, _ = _safe_read_and_stat(path, source_se3)
        return hashlib.sha256(content).hexdigest()

    h = hashlib.sha256()
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            if stat.S_ISDIR(st.st_mode):
                raise IsADirectoryError(21, "Is a directory", str(path))
            raise OSError(errno.EINVAL, "Not a regular file", str(path))
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            h.update(chunk)
    finally:
        os.close(fd)
    return h.hexdigest()


# Tier A: directories to recursively scan (relative to se3/)
TIER_A_DIRS = [
    "history",
    "logs",
    "state/archive",
    "collab/tasks",
]

# Tier A: glob patterns (relative to se3/).
# base.glob() matches direct children only, but when a match is a directory
# _collect_glob_files recurses into it via _collect_files_under. So e.g.
# "state/summary-*" collects files directly under state/ AND any nested
# files under matching directories like state/summary-flow/details.md.
TIER_A_GLOBS = [
    "state/summary-*",
    "calls/confirm_*",
]

# Tier B: specific files to discard from source (relative to se3/)
TIER_B_FILES = [
    "state/engine.json",
    "state/known_test_failures.json",
]

# Tier B: directories to discard from source (relative to se3/)
TIER_B_DIRS = [
    "calls/active",
]

# Tier C: directories completely skipped (relative to se3/)
TIER_C_DIRS = [
    "cache",
    "tmp",
    "worktrees",
]


class RuntimeSyncCollision(RuntimeError):
    """Raised when a tier A file exists in both source and target at the same relative path."""

    def __init__(
        self,
        rel_path: str,
        reason: str = "collision",
        sidecar_path: str | None = None,
        errno_code: int | None = None,
    ) -> None:
        super().__init__(f"Runtime sync collision: {rel_path}")
        self.rel_path = rel_path
        self.reason = reason
        self.sidecar_path = sidecar_path
        self.errno = errno_code


@dataclass(frozen=True)
class BypassedCollision:
    """Record of a tier A collision that was bypassed via sidecar file.

    The *branch* field always stores the raw git branch name (e.g.
    ``feat/foo``).  The safe label used in the sidecar filename
    (``feat__foo``) is a filesystem artefact and is NOT stored here.

    *written* indicates whether the source content was actually preserved on
    disk at *sidecar_rel_path*.  ``True`` (default) means a sidecar file was
    created or already matched the source content — the source bytes are
    recoverable from disk.  ``False`` means the bypass attempt itself failed
    (e.g. ENAMETOOLONG, EROFS, sidecar path is a directory, source vanished
    after pre-validation, or sidecar disambiguation exhausted) and the row
    is **audit-only**: the collision is recorded for traceability, but the
    source data is NOT represented on disk and cannot be recovered from the
    target tree.  ``dest_hash`` equal to the module-level sentinel
    :data:`DEST_HASH_UNAVAILABLE` typically accompanies ``written=False``
    rows but is not a substitute for this flag — operators and downstream
    consumers should branch on ``written`` rather than parsing hashes, and
    should compare ``dest_hash`` to :data:`DEST_HASH_UNAVAILABLE` (imported
    from this module) rather than against the bare ``"unavailable"``
    literal.
    """

    branch: str
    original_rel_path: str
    sidecar_rel_path: str
    src_hash: str
    dest_hash: str
    written: bool = True


class SymlinkDepthExceeded(OSError):
    """Raised when a symlink chain exceeds the maximum traversal depth."""

    def __init__(self, path: Path, max_depth: int) -> None:
        super().__init__(
            f"Symlink chain depth exceeded {max_depth} for {path}"
        )
        self.path = path
        self.max_depth = max_depth


@dataclass
class SyncReport:
    """Outcome of ``sync_branch_runtime``."""

    copied: list[str] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)
    skipped: bool = False
    skipped_files: list[str] = field(default_factory=list)
    collisions: list[BypassedCollision] = field(default_factory=list)
    # Number of tier A files where a sidecar already existed with content
    # matching the source. Surfaced as a weak signal so operators can detect
    # stale sidecar leftovers from a prior aborted run that may now mask a
    # genuinely new collision. Not added to ``collisions`` so re-runs of
    # ``se3 merge`` do not produce repeated audit entries.
    idempotent_bypasses: int = 0
    # Per-file audit detail of idempotent bypasses (parallel to ``collisions``).
    # Stored as ``BypassedCollision`` records but kept on a separate list so
    # the rendered output of ``se3 merge`` does not surface a noisy "collision"
    # row for every idempotent re-run, while operators investigating a stale
    # sidecar warning still get the per-file detail without re-running under
    # DEBUG logging. ``idempotent_bypasses`` (the counter above) is the
    # rendered signal; ``idempotent_bypass_records`` is the audit trail.
    idempotent_bypass_records: list[BypassedCollision] = field(
        default_factory=list,
    )


def _collect_files_under(path: Path, source_se3: Path | None = None) -> list[Path]:
    """Return all files recursively under *path*, or empty list if path does not exist.

    Includes broken symlinks so they are explicitly skipped downstream rather
    than silently disappearing. Symlinks to directories are followed (with a
    cycle guard so loops do not cause infinite recursion), but only when the
    resolved target stays within *source_se3* (when provided).
    """
    if not path.exists():
        return []

    source_se3_resolved = source_se3.resolve() if source_se3 else None

    # Entry-path boundary check: verify the resolved target stays within
    # source_se3. This catches both the case where *path* itself is a symlink
    # and the case where an intermediate directory component on the path is a
    # symlink pointing outside source_se3.
    if source_se3_resolved is not None:
        try:
            path.resolve().relative_to(source_se3_resolved)
        except ValueError:
            return []

    def _walk(current: Path, seen: set[Path]) -> list[Path]:
        result: list[Path] = []
        try:
            items = list(current.iterdir())
        except OSError:
            return result
        for p in items:
            if p.is_symlink():
                if not p.exists():
                    # Broken symlink — include so it is explicitly skipped
                    result.append(p)
                else:
                    target = p.resolve()
                    # Boundary check: reject symlinks that point outside
                    # source_se3 or resolve into tier-C territory.
                    if source_se3_resolved is not None:
                        try:
                            target.relative_to(source_se3_resolved)
                        except ValueError:
                            # Symlink points outside source_se3 — skip
                            continue
                        try:
                            rel_str = _rel_path_str(target, source_se3_resolved)
                            if _is_tier_c_path(rel_str):
                                # Symlink resolves into tier-C — skip
                                continue
                        except ValueError:
                            pass
                    if target.is_dir():
                        if target not in seen:
                            seen.add(target)
                            result.extend(_walk(p, seen))
                    else:
                        # Symlink to file
                        result.append(p)
            elif p.is_dir():
                resolved = p.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    result.extend(_walk(p, seen))
            else:
                result.append(p)
        return result

    return _walk(path, {path.resolve()})


def _cleanup_created_dirs(dirs: set[Path]) -> None:
    """Remove directories created during sync that ended up empty.

    Sorts by depth (deepest first) so children are removed before parents.
    Swallows OSError so non-empty or externally-mutated directories do not
    abort the caller.  This is used both on the success path (cleanup of
    empty intermediate directories) and the rollback path (undo of partially-
    created directories).
    """
    for created_dir in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            created_dir.rmdir()
        except OSError:
            pass  # Directory not empty, removed externally, or other error


def _collect_glob_files(base: Path, pattern: str, source_se3: Path | None = None) -> list[Path]:
    """Return files matching glob *pattern* relative to *base*."""
    if not base.exists():
        return []
    result: list[Path] = []
    source_se3_resolved = source_se3.resolve() if source_se3 else None
    for p in base.glob(pattern):
        # Boundary check: verify the resolved target stays within source_se3.
        # This catches intermediate-directory symlinks that glob() silently
        # traversed — the match itself may not be a symlink, but a parent
        # directory component could be. Broken symlinks that resolve outside
        # source_se3 are also skipped here (they would be skipped downstream
        # anyway, but filtering at collection keeps the two phases consistent).
        if source_se3_resolved is not None:
            try:
                p.resolve().relative_to(source_se3_resolved)
            except ValueError:
                continue
        if p.is_symlink():
            if not p.exists():
                result.append(p)
            else:
                target = p.resolve()
                # Boundary check: reject symlinks outside source_se3 or
                # resolving into tier-C territory.
                if source_se3_resolved is not None:
                    try:
                        target.relative_to(source_se3_resolved)
                    except ValueError:
                        continue
                    try:
                        rel_str = _rel_path_str(target, source_se3_resolved)
                        if _is_tier_c_path(rel_str):
                            continue
                    except ValueError:
                        pass
                if target.is_dir():
                    result.extend(_collect_files_under(p, source_se3))
                else:
                    result.append(p)
        elif p.is_dir():
            # Same boundary check for plain directories discovered by glob.
            if source_se3_resolved is not None:
                try:
                    p.resolve().relative_to(source_se3_resolved)
                except ValueError:
                    continue
            result.extend(_collect_files_under(p, source_se3))
        else:
            result.append(p)
    return result


def _safe_read_and_stat(path: Path, source_se3: Path) -> tuple[bytes, os.stat_result]:
    """Open with O_NOFOLLOW, read content, and return stat info.

    Falls back to following internal symlinks by reading the symlink target,
    resolving it, and opening the resolved path with O_NOFOLLOW. Internal
    symlink chains are followed up to a bounded depth. This closes the
    TOCTOU window between a symlink check and ``Path.read_bytes()``.
    Raises the same exceptions as ``Path.read_bytes()`` for missing files,
    directories, etc.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as open_exc:
        # Symlink fallback: follow internal symlinks up to a bounded depth.
        # We avoid re-evaluating _is_outside_source_symlink or calling
        # source_se3.resolve() repeatedly inside the loop to reduce TOCTOU
        # exposure under contention.
        source_se3_resolved = source_se3.resolve()
        current_path = path
        max_depth = 8
        for _ in range(max_depth):
            try:
                link_target = os.readlink(current_path)
            except OSError:
                # Not a symlink or unreadable — can't follow further.
                # If we haven't moved from the original path, this means the
                # original open failed for a reason other than it being a
                # symlink (e.g. permission denied, missing file).
                break
            resolved = Path(
                os.path.normpath(os.path.join(str(current_path.parent), link_target))
            )
            # Boundary check: resolved target must stay within source_se3.
            # TODO(defense-in-depth): ``os.path.normpath`` is purely
            # lexical — it collapses ``..`` and ``.`` segments but does
            # NOT resolve symlinks in parent components of
            # ``current_path``. A symlinked intermediate directory pointing
            # outside ``source_se3`` therefore evades this check at the
            # lexical layer; the downstream ``os.open(... O_NOFOLLOW)``
            # does not catch it either because it only refuses to follow
            # the *final* path component as a symlink. The source worktree
            # is user-controlled, so the practical risk is low; a fully
            # robust fix requires fd-based traversal (``openat`` with
            # O_NOFOLLOW per component) so each parent symlink is rejected
            # at resolve time.
            try:
                resolved.relative_to(source_se3_resolved)
            except ValueError:
                raise open_exc
            current_path = resolved

        # If we never followed any symlink, the original error stands.
        if current_path == path:
            raise open_exc

        # If the chain is still a symlink after max_depth, the depth was
        # exceeded. Raise SymlinkDepthExceeded so the caller can skip the
        # file rather than aborting the entire sync with an ELOOP.
        try:
            os.readlink(current_path)
        except OSError:
            pass  # Not a symlink — proceed to open
        else:
            raise SymlinkDepthExceeded(path, max_depth)

        # Open the resolved (non-symlink or final-in-chain) path with
        # O_NOFOLLOW so we never follow a symlink at open time.
        # NOTE: A TOCTOU window remains here — between the readlink above
        # and this open, the resolved path could be swapped. The source
        # worktree is user-controlled, so this is a defense-in-depth gap
        # acknowledged in prior reviews. A fully robust fix requires fd-based
        # traversal (openat with O_NOFOLLOW per component).
        fd2 = os.open(str(current_path), os.O_RDONLY | os.O_NOFOLLOW)
        try:
            stat_info = os.fstat(fd2)
            if not stat.S_ISREG(stat_info.st_mode):
                if stat.S_ISDIR(stat_info.st_mode):
                    raise IsADirectoryError(21, "Is a directory", str(path))
                raise OSError(1, "Not a regular file", str(path))
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd2, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks), stat_info
        finally:
            os.close(fd2)

    try:
        stat_info = os.fstat(fd)
        if not stat.S_ISREG(stat_info.st_mode):
            if stat.S_ISDIR(stat_info.st_mode):
                raise IsADirectoryError(21, "Is a directory", str(path))
            raise OSError(1, "Not a regular file", str(path))

        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), stat_info
    finally:
        os.close(fd)


def _rel_path_str(path: Path, base: Path) -> str:
    """Return POSIX-style relative path string of *path* from *base*."""
    return str(path.relative_to(base)).replace("\\", "/")


def _is_tier_c_path(rel_path: str) -> bool:
    """Return True if *rel_path* (relative to se3/) is inside a tier C directory."""
    for tier_c in TIER_C_DIRS:
        if rel_path == tier_c or rel_path.startswith(tier_c + "/"):
            return True
    return False


def _safe_branch_label(branch: str) -> str:
    """Return a filesystem-safe label derived from *branch*.

    Replaces ``/`` and ``\\`` with ``__`` (consistent with SE3 worktree
    path naming) and replaces any character outside ``[A-Za-z0-9._-]``
    with ``_``.  The result is truncated to 64 characters (with a trailing
    ``_`` marker) to avoid exceeding common filesystem NAME_MAX limits.

    Returns ``"unnamed"`` when *branch* is empty, so the sidecar suffix
    ``.from-`` never produces a path that would collapse across calls.

    .. note::

        This function is **not injective**: ``feat/foo`` and ``feat__foo``
        both collapse to ``feat__foo``.  When two branches differ only by
        these collapsed characters, the hash-suffix disambiguation in
        ``_write_sidecar`` preserves correctness, but the human-readable
        sidecar name no longer identifies the source branch unambiguously.
        ``BypassedCollision.branch`` always stores the raw branch name,
        so the audit trail remains correct.
    """
    label, _ = _safe_branch_label_with_truncation(branch)
    return label


def _safe_branch_label_with_truncation(branch: str) -> tuple[str, bool]:
    """Companion to :func:`_safe_branch_label` that also reports truncation.

    Returns ``(label, truncated)`` where *truncated* is ``True`` when the
    sanitized branch exceeded the 64-character cap and was clipped.  Used
    by ``_write_sidecar`` to surface a warning when an idempotent sidecar
    match could be misleading: a long-branch truncation collapses entropy
    into ``<head>_``, so an on-disk sidecar with that name may have been
    written by a *different* branch whose first 63 characters matched.
    """
    if not branch:
        return "unnamed", False
    safe = branch.replace("/", "__").replace("\\", "__")
    result = "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in safe)
    if len(result) > 64:
        truncated = result[:63] + "_"
        logger.debug("_safe_branch_label truncated '%s' to '%s'", branch, truncated)
        return truncated, True
    return result, False


def _atomic_write_bytes(dest_path: Path, content: bytes) -> None:
    """Write *content* to *dest_path* atomically via a unique temporary file.

    Creates a process-unique temp file in the same directory as *dest_path*
    via :func:`tempfile.mkstemp` (which opens with ``O_RDWR | O_CREAT |
    O_EXCL``), writes the full content, then renames it to *dest_path*.
    The unique random suffix avoids collisions with concurrent ``se3
    merge`` runs and with stale ``.tmp`` files left by crashed runs that
    previously used the fixed ``<dest>.tmp`` name.  ``O_EXCL`` also
    rejects hostile pre-existing files at the temp path, replacing the
    ``O_NOFOLLOW`` defence the older fixed-name implementation relied on.
    If the write or rename fails, the temporary file is removed so no
    partial file remains at either path.
    """
    parent_dir = dest_path.parent
    fd, temp_path_str = tempfile.mkstemp(
        prefix=f"{dest_path.name}.",
        suffix=".tmp",
        dir=str(parent_dir),
    )
    temp_path = Path(temp_path_str)
    try:
        try:
            offset = 0
            while offset < len(content):
                offset += os.write(fd, content[offset:])
        finally:
            os.close(fd)
        # TODO(defense-in-depth): replace() follows symlinks on the
        # destination side.  A TOCTOU window remains between mkstemp and
        # replace: if dest_path (or a sidecar path) is swapped to a
        # symlink, replace() will overwrite the symlink's target rather
        # than the symlink itself. The destination lives under the
        # user-controlled project_root/se3/, so the practical risk is low;
        # a fully robust fix requires destination-side O_NOFOLLOW or
        # fd-based renameat (openat + renameat).
        temp_path.replace(dest_path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _write_sidecar(
    src_file: Path,
    dest_file: Path,
    rel_str: str,
    source_se3: Path,
    target_se3: Path,
    branch: str,
    src_hash: str,
) -> tuple[BypassedCollision, bool]:
    """Write source content to a sidecar file next to *dest_file*.

    Sidecar path: ``<dest>.from-<safe_branch>``. Handles self-collision:
    - If sidecar exists with identical content → idempotent no-op.
    - If sidecar exists with different content → use ``<dest>.from-<branch>.<short_hash>``.
    - If hash-suffix path also exists with different content → raises RuntimeSyncCollision.

    Preserves source mtime/mode (same as normal copy path).

    *branch* is used as both the raw audit value (stored in
    ``BypassedCollision.branch``) and (after sanitization via
    ``_safe_branch_label``) as the sidecar filename suffix.

    .. note::

        The ``<short_hash>`` embedded in the filename comes from the
        ``src_hash`` argument captured at collision detection time.  If
        the source file mutates between that hash and the second read
        performed here, the bytes actually written to the sidecar — and
        the recorded ``BypassedCollision.src_hash`` (i.e.
        ``written_src_hash``) — reflect the *newer* content while the
        filename's hash suffix still encodes the *older* content.  The
        audit record stays internally consistent; an investigator
        comparing filename suffix vs. recorded ``src_hash`` should
        expect divergence under concurrent source mutation.

    Returns:
        (collision_record, did_write): *collision_record* always records the
        collision for audit. *did_write* is ``True`` only when a sidecar file
        was newly created (not when the sidecar already existed with matching
        content).
    """
    # Invariant guard: an empty branch name would let _safe_branch_label
    # collapse the sidecar suffix to ``.from-unnamed``, silently merging
    # collisions across all unnamed callers and corrupting both the
    # filesystem layout and the audit record.  ``sync_branch_runtime``
    # rejects empty branches up front, but a future refactor (e.g. invoking
    # ``_write_sidecar`` directly for an unbound worktree) must not bypass
    # that check — raise here so the invariant survives the public-API
    # boundary.
    if not branch:
        raise ValueError(
            "_write_sidecar: branch must not be empty — sidecar suffix "
            "would collapse across callers, silently merging audit identity",
        )
    safe_label, label_truncated = _safe_branch_label_with_truncation(branch)
    sidecar_file = Path(str(dest_file) + f".from-{safe_label}")

    def _hash_or_collision(path: Path) -> str:
        # Wrap _file_hash so that an OSError (EACCES, ELOOP, EISDIR, etc.)
        # surfaces as a RuntimeSyncCollision carrying the *currently
        # in-progress* sidecar path (closed over via ``sidecar_file``).
        # Without this, an OSError from hashing a hash-suffix sidecar would
        # escape into the bypass loop's outer ``except OSError`` handler,
        # which has no visibility into which suffix variant was being
        # examined and falls back to recording the primary sidecar path —
        # producing an audit row that does not match the file that actually
        # failed.  Routing through RuntimeSyncCollision lets the bypass
        # loop's ``except RuntimeSyncCollision`` branch use exc.sidecar_path
        # for accurate audit data.
        try:
            return _file_hash(path)
        except OSError as exc:
            raise RuntimeSyncCollision(
                rel_str,
                reason="sidecar_write_os_error",
                sidecar_path=str(sidecar_file),
                errno_code=getattr(exc, "errno", None),
            ) from exc

    dest_hash = _hash_or_collision(dest_file)

    # Check if sidecar already exists
    if sidecar_file.exists():
        if sidecar_file.is_dir():
            raise RuntimeSyncCollision(
                rel_str, reason="sidecar_is_directory", sidecar_path=str(sidecar_file),
            )
        sidecar_hash = _hash_or_collision(sidecar_file)
        if sidecar_hash == src_hash:
            # Idempotent: sidecar already has same content.  Re-hash the
            # on-disk sidecar rather than reusing the caller-supplied
            # src_hash so the audit record reflects the bytes actually on
            # disk (defense against concurrent sidecar mutation).
            if label_truncated:
                # Entropy lost: the filename's truncated label cannot
                # distinguish this branch from any other branch sharing
                # its first 63 sanitized characters. The on-disk sidecar
                # may have been written by a *different* long branch whose
                # sanitized name happens to share the same prefix; in that
                # case BypassedCollision.branch records the *current*
                # call's raw branch name even though the file content was
                # written by a different branch. Treat the audit record as
                # ambiguous (not authoritative) when investigating a
                # stale-sidecar discrepancy.
                logger.warning(
                    "Runtime sync idempotent sidecar match for '%s' (branch "
                    "'%s'): on-disk filename uses a truncated label '%s' "
                    "that does not uniquely identify the source branch; "
                    "the on-disk sidecar may have been written by a "
                    "different long branch sharing the same sanitized "
                    "prefix, so BypassedCollision.branch reflects the "
                    "current call rather than verified provenance",
                    rel_str, branch, safe_label,
                )
            return BypassedCollision(
                branch=branch,
                original_rel_path=rel_str,
                sidecar_rel_path=_rel_path_str(sidecar_file, target_se3),
                src_hash=sidecar_hash,
                dest_hash=dest_hash,
            ), False
        # Different content: try hash-suffix disambiguation
        short_hash = src_hash[:8]
        sidecar_file = Path(str(dest_file) + f".from-{safe_label}.{short_hash}")
        if sidecar_file.exists():
            if sidecar_file.is_dir():
                raise RuntimeSyncCollision(
                    rel_str, reason="sidecar_is_directory", sidecar_path=str(sidecar_file),
                )
            sidecar_hash = _hash_or_collision(sidecar_file)
            if sidecar_hash == src_hash:
                if label_truncated:
                    # Same entropy-loss caveat as the primary sidecar branch
                    # above: BypassedCollision.branch reflects the current
                    # call rather than verified provenance.
                    logger.warning(
                        "Runtime sync idempotent hash-suffix sidecar match for "
                        "'%s' (branch '%s'): on-disk filename uses a truncated "
                        "label '%s' that does not uniquely identify the source "
                        "branch; the on-disk sidecar may have been written by "
                        "a different long branch sharing the same sanitized "
                        "prefix, so BypassedCollision.branch reflects the "
                        "current call rather than verified provenance",
                        rel_str, branch, safe_label,
                    )
                return BypassedCollision(
                    branch=branch,
                    original_rel_path=rel_str,
                    sidecar_rel_path=_rel_path_str(sidecar_file, target_se3),
                    src_hash=sidecar_hash,
                    dest_hash=dest_hash,
                ), False
            # Still different — try 16-char hash suffix for extra
            # defense-in-depth against hash-prefix collisions between
            # branches whose sanitized labels collapse to the same value.
            long_hash = src_hash[:16]
            sidecar_file = Path(
                str(dest_file) + f".from-{safe_label}.{long_hash}"
            )
            if sidecar_file.exists():
                if sidecar_file.is_dir():
                    raise RuntimeSyncCollision(
                        rel_str, reason="sidecar_is_directory", sidecar_path=str(sidecar_file),
                    )
                sidecar_hash = _hash_or_collision(sidecar_file)
                if sidecar_hash == src_hash:
                    if label_truncated:
                        logger.warning(
                            "Runtime sync idempotent long-hash sidecar match for "
                            "'%s' (branch '%s'): on-disk filename uses a truncated "
                            "label '%s' that does not uniquely identify the source "
                            "branch; the on-disk sidecar may have been written by "
                            "a different long branch sharing the same sanitized "
                            "prefix, so BypassedCollision.branch reflects the "
                            "current call rather than verified provenance",
                            rel_str, branch, safe_label,
                        )
                    return BypassedCollision(
                        branch=branch,
                        original_rel_path=rel_str,
                        sidecar_rel_path=_rel_path_str(sidecar_file, target_se3),
                        src_hash=sidecar_hash,
                        dest_hash=dest_hash,
                    ), False
                # Disambiguation truly exhausted
                raise RuntimeSyncCollision(
                    rel_str, reason="disambiguation_exhausted", sidecar_path=str(sidecar_file),
                )

    # Preflight: catch NAME_MAX early for a clearer diagnostic.
    # NAME_MAX on most Linux filesystems is 255 bytes (not characters).
    # Use os.fsencode for a byte-accurate check so multi-byte characters
    # in the source filename do not evade the guard.
    if len(os.fsencode(sidecar_file.name)) > 255:
        raise RuntimeSyncCollision(
            rel_str,
            reason="sidecar_write_os_error",
            sidecar_path=str(sidecar_file),
            errno_code=errno.ENAMETOOLONG,
        )

    if label_truncated:
        # Surface the truncation at write time, not just on idempotent
        # retries.  The on-disk sidecar filename uses
        # ``<head63 chars>_`` as its branch identifier, so any future call
        # for a different branch sharing the same first 63 sanitized
        # characters will collapse to the same primary sidecar path and
        # require hash-suffix disambiguation.  Operators inheriting a se3/
        # tree should be able to see this from normal-operation logs
        # (INFO) rather than only catching it on a retry that hits the
        # idempotent-match warnings.
        logger.info(
            "Runtime sync sidecar label truncated at write time for '%s' "
            "(branch '%s' -> label '%s'): the on-disk sidecar filename does "
            "not uniquely identify the source branch; future writes from a "
            "different long branch sharing the same first 63 sanitized "
            "characters will require hash-suffix disambiguation",
            rel_str, branch, safe_label,
        )

    # Read content and write sidecar atomically.
    try:
        content, src_stat = _safe_read_and_stat(src_file, source_se3)
        _atomic_write_bytes(sidecar_file, content)
    except OSError as exc:
        raise RuntimeSyncCollision(
            rel_str,
            reason="sidecar_write_os_error",
            sidecar_path=str(sidecar_file),
            errno_code=getattr(exc, "errno", None),
        ) from exc
    # Hash the bytes actually written to the sidecar rather than reusing the
    # earlier `src_hash` from _check_collision: enforces the audit invariant
    # `BypassedCollision.src_hash == sha256(sidecar bytes)` even if src_file
    # mutated between collision detection and this read.
    written_src_hash = hashlib.sha256(content).hexdigest()
    collision = BypassedCollision(
        branch=branch,
        original_rel_path=rel_str,
        sidecar_rel_path=_rel_path_str(sidecar_file, target_se3),
        src_hash=written_src_hash,
        dest_hash=dest_hash,
    )
    # Preserve metadata (mtime, mode) from the source file — best-effort
    # so that a metadata failure (e.g. permission denied) does not leave
    # an untracked sidecar file on disk.
    try:
        os.utime(sidecar_file, (src_stat.st_atime, src_stat.st_mtime))
        os.chmod(sidecar_file, stat.S_IMODE(src_stat.st_mode))
    except OSError as exc:
        logger.debug(
            "Metadata convergence skipped for sidecar %s: %s",
            sidecar_file, exc,
        )
    return collision, True


def _is_outside_source_symlink(src_file: Path, source_se3: Path) -> bool:
    """Return True if *src_file* is a symlink whose resolved target lies outside *source_se3*.

    Returns False for regular files and for symlinks that resolve inside
    *source_se3*. Broken or unresolvable symlinks are treated as outside
    (True) so they are skipped rather than raising cryptic errors.
    """
    if not src_file.is_symlink():
        return False
    # Broken symlinks: exists() follows the link, so a missing target
    # makes this True. Treat them as outside so both validation and copy
    # phases agree on skipping them (no FileNotFoundError discrepancy).
    if not src_file.exists():
        return True
    try:
        resolved = src_file.resolve()
        resolved.relative_to(source_se3.resolve())
        return False
    except (ValueError, OSError):
        return True


def sync_branch_runtime(
    project_root: Path,
    branch: str,
    *,
    strict: bool = False,
) -> SyncReport:
    """Sync runtime content from *branch*'s bound worktree into current branch's se3/.

    Tier A files (``history/``, ``logs/``, ``state/summary-*``,
    ``state/archive/``, ``calls/confirm_*``, ``collab/tasks/``) are copied
    from the source worktree's ``se3/`` to the current branch's ``se3/`` if
    the target does not already have a file at the same relative path. If a
    collision is detected:
    - ``strict=True``: ``RuntimeSyncCollision`` is raised (legacy behaviour).
    - ``strict=False``: the source version is written to a sidecar file
      ``<dest>.from-<branch>`` and recorded in ``SyncReport.collisions``.

    Tier B files (``state/engine.json``, ``state/known_test_failures.json``,
    ``calls/active/``) are recorded as discarded but not copied.

    Tier C directories (``cache/``, ``tmp/``, ``worktrees/``) are completely
    ignored.

    .. note::

        The internal-symlink fallback used when reading source files
        (``_safe_read_and_stat``) verifies that resolved targets stay within
        ``source_se3`` using ``os.path.normpath``, which is purely lexical
        and does NOT resolve symlinks in *parent* directory components.  A
        symlinked intermediate directory pointing outside ``source_se3``
        therefore evades this defense at the lexical layer; the downstream
        ``os.open(O_NOFOLLOW)`` only refuses to follow the *final* path
        component.  The source worktree is user-controlled, so the
        practical risk is low — callers (e.g. the merge orchestrator)
        should treat the source-side path-confinement check as
        defense-in-depth rather than a hard sandbox.  A fully robust fix
        requires fd-based traversal (``openat`` with ``O_NOFOLLOW`` per
        component) so each parent symlink is rejected at resolve time.

    Args:
        project_root: Root of the project (current branch).
        branch: Branch name whose bound worktree is the source. Also used as
            the raw audit value in ``BypassedCollision.branch`` and (after
            sanitization via ``_safe_branch_label``) as the sidecar filename
            suffix.
        strict: When ``True``, tier A collisions raise ``RuntimeSyncCollision``.
            When ``False`` (default), collisions are bypassed via sidecar files.

    Returns:
        SyncReport describing what was copied, discarded, and bypassed. When
        the source worktree does not exist, returns ``SyncReport(skipped=True)``.

    Raises:
        RuntimeSyncCollision: When ``strict=True`` and a tier A file exists at
            the same relative path in both source and target with different
            content. In lenient mode, ``RuntimeSyncCollision`` is NEVER
            propagated to the caller — every collision (including
            disambiguation_exhausted, sidecar_is_directory, and
            sidecar_write_os_error) is captured inside the bypass loop and
            recorded as an audit-only ``BypassedCollision`` entry with
            ``written=False`` while the sync continues.
    """
    if not branch:
        raise ValueError("branch name must not be empty")

    source_wt = _get_worktree_path_for_branch(project_root, branch)
    if source_wt is None:
        logger.warning(
            "No bound worktree for branch '%s', skipping runtime sync", branch
        )
        return SyncReport(skipped=True)

    # Source worktree path exists in git metadata but the directory was
    # force-removed externally.
    if not source_wt.exists():
        logger.warning(
            "Bound worktree directory for branch '%s' does not exist "
            "(%s), skipping runtime sync", branch, source_wt
        )
        return SyncReport(skipped=True)

    # Defensive: source worktree must not be the same as project root.
    # When they are identical, every tier A file would trigger a spurious
    # RuntimeSyncCollision because dest_file.exists() is true by definition.
    if source_wt.resolve() == project_root.resolve():
        logger.warning(
            "Source worktree for branch '%s' is the same as project root, "
            "skipping runtime sync", branch,
        )
        return SyncReport(skipped=True)

    source_se3 = source_wt / "se3"
    target_se3 = project_root / "se3"
    target_se3_existed = target_se3.exists()

    report = SyncReport()

    def _check_collision(
        src_file: Path,
        dest_file: Path,
        rel_str: str,
        src_hash: str,
        *,
        src_size: int | None = None,
    ) -> bool:
        """Return True if the destination should be skipped (idempotent match).

        Raises RuntimeSyncCollision when a non-idempotent collision is detected.
        Does NOT mutate destination metadata — metadata convergence is deferred
        to after the copy phase so that rollback leaves the target unchanged.

        When *src_size* is provided, the fast-path size check uses it instead
        of ``src_file.stat().st_size``.  Callers that have already buffered
        the source content into memory should pass ``len(content)`` so that
        the size comparison reflects the bytes about to be written rather
        than the on-disk source — otherwise a concurrent mutation of
        ``src_file`` between buffering and this check could trigger a
        spurious collision when the buffered content actually equals dest.
        """
        if not dest_file.exists():
            return False
        # Defensive: a directory at the destination path is a collision,
        # not an idempotent match (read_bytes would raise IsADirectoryError
        # and propagate as a confusing OSError).
        if dest_file.is_dir():
            raise RuntimeSyncCollision(rel_str)
        # Defensive: non-regular entries (FIFOs, sockets, device files) are
        # not hashable as ordinary files and should not be treated as
        # idempotent matches.  _file_hash now validates S_ISREG, but an
        # early explicit guard keeps the intent readable.
        if not dest_file.is_file():
            raise RuntimeSyncCollision(rel_str)
        # Fast path: different sizes mean different content.  When the
        # caller has buffered src into memory (src_size given) we use the
        # buffered length so the comparison matches what we actually intend
        # to write; otherwise fall back to stat() of the on-disk source.
        try:
            effective_src_size = (
                src_size if src_size is not None else src_file.stat().st_size
            )
            if effective_src_size != dest_file.stat().st_size:
                raise RuntimeSyncCollision(rel_str)
        except OSError:
            pass  # Fall through to hash comparison
        # Idempotent: when source and target have identical content,
        # treat as a no-op rather than a fatal collision. This allows
        # re-running `se3 merge` on an already-synced branch.
        # Streaming hash comparison avoids loading large files into memory.
        if _file_hash(dest_file) == src_hash:
            return True
        raise RuntimeSyncCollision(rel_str)

    # --- Tier A: two-pass (validate all dest paths, then copy) ---
    tier_a_files: list[tuple[Path, str, Path]] = []
    bypass_files: list[tuple[Path, str, Path, str]] = []
    idempotent_skips: list[tuple[Path, Path]] = []
    seen_rel_paths: set[str] = set()

    def _process_single_src_file(src_file: Path) -> None:
        """Run collision logic for a single source file.

        Appends to ``tier_a_files``, ``bypass_files``, ``idempotent_skips``,
        and ``report.skipped_files`` depending on the destination state.
        """
        rel_str = _rel_path_str(src_file, source_se3)
        if rel_str in seen_rel_paths:
            return
        seen_rel_paths.add(rel_str)
        # Filter out cross-tree symlinks before collision checking so
        # that validation and copy phases agree on which files "count".
        if _is_outside_source_symlink(src_file, source_se3):
            report.skipped_files.append(rel_str)
            return
        dest_file = target_se3 / rel_str
        if dest_file.exists():
            try:
                src_hash = _file_hash(src_file, source_se3)
                if _check_collision(src_file, dest_file, rel_str, src_hash):
                    idempotent_skips.append((src_file, dest_file))
                    return
            except RuntimeSyncCollision:
                if strict:
                    raise
                if dest_file.is_dir() or not dest_file.is_file():
                    # Directory or non-regular entry (FIFO, socket, device)
                    # at destination cannot be bypassed as a sidecar file;
                    # skip it in lenient mode rather than aborting the
                    # entire sync.  Record an audit-only ``BypassedCollision``
                    # row for symmetry with the bypass loop's
                    # ``sidecar_is_directory`` branch (lines ~1230) — without
                    # this, operators reading ``runtime_sync_collisions``
                    # would have to cross-reference ``skipped_files`` to spot
                    # directory-at-dest cases caught here, while a
                    # structurally identical case caught later in the bypass
                    # loop would already be recorded.
                    safe_label, label_truncated = (
                        _safe_branch_label_with_truncation(branch)
                    )
                    if label_truncated:
                        logger.info(
                            "Runtime sync audit-only collision (directory-at-"
                            "dest) for '%s' (branch '%s'): sidecar label "
                            "truncated to '%s'; the recorded sidecar_rel_path "
                            "does not uniquely identify the source branch",
                            rel_str, branch, safe_label,
                        )
                    sidecar_path = Path(
                        str(dest_file) + f".from-{safe_label}"
                    )
                    report.collisions.append(
                        BypassedCollision(
                            branch=branch,
                            original_rel_path=rel_str,
                            sidecar_rel_path=_rel_path_str(sidecar_path, target_se3),
                            src_hash=src_hash,
                            dest_hash=DEST_HASH_UNAVAILABLE,
                            written=False,
                        )
                    )
                    report.skipped_files.append(rel_str)
                    return
                bypass_files.append((src_file, rel_str, dest_file, src_hash))
                return
            except OSError:
                # _file_hash / _check_collision raised — covers any IO
                # failure on src or dest (removal, EACCES, EIO, EISDIR
                # on an intermediate path component, etc.). Skip this
                # file rather than aborting the entire sync.
                report.skipped_files.append(rel_str)
                return
            # Defensive TOCTOU recovery: if dest_file existed at the outer
            # check but vanished between then and `_check_collision`'s
            # internal `dest_file.exists()` test, _check_collision returns
            # False without raising. Fall through to the no-collision path
            # so the source data is preserved — the copy phase's TOCTOU
            # re-check handles any state change between here and write.
            tier_a_files.append((src_file, rel_str, dest_file))
        else:
            tier_a_files.append((src_file, rel_str, dest_file))

    for dir_name in TIER_A_DIRS:
        source_dir = source_se3 / dir_name
        for src_file in _collect_files_under(source_dir, source_se3):
            _process_single_src_file(src_file)

    for glob_pattern in TIER_A_GLOBS:
        for src_file in _collect_glob_files(source_se3, glob_pattern, source_se3):
            _process_single_src_file(src_file)

    # Copy phase — all destinations pre-validated
    copied_so_far: list[Path] = []
    bypassed_so_far: list[Path] = []
    created_dirs: set[Path] = set()
    try:
        for src_file, rel_str, dest_file in tier_a_files:
            # Defense-in-depth: open with O_NOFOLLOW to close the TOCTOU
            # window between validation and copy. A malicious swap of a
            # regular file to an outside symlink is blocked at open time.
            # Internal symlinks are handled by the fallback in
            # _safe_read_and_stat.
            try:
                content, src_stat = _safe_read_and_stat(src_file, source_se3)
            except FileNotFoundError:
                # Dangling symlink or file removed after collection — skip
                report.skipped_files.append(rel_str)
                continue
            except IsADirectoryError:
                # Became a directory after collection — skip
                report.skipped_files.append(rel_str)
                continue
            except SymlinkDepthExceeded:
                # Symlink chain too deep — skip rather than abort entire sync
                report.skipped_files.append(rel_str)
                continue
            except OSError:
                # Transient permission denied, ENOSPC, or other IO error
                # during read — skip this file rather than aborting the
                # entire sync. Symmetric with bypass-phase handling.
                report.skipped_files.append(rel_str)
                continue
            # mkdir runs only after a successful read so that skipped files
            # do not leave behind empty directories that won't be rolled back.
            # Track newly-created directories for precise rollback.
            try:
                for parent in reversed(list(dest_file.parents)):
                    if parent == target_se3:
                        continue
                    try:
                        parent.relative_to(target_se3)
                    except ValueError:
                        continue
                    if not parent.exists():
                        parent.mkdir(parents=True, exist_ok=True)
                        created_dirs.add(parent)
            except OSError:
                # Transient error on a single destination subdirectory
                # (permission denied, ENOSPC, etc.) — skip this file rather
                # than rolling back all already-copied tier-A files.
                report.skipped_files.append(rel_str)
                continue
            # TOCTOU defense: re-check whether dest_file appeared between
            # validation and copy (e.g. concurrent process). If it now
            # exists, re-run collision logic rather than silently overwriting.
            if dest_file.exists():
                try:
                    # Hash the already-read content rather than re-reading
                    # src_file. This both saves one pass over the file and
                    # makes the TOCTOU check authoritative against what we
                    # actually intend to write.  src_size=len(content) keeps
                    # _check_collision's fast-path size comparison aligned
                    # with the buffered bytes — using src_file.stat() here
                    # would let a concurrent mutation of src_file trigger a
                    # spurious collision when the buffered content actually
                    # equals dest.
                    src_hash = hashlib.sha256(content).hexdigest()
                    if _check_collision(
                        src_file, dest_file, rel_str, src_hash,
                        src_size=len(content),
                    ):
                        # Idempotent — skip
                        continue
                except RuntimeSyncCollision:
                    if strict:
                        raise
                    # Directory or non-regular entry (FIFO, socket, device)
                    # at destination cannot be bypassed as a sidecar file;
                    # skip it in lenient mode rather than aborting the
                    # entire sync.  Record an audit-only ``BypassedCollision``
                    # row for symmetry with the bypass loop's
                    # ``sidecar_is_directory`` branch — without this,
                    # operators reading ``runtime_sync_collisions`` would
                    # have to cross-reference ``skipped_files`` to spot a
                    # TOCTOU directory swap caught here, while a structurally
                    # identical case caught later in the bypass loop would
                    # already be recorded.
                    if dest_file.is_dir() or not dest_file.is_file():
                        safe_label, label_truncated = (
                            _safe_branch_label_with_truncation(branch)
                        )
                        if label_truncated:
                            logger.info(
                                "Runtime sync audit-only collision (directory-at-"
                                "dest, TOCTOU) for '%s' (branch '%s'): sidecar "
                                "label truncated to '%s'; the recorded "
                                "sidecar_rel_path does not uniquely identify "
                                "the source branch",
                                rel_str, branch, safe_label,
                            )
                        sidecar_path = Path(
                            str(dest_file) + f".from-{safe_label}"
                        )
                        report.collisions.append(
                            BypassedCollision(
                                branch=branch,
                                original_rel_path=rel_str,
                                sidecar_rel_path=_rel_path_str(sidecar_path, target_se3),
                                src_hash=src_hash,
                                dest_hash=DEST_HASH_UNAVAILABLE,
                                written=False,
                            )
                        )
                        report.skipped_files.append(rel_str)
                        continue
                    # Route to bypass loop for symmetry with pre-validation
                    # phase so the source content is preserved as a sidecar.
                    bypass_files.append((src_file, rel_str, dest_file, src_hash))
                    continue
                except OSError:
                    report.skipped_files.append(rel_str)
                    continue
            try:
                _atomic_write_bytes(dest_file, content)
            except OSError as exc:
                # ENOSPC, permission denied, etc. during atomic write.
                # _atomic_write_bytes cleans up its temp file, so skipping
                # is safe — no partial file remains at dest_file.
                #
                # Surface severity-specific errors with dedicated warnings
                # (symmetric with the bypass loop): a silent skip would be
                # misleading because operators reading skipped_files cannot
                # tell a transient permission error apart from a legitimate
                # skip (cross-tree symlink, broken link).
                exc_errno = getattr(exc, "errno", None)
                if exc_errno == errno.ENAMETOOLONG:
                    logger.warning(
                        "Runtime sync copy filename too long for '%s' "
                        "(branch '%s'): filesystem rejected the destination "
                        "or temp filename (NAME_MAX); source data is not "
                        "represented on disk",
                        rel_str, branch,
                    )
                elif exc_errno in (errno.EACCES, errno.EROFS, errno.ENOSPC, errno.EDQUOT):
                    logger.warning(
                        "Runtime sync copy write failed for '%s' "
                        "(branch '%s'): %s (errno=%s); source data is not "
                        "represented on disk",
                        rel_str, branch, exc, exc_errno,
                    )
                report.skipped_files.append(rel_str)
                continue
            copied_so_far.append(dest_file)
            report.copied.append(rel_str)
            # Preserve metadata (mtime, mode) from the source file.
            # src_stat comes from fstat(fd) for regular files or stat()
            # for followed symlinks — both give the target's metadata.
            # Best-effort: failures (e.g. permission denied on chmod) are
            # logged and do not fail the sync — mirroring the idempotent
            # metadata-convergence path.
            try:
                os.utime(dest_file, (src_stat.st_atime, src_stat.st_mtime))
                os.chmod(dest_file, stat.S_IMODE(src_stat.st_mode))
            except OSError as exc:
                logger.debug(
                    "Metadata convergence skipped for copied file %s: %s",
                    dest_file, exc,
                )

        # Bypass phase — write sidecar files for collisions detected in lenient mode
        for src_file, rel_str, dest_file, src_hash in bypass_files:
            try:
                collision, did_write = _write_sidecar(
                    src_file, dest_file, rel_str, source_se3, target_se3,
                    branch, src_hash,
                )
            except RuntimeSyncCollision as exc:
                # Sidecar disambiguation exhausted, sidecar path is a
                # directory, or an OSError occurred during sidecar write
                # (captured inside _write_sidecar so the attempted path is
                # preserved).  Treat as skipped so the lenient sync can still
                # proceed. Unlike strict mode (which detects collisions in
                # pre-copy validation and leaves the target untouched), the
                # copy phase may have already written tier-A files here —
                # halting would leave a partially-synced state.
                #
                # Emit a warning rather than relying on report.skipped_files
                # alone: a path landing in skipped_files alongside benign
                # skips (cross-tree symlinks, transient IO errors) gives
                # operators no signal that branch data was actually unmerged.
                if exc.reason == "sidecar_write_os_error":
                    # OSError captured inside _write_sidecar (e.g. during
                    # _atomic_write_bytes for the hash-suffix sidecar).
                    # exc.sidecar_path records the actual attempted path so
                    # the audit row matches the log warning.
                    exc_errno = exc.errno
                    if exc_errno == errno.ENAMETOOLONG:
                        logger.warning(
                            "Runtime sync sidecar name too long for '%s' "
                            "(branch '%s'): filesystem rejected the sidecar "
                            "filename (NAME_MAX); source data is not represented "
                            "on disk",
                            rel_str, branch,
                        )
                    elif exc_errno in (errno.EACCES, errno.EROFS, errno.ENOSPC, errno.EDQUOT):
                        logger.warning(
                            "Runtime sync sidecar write failed for '%s' "
                            "(branch '%s'): %s (errno=%s); source data is not "
                            "represented on disk",
                            rel_str, branch, exc, exc_errno,
                        )
                    else:
                        logger.warning(
                            "Runtime sync sidecar write failed for '%s' "
                            "(branch '%s'): %s (errno=%s); source data is not "
                            "represented on disk",
                            rel_str, branch, exc, exc_errno,
                        )
                    sidecar_path = (
                        Path(exc.sidecar_path)
                        if exc.sidecar_path
                        else Path(str(dest_file) + f".from-{_safe_branch_label(branch)}")
                    )
                    try:
                        dest_hash = _file_hash(dest_file)
                    except OSError:
                        dest_hash = DEST_HASH_UNAVAILABLE
                    report.collisions.append(
                        BypassedCollision(
                            branch=branch,
                            original_rel_path=rel_str,
                            sidecar_rel_path=_rel_path_str(sidecar_path, target_se3),
                            src_hash=src_hash,
                            dest_hash=dest_hash,
                            written=False,
                        )
                    )
                elif exc.reason == "sidecar_is_directory":
                    logger.warning(
                        "Runtime sync sidecar path is a directory for '%s' "
                        "(branch '%s'): cannot write sidecar; source data is "
                        "not represented on disk",
                        rel_str, branch,
                    )
                    # Audit trail: record the collision even though the sidecar
                    # could not be written, so operators reading
                    # runtime_sync_collisions see a uniform entry for every
                    # colliding file rather than having to cross-reference
                    # skipped_files and log warnings.
                    safe_label = _safe_branch_label(branch)
                    primary_sidecar = Path(str(dest_file) + f".from-{safe_label}")
                    if primary_sidecar.exists() and primary_sidecar.is_dir():
                        sidecar_path = primary_sidecar
                    else:
                        short_hash = src_hash[:8]
                        sidecar_path = Path(
                            str(dest_file) + f".from-{safe_label}.{short_hash}"
                        )
                    # Best-effort dest hash: if the dest_file vanished or
                    # became unreadable between pre-validation and this audit
                    # recording (TOCTOU under concurrent activity), fall back
                    # to a placeholder rather than letting OSError escape.
                    # Otherwise it would propagate out of `except
                    # RuntimeSyncCollision` into the outer
                    # `except (OSError, RuntimeSyncCollision)` and trigger
                    # rollback of all already-copied tier A files for an
                    # audit-only failure.
                    try:
                        dest_hash = _file_hash(dest_file)
                    except OSError:
                        dest_hash = DEST_HASH_UNAVAILABLE
                    report.collisions.append(
                        BypassedCollision(
                            branch=branch,
                            original_rel_path=rel_str,
                            sidecar_rel_path=_rel_path_str(sidecar_path, target_se3),
                            src_hash=src_hash,
                            dest_hash=dest_hash,
                            written=False,
                        )
                    )
                else:
                    logger.warning(
                        "Runtime sync sidecar disambiguation exhausted for '%s' "
                        "(branch '%s'): both `<dest>.from-<branch>` and "
                        "`<dest>.from-<branch>.<short_hash>` already exist with "
                        "different content; source data is not represented on disk",
                        rel_str, branch,
                    )
                    # Audit trail uniformity: record the collision even when
                    # neither sidecar slot was writable.
                    safe_label = _safe_branch_label(branch)
                    short_hash = src_hash[:8]
                    sidecar_path = Path(
                        str(dest_file) + f".from-{safe_label}.{short_hash}"
                    )
                    # Best-effort dest hash (see sidecar_is_directory branch
                    # above for rationale).
                    try:
                        dest_hash = _file_hash(dest_file)
                    except OSError:
                        dest_hash = DEST_HASH_UNAVAILABLE
                    report.collisions.append(
                        BypassedCollision(
                            branch=branch,
                            original_rel_path=rel_str,
                            sidecar_rel_path=_rel_path_str(sidecar_path, target_se3),
                            src_hash=src_hash,
                            dest_hash=dest_hash,
                            written=False,
                        )
                    )
                report.skipped_files.append(rel_str)
                continue
            except OSError as exc:
                # Defense-in-depth: every OSError-raising call inside
                # ``_write_sidecar`` is wrapped to re-raise as
                # ``RuntimeSyncCollision(reason="sidecar_write_os_error")``
                # so the attempted sidecar path is preserved for audit:
                # ``_safe_read_and_stat`` and ``_atomic_write_bytes`` go
                # through the inner write try-block, while every internal
                # ``_file_hash`` call (``dest_file`` and the primary /
                # short-hash / long-hash sidecar variants) goes through the
                # ``_hash_or_collision`` closure that closes over the
                # in-progress ``sidecar_file``.
                #
                # In the current code path no OSError should escape
                # ``_write_sidecar`` as a raw OSError — it is always wrapped
                # into RuntimeSyncCollision and handled by the
                # ``except RuntimeSyncCollision`` branch above (which uses
                # ``exc.sidecar_path`` for accurate audit data). Specific
                # subclass handlers (FileNotFoundError, IsADirectoryError,
                # SymlinkDepthExceeded) were intentionally removed because
                # they were unreachable in the current code path; if a
                # future refactor unwraps one of those, this fallback still
                # catches it but without the variant-specific sidecar path
                # — so prefer adding the wrapping back at the new raise site
                # rather than reintroducing a subclass handler here.
                #
                # This fallback exists as a safety net so a future refactor
                # that introduces a new unwrapped OSError site cannot
                # silently propagate to the outer ``except (OSError,
                # RuntimeSyncCollision)`` rollback and destroy already-synced
                # tier-A files for an audit-only bookkeeping failure.  In
                # the fallback the actual sidecar variant is unknown, so the
                # audit row records the *primary* sidecar path as a
                # best-effort approximation; if this fallback fires in
                # practice it indicates a missing wrapper that should be
                # added rather than tolerated.  Every log message and the
                # audit row's sidecar_rel_path are tagged with the
                # ``[unwrapped-oserror-fallback]`` marker so an operator can
                # tell at a glance that the attempted sidecar variant
                # (primary vs short-hash vs long-hash) is unknown and the
                # recorded path is the primary slot only as a best-effort
                # default.
                exc_errno = getattr(exc, "errno", None)
                if exc_errno == errno.ENAMETOOLONG:
                    logger.warning(
                        "Runtime sync sidecar name too long for '%s' "
                        "(branch '%s') [unwrapped-oserror-fallback]: "
                        "filesystem rejected the sidecar filename (NAME_MAX); "
                        "source data is not represented on disk; the "
                        "attempted sidecar variant (primary/short-hash/long-hash) "
                        "is unknown — investigate the missing OSError wrapper",
                        rel_str, branch,
                    )
                elif exc_errno in (errno.EACCES, errno.EROFS, errno.ENOSPC, errno.EDQUOT):
                    logger.warning(
                        "Runtime sync sidecar write failed for '%s' "
                        "(branch '%s') [unwrapped-oserror-fallback]: %s "
                        "(errno=%s); source data is not represented on disk; "
                        "the attempted sidecar variant "
                        "(primary/short-hash/long-hash) is unknown — "
                        "investigate the missing OSError wrapper",
                        rel_str, branch, exc, exc_errno,
                    )
                else:
                    logger.warning(
                        "Runtime sync sidecar write failed for '%s' "
                        "(branch '%s') [unwrapped-oserror-fallback]: %s "
                        "(errno=%s); source data is not represented on disk; "
                        "the attempted sidecar variant "
                        "(primary/short-hash/long-hash) is unknown — "
                        "investigate the missing OSError wrapper",
                        rel_str, branch, exc, exc_errno,
                    )
                # Audit trail uniformity: record the collision even when the
                # sidecar write failed on disk, so operators reading
                # `report.runtime_sync_collisions` see a consistent row for
                # every collided file. Without this, a sidecar OSError
                # (ENAMETOOLONG, EROFS, EACCES, ENOSPC, EDQUOT) leaves a
                # ghost: skipped_files records the path but the collisions
                # list is silent, forcing operators to cross-reference logs.
                safe_label = _safe_branch_label(branch)
                sidecar_path = Path(str(dest_file) + f".from-{safe_label}")
                # Best-effort dest hash: parallel to the RuntimeSyncCollision
                # branches above, fall back to a placeholder rather than
                # letting OSError escape into the outer rollback handler.
                try:
                    dest_hash = _file_hash(dest_file)
                except OSError:
                    dest_hash = DEST_HASH_UNAVAILABLE
                report.collisions.append(
                    BypassedCollision(
                        branch=branch,
                        original_rel_path=rel_str,
                        sidecar_rel_path=_rel_path_str(sidecar_path, target_se3),
                        src_hash=src_hash,
                        dest_hash=dest_hash,
                        written=False,
                    )
                )
                report.skipped_files.append(rel_str)
                continue
            if did_write:
                bypassed_so_far.append(target_se3 / collision.sidecar_rel_path)
                report.collisions.append(collision)
            else:
                # Idempotent: sidecar already existed with identical content.
                # Do NOT add to report.collisions so re-runs of se3 merge do
                # not surface spurious warnings.
                # Surface a weak signal via report.idempotent_bypasses so
                # operators inheriting a worktree with stale sidecar leftovers
                # can detect that prior runs preserved divergent source data
                # at the same path — without producing repeated warnings on
                # legitimate re-runs.
                report.idempotent_bypasses += 1
                # Capture per-file audit detail in a parallel list so an
                # operator investigating a stale-sidecar warning has names,
                # not just a count, without having to rerun under DEBUG.
                report.idempotent_bypass_records.append(collision)
                logger.debug(
                    "Runtime sync idempotent bypass for %s: sidecar %s already "
                    "matches source content",
                    rel_str, collision.sidecar_rel_path,
                )

    except (OSError, RuntimeSyncCollision):
        # Defense-in-depth rollback.  In lenient mode, all expected OSError
        # paths in copy/bypass loops are caught individually and absorbed as
        # ``skipped_files`` entries, so this outer handler is primarily a
        # safety net for strict mode (where RuntimeSyncCollision propagates
        # uncaught) and for unexpected edge cases.  A future refactor that
        # removes one of the inner OSError handlers could silently start
        # triggering rollback in lenient mode — maintain inner handlers or
        # update this comment if the invariant changes.
        #
        # Scope invariant: this try-block wraps ONLY the copy and bypass
        # loops above.  The post-success metadata-convergence loop and
        # ``_cleanup_created_dirs`` call live outside the try-block so an
        # unforeseen exception in those best-effort operations cannot trigger
        # rollback of already-synced tier-A files.  If a future refactor
        # widens this try-block, the no-rollback invariant for metadata
        # convergence is lost — keep them outside.
        #
        # Rollback: remove partially copied files so a retry does not
        # fail with RuntimeSyncCollision on files that were only half-synced.
        for copied_file in copied_so_far:
            try:
                if copied_file.exists():
                    copied_file.unlink()
            except OSError:
                pass  # best-effort cleanup
        # Rollback bypassed sidecar files as well.
        for bypassed_file in bypassed_so_far:
            try:
                if bypassed_file.exists():
                    bypassed_file.unlink()
            except OSError:
                pass
        _cleanup_created_dirs(created_dirs)
        # If se3/ itself did not exist before sync, remove it too so the
        # rolled-back state matches the pre-sync state.
        if not target_se3_existed:
            try:
                target_se3.rmdir()
            except OSError:
                pass
        raise

    # Metadata convergence for idempotent skips — deferred until after the
    # copy phase succeeds so that rollback does not leave partially-synced
    # metadata on destination files.  Lives OUTSIDE the rollback try-block
    # (per the scope invariant in the except handler above) so a benign
    # best-effort failure here cannot tear down already-synced files.
    for src_file, dest_file in idempotent_skips:
        try:
            # Same rationale as copy phase: stat() follows symlinks so
            # the destination inherits the target file's metadata.
            # Order matches the copy phase (utime then chmod) for symmetry.
            src_stat = src_file.stat()
            os.utime(dest_file, (src_stat.st_atime, src_stat.st_mtime))
            os.chmod(dest_file, stat.S_IMODE(src_stat.st_mode))
        except OSError as exc:
            # Metadata convergence is best-effort; do not fail the sync
            # for a content-identical file whose permissions cannot be
            # changed (e.g. owned by another user).
            logger.debug(
                "Metadata convergence skipped for idempotent file %s: %s",
                dest_file, exc,
            )

    # Post-success cleanup: remove any directories created during the copy
    # phase that ended up empty. This covers the case where a TOCTOU
    # re-check rerouted the file to bypass and the sidecar write later
    # failed (recorded as skipped_files, no rollback). Without this pass,
    # an empty directory would persist under target_se3. Best-effort:
    # ``_cleanup_created_dirs`` swallows OSError, and the call lives
    # outside the rollback try-block so any unexpected failure does not
    # tear down already-synced files.
    _cleanup_created_dirs(created_dirs)

    # --- Tier B: record as discarded ---
    for file_name in TIER_B_FILES:
        source_file = source_se3 / file_name
        if source_file.exists() and source_file.is_file():
            report.discarded.append(file_name)

    for dir_name in TIER_B_DIRS:
        source_dir = source_se3 / dir_name
        for src_file in _collect_files_under(source_dir, source_se3):
            # Broken symlinks are included by _collect_files_under so they are
            # explicitly skipped downstream, but they should not semantically
            # appear as "discarded real files".  Filter to actual files
            # (is_file() follows symlinks and returns False for broken ones).
            if not src_file.is_file():
                continue
            rel_str = _rel_path_str(src_file, source_se3)
            report.discarded.append(rel_str)

    return report
