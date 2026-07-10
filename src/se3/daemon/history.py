"""Historical-session reading for the SE3 daemon.

:class:`DaemonHistoryReader` enumerates the ``se3 history`` artifacts of every
project root the daemon tracks and turns them into:

* a *session index* — one :class:`SessionMeta` per known flow (flow id, task
  description, status, timestamps, active flag) — reported to the central
  server via :data:`~se3.daemon.protocol.MSG_HISTORY_INDEX` so the web UI can
  list historical sessions;
* incremental *flow reads* — the per-step ``jsonl`` conversation records of a
  single flow (:func:`read_flow`), returned either as a ``full`` snapshot or as
  an ``append`` delta keyed off a per-step *line cursor*.

Like :class:`~se3.daemon.aggregator.DaemonAggregator`, the reader is a pure
reader of the files ``se3 run`` leaves on disk — it never touches a flow's
process. The central server is only an in-memory relay; nothing here writes or
persists anything server-side.

Sources, per project root:

* ``se3/state/engine.json`` — the *active* flow (status decides whether it is
  still running);
* ``se3/state/archive/engine_*.json`` — archived, terminated flows;
* ``se3/history/<flow_id>/`` — history-only flows that may have no surviving
  ``engine.json`` at all (best-effort metadata is still produced for them).

Cursor model
------------
A *cursor* is a ``{jsonl-filename: line-count-consumed}`` dict. ``jsonl`` files
are append-only, so a line count is a stable, monotonic incremental marker. An
empty / absent cursor requests a ``full`` snapshot; a populated one requests an
``append`` delta. :data:`MAX_RECORDS_PER_REPORT` bounds a single read so a very
large flow cannot OOM the daemon or the wire — the cursor simply advances part
way and the remainder is picked up by the next request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from .disk_json_cache import (
    cached_content_digest,
    read_engine_header,
    read_json_cached,
)
from .protocol import HISTORY_MODE_APPEND, HISTORY_MODE_FULL
from .supervisor import resolve_worktree_main_root

logger = logging.getLogger(__name__)

#: Paths already reported as unreadable at WARNING level. The first time a
#: corrupt archive / meta file is seen it gets one WARNING; every subsequent
#: encounter of the *same* path is logged at DEBUG. This keeps a permanently
#: corrupt ``_meta.json`` (re-scanned on every historical enumeration) from
#: flooding ``daemon.log`` while preserving first-sight observability. The set
#: is naturally bounded by the (small) number of corrupt files on disk.
_warned_unreadable_paths: Set[str] = set()


def _warn_once_unreadable(path: Path, kind: str) -> None:
    """Log an unreadable history file, deduplicated by path.

    *kind* is a short noun (``"archive file"`` / ``"meta file"``) spliced into
    the message. The first sighting of *path* is a WARNING; repeats are DEBUG.
    """
    key = str(path)
    if key in _warned_unreadable_paths:
        logger.debug("history: skipping unreadable %s %s", kind, path)
        return
    _warned_unreadable_paths.add(key)
    logger.warning("history: skipping unreadable %s %s", kind, path)


#: Hard cap on the number of history records a single :func:`read_flow` call
#: returns. When a flow has more new records than this, the read is truncated
#: and the returned cursor advances only as far as the truncation point, so the
#: caller picks the rest up incrementally on its next request.
MAX_RECORDS_PER_REPORT = 2000

#: Rewrite detection must satisfy the hard correctness guarantee — catch ANY
#: change to the already-consumed prefix no matter which part the rewrite
#: preserved — while staying far cheaper than the whole-file
#: ``read_text().splitlines()`` re-read the byte-offset incremental path replaced.
#:
#: A **running full-prefix** hash over the WHOLE consumed region ``[0, offset)`` is
#: the only complete rewrite signal, so it is used uniformly whenever the file has
#: NOT shrunk (both the grow and the equal-size cases).  The hash is maintained
#: *incrementally from the bytes already read* as records are consumed (so the
#: append path never re-reads the prefix to advance it) and is verified against a
#: fresh disk hash whenever a read could be incremental: a genuine append leaves
#: the entire consumed prefix byte-for-byte identical, so the two hashes match,
#: whereas an in-place retry that re-runs the step changes at least one prefix byte
#: — the leading prompt/status head, the trailing terminal record, or any record
#: in the MIDDLE of a large prefix — so they diverge and the stale offset/cursor is
#: discarded.  Earlier iterations used bounded sampled windows (a head window
#: ``[0, W)`` and a boundary window ``[offset - W, offset)``, each
#: ``W = HEAD_SIGNATURE_BYTES`` bytes) as a cheaper grow-case guard, but together
#: they only cover a consumed prefix up to ``2·W`` bytes: a retry that preserved
#: both the head and the boundary while changing a record between them (and grew
#: the file) slipped through, trusted the stale offset, and delivered only the
#: replacement suffix — dropping the start of the retry batch until a full reload
#: (issue #209, fix iterations 6–8).  The whole-prefix hash closes that gap for
#: every prefix size.  The disk verification is bounded by the consumed offset
#: (it reads at most ``offset`` bytes even after the file has grown) and streams
#: raw bytes through a fast C digest with no Python-level UTF-8 decode /
#: splitlines / record construction, so it never reintroduces the decode-bound
#: whole-file re-read on the append path, and the genuinely new appended tail is
#: still read only once.
#:
#: A ``stat``-identical (same size AND mtime) result is deliberately NOT treated
#: as a zero-read "untouched" fast path: under a coarse mtime granularity an
#: equal-size in-place rewrite landing within the same mtime tick is
#: indistinguishable from "untouched" by ``stat`` alone, so the content hash runs
#: regardless of mtime.
HEAD_SIGNATURE_BYTES = 128

#: Chunk size (bytes) for streaming the consumed region through the full-prefix
#: disk hash, so a multi-megabyte prefix is hashed without loading it all into
#: memory at once.
SIGNATURE_CHUNK_BYTES = 1 << 20  # 1 MiB

#: Size of the bounded **boundary** window (the last bytes ending at the consumed
#: offset).  Rewrite detection no longer relies on bounded windows — it hashes the
#: whole consumed prefix (see above), which is the only signal that catches a
#: middle-of-prefix change — but the head/boundary fingerprints are still recorded
#: in the offset table (and this constant is still referenced by callers/tests),
#: so the size is retained as a stable alias.
BOUNDARY_SIGNATURE_BYTES = HEAD_SIGNATURE_BYTES

#: Flow statuses that mean a flow is no longer running. Anything else on the
#: *active* (``engine.json``) source is treated as an in-progress session.
_TERMINAL_STATUSES = frozenset({"completed", "failed"})


def _is_active_status(status: str) -> bool:
    """Return whether *status* names a still-running (non-terminal) flow.

    Anything that is not COMPLETED / FAILED counts as active — INIT, RUNNING,
    PAUSED and RECOVERING included — so a flow that pauses (a discovery
    clarification, a confirm gate) and is later resumed keeps being read
    incrementally rather than dropping out of the active set the moment it
    pauses. The comparison is case-insensitive because ``engine.json`` stores
    the enum value verbatim (e.g. ``"RUNNING"`` / ``"PAUSED"``).
    """
    return status.strip().lower() not in _TERMINAL_STATUSES


def _is_resumable_status(status: str) -> bool:
    """Return whether *status* names a resumable flow.

    Mirrors :func:`se3.daemon.aggregator._is_resumable_status`: every flow that
    has NOT completed normally is resumable — running (interrupted), paused
    (awaiting input), failed (recoverable error) and the transient
    init/recovering states all qualify. Only ``completed`` is terminal-and-done.
    """
    return status.strip().lower() != "completed"


#: The authoritative set of step types, mirroring ``StepType`` values in
#: ``se3.engine.models``. Hard-coded (not imported) on purpose: importing
#: ``se3.engine.models`` would execute ``se3.engine.__init__`` and drag the
#: whole engine (``llm_caller`` / ``state_machine`` / …) into the daemon's
#: import graph, against the daemon's deferred-import design. Used only as a
#: *soft* confidence signal by :func:`parse_step_type_from_step_id`; a name
#: that follows the file-name convention but is absent here still parses to its
#: middle segment, so a future step type drifting out of this set degrades to
#: best-effort rather than to a wrong answer.
_KNOWN_STEP_TYPES = frozenset(
    {
        "discovery",
        "analyze",
        "project_summary",
        "plan",
        "propose",
        "design",
        "plan_tasks",
        "confirm",
        "implement",
        "test",
        "self_check",
        "verify_spec",
        "update_spec",
        "version_analyze",
        "commit",
        "summarize",
    }
)


def parse_step_type_from_step_id(stem: str) -> str:
    """Parse the authoritative step type out of a history jsonl file-name stem.

    Real daemon chat-history files are named by the convention
    ``NN_<step_type>_<hash>(_Gk)`` (the file-name stem; ``NN`` is a two-digit
    sequence number, ``<hash>`` a hexadecimal id, and an optional ``_Gk`` group
    suffix is appended for DAG-grouped steps). The raw ``message`` records the
    daemon pushes carry *no* ``step_type`` field, so this parser recovers it
    deterministically from the stem instead of having the frontend guess.

    The middle segment may itself contain underscores (e.g. ``version_analyze``),
    so the parser peels the known structural pieces — leading ``NN_`` sequence,
    trailing ``_G\\d+`` group suffix, trailing hexadecimal hash — and treats
    whatever remains as the step type. Examples::

        01_discovery_975607bb      -> "discovery"
        13_version_analyze_def456  -> "version_analyze"
        05_implement_61605e42_G2   -> "implement"

    The result is soft-validated against :data:`_KNOWN_STEP_TYPES`. A name that
    clearly follows the convention but whose type is not (yet) known still
    returns its parsed middle segment (self-describing, drift-tolerant). An
    old / non-conforming name with no sequence prefix and no hash tail (e.g.
    the legacy ``commit_summary``) gracefully falls back to the original stem
    rather than guessing.

    This is a pure function: it never reads disk, has no side effects, and never
    raises — any unexpected input yields an empty string.
    """
    try:
        if not isinstance(stem, str):
            return ""
        original = stem.strip()
        if not original:
            return ""

        # Defensive: if a physical / sidecar file name slipped through (e.g.
        # ``01_discovery_ab12.jsonl`` or
        # ``01_discovery_ab12.jsonl.from-worktree__b``) instead of a logical
        # step id, reduce it to the logical id by stripping the ``.jsonl``
        # extension and any trailing ``.from-<branch>`` sidecar suffix first, so
        # callers that pass a raw name still parse the right step type.
        marker = original.find(".jsonl")
        if marker >= 0:
            original = original[:marker].strip()
            if not original:
                return ""

        s = original

        # 1. Strip a leading numeric sequence number ("NN_").
        without_seq = re.sub(r"^\d+_", "", s, count=1)
        seq_stripped = without_seq != s
        s = without_seq

        # 2. Strip an optional trailing group suffix ("_G\\d+").
        without_group = re.sub(r"_G\d+$", "", s, count=1)
        group_stripped = without_group != s
        s = without_group

        # 3. Strip a trailing hexadecimal hash segment, keeping the middle
        #    (which may itself contain underscores, e.g. "version_analyze").
        hash_stripped = False
        parts = s.split("_")
        if (
            len(parts) >= 2
            and parts[-1]
            and re.fullmatch(r"[0-9a-fA-F]+", parts[-1])
        ):
            s = "_".join(parts[:-1])
            hash_stripped = True

        candidate = s.strip("_")

        # Soft-validate against the known step types (authoritative confidence).
        if candidate in _KNOWN_STEP_TYPES:
            return candidate

        # Followed the naming convention (had a sequence / hash / group marker)
        # but the type is not in the known set: the parsed middle is still the
        # best self-describing answer, so future step types keep working.
        if candidate and (seq_stripped or hash_stripped or group_stripped):
            return candidate

        # Old / non-conforming name (no NN prefix, no hash, e.g.
        # "commit_summary"): fall back to the original stem rather than guess.
        return original
    except Exception:  # pragma: no cover - parser must never raise
        return ""


#: Index task-description fields are clipped to this many characters.
_DESC_CLIP = 200

#: TTL (seconds) for the cached :meth:`DaemonHistoryReader.build_index` result.
#: The daemon client's :meth:`~se3.daemon.client.DaemonClient._push_loop` calls
#: ``build_index`` every fast tick (1 s) via ``_push_history``.  On a machine
#: with a large history tree the full directory walk + JSON parse is expensive
#: enough to saturate the thread-pool workers and starve the event loop of CPU
#: (the same class of stall the aggregator's ``HISTORICAL_ROOTS_TTL`` fixed for
#: ``all_project_roots``).  Caching the index for a conservative window
#: collapses repeated identical rebuilds into one, while still reflecting new
#: flows within the window.
BUILD_INDEX_TTL = 3.0

#: Cross-root flow_id de-duplication precedence (lower number = higher priority).
#: When the SAME flow_id is found under more than one root — the classic
#: ``se3 run --worktree`` split where the pre-fork discovery dir survives in the
#: main repo as a *history-only* entry while the live ``engine.json`` + later
#: steps live under the worktree subdir — the higher-precedence source wins
#: REGARDLESS of which root is enumerated first. ``_iter_roots`` enumerates the
#: main repo before its ``se3/worktrees/<name>`` subdir (a main root sorts
#: before a path nested under it), so a plain first-claim-wins dedup would record
#: the active worktree flow as a non-active ``history`` row under the MAIN root:
#: it would then neither be recognised as active (so never stream live) nor — for
#: the index's recorded authoritative root — point at the worktree. Letting an
#: ``active`` claim supersede a ``history`` claim keeps the authoritative
#: ``SessionMeta.project_root`` on the worktree root.
_SOURCE_PRIORITY = {"active": 0, "archived": 1, "resumable": 2, "history": 3}

#: Type of the project-roots provider — a zero-arg callable returning the roots
#: the daemon currently tracks (typically ``aggregator.project_roots``).
ProjectRootsProvider = Callable[[], Iterable[Any]]


@dataclass
class SessionMeta:
    """Metadata describing one history session (one flow)."""

    flow_id: str
    project_root: str
    task_description: str = ""
    task_type: str = ""
    status: str = "unknown"
    created_at: str = ""
    updated_at: str = ""
    active: bool = False
    source: str = "history"  # "active" | "archived" | "resumable" | "history"
    step_count: int = 0
    # Running sub-state mirrored from the active engine.json's top-level
    # ``waiting_for_lock`` flag: True while a synchronous run is queued behind
    # the main-worktree mutex before its first code-touching step. Only ever
    # True for an active ("active" source) flow; history-only / archived flows
    # are never waiting.
    waiting_for_lock: bool = False
    # Authoritative "can this flow be resumed" signal, mirroring the daemon
    # aggregator's ``FlowSnapshot.resumable``. True for a non-completed active
    # flow and for every per-flow resumable snapshot (source ``"resumable"``)
    # that survived its engine.json being overwritten by a later run. The server
    # / frontend read this as the primary resume-eligibility signal so a
    # superseded paused/interrupted flow keeps its resume entry instead of
    # degrading to a non-resumable history-only row.
    resumable: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly dict form of this metadata."""
        return {
            "flow_id": self.flow_id,
            "project_root": self.project_root,
            "task_description": self.task_description,
            "task_type": self.task_type,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active": self.active,
            "source": self.source,
            "step_count": self.step_count,
            "waiting_for_lock": self.waiting_for_lock,
            "resumable": self.resumable,
        }


