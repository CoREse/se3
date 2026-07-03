I need to see what the already-merged callers expect from this module to pick the right resolution. Let me inspect the codebase.Both callers use `read_engine_header` and `read_json_cached` — both versions provide these. Now let me check which internal parse seam the tests patch.The staged/merged test files (`test_issue_209_push_starvation.py`, `test_issue209_live_append_regression.py`, `test_daemon_history_readpath_cache.py`) all patch `_parse_json` — the G3 seam. Let me confirm the other test's status and check `history.py`'s expectations more closely.`test_daemon_readpath_cache.py` (from G2, committed) patches `_json_loads`, while three other tests patch `_parse_json`. Both test files are tracked. I need the resolved file to satisfy both. Let me read the G2 test in detail.This is now clear. The resolution must be the **G3 version**:

- The merged `history.py` and `aggregator.py` import `read_engine_header`/`read_json_cached` and their comments match G3's semantics (returns `None` on failure; caller warns).
- The three staged/modified tests (`test_issue_209_push_starvation.py`, `test_issue209_live_append_regression.py`, `test_daemon_history_readpath_cache.py`) all patch `_parse_json` — the G3 seam.
- The one test aligned with HEAD's `_json_loads`/`_CACHE` (`test_daemon_readpath_cache.py`, from G2) is *already broken independently* against the merged tree: its `_patch_all_parse_seams` patches `history_mod._parse_engine_json` and `history_mod._read_json`, both of which the merged `history.py` removed. It cannot pass regardless and is out of scope for this file.

So I take the G3 side wholesale.

Here is the fully resolved file content:

