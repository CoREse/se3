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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from .protocol import HISTORY_MODE_APPEND, HISTORY_MODE_FULL

logger = logging.getLogger(__name__)

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

#: Index task-description fields are clipped to this many characters.
_DESC_CLIP = 200

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
        }


@dataclass
class FlowRead:
    """The result of one :func:`DaemonHistoryReader.read_flow` call.

    Attributes:
        flow_id: The flow these records belong to.
        mode: :data:`~se3.daemon.protocol.HISTORY_MODE_FULL` for the initial
            batch (the requester had no cursor) or
            :data:`~se3.daemon.protocol.HISTORY_MODE_APPEND` for a delta.
        records: A list of ``{"step_id": str, "message": dict}`` records, one
            per conversation line, ordered by step file then line.
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
        """
        metas: List[SessionMeta] = []
        seen: set = set()
        for root in self._iter_roots():
            try:
                self._index_root(root, metas, seen)
            except Exception:  # pragma: no cover - defensive
                logger.exception("history: failed to index root %s", root)
        metas.sort(key=lambda m: m.updated_at or "", reverse=True)
        return metas

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
        )

    def _meta_from_history(self, root: Path, flow_dir: Path) -> SessionMeta:
        """Build a best-effort :class:`SessionMeta` for a history-only flow.

        A history-only flow has no surviving ``engine.json``; metadata is
        recovered from an optional ``_meta.json`` plus the ``jsonl`` files
        themselves, so the session still appears in the index.
        """
        flow_id = flow_dir.name
        meta = _read_json(flow_dir / "_meta.json") or {}
        try:
            latest = max(
                (f.stat().st_mtime for f in flow_dir.iterdir() if f.is_file()),
                default=0.0,
            )
        except OSError:  # pragma: no cover - defensive
            latest = 0.0
        updated = datetime.fromtimestamp(latest).isoformat() if latest else ""
        return SessionMeta(
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

        for jsonl in sorted(flow_dir.glob("*.jsonl")):
            if truncated:
                break
            step_id = jsonl.stem
            try:
                lines = jsonl.read_text(encoding="utf-8").splitlines()
            except OSError:  # pragma: no cover - defensive
                continue
            start = int(cursor.get(jsonl.name, 0) or 0)
            if start < 0:
                start = 0
            consumed = min(start, len(lines))
            for idx in range(start, len(lines)):
                consumed = idx + 1
                line = lines[idx].strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(message, dict):
                    continue
                records.append({"step_id": step_id, "message": message})
                if len(records) >= MAX_RECORDS_PER_REPORT:
                    truncated = True
                    break
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
            if not meta.active:
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
                for jsonl in sorted(hist_dir.glob("*.jsonl")):
                    mtime, size = _safe_stat(jsonl)
                    parts.append((jsonl.name, mtime, size))
            signature[flow_id] = tuple(parts)
        return signature


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


def _count_jsonl(history_dir: Path) -> int:
    """Count the per-step ``jsonl`` files in a flow's history directory."""
    if not history_dir.is_dir():
        return 0
    return sum(1 for _ in history_dir.glob("*.jsonl"))


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

    Args:
        search_roots: Iterable of base directories to scan. Anything that
            cannot be turned into a real path is silently skipped.

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
                    logger.warning(
                        "history: skipping unreadable archive file %s",
                        archive_file,
                    )
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
                    logger.warning(
                        "history: skipping unreadable meta file %s", meta_path
                    )
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
    flow still carries a human-readable description in the index.
    """
    jsonl_files = sorted(flow_dir.glob("*.jsonl"))
    if not jsonl_files:
        return "(no history data)"
    try:
        first_line = jsonl_files[0].read_text(encoding="utf-8").split("\n")[0]
        data = json.loads(first_line)
        content = data.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    content = block.get("text", "")
                    break
            else:
                content = str(content)
        match = re.search(
            r"Task description:\s*-+\s*(.*?)\s*-+", str(content), re.DOTALL
        )
        if match:
            return match.group(1).strip()
        return str(content)
    except Exception:
        return "(no state data)"