#: Meta fields whose change *alone* (everything else identical) is treated as
#: non-substantive "liveness churn" rather than a real state change. An active
#: flow appends a jsonl line every step, which bumps ``updated_at`` continuously
#: without altering any operator-visible field. The daemon rate-limits an index
#: delta whose only difference is one of these fields to the status-heartbeat
#: cadence (see ``DaemonClient._compute_index_delta``), so an active flow stops
#: re-pushing its meta row every few seconds purely for a timestamp tick, while a
#: substantive change (status / step_count / resumable / …) is still delivered at
#: once. Kept here — next to :class:`SessionMeta` — so the notion of "which meta
#: fields are timestamp noise" lives with the meta schema, not in the client.
THROTTLED_META_FIELDS: FrozenSet[str] = frozenset({"updated_at"})


def meta_change_is_throttleable(
    new_meta: Dict[str, Any], old_meta: Dict[str, Any]
) -> bool:
    """Return whether *new_meta* differs from *old_meta* only in throttled fields.

    Both arguments are :meth:`SessionMeta.to_dict` outputs. Returns ``True`` when
    every key outside :data:`THROTTLED_META_FIELDS` is identical *and* at least
    one throttled field changed — i.e. the update is pure liveness churn safe to
    rate-limit to the heartbeat. Any substantive difference (status, step_count,
    …) returns ``False`` so the update is delivered immediately. Two identical
    metas return ``False`` (nothing to throttle — there is no delta at all).
    """
    changed_throttled = False
    for key in set(new_meta) | set(old_meta):
        if new_meta.get(key) == old_meta.get(key):
            continue
        if key in THROTTLED_META_FIELDS:
            changed_throttled = True
        else:
            return False
    return changed_throttled


@dataclass
class FlowRead:
    """The result of one :func:`DaemonHistoryReader.read_flow` call.

    Attributes:
        flow_id: The flow these records belong to.
        mode: :data:`~se3.daemon.protocol.HISTORY_MODE_FULL` for the initial
            batch (the requester had no cursor) or
            :data:`~se3.daemon.protocol.HISTORY_MODE_APPEND` for a delta.
        records: A list of ``{"step_id": str, "step_type": str, "ordinal": int,
            "message": dict}`` records, one per conversation line, ordered by
            step file then line. ``step_type`` is the authoritative type parsed
            from the jsonl file-name stem (see
            :func:`parse_step_type_from_step_id`); it is injected at the envelope
            level so the frontend never has to guess it, while ``message`` keeps
            its original bytes untouched. ``ordinal`` is the record's 0-based
            physical line position within its step file — a stable identity fixed
            at write time (the jsonl is append-only), IDENTICAL across a ``full``
            snapshot and any later ``append`` delta for the same logical line, and
            preserved across a retry's in-place rewrite (the line at ordinal N now
            carries the rewritten content). Paired with ``step_id`` it forms the
            ``step_id#ordinal`` key the frontend uses to reconcile records
            idempotently — so a marker record with empty ``content`` is no longer
            mistaken for a duplicate of another marker.
        cursor: The updated per-step line cursor to send back on the next
            request to continue incrementally.
    """

    flow_id: str
    mode: str
    records: List[Dict[str, Any]] = field(default_factory=list)
    cursor: Dict[str, int] = field(default_factory=dict)