```python
"""Unified ``(path, mtime, size)``-keyed disk-JSON parse cache for the daemon.

The daemon's control plane is a *pure reader* of the files ``se3 run`` leaves on
disk. Its hot paths (the aggregator's per-tick ``_active_worktree_run_roots`` /
snapshot builds and the history reader's index / historical-root enumeration)
re-touch the SAME ``engine.json`` / archive snapshot / resumable snapshot /
``_meta.json`` files on every ~1 s push tick. ``json.loads`` of a multi-MB
``engine.json`` is a GIL-bound parse; repeated tick × reader it starves the
event loop and was a root cause of the #209 WebUI freeze (the live-append frame
never got pushed).

This module is the single seam that fixes it, superseding the earlier
content-keyed ``history._read_engine_cached`` (which still ``read_text``'d the
whole file and held a raw copy in memory every tick). Two guarantees:

* **stat-keyed caching** — a parse is keyed by ``(path, mtime, size)``. When a
  file has not changed since the last read the cached result is returned WITHOUT
  re-reading OR re-parsing it. A completed / archived flow's file never changes
  again, so it is parsed exactly once for the life of the process.

* **size guard + degraded read** — a file larger than :data:`MAX_PARSE_BYTES`
  (5 MiB) is *never* fully parsed. Instead a bounded head+tail window is read and
  the few top-level *hot* keys the daemon actually needs (``flow_id``,
  ``status``, ``is_worktree_mode``, ``project_root``) are scanned out of it,
  relying on the stable ``json.dumps(..., indent=2)`` two-space top-level
  indentation ``se3.engine.persistence`` writes. Oversized files' content and
  results are NEVER cached (bounding memory); the bounded re-scan per tick is the
  guardrail that keeps a 50 MB legacy ``engine.json`` from pinning a CPU. A
  degraded read that extracts nothing usable returns ``None`` so the caller skips
  the file (and warns once via its own dedup).

Callers on the event-loop thread MUST invoke these via ``asyncio.to_thread`` —
even the degraded read still does bounded blocking disk I/O.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: Files strictly larger than this are never fully parsed — only a bounded
#: head+tail window is read and scanned for the hot top-level keys. 5 MiB is the
#: decided threshold: comfortably above a healthy new-format header (KB-level)
#: and a normal ~1 MB long-running-flow ``engine.json``, but below the pathological
#: tens-of-MB legacy files that triggered the freeze.
MAX_PARSE_BYTES = 5 * 1024 * 1024

#: Bytes read from each end of an oversized file during a degraded read. The hot
#: keys live at the extremes of a legacy ``engine.json``: ``flow_id`` / ``status``
#: head the file, while ``is_worktree_mode`` / ``worktree_*`` trail the giant
#: ``state`` blob at the tail (see ``FlowInstance.to_dict``). 128 KiB per end is
#: generous slack for those bands while keeping the per-tick read bounded.
HEAD_TAIL_WINDOW_BYTES = 128 * 1024

#: The top-level *hot* keys a degraded read extracts. These are the only fields
#: the daemon's periodic hot paths consult (flow identity, liveness, worktree
#: mode, owning project root); everything else — inputs/outputs, the step table,
#: token usage — is irrelevant to those paths and is intentionally not recovered
#: from an oversized file.
_HOT_KEYS: Tuple[str, ...] = ("flow_id", "status", "is_worktree_mode", "project_root")

#: Precompiled per-key matchers for a top-level (exactly two-space-indented)
#: scalar entry in an ``indent=2`` JSON object. The value is captured
#: non-greedily up to an optional trailing comma so it decodes as a standalone
#: JSON token. A nested key (>=4 spaces) never matches, so a ``"status"`` buried
#: inside the ``state`` blob cannot be mistaken for the flow's top-level status.
_HOT_KEY_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    key: re.compile(r'(?m)^  "' + re.escape(key) + r'":[ \t]+(.+?),?[ \t]*$')
    for key in _HOT_KEYS
}

# Cache: path-string -> ((mtime, size), parsed-or-None). Module-level and
# thread-safe so every reader instance and every executor thread shares one
# parse per actual file change.
_CACHE_LOCK = threading.Lock()
_cache: Dict[str, Tuple[Tuple[float, int], Optional[Dict[str, Any]]]] = {}


def clear_cache() -> None:
    """Drop the entire parse cache. For tests / explicit invalidation only."""
    with _CACHE_LOCK:
        _cache.clear()


def _safe_stat(path: Path) -> Optional[Tuple[float, int]]:
    """Return *path*'s ``(mtime, size)``, or ``None`` when it is unreadable."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def _parse_json(raw: str) -> Optional[Dict[str, Any]]:
    """Full ``json.loads`` of a whole file's text into a dict (or ``None``).

    This is the single GIL-bound full-parse seam the #209 fix collapses to one
    call per actual change; the parse-counting regression tests patch exactly
    this symbol. The bounded degraded read deliberately does NOT route through
    here (it decodes only a handful of tiny scalar tokens), so a count on this
    seam measures only the expensive whole-file parses.
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _load_and_parse(path: Path) -> Optional[Dict[str, Any]]:
    """Read *path*'s whole text and full-parse it (both may fail → ``None``)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _parse_json(raw)


def _read_head_tail(path: Path, size: int) -> Optional[str]:
    """Read a bounded head+tail window of an oversized file as text.

    Returns the head window concatenated with the tail window (separated by a
    newline so a partial line truncated at either window edge can never fuse
    into a spurious cross-window match). Because this is only ever called for a
    file above :data:`MAX_PARSE_BYTES` the two windows never overlap. Undecodable
    bytes are replaced rather than raising, so a window that slices a multi-byte
    UTF-8 sequence still yields scannable text.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEAD_TAIL_WINDOW_BYTES)
            fh.seek(max(0, size - HEAD_TAIL_WINDOW_BYTES))
            tail = fh.read(HEAD_TAIL_WINDOW_BYTES)
    except OSError:
        return None
    return head.decode("utf-8", "replace") + "\n" + tail.decode("utf-8", "replace")


def _scan_hot_keys(blob: str) -> Dict[str, Any]:
    """Extract the top-level hot keys present in an ``indent=2`` text *blob*.

    Only keys whose value decodes as a standalone JSON scalar are included; a
    key absent from the scanned windows (or one whose captured token fails to
    decode) is simply omitted. Returns a possibly-empty dict — the caller treats
    empty as a degraded-extraction failure.
    """
    out: Dict[str, Any] = {}
    for key, pattern in _HOT_KEY_PATTERNS.items():
        m = pattern.search(blob)
        if not m:
            continue
        try:
            out[key] = json.loads(m.group(1))
        except ValueError:
            continue
    return out


def _degraded_header(path: Path, size: int) -> Optional[Dict[str, Any]]:
    """Bounded head+tail extraction of the hot keys from an oversized file.

    Never full-parses and never caches (guarding memory against a 50 MB file).
    Returns the extracted-keys dict, or ``None`` when the window is unreadable
    or yields no usable key so the caller skips (and warns once about) the file.
    """
    blob = _read_head_tail(path, size)
    if blob is None:
        return None
    header = _scan_hot_keys(blob)
    return header or None


def read_json_cached(path: Path) -> Optional[Dict[str, Any]]:
    """Return the parsed JSON object at *path*, cached by ``(path, mtime, size)``.

    An unchanged file is served from the cache with neither a re-read nor a
    re-parse. A file over :data:`MAX_PARSE_BYTES` is degraded to a bounded
    head+tail hot-key extraction (never cached). Returns ``None`` on any read /
    parse / extraction failure.

    Used for arbitrary small daemon JSON (e.g. ``_meta.json``); for engine-shaped
    state files prefer the intent-named :func:`read_engine_header`.
    """
    return _read_cached(path)


def read_engine_header(path: Path) -> Optional[Dict[str, Any]]:
    """Return the header of an ``engine.json`` / archive / resumable snapshot.

    A new-format engine file is a KB-level header, so the full parse *is* the
    header; a small legacy file is parsed in full and its hot keys read off the
    resulting dict. Either way the result is cached by ``(path, mtime, size)`` and
    parsed at most once per change. An oversized legacy file is degraded to a
    bounded head+tail hot-key extraction (``flow_id`` / ``status`` /
    ``is_worktree_mode`` / ``project_root``) so an active worktree run with a
    giant legacy ``engine.json`` stays visible in the WebUI without a full parse.
    Returns ``None`` on failure so the caller skips the file.
    """
    return _read_cached(path)


def _read_cached(path: Path) -> Optional[Dict[str, Any]]:
    """Core stat-keyed read shared by the public readers (see their docstrings)."""
    stat = _safe_stat(path)
    if stat is None:
        return None
    mtime, size = stat
    key = str(path)
    with _CACHE_LOCK:
        entry = _cache.get(key)
        if entry is not None and entry[0] == (mtime, size):
            return entry[1]
    # Oversized: bounded degraded read, never cached (memory guard). A completed
    # legacy file is stat-stable, so the per-tick cost is a bounded head+tail
    # re-read, not a tens-of-MB re-parse.
    if size > MAX_PARSE_BYTES:
        return _degraded_header(path, size)
    parsed = _load_and_parse(path)
    with _CACHE_LOCK:
        _cache[key] = ((mtime, size), parsed)
    return parsed
```