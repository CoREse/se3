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
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Set

from . import protocol
from .history import enumerate_historical_project_roots
from .supervisor import is_worktree_copy_root, resolve_worktree_main_root


logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 2.0

# TTL (seconds) for the cached on-disk *historical* project-root enumeration in
# :meth:`DaemonAggregator.all_project_roots`. The history enumeration walks the
# whole ``se3/history`` tree (reading every ``_meta.json``) and is far too
# expensive to repeat on every status tick once a project has accumulated
# history. Caching it behind a conservative TTL collapses that per-tick full
# scan to at most one walk per window. Only the *historical* discovery is
# throttled: the active base (``_project_roots`` ∪ registry) is always merged
# fresh, so a newly active / registered root is still visible immediately. A
# conservative value keeps newly-appearing *pure history* roots visible within
# a window without scanning every tick.
HISTORICAL_ROOTS_TTL = 60.0

# Call kinds that are *exempt* from the ``status == "failed"`` staleness rule
# applied by :meth:`DaemonAggregator._filter_stale_calls`. A call of one of
# these kinds keyed to the flow's current step stays pending even when that
# step is itself in the FAILED state — it exists *because* the step failed and
# is the operator's decision channel for what to do about it. Without the
# exemption the chip would be filtered out the instant the flow paused, hiding
# the very interaction the human needs to answer.
#
# The exemption is keyed on call *kind* (not step type): call kind is the
# semantic identity of the interaction, retry_decision exists only on a FAILED
# step by construction, and routing it this way keeps step / kind decoupled so
# a future decision-class kind (``partial_decision`` etc.) joins this set
# without re-touching the filter body.
_FAILED_EXEMPT_CALL_KINDS: FrozenSet[str] = frozenset(
    {protocol.CALL_KIND_RETRY_DECISION}
)


@dataclass
class PendingCall:
    """A queued interaction awaiting a human response.

    Mirrors a file under a project's ``se3/calls/`` directory — the unified
    carrier for every interaction that needs a human in the loop while a flow
    runs. The file's ``kind`` field (one of
    :data:`~se3.daemon.protocol.CALL_KINDS`) tells the UI how to render it:
    a pending MCP call, a mid-flow interjection request, a retry / failure
    decision, a CLI subprocess confirmation prompt, or a discovery
    confirmation gate (carrying ``options`` for a one-click confirm button).

    The display fields (``prompt`` / ``context`` / ``options`` / ``step_id``)
    are read straight from the call file's JSON body when present; a legacy
    call file with no metadata reports ``kind="call"`` and leaves them empty.
    """

    call_id: str
    path: str
    project_root: str
    kind: str = protocol.CALL_KIND_CALL
    created_at: float = 0.0
    prompt: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    options: List[Any] = field(default_factory=list)
    step_id: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "call_id": self.call_id,
            "path": self.path,
            "project_root": self.project_root,
            "kind": self.kind,
            "created_at": self.created_at,
            "prompt": self.prompt,
            "context": self.context,
            "options": self.options,
            "step_id": self.step_id,
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
    # Running sub-state: True while a synchronous run is blocked acquiring the
    # project's main-worktree mutex before its first code-touching step. Read
    # from engine.json's top-level ``waiting_for_lock`` flag (only ever present
    # and True for a queued synchronous run); surfaced so the web console shows
    # the flow as RUNNING·waiting-for-lock rather than a silent "已发布" stall.
    waiting_for_lock: bool = False
    # Authoritative "can this flow be resumed" signal computed by the daemon
    # from the flow's semantic status (see :func:`_is_resumable_status`): True
    # for any flow that has NOT completed normally and still has recoverable
    # state. Set on a non-completed active engine.json flow and on every per-flow
    # ``se3/state/resumable/<flow_id>.json`` snapshot that has been superseded by
    # a later run (status preserved, ``resumable=True``). The server / frontend
    # read this flag as the primary resume-eligibility signal, falling back to
    # the legacy status/source heuristic only when it is absent/false.
    resumable: bool = False

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
            "waiting_for_lock": self.waiting_for_lock,
            "resumable": self.resumable,
        }


