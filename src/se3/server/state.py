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
from typing import Any, Dict, List, Optional, Set, Tuple

from se3.daemon import protocol

logger = logging.getLogger(__name__)


def _display_step_id(filename: str) -> str:
    """Map a history cursor key (a bare ``*.jsonl`` filename) to the ``step_id``
    the daemon stamps on the records read out of that file.

    The wire cursor is keyed by physical filename while records are keyed by
    display step id, so the gap check's version-skew fallback
    (:meth:`ServerState._detect_cursor_gap`, used only for a frame that declares
    no ``cursor_base``) can line a frame's records up against its cursor only by
    re-deriving one key from the other. This mirrors
    ``se3.daemon.history._display_step_id`` (the
    ``.from-<branch>`` sidecar marker is KEPT, so a step's primary file and its
    sidecars stay distinct streams); it is duplicated rather than imported to
    keep the server package free of a daemon-internals import for one pure
    string rule.
    """
    idx = filename.find(".jsonl")
    if idx < 0:
        return filename
    stem = filename[:idx]
    suffix = filename[idx + len(".jsonl"):]
    if suffix.startswith(".from-") and len(suffix) > len(".from-"):
        return f"{stem}{suffix}"
    return stem


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
    #: Mirror of the daemon's *persistent* project registry — one
    #: ``{"path", "exists", "active"}`` entry per registered root, including
    #: entries whose directory has vanished (``exists`` False). Distinct from
    #: ``project_roots``, which is the merged active∪registry∪history view the
    #: project pickers use and therefore cannot express "registered but stale".
    #: Empty for a daemon predating the field — the management dialog then
    #: simply shows nothing rather than failing.
    registered_projects: List[Dict[str, Any]] = field(default_factory=list)

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
            "registered_projects": [dict(p) for p in self.registered_projects],
        }
        if include_flows:
            data["flows"] = [f.to_dict() for f in self.flows.values()]
        return data


