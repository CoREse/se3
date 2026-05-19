"""On-disk flow-state aggregation for the SE3 daemon.

:class:`DaemonAggregator` polls the structured artifacts that ``se3 run``
leaves on disk — ``se3/state/engine.json``, ``se3/state/summary-*.json``,
``se3/calls/``, ``se3/logs/``, ``se3/issues/`` — and folds them into a single
:class:`MachineStatus` snapshot describing the whole local machine.

The aggregator never reaches into a flow's process: it is a pure reader of the
files those flows write. This keeps it decoupled from the ``se3 run`` process
model (one-shot foreground command, no IPC) and robust to flows that started
before the daemon did.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 2.0


@dataclass
class PendingCall:
    """A queued human call awaiting a response.

    Mirrors a file under a project's ``se3/calls/`` directory (the interjection
    / human-call queue mechanism).
    """

    call_id: str
    path: str
    project_root: str
    kind: str = "call"
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return {
            "call_id": self.call_id,
            "path": self.path,
            "project_root": self.project_root,
            "kind": self.kind,
            "created_at": self.created_at,
        }


@dataclass
class FlowSnapshot:
    """Aggregated state of a single flow, read from its ``engine.json``."""

    project_root: str
    flow_id: Optional[str] = None
    task_description: str = ""
    task_type: str = ""
    status: str = "unknown"
    current_step: Optional[str] = None
    current_step_index: int = 0
    total_steps: int = 0
    progress: float = 0.0
    updated_at: Optional[str] = None
    pending_calls: List[PendingCall] = field(default_factory=list)
    log_count: int = 0
    issue_count: int = 0
    summary: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "project_root": self.project_root,
            "flow_id": self.flow_id,
            "task_description": self.task_description,
            "task_type": self.task_type,
            "status": self.status,
            "current_step": self.current_step,
            "current_step_index": self.current_step_index,
            "total_steps": self.total_steps,
            "progress": self.progress,
            "updated_at": self.updated_at,
            "pending_calls": [c.to_dict() for c in self.pending_calls],
            "log_count": self.log_count,
            "issue_count": self.issue_count,
            "summary": self.summary,
        }


@dataclass
class MachineStatus:
    """A full status snapshot of one SE3 machine."""

    machine_id: str
    hostname: str
    flows: List[FlowSnapshot] = field(default_factory=list)
    pending_calls: List[PendingCall] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, object]:
        return {
            "machine_id": self.machine_id,
            "hostname": self.hostname,
            "flows": [f.to_dict() for f in self.flows],
            "pending_calls": [c.to_dict() for c in self.pending_calls],
            "generated_at": self.generated_at,
        }


def _stable_machine_id() -> str:
    """Return a process-stable machine id (hostname plus a short uuid tail)."""
    return f"{socket.gethostname()}-{uuid.getnode():x}"


class DaemonAggregator:
    """Polls on-disk flow artifacts into a :class:`MachineStatus` snapshot."""

    def __init__(
        self,
        *,
        machine_id: Optional[str] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self.machine_id = machine_id or _stable_machine_id()
        self.hostname = socket.gethostname()
        self.poll_interval = max(0.1, float(poll_interval))
        self._project_roots: Set[Path] = set()
        # engine.json mtime per project root, for change detection.
        self._mtimes: Dict[str, float] = {}

    # -- project-root registry --------------------------------------------

    def add_project_root(self, path: object) -> None:
        """Register a project root whose ``engine.json`` should be polled."""
        self._project_roots.add(Path(path).resolve())

    def remove_project_root(self, path: object) -> None:
        """Stop polling *path*."""
        self._project_roots.discard(Path(path).resolve())

    def set_project_roots(self, paths: object) -> None:
        """Replace the polled project-root set with *paths*."""
        self._project_roots = {Path(p).resolve() for p in paths}

    @property
    def project_roots(self) -> List[Path]:
        """A snapshot list of registered project roots."""
        return sorted(self._project_roots)

    # -- change detection --------------------------------------------------

    def has_changes(self) -> bool:
        """Return whether any tracked ``engine.json`` mtime changed since last poll.

        Calling this updates the internal mtime cache, so two consecutive calls
        without an intervening file change report ``False`` the second time.
        """
        changed = False
        for root in self._project_roots:
            engine_json = root / "se3" / "state" / "engine.json"
            mtime = _safe_mtime(engine_json)
            key = str(engine_json)
            if mtime is not None and self._mtimes.get(key) != mtime:
                changed = True
            self._mtimes[key] = mtime if mtime is not None else 0.0
        return changed

    # -- snapshot ----------------------------------------------------------

    def get_snapshot(self) -> MachineStatus:
        """Build and return the current :class:`MachineStatus` snapshot."""
        flows: List[FlowSnapshot] = []
        all_calls: List[PendingCall] = []
        for root in sorted(self._project_roots):
            snapshot = self._snapshot_for_root(root)
            if snapshot is None:
                continue
            flows.append(snapshot)
            all_calls.extend(snapshot.pending_calls)
        return MachineStatus(
            machine_id=self.machine_id,
            hostname=self.hostname,
            flows=flows,
            pending_calls=all_calls,
        )

    # -- internals ---------------------------------------------------------

    def _snapshot_for_root(self, root: Path) -> Optional[FlowSnapshot]:
        """Build a :class:`FlowSnapshot` for one project root.

        Returns ``None`` only when the root has neither an ``engine.json`` nor
        any other readable SE3 artifact (nothing to report).
        """
        state_dir = root / "se3" / "state"
        engine_json = state_dir / "engine.json"
        data = _read_json(engine_json)

        pending_calls = self._enumerate_calls(root)
        log_count = _count_dir(root / "se3" / "logs")
        issue_count = _count_issues(root / "se3" / "issues")

        if data is None:
            if not pending_calls and log_count == 0 and issue_count == 0:
                return None
            return FlowSnapshot(
                project_root=str(root),
                pending_calls=pending_calls,
                log_count=log_count,
                issue_count=issue_count,
            )

        state = data.get("state") or {}
        selected = state.get("selected_steps") or []
        total = len(selected)
        index = int(state.get("current_step_index") or 0)
        progress = (index / total) if total else 0.0

        flow_id = data.get("flow_id")
        return FlowSnapshot(
            project_root=str(root),
            flow_id=str(flow_id) if flow_id else None,
            task_description=str(data.get("task_description") or ""),
            task_type=str(data.get("task_type") or ""),
            status=str(data.get("status") or "unknown"),
            current_step=_current_step(state),
            current_step_index=index,
            total_steps=total,
            progress=round(progress, 4),
            updated_at=data.get("updated_at"),
            pending_calls=pending_calls,
            log_count=log_count,
            issue_count=issue_count,
            summary=self._read_summary(state_dir, str(flow_id) if flow_id else None),
        )

    def _enumerate_calls(self, root: Path) -> List[PendingCall]:
        """List genuinely pending human-call files under ``se3/calls/``.

        An answered call's ``.json`` request file and its sibling
        ``.response`` / ``.response.json`` answer file both linger in the
        directory indefinitely (``se3 history`` and friends rely on them).
        Such answered calls MUST NOT be reported as pending, so we first
        collect the base name of every answered call, then emit only those
        call files that have no matching response sibling — and never emit
        the response files themselves.
        """
        calls_dir = root / "se3" / "calls"
        if not calls_dir.is_dir():
            return []

        entries = [
            entry
            for entry in sorted(calls_dir.iterdir())
            if entry.is_file() and not entry.name.startswith(".")
        ]

        # Collect the base names of calls that already have a response file.
        answered: Set[str] = set()
        for entry in entries:
            name = entry.name
            if name.endswith(".response.json"):
                answered.add(name[: -len(".response.json")])
            elif name.endswith(".response"):
                answered.add(name[: -len(".response")])

        calls: List[PendingCall] = []
        for entry in entries:
            name = entry.name
            # Response files are answers, not pending calls — skip them.
            if name.endswith(".response.json") or name.endswith(".response"):
                continue
            # Skip calls that already have a sibling response file.
            if entry.stem in answered:
                continue
            calls.append(
                PendingCall(
                    call_id=entry.stem,
                    path=str(entry),
                    project_root=str(root),
                    kind="call",
                    created_at=_safe_mtime(entry) or 0.0,
                )
            )
        return calls

    @staticmethod
    def _read_summary(state_dir: Path, flow_id: Optional[str]) -> Optional[str]:
        """Return the most recent ``summary-*.json`` summary text, if any."""
        if not state_dir.is_dir():
            return None
        candidates = sorted(
            state_dir.glob("summary-*.json"),
            key=lambda p: _safe_mtime(p) or 0.0,
            reverse=True,
        )
        for cand in candidates:
            data = _read_json(cand)
            if data is None:
                continue
            summary = data.get("summary") or data.get("text")
            if summary:
                return str(summary)
        return None


# -- module-level file helpers --------------------------------------------


def _safe_mtime(path: Path) -> Optional[float]:
    """Return *path*'s mtime, or ``None`` if it does not exist / is unreadable."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _read_json(path: Path) -> Optional[dict]:
    """Read and parse a JSON file; return ``None`` on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _current_step(state: dict) -> Optional[str]:
    """Resolve a human-readable current-step label from a flow ``state`` dict."""
    step_id = state.get("current_step_id")
    steps = state.get("steps") or {}
    if step_id and isinstance(steps, dict):
        step = steps.get(step_id)
        if isinstance(step, dict):
            return str(step.get("step_type") or step_id)
    return str(step_id) if step_id else None


def _count_dir(path: Path) -> int:
    """Count regular files anywhere under *path* (0 if it does not exist)."""
    if not path.is_dir():
        return 0
    count = 0
    for _root, _dirs, files in os.walk(path):
        count += len(files)
    return count


def _count_issues(issues_dir: Path) -> int:
    """Count open issue records under ``se3/issues/`` (``open/`` subtree)."""
    if not issues_dir.is_dir():
        return 0
    open_dir = issues_dir / "open"
    target = open_dir if open_dir.is_dir() else issues_dir
    return sum(1 for p in target.glob("*.yaml") if p.is_file())
