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
  completed→new-flow swap (or an in-place step-status flip) can keep the byte
  size identical and land in the same mtime tick (coarse-mtime filesystems, or
  two fast writes on ext4), a pure stat key would surface the just-superseded
  parse. So on each poll the file's WHOLE content — bounded by the size guard the
  caller has already enforced, never an unbounded read — is re-read and hashed;
  the cached parse is reused only while the ``(mtime, size)`` key AND that content
  hash both still match, and the expensive ``json.loads`` (the #209/#243 freeze)
  runs whenever either moves. Hashing the whole content (not just a bounded
  head+tail window) is what catches a rewrite confined to the file's MIDDLE — a
  deep-in-the-``state.steps``-table step-status flip in the dense
  discovery→analyze rewrite window (#260) that leaves the head/tail bytes
  identical — which a windowed hash silently masked, returning a stale parse. The
  per-poll cost stays a bounded read + one C-speed digest instead of the
  full-file ``json.loads`` it used to be.
* A size guard (:data:`MAX_PARSE_BYTES`): a file above the threshold is never
  fully parsed and its parsed body is never cached (so a giant legacy file
  cannot inflate daemon memory). The hot path instead uses
  :func:`read_engine_header`, which extracts just the few top-level keys it
  needs from a bounded head+tail read. That small *extracted header* (KB-scale,
  never the multi-MB body) IS cached, keyed by ``(path, mtime_ns, size)`` — see
  :data:`_DEGRADED_CACHE` — so an unchanged oversized archive snapshot costs one
  ``stat`` per enumeration instead of a 256 KiB head+tail re-read every tick.

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

#: (path) -> (mtime, size, content-digest or None, parsed dict or None).
#: Only entries for at-or-under-guard files are ever stored. For an immutable
#: snapshot (digest ``None``) a ``(mtime, size)`` hit is trusted — the file is
#: neither re-read nor re-parsed. For the live engine.json (``verify_content``)
#: the digest is a hash of the file's WHOLE content, re-read every poll; the
#: cached parse is reused only when the ``(mtime, size)`` key STILL matches AND
#: that content-hash matches. The stat key catches every ordinary rewrite; the
#: content hash is the extra guard that catches a same-``(mtime, size)`` swap the
#: stat key cannot see — including one confined to the file's MIDDLE, which the
#: earlier bounded head+tail window silently masked (#260). Neither check alone is
#: sufficient, so both must hold.
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

#: (path) -> (mtime_ns, size, extracted header dict or None) for OVERSIZED
#: files — the :func:`_degraded_header` result cache. Kept separate from
#: :data:`_CACHE` on purpose: the main store holds *full parses* whose
#: ``verify_content`` / ``peek_cached_header`` semantics must never be
#: satisfied by a lossy degraded extraction, while this store holds only the
#: tiny header dict (a handful of string/bool keys — never the multi-MB body,
#: so the memory ceiling the size guard exists for is preserved).
#:
#: WHY stat-keyed is enough here: the oversized population is dominated by
#: archive snapshots (``tianluo/state/archive/engine_*.json``) whose content is
#: immutable after archival — their mtime only moves when the file is replaced —
#: so a ``(mtime_ns, size)`` hit is authoritative, exactly like the
#: immutable-snapshot path of :func:`read_json_cached`. An oversized *live*
#: engine.json thereby trades away the same-``(mtime_ns, size)`` in-place-
#: rewrite detection the under-guard verify_content path provides; that is an
#: accepted degraded-mode trade-off — such a file is a legacy artifact already
#: served best-effort, every real step transition rewrites it (moving
#: ``mtime_ns``), and the alternative was the 256 KiB head+tail re-read per
#: tick that produced the residual ~1.1 MB/s idle disk load. A failed
#: extraction (``None``) is cached too, so a persistently broken oversized file
#: costs one scan (and one warning via :func:`_warn_once_degraded`), not one
#: per tick.
_DEGRADED_CACHE: "OrderedDict[str, Tuple[int, int, Optional[dict]]]" = OrderedDict()

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

#: Top-level boolean keys the daemon hot path needs. ``waiting_for_lock`` is the
#: lock-wait sub-state (emit-when-True in engine.json): an oversized engine.json
#: queued behind the main-worktree mutex must still surface it via the degraded
#: head+tail scan.
_BOOL_HEADER_KEYS = ("is_worktree_mode", "waiting_for_lock")


def clear_cache() -> None:
    """Drop all cached parses (used by tests for isolation)."""
    with _CACHE_LOCK:
        _CACHE.clear()
        _DEGRADED_CACHE.clear()
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
    The degraded-header store is dropped alongside for the same reason.
    """
    with _CACHE_LOCK:
        _CACHE.pop(key, None)
        _DEGRADED_CACHE.pop(key, None)


def cached_content_digest(path: Path) -> Optional[bytes]:
    """Return the whole-content digest of *path*'s last ``active`` read.

    The live ``engine.json`` read (``read_engine_header(active=True)``) hashes
    the file's WHOLE content each poll and caches that digest alongside the
    parse (see :func:`read_json_cached`). This exposes exactly that digest so
    the change-detection signature can fold it in *without paying a second read
    or hash*: a same-``(mtime, size)`` in-place rewrite — the PAUSE→resume
    engine.json churn that a stat-only fingerprint debounces — still shifts the
    signature because its content digest moved, so the push loop reads the delta
    on the next tick instead of stalling until an unrelated jsonl append happens
    to nudge the stat token. Correctness itself is the frontend's periodic full
    snapshot; this only trims the residual frame-delay that stat-only
    debouncing would otherwise leave on a pure in-place engine.json rewrite.

    Returns ``None`` when the path was never read via the ``active`` path, when
    it degraded (oversized, never hashed), or when its last read failed — the
    caller then falls back to the ``(mtime, size)`` token alone.
    """
    with _CACHE_LOCK:
        cached = _CACHE.get(str(path))
    if cached is None:
        return None
    return cached[2]


def peek_cached_header(path: Path) -> Optional[dict]:
    """Return *path*'s cached parse ONLY on a ``(mtime_ns, size)`` stat hit.

    A pure lookup — one ``stat`` and a dict probe, never a read or parse. On a
    miss (first sighting, changed/oversized/vanished file, or a cached parse
    failure) it returns ``None`` and the caller chooses which read path to pay.

    WHY: this is the cheap pre-pass the 1s fast tick uses to decide whether a
    root's engine.json is even worth the ``active=True`` verify_content read
    (whole-file read + hash). A terminal (completed / failed) flow's cached
    header lets the tick skip that read entirely; routing the peek through
    :func:`read_engine_header` instead would parse on a miss WITHOUT recording
    a content digest, forcing the verify read that follows to parse the same
    unchanged file a second time (the issue-#209 parse-once invariant).

    The stat hit is trusted here exactly like the immutable-snapshot path in
    :func:`read_json_cached` — the caller must treat the result as a *hint*
    and re-verify through ``active=True`` before acting on a live flow.
    """
    stat = _safe_stat(path)
    if stat is None:
        _drop_entry(str(path))
        return None
    mtime, size = stat
    key = str(path)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == mtime and cached[1] == size:
            # Refresh LRU recency: a terminal engine.json served by peek every
            # tick must not be evicted under the cap (a drop would re-trigger
            # its full verify read).
            _CACHE.move_to_end(key)
            return cached[3]
    return None


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
    # identical ``st_mtime_ns``. That swap is caught by the whole-content hash in
    # :func:`read_json_cached` (verify_content), not by this key.
    return (st.st_mtime_ns, st.st_size)


def _read_active_content(path: Path) -> Optional[bytes]:
    """Read the WHOLE content of *path* for the active-engine.json freshness hash.

    Backs the ``verify_content`` change-detection: the cached parse for the live
    engine.json is reused only while a hash of this content is unchanged. Hashing
    the ENTIRE file — not merely a bounded head+tail window — is what catches a
    rewrite confined to the file's MIDDLE (a deep-in-the-``state.steps``-table
    step-status flip in the dense discovery→analyze rewrite window, #260) that
    keeps ``(mtime, size)`` and both former 64 KiB windows byte-identical; the old
    windowed hash silently masked exactly that and returned a stale parse.

    The read is bounded: :func:`read_json_cached` only reaches the verify path for
    a file at or under :data:`MAX_PARSE_BYTES` (an oversized legacy engine.json
    degrades to the stat-keyed head+tail scan instead), so this reads at most
    that many bytes and hashes them through a fast C digest — never the whole-file
    ``json.loads`` (which still runs only when the digest moves), so it does not
    reintroduce the #209/#243 per-tick parse sink. Returns ``None`` on an I/O
    error, which the caller surfaces as "unreadable".
    """
    try:
        with open(path, "rb") as fh:
            return fh.read()
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
    force_fresh: bool = False,
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
      swap — or an in-place step-status flip — can preserve the file size and
      share an ``st_mtime_ns`` (coarse-mtime filesystem, or two fast writes), so a
      pure stat hit could return the just-superseded parse. On each poll the WHOLE
      content (bounded by the size guard) is re-read and hashed; the cached parse
      is reused only while BOTH the ``(mtime, size)`` key and that content-hash are
      unchanged. Hashing the whole content — not merely a head+tail window —
      additionally catches a rewrite confined to the file's MIDDLE (a
      deep-in-the-``state.steps``-table status flip in the dense discovery→analyze
      rewrite window, #260) that keeps head/tail bytes identical; the earlier
      windowed hash masked exactly that and returned a stale ``state``. Per-poll
      cost is a bounded read + one C-speed hash, not the full-file read + parse it
      used to be, while any real flow change is caught.

    *force_fresh* (only meaningful with ``verify_content``) bypasses the cached
    parse and forces one fresh read + parse this call, then refreshes the cache.
    It is the *true-value* fallback :func:`~tianluo.daemon.history` uses on the rare
    active-flow *drop* decision: rather than exclude a still-active flow on a
    cache result that could be a same-``(mtime, size)`` collision, it re-confirms
    against disk before acting.

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

    # Active engine.json. The content hash is a SAME-STAT safeguard layered on
    # top of the (mtime, size) key, never a replacement for it. Two distinct
    # change classes must both force a reparse:
    #
    #  * A normal in-place rewrite advances st_mtime_ns (and usually st_size),
    #    so ``stat_hit`` is False → reparse.
    #  * A completed→new-flow swap — or a same-size in-place step-status flip —
    #    on a coarse-mtime filesystem (tmpfs / overlayfs, and observably even ext4
    #    for two writes in one jiffy) can keep the byte size identical AND share an
    #    st_mtime_ns, so ``stat_hit`` is True yet the content differs. Hashing the
    #    WHOLE content catches exactly that, wherever the change lands — head, tail
    #    OR the true middle of the ``state.steps`` table. The earlier head+tail
    #    window missed a middle-only rewrite (both windows byte-identical) and
    #    served a stale parse into the dense discovery→analyze rewrite window
    #    (#260); the whole-content hash closes that blind spot.
    #
    # The cached parse is therefore reused only when BOTH the stat key matches
    # AND the content hash matches (and *force_fresh* is not requested); any change
    # to either pays for one full read + parse, so the expensive decode still runs
    # at most once per real change while never masking a mid-file mutation.
    content = _read_active_content(path)
    if content is None:
        return None
    digest = hashlib.blake2b(content, digest_size=16).digest()
    if not force_fresh and stat_hit and cached[2] is not None and cached[2] == digest:
        with _CACHE_LOCK:
            # Refresh LRU recency for the live engine.json so the cap never
            # evicts the one file polled every tick.
            if key in _CACHE:
                _CACHE.move_to_end(key)
        # HOP-1 (change-detection) DEBUG observability: the live engine.json's
        # cached parse was REUSED on a (mtime,size)+whole-content hit — i.e. the
        # file is byte-identical to the last parse, so the reuse is correct.
        # Logged so a live run shows how often the active engine.json is served
        # from cache vs re-parsed across the discovery→analyze boundary.
        logger.debug(
            "hist-diag disk_json_cache: active engine.json REUSE cached parse "
            "path=%s mtime_ns=%s size=%s (whole-content match)",
            path, mtime, size,
        )
        return cached[3]
    logger.debug(
        "hist-diag disk_json_cache: active engine.json RE-PARSE path=%s "
        "mtime_ns=%s size=%s stat_hit=%s force_fresh=%s",
        path, mtime, size, stat_hit, force_fresh,
    )
    parsed = _parse_json_file(path)
    with _CACHE_LOCK:
        _store(key, (mtime, size, digest, parsed))
    return parsed


def read_engine_header(
    path: Path,
    parse: Optional[Callable[[Path], Optional[dict]]] = None,
    active: bool = False,
    force_fresh: bool = False,
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
      *visible* in the WebUI without ever freezing the loop. The small
      *extracted header* (including a ``None`` extraction failure) IS cached
      keyed by ``(path, mtime_ns, size)`` — see :data:`_DEGRADED_CACHE` — so an
      unchanged oversized archive snapshot costs one ``stat`` per enumeration
      instead of a 256 KiB head+tail re-read. Degraded extraction that cannot
      even find ``flow_id`` returns ``None`` and warns once.

    *active* ``True`` reads the at/under-guard file with
    ``verify_content=True`` — for the one live ``engine.json`` whose in-place,
    possibly same-size / same-mtime rewrites must not be masked by a stat-only
    cache hit (see :func:`read_json_cached`). It re-hashes the file's whole
    content each poll and reuses the parse while that content is unchanged, so the
    decode still runs at most once per real change while a middle-only rewrite is
    never masked. An oversized active file degrades to the stat-keyed head+tail
    scan — accepting that a same-``(mtime_ns, size)`` in-place rewrite is masked
    there (the trade-off :data:`_DEGRADED_CACHE` records), since degraded mode is
    already a best-effort legacy path.

    *force_fresh* (only with ``active``) bypasses the cached parse for this call —
    the true-value re-confirmation the active-flow *drop* decision uses so a
    still-active flow is never excluded on a stale/collided cache result.
    """
    stat = _safe_stat(path)
    if stat is None:
        # Path gone: drop any stale entry so a deleted file's parse is not
        # pinned for the daemon's lifetime (mirrors read_json_cached).
        _drop_entry(str(path))
        return None
    mtime, size = stat
    if size > MAX_PARSE_BYTES:
        key = str(path)
        # force_fresh is the drop-decision's true-value re-confirmation: it
        # must reach disk even here, or the re-confirm would just echo the
        # possibly-collided cached header it is meant to double-check.
        if not force_fresh:
            with _CACHE_LOCK:
                cached = _DEGRADED_CACHE.get(key)
                if cached is not None and cached[0] == mtime and cached[1] == size:
                    # Stat hit on an oversized file: serve the cached header
                    # (possibly a cached None failure — a broken file is scanned
                    # once, not once per tick). Refresh LRU recency so the archive
                    # working set enumerated every rebuild is not evicted.
                    _DEGRADED_CACHE.move_to_end(key)
                    return cached[2]
        header = _degraded_header(path, size)
        with _CACHE_LOCK:
            _DEGRADED_CACHE[key] = (mtime, size, header)
            _DEGRADED_CACHE.move_to_end(key)
            # Same LRU bound as the main store: each entry is a tiny header
            # dict, but a long-lived daemon must not pin one per path ever seen.
            while len(_DEGRADED_CACHE) > _MAX_CACHE_ENTRIES:
                _DEGRADED_CACHE.popitem(last=False)
        return header
    return read_json_cached(
        path, parse=parse, verify_content=active, force_fresh=force_fresh
    )


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