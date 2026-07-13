"""In-memory aggregated state for the SE3 central server.

:class:`ServerState` holds the live picture of every SE3 machine whose daemon
has dialed in: the machine's identity, connection liveness, and the set of
flows it is running. It is the server-side mirror of the per-machine snapshots
that daemons push as ``STATUS_UPDATE`` messages.

The store is intentionally **not persisted** — this delivery deliberately
scopes out a database. All state lives in process memory and is rebuilt as
daemons reconnect and re-push their snapshots. Access is guarded by an
``asyncio.Lock`` so the WebSocket handler and the REST handlers (all running
on the same event loop) never observe a half-applied update.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from se3.daemon import protocol

logger = logging.getLogger(__name__)


def _is_worktree_session_path(project_root: object) -> bool:
    """Return whether *project_root* is an se3 ``--worktree`` isolation dir.

    A ``se3 run --worktree`` flow body executes inside, and persists its
    ``engine.json`` under, ``<main_root>/se3/worktrees/<name>/``. The daemon
    reports such a live (possibly dangling) run with that path as its
    ``project_root``. This is the **structural** half of the daemon's
    :func:`se3.daemon.supervisor.resolve_worktree_main_root` check — the last
    two path segments being ``se3/worktrees`` — without the filesystem
    ``<main>/se3`` directory probe, because the server runs on a different host
    than the worktree and cannot stat it. Used by :meth:`ServerState.is_flow_endable`
    so a *completed* worktree session whose follow-up cleanup failed is still
    recognised as having an orphan worktree to archive.
    """
    if not project_root:
        return False
    # Split on both separators and drop empties so trailing slashes don't shift
    # the segment positions; a worktree dir is ``…/se3/worktrees/<name>``.
    parts = [seg for seg in str(project_root).replace("\\", "/").split("/") if seg]
    if len(parts) < 3:
        return False
    return parts[-2] == "worktrees" and parts[-3] == "se3"


# -- history progress token --------------------------------------------------
#
# The REST snapshot endpoint (``GET /api/history/{flow_id}``) can serve an
# incremental *delta* to a reconnecting client instead of the full record list.
# To do so safely the client echoes back an **opaque progress token** describing
# how far it had already consumed the server's in-memory history bundle. The
# token binds three facts:
#
#   * ``generation`` — the cache bundle's lifecycle id. It changes whenever the
#     bundle is replaced (a ``full`` push, a first sighting, or a machine
#     change) and stays stable across ordinary ``append`` pushes, so a token
#     issued before an append still validates while a token issued against a
#     since-replaced bundle is rejected.
#   * ``offset`` — how many records the client already holds (the index into
#     the bundle's flat ``records`` array).
#   * ``machine_id`` — the machine whose daemon produced the bundle, so a
#     bundle that has been re-pulled from a different daemon invalidates the
#     token.
#
# The token is deliberately content-free: it carries no record bodies and no
# owner credentials, only these three scalars. It is signed with a process-local
# secret so a client cannot advance the offset and cause records to be skipped.
# Any malformed, unsigned, or tampered token falls back to a full snapshot.

_PROGRESS_VERSION = 1


def _progress_payload(generation: int, offset: int, machine_id: str) -> bytes:
    return json.dumps(
        {
            "v": _PROGRESS_VERSION,
            "g": int(generation),
            "o": int(offset),
            "m": str(machine_id),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def encode_progress(
    generation: int,
    offset: int,
    machine_id: str,
    *,
    secret: Optional[bytes] = None,
) -> str:
    """Encode a history progress token (opaque base64url string).

    Carries only ``(generation, offset, machine_id)`` — never record content
    or owner credentials.
    """
    payload = _progress_payload(generation, offset, machine_id)
    signature = hmac.new(secret, payload, hashlib.sha256).hexdigest() if secret else ""
    envelope = json.dumps(
        {
            "p": base64.urlsafe_b64encode(payload).decode("ascii"),
            "s": signature,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(envelope).decode("ascii")


def decode_progress(
    token: Optional[str],
    *,
    secret: Optional[bytes] = None,
) -> Optional[Dict[str, Any]]:
    """Decode a progress token, returning ``None`` on invalid input.

    A ``None`` result means "no usable progress" and the caller MUST fall back
    to a full snapshot. Returns ``{"generation", "offset", "machine_id"}`` on
    success. When *secret* is supplied, the token must also carry a valid HMAC;
    decoding without a secret is inspection-only and does not establish that
    the server issued the token.
    """
    if not token or not isinstance(token, str):
        return None
    try:
        envelope_raw = base64.urlsafe_b64decode(token.encode("ascii"))
        envelope = json.loads(envelope_raw.decode("utf-8"))
        if not isinstance(envelope, dict):
            return None
        payload_raw = base64.urlsafe_b64decode(envelope["p"].encode("ascii"))
        data = json.loads(payload_raw.decode("utf-8"))
    except Exception:
        return None
    if secret is not None:
        signature = envelope.get("s")
        if not isinstance(signature, str) or not hmac.compare_digest(
            signature,
            hmac.new(secret, payload_raw, hashlib.sha256).hexdigest(),
        ):
            return None
    if not isinstance(data, dict) or data.get("v") != _PROGRESS_VERSION:
        return None
    generation = data.get("g")
    offset = data.get("o")
    machine_id = data.get("m")
    # Booleans are ints in Python; reject them explicitly so a tampered token
    # cannot smuggle a ``True``/``False`` past the type check.
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or not isinstance(offset, int)
        or isinstance(offset, bool)
        or not isinstance(machine_id, str)
        or offset < 0
    ):
        return None
    return {
        "generation": generation,
        "offset": offset,
        "machine_id": machine_id,
    }


# -- history bundle content signature ---------------------------------------
#
# The progress *token* pins a client to an exact bundle lifecycle so a REST
# reconnect can be served a delta. The bundle *signature* is the same three
# facts (generation, record count, machine) rendered as a short, stable,
# **non-secret** string the client can hold and echo verbatim to ask a cheap
# "has anything changed?" question. It is deliberately O(1) — a hash of
# ``(generation, total, machine_id)`` rather than of the record bodies — so the
# self-heal poll that fires every few seconds against a multi-MB bundle costs a
# constant-time compare, never a re-hash of the whole conversation (the very
# cost this traffic-reduction work exists to remove). It changes exactly when a
# delta would carry new tail (``total`` grew) or when the bundle was replaced
# (``generation`` rolled) or re-pulled from a different daemon (``machine_id``),
# which is precisely the "no new records" vs "new records" distinction the
# not-modified fast path needs.
def bundle_signature(generation: int, total: int, machine_id: str) -> str:
    """Return the short content-version signature for a bundle snapshot."""
    raw = f"{int(generation)}:{int(total)}:{machine_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass
class FlowSnapshot:
    """Server-side view of one flow on one machine.

    Mirrors the per-flow shape produced by the daemon's aggregator
    (``se3.daemon.aggregator.FlowSnapshot``), kept as a plain record so the
    server never has to import the daemon's dataclasses.
    """

    flow_id: str
    project_root: str = ""
    task_description: str = ""
    task_type: str = ""
    status: str = "unknown"
    current_step: Optional[str] = None
    current_step_index: int = 0
    total_steps: int = 0
    progress: float = 0.0
    updated_at: Optional[str] = None
    summary: Optional[str] = None
    pending_calls: List[Dict[str, Any]] = field(default_factory=list)
    step_history: List[Dict[str, Any]] = field(default_factory=list)
    # Running sub-state mirrored from the daemon aggregator's FlowSnapshot: True
    # while a synchronous run is queued behind the main-worktree mutex. The flow
    # stays RUNNING; the frontend renders it as RUNNING·waiting-for-lock.
    waiting_for_lock: bool = False
    # Authoritative resumability signal computed by the daemon aggregator from
    # the flow's semantic state (a non-completed flow with a valid intermediate
    # state — including a per-flow snapshot superseded in engine.json). When the
    # daemon supplies this (the daemon→server protocol carries it), it is the
    # primary signal both the server's ``is_flow_resumable`` and the frontend's
    # ``isFlowResumable`` honour; an older daemon that omits it defaults to
    # ``False`` and the consumers fall back to their legacy status-based logic.
    resumable: bool = False

    @classmethod
    def from_payload(cls, data: Dict[str, Any]) -> "FlowSnapshot":
        """Build a snapshot from a daemon-supplied per-flow dict."""
        flow_id = data.get("flow_id") or data.get("project_root") or "unknown"
        return cls(
            flow_id=str(flow_id),
            project_root=str(data.get("project_root") or ""),
            task_description=str(data.get("task_description") or ""),
            task_type=str(data.get("task_type") or ""),
            status=str(data.get("status") or "unknown"),
            current_step=data.get("current_step"),
            current_step_index=int(data.get("current_step_index") or 0),
            total_steps=int(data.get("total_steps") or 0),
            progress=float(data.get("progress") or 0.0),
            updated_at=data.get("updated_at"),
            summary=data.get("summary"),
            pending_calls=list(data.get("pending_calls") or []),
            step_history=list(data.get("step_history") or []),
            waiting_for_lock=bool(data.get("waiting_for_lock", False)),
            resumable=bool(data.get("resumable", False)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "project_root": self.project_root,
            "task_description": self.task_description,
            "task_type": self.task_type,
            "status": self.status,
            "current_step": self.current_step,
            "current_step_index": self.current_step_index,
            "total_steps": self.total_steps,
            "progress": self.progress,
            "updated_at": self.updated_at,
            "summary": self.summary,
            "pending_calls": self.pending_calls,
            "step_history": self.step_history,
            "waiting_for_lock": self.waiting_for_lock,
            "resumable": self.resumable,
        }


@dataclass
class MachineRecord:
    """Server-side record of one connected (or recently-seen) SE3 machine.

    ``owner_id`` is the internal owner this machine's daemon authenticated as
    during its ``HELLO`` (resolved from the daemon key by the identity layer
    and written by :meth:`ServerState.register_machine`). It is the trust-domain
    key every owner-scoped query filters on: a machine with no resolved owner
    (``None``) belongs to no trust domain and is therefore invisible to any
    owner-scoped view. The field is live state — it is set on each daemon
    reconnect and never persisted, matching the rest of this in-memory store.
    """

    machine_id: str
    hostname: str = ""
    se3_version: str = ""
    #: The wire ``protocol_version`` the daemon advertised in its HELLO. Drives
    #: the per-machine full-frame fall-back on the detail leg: a daemon speaking
    #: a revision older than 3 (see :func:`protocol.supports_traffic_reduction`)
    #: never received the DETAIL_REQUEST message type and would silently drop it,
    #: so the server must NOT route a detail pull to it — its STATUS_UPDATE mirror
    #: already carries the untruncated body. Empty string means "unknown / legacy".
    protocol_version: str = ""
    owner_id: Optional[str] = None
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    online: bool = True
    flows: Dict[str, FlowSnapshot] = field(default_factory=dict)
    project_roots: List[str] = field(default_factory=list)

    def to_dict(self, *, include_flows: bool = True) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "machine_id": self.machine_id,
            "hostname": self.hostname,
            "se3_version": self.se3_version,
            "protocol_version": self.protocol_version,
            "owner_id": self.owner_id,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "online": self.online,
            "flow_count": len(self.flows),
            "project_roots": list(self.project_roots),
        }
        if include_flows:
            data["flows"] = [f.to_dict() for f in self.flows.values()]
        return data


def _owned(record: "MachineRecord", owner: Optional[str]) -> bool:
    """Whether *record* is visible to an *owner*-scoped query.

    ``owner is None`` is the unscoped / admin view: every machine is visible
    (this preserves the pre-multi-tenant behaviour and lets a not-yet-wired
    deployment keep working). When *owner* is a concrete id, only machines the
    daemon authenticated into that same trust domain are visible — an
    unbound machine (``owner_id is None``) is fail-closed out of every
    owner-scoped view.
    """
    return owner is None or record.owner_id == owner


@dataclass(frozen=True)
class HistoryWriteOutcome:
    """What a single daemon history frame did to the cached bundle.

    A plain ``bool`` cannot express the case #287 turns on: a ``full`` frame the
    cache layer REFUSED as destructive (same machine, fewer records than the
    bundle already holds) still reports "no waiter should time out on this", yet
    its records are exactly the ones that must never reach a browser. The
    fan-out layer needs both facts separately, so the write reports them
    separately.
    """

    #: The frame left the cache in an authoritative state, so a REST pull waiter
    #: parked on this flow may be resolved from it. True both for a frame that
    #: populated / extended the bundle AND for one that was refused as a no-op
    #: (identical or shrinking full) — in either case the daemon answered and
    #: the cache holds the right records, so parking on to a 504 helps nobody.
    resolves_pull: bool

    #: The frame was a ``full`` snapshot REJECTED as destructive: it carried
    #: fewer records than the same machine's non-empty cached bundle. The cache
    #: kept its own records, so this frame's payload is known-truncated and MUST
    #: NOT be relayed anywhere. Set in the two cases the full branch refuses,
    #: whose scopes deliberately differ: an EMPTY frame is refused for EVERY
    #: flow (a zero-record full can never be a legitimate answer for a flow the
    #: server already holds records for — on the wire it is indistinguishable
    #: from an unresolved history directory), while a SHORTER-but-non-empty
    #: frame is refused only for an ACTIVE WORKTREE flow (for an ordinary flow a
    #: shrink is legitimate: a retried failed step rewrites its step jsonl in
    #: place). See the ``INVARIANT`` notes in :meth:`apply_history_frame`.
    rejected_full: bool = False


class ServerState:
    """Thread-safe (asyncio-safe) in-memory store of all machine state."""

    #: Seconds after which an unanswered self-heal recovery pull (see
    #: :attr:`_history_recovery_inflight`) is treated as lost so a later
    #: discarded append can re-arm a fresh pull. Sized well above the
    #: round-trip of a healthy full pull (sub-second) yet small enough that a
    #: silently-dropped reply self-corrects within one grace-window cadence, so
    #: at worst one redundant pull fires per interval and the flow is never
    #: permanently wedged.
    _HISTORY_RECOVERY_TTL: float = 30.0

    #: Minimum seconds between cache-miss ``full`` daemon pulls for the SAME
    #: flow. A self-heal poll that presents a diverged token forces one full
    #: rebuild; without this floor a client stuck presenting the same stale
    #: token would trigger a fresh multi-MB回源 pull on every poll. Sized to a
    #: few poll intervals so a genuine divergence still heals promptly while a
    #: repeated-miss storm collapses onto one in-flight pull.
    _HISTORY_FULL_PULL_MIN_INTERVAL: float = 5.0

    def __init__(self) -> None:
        self._machines: Dict[str, MachineRecord] = {}
        # History relay caches. The server is a pure in-memory relay for
        # history data — neither of these is ever written to disk.
        #: machine_id -> list of history session-meta dicts (the daemon's
        #: ``se3 history`` index).
        self._history_index: Dict[str, List[Dict[str, Any]]] = {}
        #: flow_id -> cached history bundle (records + cursor + owner + the
        #: ``generation`` lifecycle id backing the incremental progress token).
        self._history_data: Dict[str, Dict[str, Any]] = {}
        #: Flows whose cache was invalidated by a cross-machine append. Further
        #: appends stay ignored until an authoritative full bundle arrives.
        self._history_requires_full: set[str] = set()
        #: flow_id -> monotonic dispatch time of a self-heal
        #: ``MSG_HISTORY_REQUEST`` (full pull) sent to the owning daemon and not
        #: yet answered. Without this, a flow that landed in
        #: ``_history_requires_full`` stayed frozen until the user exited and
        #: re-entered the chat (the only path that previously re-pulled a ``full``
        #: frame). The receive loop consults :meth:`take_recovery_pull` after
        #: every discarded append so it fires at most one recovery pull per stuck
        #: flow; the marker clears when the authoritative full frame lands (see
        #: :meth:`append_history`). It is a *timestamp*, not a bare flag, so a
        #: pull whose reply never arrives (the daemon swallowed a read error and
        #: returned silently, or disconnected right after the request left the
        #: server) cannot wedge the flow forever: after
        #: :data:`_HISTORY_RECOVERY_TTL` seconds :meth:`take_recovery_pull`
        #: treats the marker as stale and re-arms a fresh pull, so the bundle
        #: still self-heals without the user exiting and re-entering the chat.
        self._history_recovery_inflight: Dict[str, float] = {}
        #: flow_id -> monotonic time of the last cache-miss ``full`` daemon pull
        #: dispatched by the REST endpoint. Used to rate-limit repeated full
        #: rebuilds of the SAME flow (see :meth:`mark_full_pull` /
        #: :meth:`full_pull_throttled`): a client that keeps presenting a
        #: diverged token would otherwise fan out one 17 MB回源 pull per poll.
        self._history_full_pull_at: Dict[str, float] = {}
        #: Monotonic counter handing out a fresh ``generation`` to every newly
        #: created / replaced history bundle, so a progress token is bound to
        #: exactly one bundle lifecycle (see ``encode_progress``).
        self._history_generation: int = 0
        #: Process-local signing key for opaque history progress tokens. Tokens
        #: naturally become invalid after a server restart, which correctly
        #: degrades reconnects to a full snapshot.
        self._history_progress_secret = secrets.token_bytes(32)
        #: Issue mirror: machine_id -> project_root -> list of issue dicts.
        #: Updated from daemon STATUS_UPDATE snapshots; the server never writes
        #: issues to disk — writes are dispatched as MSG_ISSUE_COMMAND to the
        #: owning daemon which applies them via IssueManager.
        self._issues: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()

    # -- machine lifecycle -------------------------------------------------

    async def register_machine(
        self,
        machine_id: str,
        hostname: str = "",
        se3_version: str = "",
        *,
        owner_id: Optional[str] = None,
        protocol_version: str = "",
    ) -> MachineRecord:
        """Register (or refresh) a machine on HELLO and mark it online.

        A reconnecting machine keeps its previously aggregated flows **only when
        the resolved owner is unchanged**, until the next STATUS_UPDATE replaces
        them. *owner_id* is the trust domain the daemon authenticated into
        (resolved from its HELLO key by the identity layer); it is recorded on
        the machine so every owner-scoped query can filter on it. ``None`` leaves
        the machine unbound — only the unscoped/admin view will see it.

        ``machine_id`` is **not** a secret: the daemon derives it from the
        hostname + NIC MAC and supplies it verbatim in HELLO, so any holder of a
        valid daemon key can connect under a victim's ``machine_id``. To stop a
        machine_id collision/takeover from leaking one owner's trust-domain state
        to another, whenever the resolved owner of an existing record *changes*
        we discard the previous owner's aggregated flows and the machine's cached
        history (index + bundles) before rebinding the record to the new owner.
        """
        async with self._lock:
            record = self._machines.get(machine_id)
            now = time.time()
            if record is None:
                record = MachineRecord(
                    machine_id=machine_id,
                    hostname=hostname,
                    se3_version=se3_version,
                    protocol_version=protocol_version,
                    owner_id=owner_id,
                    connected_at=now,
                    last_seen=now,
                    online=True,
                )
                self._machines[machine_id] = record
            else:
                record.hostname = hostname or record.hostname
                record.se3_version = se3_version or record.se3_version
                # Always refresh the advertised protocol version on reconnect —
                # unlike hostname/se3_version this must reflect the CURRENT peer
                # even if it downgraded, so the detail-leg fall-back stays correct.
                record.protocol_version = protocol_version
                if record.owner_id != owner_id:
                    # Owner takeover on a forgeable machine_id: scrub the prior
                    # owner's flows and history so the new owner can never read
                    # them. Flow/history retention across reconnects is only safe
                    # when the owner is unchanged.
                    self._discard_machine_state(machine_id)
                    record.flows = {}
                record.owner_id = owner_id
                record.connected_at = now
                record.last_seen = now
                record.online = True
            return record

    def _discard_machine_state(self, machine_id: str) -> None:
        """Drop the cached history index/bundles/issues owned by *machine_id*.

        Caller must hold ``self._lock``. Used on an owner change so a
        machine_id collision/takeover cannot expose the prior owner's history
        or issues. ``record.flows`` is cleared by the caller (it owns the
        record).
        """
        self._history_index.pop(machine_id, None)
        self._history_data = {
            flow_id: bundle
            for flow_id, bundle in self._history_data.items()
            if str(bundle.get("machine_id") or "") != machine_id
        }
        self._issues.pop(machine_id, None)

    async def mark_offline(self, machine_id: str) -> None:
        """Mark a machine offline (its daemon disconnected)."""
        async with self._lock:
            record = self._machines.get(machine_id)
            if record is not None:
                record.online = False
                record.last_seen = time.time()

    async def touch(self, machine_id: str) -> None:
        """Refresh a machine's ``last_seen`` (e.g. on a heartbeat PONG)."""
        async with self._lock:
            record = self._machines.get(machine_id)
            if record is not None:
                record.last_seen = time.time()

    # -- status ingestion --------------------------------------------------

    async def update_status(
        self, machine_id: str, snapshot: Dict[str, Any]
    ) -> None:
        """Apply a daemon STATUS_UPDATE snapshot to the machine record.

        *snapshot* is the dict form of the daemon's ``MachineStatus`` — its
        ``flows`` list fully replaces the machine's known flows.  The
        ``issues`` list (when present) replaces the machine's issue mirror
        keyed by ``project_root``.
        """
        async with self._lock:
            record = self._machines.get(machine_id)
            now = time.time()
            if record is None:
                record = MachineRecord(machine_id=machine_id, connected_at=now)
                self._machines[machine_id] = record
            record.last_seen = now
            record.online = True
            hostname = snapshot.get("hostname")
            if hostname:
                record.hostname = str(hostname)
            raw_roots = snapshot.get("project_roots")
            if isinstance(raw_roots, list):
                record.project_roots = [str(p) for p in raw_roots if p]
            else:
                record.project_roots = []
            flows: Dict[str, FlowSnapshot] = {}
            for raw in snapshot.get("flows") or []:
                if not isinstance(raw, dict):
                    continue
                flow = FlowSnapshot.from_payload(raw)
                flows[flow.flow_id] = flow
            record.flows = flows

            # Ingest issues from the snapshot, keyed by project_root.
            issues_by_root: Dict[str, List[Dict[str, Any]]] = {}
            for raw_issue in snapshot.get("issues") or []:
                if not isinstance(raw_issue, dict):
                    continue
                root = str(raw_issue.get("project_root") or "")
                if not root:
                    continue
                issues_by_root.setdefault(root, []).append(dict(raw_issue))
            self._issues[machine_id] = issues_by_root

    # -- queries -----------------------------------------------------------

    async def get_machine_owner(self, machine_id: str) -> Optional[str]:
        """Return the owner bound to *machine_id*, or ``None`` if unknown/unbound.

        Used by the owner-scoped ``/ws/ui`` push paths to decide which UI
        clients may see a machine's flow/history/interjection events.
        """
        async with self._lock:
            record = self._machines.get(machine_id)
            return record.owner_id if record is not None else None

    async def get_machines(
        self, *, owner: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return summary dicts for every known machine (no nested flows).

        When *owner* is given, only machines bound to that owner are returned.
        """
        async with self._lock:
            return [
                m.to_dict(include_flows=False)
                for m in self._machines.values()
                if _owned(m, owner)
            ]

    async def get_machine(
        self, machine_id: str, *, owner: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Return the full record for *machine_id*, or ``None`` if unknown.

        With *owner* set, a machine owned by a different owner is reported as
        ``None`` (indistinguishable from absent — no cross-owner existence
        leak).
        """
        async with self._lock:
            record = self._machines.get(machine_id)
            if record is None or not _owned(record, owner):
                return None
            return record.to_dict()

    async def machine_supports_detail_pull(
        self, machine_id: str, *, owner: Optional[str] = None
    ) -> bool:
        """Whether *machine_id*'s daemon understands the on-demand detail leg.

        Returns ``True`` only when the machine is known (and owner-visible) and
        advertised a protocol version that includes ``MSG_DETAIL_REQUEST``
        (revision 3+). A pre-v3 daemon would silently drop the frame, so the
        detail endpoint must fall back to serving the STATUS_UPDATE mirror
        instead of parking a waiter that can only time out. An unknown machine
        reads ``False`` (fail-closed to the mirror path).
        """
        async with self._lock:
            record = self._machines.get(machine_id)
            if record is None or not _owned(record, owner):
                return False
            return protocol.supports_traffic_reduction(record.protocol_version)

    async def get_machines_full(
        self, *, owner: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return full dicts (machines *with* their nested flows).

        Used to build the realtime payload broadcast to web-frontend clients,
        which need the flow list in a single frame rather than one REST call
        per machine. With *owner* set, only that owner's machines are included.
        """
        async with self._lock:
            return [
                m.to_dict(include_flows=True)
                for m in self._machines.values()
                if _owned(m, owner)
            ]

    async def get_machine_flows(
        self, machine_id: str, *, owner: Optional[str] = None
    ) -> Optional[List[Dict[str, Any]]]:
        """Return the flow list for *machine_id*, or ``None`` if unknown.

        With *owner* set, a machine owned by a different owner reads as
        ``None`` (no cross-owner visibility).
        """
        async with self._lock:
            record = self._machines.get(machine_id)
            if record is None or not _owned(record, owner):
                return None
            return [f.to_dict() for f in record.flows.values()]

    async def get_flow(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Find a flow by id across all machines.

        Returns ``(machine_id, flow_dict)`` or ``None`` when no machine owns a
        flow with that id. With *owner* set, flows on machines belonging to a
        different owner are skipped — owner A can neither see nor (via the
        callers that gate on this) control owner B's flows.
        """
        async with self._lock:
            for machine_id, record in self._machines.items():
                if not _owned(record, owner):
                    continue
                flow = record.flows.get(flow_id)
                if flow is not None:
                    return machine_id, flow.to_dict()
        return None

    async def find_machine_for_flow(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> Optional[str]:
        """Return the machine id owning *flow_id*, or ``None``."""
        result = await self.get_flow(flow_id, owner=owner)
        return result[0] if result is not None else None

    async def find_call_owner(
        self,
        call_id: str,
        *,
        owner: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> Optional[Tuple[str, str]]:
        """Resolve the machine + project_root owning a pending *call_id*.

        Scans every owner-scoped flow's ``pending_calls`` for a matching
        ``call_id`` and returns ``(machine_id, project_root)`` so the on-demand
        detail endpoint can route a :data:`~se3.daemon.protocol.MSG_DETAIL_REQUEST`
        to the daemon whose flow raised the call (its truncated prompt is
        surfaced in STATUS_UPDATE; the full prompt is fetched on demand).
        Returns ``None`` when no owner-scoped flow holds the call.

        ``call_id`` is only unique within a single project; two owner-scoped
        projects can each hold a pending call with the same local id. When the
        caller knows the *project_root* of the call whose full prompt it wants,
        it MUST be passed so resolution is pinned to that project — otherwise an
        earlier-scanned project's matching call would misroute the detail
        request to the wrong daemon / filesystem target.
        """
        async with self._lock:
            for machine_id, record in self._machines.items():
                if not _owned(record, owner):
                    continue
                for flow in record.flows.values():
                    if project_root and str(flow.project_root or "") != str(
                        project_root
                    ):
                        continue
                    for call in flow.pending_calls or []:
                        if not isinstance(call, dict):
                            continue
                        if str(call.get("call_id") or "") == str(call_id):
                            return machine_id, str(flow.project_root or "")
        return None

    async def get_pending_call(
        self,
        call_id: str,
        *,
        owner: Optional[str] = None,
        machine_id: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return the mirror dict of pending *call_id*, or ``None``.

        Used by the detail endpoint's pre-v3 fall-back: when the owning daemon
        does not speak the DETAIL_REQUEST protocol, its STATUS_UPDATE mirror
        already carries the untruncated prompt, so the endpoint serves this dict
        directly rather than pulling. Scoping mirrors :meth:`find_call_owner`.
        """
        async with self._lock:
            for mid, record in self._machines.items():
                if not _owned(record, owner):
                    continue
                if machine_id and mid != machine_id:
                    continue
                for flow in record.flows.values():
                    if project_root and str(flow.project_root or "") != str(
                        project_root
                    ):
                        continue
                    for call in flow.pending_calls or []:
                        if not isinstance(call, dict):
                            continue
                        if str(call.get("call_id") or "") == str(call_id):
                            return dict(call)
        return None

    # -- resume helpers ----------------------------------------------------

    #: Flow statuses that the daemon can directly resume via
    #: ``se3 run --resume --flow-id <id>``.  RUNNING flows already have a
    #: live process; COMPLETED flows are done; INIT/RECOVERING are transient.
    RESUMABLE_STATUSES: set = {"failed", "paused"}

    async def is_flow_resumable(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Return ``(machine_id, flow_dict)`` when *flow_id* is resumable.

        A flow is resumable when it is owned by *owner* (or the unscoped
        admin view) and either the daemon's authoritative ``resumable`` flag is
        set (the primary signal — covers paused / interrupted / recoverable-error
        flows surfaced from a per-flow snapshot, whose raw status may still read
        ``running``) or, as a backward-compatible fallback for an older daemon
        that omits the flag, its status is in :data:`RESUMABLE_STATUSES`.
        Returns ``None`` when neither holds — the caller maps ``None`` to 404.

        A ``completed`` status is terminal-and-done and is never resumable,
        even if a stale snapshot mistakenly carries ``resumable=True``: the
        daemon resume validator rejects a COMPLETED flow, so honoring the flag
        here would let the UI dispatch a resume the daemon then bounces. The
        completed guard therefore takes precedence over the flag.
        """
        result = await self.get_flow(flow_id, owner=owner)
        if result is None:
            return None
        machine_id, flow = result
        status = str(flow.get("status") or "").lower()
        if status == "completed":
            return None
        if flow.get("resumable"):
            return machine_id, flow
        if status not in self.RESUMABLE_STATUSES:
            return None
        return machine_id, flow

    # -- end-session helpers -----------------------------------------------

    async def is_flow_endable(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Return ``(machine_id, flow_dict)`` when *flow_id* can be ended.

        A flow is endable when it is owned by *owner* (or the unscoped admin
        view) and either:

        * its status is **not** ``completed`` — a dangling worktree may be left
          behind by a RUNNING / PAUSED / FAILED / RECOVERING / INIT session, so
          all of those are endable; or
        * it *is* ``completed`` **but** is still a live worktree session whose
          ``project_root`` points inside ``<main>/se3/worktrees/<name>``. This
          is the dangling-worktree case this feature exists for: when a
          ``se3 run --worktree`` flow reaches COMPLETED but the follow-up
          merge/cleanup fails or is interrupted, the worktree stays on disk with
          a completed ``engine.json`` and the daemon keeps reporting it live
          under its worktree root. Such an orphan must still be endable so the
          daemon can archive and remove it. An ordinary completed (main-branch)
          session, or a worktree session already cleaned up the normal way
          (which is reported only as an archived/main-root flow, never under a
          live worktree root), has nothing left to end.

        Returns ``None`` for an unknown / cross-owner flow (caller maps to 404)
        and for an ordinary already-completed flow (caller maps to 409),
        mirroring the honest receipt the resume gate provides.
        """
        result = await self.get_flow(flow_id, owner=owner)
        if result is None:
            return None
        machine_id, flow = result
        status = str(flow.get("status") or "").lower()
        if status == "completed" and not _is_worktree_session_path(
            flow.get("project_root")
        ):
            return None
        return machine_id, flow

    # -- history relay (in-memory only, never persisted) -------------------

    async def update_history_index(
        self, machine_id: str, sessions: List[Dict[str, Any]]
    ) -> None:
        """Replace the history-session index reported by *machine_id*.

        *sessions* is the daemon's ``MSG_HISTORY_INDEX`` list of session-meta
        dicts (flow id, task description, status, timestamps, active flag).
        It fully replaces the machine's previously known index — the daemon
        always reports the complete index, not a delta. Kept purely in memory.
        """
        async with self._lock:
            cleaned = [dict(s) for s in (sessions or []) if isinstance(s, dict)]
            self._history_index[machine_id] = cleaned

    async def merge_history_index_delta(
        self,
        machine_id: str,
        upserts: List[Dict[str, Any]],
        removed: List[str],
    ) -> None:
        """Merge a ``MSG_HISTORY_INDEX_DELTA`` into the machine's full index.

        In the steady state a daemon reports only the SessionMeta rows that
        changed (*upserts*, keyed by ``flow_id``) and the flow ids that vanished
        (*removed*) instead of re-sending the whole index — so index traffic
        scales with the number of *changed* flows, not the total flow count.
        The server keeps the authoritative full index in memory and applies the
        delta on top of it: each upsert replaces (or adds) the row for its
        ``flow_id``, each removed id drops its row. A full
        :meth:`update_history_index` still lands on connect / reconnect /
        HISTORY_INDEX_REQUEST as the reconciliation baseline both ends agree on.

        Existing insertion order is preserved (new flow ids append) so a
        subsequent full re-push and this incremental path converge on the same
        set; ``get_history_index`` re-sorts by ``updated_at`` regardless, so
        order here is not load-bearing for the UI. Rows without a usable
        ``flow_id`` are ignored — a delta row can only be addressed by id.
        """
        async with self._lock:
            existing = self._history_index.get(machine_id, [])
            by_id: Dict[str, Dict[str, Any]] = {}
            order: List[str] = []
            for session in existing:
                fid = str(session.get("flow_id") or "")
                if not fid:
                    continue
                if fid not in by_id:
                    order.append(fid)
                by_id[fid] = dict(session)
            for up in upserts or []:
                if not isinstance(up, dict):
                    continue
                fid = str(up.get("flow_id") or "")
                if not fid:
                    continue
                if fid not in by_id:
                    order.append(fid)
                by_id[fid] = dict(up)
            for fid in removed or []:
                by_id.pop(str(fid), None)
            self._history_index[machine_id] = [
                by_id[fid] for fid in order if fid in by_id
            ]

    async def mark_full_pull(self, flow_id: str) -> None:
        """Stamp the monotonic time of a cache-miss ``full`` daemon pull.

        Called by the REST history endpoint right before it dispatches a回源
        ``MSG_HISTORY_REQUEST`` for *flow_id*, so :meth:`full_pull_throttled`
        can rate-limit a repeated-miss storm for the same flow.
        """
        async with self._lock:
            self._history_full_pull_at[flow_id] = time.monotonic()

    async def full_pull_throttled(
        self, flow_id: str, *, min_interval: Optional[float] = None
    ) -> bool:
        """Return whether a full pull for *flow_id* fired within the floor window.

        ``True`` means a cache-miss full rebuild for this flow was dispatched
        less than *min_interval* seconds ago (default
        :attr:`_HISTORY_FULL_PULL_MIN_INTERVAL`), so the caller should prefer a
        already-cached snapshot over firing another multi-MB回源 pull. Measured
        on the monotonic clock so a wall-clock step cannot defeat the floor.
        """
        window = (
            self._HISTORY_FULL_PULL_MIN_INTERVAL
            if min_interval is None
            else min_interval
        )
        async with self._lock:
            at = self._history_full_pull_at.get(flow_id)
            if at is None:
                return False
            return (time.monotonic() - at) < window

    async def get_history_index(
        self, *, owner: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Return the history index aggregated across every machine.

        Each entry is annotated with the ``machine_id`` that reported it and
        the list is sorted by ``updated_at`` descending (entries lacking the
        field sort last). With *owner* set, only sessions reported by machines
        bound to that owner are included — history is owner-scoped just like the
        live machine/flow views.
        """
        async with self._lock:
            entries: List[Dict[str, Any]] = []
            for machine_id, sessions in self._history_index.items():
                record = self._machines.get(machine_id)
                if owner is not None and (
                    record is None or not _owned(record, owner)
                ):
                    continue
                for session in sessions:
                    entry = dict(session)
                    entry.setdefault("machine_id", machine_id)
                    entries.append(entry)
        entries.sort(key=lambda e: str(e.get("updated_at") or ""), reverse=True)
        return entries

    async def append_history(
        self,
        flow_id: str,
        mode: str,
        records: List[Dict[str, Any]],
        *,
        cursor: Optional[Dict[str, Any]] = None,
        machine_id: str = "",
    ) -> bool:
        """Cache history *records* for *flow_id*; report waiter-resolvability.

        Thin bool view of :meth:`apply_history_frame` for callers that only need
        to know whether an on-demand pull waiter may be resolved from the frame
        (see that method for the full write semantics). A caller that also
        RELAYS the frame onward — the ``/ws/ui`` fan-out — must use
        :meth:`apply_history_frame` instead: a rejected shrinking full resolves
        the waiter yet carries records the cache refused, and this bool cannot
        tell the two apart.
        """
        outcome = await self.apply_history_frame(
            flow_id, mode, records, cursor=cursor, machine_id=machine_id
        )
        return outcome.resolves_pull

    async def apply_history_frame(
        self,
        flow_id: str,
        mode: str,
        records: List[Dict[str, Any]],
        *,
        cursor: Optional[Dict[str, Any]] = None,
        machine_id: str = "",
    ) -> HistoryWriteOutcome:
        """Cache history *records* for *flow_id* and report what the write did.

        ``mode == "full"`` replaces any cached records — except when the frame
        would shrink the same machine's non-empty bundle: an EMPTY full frame is
        refused for ANY flow, and a merely shorter (non-empty) one is refused for
        an ACTIVE WORKTREE flow (see the ``INVARIANT`` notes in the full branch);
        ``mode == "append"`` extends an existing authoritative bundle. A first-sighting
        append is ignored and marks the flow as requiring a full pull, because
        it may be only the tail after a server restart. *cursor* is stored
        verbatim for the next incremental pull. Purely in-memory.

        ``resolves_pull`` is ``True`` when the cache is left authoritative — it
        populated / extended the bundle, or it was a benign no-op on an already
        correct bundle — and ``False`` when the records were discarded (a
        first-sighting or otherwise unanchored append, or a cross-machine
        delta). An on-demand pull waiter must be resolved only on a ``True``
        result so a racing ignored append cannot prematurely wake the REST
        handler before the daemon's authoritative full reply lands.

        ``rejected_full`` flags the accepted-but-untrustworthy case: a full
        snapshot refused for shrinking the same machine's non-empty bundle —
        when EMPTY, for any flow; when merely shorter, only for an active
        worktree flow (see the ``INVARIANT`` notes in the full branch). Its
        records are truncated, so no consumer may relay them.
        """
        new_records = list(records or [])
        async with self._lock:
            existing = self._history_data.get(flow_id)
            if mode == protocol.HISTORY_MODE_APPEND and existing is None:
                # An append is only meaningful relative to an authoritative
                # full bundle. After a server restart the daemon may retain its
                # cursor and send only a new tail; caching that tail as a full
                # snapshot would permanently omit all older records.
                self._history_requires_full.add(flow_id)
                # HOP-4 DEBUG (server bundle): a first-sighting append is
                # discarded and the flow is FLAGGED requires_full. Every later
                # append is then dropped (below) until a full frame lands — the
                # persistent-freeze mode. Logged so a live run reveals the flow
                # entering this stuck state at the discovery→analyze boundary.
                logger.debug(
                    "hist-diag append_history DISCARD flow=%s reason=first-sighting-append "
                    "(now flagged requires_full)",
                    flow_id,
                )
                return HistoryWriteOutcome(resolves_pull=False)
            if (
                mode == protocol.HISTORY_MODE_APPEND
                and flow_id in self._history_requires_full
            ):
                logger.debug(
                    "hist-diag append_history DISCARD flow=%s reason=requires_full-set "
                    "records=%d (stuck until a full frame)",
                    flow_id, len(new_records),
                )
                return HistoryWriteOutcome(resolves_pull=False)
            if mode == protocol.HISTORY_MODE_APPEND:
                # An ordinary append keeps the bundle ``generation`` stable so a
                # progress token issued before the append still validates. A
                # machine change mid-bundle, however, makes prior progress
                # unsafe (a different daemon's records), so it rolls a fresh
                # generation that invalidates any outstanding token.
                if machine_id and machine_id != str(
                    existing.get("machine_id") or ""
                ):
                    # A delta from another daemon is not an authoritative
                    # replacement. Discard both the stale bundle and this
                    # unanchored delta so the next REST read is a cache miss and
                    # pulls the new machine's complete history.
                    del self._history_data[flow_id]
                    self._history_requires_full.add(flow_id)
                    # The just-superseded machine's recovery pull (if any) no
                    # longer helps — the flow now needs a full pull from the NEW
                    # daemon. Drop the marker so ``take_recovery_pull`` can fire a
                    # fresh recovery for this machine instead of being wedged.
                    self._history_recovery_inflight.pop(flow_id, None)
                    logger.debug(
                        "hist-diag append_history DISCARD flow=%s reason=machine-change "
                        "(bundle dropped, flagged requires_full)",
                        flow_id,
                    )
                    return HistoryWriteOutcome(resolves_pull=False)
                # Back-fill a stable generation for an old-format bundle that the
                # ``full`` branch never created (or that lost the field), so the
                # extended bundle is a first-class delta participant rather than
                # being stuck on the full fallback forever.
                self._ensure_generation(existing)
                existing["records"].extend(new_records)
                existing["mode"] = mode
                if cursor:
                    existing["cursor"] = dict(cursor)
                if machine_id:
                    existing["machine_id"] = machine_id
                existing["updated_at"] = time.time()
                logger.debug(
                    "hist-diag append_history APPLIED-append flow=%s records=%d "
                    "total=%d",
                    flow_id, len(new_records), len(existing["records"]),
                )
                return HistoryWriteOutcome(resolves_pull=True)
            else:
                # Any branch that replaces the cached bundle wholesale (a true
                # ``full`` snapshot, or any other non-append / unrecognized mode
                # from a version-skewed or malformed daemon) establishes a fresh
                # authoritative bundle and generation, so the requires-full flag
                # MUST be cleared here too. Otherwise the flow stays flagged
                # requires-full while the new bundle is cache-hit by REST, and
                # every subsequent append delta is silently discarded — clients
                # echo a valid token and get an empty delta forever until the
                # daemon restarts and pushes a real full snapshot.
                self._history_requires_full.discard(flow_id)
                # The bundle is now authoritative again: any in-flight self-heal
                # recovery pull for this flow has been satisfied (this frame is
                # its reply, or a concurrent full pull beat it). Clear the marker
                # so a later re-desync can dispatch a fresh recovery.
                self._history_recovery_inflight.pop(flow_id, None)
                # INVARIANT: an identical full replace MUST keep the existing
                # bundle generation. The running-worktree self-heal reconcile
                # (see ``is_active_worktree_flow`` / the history endpoint) fires a
                # cursorless — hence ``full`` — pull on a throttle even when the
                # daemon has nothing new to add, so a live worktree discovery can
                # catch a round the live push dropped. If we rebuilt the bundle
                # (and rolled a fresh ``generation``) on such a no-op re-pull, we
                # would invalidate every outstanding progress token and force each
                # in-sync client into a full re-fetch + DOM rebuild on the very
                # next poll — the churn the delta/not-modified path exists to
                # avoid. So when the incoming full records are identical to the
                # cached bundle from the SAME machine, keep the bundle and its
                # generation (only the cursor may advance); the token stays valid
                # and the next poll still answers the cheap ``not_modified``.
                # INVARIANT: a ``full`` frame from the SAME machine may never take
                # records AWAY from a non-empty cached bundle. Two cases, with
                # deliberately different scopes:
                #
                # (1) EMPTY frame — refused for EVERY flow, worktree or not. The
                # daemon cannot distinguish "this flow has no records" from "I
                # failed to resolve its history directory" on the wire: both
                # arrive as ``mode=full, records=[]``. So once the server holds
                # records for a flow, an empty full can never be a legitimate
                # answer for it — whatever the flow's kind, and even when the
                # server has no ``flows`` snapshot for it at all (e.g. right after
                # a daemon reconnect, before the STATUS_UPDATE lands, when the
                # worktree predicate below cannot yet recognize it). Accepting one
                # wipes the cached rounds, rolls a fresh generation, and fans a
                # zero-record full out to every open console — a blank chat pane,
                # which is exactly the #287 symptom.
                #
                # (2) SHORTER-but-non-empty frame — refused only for an active
                # (``running`` / ``paused``) worktree flow, i.e. the same
                # ``_is_active_worktree_flow_locked`` predicate that gates the
                # self-heal reconcile. There a shrink can only come from a
                # partially-resolved read: if the worktree copy of the history is
                # pruned/renamed while the flow is still reported ``paused``, the
                # daemon honestly resolves only the main-repo copy and returns
                # round 1 alone, silently dropping every later round. For an
                # ordinary flow a shrink IS legitimate — a FAILED step retried
                # rewrites its step jsonl in place with a fresh, shorter batch —
                # and refusing it would pin the chat to stale pre-retry records.
                #
                # In both cases: keep the bundle AND its generation (so in-sync
                # clients stay on the cheap ``not_modified`` path), still resolve
                # a pull waiter blocked on this reply instead of letting it time
                # out, and report the refusal back as ``rejected_full`` — keeping
                # the cache correct is only half the job, the frame's truncated
                # records must also never be RELAYED (the ``/ws/ui`` fan-out would
                # otherwise rebuild every open chat pane from them). A frame that
                # brings records through — the self-heal path that fixes the
                # original multi-round loss — falls through and replaces the
                # bundle as before.
                if (
                    existing is not None
                    and str(existing.get("machine_id") or "") == machine_id
                    and existing.get("records")
                    and len(new_records) < len(existing["records"])
                    and (
                        not new_records
                        or self._is_active_worktree_flow_locked(flow_id)
                    )
                ):
                    if cursor:
                        existing["cursor"] = dict(cursor)
                    existing["updated_at"] = time.time()
                    generation = self._ensure_generation(existing)
                    logger.warning(
                        "hist-diag append_history REJECTED-shrinking-full flow=%s "
                        "machine=%s (incoming %d records < %d cached; kept the "
                        "cached bundle, generation %d) — the daemon returned a "
                        "full snapshot shorter than what the server already holds "
                        "for this flow; likely an unresolved or partially resolved "
                        "history directory on the daemon side",
                        flow_id, machine_id, len(new_records),
                        len(existing["records"]), generation,
                    )
                    return HistoryWriteOutcome(
                        resolves_pull=True, rejected_full=True
                    )
                if (
                    existing is not None
                    and str(existing.get("machine_id") or "") == machine_id
                    and existing.get("records") == new_records
                ):
                    if cursor:
                        existing["cursor"] = dict(cursor)
                    existing["updated_at"] = time.time()
                    generation = self._ensure_generation(existing)
                    logger.debug(
                        "hist-diag append_history APPLIED-full-noop flow=%s "
                        "records=%d (identical bundle, generation %d kept)",
                        flow_id, len(new_records), generation,
                    )
                    return HistoryWriteOutcome(resolves_pull=True)
                self._history_data[flow_id] = {
                    "flow_id": flow_id,
                    "machine_id": machine_id,
                    "mode": mode,
                    "records": new_records,
                    "cursor": dict(cursor) if cursor else {},
                    "generation": self._next_generation(),
                    "updated_at": time.time(),
                }
                logger.debug(
                    "hist-diag append_history APPLIED-full flow=%s records=%d "
                    "(bundle replaced, requires_full cleared)",
                    flow_id, len(new_records),
                )
                return HistoryWriteOutcome(resolves_pull=True)

    async def take_recovery_pull(self, flow_id: str) -> bool:
        """Return ``True`` when a self-heal full pull should be sent for *flow_id*.

        A flow lands in ``_history_requires_full`` when a live ``append`` frame
        arrives with no authoritative bundle to extend — a first sighting (e.g.
        the daemon retained its cursor across a server restart and sent only a
        tail), or a cross-machine/version desync. Historically the flow then
        stayed frozen: the live push loop only ever sends ``append`` frames, and
        every one of them was discarded (below) until a ``full`` frame arrived,
        which only happened when the user exited and re-entered the chat and its
        REST cache-miss pull fetched one. That is the "must re-enter" symptom.

        To self-heal, the receive loop calls this after every discarded append.
        The FIRST call for a stuck flow (flagged ``requires_full`` with no
        recovery already in flight) returns ``True`` and marks a recovery in
        flight, so the caller sends exactly one ``MSG_HISTORY_REQUEST`` (a
        cursorless — hence ``full`` — pull) to the owning daemon. Its ``full``
        reply repopulates the bundle and clears both flags via
        :meth:`append_history`, after which subsequent appends apply and
        broadcast normally. Every later discarded append for the same still-stuck
        flow returns ``False`` so a per-cycle append storm cannot fan out one
        request per frame.

        The in-flight marker records the dispatch *time*, not a bare flag: if a
        pull's reply never arrives (the daemon swallowed a read error and
        returned without sending HISTORY_DATA, its reply send failed, or it
        disconnected right after the request left the server) a bare flag would
        wedge the flow forever — every later append still discarded, every
        ``take_recovery_pull`` returning ``False``, the bundle frozen until some
        client happens to trigger a REST cache-miss full pull. So a marker older
        than :attr:`_HISTORY_RECOVERY_TTL` is treated as lost and this call
        re-arms a fresh pull, letting the bundle self-heal without the user
        exiting and re-entering the chat.
        """
        async with self._lock:
            if flow_id not in self._history_requires_full:
                return False
            dispatched_at = self._history_recovery_inflight.get(flow_id)
            # Age the marker on the monotonic clock so the TTL measures real
            # elapsed time: a backward wall-clock step (NTP correction) must not
            # keep a lost pull's marker "fresh" and freeze the flow past the TTL.
            if (
                dispatched_at is not None
                and (time.monotonic() - dispatched_at) < self._HISTORY_RECOVERY_TTL
            ):
                return False
            self._history_recovery_inflight[flow_id] = time.monotonic()
            return True

    async def clear_recovery_pull(self, flow_id: str) -> None:
        """Drop the in-flight recovery marker for *flow_id*.

        Called when the recovery ``MSG_HISTORY_REQUEST`` send failed (the daemon
        disconnected between the append and the recovery dispatch), so a later
        append can re-arm a fresh recovery rather than being wedged behind a
        request that never left the server.
        """
        async with self._lock:
            self._history_recovery_inflight.pop(flow_id, None)

    def _next_generation(self) -> int:
        """Hand out a fresh bundle generation. Caller must hold ``self._lock``."""
        self._history_generation += 1
        return self._history_generation

    def _ensure_generation(self, bundle: Dict[str, Any]) -> int:
        """Return *bundle*'s stable generation, back-filling one on first contact.

        Bundles created by the current ``full`` branch always carry a positive
        ``generation``. An **old-format bundle** — one that predates the
        ``generation`` field, or that has only ever been extended through the
        ``append`` branch (which historically never initialised it) — carries no
        ``generation`` key, or a falsy ``0``/``None``. Reading such a bundle with
        the old ``int(cached.get("generation") or 0)`` idiom yielded ``0`` every
        time, and because the per-bundle value was never written back, the token
        minted on one read and the generation observed on the next never had a
        durable anchor: the flow was perpetually shunted onto the ``full``
        fallback instead of serving a ``delta``.

        On first contact we hand the bundle a fresh, **stable** generation via
        :meth:`_next_generation` and write it back into the bundle dict, so every
        later snapshot read, ``get_history`` copy, and ``append`` extend observes
        the SAME generation and a progress token minted against it validates on
        the next reconnect (the delta path) rather than perpetually falling back
        to a full reload. Missing / ``0`` / ``None`` are all treated as "not yet
        assigned"; a positive int is returned unchanged. Caller must hold
        ``self._lock``.
        """
        gen = bundle.get("generation")
        if not isinstance(gen, int) or isinstance(gen, bool) or gen <= 0:
            gen = self._next_generation()
            bundle["generation"] = gen
        return gen

    async def get_history(self, flow_id: str) -> Optional[Dict[str, Any]]:
        """Return a copy of cached history for *flow_id*, or ``None`` on miss."""
        async with self._lock:
            cached = self._history_data.get(flow_id)
            if cached is None:
                return None
            return {
                "flow_id": cached["flow_id"],
                "machine_id": cached.get("machine_id", ""),
                "mode": cached.get("mode", ""),
                "records": list(cached["records"]),
                "cursor": dict(cached.get("cursor") or {}),
                "generation": self._ensure_generation(cached),
                "updated_at": cached.get("updated_at"),
            }

    async def get_history_snapshot(
        self,
        flow_id: str,
        *,
        after: Optional[str] = None,
        expected_machine_id: Optional[str] = None,
        expected_owner: Optional[str] = None,
        known_signature: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Atomically read a full or incremental history snapshot for *flow_id*.

        Validation, record slicing and the new progress token are produced
        under a single hold of ``self._lock`` so the returned ``records`` and
        ``progress`` describe the **same** bundle snapshot — a concurrent
        append / replacement cannot interleave between them.

        Returns ``None`` (a cache miss the caller resolves by pulling from the
        daemon) when no bundle is cached, when *expected_machine_id* is given
        and the cached bundle belongs to a different machine, or when
        *expected_owner* is given and owner-scoped flow resolution no longer
        points at that machine. The ownership check and snapshot read happen
        under the same lock.

        Otherwise returns a dict with:

        * ``delivery`` — one of three states:
          - ``"not_modified"`` — *after* is a valid token whose offset already
            equals the record count AND *known_signature* matches the current
            bundle signature, so the client is provably in sync. ``records`` is
            empty; this is the extra-small idle-poll reply the self-heal path
            gates on. Only ever returned when *known_signature* is supplied — an
            older client that echoes only a token still gets a (records-empty)
            ``"delta"`` here, so the new state is opt-in and backward compatible.
          - ``"delta"`` — a valid token with an in-range offset behind the
            record count; ``records`` holds only the tail after that offset.
          - ``"full"`` — every fallback (no / malformed / stale token,
            out-of-range offset, generation or machine mismatch); ``records``
            holds the complete bundle.
        * ``progress`` — a fresh opaque token pinned to this snapshot's
          generation, machine and record count, for the client to echo on its
          next reconnect.
        * ``signature`` — the bundle's short content-version signature (see
          :func:`bundle_signature`), for the client to echo back as
          *known_signature* so the server can answer ``not_modified`` cheaply.
        """
        async with self._lock:
            cached = self._history_data.get(flow_id)
            if cached is None:
                return None
            bundle_machine = str(cached.get("machine_id") or "")
            if (
                expected_machine_id is not None
                and bundle_machine != expected_machine_id
            ):
                # The cached bundle was produced by a different daemon than the
                # one that currently owns the flow; treat it as a miss so the
                # route re-pulls the authoritative records and returns full.
                return None
            if expected_owner is not None:
                resolved_machine = self._find_machine_for_history_flow_locked(
                    flow_id, owner=expected_owner
                )
                if (
                    resolved_machine is None
                    or resolved_machine != expected_machine_id
                ):
                    return None
            records = cached["records"]
            # Back-fill a stable generation for an old-format bundle on first
            # contact (see ``_ensure_generation``); a positive generation is
            # returned unchanged. This makes the token minted here durable, so a
            # reconnecting client echoing it gets a delta instead of being pinned
            # to the full fallback forever.
            generation = self._ensure_generation(cached)
            total = len(records)

            token = decode_progress(after, secret=self._history_progress_secret)
            # Bind the delta to the exact bundle generation + machine, and clamp
            # the offset into ``[0, total]`` so an out-of-range / forged offset
            # can never slice past the records (which would silently drop the
            # head). Any failed check falls through to the COMPLETE record list
            # below — a delta is served only when the client is provably in sync.
            is_delta = (
                token is not None
                and token["generation"] == generation
                and token["machine_id"] == bundle_machine
                and 0 <= token["offset"] <= total
            )
            signature = bundle_signature(generation, total, bundle_machine)
            if is_delta:
                out_records = list(records[token["offset"]:])
                # No new records AND the client echoed a signature that still
                # matches ⇒ the extra-small not-modified reply. Gated on a
                # supplied signature so a legacy client (token only) keeps
                # getting the records-empty ``delta`` it already handles — the
                # new state never reaches a consumer that cannot interpret it.
                if (
                    token["offset"] == total
                    and known_signature is not None
                    and known_signature == signature
                ):
                    delivery = "not_modified"
                else:
                    delivery = "delta"
            else:
                # Full fallback MUST carry the whole bundle — never a slice — so
                # the client rebuilds a record set identical to the on-disk jsonl.
                out_records = list(records)
                delivery = "full"
                # De-latch on a served full snapshot. A cached bundle only ever
                # comes into existence through an authoritative full/replace
                # frame, so returning its COMPLETE record set means this client
                # now holds authoritative history for the flow. Any lingering
                # ``requires_full`` flag (which would keep silently discarding
                # every later append) MUST clear here, and the self-heal recovery
                # marker MUST re-arm, so the front-end's periodic full pull is
                # itself a latch-clearing event rather than depending on the
                # daemon happening to push a fresh ``full`` frame. This is the
                # third, independent de-latch path (alongside the append_history
                # full branch and the ws.py recovery pull): together they ensure
                # no flow can stay frozen behind a stale flag until the user
                # exits and re-enters the chat. It is a no-op on the common path
                # (a present bundle already implies the flag is clear), but keeps
                # the "full served ⇒ not latched" invariant true unconditionally.
                self._history_requires_full.discard(flow_id)
                self._history_recovery_inflight.pop(flow_id, None)

            return {
                "flow_id": cached["flow_id"],
                "machine_id": bundle_machine,
                "mode": cached.get("mode", ""),
                "delivery": delivery,
                "records": out_records,
                "progress": encode_progress(
                    generation,
                    total,
                    bundle_machine,
                    secret=self._history_progress_secret,
                ),
                "signature": signature,
                "cursor": dict(cached.get("cursor") or {}),
                "updated_at": cached.get("updated_at"),
            }

    async def find_machine_for_history_flow(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> Optional[str]:
        """Resolve which machine owns *flow_id* for an on-demand history pull.

        Checks the reported history index first, then any cached history
        bundle's owner, then the live flow set — so a flow can be pulled
        whether it is historical or still active. With *owner* set, a candidate
        machine is only accepted when it is bound to that owner, so one owner
        cannot pull another owner's history.
        """
        async with self._lock:
            return self._find_machine_for_history_flow_locked(flow_id, owner=owner)

    def _find_machine_for_history_flow_locked(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> Optional[str]:
        """Locked implementation of :meth:`find_machine_for_history_flow`."""

        def _accept(machine_id: str) -> bool:
            if owner is None:
                return True
            record = self._machines.get(machine_id)
            return record is not None and _owned(record, owner)

        for machine_id, sessions in self._history_index.items():
            if not _accept(machine_id):
                continue
            for session in sessions:
                if str(session.get("flow_id") or "") == flow_id:
                    return machine_id
        cached = self._history_data.get(flow_id)
        if cached is not None and cached.get("machine_id"):
            cached_mid = str(cached["machine_id"])
            if _accept(cached_mid):
                return cached_mid
        for machine_id, record in self._machines.items():
            if not _owned(record, owner):
                continue
            if flow_id in record.flows:
                return machine_id
        return None

    async def get_history_flow_project_root(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> Optional[str]:
        """Resolve the authoritative ``project_root`` a flow runs under.

        This is the single source of truth the on-demand history pull uses to
        tell the daemon *which* root to read, instead of letting the daemon
        guess by scanning its whole project-root registry and taking the first
        root that happens to contain ``se3/history/<flow_id>/``. A worktree-mode
        flow runs its discovery step in the main repo root (writing one
        ``01_discovery`` file there) and every later step under the worktree
        root, so two distinct roots can each contain a ``se3/history/<flow_id>``
        directory; without the authoritative root the daemon's first-match
        heuristic returns the main repo's discovery-only directory and the web
        view freezes after the first step.

        Resolution order, all owner-scoped:

        1. The reported history index (the daemon's ``SessionMeta`` carries the
           authoritative ``project_root`` of each flow's run).
        2. The live flow set (an active flow that has not yet been indexed).

        Returns the non-empty ``project_root`` string, or ``None`` when the
        flow is unknown, owner-scoped out, or has no recorded root (the caller
        then degrades to the legacy empty-``project_root`` behaviour).
        """

        def _accept(machine_id: str) -> bool:
            if owner is None:
                return True
            record = self._machines.get(machine_id)
            return record is not None and _owned(record, owner)

        async with self._lock:
            # 1) History index — the authoritative SessionMeta.project_root.
            for machine_id, sessions in self._history_index.items():
                if not _accept(machine_id):
                    continue
                for session in sessions:
                    if str(session.get("flow_id") or "") == flow_id:
                        root = str(session.get("project_root") or "")
                        if root:
                            return root
            # 2) Fall back to the live flow set.
            for machine_id, record in self._machines.items():
                if not _owned(record, owner):
                    continue
                flow = record.flows.get(flow_id)
                if flow is not None:
                    root = str(flow.project_root or "")
                    if root:
                        return root
            return None

    async def is_active_worktree_flow(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> bool:
        """Whether *flow_id* is a still-running ``--worktree`` isolation flow.

        The history self-heal uses this to decide whether a ``not_modified``
        cache reply for a live flow should be reconciled against the daemon once
        (subject to :meth:`full_pull_throttled`). A running worktree flow's
        discovery step appends round after round inside the worktree; if the live
        push dropped or collided on a round, the server cache freezes at the
        first one and every later poll keeps answering ``not_modified`` — the
        "worktree discovery only shows round 1" symptom. Re-pulling the whole
        bundle from the daemon reconciles it.

        Returns ``True`` when an owner-visible flow with this id is still active
        (``running`` OR ``paused``) AND its ``project_root`` points inside
        ``…/se3/worktrees/<name>`` (:func:`_is_worktree_session_path`). The
        ``paused`` state matters precisely for the failing case: a discovery
        round writes its chat records and then blocks on a human reply/decision
        call, flipping the flow to ``paused`` while the round-2 records are still
        only on the daemon and never reached the server cache. Gating on
        ``running`` alone stranded that pending-reply window — the self-heal was
        skipped exactly when it was needed — so the intermediate chat stayed
        invisible. A terminal ``completed`` / ``failed`` flow, or any
        non-worktree flow, returns ``False`` so the reconcile never fires for an
        ordinary session (which is served entirely from cache exactly as before).
        """
        async with self._lock:
            return self._is_active_worktree_flow_locked(flow_id, owner=owner)

    def _is_active_worktree_flow_locked(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> bool:
        """:meth:`is_active_worktree_flow` for a caller that already holds the lock.

        The history write path (:meth:`apply_history_frame`) must consult the
        same predicate to keep its add-only floor scoped to worktree flows, and
        it runs inside ``self._lock`` — re-entering it would deadlock.
        """
        for record in self._machines.values():
            if not _owned(record, owner):
                continue
            flow = record.flows.get(flow_id)
            if flow is None:
                continue
            if str(flow.status or "").lower() not in ("running", "paused"):
                return False
            return _is_worktree_session_path(flow.project_root)
        return False

    # -- issue mirror (from daemon STATUS_UPDATE snapshots) -----------------

    async def get_issues(
        self,
        *,
        owner: Optional[str] = None,
        machine_id: Optional[str] = None,
        project_root: Optional[str] = None,
        include_closed: bool = False,
        source: Optional[str] = None,
        type_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return issues matching the given filters.

        Issues are an in-memory mirror of the daemon's on-disk YAML files,
        refreshed on every STATUS_UPDATE.  The server never reads issue YAML
        directly — the daemon is the sole persistence boundary.

        With *owner* set, only issues on machines belonging to that owner are
        included.  *machine_id* and *project_root* further narrow the scope.
        *include_closed* controls whether closed/resolved/won't-fix issues
        are included (default: open only).  *source* and *type_filter* are
        exact-match filters on the respective issue fields.
        """
        async with self._lock:
            result: List[Dict[str, Any]] = []
            for mid, by_root in self._issues.items():
                # Owner gate
                if owner is not None:
                    record = self._machines.get(mid)
                    if record is None or not _owned(record, owner):
                        continue
                # Machine gate
                if machine_id and mid != machine_id:
                    continue
                for root, issues in by_root.items():
                    # Project root gate
                    if project_root and root != project_root:
                        continue
                    for iss in issues:
                        # Status gate: open-only by default
                        status = str(iss.get("status") or "open")
                        if not include_closed and status not in (
                            "open", "in-progress"
                        ):
                            continue
                        # Source gate
                        if source and str(iss.get("source") or "") != source:
                            continue
                        # Type gate
                        if type_filter and str(iss.get("type") or "") != type_filter:
                            continue
                        entry = dict(iss)
                        entry.setdefault("machine_id", mid)
                        result.append(entry)
            return result

    async def get_issue_by_id(
        self,
        issue_id: str,
        *,
        owner: Optional[str] = None,
        machine_id: Optional[str] = None,
        project_root: Optional[str] = None,
    ) -> Optional[Tuple[str, str, Dict[str, Any]]]:
        """Find an issue by ID, returning ``(machine_id, project_root, issue)``.

        With *owner* set, only machines belonging to that owner are searched.
        *machine_id* and *project_root* narrow the scope.  Returns ``None``
        when the issue cannot be found within the scoped machines/roots.
        """
        async with self._lock:
            for mid, by_root in self._issues.items():
                if owner is not None:
                    record = self._machines.get(mid)
                    if record is None or not _owned(record, owner):
                        continue
                if machine_id and mid != machine_id:
                    continue
                for root, issues in by_root.items():
                    if project_root and root != project_root:
                        continue
                    for iss in issues:
                        if str(iss.get("id") or "") == str(issue_id):
                            return mid, root, dict(iss)
            return None

    async def find_machine_for_project(
        self,
        project_root: str,
        *,
        owner: Optional[str] = None,
    ) -> Optional[str]:
        """Return the machine id that owns *project_root*, or ``None``.

        Searches the issue mirror for a machine that has reported issues
        for this root.  Falls back to checking ``MachineRecord.project_roots``
        for machines that have no issues but do have the root registered.
        With *owner* set, only machines bound to that owner are candidates.
        """
        async with self._lock:
            # First: check the issue mirror for a direct hit.
            for mid, by_root in self._issues.items():
                if project_root in by_root:
                    if owner is not None:
                        record = self._machines.get(mid)
                        if record is None or not _owned(record, owner):
                            continue
                    return mid
            # Second: check MachineRecord.project_roots.
            for mid, record in self._machines.items():
                if not _owned(record, owner):
                    continue
                if project_root in record.project_roots:
                    return mid
        return None
