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

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .protocol import HISTORY_MODE_APPEND, HISTORY_MODE_FULL

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
    source: str = "history"  # "active" | "archived" | "history"
    step_count: int = 0
    # Running sub-state mirrored from the active engine.json's top-level
    # ``waiting_for_lock`` flag: True while a synchronous run is queued behind
    # the main-worktree mutex before its first code-touching step. Only ever
    # True for an active ("active" source) flow; history-only / archived flows
    # are never waiting.
    waiting_for_lock: bool = False

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
        }


@dataclass
class FlowRead:
    """The result of one :func:`DaemonHistoryReader.read_flow` call.

    Attributes:
        flow_id: The flow these records belong to.
        mode: :data:`~se3.daemon.protocol.HISTORY_MODE_FULL` for the initial
            batch (the requester had no cursor) or
            :data:`~se3.daemon.protocol.HISTORY_MODE_APPEND` for a delta.
        records: A list of ``{"step_id": str, "step_type": str, "message":
            dict}`` records, one per conversation line, ordered by step file
            then line. ``step_type`` is the authoritative type parsed from the
            jsonl file-name stem (see :func:`parse_step_type_from_step_id`); it
            is injected at the envelope level so the frontend never has to guess
            it, while ``message`` keeps its original bytes untouched.
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
        # Value: ``(consumed_lines, byte_offset, mtime, size)`` where
        # *consumed_lines* is the count of fully consumed newline-terminated
        # lines, *byte_offset* is the file position after the last consumed
        # newline, and *mtime*/*size* are the file stat at the time of the last
        # read (used to detect truncation / replacement).
        #
        # When the caller's cursor line-count matches *consumed_lines* and the
        # current file size is >= *byte_offset*, only the new bytes past the
        # offset are read (seek + incremental parse).  Otherwise (first read,
        # cursor rollback, file shrunk) a full read from the start is performed
        # and the entry is rebuilt.
        self._read_offsets: Dict[str, Tuple[int, int, float, int]] = {}

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
        """Uncached index build — walks all roots from disk."""
        metas: List[SessionMeta] = []
        seen: set = set()
        for root in self._iter_roots():
            try:
                self._index_root(root, metas, seen)
            except Exception:  # pragma: no cover - defensive
                logger.exception("history: failed to index root %s", root)
        metas.sort(key=lambda m: m.updated_at or "", reverse=True)
        return metas

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
        data = _read_json(engine_json)
        if not isinstance(data, dict):
            return False
        # Confirm the engine.json still describes *this* flow.  Without this
        # check, if flow F1 completed and a different flow F2 is now the
        # active engine.json flow (status RUNNING), _is_still_active(F1_meta)
        # would read F2's RUNNING status and incorrectly return True — keeping
        # the stale F1 meta in the active set and missing F2 entirely.
        if str(data.get("flow_id") or "") != meta.flow_id:
            return False
        status = str(data.get("status") or "")
        return _is_active_status(status)

    def _index_root(
        self, root: Path, metas: List[SessionMeta], seen: set
    ) -> None:
        """Append the sessions found under one project *root* into *metas*."""
        state_dir = root / "se3" / "state"

        # 1. Active flow from engine.json.
        data = _read_json(state_dir / "engine.json")
        if isinstance(data, dict) and data.get("flow_id"):
            flow_id = str(data["flow_id"])
            if flow_id not in seen:
                seen.add(flow_id)
                metas.append(self._meta_from_engine(root, data, source="active"))

        # 2. Archived flows.
        archive_dir = state_dir / "archive"
        if archive_dir.is_dir():
            for archive_file in sorted(archive_dir.glob("engine_*.json")):
                adata = _read_json(archive_file)
                if not isinstance(adata, dict):
                    continue
                flow_id = str(adata.get("flow_id") or "")
                if not flow_id or flow_id in seen:
                    continue
                seen.add(flow_id)
                metas.append(
                    self._meta_from_engine(
                        root,
                        adata,
                        source="archived",
                        fallback_mtime=_safe_mtime(archive_file),
                    )
                )

        # 3. History-only flows (may have no engine.json at all).
        history_root = root / "se3" / "history"
        if history_root.is_dir():
            for flow_dir in sorted(history_root.iterdir()):
                if not flow_dir.is_dir():
                    continue
                flow_id = flow_dir.name
                if flow_id in seen:
                    continue
                seen.add(flow_id)
                metas.append(self._meta_from_history(root, flow_dir))

    def _meta_from_engine(
        self,
        root: Path,
        data: Dict[str, Any],
        *,
        source: str,
        fallback_mtime: Optional[float] = None,
    ) -> SessionMeta:
        """Build a :class:`SessionMeta` from an ``engine.json``-shaped dict."""
        status = str(data.get("status") or "unknown")
        active = source == "active" and _is_active_status(status)
        updated = str(data.get("updated_at") or "")
        if not updated and fallback_mtime:
            updated = datetime.fromtimestamp(fallback_mtime).isoformat()
        flow_id = str(data.get("flow_id"))
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
            # A flow is only meaningfully "waiting for lock" while it is the live
            # active flow; an archived/terminal snapshot is never queued. Reading
            # only on the active source also keeps a stale True out of history.
            waiting_for_lock=bool(active and data.get("waiting_for_lock", False)),
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
        meta = _read_json(flow_dir / "_meta.json") or {}
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
            project_root: Optional root to scope the lookup; when omitted every
                tracked root is searched.
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

        flow_dir = self._resolve_flow_dir(flow_id, project_root)
        if flow_dir is None:
            return FlowRead(flow_id=flow_id, mode=mode, records=[], cursor=cursor)

        new_cursor: Dict[str, int] = dict(cursor)
        records: List[Dict[str, Any]] = []
        truncated = False

        for jsonl in _iter_history_jsonl(flow_dir):
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
            can_incremental = (
                prev is not None
                and cursor_lines == prev[0]       # cursor matches consumed lines
                and cur_size >= prev[1]            # file has not shrunk
                and prev[1] >= 0                   # offset is valid
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
                    records.append(
                        {
                            "step_id": step_id,
                            "step_type": step_type,
                            "message": message,
                        }
                    )
                    if len(records) >= MAX_RECORDS_PER_REPORT:
                        truncated = True
                        # Only advance the offset table to the truncation
                        # point.  The caller's cursor will match consumed
                        # and the next read continues from here.
                        self._read_offsets[jsonl_key] = (
                            consumed, offset, cur_mtime, cur_size,
                        )
                        new_cursor[jsonl.name] = consumed
                        break
                else:
                    # No truncation — update offset table with full state.
                    self._read_offsets[jsonl_key] = (
                        consumed, offset, cur_mtime, cur_size,
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
                # For a full read we process ALL lines, including a last line
                # without a trailing newline (a complete file written by
                # write_text without "\n").  Unlike the incremental path,
                # there is no concurrent writer risk here — the file was
                # either just created or fully replaced.
                if raw_lines and raw_lines[-1] == "":
                    # Trailing newline: drop the split artifact.
                    all_lines = raw_lines[:-1]
                else:
                    # No trailing newline: every element is a real line.
                    all_lines = raw_lines
                consumed = 0
                offset = 0
                start = cursor_lines
                num_lines = len(all_lines)
                for idx, line_text in enumerate(all_lines):
                    consumed = idx + 1
                    line_bytes = len(line_text.encode("utf-8"))
                    # Add 1 for the \n delimiter — except for the very last
                    # line when the file has no trailing newline (the writer
                    # hasn't finished that line yet, or simply omitted \n).
                    has_trailing_nl = (raw_lines[-1] == "" if raw_lines else False)
                    if idx < num_lines - 1 or has_trailing_nl:
                        offset += line_bytes + 1
                    else:
                        offset += line_bytes
                    if idx < start:
                        continue
                    stripped = line_text.strip()
                    if not stripped:
                        continue
                    try:
                        message = json.loads(stripped)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(message, dict):
                        continue
                    records.append(
                        {
                            "step_id": step_id,
                            "step_type": step_type,
                            "message": message,
                        }
                    )
                    if len(records) >= MAX_RECORDS_PER_REPORT:
                        truncated = True
                        break
                self._read_offsets[jsonl_key] = (
                    consumed, offset, cur_mtime, cur_size,
                )
                new_cursor[jsonl.name] = consumed

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
        for meta in self.build_index():
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
            read = self.read_flow(flow_id, cursor=cursor)
            if read.records:
                reads.append(read)
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
            data = _read_json(engine_json)
            if not isinstance(data, dict):
                continue
            flow_id = str(data.get("flow_id") or "")
            if not flow_id:
                continue
            status = str(data.get("status") or "")
            if not _is_active_status(status):
                continue
            parts: List[Any] = [
                ("__engine__", *_safe_stat(engine_json)),
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
            data = _read_json(root / "se3" / "state" / "engine.json")
            if isinstance(data, dict) and data.get("flow_id"):
                ids.add(str(data["flow_id"]))
        return ids


# -- module-level file helpers --------------------------------------------


def _read_json(path: Path) -> Optional[dict]:
    """Read and parse a JSON file; return ``None`` on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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
                data = _read_json(archive_file)
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
                data = _read_json(meta_path)
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
    """
    from ..engine.prompt_markers import extract_user_content

    # Include ``*.jsonl.from-<branch>`` sidecars so a worktree session whose
    # first step exists only as a merge-back sidecar still recovers a title.
    jsonl_files = _iter_history_jsonl(flow_dir)
    if not jsonl_files:
        return "(no history data)"
    try:
        # Stream-read only the first line instead of reading the entire file.
        # Previously ``read_text().split("\\n")[0]`` loaded the whole jsonl
        # (which can be tens of MB) just to extract the title from line 1.
        with open(jsonl_files[0], "r", encoding="utf-8", errors="replace") as fh:
            first_line = fh.readline().rstrip("\n").rstrip("\r")
        data = json.loads(first_line)
        content = data.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    content = block.get("text", "")
                    break
            else:
                content = str(content)
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
