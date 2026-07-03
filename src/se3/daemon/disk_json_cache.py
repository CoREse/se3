"""Unified ``(path, mtime, size)``-keyed disk JSON parse cache with a size guard.

The daemon's per-tick push loop touches the same on-disk JSON — the active
``engine.json``, archived ``engine_*.json`` snapshots, resumable snapshots and
per-flow ``_meta.json`` — through several readers every tick. Reading the bytes
is cheap; ``json.loads`` on a multi-MB (worst case 50MB+) file is a GIL-bound
parse that, repeated tick × reader, starves the asyncio event loop. That is the
root pathology behind the #209 WebUI freeze and the #243 daemon-read-path work:
two completed worktree runs left 50MB+34MB ``engine.json`` files on disk and the
push loop wedged at 100% in ``raw_decode``.

This module collapses those repeats to *one parse per genuine file change* and
puts a hard ceiling on how large a file the hot path will ever fully parse:

* :func:`read_json_cached` keys the parsed object on ``(str(path), mtime,
  size)`` — an unchanged file (the common case: completed / archived flows never
  change again) is never re-read *or* re-parsed after the first miss. This
  supersedes the earlier content-keyed ``_read_engine_cached`` (which still
  ``read_text``'d the whole file every tick and kept a raw copy resident).

* :func:`read_engine_header` adds the :data:`SIZE_GUARD_BYTES` guard: a file
  above the threshold is *never* fully parsed and *never* cached (neither its
  bytes nor a result — that would defeat the memory ceiling). Instead it is read
  degraded — a bounded head+tail slice scanned for the handful of top-level keys
  the hot path actually needs — so a live worktree run whose ``engine.json`` is a
  giant legacy file stays visible in the WebUI rather than vanishing.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)

#: Files at or below this size are parsed in full and their result cached; files
#: above it are never fully parsed nor cached (see :func:`read_engine_header`).
#: 5 MiB comfortably covers a KB-scale new-format header and a healthy legacy
#: ``engine.json``, while excluding the tens-of-MB pathological files that
#: wedged the event loop.
SIZE_GUARD_BYTES = 5 * 1024 * 1024

#: How many bytes to read from each of the file's head and tail on the degraded
#: path. The old-format top-level keys split into a head cluster
#: (``flow_id``/``status``/``task_description``/``task_type``, before the giant
#: ``state`` dict) and a tail cluster (``updated_at`` and the ``worktree_*`` /
#: ``is_worktree_mode`` fields, after it), so both ends must be sampled. 64 KiB
#: is generous headroom for either cluster while staying bounded.
_HEAD_TAIL_BYTES = 64 * 1024

#: The small set of top-level keys the daemon hot paths need from an oversized
#: engine.json (identity + status + worktree/root routing), extracted without a
#: full parse on the degraded path.
_HEADER_KEYS: Tuple[str, ...] = (
    "flow_id",
    "status",
    "is_worktree_mode",
    "project_root",
    "task_description",
    "task_type",
    "updated_at",
)

#: Matches a single top-level key line of an ``indent=2`` ``json.dumps`` file:
#: exactly two leading spaces (nested keys are indented deeper), a quoted
#: snake_case key, then its value as the rest of the physical line. json.dumps
#: escapes newlines inside string values, so every physical line break is
#: structural and this reliably isolates top-level scalar keys.
_TOP_LEVEL_RE = re.compile(r'^  "([a-z_]+)": (.*)$', re.MULTILINE)

# Module-level parse cache, keyed by (str(path), mtime, size). Shared across all
# reader instances; guarded by a lock because the daemon parses on several
# executor threads concurrently.
_CACHE: Dict[Tuple[str, float, int], Optional[dict]] = {}
_CACHE_LOCK = threading.Lock()

# One-time warn dedup for degraded-read extraction failures, so a single
# unreadable oversized file does not spam a warning every tick.
_WARNED: Set[str] = set()
_WARNED_LOCK = threading.Lock()


def _parse_json(text: str) -> Optional[dict]:
    """Parse *text* into a dict, or ``None`` on malformed / non-object JSON.

    This is the single ``json.loads`` seam the regression tests patch to count
    full-file parses; keep every cached full parse routed through here.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def read_json_cached(path: Path) -> Optional[dict]:
    """Read and parse *path*, parsing at most once per ``(mtime, size)`` change.

    On a cache hit (same path + mtime + size as a prior call) the already-parsed
    object is returned without touching the disk at all. On a miss the file is
    read and parsed once via :func:`_parse_json` and the result cached. Any OS
    error (missing / unreadable) or parse failure yields ``None``; a parse
    failure is still cached against the current ``(mtime, size)`` so a corrupt
    unchanged file is not re-parsed every tick.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime, st.st_size)
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    parsed = _parse_json(text)
    with _CACHE_LOCK:
        _CACHE[key] = parsed
    return parsed


def read_engine_header(path: Path) -> Optional[dict]:
    """Return the small top-level header of an ``engine.json`` / snapshot.

    For a file at or below :data:`SIZE_GUARD_BYTES` this is exactly
    :func:`read_json_cached` (full parse, cached). Above the threshold the file
    is read *degraded*: bounded head and tail slices are scanned for the
    top-level keys in :data:`_HEADER_KEYS` (which for the old format live at the
    two ends, straddling the giant ``state`` dict). The oversized file's bytes
    and extracted result are never cached, so memory stays bounded no matter how
    many giant files exist.

    Returns the extracted header dict, or ``None`` when the file is unreadable or
    the degraded scan cannot recover the minimum identity (``flow_id``) — in the
    latter case a warning is emitted once per path.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= SIZE_GUARD_BYTES:
        return read_json_cached(path)
    return _read_engine_header_degraded(path, size)


