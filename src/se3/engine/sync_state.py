"""SyncState — persistent cache of the last successful ``se3 sync`` convergence.

``SyncState`` is the "last successful convergence" snapshot, recording the
global content fingerprint, whether discovery converged, per-spec hashes and
dependency file sets, and obsolete-spec candidates. It is read by ``SyncLoop``
at the start of a new ``se3 sync`` to evaluate skip gates, and written only
when the loop genuinely converges (``converged=True`` with no unresolved
failed analyses).

It is intentionally separate from ``SyncCheckpoint``:
- ``SyncCheckpoint`` is a "recover from interruption" temporary file that is
  deleted when the run completes or resumes successfully.
- ``SyncState`` is a "last good convergence" cache that persists across
  invocations and is re-used as a skip-optimisation baseline.

The file lives at ``se3/state/sync_state.json`` — a path that is gitignored
by the ``/se3/*`` rule and therefore never tracked.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYNC_STATE_SCHEMA_VERSION = 1
_STATE_REL_PATH = Path("se3") / "state" / "sync_state.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SyncState:
    """Persistent snapshot of the last successful sync convergence.

    Every field except ``state_version`` is serialised; ``state_version`` is
    written at the top level for schema-migration detection on load.
    """

    state_version: int = SYNC_STATE_SCHEMA_VERSION
    converged_at: Optional[str] = None
    code_fingerprint: str = ""
    discovery_converged: bool = False
    spec_deps: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    obsolete_specs: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state_version": self.state_version,
            "converged_at": self.converged_at,
            "code_fingerprint": self.code_fingerprint,
            "discovery_converged": self.discovery_converged,
            "spec_deps": _deep_copy_spec_deps(self.spec_deps),
            "obsolete_specs": list(self.obsolete_specs),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SyncState:
        return cls(
            state_version=int(data.get("state_version", SYNC_STATE_SCHEMA_VERSION)),
            converged_at=data.get("converged_at"),
            code_fingerprint=str(data.get("code_fingerprint", "")),
            discovery_converged=bool(data.get("discovery_converged", False)),
            spec_deps=data.get("spec_deps") or {},
            obsolete_specs=list(data.get("obsolete_specs") or []),
        )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def spec_in_sync(
        self, spec_name: str, current_hashes: Dict[str, str]
    ) -> bool:
        """Return True when the spec's recorded hash and all its dep-file
        content hashes match *current_hashes*.

        *current_hashes* is a flat dict mapping ``{relative_path: sha256}``,
        typically pre-computed by the caller for all files touched by the
        spec's deps.
        """
        entry = self.spec_deps.get(spec_name)
        if not entry:
            return False
        recorded_deps: Dict[str, str] = entry.get("deps", {})
        if not recorded_deps:
            # No deps on record — cannot validate, so conservatively not in-sync.
            return False
        for rel_path, recorded_hash in recorded_deps.items():
            if current_hashes.get(rel_path) != recorded_hash:
                return False
        return True


def _deep_copy_spec_deps(deps: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep-ish copy suitable for JSON serialisation."""
    result: Dict[str, Any] = {}
    for spec_name, entry in deps.items():
        entry_copy: Dict[str, Any] = {}
        entry_copy["spec_hash"] = entry.get("spec_hash", "")
        deps_copy: Dict[str, str] = {}
        for path, h in entry.get("deps", {}).items():
            deps_copy[str(path)] = str(h)
        entry_copy["deps"] = deps_copy
        result[str(spec_name)] = entry_copy
    return result


# ---------------------------------------------------------------------------
# File-system paths
# ---------------------------------------------------------------------------

def state_path(project_root: Path) -> Path:
    return Path(project_root) / _STATE_REL_PATH


# ---------------------------------------------------------------------------
# Atomic load / save (mirrors sync_checkpoint.py pattern)
# ---------------------------------------------------------------------------

def load(project_root: Path) -> Optional[SyncState]:
    """Read ``sync_state.json`` if it exists.

    Returns ``None`` (treated as "no cache — full sync") when the file is
    missing, its JSON is corrupt, or its ``state_version`` does not match the
    current schema version.
    """
    path = state_path(project_root)
    if not path.exists():
        logger.debug("No sync_state file at %s", path)
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load sync_state at %s: %s", path, exc)
        return None
    try:
        state = SyncState.from_dict(data)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Invalid sync_state structure at %s: %s", path, exc)
        return None

    if state.state_version != SYNC_STATE_SCHEMA_VERSION:
        logger.info(
            "sync_state version mismatch (file=%d, current=%d) — treating as no cache",
            state.state_version,
            SYNC_STATE_SCHEMA_VERSION,
        )
        return None

    return state


