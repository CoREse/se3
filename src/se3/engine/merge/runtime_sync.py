"""Runtime content synchronization for se3 merge.

After a successful git merge, git-ignored runtime data under ``se3/`` is not
automatically merged. This module copies tier A runtime content from the
source branch's bound worktree into the current branch's ``se3/`` directory.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .cleanup import _get_worktree_path_for_branch

logger = logging.getLogger(__name__)


def _file_hash(path: Path) -> str:
    """Return a SHA-256 hex digest of the file at *path* (streaming, 64 KiB chunks)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# Tier A: directories to recursively scan (relative to se3/)
TIER_A_DIRS = [
    "history",
    "logs",
    "state/archive",
    "collab/tasks",
]

# Tier A: glob patterns (relative to se3/).
# base.glob() is non-recursive: only direct children of the base directory
# are matched. This is intentional — e.g. "state/summary-*" targets files
# directly under state/, not nested directories like state/summary-flow/details.md.
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

    def __init__(self, rel_path: str) -> None:
        super().__init__(f"Runtime sync collision: {rel_path}")
        self.rel_path = rel_path


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
                    if target.is_dir():
                        if target not in seen:
                            # Boundary check: only recurse if target is within source_se3
                            if source_se3_resolved is not None:
                                try:
                                    target.relative_to(source_se3_resolved)
                                except ValueError:
                                    # Symlink points outside source_se3 — skip
                                    continue
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
            elif p.resolve().is_dir():
                # Boundary check: only descend if the resolved directory is
                # within source_se3.
                if source_se3_resolved is not None:
                    try:
                        p.resolve().relative_to(source_se3_resolved)
                    except ValueError:
                        continue
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


def sync_branch_runtime(project_root: Path, branch: str) -> SyncReport:
    """Sync runtime content from *branch*'s bound worktree into current branch's se3/.

    Tier A files (``history/``, ``logs/``, ``state/summary-*``,
    ``state/archive/``, ``calls/confirm_*``, ``collab/tasks/``) are copied
    from the source worktree's ``se3/`` to the current branch's ``se3/`` if
    the target does not already have a file at the same relative path. If a
    collision is detected, ``RuntimeSyncCollision`` is raised.

    Tier B files (``state/engine.json``, ``state/known_test_failures.json``,
    ``calls/active/``) are recorded as discarded but not copied.

    Tier C directories (``cache/``, ``tmp/``, ``worktrees/``) are completely
    ignored.

    Args:
        project_root: Root of the project (current branch).
        branch: Branch name whose bound worktree is the source.

    Returns:
        SyncReport describing what was copied and discarded. When the source
        worktree does not exist, returns ``SyncReport(skipped=True)``.

    Raises:
        RuntimeSyncCollision: When a tier A file exists at the same
            relative path in both source and target.
    """
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

    def _check_collision(src_file: Path, dest_file: Path, rel_str: str, src_hash: str) -> bool:
        """Return True if the destination should be skipped (idempotent match).

        Raises RuntimeSyncCollision when a non-idempotent collision is detected.
        Does NOT mutate destination metadata — metadata convergence is deferred
        to after the copy phase so that rollback leaves the target unchanged.
        """
        if not dest_file.exists():
            return False
        # Defensive: a directory at the destination path is a collision,
        # not an idempotent match (read_bytes would raise IsADirectoryError
        # and propagate as a confusing OSError).
        if dest_file.is_dir():
            raise RuntimeSyncCollision(rel_str)
        # Fast path: different sizes mean different content.
        try:
            if src_file.stat().st_size != dest_file.stat().st_size:
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
    idempotent_skips: list[tuple[Path, Path]] = []
    seen_rel_paths: set[str] = set()

    for dir_name in TIER_A_DIRS:
        source_dir = source_se3 / dir_name
        for src_file in _collect_files_under(source_dir, source_se3):
            rel_str = _rel_path_str(src_file, source_se3)
            if rel_str in seen_rel_paths:
                continue
            seen_rel_paths.add(rel_str)
            # Filter out cross-tree symlinks before collision checking so
            # that validation and copy phases agree on which files "count".
            if _is_outside_source_symlink(src_file, source_se3):
                report.skipped_files.append(rel_str)
                continue
            dest_file = target_se3 / rel_str
            if dest_file.exists():
                src_hash = _file_hash(src_file)
                if _check_collision(src_file, dest_file, rel_str, src_hash):
                    idempotent_skips.append((src_file, dest_file))
                    continue
                tier_a_files.append((src_file, rel_str, dest_file))
            else:
                tier_a_files.append((src_file, rel_str, dest_file))

    for glob_pattern in TIER_A_GLOBS:
        for src_file in _collect_glob_files(source_se3, glob_pattern, source_se3):
            rel_str = _rel_path_str(src_file, source_se3)
            if rel_str in seen_rel_paths:
                continue
            seen_rel_paths.add(rel_str)
            if _is_outside_source_symlink(src_file, source_se3):
                report.skipped_files.append(rel_str)
                continue
            dest_file = target_se3 / rel_str
            if dest_file.exists():
                src_hash = _file_hash(src_file)
                if _check_collision(src_file, dest_file, rel_str, src_hash):
                    idempotent_skips.append((src_file, dest_file))
                    continue
                tier_a_files.append((src_file, rel_str, dest_file))
            else:
                tier_a_files.append((src_file, rel_str, dest_file))

    # Copy phase — all destinations pre-validated
    copied_so_far: list[Path] = []
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
                # Unexpected OS error during read — let the outer handler
                # roll back and propagate the exception.
                raise
            # mkdir runs only after a successful read so that skipped files
            # do not leave behind empty directories that won't be rolled back.
            # Track newly-created directories for precise rollback.
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
            dest_file.write_bytes(content)
            # Preserve metadata (mtime, mode) from the source file.
            # src_stat comes from fstat(fd) for regular files or stat()
            # for followed symlinks — both give the target's metadata.
            os.utime(dest_file, (src_stat.st_atime, src_stat.st_mtime))
            os.chmod(dest_file, stat.S_IMODE(src_stat.st_mode))
            copied_so_far.append(dest_file)
            report.copied.append(rel_str)

        # Metadata convergence for idempotent skips — deferred until after the
        # copy phase succeeds so that rollback does not leave partially-synced
        # metadata on destination files.
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
    except OSError:
        # Rollback: remove partially copied files so a retry does not
        # fail with RuntimeSyncCollision on files that were only half-synced.
        for copied_file in copied_so_far:
            try:
                if copied_file.exists():
                    copied_file.unlink()
            except OSError:
                pass  # best-effort cleanup
        # Remove only directories that were created during this sync.
        # Sort by depth (deepest first) so children are removed before parents.
        for created_dir in sorted(created_dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                created_dir.rmdir()
            except OSError:
                pass  # Directory not empty or other error — skip
        # If se3/ itself did not exist before sync, remove it too so the
        # rolled-back state matches the pre-sync state.
        if not target_se3_existed:
            try:
                target_se3.rmdir()
            except OSError:
                pass
        raise

    # --- Tier B: record as discarded ---
    for file_name in TIER_B_FILES:
        source_file = source_se3 / file_name
        if source_file.exists() and source_file.is_file():
            report.discarded.append(file_name)

    for dir_name in TIER_B_DIRS:
        source_dir = source_se3 / dir_name
        for src_file in _collect_files_under(source_dir, source_se3):
            rel_str = _rel_path_str(src_file, source_se3)
            report.discarded.append(rel_str)

    return report