class DaemonHistoryReader:
    """Builds a history index and serves incremental per-flow conversation reads."""

    def __init__(self, project_roots_provider: ProjectRootsProvider) -> None:
        """Create a reader.

        Args:
            project_roots_provider: Zero-arg callable returning the project
                roots whose ``se3/history`` should be enumerated. Wiring this
                to the aggregator's ``project_roots`` keeps the history view in
                lock-step with the rest of the daemon's tracked roots.
        """
        self._provider = project_roots_provider
        # TTL cache for :meth:`build_index`.  The daemon client's push loop
        # calls ``build_index`` every fast tick (1 s); on a large history tree
        # the full directory walk + JSON parse is expensive enough to saturate
        # thread-pool workers.  Caching for a conservative window collapses
        # repeated identical rebuilds into one.
        self._index_cache: Optional[List[SessionMeta]] = None
        self._index_cache_at: float = 0.0

        # Per-directory content-signature cache for history-only flows.
        # When ``_build_index_fresh`` rebuilds the index, unchanged directories
        # (same set of files, same mtimes/sizes) reuse their cached
        # :class:`SessionMeta` without re-reading ``_meta.json`` or
        # re-extracting the title from the first jsonl line.  The cache is
        # keyed by the stringified directory path; each entry maps a
        # content-signature tuple to the :class:`SessionMeta` produced from
        # that content.  ``invalidate_index_cache`` does *not* clear this
        # cache — it self-invalidates via the directory signature.
        self._history_meta_cache: Dict[str, Tuple[tuple, SessionMeta]] = {}

        # Per-file byte-offset table for incremental reads in :meth:`read_flow`.
        # Key: absolute path of the jsonl file (str).
        # Value: ``(consumed_lines, byte_offset, mtime, size, head_sig)`` where
        # *consumed_lines* is the count of fully consumed newline-terminated
        # lines, *byte_offset* is the file position after the last consumed
        # newline, *mtime*/*size* are the file stat at the time of the last
        # read, *head_sig* is the bounded HEAD fingerprint (the first
        # ``min(byte_offset, HEAD_SIGNATURE_BYTES)`` bytes) and *boundary_sig* is
        # the bounded BOUNDARY fingerprint (the last
        # ``min(byte_offset, HEAD_SIGNATURE_BYTES)`` bytes, ending at
        # *byte_offset*) — together the two constant-cost grow-case rewrite guards
        # (see :meth:`_head_signature` / :meth:`_boundary_signature`).
        #
        # When the caller's cursor line-count matches *consumed_lines*, the
        # current file size is >= *byte_offset*, AND the rewrite check passes
        # (the file was appended-to, not rewritten), only the new bytes past the
        # offset are read (seek + incremental parse).  Otherwise (first read,
        # cursor rollback, file shrunk, or file truncated/replaced in place even
        # at an equal-or-larger size) a full read from the start is performed and
        # the entry is rebuilt.
        self._read_offsets: Dict[
            str, Tuple[int, int, float, int, Optional[bytes], Optional[bytes]]
        ] = {}

        # Per-file running full-prefix hasher, parallel to ``_read_offsets`` and
        # keyed by the same absolute path.  Each entry is a live ``blake2b`` whose
        # digest covers exactly the consumed region ``[0, byte_offset)`` of the
        # file, extended *incrementally from the bytes already read* on every
        # read (never by re-reading the prefix from disk).  Used only in the
        # equal-size, no-new-bytes case to detect an in-place rewrite that
        # preserves the bounded head window but changes a later record (issue
        # #209, fix iteration 6): there the stored digest is compared against a
        # fresh whole-prefix disk hash.  Kept out of ``_read_offsets`` because a
        # hasher object is not part of the wire/cursor contract — it is purely
        # reader-internal optimization state and cold-starts after a restart.
        self._prefix_hashers: Dict[str, "hashlib._Hash"] = {}

    def invalidate_index_cache(self) -> None:
        """Drop the cached index, forcing the next ``build_index`` to rebuild.

        Called when a new flow is spawned or a flow status changes, so the
        index reflects the new state promptly rather than waiting out the TTL.
        """
        self._index_cache = None
        self._index_cache_at = 0.0

    # -- project roots -----------------------------------------------------

    def _iter_roots(self) -> List[Path]:
        """Return the deduplicated, resolved project roots to enumerate."""
        try:
            raw = self._provider() or []
        except Exception:  # pragma: no cover - defensive
            logger.exception("history: project-roots provider failed")
            return []
        roots: List[Path] = []
        seen: set = set()
        for entry in raw:
            try:
                root = Path(entry).resolve()
            except Exception:  # pragma: no cover - defensive
                continue
            if root not in seen:
                seen.add(root)
                roots.append(root)
        return roots

    # -- index -------------------------------------------------------------

    def build_index(self) -> List[SessionMeta]:
        """Return :class:`SessionMeta` for every history session, all roots.

        Flows are deduplicated by ``flow_id`` (the active ``engine.json`` flow
        wins over an archive copy, which wins over a history-only directory)
        and sorted by ``updated_at`` descending.

        Results are cached for :data:`BUILD_INDEX_TTL` seconds so the daemon
        client's per-tick ``_push_history`` call does not trigger a full
        directory walk + JSON parse on every fast tick.

        Callers that need *live* active-status (e.g. :meth:`read_active_flows`)
        must re-check the on-disk ``engine.json`` status because a cached index
        may carry stale ``active`` flags for up to the TTL window.
        """
        now = time.monotonic()
        cached = self._index_cache
        if cached is not None and (now - self._index_cache_at) < BUILD_INDEX_TTL:
            return cached
        metas = self._build_index_fresh()
        self._index_cache = metas
        self._index_cache_at = now
        return metas

    def _build_index_fresh(self) -> List[SessionMeta]:
        """Uncached index build — walks all roots from disk.

        ``seen`` maps ``flow_id`` to its position in *metas* (rather than being a
        bare presence set) so a later, higher-precedence source for an
        already-claimed flow can *replace* the earlier entry — see
        :data:`_SOURCE_PRIORITY` and :meth:`_claim`. This is what lets an active
        worktree flow (its live ``engine.json`` under the worktree subdir,
        enumerated AFTER the main repo) supersede the main repo's history-only
        clone of the same flow_id.
        """
        metas: List[SessionMeta] = []
        seen: Dict[str, int] = {}
        for root in self._iter_roots():
            try:
                self._index_root(root, metas, seen)
            except Exception:  # pragma: no cover - defensive
                logger.exception("history: failed to index root %s", root)
        metas.sort(key=lambda m: m.updated_at or "", reverse=True)
        return metas

    def _claim(
        self,
        flow_id: str,
        source: str,
        metas: List[SessionMeta],
        seen: Dict[str, int],
        factory: Callable[[], SessionMeta],
    ) -> None:
        """Record (or upgrade) a flow's :class:`SessionMeta` by source precedence.

        On the first sighting of *flow_id* the *factory* result is appended. On a
        later sighting the new *source* replaces the recorded entry ONLY when it
        has strictly higher precedence (a smaller :data:`_SOURCE_PRIORITY`), so a
        cross-root ``active`` claim supersedes an earlier ``history`` claim while
        an equal/lower-precedence repeat is ignored. The *factory* is invoked
        lazily — never for an ignored repeat — so an unchanged flow incurs no
        extra disk read (preserving the prior first-claim-wins cost profile).
        """
        new_pri = _SOURCE_PRIORITY.get(source, 99)
        pos = seen.get(flow_id)
        if pos is None:
            seen[flow_id] = len(metas)
            metas.append(factory())
            return
        existing_pri = _SOURCE_PRIORITY.get(metas[pos].source, 99)
        if new_pri < existing_pri:
            metas[pos] = factory()

    @staticmethod
    def _is_still_active(meta: SessionMeta) -> bool:
        """Re-check whether *meta*'s flow is still active on disk.

        The :meth:`build_index` cache may carry stale ``active`` flags for up
        to :data:`BUILD_INDEX_TTL` seconds.  This lightweight re-check reads
        only the single ``engine.json`` for the flow's project root (a
        ``stat`` + small JSON parse), which is dramatically cheaper than the
        full index rebuild it replaces.
        """
        if not meta.active:
            return False
        if meta.source != "active":
            return False
        engine_json = Path(meta.project_root) / "se3" / "state" / "engine.json"
        # active=True read: the live-engine cache now hashes the WHOLE content, so
        # a same-(mtime,size) middle rewrite (the #260 dense discovery→analyze
        # window) forces a re-parse rather than serving a stale flow_id/status —
        # the disk_json_cache blind spot the diagnosis pinned is closed at source.
        # The true-value fallback below is defence-in-depth on TOP of that: only
        # ever reached on the drop path, so it re-confirms against disk (bypassing
        # the cache) before a still-active flow is excluded, and never adds a parse
        # on the healthy keep path.
        data = read_engine_header(engine_json, active=True)
        active = DaemonHistoryReader._header_keeps_flow_active(meta, data)
        if active is not None:
            return active
        # Cache-based read said DROP (unreadable / flow_id mismatch / terminal).
        # Because dropping an active flow silently freezes its live WebUI stream
        # for the rest of the step (the #260 symptom), re-confirm with a forced
        # fresh read before acting — a same-(mtime,size) collision or a write
        # racing our read must not transiently exclude a flow that is in fact live.
        fresh = read_engine_header(engine_json, active=True, force_fresh=True)
        confirmed = DaemonHistoryReader._header_keeps_flow_active(meta, fresh)
        if confirmed is None:
            # Forced fresh read still says DROP — the drop is genuine.
            logger.debug(
                "hist-diag _is_still_active: flow=%s DROP confirmed by forced "
                "fresh read (root=%s)",
                meta.flow_id, meta.project_root,
            )
            return False
        if confirmed and confirmed is not active:
            logger.debug(
                "hist-diag _is_still_active: flow=%s RESCUED — cached read would "
                "have dropped it, forced fresh read confirms it is still active",
                meta.flow_id,
            )
        return confirmed

    @staticmethod
    def _header_keeps_flow_active(
        meta: SessionMeta, data: Any
    ) -> Optional[bool]:
        """Decide active-liveness from an engine.json header read.

        Returns ``True`` when *data* confirms *meta*'s flow is still the live,
        non-terminal engine.json flow, ``False`` when it is present-but-terminal,
        and ``None`` for an *inconclusive* drop (unreadable / degraded-miss /
        flow_id mismatch) that the caller should re-confirm with a forced fresh
        read before trusting. Distinguishing a definite-terminal ``False`` from an
        inconclusive ``None`` is what lets the caller pay the extra fresh read only
        on the ambiguous drop, never on a clean terminal transition.
        """
        if not isinstance(data, dict):
            # Missing / oversized-degraded-miss / caught mid-write: inconclusive.
            return None
        # Confirm the engine.json still describes *this* flow.  Without this
        # check, if flow F1 completed and a different flow F2 is now the active
        # engine.json flow (status RUNNING), this would read F2's RUNNING status
        # and incorrectly keep the stale F1 meta active while missing F2. A
        # mismatch is inconclusive (a same-(mtime,size) collision could show a
        # stale id), so the caller re-confirms fresh.
        if str(data.get("flow_id") or "") != meta.flow_id:
            return None
        status = str(data.get("status") or "")
        return _is_active_status(status)

    def _index_root(
        self, root: Path, metas: List[SessionMeta], seen: Dict[str, int]
    ) -> None:
        """Append the sessions found under one project *root* into *metas*.

        Each source claims its flow_ids through :meth:`_claim`, so a
        higher-precedence source — whether processed later within this root or
        discovered under a *different* root in a later :meth:`_index_root` call —
        supersedes a lower-precedence claim of the same flow_id (see
        :data:`_SOURCE_PRIORITY`).
        """
        state_dir = root / "se3" / "state"

        # 1. Active flow from engine.json (active=True). A matching
        # (path, mtime, size) is trusted without a re-read; a real per-step
        # rewrite moves the key and re-parses. The expensive, immutable archive /
        # resumable / meta reads below are stat-cached (no re-read while
        # unchanged).
        data = read_engine_header(state_dir / "engine.json", active=True)
        if isinstance(data, dict) and data.get("flow_id"):
            flow_id = str(data["flow_id"])
            self._claim(
                flow_id,
                "active",
                metas,
                seen,
                lambda: self._meta_from_engine(root, data, source="active"),
            )

        # 2. Archived flows.
        archive_dir = state_dir / "archive"
        if archive_dir.is_dir():
            for archive_file in sorted(archive_dir.glob("engine_*.json")):
                # An archived flow's engine.json never changes again, so the
                # stat-keyed cache parses it once; a giant legacy archive is
                # degraded to its hot keys rather than fully parsed.
                adata = read_engine_header(archive_file)
                if not isinstance(adata, dict):
                    continue
                flow_id = str(adata.get("flow_id") or "")
                if not flow_id:
                    continue
                self._claim(
                    flow_id,
                    "archived",
                    metas,
                    seen,
                    lambda adata=adata, archive_file=archive_file: self._meta_from_engine(
                        root,
                        adata,
                        source="archived",
                        fallback_mtime=_safe_mtime(archive_file),
                    ),
                )

        # 3. Resumable per-flow snapshots (se3/state/resumable/<flow_id>.json).
        # A paused/interrupted/failed flow writes a snapshot here; a later
        # ``se3 run`` overwrites the single-slot engine.json but leaves the
        # snapshot intact, so the flow survives only here (plus possibly a
        # history dir). ``_SOURCE_PRIORITY`` keeps it above the history-only
        # source so such a flow keeps its original status + ``resumable=True``
        # rather than degrading to a non-resumable ``source="history"`` row, and
        # below active/archived (which win).
        resumable_dir = state_dir / "resumable"
        if resumable_dir.is_dir():
            for snap_file in sorted(resumable_dir.glob("*.json")):
                # A resumable snapshot is engine-shaped; the stat-keyed cache
                # parses it once, and a bloated in-flight snapshot is degraded to
                # its hot keys (flow_id / status suffice for the claim below).
                sdata = read_engine_header(snap_file)
                if not isinstance(sdata, dict):
                    continue
                flow_id = str(sdata.get("flow_id") or "")
                if not flow_id:
                    continue
                # The embedded flow_id MUST match the snapshot filename
                # (resumable/<flow_id>.json); the load/resume path is keyed by
                # filename, so a mismatched/misnamed payload would advertise a
                # resume entry that can never actually resume. Skip it without
                # claiming the flow_id so its real history-only row can surface.
                if flow_id != snap_file.stem:
                    continue
                # A stale ``completed`` snapshot (e.g. clear_resumable_snapshot
                # failed, or an operator/test artifact) must not be advertised
                # as resumable — the daemon resume validator rejects a COMPLETED
                # flow. Skip it without claiming the flow_id, so the flow can
                # still degrade to a normal history-only row below.
                if not _is_resumable_status(str(sdata.get("status") or "")):
                    continue
                self._claim(
                    flow_id,
                    "resumable",
                    metas,
                    seen,
                    lambda sdata=sdata, snap_file=snap_file: self._meta_from_engine(
                        root,
                        sdata,
                        source="resumable",
                        fallback_mtime=_safe_mtime(snap_file),
                        resumable=True,
                    ),
                )

        # 4. History-only flows (may have no engine.json at all).
        history_root = root / "se3" / "history"
        if history_root.is_dir():
            for flow_dir in sorted(history_root.iterdir()):
                if not flow_dir.is_dir():
                    continue
                flow_id = flow_dir.name
                self._claim(
                    flow_id,
                    "history",
                    metas,
                    seen,
                    lambda flow_dir=flow_dir: self._meta_from_history(root, flow_dir),
                )

    def _meta_from_engine(
        self,
        root: Path,
        data: Dict[str, Any],
        *,
        source: str,
        fallback_mtime: Optional[float] = None,
        resumable: Optional[bool] = None,
    ) -> SessionMeta:
        """Build a :class:`SessionMeta` from an ``engine.json``-shaped dict.

        *resumable* overrides the computed resume-eligibility flag — used by the
        ``"resumable"`` source to force ``True`` for a per-flow snapshot whose
        stored status (often ``running`` for an interrupted flow) would not by
        itself imply resumability. When left ``None`` it is derived from the
        status: a non-completed *active* flow is resumable, while archived /
        history-only snapshots default to non-resumable.
        """
        status = str(data.get("status") or "unknown")
        active = source == "active" and _is_active_status(status)
        updated = str(data.get("updated_at") or "")
        if not updated and fallback_mtime:
            updated = datetime.fromtimestamp(fallback_mtime).isoformat()
        flow_id = str(data.get("flow_id"))
        if resumable is None:
            resumable = source == "active" and _is_resumable_status(status)
        return SessionMeta(
            flow_id=flow_id,
            project_root=str(root),
            task_description=_clip(str(data.get("task_description") or "")),
            task_type=str(data.get("task_type") or ""),
            status=status,
            created_at=str(data.get("created_at") or ""),
            updated_at=updated,
            active=active,
            source=source,
            step_count=_count_jsonl(root / "se3" / "history" / flow_id),
            # "waiting for lock" is a live sub-state of a still-running flow that
            # is queued behind the merge lock (including a --worktree flow that is
            # running its own merge_integrate / version_reconcile steps under the
            # lock). Read only from the live engine.json (source=='active') and
            # require the flow be active, so a stale True never shows on an
            # archived/terminal snapshot.
            waiting_for_lock=bool(
                source == "active"
                and active
                and data.get("waiting_for_lock", False)
            ),
            resumable=bool(resumable),
        )

    @staticmethod
    def _dir_signature(flow_dir: Path) -> Tuple[tuple, float]:
        """Compute a content-signature tuple for a history directory.

        The signature captures every factor that affects the
        :class:`SessionMeta` output: the set of files (by name), each file's
        mtime and size (so content changes and appends are detected), and the
        latest mtime (which drives ``updated_at``).  The returned tuple is
        hashable and comparable; the float is the latest mtime (0.0 when the
        directory is empty or unreadable).

        This is separated from :meth:`_meta_from_history` so it can be tested
        independently and so the iterdir traversal is done exactly once per
        call.
        """
        file_entries: List[Tuple[str, float, int]] = []
        latest = 0.0
        try:
            for f in flow_dir.iterdir():
                if not f.is_file():
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                mtime = st.st_mtime
                size = st.st_size
                file_entries.append((f.name, mtime, size))
                if mtime > latest:
                    latest = mtime
        except OSError:
            pass
        file_entries.sort()
        return tuple(file_entries), latest

    def _meta_from_history(self, root: Path, flow_dir: Path) -> SessionMeta:
        """Build a best-effort :class:`SessionMeta` for a history-only flow.

        A history-only flow has no surviving ``engine.json``; metadata is
        recovered from an optional ``_meta.json`` plus the ``jsonl`` files
        themselves, so the session still appears in the index.

        Results are cached per directory keyed by a content-signature tuple
        (file names, mtimes, sizes).  When the directory content has not
        changed since the last call, the cached :class:`SessionMeta` is
        returned without re-reading ``_meta.json`` or re-extracting the
        summary title — this eliminates the repeated ~115 MB of reads that
        previously occurred every ~1 second across ~161 unchanged history
        directories.
        """
        flow_id = flow_dir.name
        sig_key = str(flow_dir)
        sig, latest = self._dir_signature(flow_dir)
        cached = self._history_meta_cache.get(sig_key)
        if cached is not None and cached[0] == sig:
            return cached[1]
        meta = read_json_cached(flow_dir / "_meta.json") or {}
        updated = datetime.fromtimestamp(latest).isoformat() if latest else ""
        result = SessionMeta(
            flow_id=flow_id,
            project_root=str(root),
            task_description=_clip(_extract_history_summary(flow_dir)),
            task_type=str(meta.get("type") or ""),
            status="history",
            created_at=str(meta.get("created_at") or ""),
            updated_at=updated,
            active=False,
            source="history",
            step_count=_count_jsonl(flow_dir),
        )
        self._history_meta_cache[sig_key] = (sig, result)
        return result

    # -- per-flow reads ----------------------------------------------------

    def _resolve_flow_dir(
        self, flow_id: str, project_root: Optional[str]
    ) -> Optional[Path]:
        """Locate the ``se3/history/<flow_id>`` directory for *flow_id*."""
        if project_root:
            try:
                roots = [Path(project_root).resolve()]
            except Exception:  # pragma: no cover - defensive
                roots = []
        else:
            roots = self._iter_roots()
        for root in roots:
            candidate = root / "se3" / "history" / flow_id
            if candidate.is_dir():
                return candidate
        return None

    def _resolve_flow_dirs(
        self, flow_id: str, project_root: Optional[str]
    ) -> List[Path]:
        """Locate *every* ``se3/history/<flow_id>`` directory for *flow_id*.

        Unlike :meth:`_resolve_flow_dir` (which returns a single first-match),
        this returns an ordered, de-duplicated list of candidate directories so
        the reader can merge a flow whose history is split across two roots — the
        classic ``se3 run --worktree`` case where the discovery step ran in the
        main repo before the fork (writing ``<main>/se3/history/<flow_id>/``) and
        every later step ran in the worktree (writing
        ``<worktree>/se3/history/<flow_id>/``, which usually also carries a clone
        of the discovery file).

        With an authoritative *project_root* — the flow's recorded
        ``SessionMeta.project_root``, the single source of truth threaded down by
        the server — the candidate order is:

          1. the authoritative root's directory (highest priority);
          2. the owning main-repo root (``project_root`` itself when it is the
             main repo, else the one reverse-resolved from a worktree copy by
             :func:`resolve_worktree_main_root`), so a worktree flow still picks
             up the pre-fork discovery written to the main repo;
          3. **every** ``<main>/se3/worktrees/<name>`` isolation copy under that
             main repo that carries this flow's history — the *forward* (main →
             worktree) expansion. This is what makes the read complete even when
             the authoritative root recorded by the index is the **main** repo
             (e.g. the pre-fork discovery dir is enumerated first and claims the
             flow_id): without it, ``read_flow(project_root=<main>)`` would
             resolve to ``[main dir]`` only and again show discovery alone.

        So the merge reaches BOTH the main repo and the worktree regardless of
        which of the two roots the index recorded as the authoritative
        ``project_root``.

        Each candidate is admitted only when its directory exists (``is_dir``);
        a missing root is silently skipped, so a non-worktree main-repo
        *project_root* with no worktree copies simply yields ``[its own dir]``.

        When *project_root* is empty the method degrades to the legacy
        registry-walk heuristic (:meth:`_resolve_flow_dir`), returning the single
        first-matching directory (or an empty list) for backward compatibility.
        """
        if not project_root:
            legacy = self._resolve_flow_dir(flow_id, None)
            return [legacy] if legacy is not None else []

        ordered: List[Path] = []
        seen: Set[Path] = set()

        def _add(root: Optional[Path]) -> None:
            if root is None:
                return
            candidate = root / "se3" / "history" / flow_id
            try:
                if not candidate.is_dir():
                    return
                resolved = candidate.resolve()
            except OSError:  # pragma: no cover - defensive
                return
            if resolved in seen:
                return
            seen.add(resolved)
            ordered.append(resolved)

        # 1. Authoritative root (single source of truth, highest priority).
        try:
            auth = Path(project_root).resolve()
        except Exception:  # pragma: no cover - defensive
            auth = None
        _add(auth)

        # 2. Owning main-repo root. When *project_root* is a worktree copy,
        #    reverse-resolve it; otherwise *project_root* IS the main repo.
        try:
            resolved_main = resolve_worktree_main_root(project_root)
        except Exception:  # pragma: no cover - defensive
            resolved_main = None
        main_root: Optional[Path] = None
        if resolved_main:
            try:
                main_root = Path(resolved_main).resolve()
            except Exception:  # pragma: no cover - defensive
                main_root = None
        elif auth is not None:
            main_root = auth
        _add(main_root)

        # 3. Forward expansion: every worktree isolation copy under the main repo
        #    that carries this flow's history (main → worktree direction). Keeps
        #    the merge complete even when the index recorded the *main* root.
        if main_root is not None:
            worktrees_dir = main_root / "se3" / "worktrees"
            try:
                entries = (
                    sorted(worktrees_dir.iterdir())
                    if worktrees_dir.is_dir()
                    else []
                )
            except OSError:  # pragma: no cover - defensive
                entries = []
            for wt in entries:
                try:
                    if wt.is_dir():
                        _add(wt)
                except OSError:  # pragma: no cover - racy unlink
                    continue

        return ordered

    @staticmethod
    def _merge_flow_jsonl(flow_dirs: List[Path]) -> List[Path]:
        """Merge the per-step history files across *flow_dirs* into one ordered,
        de-duplicated physical-file list.

        Several candidate ``se3/history/<flow_id>`` directories may hold the
        *same* logical step — most commonly a worktree flow whose pre-fork
        discovery file was written to the main repo and then cloned into the
        worktree, so the discovery step exists under both roots. The clone may
        carry the *same* physical name (``01_discovery_ab.jsonl`` under both) or
        a name differing only by the per-step ``<hash>`` segment
        (``01_discovery_ab.jsonl`` vs ``01_discovery_cd.jsonl``). De-duplication
        is therefore keyed by the *logical* step identity
        (:func:`_cross_root_step_key`) — the ``NN`` sequence + step type + group
        + sidecar marker, hash stripped — NOT the raw filename, so a discovery
        clone collapses to one entry regardless of whether its hash matches.

        The cursor is keyed by physical filename, so each logical step MUST
        resolve to exactly ONE physical file or two copies of the same step
        would render twice. The chosen copy is the most complete one (largest
        byte size), with ties broken by directory priority (the authoritative
        root comes first in *flow_dirs*); this keeps the result stable (a given
        step file is only ever *written* by one root, so the other root's copy
        is a static pre-fork clone) and loses no records.

        A logical step unique to a single root is always included, so a split
        history — discovery only in the main repo, later steps only in the
        worktree — is merged in full with no missing step and no duplicate
        discovery.  A step's primary ``*.jsonl`` and its ``*.jsonl.from-<branch>``
        sidecar carry distinct cross-root keys (the sidecar marker), so both are
        kept and merged into one step stream by :meth:`read_flow`.

        The result is sorted by filename so a step's primary file sorts ahead of
        its sidecars and steps stay in ``NN_`` order, matching
        :func:`_iter_history_jsonl`'s single-root ordering.
        """
        # logical-step-key -> (priority, size, path); priority = index in
        # flow_dirs (lower index = higher priority = authoritative root first).
        chosen: Dict[str, Tuple[int, int, Path]] = {}
        for priority, flow_dir in enumerate(flow_dirs):
            for jsonl in _iter_history_jsonl(flow_dir):
                try:
                    size = jsonl.stat().st_size
                except OSError:  # pragma: no cover - defensive
                    continue
                key = _cross_root_step_key(jsonl.name)
                prev = chosen.get(key)
                if prev is None:
                    chosen[key] = (priority, size, jsonl)
                    continue
                prev_priority, prev_size, _prev_path = prev
                # More complete (larger) wins; on an exact tie the
                # higher-priority (authoritative) root's copy wins.
                if size > prev_size or (
                    size == prev_size and priority < prev_priority
                ):
                    chosen[key] = (priority, size, jsonl)
        return [entry[2] for entry in sorted(chosen.values(), key=lambda e: e[2].name)]

    @staticmethod
    def _head_signature(path: Path, offset: int) -> Optional[bytes]:
        """Bounded fingerprint of the file *head* — the first bytes of the prefix.

        Hashes the first ``min(offset, HEAD_SIGNATURE_BYTES)`` bytes (anchored at
        byte 0), a single small constant-cost read regardless of file size.  Used
        as the cheap grow-case rewrite guard: a genuine append never touches the
        head, while a typical in-place retry re-runs the step from the start and
        rewrites its leading records, so the head changes.  This guard is the half
        that keeps the incremental-append read bounded; the equal-size,
        head-preserving rewrite the head window cannot see is covered separately by
        the running full-prefix hash (see :meth:`_consumed_signature` and the class
        ``_prefix_hashers``).  Returns ``b""`` for an empty prefix and ``None``
        when the file cannot be read (caller then falls back to a safe full read).
        """
        window = min(offset, HEAD_SIGNATURE_BYTES)
        if window <= 0:
            return b""
        try:
            with open(path, "rb") as fh:
                data = fh.read(window)
        except OSError:
            return None
        return hashlib.blake2b(data, digest_size=16).digest()

    @staticmethod
    def _boundary_signature(path: Path, offset: int) -> Optional[bytes]:
        """Bounded fingerprint of the consumed *boundary* — the last bytes ending
        at *offset*.

        Hashes the ``min(offset, HEAD_SIGNATURE_BYTES)`` bytes immediately before
        *offset* (the window ``[offset - W, offset)``), a single small
        constant-cost read regardless of file size.  Paired with the head window
        as the second cheap grow-case rewrite guard: a genuine append never
        touches any byte before the consumed offset, so the bytes ending at the
        offset are stable; an in-place retry that re-runs the step from the start
        rewrites the records that fall before the old offset, so the boundary
        window changes even when the leading head window happens to be preserved
        (issue #209, fix iteration 7, larger prefix-preserving retry rewrite).
        Together the head window ``[0, W)`` and this boundary window cover the
        whole consumed prefix for any region up to ``2·W`` bytes, so a small
        consumed prefix is fully verified.  Returns ``b""`` for an empty prefix
        and ``None`` when the file cannot be read (caller then falls back to a safe
        full read).
        """
        window = min(offset, HEAD_SIGNATURE_BYTES)
        if window <= 0:
            return b""
        try:
            with open(path, "rb") as fh:
                fh.seek(offset - window)
                data = fh.read(window)
        except OSError:
            return None
        return hashlib.blake2b(data, digest_size=16).digest()

    @staticmethod
    def _consumed_signature(path: Path, offset: int) -> Optional[bytes]:
        """Fingerprint the **whole consumed region** ``[0, offset)`` of the file.

        Streams the first *offset* bytes of the file (anchored at byte 0) through
        a fast C digest in :data:`SIGNATURE_CHUNK_BYTES` chunks, so a
        multi-megabyte prefix is hashed without loading it all into memory and
        without any Python-level UTF-8 decode / splitlines / record
        construction.  Because a genuine append never mutates any byte before the
        consumed offset — and this fingerprint covers exactly that
        already-consumed region — the signature is stable across appends.  It
        changes whenever the file is truncated / rewritten in place: a step
        retried in place re-runs from the start and rewrites its records, so at
        least one byte in the prefix differs — **regardless of which part of the
        prefix the rewrite happened to preserve** (the leading prompt/status
        head, the trailing terminal/status record, or any sampled window).  A
        bounded sampled window (head- or boundary-anchored) cannot guarantee this:
        an equal-size replacement that keeps the sampled bytes identical but
        mutates content elsewhere in the consumed prefix kept the windowed
        fingerprint unchanged and was misclassified as a safe append, so the
        whole replacement batch was silently skipped until a full reload (issue
        #209, equal-size / prefix-preserving retry rewrite).  Hashing the whole
        prefix is the reliable rewrite signal :meth:`read_flow` uses to
        invalidate a stale offset and deliver the replacement from the start.
        Reads only up to *offset* bytes even if the file has since grown, so the
        cost tracks the consumed region, not the (possibly much larger) appended
        tail.  Returns ``b""`` for an empty prefix (compares equal to another
        empty prefix) and ``None`` when the file cannot be read (so the caller
        falls back to a safe full read).
        """
        if offset <= 0:
            return b""
        digest = hashlib.blake2b(digest_size=16)
        remaining = offset
        try:
            with open(path, "rb") as fh:
                while remaining > 0:
                    chunk = fh.read(min(remaining, SIGNATURE_CHUNK_BYTES))
                    if not chunk:
                        # File is now shorter than the consumed offset — it was
                        # truncated / replaced.  The hash covers fewer bytes than
                        # the stored signature did, so it cannot compare equal,
                        # forcing a safe full re-read.
                        break
                    digest.update(chunk)
                    remaining -= len(chunk)
        except OSError:
            return None
        return digest.digest()

    def _record_offset(
        self,
        jsonl_key: str,
        jsonl: Path,
        consumed: int,
        offset: int,
        mtime: float,
        size: int,
        base_hasher: Optional["hashlib._Hash"],
        new_consumed_bytes: bytes,
    ) -> None:
        """Persist the per-file read position plus all rewrite fingerprints.

        Updates :attr:`_read_offsets` with the bounded head and boundary
        signatures (the two cheap grow-case guards) and advances the running
        full-prefix hash in :attr:`_prefix_hashers` by *new_consumed_bytes* — the
        raw bytes consumed in this read, taken from the bytes already in memory so
        the running hash is maintained WITHOUT any extra disk read.  *base_hasher*
        is the prior running hasher to extend (for an incremental read) or ``None``
        to start a fresh hash over a from-scratch full read.
        """
        self._read_offsets[jsonl_key] = (
            consumed,
            offset,
            mtime,
            size,
            self._head_signature(jsonl, offset),
            self._boundary_signature(jsonl, offset),
        )
        hasher = base_hasher.copy() if base_hasher is not None else hashlib.blake2b(
            digest_size=16
        )
        if new_consumed_bytes:
            hasher.update(new_consumed_bytes)
        self._prefix_hashers[jsonl_key] = hasher

    def read_flow(
        self,
        flow_id: str,
        *,
        project_root: Optional[str] = None,
        cursor: Optional[Dict[str, int]] = None,
    ) -> FlowRead:
        """Read the conversation records of *flow_id* incrementally.

        Uses an internal byte-offset table (:attr:`_read_offsets`) to avoid
        re-reading already-consumed bytes.  When the caller's cursor line-count
        matches the offset table's consumed-line count and the file has not
        shrunk, only the new bytes past the recorded offset are read (``seek``
        + incremental parse).  Otherwise (first read, cursor rollback, file
        truncation/replacement) a full read from the start is performed.

        Only complete (newline-terminated) lines are consumed; a partial line
        at the end of the file (no trailing ``\\n``) is left for the next round.

        Args:
            flow_id: The flow whose ``se3/history/<flow_id>`` is read.
            project_root: The flow's authoritative ``SessionMeta.project_root``.
                Used as the single source of truth for locating the history: the
                authoritative root plus its owning main repo (and any other
                registered root carrying this flow) are merged, so a worktree
                flow split across two roots reads in full. When omitted, the
                legacy registry-walk first-match heuristic is used.
            cursor: A ``{jsonl-filename: line-count}`` dict. An empty / ``None``
                cursor yields a ``full`` snapshot; a populated one yields an
                ``append`` delta of only the lines past the cursor.

        Returns:
            A :class:`FlowRead`. Its ``mode`` is ``full`` when *cursor* was
            empty, else ``append``. ``records`` is capped at
            :data:`MAX_RECORDS_PER_REPORT`; when capped, ``cursor`` advances
            only to the truncation point so the caller can continue.
        """
        cursor = dict(cursor) if cursor else {}
        mode = HISTORY_MODE_FULL if not cursor else HISTORY_MODE_APPEND

        # Resolve EVERY candidate root for this flow (authoritative root first,
        # then the owning main repo, then any other registered root) and merge
        # their per-step files so a worktree flow whose discovery ran in the main
        # repo before the fork displays in full. ``_merge_flow_jsonl`` de-dups by
        # physical filename (largest/most-complete copy wins) so the by-filename
        # cursor never collides between a root's clone of the same step file.
        flow_dirs = self._resolve_flow_dirs(flow_id, project_root)
        if not flow_dirs:
            return FlowRead(flow_id=flow_id, mode=mode, records=[], cursor=cursor)

        new_cursor: Dict[str, int] = dict(cursor)
        records: List[Dict[str, Any]] = []
        truncated = False

        for jsonl in self._merge_flow_jsonl(flow_dirs):
            if truncated:
                break
            # Merge a step's primary ``*.jsonl`` and its ``*.jsonl.from-<branch>``
            # sidecars under one logical step id so a worktree merge-back's
            # records group into the same step stream as the primary file.
            step_id = _logical_step_id(jsonl.name)
            step_type = parse_step_type_from_step_id(step_id)
            # Cursor / offset table stay keyed by the *physical* file name and
            # absolute path, so each file (primary and each sidecar) advances
            # independently and is never read twice.
            jsonl_key = str(jsonl)

            # --- Determine whether we can do an incremental read -----------
            cursor_lines = int(cursor.get(jsonl.name, 0) or 0)
            if cursor_lines < 0:
                cursor_lines = 0

            try:
                st = jsonl.stat()
                cur_size = st.st_size
                cur_mtime = st.st_mtime
            except OSError:
                continue

            prev = self._read_offsets.get(jsonl_key)

            # Detect an in-place truncation / rewrite that a size/offset
            # comparison alone cannot see.  A genuine append never mutates any
            # byte before the consumed offset, so the recorded offset still points
            # into the *same* content and an incremental seek-read is safe; a
            # rewrite makes the recorded offset/cursor stale and MUST trigger a
            # full read from the start (delivering the replacement content from the
            # beginning as the next append delta).  The discriminator MUST catch a
            # consumed-content change *regardless of which part of the prefix the
            # rewrite happened to preserve*:
            #   1. shrank below the consumed offset → definitely replaced, no read
            #      needed (the whole-prefix hash would also diverge, but the size
            #      shortcut spares even that read).
            #   2. otherwise (grew past, or equal to, the consumed offset) →
            #      re-hash the WHOLE consumed prefix ``[0, offset)`` from disk and
            #      compare it against the running full-prefix hash maintained from
            #      the bytes already read.  A genuine append leaves the entire
            #      consumed prefix byte-for-byte identical, so the two hashes match;
            #      an in-place retry that re-runs the step rewrites at least one
            #      byte of the prefix, so they diverge — whether the changed record
            #      is the leading prompt/status head, the trailing terminal/status
            #      record, or any record in the MIDDLE of a large prefix.
            #
            # Bounded sampled windows (a head window ``[0, W)`` and/or a boundary
            # window ``[offset - W, offset)``) were tried as a cheaper grow-case
            # guard but are NOT sufficient: together they only cover a consumed
            # prefix up to ``2·W`` bytes, so a retry that preserves both the leading
            # head and the boundary tail while changing a record between them — and
            # appends additional records so the file grows — kept both windows equal,
            # left ``rewritten`` false, trusted the stale offset, and delivered only
            # the replacement suffix, dropping the beginning of the retry batch until
            # a full reload (issue #209, fix iteration 8, middle-of-prefix retry
            # rewrite).  Only a hash over the *entire* consumed prefix is a complete
            # rewrite signal, so it is used uniformly for both the grow and the
            # equal-size cases.  The disk read is bounded by the consumed offset
            # (``_consumed_signature`` reads at most ``offset`` bytes even after the
            # file has grown) and streams raw bytes through a fast C digest with no
            # Python-level UTF-8 decode / splitlines / record construction, so it is
            # dramatically cheaper than the whole-file ``read_text().splitlines()``
            # re-read the byte-offset incremental path replaced, and the genuinely
            # new appended tail is still read only once via the seek below.
            #
            # A ``stat``-identical (same size AND mtime) result is deliberately
            # NOT treated as a zero-read "untouched" fast path: under a coarse
            # mtime granularity an equal-size in-place rewrite landing within the
            # same mtime tick is indistinguishable from "untouched" by ``stat``
            # alone, so the recorded offset/cursor would be wrongly trusted and the
            # whole replacement batch silently skipped as "no new bytes" (issue
            # #209, equal-size retry rewrite).  Hence the content check above runs
            # regardless of mtime.
            rewritten = False
            if prev is not None:
                prev_offset = prev[1]
                if cur_size < prev_offset:
                    # File is now smaller than what we already consumed →
                    # definitely truncated / replaced.
                    rewritten = True
                else:
                    # File grew past, or is equal in size to, the consumed offset.
                    # Neither proves an append: an in-place retry can rewrite the
                    # step from the start and end up the same size as, or larger
                    # than, the old consumed offset.  Verify the WHOLE consumed
                    # prefix against the running full-prefix hash; any divergence
                    # means a consumed record changed (head, middle, or boundary)
                    # and the stale offset/cursor must be discarded.
                    cur_consumed_sig = self._consumed_signature(jsonl, prev_offset)
                    prev_hasher = self._prefix_hashers.get(jsonl_key)
                    prev_consumed_sig = (
                        prev_hasher.digest() if prev_hasher is not None else None
                    )
                    if (
                        cur_consumed_sig is None
                        or prev_consumed_sig is None
                        or cur_consumed_sig != prev_consumed_sig
                    ):
                        rewritten = True

            can_incremental = (
                prev is not None
                and cursor_lines == prev[0]       # cursor matches consumed lines
                and cur_size >= prev[1]            # file has not shrunk
                and prev[1] >= 0                   # offset is valid
                and not rewritten                  # rewrite check passed
            )
            # HOP-2 DEBUG: the per-file incremental-read decision. A freshly
            # created boundary file (02_analyze) has prev=None → full read from
            # cursor 0; a stale/desynced cursor (cursor_lines != prev consumed)
            # forces a full re-read. Logged so a live run shows exactly which
            # branch each step file takes across the discovery→analyze boundary.
            logger.debug(
                "hist-diag read_flow file=%s cursor_lines=%s prev_consumed=%s "
                "cur_size=%s rewritten=%s can_incremental=%s",
                jsonl.name, cursor_lines,
                prev[0] if prev is not None else None,
                cur_size, rewritten, can_incremental,
            )

            if can_incremental and cur_size == prev[1]:
                # No new bytes — file is unchanged since last read.
                new_cursor[jsonl.name] = prev[0]
                continue

            # --- Read lines ------------------------------------------------
            if can_incremental:
                # Incremental: seek past already-consumed bytes.
                try:
                    with open(jsonl, "r", encoding="utf-8") as fh:
                        fh.seek(prev[1])
                        new_bytes = fh.read()
                except OSError:
                    continue
                # consumed_lines so far from prior reads.
                consumed = prev[0]
                offset = prev[1]
                # The running full-prefix hash is extended from the bytes we just
                # read (no extra disk read): the bytes consumed THIS round are the
                # leading ``offset - prev_offset_start`` bytes of ``new_bytes``.
                prev_offset_start = prev[1]
                prev_hasher = self._prefix_hashers.get(jsonl_key)
                new_bytes_encoded = new_bytes.encode("utf-8")
                raw_lines = new_bytes.split("\n")
                # The last element may be a partial line (no trailing \n).
                # Only process elements that end with a newline — i.e. all
                # but the last element when it is non-empty (partial).
                if raw_lines and raw_lines[-1] != "":
                    # Last element is a partial line — don't consume it.
                    complete_lines = raw_lines[:-1]
                    # Adjust the raw_lines buffer so the offset calculation
                    # below doesn't count the partial tail.
                else:
                    # Last element is "" (file ends with \n) — all lines
                    # are complete.  The trailing "" is an artifact of
                    # split("\n") and doesn't correspond to a real line.
                    complete_lines = raw_lines[:-1] if raw_lines else []
                for line_text in complete_lines:
                    consumed += 1
                    offset += len(line_text.encode("utf-8")) + 1  # +1 for \n
                    stripped = line_text.strip()
                    if not stripped:
                        continue
                    try:
                        message = json.loads(stripped)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(message, dict):
                        continue
                    # ``ordinal`` is the record's 0-based physical line position
                    # in its step file. ``consumed`` counts every physical line
                    # (blank lines included, so it stays a true line number), and
                    # was just incremented for THIS line, so ``consumed - 1`` is
                    # the 0-based index. It is the stable identity a full read of
                    # the same file reproduces exactly (see the full-read ``_emit``
                    # ordinal below), so an append delta and a later full snapshot
                    # tag the same logical line with the same ordinal — the
                    # invariant the frontend's idempotent reconcile relies on.
                    records.append(
                        {
                            "step_id": step_id,
                            "step_type": step_type,
                            "ordinal": consumed - 1,
                            "message": message,
                        }
                    )
                    if len(records) >= MAX_RECORDS_PER_REPORT:
                        truncated = True
                        # Only advance the offset table to the truncation
                        # point.  The caller's cursor will match consumed
                        # and the next read continues from here.
                        self._record_offset(
                            jsonl_key,
                            jsonl,
                            consumed,
                            offset,
                            cur_mtime,
                            cur_size,
                            prev_hasher,
                            new_bytes_encoded[: offset - prev_offset_start],
                        )
                        new_cursor[jsonl.name] = consumed
                        break
                else:
                    # No truncation — update offset table with full state.
                    self._record_offset(
                        jsonl_key,
                        jsonl,
                        consumed,
                        offset,
                        cur_mtime,
                        cur_size,
                        prev_hasher,
                        new_bytes_encoded[: offset - prev_offset_start],
                    )
                    new_cursor[jsonl.name] = consumed
            else:
                # Full read: first read, cursor rollback, or file replaced.
                try:
                    with open(jsonl, "r", encoding="utf-8") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                raw_lines = raw.split("\n")
                # ``split("\n")`` always yields a trailing element after the
                # final byte: ``""`` when the file ends with ``\n`` (every real
                # line is newline-terminated), or the bytes of the last line
                # when the file has NO trailing newline.  That un-terminated
                # tail is ambiguous: it is either
                #   (a) a COMPLETE record written atomically without a newline
                #       (``write_text(json.dumps(record))`` — terminal step
                #       files, sidecars), which MUST be read; or
                #   (b) a record caught MID-WRITE while the agent streams (a
                #       worktree / discovery first snapshot landing on a
                #       half-flushed line), which must be left for the next
                #       round.
                # The two are told apart by parseability: a complete record is
                # valid JSON, a half-written one is truncated and is not.  The
                # previous code unconditionally consumed the tail — for case (b)
                # the truncated JSON failed ``json.loads`` and was dropped, yet
                # ``consumed``/the byte offset still advanced past it, so the
                # record was never re-read once completed (the "first assistant
                # body empty, no further records" symptom).  Now an unparseable
                # tail is left intact; a parseable one is still consumed.
                if raw_lines and raw_lines[-1] == "":
                    # File ends with a newline: the trailing "" is a split
                    # artifact; every remaining element is a complete line.
                    complete_lines = raw_lines[:-1]
                    tail = None
                else:
                    # No trailing newline: hold the last element aside and
                    # decide whether to consume it by parseability below.
                    complete_lines = raw_lines[:-1] if raw_lines else []
                    tail = raw_lines[-1] if raw_lines else None

                consumed = 0
                offset = 0
                start = cursor_lines

                # Detect a truncated / replaced file.  The full-read fallback is
                # entered (among other reasons) when the file has shrunk below
                # its recorded byte offset, which happens when a step's jsonl is
                # rewritten in place — e.g. a FAILED step retried, replacing the
                # old records with a fresh, shorter batch.  In that case the
                # caller's line-count cursor refers to lines of the *old* file
                # and is meaningless against the replacement: honouring it as
                # ``start`` would skip every replacement line whose index is
                # below it (old cursor 20, replacement of 5 records → all 5
                # skipped, an empty append recorded, the cursor advanced to 5),
                # silently dropping the retry batch from the live conversation
                # until a separate full reload runs.  Two independent signals
                # mark a stale cursor: (a) the file was rewritten in place — its
                # bounded head fingerprint changed (grow case), its whole-prefix
                # hash diverged (equal-size case), or it shrank below the size we
                # last saw — even when the replacement is the same size as, or larger
                # than, the old consumed byte offset (``rewritten``, computed
                # above; a pure size/offset comparison cannot detect this and the
                # incremental path would otherwise either record no new bytes or
                # read only the suffix past the stale offset); and (b) the cursor
                # points past the end of the file's current content (covers the
                # daemon cold-start case where ``prev`` is ``None`` but the
                # on-disk file was already replaced while the daemon was down).
                # Either way reset ``start`` to 0 so the full replacement content
                # is delivered from the beginning rather than skipped.
                total_physical_lines = len(complete_lines) + (
                    1 if tail is not None else 0
                )
                if rewritten or start > total_physical_lines:
                    start = 0

                def _emit(line_text: str, ordinal: int) -> bool:
                    """Append a parsed record for *line_text*; return truncation.

                    *ordinal* is the record's 0-based physical line position in
                    its step file. A full read reproduces the SAME ordinal an
                    append delta assigned the same logical line (both derive it
                    from the physical line index), so the frontend can dedupe /
                    update in place by ``step_id#ordinal`` regardless of which
                    read path delivered the record.
                    """
                    stripped = line_text.strip()
                    if not stripped:
                        return False
                    try:
                        message = json.loads(stripped)
                    except (ValueError, TypeError):
                        return False
                    if not isinstance(message, dict):
                        return False
                    records.append(
                        {
                            "step_id": step_id,
                            "step_type": step_type,
                            "ordinal": ordinal,
                            "message": message,
                        }
                    )
                    return len(records) >= MAX_RECORDS_PER_REPORT

                for idx, line_text in enumerate(complete_lines):
                    consumed = idx + 1
                    # Every complete line is newline-terminated, so the on-disk
                    # span is its UTF-8 byte length plus the one ``\n`` delimiter.
                    offset += len(line_text.encode("utf-8")) + 1
                    if idx < start:
                        continue
                    if _emit(line_text, idx):
                        truncated = True
                        break

                if not truncated and tail is not None:
                    # The un-terminated final line.  Consume it ONLY when it is
                    # a parseable, complete record; an unparseable (mid-write)
                    # tail is left untouched so the next read re-reads it once
                    # the writer finishes the line.
                    stripped_tail = tail.strip()
                    parsed_tail = None
                    if stripped_tail:
                        try:
                            parsed_tail = json.loads(stripped_tail)
                        except (ValueError, TypeError):
                            parsed_tail = None
                    if not stripped_tail or isinstance(parsed_tail, dict):
                        tail_idx = len(complete_lines)
                        consumed = tail_idx + 1
                        # No trailing newline for the tail line.
                        offset += len(tail.encode("utf-8"))
                        if tail_idx >= start and _emit(tail, tail_idx):
                            truncated = True
                    # else: leave consumed/offset at the last complete line so
                    # the partial tail is re-read next round.

                # Record a fresh bounded head fingerprint and start a fresh
                # running full-prefix hash over the newly consumed prefix (this
                # was a from-scratch full read, so the prior hash — if any — is
                # discarded).  The consumed prefix is exactly ``raw[:offset]``
                # in bytes, taken from the bytes already read (no extra disk
                # read).  The next round can then tell an append (prefix
                # unchanged) from another rewrite (prefix changed).
                self._record_offset(
                    jsonl_key,
                    jsonl,
                    consumed,
                    offset,
                    cur_mtime,
                    cur_size,
                    None,
                    raw.encode("utf-8")[:offset],
                )
                new_cursor[jsonl.name] = consumed

        # HOP-2 DEBUG: the delta this read produced. ``records==0`` on an active
        # flow means the push loop ships nothing this tick even though the
        # signature fired — a candidate #260 drop point; a non-empty ``append``
        # is the increment that must reach the live view.
        logger.debug(
            "hist-diag read_flow RESULT flow=%s mode=%s records=%d cursor=%s",
            flow_id, mode, len(records), new_cursor,
        )
        return FlowRead(flow_id=flow_id, mode=mode, records=records, cursor=new_cursor)

    def read_active_flows(
        self, cursors: Optional[Dict[str, Dict[str, int]]] = None
    ) -> List[FlowRead]:
        """Read incremental records for every currently active flow.

        Args:
            cursors: A ``{flow_id: cursor}`` map of the caller's last position
                per active flow. A flow absent from the map is read as a
                ``full`` snapshot (its first push); a present one is read as an
                ``append`` delta.

        Returns:
            One :class:`FlowRead` per active flow (always, even with an empty
            ``records`` list, so the caller keeps advancing the cursor), plus a
            *final-flush* read for any flow the caller was tracking that has
            since gone terminal but still has un-pushed records. The caller
            should always store the returned ``cursor`` and only transmit reads
            whose ``records`` are non-empty.
        """
        cursors = cursors or {}
        reads: List[FlowRead] = []
        active_ids: Set[str] = set()
        index = self.build_index()
        # Map every indexed flow to its project root so the final-flush pass
        # below can scope ``read_flow`` to the correct root.  Without this, the
        # final flush fell back to scanning *every* tracked root, which for a
        # ``--worktree`` / discovery flow (attributed to its main root, with its
        # history under that root) could resolve to the wrong directory — or to
        # ``None`` — and silently return no records, so the tail written just
        # before a terminal transition never reached the web.
        root_by_flow: Dict[str, str] = {m.flow_id: m.project_root for m in index}
        for meta in index:
            # Re-check live status on disk: the build_index cache may carry
            # stale ``active`` flags for up to BUILD_INDEX_TTL seconds.
            if not self._is_still_active(meta):
                continue
            active_ids.add(meta.flow_id)
            reads.append(
                self.read_flow(
                    meta.flow_id,
                    project_root=meta.project_root,
                    cursor=cursors.get(meta.flow_id),
                )
            )
        # Final flush: a flow the caller was tracking (it has a cursor) but that
        # is no longer active may still have records appended between the last
        # push and its terminal transition — e.g. the commit / summarize
        # ``step_completed`` lines written just before the flow flips to
        # COMPLETED and is archived. Without this, that tail only surfaces after
        # archival (the "web catches up only once the run finishes" symptom).
        # Read each such flow once more and surface it only when it has pending
        # records, so the next idle poll returns nothing and the caller can drop
        # its cursor without ever re-pushing an already-delivered line.
        for flow_id, cursor in cursors.items():
            if flow_id in active_ids:
                continue
            # Scope the final flush to the flow's own project root when known
            # (from the index above).  ``root_by_flow.get`` yields ``None`` for a
            # flow no longer in the index, in which case ``read_flow`` keeps its
            # backward-compatible behaviour of scanning every tracked root.
            read = self.read_flow(
                flow_id,
                project_root=root_by_flow.get(flow_id),
                cursor=cursor,
            )
            if read.records:
                reads.append(read)
        # HOP-2 DEBUG: the active set this poll produced. If a flow that is
        # appending on disk is absent from ``active_ids`` (dropped by
        # _is_still_active) it produces no delta at all — the persistent-freeze
        # failure mode. Logged with the caller's tracked-cursor flows so a live
        # run reveals a flow silently leaving the active set.
        logger.debug(
            "hist-diag read_active_flows active_ids=%s tracked_cursors=%s "
            "reads_with_records=%s",
            sorted(active_ids), sorted(cursors),
            [(r.flow_id, r.mode, len(r.records)) for r in reads],
        )
        return reads

    def active_flow_signature(self) -> Dict[str, Any]:
        """Return a cheap change-detection fingerprint for every active flow.

        Maps ``flow_id`` to a comparable token built from the on-disk artifacts
        that move forward as a running flow makes progress:

        * the active ``engine.json``'s ``(mtime, size)`` plus its ``status`` — a
          step transition (and the PAUSED↔RUNNING flip around a resume) rewrites
          ``engine.json``;
        * each per-step ``jsonl``'s ``(name, mtime, size)`` — a new prompt /
          response / ``step_completed`` record appends to (or creates) a jsonl.

        Including the byte *size* (not just the mtime) makes the token change on
        every append even when two writes land inside the filesystem's mtime
        resolution, so a caller can drive history pushes off real disk changes
        rather than a fixed timer. Terminal (completed / failed) flows are
        excluded — they have nothing left to stream incrementally.

        This is intentionally lighter than :func:`build_index`: it only reads
        the active ``engine.json`` per root (and that flow's jsonl files),
        skipping the archive / history-only enumeration, so it is safe to call
        on a fast polling cadence.
        """
        signature: Dict[str, Any] = {}
        for root in self._iter_roots():
            engine_json = root / "se3" / "state" / "engine.json"
            # active=True read: a matching (path, mtime, size) is trusted, a real
            # rewrite moves the key and re-parses. The signature below also folds
            # in the engine.json (mtime, size) directly, so a genuine flow change
            # always shifts the signature.
            data = read_engine_header(engine_json, active=True)
            if not isinstance(data, dict):
                continue
            flow_id = str(data.get("flow_id") or "")
            if not flow_id:
                continue
            status = str(data.get("status") or "")
            if not _is_active_status(status):
                continue
            # The raw ``(mtime, size)`` alone debounces a same-``(mtime, size)``
            # in-place engine.json rewrite — the PAUSE→resume / same-length
            # status-flip churn on coarse-mtime filesystems — so the push loop
            # would stall that frame until an unrelated jsonl append nudges the
            # token. Fold in the whole-content digest the ``read_engine_header``
            # call just above already computed and cached (zero extra read/hash):
            # a mid-file rewrite that keeps ``(mtime, size)`` identical still
            # moves the digest, so the signature shifts and the delta is read on
            # the next tick. ``None`` (never-active read / oversized degrade)
            # leaves the token as before, matching the previous behaviour.
            engine_digest = cached_content_digest(engine_json)
            parts: List[Any] = [
                ("__engine__", *_safe_stat(engine_json), engine_digest),
                ("__status__", status.strip().lower()),
            ]
            hist_dir = root / "se3" / "history" / flow_id
            if hist_dir.is_dir():
                # Include ``*.jsonl.from-<branch>`` sidecars so a worktree
                # merge-back that only appends sidecar records still moves the
                # signature forward and triggers a history push.
                for jsonl in _iter_history_jsonl(hist_dir):
                    mtime, size = _safe_stat(jsonl)
                    parts.append((jsonl.name, mtime, size))
            signature[flow_id] = tuple(parts)
            # HOP-1 DEBUG: the change-detection fingerprint the push loop diffs
            # (via client._history_changed) to decide whether to read+push a
            # delta. The __engine__ part carries the raw ``(mtime, size)`` PLUS
            # the whole-content digest, so a same-(mtime,size) in-place rewrite
            # still shifts it; __status__/flow_id come from the cached parse.
            # Logged with the jsonl-part count so a live run can see, tick by
            # tick, whether the boundary actually shifts the signature (a new
            # 02_analyze jsonl adds a part) or debounces.
            logger.debug(
                "hist-diag active_flow_signature: flow=%s status=%s "
                "engine=%s jsonl_parts=%d",
                flow_id, status.strip().lower(),
                (parts[0][1], parts[0][2]), len(parts) - 2,
            )
        return signature

    def live_flow_ids(self) -> Set[str]:
        """Return the flow_ids that are the current ``engine.json`` flow per root.

        Unlike :meth:`active_flow_signature` (which excludes terminal flows),
        this includes a flow whose status is terminal (FAILED / COMPLETED) as
        long as its ``engine.json`` has not yet been replaced by a new run or
        archived — i.e. a flow that can still flip back to active via
        ``se3 run --resume``. The daemon client uses this to retain a
        final-flushed terminal flow's history cursor so a resume continues
        incrementally instead of forcing a full re-read.

        The result is bounded by the number of project roots (one
        ``engine.json`` flow each), so retaining their cursors keeps the
        client's cursor map bounded over a long-lived daemon. Like
        :meth:`active_flow_signature`, it only reads each root's ``engine.json``
        (no archive / history enumeration), so it is cheap to call per push.
        """
        ids: Set[str] = set()
        for root in self._iter_roots():
            # active=True read: shares the at-most-once-per-change parse with the
            # tick's other active readers; a matching (path, mtime, size) is
            # trusted (no re-read). The caller ``_resumable_flow_ids`` is offloaded
            # off the event loop, so that parse never runs on it.
            data = read_engine_header(
                root / "se3" / "state" / "engine.json", active=True
            )
            if isinstance(data, dict) and data.get("flow_id"):
                ids.add(str(data["flow_id"]))
        return ids


