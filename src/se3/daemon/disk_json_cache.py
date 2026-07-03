"""Unified, thread-safe disk-JSON parse cache for the daemon control plane.

Every periodic daemon reader (aggregator snapshot build, history index,
historical-root enumeration) touches the same on-disk JSON artifacts —
``engine.json``, archived ``engine_*.json`` snapshots, per-flow ``resumable``
snapshots, per-history ``_meta.json`` — once or more *per push tick* (~1s).
Two failure modes fall out of parsing them naively:

* **Event-loop freeze (#243 病灶 1).** A completed worktree run can leave a
  tens-of-MB legacy ``engine.json`` on disk. Re-``json.loads``-ing it every
  tick pins a CPU core inside ``raw_decode`` and starves the push loop, which
  is the root cause of the observed WebUI freeze.
* **Executor saturation (#243 病灶 2).** Historical/archive re-enumeration
  parses large archive snapshots (up to ~100MB) in full only to read a couple
  of top-level keys.

This module collapses both to a bounded cost:

* A single module-level cache keyed by ``(path, mtime, size)`` — an unchanged
  file is neither re-read nor re-parsed. completed / archived files never
  change again, so they are parsed exactly once for the daemon's lifetime.
* A size guard (:data:`MAX_PARSE_BYTES`): a file above the threshold is never
  fully parsed and its content/result is never cached (so a giant legacy file
  cannot inflate daemon memory). The hot path instead uses
  :func:`read_engine_header`, which extracts just the few top-level keys it
  needs from a bounded head+tail read.

The cache lives here (not in ``aggregator`` / ``history``) so both subsystems
share one keyed store and one size guard, per the #243 design.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: Files larger than this are never fully parsed nor cached. 5 MiB is the
#: #243 decision: comfortably above any KB-scale new-format header while well
#: below the tens-of-MB legacy engine.json / archive snapshots that caused the
#: freeze. A new-format engine.json (header only) is always far below this, so
#: the guard never degrades a current-format read.
MAX_PARSE_BYTES = 5 * 1024 * 1024

#: (path, mtime, size) -> parsed dict (or None when the file was unparseable).
#: Only entries for at-or-under-guard files are ever stored.
_CACHE: Dict[str, Tuple[float, int, Optional[dict]]] = {}
_CACHE_LOCK = threading.Lock()

#: Paths already warned about (degraded extraction failure), so a persistently
#: broken oversized file warns once rather than every tick.
_WARNED: set = set()
_WARNED_LOCK = threading.Lock()

#: Bounded head/tail window for degraded oversized-file header extraction.
#: The top-level identity keys of an ``indent=2`` engine.json live at the very
#: start of the file (``flow_id`` / ``status`` / ``task_description``) or at the
#: very end, after the giant ``state`` object (``is_worktree_mode`` and the
#: ``worktree_*`` fields). 128 KiB each side comfortably covers both bands.
_DEGRADED_WINDOW = 128 * 1024

#: Top-level string keys the daemon hot path needs from any engine snapshot.
_STR_HEADER_KEYS = (
    "flow_id",
    "status",
    "task_description",
    "task_type",
    "project_root",
    "updated_at",
    "worktree_branch",
    "worktree_path",
    "worktree_original_branch",
)

#: Top-level boolean keys the daemon hot path needs.
_BOOL_HEADER_KEYS = ("is_worktree_mode", "waiting_for_lock")


def clear_cache() -> None:
    """Drop all cached parses (used by tests for isolation)."""
    with _CACHE_LOCK:
        _CACHE.clear()
    with _WARNED_LOCK:
        _WARNED.clear()


def _warn_once(path: Path, message: str) -> None:
    key = str(path)
    with _WARNED_LOCK:
        if key in _WARNED:
            return
        _WARNED.add(key)
    logger.warning(message)


def _safe_stat(path: Path) -> Optional[Tuple[float, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def _parse_json_file(path: Path) -> Optional[dict]:
    """Read + ``json.loads`` a file into a dict (or ``None``).

    This is the single GIL-bound parse seam the #243 fix collapses to one call
    per actual content change; the regression tests patch it to count parses.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_json_cached(
    path: Path, parse: Optional[Callable[[Path], Optional[dict]]] = None
) -> Optional[dict]:
    """Parse *path* at most once per ``(mtime, size)`` change; skip oversized.

    Returns the parsed dict, or ``None`` when the file is missing, unparseable,
    or **over the size guard** (in which case the caller that needs only a few
    top-level keys should fall back to :func:`read_engine_header`). An
    unchanged file is neither re-read nor re-parsed.

    *parse* overrides the parse seam so a caller can keep its own
    parse-counting hook (history's ``_parse_engine_json``); the cache store is
    still shared and keyed by ``(path, mtime, size)``.
    """
    stat = _safe_stat(path)
    if stat is None:
        return None
    mtime, size = stat
    # Oversized files are never parsed nor cached: parsing would pin a core and
    # caching would pin the multi-MB result in daemon memory.
    if size > MAX_PARSE_BYTES:
        return None

    key = str(path)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == mtime and cached[1] == size:
            return cached[2]

    parser = parse or _parse_json_file
    parsed = parser(path)

    with _CACHE_LOCK:
        _CACHE[key] = (mtime, size, parsed)
    return parsed


