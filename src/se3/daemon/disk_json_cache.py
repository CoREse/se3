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

* A single module-level cache in :func:`read_json_cached`. For the *immutable*
  snapshots (completed/archived engine.json, resumable snapshot, history
  ``_meta.json``) the key is ``(path, mtime, size)`` — their mtime only advances
  when their content does, so a matching key is authoritative: the file is
  neither re-read nor re-parsed and is parsed exactly once. The live
  ``engine.json`` uses the SAME function with ``verify_content=True``: because a
  completed→new-flow swap can keep the byte size identical and land in the same
  mtime tick (coarse-mtime filesystems, or two fast writes on ext4), a pure stat
  key would surface the just-superseded flow. So on each poll a bounded head+tail
  *window* — never the whole file — is re-read and hashed; the cached parse is
  reused only while the ``(mtime, size)`` key AND that window hash both still
  match, and the expensive ``json.loads`` (the #209/#243 freeze) runs whenever
  either moves. The stat key still catches a mid-file ``state`` change the window
  can't see; the window hash is the extra guard for a same-stat content swap.
  This keeps the per-poll cost a small bounded read + hash instead of the
  full-file read+parse it used to be.
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

import hashlib
import json
import logging
import re
import threading
from collections import OrderedDict
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

#: (path) -> (mtime, size, window-digest or None, parsed dict or None).
#: Only entries for at-or-under-guard files are ever stored. For an immutable
#: snapshot (digest ``None``) a ``(mtime, size)`` hit is trusted — the file is
#: neither re-read nor re-parsed. For the live engine.json (``verify_content``)
#: the digest is a hash of a bounded head+tail window that is re-read every poll;
#: the cached parse is reused only when the ``(mtime, size)`` key STILL matches
#: AND that window-hash matches. The stat key catches every ordinary rewrite
#: (including a mid-file ``state`` change the window can't see); the window hash
#: is the extra guard that catches a same-size / coarse-mtime content swap the
#: stat key cannot see. Neither check alone is sufficient, so both must hold.
#:
#: An ``OrderedDict`` keyed most-recently-used-last so the store can be bounded:
#: without a cap a long-lived daemon would pin one fully-parsed dict per path it
#: has *ever* observed (every archive/resumable/_meta.json across every root and
#: worktree — each parse typically 2-5x its source size in RAM), and entries for
#: files that were later deleted would never be dropped. Both leaks are closed by
#: :func:`_store` (LRU-evict past :data:`_MAX_CACHE_ENTRIES`) and by dropping an
#: entry the moment its path fails to stat (:func:`_drop_entry`).
_CACHE: "OrderedDict[str, Tuple[int, int, Optional[bytes], Optional[dict]]]" = (
    OrderedDict()
)
_CACHE_LOCK = threading.Lock()

#: Upper bound on cached parses. A daemon realistically tracks a handful of
#: roots, each with tens of archive/resumable snapshots plus per-history
#: ``_meta.json`` files; 512 comfortably covers the live working set while
#: capping worst-case resident parsed dicts (each under the 5 MiB guard). The
#: least-recently-used entry is evicted once the store would exceed this, so RSS
#: does not grow monotonically with every file the daemon has ever seen.
_MAX_CACHE_ENTRIES = 512

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

#: Bounded head/tail window hashed to detect a live-engine.json content swap on a
#: ``(mtime, size)`` collision (:func:`read_json_cached`, ``verify_content``).
#: Covers the head identity/status cluster and the tail worktree cluster — the
#: fields that decide active-flow staleness — without re-reading the whole file.
_VERIFY_WINDOW = 64 * 1024

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


def _store(key: str, value: Tuple[int, int, Optional[bytes], Optional[dict]]) -> None:
    """Insert/refresh an entry as most-recently-used and enforce the LRU cap.

    Caller holds ``_CACHE_LOCK``. Keeping the store bounded here (rather than
    letting it grow with every path ever read) is the memory-bloat protection:
    the oldest entries are evicted once the count would exceed
    :data:`_MAX_CACHE_ENTRIES`.
    """
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    while len(_CACHE) > _MAX_CACHE_ENTRIES:
        _CACHE.popitem(last=False)


def _drop_entry(key: str) -> None:
    """Remove a cached entry whose backing path no longer stats.

    A deleted worktree/archive file must not leak its parsed dict for the
    daemon's lifetime: the failed stat that bypasses the cache also drops the
    stale entry so resident memory tracks live files, not historical ones.
    """
    with _CACHE_LOCK:
        _CACHE.pop(key, None)


def _warn_once_degraded(path: Path) -> None:
    """Warn (once per path) that degraded header extraction failed for *path*.

    Deduplicated by path so a persistently broken oversized file — re-scanned on
    every push tick — warns exactly once rather than flooding the daemon log.
    """
    key = str(path)
    with _WARNED_LOCK:
        if key in _WARNED:
            return
        _WARNED.add(key)
    logger.warning(
        "disk_json_cache: degraded header extraction failed for oversized "
        "file %s (no flow_id found in head+tail window); skipping",
        path,
    )


def _safe_stat(path: Path) -> Optional[Tuple[int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    # Key on integer ``st_mtime_ns`` rather than the float ``st_mtime`` (a
    # float64 only resolves an epoch-scale mtime to ~100 ns). Even the ns integer
    # is not sufficient on its own for the live engine.json: coarse-mtime
    # filesystems (and fast successive writes) can give two same-size rewrites an
    # identical ``st_mtime_ns``. That swap is caught by the bounded-window content
    # check in :func:`read_json_cached` (verify_content), not by this key.
    return (st.st_mtime_ns, st.st_size)


def _read_bounded_window(path: Path, size: int) -> Optional[bytes]:
    """Read a bounded head+tail slice of *path* for cheap change-detection.

    Backs the active-engine.json verification: hashing this slice (rather than the
    whole file) detects a flow identity/status swap — those keys live at the head
    of an ``indent=2`` engine.json, the worktree keys at the tail — without the
    full-file read the #243 fix-iteration-3 finding removed. A file no larger than
    one window on each side is read whole (so the hash then covers everything).
    Returns ``None`` on an I/O error, which the caller surfaces as "unreadable".
    """
    try:
        with open(path, "rb") as fh:
            if size <= _VERIFY_WINDOW * 2:
                return fh.read()
            head = fh.read(_VERIFY_WINDOW)
            fh.seek(size - _VERIFY_WINDOW)
            tail = fh.read(_VERIFY_WINDOW)
            return head + tail
    except OSError:
        return None


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
    path: Path,
    parse: Optional[Callable[[Path], Optional[dict]]] = None,
    verify_content: bool = False,
) -> Optional[dict]:
    """Parse *path* at most once per content change; skip oversized files.

    Returns the parsed dict, or ``None`` when the file is missing, unparseable,
    or **over the size guard** (in which case the caller that needs only a few
    top-level keys should fall back to :func:`read_engine_header`). Parse
    failures are cached too, so a persistently broken small file is not
    re-parsed every tick.

    The unified store for every daemon-side JSON read:

    * **Immutable snapshots** (``verify_content`` False — completed/archived
      engine.json, resumable snapshots, history ``_meta.json``): keyed by
      ``(path, mtime, size)``. Their mtime only advances when their content does,
      so a matching key is authoritative — the file is neither re-read nor
      re-parsed and is parsed exactly once.
    * **The live engine.json** (``verify_content`` True): a completed→new-flow
      swap can preserve the file size and share an ``st_mtime_ns`` (coarse-mtime
      filesystem, or two fast writes), so a pure stat hit could return the
      just-superseded flow. On each poll a bounded head+tail *window* (never the
      whole file) is re-read and hashed; the cached parse is reused only while
      BOTH the ``(mtime, size)`` key and that window-hash are unchanged. The stat
      key alone reparses every ordinary rewrite — including one that touches only
      the middle ``state`` block while the head/tail windows stay identical (so
      the daemon never serves a stale ``state``); the window hash is the extra
      guard that additionally catches a same-size / same-mtime swap. Per-poll
      cost is a small bounded read + hash, not the full-file read + parse it used
      to be, while any real flow change is caught.

    *parse* overrides the parse seam so a caller can keep its own parse-counting
    hook (honored only on the non-``verify_content`` path; the content path reads
    and parses through :func:`_parse_json_file`).
    """
    key = str(path)
    stat = _safe_stat(path)
    if stat is None:
        # Path gone (deleted worktree/archive): drop any stale entry so its
        # parsed dict is not pinned for the daemon's lifetime.
        _drop_entry(key)
        return None
    mtime, size = stat
    # Oversized files are never parsed nor cached: parsing would pin a core and
    # caching would pin the multi-MB result in daemon memory.
    if size > MAX_PARSE_BYTES:
        return None

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        stat_hit = cached is not None and cached[0] == mtime and cached[1] == size
        if not verify_content and stat_hit:
            # Immutable snapshot (completed/archived engine.json, resumable
            # snapshot, history _meta.json): its mtime only advances when its
            # content does, so a matching (path, mtime, size) is authoritative —
            # the file is neither re-read nor re-parsed. Refresh LRU recency so
            # a live-working-set entry is not evicted under the cap.
            _CACHE.move_to_end(key)
            return cached[3]

    if not verify_content:
        parser = parse or _parse_json_file
        parsed = parser(path)
        with _CACHE_LOCK:
            _store(key, (mtime, size, None, parsed))
        return parsed

    # Active engine.json. The window hash is a SAME-STAT safeguard layered on
    # top of the (mtime, size) key, never a replacement for it. Two distinct
    # change classes must both force a reparse:
    #
    #  * A normal in-place rewrite advances st_mtime_ns (and usually st_size),
    #    so ``stat_hit`` is False → reparse. Crucially this covers an
    #    under-guard legacy engine.json whose only change is in the middle
    #    ``state`` block (e.g. current_step_index / a step status flip) while the
    #    head+tail windows stay byte-identical: the digest alone would miss it,
    #    but the advanced mtime does not, so the stale ``state`` is never served.
    #  * A completed→new-flow swap on a coarse-mtime filesystem (tmpfs /
    #    overlayfs, and observably even ext4 for two writes in one jiffy) can
    #    keep the byte size identical AND share an st_mtime_ns, so ``stat_hit``
    #    is True yet the content differs. The bounded head+tail window hash
    #    catches exactly that: the flow identity/status keys live at the head of
    #    an indent=2 engine.json and the worktree keys at the tail, so a swap
    #    changes the window even when the stat key cannot see it.
    #
    # The cached parse is therefore reused only when BOTH the stat key matches
    # AND the window hash matches; any change to either pays for one full read +
    # parse, so the expensive decode still runs at most once per real change
    # while never masking a mid-file mutation the head+tail window can't see.
    window = _read_bounded_window(path, size)
    if window is None:
        return None
    digest = hashlib.blake2b(window, digest_size=16).digest()
    if stat_hit and cached[2] is not None and cached[2] == digest:
        with _CACHE_LOCK:
            # Refresh LRU recency for the live engine.json so the cap never
            # evicts the one file polled every tick.
            if key in _CACHE:
                _CACHE.move_to_end(key)
        return cached[3]
    parsed = _parse_json_file(path)
    with _CACHE_LOCK:
        _store(key, (mtime, size, digest, parsed))
    return parsed


def read_engine_header(
    path: Path,
    parse: Optional[Callable[[Path], Optional[dict]]] = None,
    active: bool = False,
) -> Optional[dict]:
    """Return the small top-level header of an engine.json / snapshot file.

    The daemon hot path only ever needs a handful of top-level keys
    (``flow_id`` / ``status`` / ``is_worktree_mode`` / ``project_root`` and a
    few decorative fields). This returns exactly that, cheaply, for both
    formats:

    * **At/under the guard** — the unified :func:`read_json_cached` cache
      (new-format headers are KB and legacy small files parse once); the whole
      dict is returned, so callers that also want ``state`` (progress) keep
      working unchanged.
    * **Over the guard** — a bounded head+tail read scans for the top-level
      keys directly. The oversized body is never fully parsed nor cached, so a
      giant legacy engine.json belonging to a still-active worktree run stays
      *visible* in the WebUI without ever freezing the loop. Degraded
      extraction that cannot even find ``flow_id`` returns ``None`` and
      warns once.

    *active* ``True`` reads the at/under-guard file with
    ``verify_content=True`` — for the one live ``engine.json`` whose in-place,
    possibly same-size / same-mtime rewrites must not be masked by a stat-only
    cache hit (see :func:`read_json_cached`). It is NOT a full re-read: only a
    bounded head+tail window is re-hashed each poll, and the parse is reused while
    that window is unchanged, so the decode still runs at most once per real
    change. An oversized active file degrades via the always-fresh head+tail
    scan, so it is never stale either way.
    """
    stat = _safe_stat(path)
    if stat is None:
        # Path gone: drop any stale entry so a deleted file's parse is not
        # pinned for the daemon's lifetime (mirrors read_json_cached).
        _drop_entry(str(path))
        return None
    _mtime, size = stat
    if size > MAX_PARSE_BYTES:
        return _degraded_header(path, size)
    return read_json_cached(path, parse=parse, verify_content=active)


def _join_head_tail(head_text: str, tail_text: str, separate: bool) -> str:
    """Join a bounded head window and tail window into scannable text.

    When *separate* is true the tail was seeked to ``size - window`` and its
    first line is a partial fragment of whatever line the byte cut landed in —
    typically a deeply-indented step/context copy of a hot key deep inside the
    giant ``state`` block. Fabricating a ``\\n`` in front of that fragment (the
    old behaviour) would forge a ``\\n  "key"`` top-level anchor out of a nested
    key whose indentation was truncated to two spaces, so a nested copy of
    ``is_worktree_mode`` / ``worktree_path`` / ``project_root`` could be misread
    as the file's real top-level value. Dropping everything up to and including
    the first genuine newline discards that partial line, and slicing *from*
    the newline keeps a real line-start anchor for every surviving tail line, so
    only genuine top-level keys can match. When *separate* is false, head and
    tail are contiguous (the whole file), so they are concatenated verbatim.
    """
    if not separate:
        return head_text + tail_text
    nl = tail_text.find("\n")
    tail_text = tail_text[nl:] if nl != -1 else ""
    return head_text + tail_text


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
                separate = True
            else:
                # Small overlap region: head already reached (or passed) the
                # tail band, so re-reading from the window boundary avoids
                # double-counting while still covering the file's end.
                tail = fh.read()
                separate = False
    except OSError:
        return None

    text = _join_head_tail(
        head.decode("utf-8", "replace"),
        tail.decode("utf-8", "replace"),
        separate,
    )

    result: Dict[str, Any] = {}
    for key in _STR_HEADER_KEYS:
        # Exclude a raw newline from the value class: a valid single-line indent=2
        # JSON string never contains one (newlines are escaped as ``\n``). Without
        # this, a top-level string value whose closing quote is truncated by the
        # head-window boundary produces a seam-spanning match — the captured
        # fragment runs from the head remainder across the joined tail up to the
        # first stray quote, so a >128KiB task_description would be surfaced as
        # garbage. Barring raw newlines turns a boundary-truncated value into a
        # clean miss instead (identity/status keys precede task_description, so
        # only the decorative field is ever affected).
        m = re.search(r'\n  "' + re.escape(key) + r'":\s*"((?:[^"\\\n]|\\.)*)"', text)
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
        _warn_once_degraded(path)
        return None
    return result