@dataclass
class IssueSnapshot:
    """A single issue record for inclusion in :class:`MachineStatus`.

    Carries every webui-relevant field so the frontend can render, filter and
    operate on issues without a second round-trip to the daemon.
    """

    id: str
    project_root: str
    title: Optional[str] = None
    description: str = ""
    status: str = "open"
    priority: Optional[str] = None
    type: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    source: str = "system"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, object]:
        data: Dict[str, object] = {
            "id": self.id,
            "project_root": self.project_root,
            "description": self.description,
            "status": self.status,
            "tags": list(self.tags),
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.title is not None:
            data["title"] = self.title
        if self.priority is not None:
            data["priority"] = self.priority
        if self.type is not None:
            data["type"] = self.type
        return data


@dataclass
class MachineStatus:
    """A full status snapshot of one SE3 machine."""

    machine_id: str
    hostname: str
    flows: List[FlowSnapshot] = field(default_factory=list)
    pending_calls: List[PendingCall] = field(default_factory=list)
    project_roots: List[str] = field(default_factory=list)
    issues: List[IssueSnapshot] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, object]:
        return {
            "machine_id": self.machine_id,
            "hostname": self.hostname,
            "flows": [f.to_dict() for f in self.flows],
            "pending_calls": [c.to_dict() for c in self.pending_calls],
            "project_roots": list(self.project_roots),
            "issues": [i.to_dict() for i in self.issues],
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
        registry_load: Optional[Callable[[], Iterable[str]]] = None,
        registry_persist: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Create the aggregator.

        Args:
            machine_id: Stable machine id; auto-derived when ``None``.
            poll_interval: Seconds between aggregation polls.
            registry_load: Optional zero-arg callable returning the persisted
                project roots (a machine-local registry of every root that has
                run a flow through this daemon). When ``None`` (the default) the
                aggregator keeps its legacy in-memory-only behavior.
            registry_persist: Optional single-arg callable that durably records
                one project root. Wired together with *registry_load* it makes
                :meth:`add_project_root` write through to disk so the history
                index and New Task dropdown survive a daemon with no live flow.
        """
        self.machine_id = machine_id or _stable_machine_id()
        self.hostname = socket.gethostname()
        self.poll_interval = max(0.1, float(poll_interval))
        self._project_roots: Set[Path] = set()
        self._registry_load = registry_load
        self._registry_persist = registry_persist
        # engine.json mtime per project root, for change detection.
        self._mtimes: Dict[str, float] = {}
        # TTL cache for the expensive on-disk historical-root enumeration used by
        # ``all_project_roots``. ``_hist_roots_cache`` holds the last enumeration
        # result, ``_hist_roots_at`` the monotonic timestamp of that enumeration,
        # and ``_hist_roots_base`` the base fingerprint (frozenset of active ∪
        # registry roots) it was computed from. The cache is reused only while
        # the TTL has not elapsed *and* the base fingerprint is unchanged; it is
        # also invalidated eagerly by ``add_project_root``.
        self._hist_roots_cache: Optional[List[str]] = None
        self._hist_roots_at: Optional[float] = None
        self._hist_roots_base: Optional[FrozenSet[str]] = None

    # -- project-root registry --------------------------------------------

    def add_project_root(self, path: object) -> None:
        """Register a project root whose ``engine.json`` should be polled.

        Besides joining the in-memory active set, the resolved root is written
        through to the persistent registry (when a ``registry_persist`` callback
        was supplied), so a root that ran a flow stays known across restarts and
        when no ``se3 run`` process is currently live. Persistence is
        best-effort: a failing callback is logged, never propagated, so a
        registry I/O hiccup can't break aggregation.

        This is the single write-through seam for the displayed project-root
        set (both the in-memory active set and the persistent registry), so the
        worktree→main normalization is applied here once: an
        ``<main>/se3/worktrees/<name>`` isolation sandbox is folded back to its
        owning ``<main>`` before it can enter either path. Every registration
        entry point (``__init__`` / ``request_spawn`` / ``request_resume`` /
        ``_handle_ensure_request`` / ``_resume_paused_flow``, plus the poll
        loop) routes through here, so none can leak a worktree copy into the
        WebUI project list / New Task dropdown. A non-worktree path
        (``resolve_worktree_main_root`` returns ``None``) is registered
        unchanged.
        """
        main_root = resolve_worktree_main_root(path)
        resolved = Path(main_root if main_root is not None else path).resolve()
        # Only a *genuinely new* root needs to bust the historical-roots cache.
        # The daemon poll loop re-adds every active flow's already-known root on
        # every ~2s tick; invalidating unconditionally there would re-run the
        # full ``se3/history`` walk every tick (the exact high-frequency disk
        # scan this cache exists to eliminate). When the root is already tracked,
        # the active base is unchanged, so the base-fingerprint guard in
        # ``_historical_roots`` already keeps the warm cache valid — leave it be.
        is_new = resolved not in self._project_roots
        self._project_roots.add(resolved)
        if is_new:
            # A new active root must be discoverable on the very next
            # ``all_project_roots`` call, not after the TTL — invalidate the
            # cached historical enumeration so "register and it's immediately
            # visible" holds even mid-window.
            self._invalidate_hist_roots_cache()
        if self._registry_persist is not None:
            try:
                self._registry_persist(str(resolved))
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "aggregator: failed to persist project root %s", resolved
                )

    def remove_project_root(self, path: object) -> None:
        """Stop polling *path*."""
        self._project_roots.discard(Path(path).resolve())
        self._invalidate_hist_roots_cache()

    def set_project_roots(self, paths: object) -> None:
        """Replace the polled project-root set with *paths*."""
        self._project_roots = {Path(p).resolve() for p in paths}
        self._invalidate_hist_roots_cache()

    def _invalidate_hist_roots_cache(self) -> None:
        """Drop the cached historical-root enumeration.

        Forces the next :meth:`all_project_roots` to re-run the on-disk history
        walk. Called whenever the active root set changes so a freshly
        registered / removed root is reflected without waiting out the TTL.
        """
        self._hist_roots_cache = None
        self._hist_roots_at = None
        self._hist_roots_base = None

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
        """Build and return the current :class:`MachineStatus` snapshot.

        ``FlowSnapshot.pending_calls`` is scoped to its own flow (via
        :meth:`_filter_calls_for_flow`) to prevent cross-session leakage in
        the per-flow web view, but the machine-wide ``pending_calls`` field
        aggregates *all* call files under each project root unfiltered, so the
        machine-level surface keeps showing every queued interaction.
        """
        flows: List[FlowSnapshot] = []
        all_calls: List[PendingCall] = []
        all_issues: List[IssueSnapshot] = []
        # Build flow snapshots and collect calls from ALL known project roots
        # (active + registry + historical), not just the active set.  After a
        # daemon restart with no live ``se3 run`` process, a FAILED/PAUSED
        # flow's project root exists only in the persistent registry; building
        # snapshots only from the active set would leave it invisible in
        # MachineStatus.flows, making the webui resume button (which the
        # history index correctly renders) resolve to a 404 because
        # ``record.flows`` in ServerState has no entry for that flow_id.
        # ``_snapshot_for_root`` is cheap for roots without an engine.json
        # (a single failed file read that returns None), so the incremental
        # cost of scanning registry roots is negligible.
        #
        # ``all_observable_roots`` additionally folds in any active
        # ``se3 run --worktree`` subdirectory so an isolation run gets a live
        # flow card / conversation for its whole flow body, not only after its
        # trailing merge. The dropdown-facing ``project_roots`` field stays on
        # ``all_project_roots`` (via ``_merge_project_roots``), so a transient
        # worktree sandbox never appears as a New Task target.
        observable_roots = self.all_observable_roots()
        # Track the flow_ids carried by the *active* engine.json snapshots so a
        # resumable per-flow snapshot for the same flow is de-duplicated (the
        # active engine.json copy wins) in the supplement pass below.
        active_flow_ids: Set[str] = set()
        for root_str in observable_roots:
            root = Path(root_str)
            snapshot = self._snapshot_for_root(root)
            if snapshot is not None:
                flows.append(snapshot)
                if snapshot.flow_id:
                    active_flow_ids.add(snapshot.flow_id)
            # Calls and issues are collected independently of whether an
            # active engine.json exists.  After a flow completes and
            # engine.json is archived, the project root still holds its
            # issue YAML files under se3/issues/; skipping collection
            # when _snapshot_for_root() returns None would make those
            # issues invisible in the webui.
            all_calls.extend(self._enumerate_calls(root))
            all_issues.extend(self._collect_issues(root))
        # Supplement: per-flow resumable snapshots that no longer have a live
        # engine.json entry. A paused/interrupted/failed flow writes
        # ``se3/state/resumable/<flow_id>.json`` (see PersistenceManager); a
        # later ``se3 run`` overwrites the single-slot engine.json but leaves
        # that snapshot intact. Without this pass such a flow would be invisible
        # in ``MachineStatus.flows``, so a webui resume click would 404 because
        # ServerState.record.flows has no entry for it. Run this *after* the
        # active pass so ``active_flow_ids`` is fully populated and an active
        # flow always wins over its own (still-present) resumable snapshot.
        seen_resumable: Set[str] = set(active_flow_ids)
        for root_str in observable_roots:
            root = Path(root_str)
            flows.extend(
                self._enumerate_resumable_snapshots(root, seen_resumable)
            )
        return MachineStatus(
            machine_id=self.machine_id,
            hostname=self.hostname,
            flows=flows,
            pending_calls=all_calls,
            project_roots=self._merge_project_roots(),
            issues=all_issues,
        )

    def all_project_roots(self) -> List[str]:
        """Return the union view used for reporting and the history index.

        This is the single source of truth for both the ``project_roots`` field
        of the :class:`MachineStatus` snapshot (which drives the web *New Task*
        project dropdown) and the history index's root provider (which drives
        the history list). Three sources feed it, deduplicated by resolved
        absolute path and returned sorted:

        * the currently registered active roots (``self._project_roots`` —
          supervisor / spawner / config-seeded entries discovered this process
          lifetime);
        * the persistent registry roots (``registry_load`` — every root that
          has ever run a flow through this daemon, surviving restarts and
          zero-live-process periods);
        * the disk-history roots discovered by
          :func:`~se3.daemon.history.enumerate_historical_project_roots`, fed
          the *union* of the two sets above so a registry root with on-disk
          history (but no live flow) is still scanned for its artifacts.

        Crucially this view is **not** what per-flow polling iterates: that loop
        stays on ``self._project_roots`` (the active set) so adding many
        historical roots here never widens the per-tick snapshot work.

        The active base (active ∪ registry roots) is *always* recomputed fresh
        so a newly active / registered root appears immediately. Only the
        expensive on-disk *historical* enumeration is throttled behind a TTL +
        base-fingerprint cache (see :data:`HISTORICAL_ROOTS_TTL`): repeating its
        full ``se3/history`` walk on every status tick is what previously stalled
        the event loop. Iteration over ``self._project_roots`` is snapshotted
        into a local set up front so a concurrent ``add_project_root`` (the
        snapshot build runs in a worker thread) cannot raise ``RuntimeError``.
        """
        base: Set[str] = set()
        # Local snapshot of the live set first: ``all_project_roots`` may run in
        # a worker thread (offloaded snapshot build) while the event loop calls
        # ``add_project_root``, so iterate a copy, never the live set.
        for path in list(self._project_roots):
            try:
                base.add(os.path.realpath(str(path)))
            except OSError:  # pragma: no cover - defensive
                base.add(str(path))
        if self._registry_load is not None:
            try:
                for entry in self._registry_load() or []:
                    if not entry:
                        continue
                    try:
                        base.add(os.path.realpath(str(entry)))
                    except OSError:  # pragma: no cover - defensive
                        base.add(str(entry))
            except Exception:  # pragma: no cover - defensive
                logger.exception("aggregator: registry_load failed")

        historical = self._historical_roots(base)

        merged: Set[str] = set(base)
        merged.update(historical)
        return sorted(merged)

    def all_observable_roots(self) -> List[str]:
        """:meth:`all_project_roots` plus active ``--worktree`` run subdirectories.

        This is the root view used for building flow snapshots
        (:meth:`get_snapshot`) and for the history reader's enumeration, so a
        ``se3 run --worktree`` flow is observable in the WebUI — flow card,
        status, and live conversation — for its whole flow body, exactly like a
        synchronous run rather than only after its trailing merge syncs history
        back into the main repo.

        It deliberately does NOT feed the snapshot's ``project_roots`` field
        (the New Task project dropdown), which stays on :meth:`all_project_roots`
        via :meth:`_merge_project_roots`: an isolation worktree is a transient
        execution sandbox, not a project the user should start fresh tasks
        against.
        """
        roots: Set[str] = set(self.all_project_roots())
        roots.update(self._active_worktree_run_roots())
        return sorted(roots)

    def _active_worktree_run_roots(self) -> List[str]:
        """Discover ``se3 run --worktree`` isolation subdirectories to observe.

        A ``--worktree`` run executes its entire flow body inside
        ``<main_repo>/se3/worktrees/<name>/``, persisting its own
        ``se3/state/engine.json`` and ``se3/history/<flow_id>/`` there — never
        in the main repo until the trailing merge syncs history back. The daemon
        registers only the *main* repo as a project root, so without this
        discovery the snapshot and history reader never see a worktree run while
        it executes and the WebUI stays blank (or shows a stale main-repo flow)
        for the whole flow body.

        This scans every tracked main root's ``se3/worktrees/*/`` for a child
        whose ``engine.json`` describes an ``is_worktree_mode`` flow and returns
        those child paths. The strict ``is_worktree_mode`` gate keeps the
        implement step's internal DAG isolation worktrees — which share the same
        ``se3/worktrees/`` parent directory but never write a top-level
        ``is_worktree_mode`` flow record — out of the observed set.

        The scan covers the active root set (snapshotted up front so a
        concurrent :meth:`add_project_root` cannot raise) unioned with the
        persistent registry, mirroring the ``base`` of :meth:`all_project_roots`
        so a failed worktree run stays observable across a daemon restart.
        """
        bases: Set[str] = {str(path) for path in list(self._project_roots)}
        if self._registry_load is not None:
            try:
                for entry in self._registry_load() or []:
                    if entry:
                        bases.add(str(entry))
            except Exception:  # pragma: no cover - defensive
                logger.exception("aggregator: registry_load failed (worktree scan)")

        roots: List[str] = []
        seen: Set[str] = set()
        for base in bases:
            worktrees_dir = Path(base) / "se3" / "worktrees"
            try:
                entries = sorted(worktrees_dir.iterdir())
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:  # pragma: no cover - racy unlink
                    continue
                data = _read_json(entry / "se3" / "state" / "engine.json")
                if not isinstance(data, dict):
                    continue
                if not data.get("flow_id") or not data.get("is_worktree_mode"):
                    continue
                try:
                    resolved = os.path.realpath(str(entry))
                except OSError:  # pragma: no cover - defensive
                    resolved = str(entry)
                if resolved not in seen:
                    seen.add(resolved)
                    roots.append(resolved)
        return roots

    def _historical_roots(self, base: Set[str]) -> List[str]:
        """Return the on-disk historical roots for *base*, TTL-cached.

        Reuses the previous enumeration when it is still within
        :data:`HISTORICAL_ROOTS_TTL` *and* the *base* fingerprint is unchanged;
        otherwise it re-runs :func:`enumerate_historical_project_roots` and
        refreshes the cache. This is the single throttle point that keeps the
        full ``se3/history`` walk off the per-tick hot path.
        """
        base_fp = frozenset(base)
        now = time.monotonic()
        # Capture the three cache fields into locals once. This method runs in a
        # worker thread (the snapshot build is offloaded via asyncio.to_thread)
        # while the event loop may concurrently call ``_invalidate_hist_roots_cache``
        # and null all three fields. Reading the instance attributes repeatedly
        # across the guard could let one pass the ``is not None`` check and then
        # be nulled before ``now - at`` / the return, yielding ``None`` (or a
        # ``TypeError`` on ``now - None``) — which would bubble up as
        # ``merged.update(None)`` and drop the STATUS_UPDATE. A single read of
        # each field makes the guard atomic with respect to the use.
        cache = self._hist_roots_cache
        cached_at = self._hist_roots_at
        cached_base = self._hist_roots_base
        if (
            cache is not None
            and cached_at is not None
            and cached_base == base_fp
            and (now - cached_at) < HISTORICAL_ROOTS_TTL
        ):
            return cache
        result: List[str] = []
        try:
            result = list(enumerate_historical_project_roots(base))
        except Exception:  # pragma: no cover - defensive
            logger.exception("aggregator: enumerate_historical_project_roots failed")
        self._hist_roots_cache = result
        self._hist_roots_at = now
        self._hist_roots_base = base_fp
        return result

    def pending_calls_signature(self) -> Dict[str, Any]:
        """Return a cheap stat-based fingerprint of every ``se3/calls/`` file.

        For each registered project root, the fingerprint lists every regular
        file under ``se3/calls/`` as ``(name, mtime, size)``. Listing the
        directory and stat-ing each entry is dramatically cheaper than the
        full :meth:`get_snapshot` pass (no JSON parsing, no per-flow
        decoration), so the daemon client can call this on a tight cadence
        and only run the heavy snapshot when the signature actually moves.

        The signature is intentionally **kind-agnostic** — it changes for an
        interjection file *as well as* a retry-decision / cli-confirm /
        discovery-confirm file appearing or disappearing. Reading the JSON
        body of every file to filter on ``kind`` would defeat the "cheap
        polling" point; the actual interjection-vs-other classification
        happens in the server's WebSocket diff layer, which already inspects
        the full ``pending_calls`` list.

        Pairing mtime with byte size mirrors :func:`_safe_stat`'s reasoning
        in the history reader: two writes inside the filesystem's mtime
        resolution still flip the signature via the size delta. A directory
        that cannot be listed is skipped silently — it shows up as "absent",
        which is the same as "no pending calls", and the next successful
        scan picks the change up correctly.
        """
        signature: Dict[str, Any] = {}
        # Scan the active roots plus any active ``--worktree`` run subdir: an
        # isolation run writes its human-call files (e.g. a discovery
        # clarification) under ``<worktree>/se3/calls/``, so without the worktree
        # roots here the fast (~1 s) call-change push would miss them and the
        # chip would only surface on the slower full status tick.
        roots = [str(r) for r in self._project_roots]
        roots.extend(self._active_worktree_run_roots())
        seen_dirs: Set[str] = set()
        for root_str in roots:
            if root_str in seen_dirs:
                continue
            seen_dirs.add(root_str)
            root = Path(root_str)
            calls_dir = root / "se3" / "calls"
            try:
                entries = sorted(calls_dir.iterdir())
            except OSError:
                continue
            parts: List[Any] = []
            for entry in entries:
                name = entry.name
                if name.startswith("."):
                    continue
                try:
                    if not entry.is_file():
                        continue
                except OSError:  # pragma: no cover - racy unlink
                    continue
                mtime, size = _safe_stat(entry)
                parts.append((name, mtime, size))
            signature[str(root)] = tuple(parts)
        return signature

    def _merge_project_roots(self) -> List[str]:
        """Produce the snapshot's ``project_roots`` field.

        Delegates to :meth:`all_project_roots` so the machine snapshot, the
        history index and the New Task dropdown all share one root-union source.
        ``self._project_roots`` itself remains a mutable :class:`set` — spawn
        paths continue to ``add()`` directly into it; this helper is a pure read
        of the merged view.
        """
        return self.all_project_roots()

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
            # No engine.json: no current flow_id to filter by, but historical
            # call files may still be lingering. Pass ``None`` to keep the
            # retro-compatible behavior (no filtering when flow_id unknown).
            flow_scoped_calls = self._filter_calls_for_flow(pending_calls, None)
            if not flow_scoped_calls and log_count == 0 and issue_count == 0:
                return None
            return FlowSnapshot(
                project_root=str(root),
                pending_calls=flow_scoped_calls,
                log_count=log_count,
                issue_count=issue_count,
            )

        state = data.get("state") or {}
        selected = state.get("selected_steps") or []
        total = len(selected)
        index = int(state.get("current_step_index") or 0)
        progress = (index / total) if total else 0.0

        flow_id = data.get("flow_id")
        flow_id_str = str(flow_id) if flow_id else None
        flow_calls = self._filter_calls_for_flow(pending_calls, flow_id_str)
        flow_calls = self._filter_stale_calls(flow_calls, state)
        flow_calls = self._dedup_calls_by_step(flow_calls)
        status = str(data.get("status") or "unknown")
        return FlowSnapshot(
            project_root=str(root),
            flow_id=flow_id_str,
            task_description=str(data.get("task_description") or ""),
            task_type=str(data.get("task_type") or ""),
            status=status,
            current_step=_current_step(state),
            current_step_index=index,
            total_steps=total,
            progress=round(progress, 4),
            updated_at=data.get("updated_at"),
            pending_calls=flow_calls,
            log_count=log_count,
            issue_count=issue_count,
            summary=self._read_summary(state_dir, flow_id_str),
            # Surface the lock-wait sub-state; absent/false for every flow not
            # currently queued behind the main-worktree mutex.
            waiting_for_lock=bool(data.get("waiting_for_lock", False)),
            # A still-active flow that has not completed normally is resumable
            # (covers the interrupted-but-still-current-engine.json case, where
            # status may be running/paused/failed). A COMPLETED active flow that
            # has not yet been archived is not resumable.
            resumable=_is_resumable_status(status),
        )

    def _enumerate_resumable_snapshots(
        self, root: Path, seen_flow_ids: Set[str]
    ) -> List[FlowSnapshot]:
        """Build supplemental :class:`FlowSnapshot`s for resumable snapshots.

        Enumerates ``<root>/se3/state/resumable/*.json`` — the per-flow
        snapshots PersistenceManager writes for every flow that has NOT
        completed normally — and emits one ``resumable=True`` FlowSnapshot per
        snapshot whose ``flow_id`` is not already represented by an active
        engine.json flow (tracked in *seen_flow_ids*, mutated in place so the
        same flow is never emitted twice across roots).

        Each emitted snapshot preserves the flow's *original* status
        (running / paused / failed) so the webui can show why it stalled while
        still offering a resume entry. A normally COMPLETED flow has no snapshot
        here (it is cleared on completion); should a stale ``completed``
        snapshot survive — e.g. ``save_flow``'s best-effort
        ``clear_resumable_snapshot`` failed, or an operator/test artifact
        remains — it is skipped here rather than surfaced as resumable, so the
        aggregator agrees with the daemon resume validator (which rejects a
        COMPLETED flow) and the webui never shows a dead resume entry.
        """
        resumable_dir = root / "se3" / "state" / "resumable"
        if not resumable_dir.is_dir():
            return []
        try:
            snapshot_files = sorted(resumable_dir.glob("*.json"))
        except OSError:
            return []
        results: List[FlowSnapshot] = []
        for snap_file in snapshot_files:
            data = _read_json(snap_file)
            if not isinstance(data, dict):
                continue
            flow_id = data.get("flow_id")
            flow_id_str = str(flow_id) if flow_id else None
            if not flow_id_str or flow_id_str in seen_flow_ids:
                continue
            # The embedded flow_id MUST match the snapshot filename
            # (resumable/<flow_id>.json); otherwise the load/resume path
            # (PersistenceManager.load_resumable_snapshot, keyed by filename)
            # would reject it, so advertising it here offers a resume entry
            # that can never actually resume. Skip mismatched/misnamed files.
            if flow_id_str != snap_file.stem:
                continue
            # A stale completed snapshot must never be surfaced as resumable;
            # ignore it entirely (do not claim the flow_id) so it cannot mask a
            # genuinely resumable source elsewhere.
            if not _is_resumable_status(str(data.get("status") or "")):
                continue
            seen_flow_ids.add(flow_id_str)
            results.append(self._snapshot_from_resumable(root, data))
        return results

    @staticmethod
    def _snapshot_from_resumable(root: Path, data: Dict[str, Any]) -> FlowSnapshot:
        """Build a ``resumable=True`` FlowSnapshot from a resumable snapshot dict.

        The dict has the same shape as ``engine.json`` (it is
        ``FlowInstance.to_dict()``), so progress / step metadata is derived the
        same way as :meth:`_snapshot_for_root`. ``pending_calls`` / ``log_count``
        / ``issue_count`` / ``summary`` are intentionally left empty: those
        belong to the *live* flow that currently owns the project root's
        ``se3/calls`` & ``se3/issues``, not to this superseded snapshot.

        ``resumable`` is derived from the snapshot's own status via
        :func:`_is_resumable_status` (rather than hard-coded ``True``) so a
        stale ``completed`` snapshot is never advertised as resumable; callers
        that build these from a resumable directory already pre-filter
        completed snapshots, but deriving it here keeps the flag honest at the
        single source of truth.
        """
        state = data.get("state") or {}
        selected = state.get("selected_steps") or []
        total = len(selected)
        index = int(state.get("current_step_index") or 0)
        progress = (index / total) if total else 0.0
        flow_id = data.get("flow_id")
        flow_id_str = str(flow_id) if flow_id else None
        status = str(data.get("status") or "unknown")
        return FlowSnapshot(
            project_root=str(root),
            flow_id=flow_id_str,
            task_description=str(data.get("task_description") or ""),
            task_type=str(data.get("task_type") or ""),
            status=status,
            current_step=_current_step(state),
            current_step_index=index,
            total_steps=total,
            progress=round(progress, 4),
            updated_at=data.get("updated_at"),
            pending_calls=[],
            log_count=0,
            issue_count=0,
            summary=None,
            waiting_for_lock=False,
            resumable=_is_resumable_status(status),
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
            calls.append(self._parse_call_file(entry, root))
        return calls

    @staticmethod
    def _filter_calls_for_flow(
        calls: List[PendingCall], flow_id: Optional[str]
    ) -> List[PendingCall]:
        """Filter ``calls`` to those belonging to *flow_id*.

        A call belongs to *flow_id* when its ``context.flow_id`` matches
        exactly. Calls whose ``context.flow_id`` is missing or empty are
        treated as *unattributed* and are dropped — these are typically
        artifacts of other flows / scenarios (``merge_<branch>_*.json``,
        ``sync_conflicts_*.json``, …) that linger in ``se3/calls/`` and must
        not bleed into the current flow's reply chip-bar.

        When ``flow_id`` itself is ``None`` or empty, no filtering is applied
        — there is no current flow to scope against, and callers see the
        unfiltered list (used by the project-root snapshot when no
        ``engine.json`` exists yet).
        """
        if not flow_id:
            return list(calls)
        result: List[PendingCall] = []
        for call in calls:
            ctx = call.context if isinstance(call.context, dict) else {}
            call_flow_id = ctx.get("flow_id")
            if call_flow_id is None or call_flow_id == "":
                # Unattributed call — drop, do not leak into this flow.
                continue
            if str(call_flow_id) == flow_id:
                result.append(call)
        return result

    @staticmethod
    def _filter_stale_calls(
        calls: List[PendingCall], state: Dict[str, Any]
    ) -> List[PendingCall]:
        """Drop calls whose owning step the flow has already moved past.

        The response-file heuristic in :meth:`_enumerate_calls` only clears a
        call once a sibling ``.response`` file appears. Interactive confirm /
        human calls answered in the CLI terminal never get such a sibling: the
        ``se3 run`` loop consumes the terminal answer directly and advances. So
        those call files linger for the whole run and, without this filter, the
        web console would keep showing a stale "待回复" chip even though the
        flow has long since moved on.

        A call is judged **stale** when its owning step is resolvable in the
        flow ``state`` and the flow has already passed it — either the step is
        no longer ``current_step_id`` or the step itself reached a processed
        status (``completed`` / ``partial`` / ``failed`` / ``revision_needed``).
        Such calls are dropped.

        A call whose ``kind`` is in :data:`_FAILED_EXEMPT_CALL_KINDS`
        (currently the ``retry_decision`` kind) is judged against the
        processed set with ``"failed"`` removed: the retry-decision chip is
        precisely the FAILED-step decision channel and MUST stay visible while
        the step is in that state. The exemption is keyed on call kind rather
        than hard-coding ``retry_decision`` so a future decision-class kind
        joins the set without re-touching the filter body.

        A call whose step cannot be resolved (no ``step_id``, or a ``step_id``
        absent from ``state.steps``) is kept untouched, so a genuinely pending
        interaction is never lost to an over-eager progress heuristic.
        """
        if not isinstance(state, dict):
            return list(calls)
        steps = state.get("steps")
        if not isinstance(steps, dict):
            return list(calls)
        current_step_id = state.get("current_step_id")
        processed = {"completed", "partial", "failed", "revision_needed"}
        result: List[PendingCall] = []
        for call in calls:
            step_id = _call_step_id(call)
            if not step_id or step_id not in steps:
                # Unattributable to a step in this flow — keep, do not risk
                # dropping a real pending interaction.
                result.append(call)
                continue
            step = steps.get(step_id)
            status = ""
            if isinstance(step, dict):
                status = str(step.get("status") or "").lower()
            processed_for_call = (
                processed - {"failed"}
                if call.kind in _FAILED_EXEMPT_CALL_KINDS
                else processed
            )
            if step_id != current_step_id or status in processed_for_call:
                # The flow has walked past this step (or already finished it) —
                # the call is stale; do not surface it as pending.
                continue
            result.append(call)
        return result

    @staticmethod
    def _dedup_calls_by_step(calls: List[PendingCall]) -> List[PendingCall]:
        """Keep only the newest unanswered call per ``(flow_id, step_id)``.

        Discovery reuses the *same* ``step_id`` across multiple clarification
        rounds (the run loop pauses, is answered, re-runs the step, pauses
        again). When a CLI-terminal answer is consumed directly by the run loop
        without writing a sibling ``.response`` file, the old round's call file
        can linger while a new round writes a fresh one — leaving several call
        files all tagged with the same ``(flow_id, step_id)``. Only the most
        recent is a live interaction; the rest are superseded leftovers that
        would otherwise pile up as stale "待回复" chips.

        Newest-wins is decided by ``(created_at, call_id)`` — the call file's
        mtime, with the timestamp-bearing ``call_id`` as a stable tie-breaker.
        Calls that cannot be keyed (missing ``flow_id`` or ``step_id``) are
        passed through untouched, so unrelated interactions are never collapsed
        together. This composes after :meth:`_filter_calls_for_flow` and
        :meth:`_filter_stale_calls` without changing their behavior.
        """
        newest: Dict[tuple, PendingCall] = {}
        for call in calls:
            ctx = call.context if isinstance(call.context, dict) else {}
            flow_id = ctx.get("flow_id")
            step_id = _call_step_id(call)
            if not flow_id or not step_id:
                continue
            key = (str(flow_id), str(step_id))
            existing = newest.get(key)
            if existing is None or _call_sort_key(call) >= _call_sort_key(existing):
                newest[key] = call

        result: List[PendingCall] = []
        emitted: Set[tuple] = set()
        for call in calls:
            ctx = call.context if isinstance(call.context, dict) else {}
            flow_id = ctx.get("flow_id")
            step_id = _call_step_id(call)
            if not flow_id or not step_id:
                # Un-keyable — never deduplicated against other calls.
                result.append(call)
                continue
            key = (str(flow_id), str(step_id))
            if key in emitted:
                continue
            emitted.add(key)
            result.append(newest[key])
        return result

    @staticmethod
    def _parse_call_file(entry: Path, root: Path) -> PendingCall:
        """Build a :class:`PendingCall` from one ``se3/calls/`` file.

        Reads the file's JSON body to recover the display metadata
        (``kind`` / ``prompt`` / ``context`` / ``options`` / ``step_id``). A
        legacy call file that is not JSON, or carries no ``kind``, falls back
        to :data:`~se3.daemon.protocol.CALL_KIND_CALL` with empty display
        fields so old flows keep working unchanged.
        """
        call = PendingCall(
            call_id=entry.stem,
            path=str(entry),
            project_root=str(root),
            kind=protocol.CALL_KIND_CALL,
            created_at=_safe_mtime(entry) or 0.0,
        )
        data = _read_json(entry)
        if not isinstance(data, dict):
            return call

        kind = data.get("kind")
        if isinstance(kind, str) and kind in protocol.CALL_KINDS:
            call.kind = kind

        # ``prompt`` is the canonical display field; ``message`` and the legacy
        # discovery-call ``question`` field are accepted as fallbacks so an
        # older call file still surfaces a non-empty prompt in the chip panel.
        prompt = data.get("prompt") or data.get("message") or data.get("question")
        if isinstance(prompt, str):
            call.prompt = prompt

        context = data.get("context")
        if isinstance(context, dict):
            call.context = context

        # Legacy compatibility: producers that predate the `context.flow_id`
        # convention (e.g. confirm / discovery call files) record `flow_id` at
        # the top level of the payload. Fold that into `context["flow_id"]`
        # so the per-flow filter can attribute the call to its owning flow.
        top_level_flow_id = data.get("flow_id")
        if (
            isinstance(top_level_flow_id, str)
            and top_level_flow_id
            and not call.context.get("flow_id")
        ):
            call.context = {**call.context, "flow_id": top_level_flow_id}

        options = data.get("options")
        if isinstance(options, list):
            call.options = options

        step_id = data.get("step_id")
        if step_id is not None:
            call.step_id = str(step_id)
        return call

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

    @staticmethod
    def _collect_issues(root: Path) -> List[IssueSnapshot]:
        """Read issue YAML files from ``se3/issues/`` and return snapshots.

        Scans both ``open/`` and ``closed/`` subdirectories.  Malformed or
        unreadable files are silently skipped so a corrupt issue never breaks
        the status snapshot.

        An ``se3 run --worktree`` isolation directory clones the main project's
        ``se3/issues/`` into ``<main>/se3/worktrees/<name>/se3/issues/``.
        :meth:`all_observable_roots` includes those worktree roots so the flow
        gets a live card / conversation, but their issue copy MUST NOT be
        counted — otherwise every issue surfaces twice (once for the main root,
        once for the worktree copy) for the duration of the run. Worktree copy
        roots are therefore skipped here so only the main project's issues are
        aggregated.
        """
        if is_worktree_copy_root(str(root)):
            return []

        import yaml  # deferred — the core CLI path never calls this

        issues_dir = root / "se3" / "issues"
        if not issues_dir.is_dir():
            return []

        result: List[IssueSnapshot] = []
        for subdir in ("open", "closed"):
            target = issues_dir / subdir
            if not target.is_dir():
                continue
            for f in sorted(target.glob("*.yaml")):
                try:
                    content = f.read_text(encoding="utf-8")
                    data = yaml.safe_load(content)
                    if not data or not isinstance(data, dict):
                        continue
                    raw_id = data.get("id")
                    if raw_id is None:
                        continue
                    # Issue.from_dict tolerates empty/missing description
                    # (degrading to ""), so the read path no longer validates
                    # it.  Skip only files missing a valid id — description is
                    # allowed to be empty so the webui surface matches the CLI.
                    raw_desc = data.get("description")
                    # Normalize description: empty/None degrades to "", mirroring
                    # Issue.from_dict's read-tolerance.
                    result.append(
                        IssueSnapshot(
                            id=str(raw_id),
                            project_root=str(root),
                            title=data.get("title"),
                            description=str(data.get("description") or ""),
                            status=str(data.get("status") or "open"),
                            priority=data.get("priority"),
                            type=data.get("type"),
                            tags=list(data.get("tags") or []),
                            source=str(data.get("source") or "system"),
                            created_at=str(data.get("created_at") or ""),
                            updated_at=str(data.get("updated_at") or ""),
                        )
                    )
                except Exception:  # pragma: no cover — defensive
                    logger.debug(
                        "aggregator: skipping unreadable issue file %s", f,
                        exc_info=True,
                    )
        return result


# -- module-level file helpers --------------------------------------------


def _is_resumable_status(status: str) -> bool:
    """Return whether *status* names a resumable flow.

    Every flow that has NOT completed normally is resumable — running
    (interrupted), paused (awaiting input), failed (recoverable error) and the
    transient init/recovering states all qualify. Only ``completed`` is
    terminal-and-done, so it is the single non-resumable status. The comparison
    is case-insensitive because engine.json / the resumable snapshot store the
    FlowStatus value verbatim.
    """
    return status.strip().lower() != "completed"


def _safe_mtime(path: Path) -> Optional[float]:
    """Return *path*'s mtime, or ``None`` if it does not exist / is unreadable."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _safe_stat(path: Path) -> tuple:
    """Return *path*'s ``(mtime, size)``, or ``(0.0, 0)`` when unreadable.

    Pairing mtime with byte size makes a signature change on every append
    even when two writes land inside the filesystem's mtime resolution. The
    history reader uses the same idea — see ``se3.daemon.history._safe_stat``.
    """
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, 0)


def _read_json(path: Path) -> Optional[dict]:
    """Read and parse a JSON file; return ``None`` on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _call_step_id(call: PendingCall) -> Optional[str]:
    """Resolve the step a :class:`PendingCall` belongs to, or ``None``.

    Prefers the call's own ``step_id`` field, falling back to
    ``context.step_id`` (where confirm / discovery / retry-decision writers
    record it). Returns ``None`` when neither is present.
    """
    if call.step_id:
        return str(call.step_id)
    ctx = call.context if isinstance(call.context, dict) else {}
    sid = ctx.get("step_id")
    return str(sid) if sid else None


def _call_sort_key(call: PendingCall) -> tuple:
    """Recency key for a :class:`PendingCall` — newer sorts greater.

    Primary key is the call file's mtime (``created_at``); the ``call_id`` is a
    stable tie-breaker (discovery call ids embed a high-resolution timestamp, so
    string order matches creation order when two files share an mtime).
    """
    return (call.created_at or 0.0, call.call_id or "")


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