def _read_engine_header_degraded(path: Path, size: int) -> Optional[dict]:
    """Extract header keys from an oversized file via a bounded head+tail scan."""
    try:
        with path.open("rb") as fh:
            head = fh.read(_HEAD_TAIL_BYTES)
            if size > _HEAD_TAIL_BYTES:
                fh.seek(max(0, size - _HEAD_TAIL_BYTES))
                tail = fh.read(_HEAD_TAIL_BYTES)
            else:
                tail = b""
    except OSError:
        return None
    # A newline between the slices guarantees the tail's first key line starts at
    # a fresh ``^`` even though the raw seek lands mid-line; ``errors="ignore"``
    # drops any partial multi-byte char at either cut boundary.
    text = head.decode("utf-8", "ignore") + "\n" + tail.decode("utf-8", "ignore")

    extracted: Dict[str, object] = {}
    for match in _TOP_LEVEL_RE.finditer(text):
        key = match.group(1)
        if key not in _HEADER_KEYS or key in extracted:
            continue
        raw_val = match.group(2).rstrip()
        if raw_val.endswith(","):
            raw_val = raw_val[:-1]
        try:
            extracted[key] = json.loads(raw_val)
        except ValueError:
            # A value truncated by the head/tail cut (or the ``"state": {``
            # container opener) is not a scalar we can recover — skip it.
            continue

    if not extracted.get("flow_id"):
        _warn_once_degraded(path, size)
        return None
    return extracted


def _warn_once_degraded(path: Path, size: int) -> None:
    """Warn (at most once per path) that a degraded header read failed."""
    key = str(path)
    with _WARNED_LOCK:
        if key in _WARNED:
            return
        _WARNED.add(key)
    logger.warning(
        "disk_json_cache: could not extract engine header from oversized "
        "file %s (%d bytes); skipping it",
        path,
        size,
    )


def clear_cache() -> None:
    """Clear the parse cache and warn-once state (test / diagnostic hook)."""
    with _CACHE_LOCK:
        _CACHE.clear()
    with _WARNED_LOCK:
        _WARNED.clear()
