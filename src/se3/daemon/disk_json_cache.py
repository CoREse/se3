"""Unified ``(path, mtime, size)``-keyed disk-JSON parse cache for the daemon.

Part A of the #243 / #244 daemon read-path hardening. The daemon's per-tick hot
paths (:class:`~se3.daemon.aggregator.DaemonAggregator` and the history reader)
repeatedly touch the *same* on-disk ``engine.json`` / archived / resumable
snapshots and ``_meta.json`` files every ~1 s. Parsing a multi-megabyte
``engine.json`` with ``json.loads`` is a GIL-bound CPU sink that, repeated tick
× reader, starved the event loop and froze the WebUI (issue #209 / #243).

This module collapses those repeated parses to **one parse per actual file
change** and guards against pathological giant files:

* The cache key is ``(path, mtime, size)``. When a file has not changed since the
  last read it is neither re-read nor re-parsed — a completed / archived flow's
  ``engine.json`` never changes again, so it is parsed exactly once for the
  daemon's whole lifetime. This *supersedes* the older content-keyed
  ``history._read_engine_cached`` (which still re-read the whole file and kept a
  raw copy in memory every tick).

* A module-level size threshold (:data:`MAX_PARSE_BYTES`, 5 MiB) is a hard
  guardrail: a file above it is **never** fully parsed and **never** cached
  (neither its raw bytes nor a parsed dict), so a 50 MB legacy ``engine.json``
  can never blow up either CPU or memory. Hot-path callers that only need a few
  top-level keys use :func:`read_engine_header`, which degrades an oversized
  file to a bounded head+tail scan instead of a full parse — keeping an active
  worktree run with a giant legacy ``engine.json`` visible in the WebUI rather
  than dropping it.

The cache is process-global and thread-safe, so every reader instance and every
``asyncio.to_thread`` worker shares one parse result per file.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: Files strictly larger than this are never fully parsed and never cached. The
#: value is a deliberate trade-off: comfortably above a normal *new-format*
#: KB-level header (Part B) and even a healthy inlined-format ``engine.json``,
#: but far below the tens-of-MB legacy files that caused the freeze. Above the
#: threshold, hot-path callers fall back to :func:`read_engine_header`'s bounded
#: degraded scan.
MAX_PARSE_BYTES = 5 * 1024 * 1024  # 5 MiB

#: Bounded window (bytes) read from the head and from the tail of an oversized
#: file by the degraded header scan. The hot top-level keys live either right at
#: the file head (``flow_id`` / ``status`` / ``task_description`` — before the
#: giant ``state`` dict) or at the tail (``is_worktree_mode`` / ``worktree_*`` —
#: after it), so scanning both ends captures them without ever loading the
#: multi-MB middle.
_DEGRADED_WINDOW_BYTES = 64 * 1024

#: The top-level scalar keys the degraded reader extracts from an oversized
#: engine.json-shaped file. These are exactly the daemon's hot-path needs:
#: worktree-run discovery (``flow_id`` + ``is_worktree_mode``), flow-card
#: rendering (``status`` / ``task_description`` / ``task_type`` /
#: ``waiting_for_lock``) and historical-root enumeration (``project_root``).
_HEADER_KEYS = (
    "flow_id",
    "status",
    "task_description",
    "task_type",
    "is_worktree_mode",
    "project_root",
    "waiting_for_lock",
)

# path -> ((mtime, size), parsed-json-or-None). Guarded by ``_LOCK`` so a
# worker thread and the event loop never race on it. Bounded by the number of
# distinct on-disk JSON files the daemon tracks (one engine.json per root, plus
# archives/snapshots), which is small and stable.
_CACHE: Dict[str, Tuple[Tuple[float, int], Optional[Any]]] = {}
_LOCK = threading.Lock()

#: Paths already warned about (degraded extraction failed). First sighting is a
#: WARNING; the file is then skipped silently so a permanently unparseable giant
#: file cannot flood the daemon log on every tick.
_warned_degraded: set = set()
_WARN_LOCK = threading.Lock()


def _json_loads(raw: str) -> Optional[Any]:
    """Parse *raw* JSON text into a Python object, or ``None`` on any error.

    This is the single GIL-bound parse seam the cache collapses; the regression
    tests patch it to count how many times a file is actually *parsed* (as
    opposed to read from cache), locking in the "once per change" guarantee.
    """
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _stat(path: Path) -> Optional[Tuple[float, int]]:
    """Return *path*'s ``(mtime, size)``, or ``None`` when missing / unreadable."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def read_json_cached(path: Path) -> Optional[Any]:
    """Read and parse *path*, reusing the cached parse when it has not changed.

    The parse result is memoized under the ``(path, mtime, size)`` key: an
    unchanged file is neither re-read nor re-parsed. A file above
    :data:`MAX_PARSE_BYTES` is a no-op (returns ``None`` without reading or
    parsing) — callers that must still recover a few keys from such a file use
    :func:`read_engine_header`. Returns ``None`` on any stat / read / parse
    error, mirroring the daemon's prior tolerant ``_read_json`` behaviour.
    """
    st = _stat(path)
    key = str(path)
    if st is None:
        # File vanished / unreadable: drop any stale entry so a later re-creation
        # at the same path is re-parsed rather than served from the dead cache.
        with _LOCK:
            _CACHE.pop(key, None)
        return None
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == st:
            return cached[1]
    _mtime, size = st
    if size > MAX_PARSE_BYTES:
        # Size guardrail: never full-parse or cache an oversized file. Its parse
        # result is intentionally kept out of the cache to prevent memory growth.
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    parsed = _json_loads(raw)
    with _LOCK:
        _CACHE[key] = (st, parsed)
    return parsed


