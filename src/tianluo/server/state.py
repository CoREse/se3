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
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from tianluo.daemon import protocol

from .history_summary import locate_record_detail

logger = logging.getLogger(__name__)


#: Flat per-record surcharge added to a record's own estimated JSON footprint.
#: WHY it exists: a cached record does not cost only its own containers — the
#: bundle also carries an entry for it in ``_key_index`` (a ``(step_id, ordinal)``
#: tuple keyed dict slot) and, for usage-bearing records, a ``UsageRecord`` in
#: ``_usage_sources``. Those side caches scale with the record count, so folding
#: a flat surcharge into the per-record estimate keeps the accounting honest
#: without walking two more structures on every append.
_RECORD_OVERHEAD_BYTES = 200

#: Depth beyond which :func:`_estimate_json_bytes` stops descending. History
#: records are shallow (envelope → message → content), so anything deeper is
#: either malformed or a cycle; charging a flat cost is safer than recursing.
_ESTIMATE_MAX_DEPTH = 12


def _estimate_str_bytes(value: str) -> int:
    """Upper-bound a string's cost, in bytes, in O(1).

    WHY it must bound TWO different costs: the same estimator feeds the cache
    budget (which cares about RESIDENT bytes) and the render-offload gates
    (which care about the ``ensure_ascii=False`` WIRE bytes those payloads
    serialize to). A single per-char weight of 1 byte is wrong for both once
    the text is not ASCII: this project's own configured language is zh-CN, so
    CJK-dominant records are the normal content, and there PEP 393 stores 2
    bytes/char resident while UTF-8 puts 3 bytes/char on the wire. Charging
    1 byte/char under-counted a real bundle by up to 3x, which let a ~2.7 MB
    CJK response estimate under the 1 MiB gate and take the inline render +
    on-loop gzip path the gate exists to keep it off.

    ``sys.getsizeof`` is O(1) on ``str`` (header + kind x length, no walk) and
    gives the exact resident size; the extra 1 B/char lifts the answer above
    the widest UTF-8 expansion for that kind (latin-1 <=2, BMP <=3, astral <=4
    bytes/char against 1/2/4 stored), so the result bounds the wire size too.
    ASCII is kept on the exact ``49 + len`` form because there both costs
    coincide and it is the hot case for machine/step/flow identifiers.
    """
    if value.isascii():
        return 49 + len(value)
    return sys.getsizeof(value) + len(value)


def _estimate_json_bytes(value: Any, _depth: int = 0) -> int:
    """Approximate the RETAINED memory of a JSON-shaped *value*, in bytes.

    WHY an estimator rather than ``len(json.dumps(value))``: this runs inside
    ``ServerState._lock`` on the append path, and re-serialising the frame there
    would put an O(frame) allocation-heavy step back on the event loop — the
    very stall the cache budget work exists to remove. The walk below allocates
    nothing and answers within a small constant factor, which is all a budget
    needs: the number decides WHEN to evict, never what is served.

    The constants approximate CPython object headers (a dict slot per entry, a
    pointer per list element, see :func:`_estimate_str_bytes` for strings)
    rather than wire bytes, because it is resident memory — not transfer size —
    that gets the process oom-killed. They stay at or above the corresponding
    JSON wire cost (a dict entry is ~4 wire bytes against 100 charged here, an
    int <=20 against 28), so the render-offload gates that reuse this walk read
    an over-estimate of the serialized payload rather than an under-estimate.
    """
    if _depth > _ESTIMATE_MAX_DEPTH:
        return 64
    if value is None or isinstance(value, bool):
        return 16
    if isinstance(value, int):
        return 28
    if isinstance(value, float):
        return 24
    if isinstance(value, str):
        return _estimate_str_bytes(value)
    if isinstance(value, dict):
        total = 64 + 100 * len(value)
        for key, item in value.items():
            total += _estimate_json_bytes(key, _depth + 1)
            total += _estimate_json_bytes(item, _depth + 1)
        return total
    if isinstance(value, (list, tuple)):
        total = 56 + 8 * len(value)
        for item in value:
            total += _estimate_json_bytes(item, _depth + 1)
        return total
    return 64


def _estimate_record_bytes(record: Any) -> int:
    """A single cached history record's estimated resident cost."""
    return _estimate_json_bytes(record) + _RECORD_OVERHEAD_BYTES


def records_reach_bytes(records: Any, threshold: int) -> bool:
    """Whether *records* estimate to at least *threshold* bytes.

    WHY a threshold PREDICATE rather than a total: its callers are the
    server's render-offload gates, which only need to know which side of a line
    a payload falls on, and stopping the walk at the line keeps the check
    O(records up to the line) instead of O(whole bundle). The estimator costs a
    few microseconds per record, which is real money once a bundle runs to
    thousands of them — so the gates short-circuit on a record COUNT first and
    only measure BYTES for payloads too short to have tripped it.

    WHY it exists at all: a record count is not a proxy for payload size. Real
    history records are heavy-tailed (sampled over this repo's own
    ``tianluo/history/``: mean 40.7 KB, p90 12.3 KB, p99 1.1 MB), so a bundle of
    ten records can be 11 MiB and freeze the loop for ~110 ms while a bundle of
    a hundred small ones costs nothing.

    Over-estimating is the safe direction here — it can only route a borderline
    payload onto the loop-friendly path, never off it — and the estimator holds
    that direction for EVERY payload, not just ASCII ones: its per-element
    constants sit above JSON's punctuation cost and its string weight bounds the
    ``ensure_ascii=False`` expansion of non-ASCII text (see
    :func:`_estimate_str_bytes`, which exists because charging 1 B/char let CJK
    bundles under-estimate by 3x and slip past this gate).
    """
    if not isinstance(records, list):
        return False
    if threshold <= 0:
        return True
    total = 0
    for record in records:
        total += _estimate_record_bytes(record)
        if total >= threshold:
            return True
    return False


def _process_rss_bytes() -> Optional[int]:
    """Current process RSS in bytes, or ``None`` where it cannot be read.

    Best-effort and dependency-free (``/proc`` on Linux, ``getrusage`` peak
    elsewhere): the memory-attribution log is a diagnostic, so an unavailable
    reading must degrade to "not reported" rather than raise on a platform the
    server was never deployed to.
    """
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            fields = handle.read().split()
        import os as _os

        return int(fields[1]) * _os.sysconf("SC_PAGE_SIZE")
    except Exception:
        pass
    try:
        import resource

        # ru_maxrss is KiB on Linux and bytes on macOS; the peak is only ever
        # shown as a coarse water mark, so the Linux unit is the useful one.
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except Exception:
        return None