# -- module-level file helpers --------------------------------------------
#
# All engine.json / archive / resumable snapshot / _meta.json parsing is now
# served by the module-level, ``(path, mtime, size)``-keyed
# ``se3.daemon.disk_json_cache`` (``read_engine_header`` / ``read_json_cached``),
# which supersedes the earlier content-keyed ``_read_engine_cached`` /
# ``_ENGINE_PARSE_CACHE``: it skips even the ``read_text`` on an unchanged file
# and never full-parses an oversized legacy file (bounded head+tail degraded
# read instead), closing the #209 per-tick CPU sink at its source.


def _safe_mtime(path: Path) -> Optional[float]:
    """Return *path*'s mtime, or ``None`` when it is missing / unreadable."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _safe_stat(path: Path) -> tuple:
    """Return *path*'s ``(mtime, size)``, or ``(0.0, 0)`` when unreadable.

    Used by :meth:`DaemonHistoryReader.active_flow_signature`; pairing mtime
    with byte size makes the signature change on every append even when two
    writes land inside the filesystem's mtime resolution.
    """
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, 0)


def _logical_step_id(filename: str) -> str:
    """Return the logical step id for a history file name.

    Strips the ``.jsonl`` extension together with any trailing
    ``.from-<branch>`` *sidecar* suffix, so a step's primary file and its
    sidecars (written by ``se3 merge``'s runtime sync on a --worktree
    merge-back) collapse to the same step id and merge into one step stream::

        01_discovery_ab12.jsonl                        -> "01_discovery_ab12"
        01_discovery_ab12.jsonl.from-worktree__b       -> "01_discovery_ab12"
        01_discovery_ab12.jsonl.from-worktree__b.0a1b2c3d -> "01_discovery_ab12"

    A name without ``.jsonl`` is returned unchanged.
    """
    idx = filename.find(".jsonl")
    if idx < 0:
        return filename
    return filename[:idx]


def _cross_root_step_key(filename: str) -> str:
    """Return a *cross-root* logical step identity for merge de-duplication.

    When a flow's history is split across two roots (the ``se3 run --worktree``
    case), the SAME logical step can appear under file names that differ only by
    their per-step ``<hash>`` segment — e.g. the discovery step is
    ``01_discovery_ab.jsonl`` in the main repo but cloned into the worktree as
    ``01_discovery_cd.jsonl``. Keying the cross-root merge by the *physical*
    file name treats those as two different steps and renders the discovery
    twice; this key collapses them to one by stripping the trailing hash while
    keeping every structural piece that genuinely distinguishes one step from
    another:

    * the ``NN`` sequence number and the ``<step_type>`` (so ``01_discovery``
      and ``02_analyze`` stay distinct);
    * an optional ``_Gk`` DAG-group suffix (so two implement groups stay
      distinct);
    * any ``.from-<branch>`` *sidecar* marker (so a step's primary file and its
      merge-back sidecar stay DISTINCT and are BOTH read — the post-merge
      single-root behaviour relies on this).

    Examples::

        01_discovery_ab.jsonl                      -> "01_discovery\\x00"
        01_discovery_cd.jsonl                      -> "01_discovery\\x00"   (dedups)
        02_analyze_ef.jsonl                        -> "02_analyze\\x00"
        05_implement_aa_G2.jsonl                   -> "05_implement_G2\\x00"
        01_discovery_ab.jsonl.from-worktree__b     -> "01_discovery\\x00worktree__b"

    This is a pure function and never raises; the ``\\x00`` separator keeps the
    sidecar marker unambiguous against a step type that contains underscores.
    """
    idx = filename.find(".jsonl")
    if idx < 0:
        stem = filename
        sidecar = ""
    else:
        stem = filename[:idx]
        suffix = filename[idx + len(".jsonl") :]
        m = re.match(r"\.from-(.+)$", suffix)
        sidecar = m.group(1) if m else ""

    # Strip an optional trailing ``_Gk`` group suffix, preserving it in the key.
    group = ""
    gm = re.search(r"_G\d+$", stem)
    if gm:
        group = gm.group(0)
        stem = stem[: gm.start()]

    # Strip a trailing hexadecimal hash segment, keeping ``NN_<step_type>``.
    parts = stem.split("_")
    if len(parts) >= 2 and parts[-1] and re.fullmatch(r"[0-9a-fA-F]+", parts[-1]):
        stem = "_".join(parts[:-1])

    return f"{stem}{group}\x00{sidecar}"


def _iter_history_jsonl(flow_dir: Path) -> List[Path]:
    """Return a flow directory's per-step history files, sorted by name.

    Includes both the primary ``*.jsonl`` files and the
    ``*.jsonl.from-<branch>`` *sidecar* files that ``se3 merge``'s runtime sync
    writes when a --worktree flow's per-step history collides with the main
    project on merge-back (see the ``se3 merge`` *Runtime Data
    Synchronization* requirement). The plain ``glob("*.jsonl")`` never matched
    the sidecars, so a worktree session's conversation after its first record
    was silently dropped — this helper restores it.

    Sorting by name keeps a step's primary file ahead of its sidecars
    (``foo.jsonl`` sorts before ``foo.jsonl.from-…``) and orders steps by their
    ``NN_`` sequence prefix, so the merged stream stays in step / record order.
    """
    if not flow_dir.is_dir():
        return []
    files = list(flow_dir.glob("*.jsonl"))
    files.extend(flow_dir.glob("*.jsonl.from-*"))
    return sorted(files, key=lambda p: p.name)


def _count_jsonl(history_dir: Path) -> int:
    """Count the distinct per-step history streams in a flow's directory.

    Counts *logical* steps (see :func:`_logical_step_id`), not physical files,
    so a step that exists only as a ``*.jsonl.from-<branch>`` sidecar still
    counts, and a primary file together with its sidecars counts once.
    """
    if not history_dir.is_dir():
        return 0
    steps: Set[str] = set()
    for f in _iter_history_jsonl(history_dir):
        steps.add(_logical_step_id(f.name))
    return len(steps)


def _clip(text: str) -> str:
    """Clip *text* to :data:`_DESC_CLIP` characters with an ellipsis."""
    text = text.strip()
    if len(text) > _DESC_CLIP:
        return text[:_DESC_CLIP] + "..."
    return text


def enumerate_historical_project_roots(
    search_roots: Iterable[Any] = (),
) -> List[str]:
    """Enumerate project roots that contain SE3 history artifacts.

    Walks each ``search_root`` looking at:

    * ``<root>/se3/state/archive/engine_*.json`` — archived flow state; when
      the file carries a ``project_root`` field that path is included.
    * ``<root>/se3/history/<flow_id>/_meta.json`` — per-flow history meta;
      when the file carries a ``project_root`` field that path is included.

    The ``search_root`` itself is also included whenever any of the above
    artifacts are present, since the parent of ``se3/history/`` /
    ``se3/state/archive/`` is by construction a historical project root —
    even if the on-disk artifact does not (yet) record a ``project_root``
    field.

    Every candidate path is normalised via :func:`os.path.realpath`,
    deduplicated, and a stale candidate (a path that no longer exists or is
    not a directory) is dropped. Corrupt or unreadable JSON files are logged
    at warning level and skipped — they do not abort the enumeration.

    Callers (see :meth:`DaemonAggregator.all_project_roots`) now feed the
    *union* of the live active roots and the persistent registry roots, so a
    root that has on-disk history but no currently running flow is still scanned
    for its archive / history artifacts.

    Args:
        search_roots: Iterable of base directories to scan. Anything that
            cannot be turned into a real path is silently skipped; a non-dir
            entry is robustly skipped without aborting the rest.

    Returns:
        Sorted, deduplicated absolute paths of historical project roots.
    """
    candidates: Set[str] = set()
    for entry in search_roots:
        try:
            root = Path(entry).resolve()
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
        if not root.is_dir():
            continue

        has_artifacts = False

        archive_dir = root / "se3" / "state" / "archive"
        if archive_dir.is_dir():
            for archive_file in sorted(archive_dir.glob("engine_*.json")):
                has_artifacts = True
                # Only the top-level ``project_root`` is needed here, so an
                # oversized legacy archive is degraded to its hot keys instead of
                # fully parsed; ``read_engine_header`` returns ``None`` on an
                # unreadable / unextractable file so it is warned-once and skipped.
                data = read_engine_header(archive_file)
                if data is None:
                    _warn_once_unreadable(archive_file, "archive file")
                    continue
                _maybe_add_root(candidates, data.get("project_root"))

        history_root = root / "se3" / "history"
        if history_root.is_dir():
            for flow_dir in sorted(history_root.iterdir()):
                if not flow_dir.is_dir():
                    continue
                has_artifacts = True
                meta_path = flow_dir / "_meta.json"
                if not meta_path.is_file():
                    continue
                # ``_meta.json`` is small and re-scanned on every enumeration;
                # the stat-keyed cache parses an unchanged one only once.
                data = read_json_cached(meta_path)
                if data is None:
                    _warn_once_unreadable(meta_path, "meta file")
                    continue
                _maybe_add_root(candidates, data.get("project_root"))

        if has_artifacts:
            _maybe_add_root(candidates, str(root))

    return sorted(candidates)


def _maybe_add_root(candidates: Set[str], value: Any) -> None:
    """Normalise *value* and add it to *candidates* when it names a real dir."""
    if not isinstance(value, str) or not value:
        return
    try:
        resolved = os.path.realpath(value)
    except OSError:  # pragma: no cover - defensive
        return
    if not Path(resolved).is_dir():
        return
    candidates.add(resolved)


def _extract_history_summary(flow_dir: Path) -> str:
    """Recover a short task description from a history directory's first jsonl.

    Mirrors ``PersistenceManager.extract_history_summary`` so a history-only
    flow still carries a human-readable description in the index. Title
    extraction follows the same three-tier priority, aligned with the web
    chat-history display (``splitUserPromptByMarker``):

    1. The user's literal input cut out by the ``USER_CONTENT`` markers
       (:func:`~se3.engine.prompt_markers.extract_user_content`);
    2. otherwise the embedded ``Task description: --- ... ---`` block;
    3. otherwise the raw content (untruncated — clipping is applied by the
       caller via :func:`_clip`).

    The first jsonl line is frequently a ``step_started`` (or other) *event*
    record that carries no user content, with the real user prompt on a later
    line. The extractor therefore scans forward — skipping event records — to
    the first record actually carrying user content (see
    :func:`~se3.engine.prompt_markers.first_user_content`), and the scan is
    bounded so a large file is never fully read.
    """
    from ..engine.prompt_markers import extract_user_content, first_user_content

    # Include ``*.jsonl.from-<branch>`` sidecars so a worktree session whose
    # first step exists only as a merge-back sidecar still recovers a title.
    jsonl_files = _iter_history_jsonl(flow_dir)
    if not jsonl_files:
        return "(no history data)"
    try:
        # Stream the leading records (bounded) rather than reading the whole
        # file: ``first_user_content`` skips ``step_started`` / progress events
        # and stops at the first record carrying user content.
        with open(jsonl_files[0], "r", encoding="utf-8", errors="replace") as fh:
            content = first_user_content(fh)
        if content is None:
            return "(no state data)"
        # 1. Prefer the user's literal input delimited by USER_CONTENT markers.
        user_content = extract_user_content(content)
        if user_content is not None:
            return user_content
        # 2. Extract embedded task description if present.
        match = re.search(
            r"Task description:\s*-+\s*(.*?)\s*-+", str(content), re.DOTALL
        )
        if match:
            return match.group(1).strip()
        # 3. Fallback: raw content (clipped by the caller).
        return str(content)
    except Exception:
        return "(no state data)"
