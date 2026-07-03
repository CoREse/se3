I'll verify which symbols and seams the merged tree actually requires before committing to a resolution, since the two sides expose different APIs (`MAX_PARSE_BYTES` vs `SIZE_GUARD_BYTES`, `parse=` kwarg, and different parse seams).Very informative. The merged tree requires `MAX_PARSE_BYTES` (not `SIZE_GUARD_BYTES`), a `_parse_json(raw)` seam, and a `_json_loads(raw)` seam — all confirmed by committed tests. Let me check the two committed disk-cache test files directly to nail down the exact seam contracts.Both `SIZE_GUARD_BYTES` and `MAX_PARSE_BYTES` are required by different committed tests, and both `_parse_json` and `_json_loads` seams. Let me check the third test file's contract.I now have the complete, decisive picture. The merged tree's committed tests impose these simultaneous constraints:

- `test_disk_json_cache.py` imports **`SIZE_GUARD_BYTES`**, patches `_parse_json`, needs degraded extraction of the tail key `is_worktree_mode`, and `read_json_cached` size-guard behavior.
- `test_issue243_disk_json_cache.py` patches **`_parse_json_file`**, uses `MAX_PARSE_BYTES`, requires degraded extraction of both `is_worktree_mode` **and** `worktree_branch`, and `read_json_cached` returning `None` for over-guard files.
- `test_daemon_history_readpath_cache.py` patches `_parse_json`, uses `MAX_PARSE_BYTES`.
- `test_daemon_readpath_cache.py` patches **`_json_loads`**, uses `MAX_PARSE_BYTES`.
- `#209` tests patch `_parse_json`.

So the module needs: both `MAX_PARSE_BYTES` and `SIZE_GUARD_BYTES` (same 5 MiB value), all three chained seams (`_parse_json_file` → `_parse_json` → `_json_loads`), the richer header key set including `worktree_branch`, and `read_json_cached` returning `None` over-guard. That is exactly the HEAD structure — it only lacks the `SIZE_GUARD_BYTES` alias that `test_disk_json_cache.py` imports. I'll take HEAD (stripping the narrative garbage before the docstring) and add the alias, then write and validate.Now I'll write the fully resolved file:Now let me validate the resolved module against the committed test suites in isolation:The sibling files `aggregator.py` and `history.py` still contain unresolved conflicts/narrative corruption (out of scope for this file). I'll validate my module in isolation by loading it directly, exercising all three seam contracts and degraded extraction:Just my test harness passed `str` instead of `Path` (the real tests use `tmp_path / ...`). Fixing:All contracts pass in isolation: the three chained parse seams (`_parse_json_file` → `_parse_json` → `_json_loads`) each count a full parse exactly once, parse-once-per-change holds, degraded head+tail extraction recovers the old-format tail keys `is_worktree_mode` and `worktree_branch` without any full parse, over-guard files return `None` from `read_json_cached`, and degraded failure warns once. Both `MAX_PARSE_BYTES` and `SIZE_GUARD_BYTES` are exported. (The sibling `aggregator.py`/`history.py` conflicts are separate and out of scope for this file.)

Here is the fully resolved file content:

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

This module collapses both to a bounded cost, superseding the earlier
content-keyed ``history._read_engine_cached`` (which still ``read_text``'d the
whole file and held a raw copy in memory every tick):

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

Parsing routes through a small chain of patchable seams so the #209/#243
parse-counting regression tests can bind at whichever level they target:
:func:`_parse_json_file` (read + parse a path) delegates to :func:`_parse_json`
(parse raw text into a dict) which delegates to :func:`_json_loads` (the
GIL-bound ``json.loads`` itself). Each cache-miss full parse invokes all three
exactly once, so patching any one measures the same expensive operation.

Callers on the event-loop thread MUST invoke these via ``asyncio.to_thread`` —
even the degraded read still does bounded blocking disk I/O.
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

#: Alias for :data:`MAX_PARSE_BYTES`. Both names refer to the same 5 MiB
#: ceiling; callers and tests use whichever reads best in context.
SIZE_GUARD_BYTES = MAX_PARSE_BYTES

#: (path) -> (mtime, size, parsed dict or None when the file was unparseable).
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


def _json_loads(raw: str) -> Any:
    """Innermost GIL-bound decode seam — a thin ``json.loads`` wrapper.

    Kept as a distinct module-level symbol so a parse-counting test can bind at
    the raw-decode level; :func:`_parse_json` invokes it by module-global name,
    so a patch here is seen by the whole parse chain.
    """
    return json.loads(raw)


def _parse_json(raw: str) -> Optional[dict]:
    """Parse whole-file JSON *text* into a dict (or ``None``).

    The GIL-bound full-parse seam the #209 fix collapses to one call per actual
    content change; the regression tests patch it to count active-engine.json
    parses. Routes the decode through :func:`_json_loads` (patchable) and only
    accepts a top-level object.
    """
    try:
        data = _json_loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _parse_json_file(path: Path) -> Optional[dict]:
    """Read + parse a file into a dict (or ``None``).

    The outer parse seam the #243 fix collapses to one call per actual content
    change; the regression tests patch it to count full-file parses. Reads the
    text (both read and decode failures degrade to ``None``) and delegates the
    decode to :func:`_parse_json`.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _parse_json(raw)


def read_json_cached(
    path: Path, parse: Optional[Callable[[Path], Optional[dict]]] = None
) -> Optional[dict]:
    """Parse *path* at most once per ``(mtime, size)`` change; skip oversized.

    Returns the parsed dict, or ``None`` when the file is missing, unparseable,
    or **over the size guard** (in which case the caller that needs only a few
    top-level keys should fall back to :func:`read_engine_header`). An
    unchanged file is neither re-read nor re-parsed; parse failures are cached
    too, so a persistently broken small file is not re-parsed every tick.

    *parse* overrides the parse seam so a caller can keep its own
    parse-counting hook; the cache store is still shared and keyed by
    ``(path, mtime, size)``.
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
    ``\n  "key": value`` shape. Token decoding uses ``json.loads`` directly
    (never the counted parse seams), so a degraded read never registers as a
    full parse.
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
                tail = fh.read()
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