def _display_step_id(filename: str) -> str:
    """Map a history cursor key (a bare ``*.jsonl`` filename) to the ``step_id``
    the daemon stamps on the records read out of that file.

    The wire cursor is keyed by physical filename while records are keyed by
    display step id, so the gap check's version-skew fallback
    (:meth:`ServerState._detect_cursor_gap`, used only for a frame that declares
    no ``cursor_base``) can line a frame's records up against its cursor only by
    re-deriving one key from the other. This mirrors
    ``tianluo.daemon.history._display_step_id`` (the
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

    A ``luo run --worktree`` flow body executes inside, and persists its
    ``engine.json`` under, ``<main_root>/tianluo/worktrees/<name>/``. The daemon
    reports such a live (possibly dangling) run with that path as its
    ``project_root``. This is the **structural** half of the daemon's
    :func:`tianluo.daemon.supervisor.resolve_worktree_main_root` check — the last
    two path segments being ``tianluo/worktrees`` — without the filesystem
    ``<main>/se3`` directory probe, because the server runs on a different host
    than the worktree and cannot stat it. Used by :meth:`ServerState.is_flow_endable`
    so a *completed* worktree session whose follow-up cleanup failed is still
    recognised as having an orphan worktree to archive.
    """
    if not project_root:
        return False
    # Split on both separators and drop empties so trailing slashes don't shift
    # the segment positions; a worktree dir is ``…/tianluo/worktrees/<name>``.
    parts = [seg for seg in str(project_root).replace("\\", "/").split("/") if seg]
    if len(parts) < 3:
        return False
    return parts[-2] == "worktrees" and parts[-3] in ("tianluo", "se3")


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
    (``tianluo.daemon.aggregator.FlowSnapshot``), kept as a plain record so the
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
    # Control-plane projections relayed verbatim from the daemon's aggregator
    # (shared backends with the CLI history view — the server never re-derives
    # them): the plan-mode view (decomposition / granularity / group count),
    # the SELF_CHECK scope audit, and the compact records-free usage/cost
    # summary. ``None`` = the daemon did not (or could not) supply it.
    plan_mode: Optional[Dict[str, Any]] = None
    review_scope: Optional[Dict[str, Any]] = None
    usage_summary: Optional[Dict[str, Any]] = None

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
            plan_mode=(
                data.get("plan_mode")
                if isinstance(data.get("plan_mode"), dict)
                else None
            ),
            review_scope=(
                data.get("review_scope")
                if isinstance(data.get("review_scope"), dict)
                else None
            ),
            usage_summary=(
                data.get("usage_summary")
                if isinstance(data.get("usage_summary"), dict)
                else None
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
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
        # Absent projections stay absent (unknown), never a fabricated empty
        # dict that would read as a confirmed single-group / zero-usage answer.
        if self.plan_mode is not None:
            data["plan_mode"] = self.plan_mode
        if self.review_scope is not None:
            data["review_scope"] = self.review_scope
        if self.usage_summary is not None:
            data["usage_summary"] = self.usage_summary
        return data


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

    #: The frame was dropped because the budget had evicted this flow and no UI
    #: client had read it since (see ``ServerState._history_cold``). The records
    #: went nowhere and no recovery was armed, so a console DISPLAYING this flow
    #: would otherwise be told nothing at all — its own self-check only runs on
    #: an incoming frame. The fan-out layer answers this flag with a records-less
    #: cursor advisory built from the dropped frame's own cursor, which is what
    #: lets such a console notice it is short of records and re-pull over REST —
    #: and that read is exactly what re-admits the flow to the cache.
    cold_suppressed: bool = False

    #: The cursor the suppressed frame declared, carried out so the fan-out can
    #: build that advisory without the caller re-parsing the frame. ``None``
    #: whenever *cold_suppressed* is ``False``.
    suppressed_cursor: Optional[Dict[str, Any]] = None



@dataclass
class _PendingPull:
    """One dispatched ``MSG_HISTORY_REQUEST`` whose reply has not drained yet."""

    #: What the reply's HEAD frame must look like. ``read_flow`` derives the
    #: mode from the request's cursor alone — a cursorless request is answered
    #: with a ``full`` head (then ``append`` tails), a cursor-bearing one with
    #: ``append`` frames only — so this is a fact about our own request, not a
    #: guess about the daemon.
    expects_full: bool
    #: The cursor the request carried (empty for a cursorless pull). The reply
    #: reads FROM it, so its head can only be anchored AT or BEHIND it, never
    #: past it.
    cursor: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _ReplayDrain:
    """The replay markers held for one flow: pulls in flight plus drain state."""

    #: Dispatched pulls whose replies have not finished draining, front first.
    pulls: List[_PendingPull] = field(default_factory=list)
    #: Whether the frontmost pull's reply has actually STARTED arriving. Until
    #: it has, a frame that cannot be that reply's head is live traffic that
    #: raced the dispatch, and must leave the marker alone.
    draining: bool = False
    #: Leak guard for a pull whose reply never arrives at all.
    deadline: float = 0.0
    #: Whether a frame of the reply CURRENTLY draining woke a parked REST
    #: waiter. Set once by that frame and read by every later frame of the same
    #: reply, so the fan-out can tell "these records are already going back to a
    #: REST caller" from "nobody asked for these over REST" (see
    #: :meth:`ServerState.mark_history_reply_served`).
    rest_served: bool = False


@dataclass
class _OpenDelivery:
    """A multi-frame history delivery for one flow that has not finished.

    INVARIANT: while an entry exists here the flow's cached bundle is missing
    the tail of a delivery already in progress, so it may NOT present itself as
    settled — every snapshot of it is flagged ``incomplete`` and, once the
    delivery is provably no longer arriving, the flow is armed for a repair
    pull. WHY the marker is needed at all: a bundle derives its cursor,
    signature and pending window from the frames that ACTUALLY arrived, so a
    reply cut in the middle leaves a perfectly self-consistent PREFIX — cursor
    naming exactly the step files that landed, ``pending`` empty — and neither
    the server, the browser's ``stepId#ordinal`` self-check, nor the daemon
    (which keeps its own delivery cursor across a reconnect and never re-sends)
    can tell the conversation's tail is gone.
    """

    #: The machine whose delivery this is, so a daemon disconnect can stall
    #: exactly the deliveries that die with it.
    machine_id: str
    #: Monotonic time of the last frame of this delivery. A delivery that is
    #: still arriving refreshes it, which is what keeps a healthy multi-MB drain
    #: from being mistaken for an interrupted one.
    last_frame_at: float
    #: Set when the delivery is KNOWN to be dead (the owning daemon's socket
    #: ended) rather than merely quiet, so the repair fires at once instead of
    #: waiting out the stall grace.
    stalled: bool = False


@dataclass
class ReplayVerdict:
    """How one inbound ``HISTORY_DATA`` frame was classified.

    ``replay`` is the shaping verdict the browser leg needs (summarize like the
    REST bundle, or ship whole). ``from_pull`` says the frame belongs to a reply
    the SERVER asked for, and ``rest_served`` that this reply's records are
    already being handed to a REST caller — together they are what lets the
    fan-out drop a delivery the browser is receiving twice.
    """

    replay: bool
    from_pull: bool = False
    rest_served: bool = False
    #: Whether THIS frame retired the pull it answers — the reply's last frame,
    #: after which the bundle is settled and the next frame for the flow is live
    #: traffic again.
    closing: bool = False

    #: Whether the delivery this frame belongs to is still UNFINISHED after it
    #: (see :class:`_OpenDelivery`). Carried out for the receive loop's
    #: diagnostics only — the marker itself lives in ``ServerState`` — so a
    #: journal tail can attribute an ``incomplete`` bundle to the frame that
    #: left it that way.
    delivery_incomplete: bool = False


def _cursor_base_ahead(
    cursor_base: Optional[Dict[str, Any]], requested: Dict[str, Any]
) -> bool:
    """Whether *cursor_base* starts PAST *requested* for at least one step file.

    A pull's reply is read from the cursor the request carried, so every frame
    of it is anchored at or behind that cursor (a file whose cursor the daemon
    had to discard is re-delivered from line 0 — further behind still). A live
    push-loop append, by contrast, is anchored at the daemon's own push水位,
    which in every case that arms a recovery pull sits AHEAD of the water mark
    the server asked from. That asymmetry is what tells the two apart when both
    wear ``mode: append``.

    A version-skewed daemon sends no ``cursor_base`` at all; an empty one claims
    nothing, so it never disqualifies a frame from being the reply's head — the
    safe direction, since a head misread as live would ship its bodies whole.
    """
    if not cursor_base:
        return False
    for name, raw_base in cursor_base.items():
        try:
            base = int(raw_base)
        except (TypeError, ValueError):
            continue
        try:
            want = int(requested.get(name, 0) or 0)
        except (TypeError, ValueError):
            want = 0
        if base > want:
            return True
    return False


def _normalized_cursor(cursor: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A cursor reduced to comparable form (numeric line counts, string keys)."""
    normalized: Dict[str, Any] = {}
    for name, raw in (cursor or {}).items():
        try:
            normalized[str(name)] = int(raw)
        except (TypeError, ValueError):
            normalized[str(name)] = raw
    return normalized


def _same_pull_shape(
    pull: _PendingPull, expects_full: bool, cursor: Optional[Dict[str, Any]]
) -> bool:
    """Whether *pull* was armed for a request of exactly this shape.

    The request shape (cursorless-full vs. read-from-this-cursor) is all that
    distinguishes one armed pull from another — it is what :func:`_frame_opens_reply`
    matches a reply's head against — so it is also how a caller whose send failed
    finds the marker it armed itself, rather than whichever was armed last.
    """
    if pull.expects_full != expects_full:
        return False
    return _normalized_cursor(pull.cursor) == _normalized_cursor(cursor)


def _frame_opens_reply(
    pull: _PendingPull,
    mode_full: bool,
    cursor_base: Optional[Dict[str, Any]],
) -> bool:
    """Whether this frame can be *pull*'s reply HEAD rather than a live append.

    Only ever consulted before a drain has started. Both tests are one-sided on
    purpose: they reject a frame only when the shape of our OWN request proves
    it cannot be the answer, so an ambiguous frame is still read as the head
    (summarized) instead of being released to the browser whole.
    """
    if pull.expects_full:
        return mode_full
    if mode_full:
        return False
    return not _cursor_base_ahead(cursor_base, pull.cursor)


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

    #: Leak guard on a dispatched pull's REPLAY marker (see
    #: :meth:`mark_history_replay`). The marker is normally retired by the
    #: reply's own closing frame, so this only covers a pull whose reply never
    #: arrived at all — the daemon died between the request and its first frame,
    #: or dropped the request on the floor — so a flow cannot stay marked for the
    #: rest of the server's life. It is pushed forward by every frame of a drain,
    #: so a legitimately long multi-MB recovery can never age out mid-reply
    #: however slow the link.
    #:
    #: WHY it is NOT an idle window: the dispatch→first-frame gap is a cold jsonl
    #: read of a multi-MB session (the very latency ``HISTORY_PULL_TIMEOUT`` is
    #: sized for), and the drain→next-live-append gap is one push-loop tick — the
    #: two overlap, so no quiet-time threshold can separate them. A drain is
    #: bounded by COUNTING its frames instead (see :meth:`take_history_replay`).
    _HISTORY_REPLAY_TTL: float = 120.0

    #: Seconds of silence after which a still-unfinished history delivery (see
    #: :class:`_OpenDelivery`) is treated as INTERRUPTED, so a poll may repair
    #: the flow. It is a backstop: the common case — the owning daemon's socket
    #: ending mid-drain — is detected exactly, at disconnect
    #: (:meth:`note_machine_deliveries_interrupted`), and needs no timer.
    #:
    #: WHY it must sit well above the gap BETWEEN a healthy drain's frames: the
    #: daemon reads each chunk off disk in a worker thread and the frames of one
    #: reply arrive back to back, so a second or two is the normal spacing and a
    #: grace this size cannot mistake a slow-but-live drain for a dead one.
    #: Every arriving frame refreshes the marker, so the grace is measured from
    #: the LAST frame, not from the delivery's start — a genuinely long reply
    #: never ages out mid-drain however many frames it takes.
    _HISTORY_DELIVERY_STALL_GRACE: float = 15.0

    #: Minimum seconds between cache-miss ``full`` daemon pulls for the SAME
    #: flow. A self-heal poll that presents a diverged token forces one full
    #: rebuild; without this floor a client stuck presenting the same stale
    #: token would trigger a fresh multi-MB回源 pull on every poll. Sized to a
    #: few poll intervals so a genuine divergence still heals promptly while a
    #: repeated-miss storm collapses onto one in-flight pull.
    _HISTORY_FULL_PULL_MIN_INTERVAL: float = 5.0

    #: Default total budget for all cached history bundles (bytes). Mirrors
    #: ``tianluo.config.DEFAULT_HISTORY_CACHE_BUDGET_MB``; duplicated as a plain
    #: constant so a bare ``ServerState()`` (unit tests, tooling) is bounded too
    #: without importing the config layer.
    _HISTORY_CACHE_BUDGET_BYTES: int = 256 * 1024 * 1024

    #: How recently a flow must have been READ BY A UI CLIENT to count as
    #: "someone is looking at it". Two things hang off it, and both are the
    #: point of the whole eviction design:
    #:
    #: * a hot flow is never evicted, so an open chat pane keeps being served
    #:   from cache and the ``/ws/ui`` fan-out keeps finding its bundle meta;
    #: * a hot flow is never suppressed, so the ``full`` frame answering its
    #:   cache-miss pull is admitted even if the budget is exhausted.
    #:
    #: WHY recency of a UI READ and not of a daemon PUSH: the daemon pushes
    #: history for EVERY active flow whether or not anyone is watching (see
    #: ``DaemonClient._push_history`` / ``read_active_flows``). Keying recency
    #: off the push would refresh every active flow as hot on every tick, the
    #: LRU would degenerate into never evicting, and the unbounded growth this
    #: budget exists to stop would survive intact. The WebUI polls
    #: ``GET /api/history/{flow_id}`` every few seconds for the flow it is
    #: showing, so a UI read IS the per-flow "someone is watching" signal —
    #: ``UiHub`` itself only knows the global 0↔non-0 browser-presence bit and
    #: carries no per-flow subscription state to ask instead.
    #: Sized well above the frontend poll interval (~3 s) so a slow poll or a
    #: momentary stall cannot make a watched flow look abandoned.
    _HISTORY_VIEW_HOT_WINDOW: float = 30.0

    #: Minimum seconds between two cold-flow cursor advisories for the SAME
    #: flow. Well under :attr:`_HISTORY_VIEW_HOT_WINDOW` so a console displaying
    #: an evicted flow learns it moved — and re-pulls, which re-admits it —
    #: long before the flow could be considered abandoned again.
    _HISTORY_COLD_ADVISORY_INTERVAL: float = 5.0

    #: Minimum seconds between two over-threshold occupancy WARNINGs. The check
    #: runs on every cache write, so without a floor a server parked just above
    #: the threshold would emit one line per history frame.
    _HISTORY_REPORT_DEBOUNCE: float = 60.0

    def __init__(
        self,
        *,
        history_cache_budget_bytes: Optional[int] = None,
        history_cache_report_threshold_percent: Optional[int] = None,
    ) -> None:
        self._machines: Dict[str, MachineRecord] = {}
        # History relay caches. The server is a pure in-memory relay for
        # history data — neither of these is ever written to disk.
        #: machine_id -> list of history session-meta dicts (the daemon's
        #: ``luo history`` index).
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
        #: flow_id -> :class:`_ReplayDrain` for history pulls this server
        #: dispatched whose reply has not finished draining.
        #: WHY it exists: the browser leg must summarize every REPLAY of
        #: already-persisted history (see
        #: :mod:`tianluo.server.history_summary`) and leave only genuine
        #: post-subscription tail appends whole — and a pull's reply is NOT one
        #: frame. A history larger than the daemon's per-frame byte budget comes
        #: back as a ``full`` head plus ``append`` tails that are, on the wire,
        #: indistinguishable from the live push loop's appends. The distinction
        #: is the frame's ORIGIN, so the server records the origin it knows: it
        #: asked for this, and it knows the SHAPE of the head that answer must
        #: start with. One entry is armed per dispatched ``MSG_HISTORY_REQUEST``
        #: and retired by that reply's own closing frame (see
        #: :meth:`take_history_replay`); the deadline is only a leak guard for a
        #: request that is never answered at all.
        self._history_replay_pulls: Dict[str, _ReplayDrain] = {}
        #: flow_id -> :class:`_OpenDelivery` for a multi-frame delivery that has
        #: not declared itself finished. This is the completeness half of the
        #: history path: ``_history_replay_pulls`` tracks a pull's IDENTITY (is
        #: this frame a replay we asked for?), this tracks whether what the
        #: sender set out to deliver has actually all landed. They are separate
        #: because a delivery can outlive its identity question — the push
        #: loop's byte-bounded catch-up has backlog behind it with no pull
        #: marker at all — and because the identity marker is retired by the
        #: reply's closing frame precisely when this one must NOT be: a reply
        #: that never sent a closing frame is the case that needs repairing.
        self._history_deliveries: Dict[str, _OpenDelivery] = {}
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
        #: Optional ``machine_id -> bool`` probe of REAL socket connectivity,
        #: injected at app assembly (see :meth:`set_connectivity_probe`).
        #: ``None`` in bare/unit use, where ``MachineRecord.online`` is the only
        #: available truth.
        self._connectivity_probe: Optional[Callable[[str], bool]] = None
        #: Total byte budget for every cached history bundle taken together.
        #: The server mirrors each active flow's WHOLE conversation in RAM and
        #: nothing used to bound that mirror, so a long-lived server in a
        #: memory-capped container was eventually oom-killed. ``0`` is a valid
        #: (degenerate) setting: only flows a UI client is actively reading stay
        #: resident.
        self._history_cache_budget_bytes: int = (
            self._HISTORY_CACHE_BUDGET_BYTES
            if history_cache_budget_bytes is None
            else max(0, int(history_cache_budget_bytes))
        )
        #: Percentage of the budget above which an occupancy report is emitted
        #: at WARNING regardless of the periodic cadence.
        self._history_report_threshold_percent: int = (
            80
            if history_cache_report_threshold_percent is None
            else max(0, int(history_cache_report_threshold_percent))
        )
        #: flow_id -> monotonic time of the last UI-driven read (a REST history
        #: snapshot / bundle read). The eviction recency key — see
        #: :attr:`_HISTORY_VIEW_HOT_WINDOW` for why it is a READ and never a push.
        self._history_read_at: Dict[str, float] = {}
        #: flow_id -> monotonic eviction time of a bundle the budget dropped.
        #: WHY a flow must be REMEMBERED as evicted rather than simply deleted:
        #: the daemon keeps pushing that flow's appends regardless of viewers, and
        #: on the bare-delete path the very next append is a "first sighting",
        #: which arms ``_history_requires_full`` and makes the receive loop pull a
        #: fresh multi-MB full snapshot — which is then evicted again one frame
        #: later. That is an eviction⇄回拉 storm, not a bound. While a flow sits
        #: here, frames that would (re)CREATE its bundle are dropped without
        #: arming any recovery, so an unwatched flow converges to costing nothing.
        #: Re-admission is UI-driven only: a REST read (see
        #: :meth:`_note_history_view`) clears the marker, and the resulting cache
        #: miss rebuilds the bundle through the ordinary cursorless full pull.
        self._history_cold: Dict[str, float] = {}
        #: flow_id -> monotonic time of the last cold-flow cursor advisory, so a
        #: backlog drain against an evicted flow cannot turn one advisory per
        #: bounded chunk into a fan-out storm (see :meth:`_claim_cold_advisory`).
        self._history_cold_advised_at: Dict[str, float] = {}
        #: Lifetime eviction counters, surfaced in the diagnostic report so an
        #: operator can tell "the budget is doing its job" from "the budget was
        #: never reached".
        self._history_evictions: int = 0
        self._history_evicted_bytes: int = 0
        #: Monotonic time of the last over-threshold occupancy WARNING.
        self._history_report_at: float = 0.0
        self._lock = asyncio.Lock()

    def set_connectivity_probe(
        self, probe: Optional[Callable[[str], bool]]
    ) -> None:
        """Inject the live-connection predicate used by flow→machine resolution.

        WHY resolution must NOT read ``MachineRecord.online``: that flag is a
        *presence-display* value, deliberately debounced by
        :class:`~tianluo.server.ws.PresenceDebouncer` (60 s in production) so a
        lossy-link reconnect does not flap the WebUI badge. For a whole minute
        after a daemon dies its record therefore still says ``online=True``,
        which would put a machine that cannot be reached at all into the
        preferred segment and let it keep shadowing the machine that just took
        the shared filesystem over — the exact 404 the online-first resolution
        exists to remove. Routing needs *reachability now*, so the app wires
        ``ConnectionManager.is_connected`` in here; the presence flag remains
        the fallback for a bare :class:`ServerState` (unit tests, tooling) that
        has no connection manager to ask.
        """
        self._connectivity_probe = probe

    def _machine_is_reachable(
        self, machine_id: str, record: Optional["MachineRecord"]
    ) -> bool:
        """Whether *machine_id* can be reached RIGHT NOW by a dispatched frame.

        Prefers the injected connectivity probe over the debounced
        ``record.online`` presence flag (see :meth:`set_connectivity_probe`); a
        probe failure degrades to the flag rather than declaring the whole fleet
        unreachable.
        """
        probe = self._connectivity_probe
        if probe is not None:
            try:
                return bool(probe(machine_id))
            except Exception:  # pragma: no cover - defensive
                logger.debug("connectivity probe failed for %s", machine_id)
        return record is not None and bool(record.online)

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
        # Read (then drop) the machine's index BEFORE popping it: the flow ids it
        # names are part of the per-flow view state cleared below.
        indexed = [
            str(session.get("flow_id") or "")
            for session in self._history_index.pop(machine_id, [])
        ]
        dropped = [
            flow_id
            for flow_id, bundle in self._history_data.items()
            if str(bundle.get("machine_id") or "") == machine_id
        ]
        for flow_id in dropped:
            del self._history_data[flow_id]
        # A takeover is not a budget eviction: whatever this machine's flows were
        # holding is gone because it belonged to the PREVIOUS owner, and the new
        # owner must be able to populate its own bundles immediately. A leftover
        # cold marker would suppress exactly that (see ``_history_cold``), so the
        # per-flow view state is cleared for every flow this machine reported —
        # including ones the budget had already evicted, which is precisely the
        # case a bundle-only sweep would miss.
        record = self._machines.get(machine_id)
        stale = set(dropped)
        if record is not None:
            stale.update(record.flows)
        stale.update(flow_id for flow_id in indexed if flow_id)
        for flow_id in stale:
            self._history_cold.pop(flow_id, None)
            self._history_cold_advised_at.pop(flow_id, None)
            self._history_read_at.pop(flow_id, None)
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

    def _iter_owned_machines_online_first(
        self, owner: Optional[str], *, online_only: bool = False
    ) -> List[Tuple[str, "MachineRecord"]]:
        """Owner-visible machines, all REACHABLE ones first then the rest.

        Caller MUST already hold ``self._lock`` — this is a pure read over
        ``self._machines`` and takes no lock of its own. Each segment keeps the
        original ``self._machines`` insertion order, so with no reachable
        machine at all the result is byte-for-byte the previous iteration order.
        *owner* ``None`` is the unscoped/admin view (no owner filtering), matching
        ``_owned(record, None)``. With *online_only* set only the reachable
        segment is returned — used by the callers that resolve in two whole
        passes.

        WHY: on a shared filesystem (HPC clusters, where a job moves from
        node007 to node008 but reads the same disk) several daemons report the
        SAME flow_id, and the server keeps a disconnected machine's flows and
        history index around after ``mark_offline``. Resolving a flow to the
        first machine in insertion order therefore lets a long-gone machine
        permanently shadow the machine that can actually serve the request,
        which surfaces as 404s on history detail and resume. A machine no frame
        can reach is unservable by definition, so reachable candidates must win;
        the others stay as a last-resort fallback (unchanged behaviour for the
        single-machine deployment).

        Reachability is :meth:`_machine_is_reachable`, i.e. live socket state
        rather than the debounced ``online`` presence flag — during the 60 s
        offline grace the dead machine still *displays* as online and would
        otherwise keep winning this ordering.
        """
        online: List[Tuple[str, "MachineRecord"]] = []
        offline: List[Tuple[str, "MachineRecord"]] = []
        for machine_id, record in self._machines.items():
            if not _owned(record, owner):
                continue
            if self._machine_is_reachable(machine_id, record):
                online.append((machine_id, record))
            elif not online_only:
                offline.append((machine_id, record))
        return online + offline

    async def get_flow(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Find a flow by id across all machines.

        Returns ``(machine_id, flow_dict)`` or ``None`` when no machine owns a
        flow with that id. With *owner* set, flows on machines belonging to a
        different owner are skipped — owner A can neither see nor (via the
        callers that gate on this) control owner B's flows.

        Machine resolution is **reachable-first with an unreachable fallback**
        (see :meth:`_iter_owned_machines_online_first`): when the same flow_id is
        reported by several machines sharing one filesystem, the connected one
        is returned so the read is served — and any command routed off this
        lookup dispatched — to a daemon that can actually be reached. Owner
        scoping is applied before that ordering, so a connected machine
        belonging to another owner is never selected.

        INVARIANT: this is the single flow→machine resolution for EVERY caller —
        detail reads, respond/interject, and the resume/end command gates
        (:meth:`is_flow_resumable` / :meth:`is_flow_endable`) alike. A machine no
        frame can reach can only serve a frozen pre-death snapshot and can only
        fail a dispatch, so it must never be preferred over one that reports the
        same flow and is reachable — that preference is exactly the shared-
        filesystem 404 / "machine not connected" this resolution exists to
        remove.
        """
        async with self._lock:
            for machine_id, record in self._iter_owned_machines_online_first(
                owner
            ):
                flow = record.flows.get(flow_id)
                if flow is not None:
                    return machine_id, flow.to_dict()
        return None

    async def find_machine_for_flow(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> Optional[str]:
        """Return the machine id owning *flow_id*, or ``None``.

        Inherits :meth:`get_flow`'s reachable-first resolution, so a
        shared-filesystem flow resolves to a connected machine when one has it.
        """
        result = await self.get_flow(flow_id, owner=owner)
        return result[0] if result is not None else None

    async def find_live_holder_machine(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> Optional[str]:
        """Return the machine that demonstrably HOLDS *flow_id* live, or ``None``.

        Distinct from :meth:`get_flow`/:meth:`find_machine_for_flow`, whose
        reachable-first resolution answers "which daemon should serve this
        request" — it deliberately prefers a *connected* reporter and therefore
        cannot be read as ownership. On a shared filesystem every machine
        aggregating the same ``engine.json`` reports the same flow, so the
        reachable-first winner may be a mere observer while another host's
        ``run.pid`` actually holds the run.

        A reporter counts as a holder only when its own snapshot says the flow
        is live there: not completed, and ``resumable`` false — the aggregator
        clears that flag exactly when the reporting daemon sees a live local
        ``luo run`` process for the flow's root (its live-roots gate). Ambiguity
        is resolved by refusing to guess: zero or several such reporters yield
        ``None``, and the caller must then say nothing machine-specific rather
        than send an operator to the wrong host.
        """
        holders: List[str] = []
        async with self._lock:
            for machine_id, record in self._iter_owned_machines_online_first(
                owner
            ):
                flow = record.flows.get(flow_id)
                if flow is None:
                    continue
                if str(flow.status or "").lower() == "completed":
                    continue
                if flow.resumable:
                    continue
                holders.append(machine_id)
        return holders[0] if len(holders) == 1 else None

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
        detail endpoint can route a :data:`~tianluo.daemon.protocol.MSG_DETAIL_REQUEST`
        to the daemon whose flow raised the call (its truncated prompt is
        surfaced in STATUS_UPDATE; the full prompt is fetched on demand).
        Returns ``None`` when no owner-scoped flow holds the call.

        ``call_id`` is only unique within a single project; two owner-scoped
        projects can each hold a pending call with the same local id. When the
        caller knows the *project_root* of the call whose full prompt it wants,
        it MUST be passed so resolution is pinned to that project — otherwise an
        earlier-scanned project's matching call would misroute the detail
        request to the wrong daemon / filesystem target.

        Machine resolution is reachable-first with an unreachable fallback, like
        every other flow→machine lookup (see
        :meth:`_iter_owned_machines_online_first`). WHY: pulling a call's full
        prompt reads ``tianluo/calls/<id>`` off the flow's project_root, so any
        daemon mounting that (here shared) filesystem serves the identical bytes.
        Without this, a shared-filesystem call resolved to the long-gone machine
        that happened to register first and the detail endpoint answered 503
        "not connected" forever.
        """
        async with self._lock:
            for machine_id, record in self._iter_owned_machines_online_first(
                owner
            ):
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
        directly rather than pulling. Scoping and reachable-first resolution
        both mirror :meth:`find_call_owner`, so an unpinned lookup reads the
        mirror of the same machine that method would have routed the pull to.
        """
        async with self._lock:
            for mid, record in self._iter_owned_machines_online_first(owner):
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
    #: ``luo run --resume --flow-id <id>``.  RUNNING flows already have a
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

        The returned ``machine_id`` comes from :meth:`get_flow`, i.e. it is
        resolved reachable-first: a shared-filesystem flow known to both a
        disconnected and a connected machine resumes on the connected one, and
        the resumability verdict is read off that same machine's snapshot — the
        one whose daemon will actually receive the dispatch.
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
          ``project_root`` points inside ``<main>/tianluo/worktrees/<name>``. This
          is the dangling-worktree case this feature exists for: when a
          ``luo run --worktree`` flow reaches COMPLETED but the follow-up
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

        As with :meth:`is_flow_resumable`, the ``machine_id`` is resolved
        reachable-first by :meth:`get_flow`, so the end command is dispatched to
        a machine that is actually connected whenever one reports the flow — a
        node killed mid-run therefore leaves nothing stuck: the surviving node
        on the shared filesystem can still end (archive/clean up) the flow.
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

    async def mark_history_replay(
        self, flow_id: str, *, cursor: Optional[Dict[str, Any]] = None
    ) -> None:
        """Arm one outstanding pull reply for *flow_id*.

        Called right after a ``MSG_HISTORY_REQUEST`` leaves the server — the one
        funnel every回程 pull goes through (a bundle cache miss, the
        ``requires_full`` self-heal, a reconnect backfill). Everything that reply
        brings back is a re-delivery of already-persisted history, whatever
        ``mode`` each of its frames wears, so the browser leg summarizes it
        exactly like the REST bundle response.

        Queued, not counted: two pulls can be in flight for one flow (a REST
        cache miss and a ``requires_full`` self-heal), and their replies drain
        one after the other. One arm per request, one retirement per reply, so
        the second drain is not read as live traffic the moment the first ends.

        *cursor* is the cursor the request carried (``None`` for a cursorless
        full rebuild). It is kept because it fixes the shape of the reply's HEAD
        frame, which is the only thing that separates the answer we are waiting
        for from a live append that raced its dispatch (see
        :meth:`take_history_replay`).
        """
        if not flow_id:
            return
        async with self._lock:
            drain = self._history_replay_pulls.get(flow_id)
            if drain is None:
                drain = _ReplayDrain()
                self._history_replay_pulls[flow_id] = drain
            drain.pulls.append(
                _PendingPull(
                    expects_full=not cursor,
                    cursor=dict(cursor) if cursor else {},
                )
            )
            drain.deadline = time.monotonic() + self._HISTORY_REPLAY_TTL

    async def unmark_history_replay(
        self, flow_id: str, *, cursor: Optional[Dict[str, Any]] = None
    ) -> None:
        """Retract the marker THIS caller armed for a request that never left.

        :meth:`mark_history_replay` is called BEFORE the send rather than after
        it, because the daemon's reply can be read off the socket while the send
        coroutine is still resuming — a marker armed afterwards would miss its
        own reply's head, and every chunked tail behind that head would then be
        classified as live traffic and shipped whole. The cost of arming first
        is a marker left behind when the send FAILS, which this retracts.

        WHY it retracts BY SHAPE rather than the queue tail: two pulls can be
        armed concurrently for one flow (a cursorless REST rebuild and the ws
        self-heal's incremental recovery), and only one of them may fail to
        send — a stale socket after a daemon reconnect fails one caller while
        the other dispatches fine. Popping the tail would then strip the marker
        off the pull that genuinely LEFT the server, and its reply — arriving as
        ``append`` frames that no longer match any armed shape — would be
        classified as live traffic and relayed to the browsers whole. A pull
        that was dispatched keeps its replay identity until its own closing
        frame retires it, so the failed caller only ever takes back a marker
        armed for a request of the shape it armed itself (*cursor*, or
        cursorless for a full rebuild).

        Only a pull that has not started draining is retractable: once frames of
        a reply are arriving, the marker belongs to that reply. With no armed
        pull of this shape left to take back, nothing is retracted — the leak
        guard (:attr:`_HISTORY_REPLAY_TTL`) covers the residue, which is far
        cheaper than unmarking a live reply.
        """
        if not flow_id:
            return
        expects_full = not cursor
        async with self._lock:
            drain = self._history_replay_pulls.get(flow_id)
            if drain is None or not drain.pulls:
                return
            # A started drain owns the frontmost pull whatever its shape.
            floor = 1 if drain.draining else 0
            index = next(
                (
                    i
                    for i in range(len(drain.pulls) - 1, floor - 1, -1)
                    if _same_pull_shape(drain.pulls[i], expects_full, cursor)
                ),
                None,
            )
            if index is None:
                return
            drain.pulls.pop(index)
            if not drain.pulls:
                self._history_replay_pulls.pop(flow_id, None)

    async def take_history_replay(
        self,
        flow_id: str,
        *,
        mode_full: bool = False,
        chunk_bounded: Any = False,
        cursor_base: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """The shaping half of :meth:`take_history_replay_verdict`.

        Kept as the narrow boolean the shaping decision actually needs, so a
        caller that only asks "summarize this frame or ship it whole?" is not
        made to unpack a verdict it has no use for.
        """
        verdict = await self.take_history_replay_verdict(
            flow_id,
            mode_full=mode_full,
            chunk_bounded=chunk_bounded,
            cursor_base=cursor_base,
        )
        return verdict.replay

    async def take_history_replay_verdict(
        self,
        flow_id: str,
        *,
        mode_full: bool = False,
        chunk_bounded: Any = False,
        cursor_base: Optional[Dict[str, Any]] = None,
        final: Optional[bool] = None,
        machine_id: str = "",
        applied: bool = True,
    ) -> ReplayVerdict:
        """Account one inbound ``HISTORY_DATA`` frame and classify its origin.

        ``ReplayVerdict.replay`` is ``True`` when the frame REPLAYS
        already-persisted history (a pull reply's frame, or a whole-bundle
        snapshot replacement) and ``False`` for a genuine post-subscription tail
        append, which rides to the browser whole. ``from_pull`` narrows that to
        the frames of a reply the server itself asked for, and ``rest_served``
        says a REST caller is already being handed this reply's records — the
        pair the fan-out needs to stop delivering one conversation twice.

        INVARIANT: every frame of a dispatched pull's reply — its head and all
        of its chunked tails — is classified as a replay, and the marker that
        tracks that reply is retired by the reply's OWN closing frame. Called
        for EVERY history frame that arrives for *flow_id*, including the ones
        the fan-out suppresses: a drain's ``full`` head is suppressed whenever a
        REST caller is parked on the pull, and skipping its accounting would
        leave the reply one frame out of step with the marker.

        WHY the head gate (``_frame_opens_reply``): the daemon pauses its push
        loop for a flow only once the drain STARTS, so an append emitted in the
        dispatch→drain-start window (seconds on the links the widened pull
        timeouts exist for) reaches the server BEFORE the reply's head. Counting
        such an interloper against the marker retired it on the spot, and every
        tail chunk of the recovery behind it — everything past the first frame
        of a big session — then shipped to every subscribed browser whole while
        the parked REST waiter returned the same records summarized, leaving the
        browser holding both copies. So until a reply has actually started
        arriving, only a frame whose shape MATCHES the head our own request must
        be answered with may open (and hence consume) it; anything else is live
        traffic that raced the dispatch and is classified on its own shape.

        This is also the single funnel where a delivery's COMPLETENESS is
        tracked (see :class:`_OpenDelivery`), because it is called for every
        history frame — relayed, suppressed or discarded — and it already knows
        which delivery the frame belongs to. *final* is the frame's own
        statement (``protocol.make_history_data``): ``True`` ends the delivery,
        ``False`` says more of it is coming, ``None`` that the daemon is too old
        to say. When it IS stated it also settles the reply's closing frame
        outright, in place of the *chunk_bounded* heuristic below — the sender's
        own read is authoritative where the receiver's size estimate only infers
        — and the estimate stays as the fallback for the ``None`` case.

        *applied* says the frame's records actually reached the bundle
        (:meth:`apply_history_frame` left the cache authoritative AND did not
        reject them). INVARIANT: a frame the cache REFUSED may not close a
        delivery. *final* is the SENDER's statement — it describes what the
        daemon read off disk, not what this server managed to store — so a
        gapped or ``requires_full``-blocked frame declaring ``final: True``
        would otherwise retire the marker while the bundle it claims to complete
        never received a record of it, publishing a settled cursor over a hole.
        See :meth:`_note_delivery_locked` for the exact per-case rule.

        *chunk_bounded* says the frame reached the daemon's per-frame chunk bound
        (:data:`~tianluo.daemon.history.MAX_BYTES_PER_REPORT` /
        :data:`~tianluo.daemon.history.MAX_RECORDS_PER_REPORT`), which is the one
        thing the wire does say about a reply still having more to come: the
        daemon keeps reading and sending from the advancing cursor for exactly as
        long as its reads truncate, so a frame under the bound is the reply's
        LAST. That closing frame is itself a replay; the next frame for the flow
        is a live increment. It may be passed as a CALLABLE, which is evaluated
        only when a marker is actually outstanding — measuring it against the
        daemon's billing basis costs a pass over the frame's records, and the
        overwhelming majority of frames have no drain to close.

        WHY not a quiet-time threshold: the dispatch→first-frame gap is a cold
        multi-MB jsonl read (seconds — the latency ``HISTORY_PULL_TIMEOUT`` is
        sized for) while the drain→next-live-append gap is one push-loop tick
        (~1 s), so any idle window either cuts a slow recovery short or swallows
        the live traffic that follows a fast one. Counting the reply's own frames
        separates them without asking the daemon→server protocol for a bit it
        does not carry.

        WHY not "the bundle looks caught up": every drain frame declares only the
        water mark the reader has reached, so the bundle looks complete after
        each one — reading that as convergence would classify the whole tail of a
        recovery as live.

        With no reply outstanding the verdict falls back to the frame's own
        shape: a ``mode: full`` frame is a whole-bundle snapshot replacement —
        a daemon that restarted and lost its cursors re-sends a flow's entire
        persisted history that way — which is a replay of already-persisted
        history no less than an answer to a pull we sent.
        """
        if not flow_id:
            return ReplayVerdict(False)
        now = time.monotonic()
        async with self._lock:
            drain = self._history_replay_pulls.get(flow_id)
            if drain is not None and now > drain.deadline:
                # A request nobody ever answered; drop it rather than let it
                # shape this flow's live traffic for the rest of the uptime.
                self._history_replay_pulls.pop(flow_id, None)
                drain = None
            if drain is None or not drain.pulls:
                # Live traffic, or a reply whose marker is gone. With no pull
                # outstanding for this flow, the frame's own ``final`` bit is
                # both the only completeness statement available and an
                # unambiguous one — it describes the delivery this frame belongs
                # to, and that is the only delivery in progress.
                incomplete = self._note_delivery_locked(
                    flow_id, machine_id, finished=final, now=now,
                    applied=applied,
                )
                return ReplayVerdict(
                    mode_full, delivery_incomplete=incomplete
                )
            if not drain.draining:
                # Replies drain in dispatch order down one socket, but a pull
                # armed while an earlier one was still in flight would be stuck
                # behind it if the daemon ever answered out of order — so the
                # head is matched against every pull still waiting, not just the
                # frontmost, and the one it opens is promoted.
                opened = next(
                    (
                        index
                        for index, pull in enumerate(drain.pulls)
                        if _frame_opens_reply(pull, mode_full, cursor_base)
                    ),
                    None,
                )
                if opened is None:
                    # A live append that raced the dispatch. It leaves the
                    # marker untouched — and its own deadline unrefreshed, since
                    # it is no evidence the reply we are waiting for is alive.
                    #
                    # Its ``final`` bit is likewise about ITS delivery, not about
                    # the reply we are still waiting for, so it may only ever
                    # ADD incompleteness here, never clear the pull's.
                    incomplete = self._note_delivery_locked(
                        flow_id,
                        machine_id,
                        finished=(False if final is False else None),
                        now=now,
                        applied=applied,
                    )
                    return ReplayVerdict(
                        mode_full, delivery_incomplete=incomplete
                    )
                if opened:
                    drain.pulls.insert(0, drain.pulls.pop(opened))
                drain.draining = True
            # Push the leak guard out on every frame of the reply so a long
            # multi-MB recovery cannot age out mid-drain.
            drain.deadline = now + self._HISTORY_REPLAY_TTL
            rest_served = drain.rest_served
            if final is not None:
                # The sender stated it: a reply that says it has more to come is
                # not closed by an estimate that happens to put this frame under
                # the bound, and one that says it is done is not held open by an
                # estimate that puts it over. The heuristic below is what the
                # wire USED to leave us with, and stays the fallback.
                bounded = not final
            else:
                bounded = (
                    chunk_bounded() if callable(chunk_bounded) else chunk_bounded
                )
            incomplete = self._note_delivery_locked(
                flow_id,
                machine_id,
                finished=not bounded,
                now=now,
                from_pull=True,
                applied=applied,
            )
            if not bounded:
                # Under the chunk bound: this is the reply's LAST frame, so the
                # pull it answers retires here.
                drain.pulls.pop(0)
                drain.draining = False
                # The "a REST caller holds these records" fact belongs to the
                # reply that just ended, not to the next one queued behind it.
                drain.rest_served = False
                if not drain.pulls:
                    self._history_replay_pulls.pop(flow_id, None)
            return ReplayVerdict(
                True,
                from_pull=True,
                rest_served=rest_served,
                closing=not bounded,
                delivery_incomplete=incomplete,
            )

    def _note_delivery_locked(
        self,
        flow_id: str,
        machine_id: str,
        *,
        finished: Optional[bool],
        now: float,
        from_pull: bool = False,
        applied: bool = True,
    ) -> bool:
        """Record what one frame said about its delivery. Returns "still open".

        *finished* is ``True`` (this frame ended the delivery), ``False`` (more
        of it is coming) or ``None`` (nothing stated — a pre-``final`` daemon's
        live frame). ``None`` deliberately neither opens nor closes a marker: an
        unstated frame is not evidence either way, and inventing one would
        either wedge every old daemon's flow as permanently incomplete or clear
        a marker a newer frame legitimately set. It does refresh an open
        marker's clock, because a frame arriving IS evidence the delivery is
        still alive.

        *from_pull* says the frame belongs to a reply the server asked for, and
        is what a ``finished`` frame needs in order to clear a STALLED marker.
        WHY: a live one-frame append is a complete delivery in its own right and
        truthfully says so, but it says nothing about the dead delivery whose
        tail the bundle is still missing — letting it clear that marker would
        hand back a bundle that calls itself whole while a hole from an earlier
        reply remains. Only a reply that ran to its declared end brings the
        bundle up to the sender's water mark, so only that may settle it.

        INVARIANT: only a frame the cache ACCEPTED (*applied*) may make a bundle
        look more complete than it was. What a frame states is what the SENDER
        read; whether the bundle grew is what the cache decided, and the two part
        company precisely in the case this marker exists for — a reconnected
        daemon resuming from ITS cursor sends frames the cache refuses as gapped
        (or refuses outright while the flow is latched ``requires_full``), and
        those frames still carry ``final: False`` then ``final: True``. Letting
        them through would clear the stall and then retire the marker over a
        bundle that received nothing, which is exactly the self-consistent
        prefix presenting itself as whole. So a refused frame may only ever ADD
        incompleteness: it opens a marker (its records are missing from the
        bundle by definition) and never closes or un-stalls one. The repair is
        unaffected — the ``requires_full`` self-heal pull that such a frame arms
        answers with an ACCEPTED reply, and that is what settles the marker.
        Caller must hold ``self._lock``.
        """
        open_delivery = self._history_deliveries.get(flow_id)
        if finished is False:
            if open_delivery is None:
                self._history_deliveries[flow_id] = _OpenDelivery(
                    machine_id=machine_id, last_frame_at=now
                )
                self._prune_history_deliveries()
            else:
                open_delivery.last_frame_at = now
                if applied:
                    # A delivery that is arriving again is not stalled, whatever
                    # a previous disconnect concluded: the repair this marker
                    # exists to trigger is exactly what is now in progress. A
                    # REFUSED frame is not that — nothing of it landed — so the
                    # stall stands and the repair stays due.
                    open_delivery.stalled = False
                if machine_id:
                    open_delivery.machine_id = machine_id
            return True
        if finished is True:
            if not applied:
                # The frame that claims to end the delivery never made it into
                # the bundle, so the delivery did not end here for US. Open a
                # marker if there is none: the records this frame carried are
                # missing, which is the definition of an incomplete bundle.
                if open_delivery is None:
                    self._history_deliveries[flow_id] = _OpenDelivery(
                        machine_id=machine_id, last_frame_at=now
                    )
                    self._prune_history_deliveries()
                else:
                    open_delivery.last_frame_at = now
                return True
            if (
                open_delivery is not None
                and open_delivery.stalled
                and not from_pull
            ):
                return True
            self._history_deliveries.pop(flow_id, None)
            return False
        if open_delivery is not None:
            open_delivery.last_frame_at = now
            return True
        return False

    def _prune_history_deliveries(self) -> None:
        """Bound the open-delivery map. Caller must hold ``self._lock``.

        Interrupted deliveries are repaired by a poll on the flow, so a flow
        nobody ever opens again keeps its marker forever. Dropping the OLDEST
        entries costs only that flow's automatic repair — it still rebuilds
        through the ordinary cache-miss pull — while keeping a long-lived server
        from accumulating one entry per flow it ever served.
        """
        limit = 4096
        excess = len(self._history_deliveries) - limit
        if excess <= 0:
            return
        oldest = sorted(
            self._history_deliveries,
            key=lambda fid: self._history_deliveries[fid].last_frame_at,
        )
        for flow_id in oldest[:excess]:
            self._history_deliveries.pop(flow_id, None)

    async def history_delivery_incomplete(self, flow_id: str) -> bool:
        """Whether *flow_id*'s bundle is missing the tail of a delivery."""
        async with self._lock:
            return flow_id in self._history_deliveries

    async def note_machine_deliveries_interrupted(
        self, machine_id: str
    ) -> List[str]:
        """Declare every open delivery from *machine_id* dead. Returns the flows.

        Called when a daemon's socket ends. INVARIANT: a delivery whose sender
        is gone cannot finish, so the bundle it was extending stays flagged
        INCOMPLETE and the flow is armed for a repair pull — the ``requires_full``
        latch the rest of the history path already self-heals on.

        WHY the arming happens here rather than being left to the next append:
        the flow that loses its tail this way is typically a COMPLETED one (a
        console opening an archived session is what triggers the multi-frame
        pull in the first place), and a completed flow gets no further appends —
        so the append-driven recovery that repairs a live flow never fires for
        it. Its only remaining trigger is a REST poll, which is why the poll's
        reconcile branch consults :meth:`take_history_delivery_repair`.

        The reply's replay marker goes with it: that reply is not coming, and
        leaving the marker armed would make the NEXT frames for this flow — live
        traffic from the reconnected daemon — be misread as its continuation.
        """
        if not machine_id:
            return []
        async with self._lock:
            interrupted = [
                flow_id
                for flow_id, delivery in self._history_deliveries.items()
                if delivery.machine_id == machine_id
            ]
            for flow_id in interrupted:
                self._history_deliveries[flow_id].stalled = True
                self._history_replay_pulls.pop(flow_id, None)
                self._history_requires_full.add(flow_id)
                # A recovery whose reply died with this socket is not "in
                # flight" — leaving its marker would make the next poll's
                # ``plan_recovery_pull`` refuse a repair for a whole TTL while
                # nothing was coming. The dedup exists to stop a poll fighting a
                # LIVE drain, and there is no longer one.
                self._history_recovery_inflight.pop(flow_id, None)
        if interrupted:
            logger.warning(
                "hist-diag history delivery INTERRUPTED machine=%s flows=%s "
                "(the socket ended mid-delivery; the cached bundles are a "
                "PREFIX and stay flagged incomplete until a repair pull "
                "completes them)",
                machine_id, ",".join(interrupted),
            )
        return interrupted

    async def history_delivery_repair_due(self, flow_id: str) -> bool:
        """Whether *flow_id*'s interrupted delivery should be repaired now.

        ``True`` when the bundle is missing a delivery's tail AND that delivery
        is provably no longer arriving — its sender's socket ended
        (:meth:`note_machine_deliveries_interrupted`), or nothing of it has
        landed for :attr:`_HISTORY_DELIVERY_STALL_GRACE`. A delivery still in
        flight answers ``False``, which is what keeps a poll from dispatching a
        rival pull against the drain that is already filling the bundle.

        WHY this is a pure query and does NOT arm the ``requires_full`` latch it
        obviously wants: arming that latch makes every live append be discarded
        until a repair lands, so an arm that is not immediately followed by a
        dispatched repair would freeze a still-running flow for a recovery TTL
        (the poll can decide not to pull at all — no connected daemon, a repair
        already in flight). The arming therefore happens inside
        :meth:`plan_recovery_pull` (``repair=True``), in the same critical
        section that decides how to pull and marks it in flight.
        """
        async with self._lock:
            delivery = self._history_deliveries.get(flow_id)
            if delivery is None:
                return False
            return delivery.stalled or (
                (time.monotonic() - delivery.last_frame_at)
                >= self._HISTORY_DELIVERY_STALL_GRACE
            )

    async def mark_history_reply_served(self, flow_id: str) -> None:
        """Record that the reply now draining for *flow_id* woke a REST waiter.

        WHY: a cache-miss open dispatches ONE ``MSG_HISTORY_REQUEST`` and the
        daemon answers it with a ``full`` head plus every chunk-bounded tail the
        flow needs (147 frames for this repo's own
        ``20260831-095750_23865927``). The head resolves the parked REST handler,
        which returns the bundle — and the tails then extend that same bundle,
        which the same browser reads through its ordinary token-pinned polls. So
        relaying those tails ships the identical conversation a second time:
        measured at 103.9 MB down ``/ws/ui`` for one open whose REST body was
        18.9 MB gzipped. Only the head was ever suppressed, because the fan-out
        rule looked at one frame at a time and no frame but the head knows a REST
        caller is waiting. This carries that fact forward to the rest of the
        reply; the consoles are told the bundle moved by the records-less
        ``history_cursor`` advisory instead, which is what they already act on
        for a suppressed head.
        """
        if not flow_id:
            return
        async with self._lock:
            drain = self._history_replay_pulls.get(flow_id)
            if drain is not None and drain.draining:
                drain.rest_served = True

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

        A ``flow_id`` is collapsed to a single entry across machines, preferring
        the one reported by a REACHABLE machine (live socket, not the debounced
        presence flag) and, within the same reachability state, the one with the
        newest ``updated_at`` (ties keep the first encountered so the result
        stays deterministic).

        WHY: on a shared filesystem (an HPC job moving from node007 to node008
        reads the same disk) several daemons index the SAME flow, and the server
        keeps a disconnected machine's index after ``mark_offline`` — so without
        collapsing, one session shows up once per machine that ever saw it. The
        online preference also keeps the ``machine_id`` shown in the list equal
        to the machine :meth:`_find_machine_for_history_flow_locked` will
        actually route the detail fetch to. Entries with no usable ``flow_id``
        are unaddressable and pass through untouched rather than being merged
        into (and hidden behind) each other.
        """
        async with self._lock:
            entries: List[Dict[str, Any]] = []
            # flow_id -> (index into entries, machine is online) of the winner
            # currently held for that flow.
            chosen: Dict[str, Tuple[int, bool]] = {}
            for machine_id, sessions in self._history_index.items():
                record = self._machines.get(machine_id)
                if owner is not None and (
                    record is None or not _owned(record, owner)
                ):
                    continue
                online = self._machine_is_reachable(machine_id, record)
                for session in sessions:
                    entry = dict(session)
                    entry.setdefault("machine_id", machine_id)
                    flow_id = str(entry.get("flow_id") or "")
                    if not flow_id:
                        entries.append(entry)
                        continue
                    prior = chosen.get(flow_id)
                    if prior is None:
                        chosen[flow_id] = (len(entries), online)
                        entries.append(entry)
                        continue
                    index, prior_online = prior
                    if online != prior_online:
                        wins = online
                    else:
                        wins = str(entry.get("updated_at") or "") > str(
                            entries[index].get("updated_at") or ""
                        )
                    if wins:
                        entries[index] = entry
                        chosen[flow_id] = (index, online)
        entries.sort(key=lambda e: str(e.get("updated_at") or ""), reverse=True)
        return entries

    # -- history cache memory budget --------------------------------------

    def _bundle_bytes(self, bundle: Dict[str, Any]) -> int:
        """A cached bundle's accounted size, computing it once on first contact.

        Stored on the bundle under the private ``_bytes`` key alongside the
        record count it was measured for, exactly like ``_key_index`` /
        ``_usage_sources`` (every history getter names its output fields
        explicitly, so a private key can never leak onto the wire). The count
        guard makes a bundle some future path mutated behind the accounting's
        back re-measure instead of reporting a stale size.
        """
        records = bundle.get("records") or []
        size = bundle.get("_bytes")
        if isinstance(size, int) and bundle.get("_bytes_len") == len(records):
            return size
        size = sum(_estimate_record_bytes(record) for record in records)
        bundle["_bytes"] = size
        bundle["_bytes_len"] = len(records)
        return size

    def _history_cache_bytes(self) -> int:
        """Total accounted bytes held by every cached bundle.

        Summed from the per-bundle figures rather than tracked as a running
        total: the sum is O(number of cached flows) — hundreds at most, versus
        the millions of records the figures themselves cover — and it cannot
        drift out of step with the bundles the way an incrementally maintained
        counter would after any code path that adds or drops a bundle without
        telling the counter. Caller must hold ``self._lock``.
        """
        return sum(
            self._bundle_bytes(bundle) for bundle in self._history_data.values()
        )

    def _history_view_is_hot(
        self, flow_id: str, *, now: Optional[float] = None
    ) -> bool:
        """Whether a UI client read *flow_id* within the hot window.

        See :attr:`_HISTORY_VIEW_HOT_WINDOW` for why UI reads — not daemon
        pushes — are what "hot" means here.
        """
        read_at = self._history_read_at.get(flow_id)
        if read_at is None:
            return False
        current = time.monotonic() if now is None else now
        return (current - read_at) < self._HISTORY_VIEW_HOT_WINDOW

    def _note_history_view(self, flow_id: str) -> None:
        """Record a UI-driven read of *flow_id* and lift any cold suppression.

        Called from the REST-facing readers on EVERY read — hit or miss. The
        miss case is the load-bearing one: it is what re-admits an evicted flow,
        so the cache-miss回源 pull the endpoint is about to fire can populate a
        bundle again. Caller must hold ``self._lock``.
        """
        self._history_read_at[flow_id] = time.monotonic()
        self._history_cold.pop(flow_id, None)
        self._history_cold_advised_at.pop(flow_id, None)
        self._prune_history_view_markers()

    def _claim_cold_advisory(self, flow_id: str) -> bool:
        """Whether the caller may emit a cold-flow cursor advisory now.

        Debounced per flow (see :attr:`_HISTORY_COLD_ADVISORY_INTERVAL`) because
        an evicted flow whose daemon is draining a backlog produces a bounded
        chunk many times a second, and one advisory per chunk would be a fan-out
        storm for a signal whose whole content is "re-check your cursor". Caller
        must hold ``self._lock``.
        """
        now = time.monotonic()
        last = self._history_cold_advised_at.get(flow_id)
        interval = self._HISTORY_COLD_ADVISORY_INTERVAL
        if last is not None and (now - last) < interval:
            return False
        self._history_cold_advised_at[flow_id] = now
        self._prune_history_view_markers()
        return True

    def _prune_history_view_markers(self) -> None:
        """Keep the recency / cold marker maps from growing without bound.

        They are keyed by flow_id and outlive the bundles they describe, so on a
        server that sees tens of thousands of flows over a long uptime they would
        become their own (small) leak — self-defeating in a fix whose whole point
        is bounded memory. Trimming the OLDEST entries is safe: a dropped cold
        marker only means one wasted re-admission for a flow nobody has looked at
        in a very long time, and a dropped recency marker reads as "never read",
        which is already the eviction default.
        """
        limit = 4096
        for markers in (
            self._history_read_at,
            self._history_cold,
            self._history_cold_advised_at,
        ):
            excess = len(markers) - limit
            if excess <= 0:
                continue
            for flow_id in sorted(markers, key=markers.get)[:excess]:
                markers.pop(flow_id, None)
        # Survives eviction on purpose: a flow evicted mid-drain still has reply
        # frames on the wire, and mis-reading them as live increments would ship
        # their bodies whole. The reply's own closing frame retires it (and the
        # leak-guard deadline covers a request never answered); trimming here
        # only bounds the map on a very long-lived server, oldest guard first.
        excess = len(self._history_replay_pulls) - limit
        if excess > 0:
            oldest = sorted(
                self._history_replay_pulls,
                key=lambda flow_id: self._history_replay_pulls[flow_id].deadline,
            )
            for flow_id in oldest[:excess]:
                self._history_replay_pulls.pop(flow_id, None)

    def _history_admission_suppressed(self, flow_id: str) -> bool:
        """Whether a frame must NOT be allowed to (re)create *flow_id*'s bundle.

        True only for a flow the budget evicted that no UI client has read
        since. See ``_history_cold`` for why silent suppression — rather than
        the ordinary ``requires_full`` self-heal — is the correct answer for an
        unwatched flow: the self-heal would re-pull the whole conversation the
        budget just decided it cannot afford, forever.
        """
        if flow_id not in self._history_cold:
            return False
        return not self._history_view_is_hot(flow_id)

    def _evict_history_bundle(self, flow_id: str) -> int:
        """Drop *flow_id*'s bundle and mark it cold. Returns the bytes freed.

        Every piece of per-flow relay state that only makes sense WITH a bundle
        goes with it: a ``requires_full`` flag would make the next append arm a
        recovery pull, and an in-flight recovery marker would suppress a genuine
        later one. Caller must hold ``self._lock``.
        """
        bundle = self._history_data.pop(flow_id, None)
        if bundle is None:
            return 0
        freed = self._bundle_bytes(bundle)
        self._history_cold[flow_id] = time.monotonic()
        # Re-arm the advisory debounce: the FIRST frame after an eviction is the
        # one a console displaying this flow most needs, and an advisory sent
        # before the eviction says nothing about it.
        self._history_cold_advised_at.pop(flow_id, None)
        self._history_requires_full.discard(flow_id)
        self._history_recovery_inflight.pop(flow_id, None)
        # The bundle a half-finished delivery was extending is gone, so there is
        # nothing left to repair: the flow is cold, and re-admission goes through
        # the UI-driven cache-miss pull that rebuilds it whole. Keeping the marker
        # would have a poll dispatch a repair for a bundle the budget just refused
        # to hold — the eviction⇄回拉 storm ``_history_cold`` exists to prevent.
        self._history_deliveries.pop(flow_id, None)
        self._history_evictions += 1
        self._history_evicted_bytes += freed
        logger.info(
            "history-cache EVICT flow=%s machine=%s records=%d bytes=%d "
            "(least-recently-viewed; bundle rebuilt from a full daemon pull on "
            "the next UI read)",
            flow_id,
            str(bundle.get("machine_id") or ""),
            len(bundle.get("records") or []),
            freed,
        )
        return freed

    def _enforce_history_budget(self, *, protect: Optional[str] = None) -> int:
        """Evict least-recently-VIEWED bundles until the budget is met.

        Returns the number of bundles evicted. Caller must hold ``self._lock``.

        *protect* is the flow whose frame is being applied right now: it is
        exempt for this pass so a write is always observable by the caller that
        made it (the ``/ws/ui`` fan-out reads the bundle meta straight after
        ``apply_history_frame``, and a parked REST pull waiter re-reads the
        snapshot). The steady-state overshoot is therefore at most one bundle,
        which the periodic report's unprotected sweep then reclaims.

        INVARIANT: a flow a UI client is currently reading is never evicted.
        Eviction is a cache decision, and every eviction costs the next reader a
        full回源 pull; sacrificing the handful of watched flows would trade a
        bounded cache for an unbounded pull storm without saving the memory that
        actually matters (the many UNWATCHED active flows the daemon pushes).
        When only watched flows remain and the total is still above budget we
        stop and report it rather than evicting one; the report names the flows
        holding the memory, which is what makes the situation attributable from
        the log instead of merely fatal.
        """
        budget = self._history_cache_budget_bytes
        total = self._history_cache_bytes()
        if total <= budget:
            return 0
        now = time.monotonic()
        # Coldest first: a flow never read by a UI client sorts before every
        # flow that ever was.
        #
        # WHY an active worktree flow sorts LAST within the same recency: the
        # anti-shrink guard in :meth:`apply_history_frame` only protects such a
        # flow from a partially-resolved daemon read while the server still HOLDS
        # the longer bundle to compare against. Evicting it throws that
        # comparison away, so a later rebuild that resolves only the main-repo
        # slice would be accepted at face value. Draining every other candidate
        # first means the guard survives for the flows it exists for in every
        # case except "nothing but live worktree flows is cached" — where the
        # budget must still win, and the empty-full rejection (which needs no
        # cached bundle) remains as the floor.
        candidates = sorted(
            self._history_data,
            key=lambda fid: (
                self._is_active_worktree_flow_locked(fid),
                self._history_read_at.get(fid, float("-inf")),
            ),
        )
        evicted = 0
        for flow_id in candidates:
            if total <= budget:
                break
            if flow_id == protect:
                continue
            if self._history_view_is_hot(flow_id, now=now):
                continue
            total -= self._evict_history_bundle(flow_id)
            evicted += 1
        # The per-flow EVICT lines above are the attribution record and are
        # always emitted; these SUMMARY lines are debounced because this sweep
        # runs on every cache write, and a server parked just above its budget
        # would otherwise emit one summary per history frame.
        if total > budget:
            self._report_debounced(
                level=logging.WARNING, reason="over-budget-after-eviction"
            )
        elif evicted:
            self._report_debounced(level=logging.INFO, reason="post-eviction")
        return evicted

    def _report_debounced(self, *, level: int, reason: str) -> None:
        """Emit an occupancy report at most once per debounce window."""
        now = time.monotonic()
        if (now - self._history_report_at) < self._HISTORY_REPORT_DEBOUNCE:
            return
        self._history_report_at = now
        self._log_history_cache_report_locked(level=level, reason=reason)

    def _maybe_report_history_cache(self) -> None:
        """Emit an occupancy WARNING when the cache crosses its report threshold.

        Debounced (see :attr:`_HISTORY_REPORT_DEBOUNCE`) because this runs on
        every cache write. Caller must hold ``self._lock``.
        """
        budget = self._history_cache_budget_bytes
        threshold_pct = self._history_report_threshold_percent
        if budget <= 0 or threshold_pct <= 0:
            return
        total = self._history_cache_bytes()
        if total * 100 < budget * threshold_pct:
            return
        self._report_debounced(level=logging.WARNING, reason="threshold")

    def _history_cache_stats_locked(
        self, *, top: int = 8
    ) -> Dict[str, Any]:
        """Occupancy snapshot of the history cache. Caller must hold the lock."""
        sizes = {
            flow_id: self._bundle_bytes(bundle)
            for flow_id, bundle in self._history_data.items()
        }
        total = sum(sizes.values())
        now = time.monotonic()
        ranked = sorted(sizes, key=lambda fid: sizes[fid], reverse=True)[:top]
        budget = self._history_cache_budget_bytes
        return {
            "bytes": total,
            "budget_bytes": budget,
            "used_percent": (
                round(total * 100.0 / budget, 1) if budget else None
            ),
            "flows": len(self._history_data),
            "cold_flows": len(self._history_cold),
            "evictions": self._history_evictions,
            "evicted_bytes": self._history_evicted_bytes,
            "rss_bytes": _process_rss_bytes(),
            "top": [
                {
                    "flow_id": flow_id,
                    "machine_id": str(
                        self._history_data[flow_id].get("machine_id") or ""
                    ),
                    "bytes": sizes[flow_id],
                    "records": len(
                        self._history_data[flow_id].get("records") or []
                    ),
                    "idle_seconds": (
                        None
                        if flow_id not in self._history_read_at
                        else round(now - self._history_read_at[flow_id], 1)
                    ),
                }
                for flow_id in ranked
            ],
        }

    def _log_history_cache_report_locked(
        self, *, level: int, reason: str
    ) -> Dict[str, Any]:
        """Log one cache-occupancy report line. Caller must hold the lock.

        Developer-facing diagnostics (not i18n): the line is what a journald
        tail must be able to attribute a memory anomaly from, so it names the
        total, the budget, the eviction counters and the biggest cached flows by
        id — an operator reading it after an OOM can say WHICH flow was holding
        the memory instead of only that the process died.
        """
        stats = self._history_cache_stats_locked()
        top = ", ".join(
            "%s(%s)=%.1fMiB/%d rec idle=%ss"
            % (
                entry["flow_id"],
                entry["machine_id"] or "?",
                entry["bytes"] / (1024.0 * 1024.0),
                entry["records"],
                entry["idle_seconds"],
            )
            for entry in stats["top"]
        )
        logger.log(
            level,
            "history-cache report reason=%s used=%.1fMiB/%.1fMiB (%s%%) "
            "flows=%d cold=%d evictions=%d evicted=%.1fMiB rss=%s top=[%s]",
            reason,
            stats["bytes"] / (1024.0 * 1024.0),
            stats["budget_bytes"] / (1024.0 * 1024.0),
            "n/a" if stats["used_percent"] is None else stats["used_percent"],
            stats["flows"],
            stats["cold_flows"],
            stats["evictions"],
            stats["evicted_bytes"] / (1024.0 * 1024.0),
            "?" if stats["rss_bytes"] is None else
            "%.1fMiB" % (stats["rss_bytes"] / (1024.0 * 1024.0)),
            top,
        )
        return stats

    async def history_cache_stats(self) -> Dict[str, Any]:
        """Public occupancy snapshot of the history cache (tests / diagnostics)."""
        async with self._lock:
            return self._history_cache_stats_locked()

    async def sweep_history_cache(self) -> int:
        """Run one UNPROTECTED budget sweep. Returns the bundles evicted.

        WHY this is separate from :meth:`report_history_cache`: the write-path
        sweep always exempts the flow whose frame it is applying, so it can never
        evict THAT flow — and one actively pushed, unwatched flow growing past the
        budget on its own is precisely the oom-kill this budget exists to stop.
        Only a sweep with no protected flow can drop it. Enforcement therefore
        rides its own fixed cadence (``app.HISTORY_CACHE_SWEEP_INTERVAL``) and is
        never switched off by an operator turning the diagnostic log line off with
        ``server.history_cache.report_interval_seconds: 0``.
        """
        async with self._lock:
            return self._enforce_history_budget()

    async def report_history_cache(
        self, *, level: int = logging.INFO
    ) -> Dict[str, Any]:
        """Log an occupancy report and sweep the budget once.

        The server's periodic maintenance task calls this on the operator's
        report cadence; on every other tick it calls :meth:`sweep_history_cache`
        instead, so the sweep keeps running when the report is turned off.
        """
        async with self._lock:
            self._enforce_history_budget()
            return self._log_history_cache_report_locked(
                level=level, reason="periodic"
            )

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

    @staticmethod
    def _cursor_base_at_watermark(
        existing_cursor: Dict[str, Any], cursor_base: Dict[str, Any]
    ) -> bool:
        """Whether an append frame's *cursor_base* is anchored EXACTLY at the
        bundle's current water mark — it continues the bundle from where it
        stands, with no hole and no re-delivery.

        Used to rescue a cursored backfill drain that a racing live push
        re-flagged ``requires_full``: the frame we requested resumes from the
        server's own cursor, so every file it declares a base for must start at
        the line that file currently holds (0 for a file the bundle has not seen
        yet). A base sitting BEFORE the water mark (an overlap re-send) or PAST
        it (a real gap) does NOT match — only the precise anchor is exempted, so
        this cannot smuggle a hole past the ``requires_full`` guard.
        """
        if not cursor_base:
            return False
        for name, base in cursor_base.items():
            try:
                base_i = int(base)
                mark_i = int(existing_cursor.get(name, 0) or 0)
            except (TypeError, ValueError):
                return False
            if base_i != mark_i:
                return False
        return True

    async def apply_history_frame(
        self,
        flow_id: str,
        mode: str,
        records: List[Dict[str, Any]],
        *,
        cursor: Optional[Dict[str, Any]] = None,
        cursor_base: Optional[Dict[str, Any]] = None,
        machine_id: str = "",
        usage: Optional[Dict[str, Any]] = None,
        usage_catalog: Optional[Dict[str, Any]] = None,
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
        version-skewed daemon that does not send one. *usage* is the daemon's
        whole-flow usage/cost payload (full snapshots only), stored verbatim;
        *usage_catalog* is the serialized pricing catalog that priced it, and
        is stored on the bundle so the append-time re-aggregation
        (:meth:`_refresh_bundle_usage`) prices with the same table instead of
        degrading to the built-in one. Purely in-memory.

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
            if existing is None and self._history_admission_suppressed(flow_id):
                # The budget evicted this flow and no UI client has read it
                # since. The daemon keeps pushing it regardless of viewers, so
                # letting the frame through would re-establish the very bundle
                # the budget just decided it cannot afford — and, for an append,
                # would additionally arm ``requires_full`` and have the receive
                # loop pull the whole conversation back. Take nothing, arm
                # nothing: ``resolves_pull=False`` also makes the ``/ws/ui``
                # fan-out suppress the RECORDS and makes ``plan_recovery_pull``
                # return ``None``, so this converges to costing nothing per push
                # tick. The flow is re-admitted the moment a UI read touches it
                # (see ``_note_history_view``), and its bundle is then rebuilt by
                # the endpoint's cursorless FULL pull — never by a daemon-side
                # incremental, which would bake the evicted head into a hole.
                #
                # WHY the frame's cursor is nevertheless carried out
                # (``cold_suppressed``): suppressing the records is right,
                # suppressing the FACT that the flow moved is not. The History
                # view (``openHistorySession``) has no poll timer — it self-checks
                # only when a frame arrives — so a console displaying this flow
                # would freeze on what it already holds until the user re-clicked
                # the session. The fan-out answers this flag with a records-less
                # cursor advisory, which costs one tiny frame, rebuilds nothing
                # here, and lets such a console re-pull over REST; that read is
                # what re-admits the flow. A console displaying nothing ignores
                # the advisory, so an unwatched flow still converges to zero.
                announce = self._claim_cold_advisory(flow_id)
                logger.debug(
                    "history-cache SUPPRESS flow=%s mode=%s records=%d "
                    "(evicted and unwatched; not re-admitting from a push)",
                    flow_id, mode, len(new_records),
                )
                return HistoryWriteOutcome(
                    resolves_pull=False,
                    cold_suppressed=announce,
                    suppressed_cursor=(
                        (dict(cursor or {}) or None) if announce else None
                    ),
                )
            # Make room BEFORE the write, exempting this flow, so the bundle the
            # caller is about to read back is guaranteed to still be there.
            self._enforce_history_budget(protect=flow_id)
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
                # WHY: an append is normally refused while ``requires_full`` is
                # armed — the flow awaits a rebuild and any live push is a
                # potential hole. But the incremental self-heal
                # (:meth:`plan_recovery_pull`) requests a CURSORED backfill
                # anchored at the server's own water mark, and a live push the
                # daemon queued ~1 RTT before that request landed can arrive
                # first (FIFO on the socket), trip the cursor-gap guard and
                # RE-ARM ``requires_full``. Discarding the backfill drain we
                # explicitly asked for would then stall convergence for a whole
                # recovery TTL PER racing push — the exact bad-network scenario
                # the fix targets. A frame whose ``cursor_base`` meets the water
                # mark EXACTLY is contiguous by construction (no hole, no
                # re-delivery), so one stale live push must not void it: clear
                # ``requires_full`` and fall through to the normal append path.
                # A frame not so anchored is a genuine gap / first-tail and stays
                # discarded.
                if existing is not None and self._cursor_base_at_watermark(
                    existing.get("cursor") or {}, cursor_base or {}
                ):
                    self._history_requires_full.discard(flow_id)
                    logger.info(
                        "hist-diag append_history BACKFILL-RESUMED flow=%s "
                        "records=%d (append anchored at server water mark; "
                        "requires_full cleared, accepting cursored backfill "
                        "despite a racing re-arm)",
                        flow_id, len(new_records),
                    )
                    # fall through to the append processing below
                else:
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
                    # An unfinished delivery from the SUPERSEDED machine has
                    # nothing left to complete — its bundle is gone with it — and
                    # the cache miss this leaves behind already forces a full
                    # rebuild from the new daemon.
                    self._history_deliveries.pop(flow_id, None)
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
                # INVARIANT: a record the bundle already holds may never be
                # stored a SECOND time. Overlap re-delivery is a designed-for
                # input on this path — ``_detect_cursor_gap`` deliberately does
                # not treat it as a gap, so a daemon that re-reads and re-sends
                # a frame whose previous send failed, and a retried FAILED step
                # that rewrites its jsonl in place, both land here re-carrying
                # lines the bundle already has. The extreme shape is the
                # running-worktree self-heal: every throttle window the daemon
                # drains the WHOLE flow as a byte-capped ``full`` HEAD (refused
                # as shrinking) plus ``append`` TAILS that overlap the water
                # mark, so a bare ``extend`` grew the bundle by the flow's
                # entire length per window — the chat rendering each record 4×
                # and climbing. ``(step_id, ordinal)`` is the record's physical
                # jsonl line identity, so folding on it is what makes a repeated
                # drain a no-op: same line + same content is dropped, same line
                # with NEW content replaces in place (the retry case) so the
                # record count and order — hence ``bundle_signature`` and every
                # outstanding progress-token offset — stay put and an in-sync
                # client keeps getting ``not_modified``.
                applied, added, updated, dropped = self._reconcile_append_into(
                    existing, new_records
                )
                existing["mode"] = mode
                if cursor:
                    existing["cursor"] = dict(cursor)
                if machine_id:
                    existing["machine_id"] = machine_id
                if usage_catalog is not None:
                    # The daemon rides the project catalog on usage-bearing
                    # frames, so the append-time re-aggregation below prices
                    # with the same table the stored daemon payload used.
                    existing["usage_catalog"] = dict(usage_catalog)
                    self._invalidate_usage_rebuild(existing)
                existing["updated_at"] = time.time()
                if self._records_carry_usage(applied):
                    # The daemon's usage payload rides full snapshots only, so
                    # after a usage-bearing append the stored summary would
                    # under-count the extended bundle. Refresh it from the
                    # incremental source cache so every later REST/WS read sees
                    # a summary derived from ALL cached records.
                    #
                    # WHY the ACCEPTED set rather than the raw frame: the usage
                    # cache extends per append, so feeding it a re-delivered
                    # record would add that call's tokens/cost a second time —
                    # the same unbounded growth the record reconcile above
                    # closes, only in the usage summary instead of the chat.
                    self._refresh_bundle_usage(existing, applied)
                if flow_id in self._history_recovery_inflight:
                    # INFO-visible backfill CONVERGENCE signal: an append landed
                    # while a self-heal recovery is in flight for this flow, so
                    # the requested [water mark, now) window is being filled. The
                    # ordinary APPLIED-append is DEBUG-only; without this an
                    # operator tailing journalctl at INFO would see the recovery
                    # START (plan_recovery_pull kind=incremental) but never a
                    # completion, unable to tell convergence from a discard. The
                    # recovery-inflight marker ages out via the TTL, so this only
                    # fires for the recovery window, not for every live append.
                    logger.info(
                        "hist-diag append_history BACKFILL-APPLIED flow=%s "
                        "records=%d added=%d updated=%d dropped=%d total=%d "
                        "(recovery backfill append accepted — bundle extended "
                        "from the server water mark; self-heal converging)",
                        flow_id, len(new_records), added, updated, dropped,
                        len(existing["records"]),
                    )
                else:
                    logger.debug(
                        "hist-diag append_history APPLIED-append flow=%s records=%d "
                        "added=%d updated=%d dropped=%d total=%d",
                        flow_id, len(new_records), added, updated, dropped,
                        len(existing["records"]),
                    )
                self._maybe_report_history_cache()
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
                    if usage is not None:
                        # A re-pull that carries the whole-flow usage payload
                        # refreshes the cached summary even when the records
                        # are identical (usage can arrive later than the
                        # records, or ride the first full after an upgrade).
                        existing["usage"] = dict(usage)
                    if usage_catalog is not None:
                        # The pricing table can likewise arrive later than the
                        # records (or change with the project config), so the
                        # same re-pull refreshes it too.
                        existing["usage_catalog"] = dict(usage_catalog)
                        self._invalidate_usage_rebuild(existing)
                    existing["updated_at"] = time.time()
                    generation = self._ensure_generation(existing)
                    logger.debug(
                        "hist-diag append_history APPLIED-full-noop flow=%s "
                        "records=%d (identical bundle, generation %d kept)",
                        flow_id, len(new_records), generation,
                    )
                    return HistoryWriteOutcome(resolves_pull=True)
                bundle: Dict[str, Any] = {
                    "flow_id": flow_id,
                    "machine_id": machine_id,
                    "mode": mode,
                    "records": new_records,
                    "cursor": dict(cursor) if cursor else {},
                    "generation": self._next_generation(),
                    "updated_at": time.time(),
                }
                if usage is not None:
                    bundle["usage"] = dict(usage)
                if usage_catalog is not None:
                    bundle["usage_catalog"] = dict(usage_catalog)
                self._history_data[flow_id] = bundle
                # The flow has a resident bundle again, so the eviction marker
                # that suppressed push re-admission no longer describes it.
                self._history_cold.pop(flow_id, None)
                self._history_cold_advised_at.pop(flow_id, None)
                # Measure the new bundle once, here, while its records are the
                # only thing that has ever been in it; every later append keeps
                # the figure current incrementally (_reconcile_append_into).
                #
                # WHY this O(bundle) walk stays under the lock: it was measured
                # (scripts/measure_server_loop_stalls.py) at ~11 ms for a 3 MiB
                # bundle and ~29 ms for a 16 MiB one — real, but paid ONCE per
                # bundle replacement (a throttled full pull), not once per poll
                # like the usage rebuild that is memoized. Moving it off the loop
                # would mean sizing the records outside the critical section that
                # decides whether they are accepted at all, splitting the
                # accounting from the write it accounts for; the trade is not
                # worth it for a cost that does not recur.
                self._bundle_bytes(bundle)
                self._maybe_report_history_cache()
                logger.debug(
                    "hist-diag append_history APPLIED-full flow=%s records=%d "
                    "(bundle replaced, requires_full cleared)",
                    flow_id, len(new_records),
                )
                return HistoryWriteOutcome(resolves_pull=True)

    async def take_recovery_pull(self, flow_id: str) -> bool:
        """Return ``True`` when a self-heal pull should be sent for *flow_id*.

        This is the bare ARM/dedup view of the recovery gate: it answers only
        *whether* a pull should fire, sharing the exact ``_history_requires_full``
        / ``_history_recovery_inflight`` state and TTL logic as
        :meth:`plan_recovery_pull`, which the receive loop now uses to also decide
        *how* to pull (an incremental append backfill vs a cursorless full). It is
        retained as the primitive the recovery-dedup regression tests probe.

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

    async def plan_recovery_pull(
        self, flow_id: str, machine_id: str, *, repair: bool = False
    ) -> Optional[Tuple[str, Optional[Dict[str, Any]]]]:
        """Decide how to self-heal a ``requires_full`` flow, atomically.

        Returns ``None`` when no recovery should be dispatched (the flow is not
        flagged ``requires_full``, or one is already in flight within the TTL —
        the same dedup :meth:`take_recovery_pull` enforces). Otherwise arms a
        recovery in flight and returns one of:

        * ``("incremental", cursor)`` — the server already holds a NON-EMPTY,
          cursor-bearing bundle for this flow, produced by THIS machine, and the
          flow is not an active worktree flow. The gap that flagged
          ``requires_full`` is a tail the server missed, not a lost bundle, so we
          ask the daemon for an ``append`` pull anchored at the server's own
          water mark (*cursor*): the reply extends the bundle from exactly where
          it stands. On a bad network this is what makes the bundle converge —
          every disconnect only shortens the append window, never rebuilds the
          bundle from a short prefix.

          INVARIANT: clearing ``requires_full`` here is the PRECONDITION for the
          backfill append to be accepted — :meth:`apply_history_frame` discards
          any append while the flag is set. The append path only EXTENDS the
          bundle (it never replaces it or rolls the generation), so a truncated
          backfill drain leaves the bundle shorter-but-hole-free, and the next
          reconnect re-arms another incremental from the new water mark. The
          server's exposed coverage is therefore monotonic non-decreasing within
          a generation.

        * ``("full", None)`` — no trustworthy bundle to extend (no cache, a
          different machine now owns the flow, or an active worktree flow whose
          history is split across roots), so ``requires_full`` STAYS armed and
          the caller sends a cursorless full pull that rebuilds the bundle.

        Consolidating the decision here (under ``self._lock``) keeps it
        consistent with :meth:`apply_history_frame`'s invariants: the bundle
        presence/machine/cursor reads and the ``requires_full`` / recovery-inflight
        writes all happen in one critical section, so a racing append cannot slip
        between the decision and its dedup marker.

        *repair* is the interrupted-delivery entry point (see
        :meth:`history_delivery_repair_due`): the flow's bundle is a PREFIX of a
        delivery that died rather than a bundle a discarded append flagged, so
        nothing has armed ``requires_full`` for it. Arming it HERE — rather than
        in the caller — keeps the arm and the dedup marker in one critical
        section, so the latch can never be left set by a decision that then
        dispatches nothing (which would discard every live append of a still
        running flow until the recovery TTL).
        """
        async with self._lock:
            if not repair and flow_id not in self._history_requires_full:
                return None
            dispatched_at = self._history_recovery_inflight.get(flow_id)
            # Same monotonic-clock TTL dedup as ``take_recovery_pull``: at most
            # one recovery per flow is in flight, and a lost pull re-arms only
            # after the TTL, never on a backward wall-clock step.
            if (
                dispatched_at is not None
                and (time.monotonic() - dispatched_at) < self._HISTORY_RECOVERY_TTL
            ):
                return None
            # Past the dedup, so a plan IS about to be returned and dispatched:
            # only now may the latch be armed (see *repair* above).
            if repair:
                self._history_requires_full.add(flow_id)
            existing = self._history_data.get(flow_id)
            can_incremental = (
                existing is not None
                and bool(existing.get("records"))
                and bool(existing.get("cursor"))
                and str(existing.get("machine_id") or "") == machine_id
                and not self._is_active_worktree_flow_locked(flow_id)
            )
            self._history_recovery_inflight[flow_id] = time.monotonic()
            if can_incremental:
                # DISARM requires_full so the daemon's append backfill is
                # accepted (see the INVARIANT above); the bundle and its cursor
                # are left untouched — the backfill only extends them.
                self._history_requires_full.discard(flow_id)
                cursor_copy = dict(existing.get("cursor") or {})
                logger.info(
                    "hist-diag plan_recovery_pull flow=%s kind=incremental "
                    "machine=%s cursor=%s (backfilling [server water mark, now) "
                    "as append; requires_full cleared, generation preserved)",
                    flow_id, machine_id, cursor_copy,
                )
                return ("incremental", cursor_copy)
            logger.info(
                "hist-diag plan_recovery_pull flow=%s kind=full machine=%s "
                "(no reusable bundle / machine-change / active-worktree; "
                "requires_full kept armed, full rebuild)",
                flow_id, machine_id,
            )
            return ("full", None)

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
        is deliberately NOT a gap. It is the normal shape of three legitimate
        flows: a daemon that re-reads and re-sends a frame whose previous send
        failed (its cursor only advances on a successful send), a retried FAILED
        step that rewrites its jsonl in place, and the running-worktree self-heal
        drain, whose byte-capped ``full`` HEAD is refused as shrinking so every
        following tail re-covers the water mark. Forcing a full pull on any of
        them would turn a routine retry into a pull storm. What makes the overlap
        harmless is the SERVER CACHE: the append branch of
        :meth:`apply_history_frame` folds each record onto the bundle by
        ``(step_id, ordinal)``, so a re-delivered line is dropped and a rewritten
        one replaces its predecessor in place. (The frontend's
        ``reconcileAppendRecords`` applies the same rule to what a client holds,
        but it is the client-side half of the contract, not the place the
        duplicates are stopped.) Only a FORWARD jump loses information.

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

    async def get_history(
        self, flow_id: str, *, touch: bool = True
    ) -> Optional[Dict[str, Any]]:
        """Return a copy of cached history for *flow_id*, or ``None`` on miss.

        *touch* records the read as UI interest (see :meth:`_note_history_view`).
        The daemon receive loop passes ``touch=False``: it reads the bundle to
        hand a REST pull waiter its records, which is the DAEMON's push arriving,
        not a human looking — counting it would refresh every active flow as hot
        and defeat the eviction recency (see :attr:`_HISTORY_VIEW_HOT_WINDOW`).
        """
        async with self._lock:
            if touch:
                self._note_history_view(flow_id)
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
            pending = self._pending_positions_for(cached)
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
                # Same one-source-of-truth rule for the completeness bit: a
                # console told by an advisory that the bundle moved must be able
                # to read the same "this is a prefix, not the whole
                # conversation" statement the poll would give it.
                "incomplete": flow_id in self._history_deliveries,
            }

    async def get_history_usage(
        self, flow_id: str, *, rebuild: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Return the cached bundle's usage payload, or ``None``.

        Mirrors :meth:`get_history_bundle_meta` for the WS push path: the frame
        is read AFTER the frame's records were applied, so the payload describes
        the same bundle state a REST snapshot would return at this instant —
        one shared backend (:meth:`_bundle_usage`), never a second formula.

        The daemon-computed payload (stored on the bundle) is returned whenever
        present. A usage-bearing append refreshes that stored payload in place
        (see :meth:`_refresh_bundle_usage`), so the payload always describes
        the bundle's current records; the O(records) rebuild behind *rebuild*
        is the fallback for bundles that never received a daemon payload.
        """
        async with self._lock:
            cached = self._history_data.get(flow_id)
            if cached is None:
                return None
            stored = cached.get("usage")
            if isinstance(stored, dict):
                return stored
            if not rebuild:
                return None
            return self._bundle_usage(cached)

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
            key = cls._record_identity(record)
            if key is None:
                continue
            # INVARIANT: FIRST occurrence wins. A bundle that somehow holds two
            # records under one number (a pre-fix bundle still in memory, a
            # daemon full frame that carried a clone) must resolve that number to
            # the SAME position for every consumer — the backfill slice, and the
            # append reconcile that rewrites a retried line in place. Letting the
            # later copy win would make an in-place update land on a different
            # record than the one a numbered backfill just served.
            index.setdefault(key, position)
        return index

    @classmethod
    def _record_identity(cls, record: Any) -> Optional[Tuple[str, int]]:
        """The record's ``(step_id, ordinal)`` physical-line identity, or ``None``.

        ``None`` marks a record that is NOT addressable by number — a legacy
        pre-ordinal daemon's record, an optimistic echo, a non-dict line. Such a
        record's identity cannot be proven, so no path may fold it against
        another one: the append reconcile appends it verbatim, and
        :meth:`_index_records_by_ordinal` leaves it out of the index. Mirrors the
        frontend's ``recordOrdinal``-gated ``recordKey``, so both sides agree on
        which records are foldable.
        """
        if not isinstance(record, dict):
            return None
        step_id = record.get("step_id")
        ordinal = cls._record_ordinal(record)
        if not step_id or ordinal is None:
            return None
        return (str(step_id), ordinal)

    @classmethod
    def _bundle_key_index(
        cls, bundle: Dict[str, Any]
    ) -> Dict[Tuple[str, int], int]:
        """The bundle's ``(step_id, ordinal) -> position`` index, built on demand.

        Cached on the bundle under the private ``_key_index`` key alongside the
        record count it was built for (``_key_index_len``) — in-memory only,
        never serialized onto the wire, exactly like ``_usage_sources`` (every
        history getter picks its fields explicitly, so a private key cannot leak
        into a payload).

        WHY cached rather than rebuilt per frame: the whole point of the append
        reconcile is that a repeated whole-flow drain costs the SIZE OF THE
        FRAME, not the size of the bundle. Rebuilding the index on each of the
        dozens of tail frames a drain arrives in would put the O(bundle) term
        straight back.

        WHY the length guard: ``records`` has only two writers (this reconcile
        and the full-frame replacement, which drops the whole bundle dict), but
        the index must never silently mis-address a bundle some future path
        mutated behind its back. Comparing the recorded count against the live
        one is O(1) and degrades the worst case to a single rebuild instead of a
        wrong position.
        """
        records = bundle.get("records") or []
        index = bundle.get("_key_index")
        if isinstance(index, dict) and bundle.get("_key_index_len") == len(
            records
        ):
            return index
        index = cls._index_records_by_ordinal(records)
        bundle["_key_index"] = index
        bundle["_key_index_len"] = len(records)
        return index

    @classmethod
    def _reconcile_append_into(
        cls, bundle: Dict[str, Any], new_records: List[Any]
    ) -> Tuple[List[Any], int, int, int]:
        """Fold *new_records* into *bundle*'s records idempotently by identity.

        Returns ``(applied, added, updated, dropped)`` — the records that
        actually changed the bundle (new tail records plus the new content of
        in-place updates, in frame order) and the three counts for the caller's
        diagnostic log.

        Per record, keyed by :meth:`_record_identity`:

        * key unseen → appended to the tail (and indexed);
        * key held, record equal → DROPPED, the bundle is already correct;
        * key held, record differs → REPLACED at its existing position, so the
          record count and order are untouched (a retried FAILED step rewrote
          that physical jsonl line; the newest content is the truth);
        * no key (legacy / un-numbered) → appended verbatim, never folded.

        The bundle's ``generation`` is deliberately NOT touched here: the count
        and order only ever grow at the tail, so an outstanding progress token's
        offset keeps meaning what it meant and an in-sync client stays on the
        cheap ``not_modified`` reply.
        """
        records = bundle["records"]
        index = cls._bundle_key_index(bundle)
        applied: List[Any] = []
        added = updated = dropped = 0
        # WHY the byte total is maintained HERE rather than re-measured after the
        # append: this runs inside ``ServerState._lock``, and re-walking the whole
        # bundle to size it would put an O(bundle) term back on the critical
        # section for every frame — the same "heavy work proportional to bundle
        # size under the lock" the cache-budget work exists to remove. Folding
        # the delta into the loop that already touches exactly the changed
        # records keeps the accounting O(frame).
        size = bundle.get("_bytes")
        if not isinstance(size, int) or bundle.get("_bytes_len") != len(records):
            size = sum(_estimate_record_bytes(record) for record in records)
        for record in new_records:
            key = cls._record_identity(record)
            if key is None:
                records.append(record)
                applied.append(record)
                size += _estimate_record_bytes(record)
                added += 1
                continue
            position = index.get(key)
            if position is None:
                index[key] = len(records)
                records.append(record)
                applied.append(record)
                size += _estimate_record_bytes(record)
                added += 1
                continue
            if records[position] == record:
                dropped += 1
                continue
            size += _estimate_record_bytes(record) - _estimate_record_bytes(
                records[position]
            )
            records[position] = record
            applied.append(record)
            updated += 1
        bundle["_key_index_len"] = len(records)
        bundle["_bytes"] = max(0, size)
        bundle["_bytes_len"] = len(records)
        return applied, added, updated, dropped

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
    def _pending_positions_for(
        cls, bundle: Dict[str, Any]
    ) -> Dict[str, List[int]]:
        """*bundle*'s pending window, memoized on the bundle.

        WHY memoized: :meth:`_bundle_pending_positions` walks EVERY record, and
        it runs inside ``ServerState._lock`` on every REST snapshot read AND on
        every ``/ws/ui`` push-meta read — so on a multi-MB flow being polled
        every few seconds it was a recurring O(bundle) stall on the event loop
        for an answer that had not changed. The window is a pure function of the
        record set and the cursor, and the append reconcile only ever grows the
        record list at the tail or replaces a record under its EXISTING
        ``(step_id, ordinal)`` key (which cannot move the per-step highest
        ordinal), so ``(len(records), cursor)`` identifies it exactly. Cached
        under a private key, in memory only, like ``_key_index`` /
        ``_usage_sources``.
        """
        records = bundle.get("records") or []
        cursor = bundle.get("cursor") or {}
        key = (len(records), tuple(sorted((str(k), v) for k, v in cursor.items())))
        if bundle.get("_pending_key") == key:
            cached = bundle.get("_pending")
            if isinstance(cached, dict):
                return cached
        pending = cls._bundle_pending_positions(records, cursor)
        bundle["_pending_key"] = key
        bundle["_pending"] = pending
        return pending

    @classmethod
    def _locate_missing_positions(
        cls,
        records: List[Any],
        missing: Dict[str, List[int]],
        pending: Optional[Dict[str, List[int]]] = None,
        index: Optional[Dict[Tuple[str, int], int]] = None,
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
        # *index* lets the caller hand in the bundle's already-built
        # ``(step_id, ordinal) -> position`` map so a backfill does not rebuild
        # an O(bundle) index inside ``ServerState._lock``.
        if index is None:
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
        * ``incomplete`` — ``True`` while the bundle is missing the tail of a
          delivery the sender declared unfinished (see
          :class:`_OpenDelivery`). It is the ONE field not derived from the
          cached records, and therefore the only one that can contradict a
          truncated bundle's own self-consistency.
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
            # A UI client is asking for this flow RIGHT NOW — the per-flow
            # "someone is watching" signal the eviction recency is keyed on (see
            # ``_HISTORY_VIEW_HOT_WINDOW``). Stamped BEFORE the cache lookup so a
            # MISS counts too: the miss is what re-admits an evicted flow, so the
            # cursorless full pull the endpoint is about to fire can rebuild its
            # bundle.
            self._note_history_view(flow_id)
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
            #
            # WHY the remaining per-snapshot work is left ON the loop, under the
            # lock: it was measured, not assumed. With
            # ``scripts/measure_server_loop_stalls.py`` on a 16 MiB / 4000-record
            # bundle (CPython, this host) the whole set costs single-digit
            # milliseconds — pending window ~1.2 ms, ``_unnumbered_steps``
            # ~0.9 ms, the ordinal index ~1.8 ms, ``list(records)`` ~0.0 ms —
            # against ~235 ms for the usage rebuild that IS memoized and ~184 ms
            # for the render+gzip that IS batched / offloaded. Moving these off
            # the loop would buy a couple of milliseconds and cost the atomicity
            # that lets this method promise records and progress describe ONE
            # bundle state.
            pending = self._pending_positions_for(cached)
            backfill_positions: List[int] = []
            unfillable: Dict[str, List[int]] = {}
            served_backfill = False
            if is_delta and missing:
                (
                    backfill_positions,
                    unfillable,
                    needs_full,
                ) = self._locate_missing_positions(
                    records,
                    missing,
                    pending,
                    index=self._bundle_key_index(cached),
                )
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

            # INVARIANT: the returned payload is DETACHED from the cached bundle
            # — ``records`` is its own list (holding strong references to the
            # record dicts), ``cursor`` / ``unfillable`` / ``pending`` are copies,
            # and every scalar is read here under the lock.
            #
            # WHY that matters now: the REST route renders this payload to JSON
            # and gzips it in a WORKER THREAD (see ``app._history_response``),
            # which means the render runs with the lock released and the budget
            # sweep free to evict this very flow's bundle mid-render. Detachment
            # is what makes that a non-event: the in-flight response owns the
            # records it was handed and completes as a whole snapshot, while the
            # cache drops its own reference. There is no half snapshot and no
            # ``KeyError`` — the two paths simply stop sharing after this return.
            # The evicted bytes are genuinely reclaimed only once the response is
            # written, which the render-concurrency gate in ``app.py`` bounds.
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
                # INVARIANT: a bundle missing the tail of a delivery already in
                # progress may never present itself as settled. Every other field
                # of this reply is derived from the records that ACTUALLY
                # arrived, so a delivery cut in the middle produces a flawlessly
                # self-consistent PREFIX — the cursor names exactly the step
                # files that landed, ``pending`` is empty, the client's
                # ``stepId#ordinal`` self-check finds no hole — and the loss is
                # invisible to every party. This flag is the one field that is
                # NOT derived from the records: it comes from what the SENDER
                # said it would deliver (see ``_OpenDelivery``), which is why it
                # can contradict them. ``delivery`` still describes
                # client-vs-cache agreement (``not_modified`` = you hold what we
                # hold); this describes cache-vs-flow, and the two are
                # independent — the poll that repairs the bundle is dispatched
                # off exactly that combination.
                "incomplete": flow_id in self._history_deliveries,
                # True when the client presented a signed cursor (``after``) the
                # server could not bind to this bundle (see ``resync`` above) — a
                # stale/expired/rotated cursor that fell back to full. The reply's
                # ``progress``/``signature``/``generation`` are authoritative, so
                # the client resynchronises to them instead of bare-retrying the
                # dead cursor. False on a first-load full (no ``after``) and on
                # every honoured delta / not_modified / backfill.
                "resync": resync,
                "updated_at": cached.get("updated_at"),
                # The flow's usage/cost payload. Prefers the daemon-computed
                # payload (project pricing overrides applied on the owning
                # machine); when no full HISTORY_DATA frame has carried one
                # since connect, it is rebuilt from the cached records through
                # the SAME shared backend (tianluo.usage.build_usage_payload)
                # priced with the daemon-carried project catalog (built-in
                # fallback, never no catalog) — never a server-side formula,
                # and never a fabricated zero.
                "usage": self._bundle_usage(cached),
            }

    async def get_history_record_detail(
        self,
        flow_id: str,
        *,
        step_id: str,
        ordinal: int,
        tool_use_id: str,
        source: str,
        expected_machine_id: Optional[str] = None,
        expected_owner: Optional[str] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Read ONE chip's full detail out of the cached bundle.

        The counterpart of :func:`~tianluo.server.history_summary.summarize_history_records`:
        the browser receives collapsed-state fields only and comes back here for
        the body when the user expands a chip. The cache still holds the full
        bundle, so a hit costs one scan and no daemon round-trip.

        The chip is addressed by its record's stable ``(step_id, ordinal)``
        identity plus the ``tool_use_id`` within it — never by ``tool_use_id``
        alone, which repeats across a flow (codex synthesizes ``codex_tool_1``
        per call), so a flow-wide scan would answer one chip with another call's
        body.

        Ownership is validated exactly as in :meth:`get_history_snapshot` and
        under the same lock, so a flow that has moved daemons (or belongs to
        another owner) reads as a miss rather than leaking a stale body.

        Returns ``(bundle_readable, lookup)``. WHY the two are separate: only a
        MISSING bundle justifies a daemon re-pull. A bundle that is present and
        already holds the addressed record is authoritative — re-pulling it
        would spend a full multi-MB round trip (and a 10 s wait) to re-learn
        what the cache already knows, turning a stale browser tab's dead chip
        into daemon load.

        ``lookup`` carries :func:`locate_record_detail`'s verdict plus
        ``settled``: True when the answer cannot change by waiting — the record
        is here, or the daemon already streamed a higher ordinal for its step and
        so read past it. The detail route waits out a multi-frame daemon recovery
        precisely while ``settled`` is False, so it never has to guess that a
        silent gap means the recovery finished.
        """
        async with self._lock:
            # Expanding a chip is a UI read of this flow, so it refreshes the
            # eviction recency exactly like a snapshot read does — a reader
            # working through a long session's details must not have its bundle
            # swept out from under them by an idle-looking LRU.
            self._note_history_view(flow_id)
            cached = self._history_data.get(flow_id)
            if cached is None:
                return False, {"detail": None, "settled": False}
            bundle_machine = str(cached.get("machine_id") or "")
            if (
                expected_machine_id is not None
                and bundle_machine != expected_machine_id
            ):
                return False, {"detail": None, "settled": False}
            if expected_owner is not None:
                resolved_machine = self._find_machine_for_history_flow_locked(
                    flow_id, owner=expected_owner
                )
                if (
                    resolved_machine is None
                    or resolved_machine != expected_machine_id
                ):
                    return False, {"detail": None, "settled": False}
            # The returned payload references record sub-objects rather than
            # copying them; the same detachment argument as get_history_snapshot
            # applies — an eviction during the response render simply stops the
            # two paths sharing.
            found = locate_record_detail(
                cached["records"],
                step_id=step_id,
                ordinal=ordinal,
                tool_use_id=tool_use_id,
                source=source,
            )
            # INVARIANT: only the RECORDS settle the question, never the cursor.
            # A cursor total is not an upper bound on the file — a multi-frame
            # drain advances it frame by frame — and it is not a lower bound on
            # what is still coming either: the drain's head can declare the whole
            # file while its records lag far behind (that trailing window is
            # exactly what :meth:`_bundle_pending_positions` names *pending*).
            # Reading either direction as a verdict answers a chip "unavailable"
            # while its record is still on the wire. So the bundle speaks only
            # through what it HOLDS — the record itself, or a higher ordinal for
            # the same step, which proves the daemon read past this line — and
            # the route's deadline is the sole time bound.
            settled = bool(found["record_found"] or found["passed"])
            return True, {"detail": found["detail"], "settled": settled}

    @staticmethod
    def _catalog_for_bundle(cached: Dict[str, Any]) -> Any:
        """The pricing catalog to re-aggregate *cached*'s records with.

        The daemon's payload was priced with the project's effective catalog
        (its ``pricing.models`` overrides merged onto the built-in table); the
        serialized catalog rides the wire as the bundle's ``usage_catalog`` so
        a rebuild prices the same records with the same table — the server
        cannot reach the project's ``tianluo.yaml`` itself, which lives on the
        owning machine. A version-skewed daemon that sends no catalog degrades
        to the built-in table. Never ``None``: a catalog-less rebuild silently
        turns priced estimates into unknown-price (``estimate_record_cost``
        answers "no pricing catalog") and degrades completeness to partial,
        making the WebUI disagree with the CLI history view for the same flow.
        """
        stored = cached.get("usage_catalog")
        if isinstance(stored, dict):
            from tianluo.pricing import PricingCatalog

            try:
                return PricingCatalog.from_dict(stored)
            except Exception:
                # A malformed catalog must not block history delivery; the
                # built-in table still prices everything it can.
                logger.warning(
                    "hist-diag usage catalog for flow %s is malformed; "
                    "falling back to the built-in price table",
                    cached.get("flow_id"),
                )
        from tianluo.pricing import PricingCatalog

        return PricingCatalog.builtin()

    @staticmethod
    def _invalidate_usage_rebuild(bundle: Dict[str, Any]) -> None:
        """Drop the memoized usage rebuild for *bundle*.

        Called wherever the bundle's PRICING inputs change without its record
        count changing — a later-arriving ``usage_catalog`` on a re-pull. The
        memo below is keyed on the record count alone, so the catalog swap is
        the one input it cannot see; forgetting to drop it here would serve a
        summary priced with the superseded table.
        """
        bundle.pop("_usage_rebuild", None)
        bundle.pop("_usage_rebuild_len", None)

    @staticmethod
    def _bundle_usage(cached: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return the bundle's usage payload, rebuilding it when absent.

        The rebuild path is the shared-backend fallback: the daemon's payload
        (computed with the project's pricing overrides) is authoritative when
        it has arrived; otherwise the cached records — which carry each
        message's ``usage_records`` / legacy ``token_usage`` — are aggregated
        through :func:`tianluo.usage.build_usage_payload` exactly as the CLI
        history view does, priced with the bundle's daemon-carried catalog
        (built-in fallback — never no catalog). Returns ``None`` when the
        bundle holds no usage at all, so the wire omits the field instead of a
        misleading zero summary.

        WHY the rebuild is memoized on the bundle: this is the single most
        expensive thing that ran inside ``ServerState._lock``. Measured with
        ``scripts/measure_server_loop_stalls.py`` on history payloads of this
        shape (CPython, this host): extraction + ``build_usage_payload`` costs
        ~90 ms for a 3 MiB bundle and ~235 ms for a 16 MiB one — several times
        the REST render (~53 ms, now batched onto the loop) and its gzip
        (~131 ms, now genuinely offloaded), and unlike either of those it re-ran
        on EVERY ~3 s
        WebUI poll of a flow whose daemon never sent a usage payload (a
        version-skewed daemon, or one whose own usage build failed). A quarter
        second of frozen loop per poll, holding the state lock, is the largest
        confirmed stall on the steady-state path.

        The memo is keyed on the record count, exactly like ``_key_index`` /
        ``_bytes``: appends only grow the tail or replace a record under its
        existing ``(step_id, ordinal)`` key, and a usage-bearing replacement
        goes through :meth:`_refresh_bundle_usage`, which writes the STORED
        ``usage`` the branch above returns before this memo is ever consulted. A
        full frame builds a whole new bundle dict, so it starts memo-free. The
        ``None`` verdict is memoized too — "this bundle carries no usage at all"
        costs the same full walk to establish as a real payload does.
        """
        stored = cached.get("usage")
        if isinstance(stored, dict):
            return stored
        records = cached.get("records") or []
        if cached.get("_usage_rebuild_len") == len(records):
            return cached.get("_usage_rebuild")
        sources = ServerState._usage_sources_from_records(records)
        payload: Optional[Dict[str, Any]] = None
        if sources:
            from tianluo.usage import build_usage_payload

            payload = build_usage_payload(
                sources,
                ServerState._catalog_for_bundle(cached),
                call_id=str(cached.get("flow_id") or "flow"),
            )
        cached["_usage_rebuild"] = payload
        cached["_usage_rebuild_len"] = len(records)
        return payload

    @staticmethod
    def _records_carry_usage(records: List[Any]) -> bool:
        """Whether any record carries per-call usage (modern or legacy shape)."""
        for record in records:
            if not isinstance(record, dict):
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            raw_records = message.get("usage_records")
            if isinstance(raw_records, list) and any(
                isinstance(item, dict) for item in raw_records
            ):
                return True
            if message.get("role") == "assistant" and isinstance(
                message.get("token_usage"), dict
            ):
                return True
        return False

    @staticmethod
    def _usage_sources_from_records(
        records: List[Any],
    ) -> "Dict[str, List[Any]]":
        """Extract per-step usage records from history records.

        The same recovery the CLI history view performs: each message's
        ``usage_records`` list (modern per-call records) or legacy five-field
        ``token_usage`` tally (adapted via the legacy adapter, flagged
        legacy_ambiguous). Shared by :meth:`_bundle_usage` and
        :meth:`_refresh_bundle_usage` so the rebuild and the incremental
        append path can never disagree on extraction.
        """
        from tianluo.usage import UsageRecord, legacy_usage_record

        sources: Dict[str, List[Any]] = {}
        for position, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            step_id = str(record.get("step_id") or "")
            raw_records = message.get("usage_records")
            # Truthiness, not isinstance: an EMPTY usage_records list is not a
            # measurement and must not swallow the legacy token_usage fallback
            # (same rule as chat_history / daemon history — one shared result).
            if raw_records:
                step_records = sources.setdefault(step_id, [])
                for raw in raw_records:
                    if isinstance(raw, dict):
                        step_records.append(UsageRecord.from_dict(raw))
            elif message.get("role") == "assistant" and isinstance(
                message.get("token_usage"), dict
            ):
                # The frame position backs a missing ordinal so two messages of
                # one step cannot share a call id and collapse in dedup (same
                # rule as the daemon's history extraction).
                ordinal = record.get("ordinal")
                marker = ordinal if ordinal is not None else f"pos{position}"
                sources.setdefault(step_id, []).append(
                    legacy_usage_record(
                        message["token_usage"],
                        call_id=f"legacy:{step_id}:{marker}",
                    )
                )
        return sources

    @classmethod
    def _refresh_bundle_usage(
        cls, bundle: Dict[str, Any], new_records: List[Any]
    ) -> None:
        """Refresh *bundle*'s stored usage payload for usage-bearing appends.

        The daemon computes its payload only for full snapshots, so appending
        records that carry usage would leave the stored summary describing the
        pre-append bundle. The extracted sources are cached on the bundle
        (``_usage_sources``, in-memory only — never serialized onto the wire),
        so the refresh is incremental: the first usage-bearing append after a
        full frame extracts once from the whole record set, every later one
        extends the cache with just the new records and re-aggregates through
        the SAME shared backend (:func:`tianluo.usage.build_usage_payload`) as
        the full-frame payload — priced with the daemon-carried project
        catalog (built-in fallback, never no catalog) so the estimates keep
        the same prices the stored daemon payload used. An append that carries
        no usage leaves the stored payload untouched.
        """
        sources = bundle.get("_usage_sources")
        if not isinstance(sources, dict):
            # Bundle predates the incremental cache (or was just replaced by a
            # full frame): extract once from the current record set — which
            # already includes *new_records* — then keep extending the cache.
            sources = cls._usage_sources_from_records(bundle.get("records") or [])
        else:
            for step_id, records in cls._usage_sources_from_records(
                new_records
            ).items():
                sources.setdefault(step_id, []).extend(records)
        bundle["_usage_sources"] = sources
        # This path owns the bundle's usage truth from here on; drop the
        # record-count-keyed rebuild memo so an append that leaves the count
        # untouched (an in-place replacement of a retried step's line) can never
        # be answered from a summary computed before it.
        cls._invalidate_usage_rebuild(bundle)
        if not any(sources.values()):
            return
        from tianluo.usage import build_usage_payload

        bundle["usage"] = build_usage_payload(
            sources,
            cls._catalog_for_bundle(bundle),
            call_id=str(bundle.get("flow_id") or "flow"),
        )

    async def find_machine_for_history_flow(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> Optional[str]:
        """Resolve which machine owns *flow_id* for an on-demand history pull.

        Checks the reported history index first, then any cached history
        bundle's owner, then the live flow set — so a flow can be pulled
        whether it is historical or still active. With *owner* set, a candidate
        machine is only accepted when it is bound to that owner, so one owner
        cannot pull another owner's history.

        The whole three-stage resolution runs once over the REACHABLE machines
        (live socket, not the debounced presence flag) and only then over every
        machine (see :meth:`_find_machine_for_history_flow_locked`), so a
        still-connected machine wins over a disconnected one that shares the
        same filesystem.
        """
        async with self._lock:
            return self._find_machine_for_history_flow_locked(flow_id, owner=owner)

    def _find_machine_for_history_flow_locked(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> Optional[str]:
        """Locked implementation of :meth:`find_machine_for_history_flow`.

        WHY the three stages are replayed as TWO whole passes (reachable
        machines first, then all machines) instead of making each stage
        individually reachable-first: servability outranks how authoritative the
        evidence is. A
        pull routed at a disconnected machine can only fail, so a reachable
        machine that merely carries the flow in its live set is strictly more
        useful than an unreachable one holding a history-index entry for it.
        Ordering within a stage would leave the window right after a
        shared-filesystem failover — the new machine connected and pushed
        STATUS_UPDATE but not yet HISTORY_INDEX — resolving to the dead machine,
        which is exactly the 404 this exists to prevent. With no reachable
        candidate the second pass reproduces the original stage order verbatim.
        """

        def _accept(machine_id: str) -> bool:
            if owner is None:
                return True
            record = self._machines.get(machine_id)
            return record is not None and _owned(record, owner)

        def _resolve(online_only: bool) -> Optional[str]:
            def _candidate(machine_id: str) -> bool:
                if not _accept(machine_id):
                    return False
                if not online_only:
                    return True
                record = self._machines.get(machine_id)
                return self._machine_is_reachable(machine_id, record)

            for machine_id, sessions in self._history_index.items():
                if not _candidate(machine_id):
                    continue
                for session in sessions:
                    if str(session.get("flow_id") or "") == flow_id:
                        return machine_id
            cached = self._history_data.get(flow_id)
            if cached is not None and cached.get("machine_id"):
                cached_mid = str(cached["machine_id"])
                if _candidate(cached_mid):
                    return cached_mid
            for machine_id, record in self._iter_owned_machines_online_first(
                owner, online_only=online_only
            ):
                if flow_id in record.flows:
                    return machine_id
            return None

        return _resolve(True) or _resolve(False)

    async def get_history_flow_project_root(
        self, flow_id: str, *, owner: Optional[str] = None
    ) -> Optional[str]:
        """Resolve the authoritative ``project_root`` a flow runs under.

        This is the single source of truth the on-demand history pull uses to
        tell the daemon *which* root to read, instead of letting the daemon
        guess by scanning its whole project-root registry and taking the first
        root that happens to contain ``tianluo/history/<flow_id>/``. A worktree-mode
        flow runs its discovery step in the main repo root (writing one
        ``01_discovery`` file there) and every later step under the worktree
        root, so two distinct roots can each contain a ``tianluo/history/<flow_id>``
        directory; without the authoritative root the daemon's first-match
        heuristic returns the main repo's discovery-only directory and the web
        view freezes after the first step.

        Resolution order, all owner-scoped:

        1. The reported history index (the daemon's ``SessionMeta`` carries the
           authoritative ``project_root`` of each flow's run).
        2. The live flow set (an active flow that has not yet been indexed).

        Both stages run once over the REACHABLE machines and only then over
        every machine. WHY: the resolved root is handed to whichever daemon the
        pull is routed to, and that routing
        (:meth:`_find_machine_for_history_flow_locked`) is reachable-first — a
        root reported by a disconnected machine would describe a different node's
        view of the shared filesystem than the one about to read it. The
        index-before-live-flow priority (which is what keeps a worktree flow's
        main-repo discovery root from shadowing the worktree root) is preserved
        inside each pass.

        Returns the non-empty ``project_root`` string, or ``None`` when the
        flow is unknown, owner-scoped out, or has no recorded root (the caller
        then degrades to the legacy empty-``project_root`` behaviour).
        """

        def _accept(machine_id: str, online_only: bool) -> bool:
            record = self._machines.get(machine_id)
            if owner is not None and (record is None or not _owned(record, owner)):
                return False
            if not online_only:
                return True
            return self._machine_is_reachable(machine_id, record)

        def _resolve(online_only: bool) -> Optional[str]:
            # 1) History index — the authoritative SessionMeta.project_root.
            for machine_id, sessions in self._history_index.items():
                if not _accept(machine_id, online_only):
                    continue
                for session in sessions:
                    if str(session.get("flow_id") or "") == flow_id:
                        root = str(session.get("project_root") or "")
                        if root:
                            return root
            # 2) Fall back to the live flow set.
            for _machine_id, record in self._iter_owned_machines_online_first(
                owner, online_only=online_only
            ):
                flow = record.flows.get(flow_id)
                if flow is not None:
                    root = str(flow.project_root or "")
                    if root:
                        return root
            return None

        async with self._lock:
            return _resolve(True) or _resolve(False)

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
        ``…/tianluo/worktrees/<name>`` (:func:`_is_worktree_session_path`). The
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

        INVARIANT: the verdict is read off the SAME machine snapshot
        :meth:`get_flow` resolves to, hence the shared reachable-first ordering.
        On a shared filesystem the machine a resumed worktree flow moved away
        from keeps a frozen ``completed`` snapshot of that flow_id; judging off
        it would answer ``False`` for a run that is live on the machine now
        serving it, silently disabling all three guards this gates (the
        empty-full rejection in :meth:`apply_history_frame`, the incremental
        recovery-pull choice, and the history endpoint's self-heal) and
        re-freezing worktree discovery at round 1.
        """
        for _machine_id, record in self._iter_owned_machines_online_first(owner):
            flow = record.flows.get(flow_id)
            if flow is None:
                continue
            if str(flow.status or "").lower() not in ("running", "paused"):
                return False
            return _is_worktree_session_path(flow.project_root)
        return False

    # -- issue mirror (from daemon STATUS_UPDATE snapshots) -----------------

    def _iter_issue_mirrors_online_first(
        self, owner: Optional[str], *, online_only: bool = False
    ) -> List[Tuple[str, Dict[str, List[Dict[str, Any]]]]]:
        """Owner-visible issue mirrors, all REACHABLE machines first.

        The issue-mirror twin of :meth:`_iter_owned_machines_online_first`:
        caller MUST already hold ``self._lock``, each segment keeps the original
        ``self._issues`` insertion order, and *online_only* trims the result to
        the reachable segment for callers resolving in two whole passes.

        WHY the issue mirror needs the same ordering as flow resolution: issues
        live in ``tianluo/issues/*.yaml`` under the project root, so on a shared
        filesystem every daemon that ever saw that root mirrors the SAME issue
        id, and ``mark_offline`` keeps a disconnected machine's mirror. Resolving
        an issue to the first machine in insertion order therefore hands the
        issue commands (ISSUE_COMMAND, and the SPAWN_FLOW behind "start flow from
        issue") a machine no frame can reach — and unlike the debounced presence
        flag nothing ever expires that choice, so the 404 is permanent until the
        dead machine's record is evicted, even though a connected machine can
        read and write the identical YAML file.

        Owner gating matches the pre-existing issue-query semantics exactly: a
        mirror whose machine record is missing stays visible to the unscoped
        (``owner is None``) view and is fail-closed out of any owner-scoped one.
        """
        online: List[Tuple[str, Dict[str, List[Dict[str, Any]]]]] = []
        offline: List[Tuple[str, Dict[str, List[Dict[str, Any]]]]] = []
        for machine_id, by_root in self._issues.items():
            record = self._machines.get(machine_id)
            if owner is not None and (
                record is None or not _owned(record, owner)
            ):
                continue
            if self._machine_is_reachable(machine_id, record):
                online.append((machine_id, by_root))
            elif not online_only:
                offline.append((machine_id, by_root))
        return online + offline

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

        An issue is collapsed to a single entry across machines, keyed by
        ``(project_root, id)`` — the identity of the on-disk YAML file — and
        preferring the copy mirrored by a REACHABLE machine, then the one with
        the newest ``updated_at`` (ties keep the first encountered so the result
        stays deterministic). WHY: on a shared filesystem every daemon that ever
        saw the root mirrors the same issue, so without collapsing one issue
        shows up once per machine that ever saw it; the reachability preference
        additionally keeps the listed ``machine_id`` equal to the one
        :meth:`get_issue_by_id` will route a command to. The key stays
        root-scoped because ids are only unique WITHIN a project — two projects
        each holding an ``I-42`` are two different issues and must both survive.
        """
        async with self._lock:
            result: List[Dict[str, Any]] = []
            # (project_root, issue_id) -> (index into result, machine reachable)
            # of the winner currently held for that issue.
            chosen: Dict[Tuple[str, str], Tuple[int, bool]] = {}
            for mid, by_root in self._iter_issue_mirrors_online_first(owner):
                # Machine gate
                if machine_id and mid != machine_id:
                    continue
                reachable = self._machine_is_reachable(
                    mid, self._machines.get(mid)
                )
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
                        issue_id = str(entry.get("id") or "")
                        if not issue_id:
                            # Unaddressable: no id to collapse on, so it passes
                            # through rather than being hidden behind another.
                            result.append(entry)
                            continue
                        key = (root, issue_id)
                        prior = chosen.get(key)
                        if prior is None:
                            chosen[key] = (len(result), reachable)
                            result.append(entry)
                            continue
                        index, prior_reachable = prior
                        if reachable != prior_reachable:
                            wins = reachable
                        else:
                            wins = str(entry.get("updated_at") or "") > str(
                                result[index].get("updated_at") or ""
                            )
                        if wins:
                            result[index] = entry
                            chosen[key] = (index, reachable)
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

        Machine resolution is **reachable-first with an unreachable fallback**
        (see :meth:`_iter_issue_mirrors_online_first`), matching every flow→
        machine lookup.

        INVARIANT: this is the single issue→machine resolution behind every
        issue write path — the edit/close/reopen ISSUE_COMMANDs and the
        SPAWN_FLOW of "start flow from issue" alike. All of them dispatch a frame
        to the machine returned here, so a machine that cannot be reached must
        never be preferred over one mirroring the same issue off the same shared
        filesystem: that preference is a permanent "machine ... is not connected"
        404, since nothing ages a dead machine's issue mirror out.
        """
        async with self._lock:
            for mid, by_root in self._iter_issue_mirrors_online_first(owner):
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

        Both sources are scanned in TWO WHOLE PASSES — every reachable machine
        first (issue mirror, then registered roots), and only if none matches the
        same scan over the rest. WHY reachability outranks which source knows the
        root: the caller routes a frame to the returned machine, so a connected
        machine that merely has the root registered can serve it while a
        disconnected one with a full issue mirror can only fail the dispatch. On
        a shared filesystem both machines address the identical directory, so
        there is nothing the unreachable one knows better.
        """
        async with self._lock:
            for online_only in (True, False):
                for mid, by_root in self._iter_issue_mirrors_online_first(
                    owner, online_only=online_only
                ):
                    if project_root in by_root:
                        return mid
                for mid, record in self._iter_owned_machines_online_first(
                    owner, online_only=online_only
                ):
                    if project_root in record.project_roots:
                        return mid
        return None