def save(state: SyncState, project_root: Path) -> Path:
    """Atomically write *state* to ``se3/state/sync_state.json``.

    Uses tempfile + ``os.replace`` so a mid-write crash never leaves a
    half-written file behind.
    """
    path = state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(
        state.to_dict(), indent=2, ensure_ascii=False, default=str
    )

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".sync_state.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    logger.info("Wrote sync_state to %s (converged_at=%s)", path, state.converged_at)
    return path


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------

def compute_file_content_hash(path: Path) -> Optional[str]:
    """Return SHA-256 of *path* contents, or None if the file cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Code fingerprint
# ---------------------------------------------------------------------------

def compute_code_fingerprint(project_root: Path) -> str:
    """Compute a global SHA-256 content fingerprint of the working tree.

    The fingerprint is sensitive to file content changes, additions,
    deletions, and renames; it is insensitive to mtime-only changes.

    The algorithm:

    1. Discover every tracked file via ``git ls-files`` (paths only),
       compute each file's working-tree content SHA-256, and record the
       ``(rel_path, content_hash)`` pair.  Files that are tracked but
       absent from the working tree (deleted but not yet staged) are
       omitted, which causes the fingerprint to change.
    2. Discover untracked files that are NOT gitignored (via
       ``git ls-files --others --exclude-standard``), compute their
       content SHA-256, and fold those in identically.
    3. Exclude every path under ``se3/`` so spec/state self-writes don't
       perturb the fingerprint.
    4. Sort all ``(rel_path, hash)`` pairs, combine into a final SHA-256.

    Using working-tree content hashes (rather than git blob SHAs from the
    index) means unstaged modifications, deletions, and renames all change
    the fingerprint immediately — the caller doesn't need a clean index.
    """
    root = Path(project_root).resolve()
    entries: List[tuple[str, str]] = []  # (rel_path, content_hash)
    seen: Set[str] = set()

    def _add(path: str) -> None:
        if path in seen or _is_se3_path(path):
            return
        ch = compute_file_content_hash(root / path)
        if ch is not None:
            seen.add(path)
            entries.append((path, ch))

    # --- Tracked files (working-tree content hash) -------------------------
    for rel_path in _git_ls_files_paths(root):
        _add(rel_path)

    # --- Untracked, non-ignored files --------------------------------------
    for rel_path in _git_ls_files_other(root):
        _add(rel_path)

    if not entries:
        return hashlib.sha256(b"").hexdigest()

    entries.sort(key=lambda x: x[0])
    hasher = hashlib.sha256()
    for rel_path, h in entries:
        hasher.update(f"{rel_path}\0{h}\n".encode("utf-8"))
    return hasher.hexdigest()


# The git enumeration helpers were relocated to ``file_enum.py`` so they outlive
# the ``se3 sync`` machinery (the code-index subsystem depends on them after sync
# is retired). They are re-exported here unchanged so existing sync callers keep
# importing ``sync_state._git_ls_files_paths`` etc. without modification.
from .file_enum import (  # noqa: E402  (re-export after module docstring/imports)
    _git_ls_files_other,
    _git_ls_files_paths,
    _is_se3_path,
)


# ---------------------------------------------------------------------------
# File-set change detection
# ---------------------------------------------------------------------------

def detect_file_set_change(state: SyncState, project_root: Path) -> bool:
    """Return True when the working tree has gained, lost, or renamed files
    compared to what *state*'s deps were recorded from.

    This is a conservative heuristic: when the file *set* changes, all
    per-spec (level-2) skips are invalidated because a new file might belong
    to a spec whose deps don't yet reference it.

    Current path set = tracked files that exist on disk + untracked
    non-ignored files, excluding ``se3/``.  Recorded path set = the union of
    every ``deps`` key across all ``spec_deps`` entries.
    """
    root = Path(project_root).resolve()
    current_paths: Set[str] = set()

    for rel_path in _git_ls_files_paths(root):
        if not _is_se3_path(rel_path) and (root / rel_path).is_file():
            current_paths.add(rel_path)

    for rel_path in _git_ls_files_other(root):
        if not _is_se3_path(rel_path) and (root / rel_path).is_file():
            current_paths.add(rel_path)

    if not state.spec_deps:
        return False

    recorded_paths: Set[str] = set()
    for entry in state.spec_deps.values():
        deps = entry.get("deps", {})
        if isinstance(deps, dict):
            recorded_paths.update(deps.keys())

    if not recorded_paths:
        return False

    added = current_paths - recorded_paths
    removed = recorded_paths - current_paths

    if added or removed:
        logger.info(
            "File-set change detected: +%d added, -%d removed files",
            len(added), len(removed),
        )
        return True

    return False
