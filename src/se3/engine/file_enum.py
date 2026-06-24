"""Shared gitignore-respecting file enumeration for the code-index subsystem.

This module owns the canonical "what files does the project contain" enumeration
that the code-index builds its structure map from. It relocates the three git
helpers (``_git_ls_files_paths`` / ``_git_ls_files_other`` / ``_is_se3_path``)
that previously lived in ``sync_state.py`` so they survive after the ``se3 sync``
machinery is retired — ``sync_state`` now re-exports them from here for backward
compatibility while it still exists.

Why git rather than ``os.walk``: ``git ls-files`` (tracked) plus
``git ls-files --others --exclude-standard`` (untracked-but-not-ignored) makes
the project's ``.gitignore`` the primary inclusion filter for free — nested
``.gitignore`` files, ``.git/info/exclude`` and the global exclude are all
honoured, caches and generated artefacts drop out automatically, and a brand-new
non-ignored file is picked up on its first re-build without any registration. The
``se3/`` runtime directory is always excluded.

On top of the gitignore filter, two secondary guards backstop the cases git
cannot express: an explicit ``code_index.exclude`` pattern list (project-specific
vendored blobs / huge generated files git nonetheless tracks) and per-file
binary / size classification used by the indexer to drop a noisy file to a single
file-level line instead of attempting structural extraction.
"""

from __future__ import annotations

import fnmatch
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# How many leading bytes to sniff when classifying a file as binary.
_BINARY_SNIFF_BYTES = 8192


# ---------------------------------------------------------------------------
# se3/ exclusion + git enumeration (relocated from sync_state.py)
# ---------------------------------------------------------------------------

def _is_se3_path(rel_path: str) -> bool:
    """True when *rel_path* is inside the ``se3/`` runtime directory."""
    normalized = rel_path.replace("\\", "/")
    return normalized == "se3" or normalized.startswith("se3/")


def _git_ls_files_paths(root: Path) -> List[str]:
    """Return relative paths of every tracked file (``git ls-files``).

    The returned set is the index view — files deleted from the working tree
    but not yet ``git rm``'d are still listed. Callers that need working-tree
    existence must re-check via ``(root / rel_path).is_file()``.
    """
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "-z"],
            cwd=str(root),
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        logger.warning("git ls-files failed: %s", exc)
        return []

    return [p for p in out.split("\0") if p]


def _git_ls_files_other(root: Path) -> List[str]:
    """Return relative paths of untracked, non-ignored files.

    Uses ``git ls-files --others --exclude-standard`` so a brand-new file that
    is not gitignored is picked up without any registration step.
    """
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(root),
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        logger.warning("git ls-files --others failed: %s", exc)
        return []

    return [line for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Explicit-exclude matching (secondary guard over the gitignore filter)
# ---------------------------------------------------------------------------

def matches_exclude(rel_path: str, patterns: List[str]) -> bool:
    """Return True when *rel_path* matches any explicit-exclude *patterns*.

    Matching is glob-style (``fnmatch``) against the project-relative POSIX
    path, with two conveniences so a project does not need to spell out every
    nesting level:

    - a pattern with no ``/`` is also matched against the file's basename, so
      ``*.min.js`` or ``bundle.js`` work regardless of directory; and
    - a directory-style pattern matches everything beneath it, so ``vendor`` or
      ``vendor/`` excludes ``vendor/lib/x.js``.
    """
    norm = rel_path.replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    for raw in patterns:
        pat = raw.replace("\\", "/").rstrip("/")
        if not pat:
            continue
        if fnmatch.fnmatch(norm, pat):
            return True
        # Directory subtree: pattern matches the dir prefix.
        if norm == pat or norm.startswith(pat + "/"):
            return True
        if fnmatch.fnmatch(norm, pat + "/*"):
            return True
        # Basename match for slash-less patterns (e.g. ``*.lock``).
        if "/" not in pat and fnmatch.fnmatch(base, pat):
            return True
    return False


# ---------------------------------------------------------------------------
# Binary / size classification (per-file granularity guard)
# ---------------------------------------------------------------------------

def is_binary(path: Path) -> bool:
    """Return True when *path* sniffs as a binary (non-text) file.

    Reads at most ``_BINARY_SNIFF_BYTES`` leading bytes: a NUL byte is the
    classic binary signal, and content that fails to decode as UTF-8 is treated
    as binary too. An unreadable file is conservatively reported as binary so
    the indexer keeps it at a single file-level line rather than attempting
    structural extraction. An empty file is text.
    """
    try:
        with open(path, "rb") as f:
            chunk = f.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return True
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        # A multi-byte UTF-8 sequence may straddle the sniff boundary; only
        # treat as binary when a large fraction is undecodable.
        try:
            chunk.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            non_text = sum(1 for b in chunk if b < 0x09 or (0x0E <= b < 0x20))
            return non_text > len(chunk) * 0.30
    return False


@dataclass
class FileSize:
    """Cheap size measurement of a text file (line count + byte count)."""

    lines: int
    bytes: int


def measure_file(path: Path) -> Optional[FileSize]:
    """Return the (line, byte) size of *path*, or None if it cannot be read."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return FileSize(lines=data.count(b"\n") + (1 if data else 0), bytes=len(data))


# ---------------------------------------------------------------------------
# Top-level enumeration entry point
# ---------------------------------------------------------------------------

def enumerate_index_files(
    project_root: Path,
    exclude_list: Optional[List[str]] = None,
) -> List[Path]:
    """Enumerate the files the code-index should cover, gitignore-respecting.

    The set is ``git ls-files`` (tracked) ∪ ``git ls-files --others
    --exclude-standard`` (untracked, non-ignored), minus:

    - anything under the ``se3/`` runtime directory,
    - paths matched by the explicit ``exclude_list`` (``code_index.exclude``),
    - paths that no longer exist on disk (tracked-but-deleted ghosts).

    Returns absolute :class:`Path` objects sorted by their project-relative
    POSIX path, so the enumeration is deterministic across runs and machines.
    """
    root = Path(project_root).resolve()
    patterns = exclude_list or []

    rel_paths: set[str] = set()
    for rel in _git_ls_files_paths(root):
        rel_paths.add(rel)
    for rel in _git_ls_files_other(root):
        rel_paths.add(rel)

    kept: List[str] = []
    for rel in rel_paths:
        norm = rel.replace("\\", "/")
        if _is_se3_path(norm):
            continue
        if patterns and matches_exclude(norm, patterns):
            continue
        if not (root / rel).is_file():
            # Tracked-but-deleted ghost, or a path that resolved to a directory.
            continue
        kept.append(norm)

    kept.sort()
    return [root / rel for rel in kept]