def _sanitize_registered_projects(raw: Any) -> List[Dict[str, Any]]:
    """Normalize a snapshot's ``registered_projects`` into the three-key shape.

    WHY the defensive regrind rather than passing the payload through: this list
    is rendered directly by the management dialog, and the daemon is a *remote*
    peer whose revision the server does not control. A pre-field daemon sends
    nothing (→ empty list, dialog simply shows no rows), and a malformed or
    extended payload is clipped to exactly ``path``/``exists``/``active`` so no
    unvetted daemon-supplied key ever reaches the browser.
    """
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        if not path:
            continue
        out.append(
            {
                "path": path,
                "exists": bool(entry.get("exists")),
                "active": bool(entry.get("active")),
            }
        )
    return out


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

    #: The frame was a ``full`` snapshot REJECTED as untrustworthy, so its
    #: payload is known-bad and MUST NOT be relayed anywhere. Set in the three
    #: cases the full branch refuses, whose scopes deliberately differ:
    #: an EMPTY frame landing on a non-empty cached bundle is refused for EVERY
    #: flow (a zero-record full can never be a legitimate answer for a flow the
    #: server already holds records for — on the wire it is indistinguishable
    #: from an unresolved history directory); an EMPTY frame landing on NO cached
    #: bundle is refused for an ACTIVE WORKTREE flow, which by definition has
    #: already written a discovery round (there, uniquely, ``resolves_pull`` is
    #: ``False``: there is no correct bundle to hand a waiter, so it ends on its
    #: pull timeout rather than being answered "authoritatively empty"); and a
    #: SHORTER-but-non-empty frame is refused only for an ACTIVE WORKTREE flow
    #: (for an ordinary flow a shrink is legitimate: a retried failed step
    #: rewrites its step jsonl in place). See the ``INVARIANT`` notes in
    #: :meth:`apply_history_frame`.
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
        #: flow. The marker is held for the WHOLE reply: a full pull of a large
        #: active flow drains as a ``full`` head plus dozens of ``append`` tails,
        #: and the head must NOT release the marker or a cursor-gap discard among
        #: the still-arriving tails would arm a rival pull (see the INVARIANT in
        #: :meth:`append_history`'s full branch). The TTL below, an end-session
        #: wipe, or a served-full de-latch release it instead. It is a *timestamp*,
        #: not a bare flag, so a
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
            record.registered_projects = _sanitize_registered_projects(
                snapshot.get("registered_projects")
            )
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
        cursor_base: Optional[Dict[str, Any]] = None,
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
            flow_id,
            mode,
            records,
            cursor=cursor,
            cursor_base=cursor_base,
            machine_id=machine_id,
        )
        return outcome.resolves_pull

    async def apply_history_frame(
        self,
        flow_id: str,
        mode: str,
        records: List[Dict[str, Any]],
        *,
        cursor: Optional[Dict[str, Any]] = None,
        cursor_base: Optional[Dict[str, Any]] = None,
        machine_id: str = "",
    ) -> HistoryWriteOutcome:
        """Cache history *records* for *flow_id* and report what the write did.

        ``mode == "full"`` replaces any cached records — except when the frame
        would leave the flow with LESS history than it truthfully has: an EMPTY
        full frame is refused for ANY flow that already has cached records, and
        also for an ACTIVE WORKTREE flow with no bundle at all (which cannot
        truthfully be empty), while a merely shorter (non-empty) one is refused
        for an ACTIVE WORKTREE flow (see the ``INVARIANT`` notes in the full
        branch). ``mode == "append"`` extends an existing authoritative bundle,
        but only when its cursor shows it CONTINUES that bundle: a frame starting
        past the cached water mark would bake a hole into the history, so it is
        refused and the flow is armed for a self-heal full pull. A first-sighting
        append is ignored and marks the flow as requiring a full pull, because
        it may be only the tail after a server restart. *cursor* is stored
        verbatim for the next incremental pull. *cursor_base* is the frame's
        per-file coverage lower bound (the line the daemon's read started at);
        it is what makes the append continuity check exact, and is empty for a
        version-skewed daemon that does not send one. Purely in-memory.

        ``resolves_pull`` is ``True`` when the cache is left authoritative — it
        populated / extended the bundle, or it was a benign no-op on an already
        correct bundle — and ``False`` when the cache is left WITHOUT a
        trustworthy bundle for this flow: the records were discarded (a
        first-sighting, gapped, or otherwise unanchored append, or a
        cross-machine delta), or an empty full was refused with nothing cached to
        fall back on. An on-demand pull waiter must be resolved only on a ``True``
        result so a racing ignored append cannot prematurely wake the REST
        handler before the daemon's authoritative full reply lands.

        ``rejected_full`` flags a full snapshot refused as untrustworthy (see
        :class:`HistoryWriteOutcome` and the ``INVARIANT`` notes in the full
        branch for the three scopes). Its records are truncated, so no consumer
        may relay them.
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
                # INVARIANT: the server's cached history may never have a HOLE in
                # it — an append whose first line lies BEYOND the cached water
                # mark means the lines in between never arrived, and extending
                # over them would silently pin a head-truncated bundle as
                # authoritative (every later poll then answers ``not_modified``
                # on it, so the loss is permanent — the #287 "the first round is
                # missing and never comes back" symptom). This is the LAST-RESORT
                # invariant of the whole history path: it assumes nothing about
                # the daemon's correctness (a dropped frame, a cursor committed
                # for records that never went out, a reconnect mid-stream) and
                # still detects the loss, because the frame states the line
                # window it covers (``cursor_base`` → ``cursor``). On a gap we
                # take NOTHING from
                # the frame — records and cursor both — and arm ``requires_full``
                # so the receive loop's ``take_recovery_pull`` self-heals the
                # bundle from a fresh full snapshot.
                gap = self._detect_cursor_gap(
                    existing.get("cursor") or {},
                    cursor or {},
                    new_records,
                    cursor_base=cursor_base or {},
                    cache_is_empty=not existing.get("records"),
                )
                if gap is not None:
                    self._history_requires_full.add(flow_id)
                    logger.warning(
                        "hist-diag append_history DISCARD flow=%s reason=cursor-gap "
                        "file=%s existing_cursor=%s incoming_cursor=%s records=%d "
                        "(the frame starts past the cached water mark — the lines "
                        "in between never arrived; flagged requires_full so a "
                        "self-heal full pull rebuilds the bundle)",
                        flow_id, gap, existing.get("cursor") or {},
                        cursor or {}, len(new_records),
                    )
                    return HistoryWriteOutcome(resolves_pull=False)
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
                # INVARIANT: an EMPTY full frame may never be made AUTHORITATIVE
                # for an active (``running``/``paused``) worktree flow — not even
                # when the server holds no bundle for it yet. The older guard
                # below only protected a NON-EMPTY cached bundle, which left the
                # most fragile moment unprotected: the very first bundle. An
                # active worktree flow has by definition already written at least
                # one discovery round, so "this flow has no records" cannot be a
                # truthful answer for it — and on the wire it is indistinguishable
                # from the daemon's read failure (``read_flow`` returns
                # ``mode=full, records=[]`` when it resolves no history dir).
                # Accepting one pinned an empty bundle as authoritative AND
                # cleared ``requires_full`` / the recovery marker below, DISARMING
                # the self-heal — so the flow's head was lost for good. Instead:
                # take nothing, keep no bundle (so a REST read stays a cache miss
                # and re-pulls), keep the flow armed for a full pull, and report
                # ``rejected_full`` so the frame is relayed nowhere.
                # ``resolves_pull=False``: a REST waiter parked on this pull must
                # NOT be woken with "authoritatively empty" — it is left to end on
                # the existing ``HISTORY_PULL_TIMEOUT`` path (504, client retries)
                # rather than being handed a blank chat as a final answer.
                if (
                    not new_records
                    and not (existing and existing.get("records"))
                    and self._is_active_worktree_flow_locked(flow_id)
                ):
                    if existing is not None:
                        del self._history_data[flow_id]
                    self._history_requires_full.add(flow_id)
                    logger.warning(
                        "hist-diag append_history REJECTED-empty-full flow=%s "
                        "machine=%s (no cached records to fall back on; an active "
                        "worktree flow cannot legitimately have zero records, so "
                        "the frame is treated as an unresolved daemon read — no "
                        "authoritative empty bundle established, requires_full "
                        "kept armed for a self-heal full pull)",
                        flow_id, machine_id,
                    )
                    return HistoryWriteOutcome(
                        resolves_pull=False, rejected_full=True
                    )
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
                # INVARIANT: a ``full`` frame that answers an IN-FLIGHT recovery
                # pull MUST NOT clear the recovery marker here — the marker stays
                # armed for the whole drain window and is released only when it
                # ages past ``_HISTORY_RECOVERY_TTL`` (or the flow ends /
                # machine-changes / a served-full de-latch fires).
                #
                # WHY: a full pull of a large active flow does NOT arrive as one
                # frame. The owning daemon drains it as a ``full`` HEAD followed by
                # dozens of ``append`` TAILS (a 4.6 MB flow ⇒ 30~39 frames). This
                # branch runs on the HEAD while the tails are still catching up. If
                # we popped the marker here, the dedup window would REOPEN mid-drain:
                # a cursor-gap discard among the still-arriving tails re-arms
                # ``requires_full``, ``take_recovery_pull`` — finding no marker —
                # then dispatches a RIVAL full pull, and the two pulls keep
                # discarding each other's tails forever (the observed periodic
                # ``reason=cursor-gap`` DISCARD ⇄ multi-frame HISTORY_REQUEST
                # livelock). So when a recovery IS in flight, REFRESH the marker to
                # now instead of clearing it, extending the at-most-one-pull dedup
                # across the entire drain; the TTL still guarantees an eventual
                # re-arm if the drain never converges, so no flow is permanently
                # wedged. When NO recovery was in flight this full is a fresh
                # REST/push reply rather than a recovery drain head — leave the
                # marker absent, exactly as before, so a later genuine desync can
                # arm its own recovery immediately.
                if flow_id in self._history_recovery_inflight:
                    self._history_recovery_inflight[flow_id] = time.monotonic()
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
        cursorless — hence ``full`` — pull) to the owning daemon. Its reply
        repopulates the bundle and clears ``requires_full`` via
        :meth:`append_history`, after which subsequent appends apply and
        broadcast normally. Every later discarded append for the same still-stuck
        flow returns ``False`` so a per-cycle append storm cannot fan out one
        request per frame.

        The in-flight marker deliberately survives the reply's ``full`` HEAD
        frame and is NOT cleared by :meth:`append_history` there: a large active
        flow's full pull drains as one ``full`` head plus dozens of ``append``
        tails, and releasing the marker on the head reopens the dedup window for
        the whole tail-draining span — a cursor-gap discard among those tails
        would then arm a RIVAL pull and the two pulls livelock, discarding each
        other's tails. So a second recovery for the same flow is suppressed for
        the entire drain; only the TTL below (or the flow ending) re-arms it.

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

    def _detect_cursor_gap(
        self,
        existing_cursor: Dict[str, Any],
        incoming_cursor: Dict[str, Any],
        records: List[Dict[str, Any]],
        *,
        cursor_base: Optional[Dict[str, Any]] = None,
        cache_is_empty: bool = False,
    ) -> Optional[str]:
        """Return the first file whose append delta starts PAST the cached water
        mark, or ``None`` when the frame is contiguous with the bundle.

        The history cursor is a per-file ``{jsonl-filename: consumed-line-count}``
        water mark. A frame states the window it covers per file:
        ``[cursor_base[f], cursor[f])``. With the cache at line *n*, a frame whose
        window for *f* starts past *n* means the lines in between were never
        delivered — a hole.

        The frame's own *cursor_base* is the ONLY sound source for that start
        line. It cannot be re-derived from the records, because the cursor counts
        every PHYSICAL line the daemon consumed while only parseable dict lines
        become records: a delta that stepped over a blank or mid-write line
        carries fewer records than its cursor advanced, and its first record's
        ordinal sits past the cached water mark even though nothing was lost.
        Inferring the start from ``cursor - len(records)`` (or from the lowest
        ordinal) therefore condemns a perfectly contiguous frame, discards a live
        delta and fires a needless recovery pull — the console stalls until the
        full round-trips. So when the frame declares its base we trust it, and a
        file whose water advanced with NO declared window is a gap by definition:
        the daemon consumed those lines without ever putting them on the wire.

        The count-derived estimate survives only as the fallback for a frame that
        declares no base at all (a version-skewed daemon, a synthetic frame). It
        over-reports on skipped lines, but its failure mode is a redundant full
        pull rather than a baked-in hole.

        Overlap (a window starting at or before *n*, and the ``m <= n`` rollback)
        is deliberately NOT a gap. It is the normal shape of two legitimate flows:
        a daemon that re-reads and re-sends a frame whose previous send failed
        (its cursor only advances on a successful send), and a retried FAILED step
        that rewrites its jsonl in place. Both re-deliver records the bundle
        already holds, and the frontend's ``dedupeAppendRecords`` collapses the
        duplicates — whereas forcing a full pull on every overlap would turn a
        routine retry into a pull storm. Only a FORWARD jump loses information.

        A file the cache carries no water mark for is at line 0 — a step file the
        flow only just created starts there, and an append that instead starts
        mid-file has lost that file's head exactly like any other gap. The one
        exception: a bundle that holds records under a WHOLLY empty cursor was
        built by a cursorless full frame, so its water marks are *unknown* rather
        than zero and no file of it can be judged. An EMPTY bundle
        (*cache_is_empty*) is judged normally — it holds nothing, so every file's
        water mark really is 0.
        """
        if not isinstance(incoming_cursor, dict):
            return None
        if not existing_cursor and not cache_is_empty:
            return None
        record_list = [r for r in records if isinstance(r, dict)]
        starts: Dict[str, int] = {}
        counts: Dict[str, int] = {}
        for record in record_list:
            key = str(record.get("step_id") or "")
            counts[key] = counts.get(key, 0) + 1
            ordinal = record.get("ordinal")
            if isinstance(ordinal, int) and not isinstance(ordinal, bool):
                prior = starts.get(key)
                if prior is None or ordinal < prior:
                    starts[key] = ordinal
        cursor_keys = {_display_step_id(str(name)) for name in incoming_cursor}
        attributable = bool(record_list) and all(
            key in cursor_keys for key in counts
        )

        advanced: List[Tuple[str, str, int, int]] = []
        for name, raw_water in incoming_cursor.items():
            try:
                incoming_water = int(raw_water)
            except (TypeError, ValueError):
                continue
            raw_cached = (existing_cursor or {}).get(name)
            if raw_cached is None:
                cached_water = 0
            else:
                try:
                    cached_water = int(raw_cached)
                except (TypeError, ValueError):
                    continue
            if incoming_water <= cached_water:
                continue
            advanced.append(
                (str(name), _display_step_id(str(name)), cached_water,
                 incoming_water)
            )
        if not advanced:
            return None

        bases: Dict[str, int] = {}
        if isinstance(cursor_base, dict):
            for name, raw_base in cursor_base.items():
                try:
                    bases[str(name)] = int(raw_base)
                except (TypeError, ValueError):
                    continue

        if bases:
            # The frame declared its coverage windows: judge every advanced file
            # against the one it declared, and treat a file it advanced but never
            # opened a window for as a gap (those lines went nowhere).
            for name, _key, cached_water, _incoming_water in advanced:
                start = bases.get(name)
                if start is None or start > cached_water:
                    return name
            return None

        if not attributable:
            total_advance = sum(m - n for _, _, n, m in advanced)
            if total_advance > len(record_list):
                return advanced[0][0]
            return None

        for name, key, cached_water, incoming_water in advanced:
            # Prefer the record's own ``ordinal`` (its 0-based physical line
            # number, which a full read of the same file reproduces identically)
            # as the frame's start line; fall back to the count-derived ``m - r``
            # when a record carries none.
            start = starts.get(key)
            if start is None:
                start = incoming_water - counts.get(key, 0)
            if start > cached_water:
                return name
        return None

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

    async def get_history_bundle_meta(
        self, flow_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the bundle's authoritative ``{cursor, signature, generation,
        total, machine_id, pending}`` for *flow_id*, or ``None`` on a cache miss.

        WHY: a WS ``history_data`` frame historically carried only its records,
        so a client that received a partial stream (an append it joined
        mid-flight, or a frame that landed while it was still at the login gate)
        had NO way to tell that the bundle holds records it never got. The
        ``cursor`` — per-step-file record counts — is the only authoritative
        statement of what the bundle contains, and the client checks its held
        ``stepId#ordinal`` set against it. Pushing it with every frame is what
        makes the push path self-checkable at all; the ``signature`` lets the
        client tell WHICH bundle generation the counts describe, so counts from
        a superseded bundle can never be mistaken for the current one.

        The values are exactly those :meth:`get_history_snapshot` would return
        for the same bundle at the same moment — one source of truth, so the
        push and poll paths can never disagree about what the client should
        hold.
        """
        async with self._lock:
            cached = self._history_data.get(flow_id)
            if cached is None:
                return None
            generation = self._ensure_generation(cached)
            total = len(cached["records"])
            machine = str(cached.get("machine_id") or "")
            pending = self._bundle_pending_positions(
                cached["records"], cached.get("cursor") or {}
            )
            return {
                "cursor": dict(cached.get("cursor") or {}),
                "signature": bundle_signature(generation, total, machine),
                "generation": generation,
                "total": total,
                "machine_id": machine,
                # WHY on the push meta too: the WS frame carries the same cursor
                # the REST snapshot does, so it must carry the SAME pending window
                # — otherwise a client self-checking off a pushed frame would draw
                # the pending/unfillable line differently from one that polled.
                "pending": {k: list(v) for k, v in pending.items()},
            }

    @staticmethod
    def _record_ordinal(record: Any) -> Optional[int]:
        """The record's 0-based per-step line ordinal, or ``None`` if it has none.

        Envelope-first (that is where the daemon history reader stamps it), with
        a ``message.ordinal`` fallback for an already-unwrapped shape — mirroring
        the frontend's ``recordOrdinal`` so both sides agree on which records are
        addressable by ``step_id#ordinal`` and which are legacy/echo records that
        are not.
        """
        if not isinstance(record, dict):
            return None
        value = record.get("ordinal")
        if value is None:
            message = record.get("message")
            if isinstance(message, dict):
                value = message.get("ordinal")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    @classmethod
    def _index_records_by_ordinal(
        cls, records: List[Any]
    ) -> Dict[Tuple[str, int], int]:
        """Map ``(step_id, ordinal) -> position`` over a bundle's flat records.

        WHY positions rather than the records themselves: the backfill slice must
        be emitted in **bundle order** and unioned with the token's tail without
        duplicating a record that appears in both, and positions make that a
        plain set union over indices.

        Records carrying no ordinal (optimistic echoes, pre-ordinal daemons) are
        simply absent from the index — they are not addressable by number, so
        they can never be named by a client nor mis-bound to a neighbour.
        """
        index: Dict[Tuple[str, int], int] = {}
        for position, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            step_id = record.get("step_id")
            ordinal = cls._record_ordinal(record)
            if not step_id or ordinal is None:
                continue
            index.setdefault((str(step_id), ordinal), position)
        return index

    @classmethod
    def _unnumbered_steps(cls, records: List[Any]) -> Set[str]:
        """The step ids holding at least one record that carries no ordinal.

        Such a step's records are only PARTIALLY addressable by number, so a
        number that fails to resolve there says nothing about whether the bundle
        holds the record — see :meth:`_locate_missing_positions`.
        """
        steps: Set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            step_id = record.get("step_id")
            if step_id and cls._record_ordinal(record) is None:
                steps.add(str(step_id))
        return steps

    @classmethod
    def _bundle_pending_positions(
        cls, records: List[Any], cursor: Dict[str, Any]
    ) -> Dict[str, List[int]]:
        """Numbers the bundle's ``cursor`` DECLARES exist but has not received yet.

        Returns ``{step_id: [ordinal, …]}`` for the ordinals in ``0..cursor-1``
        that lie ABOVE every record the bundle currently holds for that step —
        the daemon has advanced the file's physical-line count but its records
        have not caught up to it, so those numbers are *waiting for the daemon*
        rather than provably absent.

        WHY this is a distinct verdict from ``unfillable`` (see
        :meth:`_locate_missing_positions`): both describe a number the bundle
        holds no record for, but their causes are opposite and the client must
        act on them oppositely. An ``unfillable`` number is one the bundle can
        PROVE it will never hold — a blank / unparseable physical line the daemon
        stepped over, which shows up as a hole BELOW a later record it did
        deliver (both neighbours are present, so the gap between them is
        permanent). A ``pending`` number is one the bundle has simply not been
        SENT — it lies past the highest ordinal delivered for the step, in the
        trailing window the daemon is still streaming (the livelock shape: the
        cursor says 815 lines, only the first tens of records have crossed a
        short-lived connection). Declaring a pending number unfillable would have
        the client retire a record that is genuinely on its way; leaving a
        permanent blank named pending would have it wait forever. So the split
        is drawn at the highest ordinal held: interior holes are unfillable,
        the trailing declared-but-undelivered window is pending.

        A step that carries ANY un-numbered record is skipped: its ordinals are
        not a sound completeness signal, and such a request already escalates to
        ``needs_full`` in :meth:`_locate_missing_positions` — so a numbered
        pending claim there could name a record the bundle holds un-numbered.

        A ``full`` bundle carries no cursor (the daemon reports line counts only
        on incremental reads), so ``cursor`` is empty and pending is ``{}`` —
        the pre-existing behaviour where such a bundle names nothing pending.
        Cursor keys are physical filenames folded through :func:`_display_step_id`.
        """
        if not cursor:
            return {}
        highest: Dict[str, int] = {}
        unnumbered: Set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            step_id = record.get("step_id")
            if not step_id:
                continue
            step_id = str(step_id)
            ordinal = cls._record_ordinal(record)
            if ordinal is None:
                unnumbered.add(step_id)
            elif ordinal > highest.get(step_id, -1):
                highest[step_id] = ordinal
        pending: Dict[str, List[int]] = {}
        for key, total in cursor.items():
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                continue
            step_id = _display_step_id(str(key))
            if step_id in unnumbered:
                continue
            start = highest.get(step_id, -1) + 1
            if start < total:
                pending[step_id] = list(range(start, total))
        return pending

    @classmethod
    def _locate_missing_positions(
        cls,
        records: List[Any],
        missing: Dict[str, List[int]],
        pending: Optional[Dict[str, List[int]]] = None,
    ) -> Tuple[List[int], Dict[str, List[int]], bool]:
        """Resolve a client's missing ``(step_id, ordinal)`` list against the bundle.

        Returns ``(positions, unfillable, needs_full)``: the bundle positions of
        the numbers that DO exist, ``{step_id: [ordinal, …]}`` for the numbers the
        bundle provably holds no record for, and a flag demanding the whole bundle
        instead of a numbered slice.

        WHY an unlocatable number is *declared* rather than escalated to a full
        rebuild: a number below a file's cursor need not name a record at all.
        The cursor counts PHYSICAL LINES (the daemon advances it past blank /
        unparseable lines, and a read resumed at ``cursor_base > 0`` never emits
        the lines below that base), so a bundle can be complete and still hold no
        record at some number under its own cursor. Rebuilding serves the very
        same bundle back, which still lacks the number — the client would
        re-detect the identical hole on the next signal and re-spend its budget
        forever. Naming the unfillable numbers instead lets the client retire
        them from its self-check, so a permanent, legitimate gap costs exactly
        one round-trip for the life of the flow instead of a request storm.

        INVARIANT: only a number the bundle DEMONSTRABLY holds no record for may
        be declared unfillable — a record the bundle does hold must always reach
        the client. In a step that also carries un-numbered records (a pre-ordinal
        daemon, an echo), a failed index lookup is ambiguous: the record may well
        be sitting there un-numbered. Declaring it unfillable would have the
        client retire a number whose record exists, re-creating the very
        head-loss this repair path exists to close. So such a request escalates to
        ``needs_full`` — one whole-bundle delivery, which renders every record
        including the un-numbered ones — instead of a slice that silently omits it.

        WHY *pending* is subtracted from unfillable: a number the bundle has not
        yet been SENT (past the highest ordinal delivered for its step — see
        :meth:`_bundle_pending_positions`) is not a number the bundle can prove
        it will never hold. Declaring it unfillable would have the client retire a
        record still in flight from the daemon, so a pending number is left OUT of
        unfillable — the caller reports it under ``pending`` instead, where the
        client keeps waiting for the increment rather than giving up.

        Cursor keys are physical ``*.jsonl`` filenames while records are keyed by
        display step id, so a key arriving in either form is folded through
        :func:`_display_step_id` (a no-op on a bare step id).
        """
        index = cls._index_records_by_ordinal(records)
        unnumbered = cls._unnumbered_steps(records)
        pending_sets: Dict[str, Set[int]] = {
            step_id: set(ordinals)
            for step_id, ordinals in (pending or {}).items()
        }
        positions: List[int] = []
        unfillable: Dict[str, List[int]] = {}
        needs_full = False
        for key, ordinals in missing.items():
            step_id = _display_step_id(str(key))
            for ordinal in ordinals:
                position = index.get((step_id, ordinal))
                if position is not None:
                    positions.append(position)
                elif step_id in unnumbered:
                    needs_full = True
                elif ordinal in pending_sets.get(step_id, ()):
                    # Not yet delivered by the daemon (a trailing declared-but-
                    # unsent number), so it is NOT unfillable — it travels back
                    # under ``pending`` and the client keeps waiting for it.
                    continue
                else:
                    unfillable.setdefault(step_id, []).append(ordinal)
        return positions, unfillable, needs_full

    async def get_history_snapshot(
        self,
        flow_id: str,
        *,
        after: Optional[str] = None,
        expected_machine_id: Optional[str] = None,
        expected_owner: Optional[str] = None,
        known_signature: Optional[str] = None,
        missing: Optional[Dict[str, List[int]]] = None,
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
          - ``"backfill"`` — a valid token PLUS a non-empty *missing* list: the
            client's own cursor self-check found records it never received.
            ``records`` holds exactly those numbered records (∪ the tail after
            the token's offset), taken from the SAME bundle in bundle order, and
            ``unfillable`` names the requested numbers this bundle holds no
            record for (see :meth:`_locate_missing_positions`).
          - ``"full"`` — every fallback (no / malformed / stale token,
            out-of-range offset, generation or machine mismatch, or a *missing*
            number the bundle cannot answer for unambiguously because the step
            also holds un-numbered records); ``records`` holds the complete
            bundle.
        * ``progress`` — a fresh opaque token pinned to this snapshot's
          generation, machine and record count, for the client to echo on its
          next reconnect.
        * ``signature`` — the bundle's short content-version signature (see
          :func:`bundle_signature`), for the client to echo back as
          *known_signature* so the server can answer ``not_modified`` cheaply.
        * ``pending`` — ``{step_id: [ordinal, …]}`` the bundle's cursor DECLARES
          but has not yet received (see :meth:`_bundle_pending_positions`),
          present on EVERY delivery. It is the counterpart of ``unfillable``: a
          cursor gap the client finds is *pending* (still streaming from the
          daemon → keep waiting) when it falls here, and *unfillable* (a proven
          hole → retire it) otherwise. Empty ``{}`` whenever the records cover the
          cursor — so an in-sync bundle names nothing pending.
        * ``resync`` — ``True`` only when the client presented a signed cursor
          (*after*) the server could not bind to the current bundle (expired /
          tampered / a stale generation or machine from a daemon-reconnect bundle
          rotation), so the delivery fell back to ``"full"``. It marks that reply
          as a *recoverable resync*: the client MUST adopt this snapshot's
          authoritative ``progress`` / ``signature`` / ``generation`` and stop
          re-presenting the dead cursor. A stale cursor never reaches
          ``require_owner`` (cookie-only, resolved first) so it can NEVER 401 —
          this marker is how the poll degrades gracefully instead. ``False`` on a
          first-load full (no *after*) and on every honoured delta / not_modified
          / backfill.

        *missing* is ``{step_id: [ordinal, …]}`` — the records the client's own
        cursor self-check found it does NOT hold. WHY it exists at all: the
        progress token's offset is the server's self-signed claim of what it
        SENT, which cannot witness what the client KEPT — a record dropped in
        flight leaves a hole the token can never see, and ``not_modified``
        then locks that hole in forever. The authoritative per-file record count
        (``cursor``) is what the client checks itself against, and *missing* is
        how it names the numbers that check turned up. The token's minting
        semantics are deliberately UNCHANGED (``offset`` still means "records in
        this bundle"), so the delta / not_modified state machine is untouched:
        backfill is an extra read of the same bundle, not a new token dialect.
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
            # WHY: a signed cursor the client PRESENTED but the server could not
            # bind to the current bundle — malformed / unsigned / tampered
            # (``token is None``), or a stale generation / different machine /
            # out-of-range offset from a daemon-reconnect bundle rotation — is a
            # *recoverable* miss, not an auth failure. It never reaches
            # ``require_owner`` (which is cookie-only and runs BEFORE this) so it
            # can never 401; it simply falls through to the full fallback below.
            # But a bare full is indistinguishable from a first-ever load, so a
            # client cannot tell "your cursor was rejected, resync to the
            # authoritative one" from a routine rebuild and may loop re-presenting
            # the same dead cursor. ``resync`` names that case explicitly: it is
            # set only when a non-empty ``after`` was offered yet did not validate
            # as a delta base, so the client adopts this reply's authoritative
            # ``progress``/``signature``/``generation`` and stops retrying the
            # stale cursor. Captured here, BEFORE the ``missing``-needs-full
            # demotion below, so a genuinely valid token demoted only because a
            # backfill could not be answered from the index is NOT mislabelled a
            # stale cursor.
            resync = bool(after) and not is_delta
            signature = bundle_signature(generation, total, bundle_machine)
            # A backfill is served only ON TOP of a valid token (generation +
            # machine + in-range offset): the numbers the client is naming are
            # only meaningful within the bundle its cursor came from, so a token
            # that no longer binds this bundle invalidates the numbering too and
            # must rebuild rather than pick records out by index.
            # The bundle's own pending window — ordinals its cursor declares but
            # its records have not caught up to (see _bundle_pending_positions).
            # Computed on EVERY delivery (empty for a full bundle, whose cursor is
            # itself empty) so a client that finds a cursor gap can tell a number
            # still streaming from the daemon apart from a permanent hole WITHOUT
            # a second round trip, and so this poll and the WS push (which reads
            # the same bundle via get_history_bundle_meta) can never disagree.
            pending = self._bundle_pending_positions(records, cached.get("cursor") or {})
            backfill_positions: List[int] = []
            unfillable: Dict[str, List[int]] = {}
            served_backfill = False
            if is_delta and missing:
                (
                    backfill_positions,
                    unfillable,
                    needs_full,
                ) = self._locate_missing_positions(records, missing, pending)
                if needs_full:
                    # A requested number landed in a step that also holds
                    # un-numbered records, so its absence from the index is not
                    # evidence the bundle lacks the record. Demote to the full
                    # delivery — it carries every record, numbered or not — rather
                    # than answer with a slice that would omit a record we HOLD and
                    # have the client retire its number forever.
                    is_delta = False
                    backfill_positions, unfillable = [], {}
                else:
                    served_backfill = True
            if is_delta:
                out_records = list(records[token["offset"]:])
                # No new records AND the client echoed a signature that still
                # matches ⇒ the extra-small not-modified reply. Gated on a
                # supplied signature so a legacy client (token only) keeps
                # getting the records-empty ``delta`` it already handles — the
                # new state never reaches a consumer that cannot interpret it.
                if served_backfill:
                    # The named records ∪ the token's tail, emitted in bundle
                    # order and de-duplicated, so a number that also lies in the
                    # tail travels exactly once and the client can merge the
                    # reply by ``step_id#ordinal`` without ordering surprises.
                    # Numbers that name no record travel back in ``unfillable``
                    # instead — the reply is still a backfill, just a partial one
                    # the client can reason about (see _locate_missing_positions).
                    wanted = sorted(
                        set(backfill_positions) | set(range(token["offset"], total))
                    )
                    out_records = [records[i] for i in wanted]
                    delivery = "backfill"
                elif (
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
                # every later append) MUST clear here, so the front-end's
                # periodic full pull is itself a latch-clearing event rather than
                # depending on the daemon happening to push a fresh ``full``
                # frame. This is the third, independent de-latch path (alongside
                # the append_history full branch and the ws.py recovery pull):
                # together they ensure no flow can stay frozen behind a stale
                # flag until the user exits and re-enters the chat. It is a no-op
                # on the common path (a present bundle already implies the flag
                # is clear), but keeps the "full served ⇒ not latched" invariant
                # true unconditionally.
                self._history_requires_full.discard(flow_id)
                # INVARIANT: this REST full-serve MUST NOT pop an IN-FLIGHT
                # recovery marker — mirror the append_history full branch and
                # only REFRESH it while a recovery drain is running.
                #
                # WHY: a recovery pull of a large active flow drains as a ``full``
                # HEAD followed by dozens of ``append`` TAILS. The HEAD rolls a
                # fresh bundle generation, which invalidates the polling client's
                # progress token, so the WebUI's very next ~3 s poll falls through
                # to this full-fallback delivery WHILE the drain's tails are still
                # arriving. Popping the marker here would REOPEN the at-most-one
                # dedup window mid-drain: a cursor-gap discard among the remaining
                # tails re-arms ``requires_full``, ``take_recovery_pull`` — finding
                # no marker — dispatches a RIVAL full pull, and the two pulls keep
                # discarding each other's tails (the observed periodic cursor-gap
                # DISCARD ⇄ multi-frame HISTORY_REQUEST livelock). Refreshing to
                # now extends the dedup across the whole drain window; the TTL
                # still guarantees an eventual re-arm if the drain never converges.
                # When NO recovery is in flight the pop was a no-op anyway, so
                # leaving the marker absent preserves the prior behaviour exactly.
                if flow_id in self._history_recovery_inflight:
                    self._history_recovery_inflight[flow_id] = time.monotonic()

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
                # The bundle lifecycle id the cursor's counts (and the numbers the
                # client checks against them) describe. WHY the client needs it in
                # the clear: its repair budget and its set of retired-unfillable
                # numbers are facts about ONE bundle, and must be dropped the moment
                # that bundle is replaced — a gap that was legitimately unfillable
                # in the old generation may be a real, servable record in the new
                # one. The signature cannot key that state (it is re-minted on every
                # append), and the token is opaque; the generation is the only stable
                # per-bundle identity the client can see.
                "generation": generation,
                # The numbers of *missing* this bundle holds no record for. Empty
                # on every other delivery, so the reply's key set is uniform.
                "unfillable": {k: list(v) for k, v in unfillable.items()},
                # The numbers the bundle's cursor DECLARES but has not yet been
                # sent (see _bundle_pending_positions) — waiting on the daemon,
                # not permanently absent. Present on every delivery so the client
                # can distinguish "keep waiting" from "give up" for any cursor
                # gap; empty ({}) whenever the bundle's records cover its cursor.
                "pending": {k: list(v) for k, v in pending.items()},
                # True when the client presented a signed cursor (``after``) the
                # server could not bind to this bundle (see ``resync`` above) — a
                # stale/expired/rotated cursor that fell back to full. The reply's
                # ``progress``/``signature``/``generation`` are authoritative, so
                # the client resynchronises to them instead of bare-retrying the
                # dead cursor. False on a first-load full (no ``after``) and on
                # every honoured delta / not_modified / backfill.
                "resync": resync,
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