def read_engine_header(
    path: Path, parse: Optional[Callable[[Path], Optional[dict]]] = None
) -> Optional[dict]:
    """Return the small top-level header of an engine.json / snapshot file.

    The daemon hot path only ever needs a handful of top-level keys
    (``flow_id`` / ``status`` / ``is_worktree_mode`` / ``project_root`` and a
    few decorative fields). This returns exactly that, cheaply, for both
    formats:

    * **At/under the guard** — a full cached parse (new-format headers are KB
      and legacy small files parse once); the whole dict is returned, so
      callers that also want ``state`` (progress) keep working unchanged.
    * **Over the guard** — a bounded head+tail read scans for the top-level
      keys directly. The oversized body is never fully parsed nor cached, so a
      giant legacy engine.json belonging to a still-active worktree run stays
      *visible* in the WebUI without ever freezing the loop. Degraded
      extraction that cannot even find ``flow_id`` returns ``None`` and
      warns once.
    """
    stat = _safe_stat(path)
    if stat is None:
        return None
    _mtime, size = stat
    if size <= MAX_PARSE_BYTES:
        return read_json_cached(path, parse=parse)
    return _degraded_header(path, size)


def _degraded_header(path: Path, size: int) -> Optional[dict]:
    """Extract top-level header keys from an oversized ``indent=2`` JSON file.

    Reads only a bounded head and tail window (never the whole file) and
    regex-scans for the two-space-indented top-level keys the daemon needs.
    Relies on ``persistence.py`` writing engine.json via
    ``json.dumps(..., indent=2)``, which gives every top-level key a stable
    ``\\n  "key": value`` shape.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(_DEGRADED_WINDOW)
            if size > _DEGRADED_WINDOW * 2:
                fh.seek(size - _DEGRADED_WINDOW)
                tail = fh.read(_DEGRADED_WINDOW)
            else:
                # Small overlap region: head already reached (or passed) the
                # tail band, so re-reading from the window boundary avoids
                # double-counting while still covering the file's end.
                remaining = fh.read()
                tail = remaining
    except OSError:
        return None

    # Join with a newline so a key sitting exactly at the head boundary still
    # has the ``\n  "`` anchor the regexes below require.
    text = head.decode("utf-8", "replace") + "\n" + tail.decode("utf-8", "replace")

    result: Dict[str, Any] = {}
    for key in _STR_HEADER_KEYS:
        m = re.search(r'\n  "' + re.escape(key) + r'":\s*"((?:[^"\\]|\\.)*)"', text)
        if m is not None:
            try:
                result[key] = json.loads('"' + m.group(1) + '"')
            except ValueError:
                result[key] = m.group(1)
    for key in _BOOL_HEADER_KEYS:
        m = re.search(r'\n  "' + re.escape(key) + r'":\s*(true|false)', text)
        if m is not None:
            result[key] = m.group(1) == "true"

    if "flow_id" not in result:
        _warn_once(
            path,
            "disk_json_cache: degraded header extraction failed for oversized "
            f"file {path} (no flow_id found in head+tail window); skipping",
        )
        return None
    return result