def read_engine_header(path: Path) -> Optional[dict]:
    """Return an ``engine.json``-shaped header dict, cheaply.

    For a file within :data:`MAX_PARSE_BYTES` this is exactly
    :func:`read_json_cached` (a cached full parse, so callers still get the
    complete ``state`` for progress rendering when it is affordable). For an
    oversized file it degrades to a bounded head+tail scan that extracts only the
    hot top-level scalar keys (:data:`_HEADER_KEYS`) — never a full parse, never
    cached — so a giant legacy ``engine.json`` on an *active* worktree run is
    still discoverable (``flow_id`` / ``is_worktree_mode``) and renderable
    (``status`` / ``task_description``) in the WebUI instead of being dropped.

    Returns ``None`` when the file is missing, is not a JSON object, or (when
    oversized) yields no extractable header keys — the last case is warned once
    per path so the skip is observable without flooding the log.
    """
    st = _stat(path)
    if st is None:
        return None
    _mtime, size = st
    if size <= MAX_PARSE_BYTES:
        data = read_json_cached(path)
        return data if isinstance(data, dict) else None
    header = _degraded_header(path, size)
    if not header:
        _warn_once_degraded(path)
        return None
    return header


def _degraded_header(path: Path, size: int) -> Optional[dict]:
    """Extract hot top-level keys from an oversized engine.json without a full parse.

    Reads a bounded window from the file head and another from the tail (see
    :data:`_DEGRADED_WINDOW_BYTES`) and scans each for the :data:`_HEADER_KEYS`.
    This relies on ``persistence.py`` writing engine.json with
    ``json.dumps(..., indent=2)``: every *top-level* key sits at exactly two
    spaces of indentation (``\\n  "key": value``), while every nested key inside
    the giant ``state`` dict is indented deeper — so a ``^  "key"`` anchored match
    can never be fooled by a same-named nested key. Returns the extracted dict
    (possibly partial) or ``None`` when nothing could be read.
    """
    head_n = min(_DEGRADED_WINDOW_BYTES, size)
    tail_n = min(_DEGRADED_WINDOW_BYTES, size)
    try:
        with open(path, "rb") as fh:
            head = fh.read(head_n)
            if size > tail_n:
                fh.seek(size - tail_n)
            tail = fh.read(tail_n)
    except OSError:
        return None
    # ``errors="replace"`` keeps a byte split across the window edge from raising;
    # the anchored key regex simply won't match a mangled boundary line, and the
    # key we want is never itself at the cut point (they cluster at the two ends).
    text = head.decode("utf-8", errors="replace") + "\n" + tail.decode(
        "utf-8", errors="replace"
    )
    return _extract_top_level_scalars(text, _HEADER_KEYS)


#: Matches the value token immediately after a ``  "key":`` at top-level indent.
#: Only scalar JSON values are captured (string / bool / null / number); an
#: object / array value (e.g. ``state``) is deliberately not matched — the
#: degraded reader only recovers scalars.
_SCALAR_VALUE = re.compile(
    r'\s*(?:"(?P<str>(?:[^"\\]|\\.)*)"'
    r"|(?P<bool>true|false)"
    r"|(?P<null>null)"
    r"|(?P<num>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?))"
)


def _extract_top_level_scalars(text: str, keys) -> dict:
    """Extract the given top-level scalar *keys* from indent=2 JSON *text*."""
    result: dict = {}
    for key in keys:
        anchor = re.search(r'^  "' + re.escape(key) + r'"\s*:', text, re.MULTILINE)
        if anchor is None:
            continue
        m = _SCALAR_VALUE.match(text, anchor.end())
        if m is None:
            continue
        if m.group("str") is not None:
            # Round-trip through json to unescape (\n, \uXXXX, embedded quotes).
            try:
                result[key] = json.loads(text[m.start("str") - 1 : m.end("str") + 1])
            except ValueError:  # pragma: no cover - defensive
                result[key] = m.group("str")
        elif m.group("bool") is not None:
            result[key] = m.group("bool") == "true"
        elif m.group("null") is not None:
            result[key] = None
        elif m.group("num") is not None:
            num = m.group("num")
            result[key] = int(num) if re.fullmatch(r"-?\d+", num) else float(num)
    return result


def _warn_once_degraded(path: Path) -> None:
    """Warn a single time that *path* could not be degraded-parsed, then skip."""
    key = str(path)
    with _WARN_LOCK:
        if key in _warned_degraded:
            logger.debug("disk_json_cache: skipping oversized unreadable %s", path)
            return
        _warned_degraded.add(key)
    logger.warning(
        "disk_json_cache: oversized file %s exceeds %d bytes and no header keys "
        "could be extracted; skipping it",
        path,
        MAX_PARSE_BYTES,
    )


def clear_cache() -> None:
    """Drop all cached parses (and warn-once state). Intended for tests."""
    with _LOCK:
        _CACHE.clear()
    with _WARN_LOCK:
        _warned_degraded.clear()
