"""The daemon↔server WebSocket protocol.

This module is the *single source of truth* for the wire protocol spoken
between an :class:`~se3.daemon.client.DaemonClient` (running inside a resident
``se3 daemon``) and the central server (``se3-server``). Both sides import this
module — the daemon from the core package, the server from ``se3.server`` —
so the message schema can never drift between them.

Wire format
-----------
Every message is a JSON object with exactly four top-level keys::

    {"type": <str>, "seq": <int>, "timestamp": <float>, "payload": <object>}

* ``type`` — one of the message-type constants below.
* ``seq`` — a monotonically increasing per-connection sequence number,
  assigned by the sender (0 when not tracked).
* ``timestamp`` — Unix epoch seconds at send time.
* ``payload`` — a type-specific JSON object (see the per-type helpers).

Message directions
------------------
* daemon → server: :data:`MSG_HELLO`, :data:`MSG_STATUS_UPDATE`,
  :data:`MSG_KEEPALIVE`, :data:`MSG_CALL_NOTIFICATION`, :data:`MSG_PONG`,
  :data:`MSG_HISTORY_INDEX`, :data:`MSG_HISTORY_INDEX_DELTA`,
  :data:`MSG_HISTORY_DATA`, :data:`MSG_DETAIL_DATA`, :data:`MSG_ISSUE_RESULT`,
  :data:`MSG_SPAWN_FAILED`.
* server → daemon: :data:`MSG_WELCOME`, :data:`MSG_SPAWN_FLOW`,
  :data:`MSG_RESPOND_CALL`, :data:`MSG_PING`, :data:`MSG_HISTORY_REQUEST`,
  :data:`MSG_HISTORY_INDEX_REQUEST`, :data:`MSG_INTERJECT_FLOW`,
  :data:`MSG_ISSUE_COMMAND`, :data:`MSG_DETAIL_REQUEST`,
  :data:`MSG_END_SESSION`.

Backward compatibility
----------------------
Protocol version 2 added the history messages. A peer speaking an older
revision will never *send* them; if it ever *receives* one it does not
recognise, the frame is rejected as an unknown type — callers decoding
untrusted frames should therefore tolerate :class:`ProtocolError` rather
than crash, so new and old peers can interoperate.

Protocol version 3 added the *traffic-reduction* messages
(:data:`MSG_KEEPALIVE`, :data:`MSG_HISTORY_INDEX_DELTA`,
:data:`MSG_DETAIL_REQUEST`, :data:`MSG_DETAIL_DATA`). Unlike the earlier
additive types, these carry a real behavioural downgrade risk: if a daemon
sent a KEEPALIVE (in place of a periodic STATUS_UPDATE) or an incremental
HISTORY_INDEX_DELTA to a version-2 server, that server would reject the frame
as an unknown type and lose the heartbeat / index update entirely. The version
was therefore bumped to ``3`` so each side can read the peer's advertised
``protocol_version`` (HELLO / WELCOME) and **fall back to the full-frame
semantics** — periodic full STATUS_UPDATE and full HISTORY_INDEX, no keepalive
or delta, detail inlined rather than fetched on demand — whenever the peer
speaks a revision older than 3. The version-negotiation and fall-back logic
lives in the daemon client and server relay; this module only owns the wire
schema, the version constant, and this contract. Callers decoding untrusted
frames must still tolerate :class:`ProtocolError` for genuinely unknown types.

The multi-tenant control plane added an optional ``key`` field to the HELLO
payload (the daemon credential the server resolves to an owner). It is purely
additive: a daemon with no key omits the field, and an older single-tenant
server that does not understand it simply ignores it — so the version was not
bumped for it. The key is a secret and MUST never be logged.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional

# Protocol revision. Bumped only on a breaking wire change; both daemon and
# server advertise it in HELLO / WELCOME so a mismatch can be surfaced.
# Revision "2" added the history messages (MSG_HISTORY_*).
# Revision "3" added the traffic-reduction messages (MSG_KEEPALIVE,
# MSG_HISTORY_INDEX_DELTA, MSG_DETAIL_REQUEST/DATA); a peer advertising "2" or
# older must be driven with full-frame semantics (see the module docstring).
PROTOCOL_VERSION = "3"

#: Minimum peer ``protocol_version`` that understands the revision-3
#: traffic-reduction messages. When a peer advertises a value below this in its
#: HELLO / WELCOME, the sender MUST fall back to the full-frame semantics
#: (periodic full STATUS_UPDATE + full HISTORY_INDEX, no keepalive/delta, detail
#: inlined) instead of emitting a keepalive / index delta the peer would reject.
#: Version strings are compared as integers with a safe fallback so a
#: non-numeric or missing value degrades to "legacy" (full semantics).
MIN_VERSION_TRAFFIC_REDUCTION = 3


def supports_traffic_reduction(peer_version: Any) -> bool:
    """Return whether *peer_version* understands the revision-3 lean messages.

    Used by both the daemon client and the server relay to decide, per peer,
    whether it is safe to emit :data:`MSG_KEEPALIVE` /
    :data:`MSG_HISTORY_INDEX_DELTA` / detail messages, or whether the peer is a
    legacy revision that must be driven with full-frame semantics. A missing or
    non-numeric version degrades safely to ``False`` (full semantics).
    """
    try:
        return int(str(peer_version).strip()) >= MIN_VERSION_TRAFFIC_REDUCTION
    except (TypeError, ValueError):
        return False

# Default TCP port for the central server. This is the *single source of
# truth* for the default port: ``se3-server`` binds it when ``--port`` is
# omitted, and the daemon client fills it in when ``--server-url`` carries no
# explicit port. Keeping it here — alongside the wire protocol — guarantees
# both sides agree and removes the duplicated ``8080`` magic numbers.
DEFAULT_SERVER_PORT = 8080

# Default TCP port for the *TLS* (``wss://``) scheme. The daemon client fills
# this in — instead of :data:`DEFAULT_SERVER_PORT` — when ``--server-url``
# carries a ``wss://`` (or ``https://`` normalized to ``wss://``) scheme with
# no explicit port, because a TLS connection terminates at the reverse proxy's
# HTTPS port (443), not at se3-server's plaintext default (8080). In short:
# 8080 is the plaintext / ``ws`` default (and ``se3-server --port`` default),
# 443 is the ``wss`` scheme-aware default. This keeps both defaults as named
# constants here — the single source of truth — rather than as magic numbers
# scattered through the client.
DEFAULT_SERVER_TLS_PORT = 443

# Maximum size, in bytes, of a single daemon↔server WebSocket message frame.
# This is the *single source of truth* for the per-frame inbound cap on both
# sides: the daemon passes it as ``websockets.connect(max_size=…)`` and the
# server passes it as ``uvicorn.run(ws_max_size=…)``. Sharing the one constant
# keeps the two ends from drifting apart, exactly like DEFAULT_SERVER_PORT.
#
# It is raised well above the library defaults (websockets' 1 MiB, uvicorn's
# 16 MiB) because a ``MSG_HISTORY_DATA`` frame carrying a full session's
# conversation records is currently ~33-39 MB — under the old defaults the
# server silently dropped the oversized frame, so ``GET /api/history/{flow_id}``
# never resolved and returned 504. 256 MiB is a bounded large ceiling: it
# comfortably absorbs today's frames with headroom while still capping a
# pathological frame to protect server memory (we deliberately do not use
# ``None``/unbounded).
MAX_WS_MESSAGE_BYTES = 256 * 1024 * 1024

# -- message types: daemon -> server --------------------------------------
MSG_HELLO = "hello"
MSG_STATUS_UPDATE = "status_update"
MSG_CALL_NOTIFICATION = "call_notification"
MSG_PONG = "pong"
MSG_HISTORY_INDEX = "history_index"
MSG_HISTORY_DATA = "history_data"
#: daemon → server: an extra-small heartbeat frame emitted in place of a
#: periodic STATUS_UPDATE when the aggregated snapshot's content signature is
#: unchanged since the last push. It carries only the signature (and the seq /
#: timestamp every frame has), so the server can refresh the daemon's
#: online/last-seen time — preserving the exact offline-detection semantics of a
#: STATUS_UPDATE — without the daemon re-sending the full snapshot. Revision 3;
#: only sent to a peer that advertises support (see the module docstring).
MSG_KEEPALIVE = "keepalive"
#: daemon → server: an incremental history-index update carrying only the
#: SessionMeta rows that changed (``upserts``, keyed by ``flow_id``) and the
#: flow ids that disappeared (``removed``), instead of the whole index. The
#: server merges it into its in-memory full index. Full :data:`MSG_HISTORY_INDEX`
#: frames are still sent on connect / reconnect / HISTORY_INDEX_REQUEST as the
#: reconciliation baseline. Revision 3; only sent to a supporting peer.
MSG_HISTORY_INDEX_DELTA = "history_index_delta"
#: daemon → server: deliver the full text requested by a :data:`MSG_DETAIL_REQUEST`
#: (an issue's untruncated description, or a pending call's full prompt). Carries
#: the echoed ``request_id`` so the server can correlate it to the waiting
#: REST request, plus ``ok`` / ``detail`` / ``error``. Revision 3.
MSG_DETAIL_DATA = "detail_data"
#: daemon → server: report that a server-requested spawn / resume / project
#: init failed *after* the SPAWN_FLOW was dispatched. ``POST /api/flows``
#: replies ``202 dispatched`` immediately (the daemon spawns asynchronously),
#: so a failure that happens during ``ensure_se3_project`` / the fresh spawn /
#: a resume would otherwise be silent and leave the web UI stuck on the
#: "published" pseudo-success state. This frame carries the project root, the
#: real error text, and the originating task / issue / resume id so the server
#: can route it back to the UI as a visible error. Older servers that do not
#: recognise the type simply ignore it (mixed-version compatibility).
MSG_SPAWN_FAILED = "spawn_failed"

# -- message types: server -> daemon --------------------------------------
MSG_WELCOME = "welcome"
MSG_SPAWN_FLOW = "spawn_flow"
MSG_RESPOND_CALL = "respond_call"
MSG_PING = "ping"
MSG_HISTORY_REQUEST = "history_request"
#: server → daemon: force a fresh rebuild + immediate re-push of the history
#: index (:data:`MSG_HISTORY_INDEX`), bypassing the daemon's change-debounce.
#: The web ``GET /api/history`` broadcasts this to every connected daemon so
#: entering the history view always reflects the latest sessions rather than
#: the last index a daemon happened to push. The payload is empty — it has no
#: flow dimension and merely triggers the re-push.
MSG_HISTORY_INDEX_REQUEST = "history_index_request"
#: server → daemon: deliver a mid-flow user interjection to a running flow.
#: The daemon turns it into an ``interjection``-kind call file under
#: ``se3/calls/`` which ``se3 run`` drains at the next step boundary.
MSG_INTERJECT_FLOW = "interject_flow"

#: server → daemon: end (terminate + archive) a session by ``flow_id``. The
#: daemon validates the flow against its supervisor and then off-loads the heavy
#: work — gracefully terminating the live ``se3 run`` process and archiving a
#: worktree session the way a normally-completed session would be cleaned up —
#: to an ``se3 end-session`` subprocess, so the event loop is never blocked by
#: the grace wait or the on-disk archival. Older daemons that do not recognise
#: the type simply ignore it (mixed-version compatibility), so no
#: ``PROTOCOL_VERSION`` bump is required.
MSG_END_SESSION = "end_session"

#: server → daemon: instruct the daemon to execute an issue write operation
#: (create / edit / close / reopen). The daemon resolves the project root,
#: validates the operation and delegates to :class:`IssueManager`.
MSG_ISSUE_COMMAND = "issue_command"

#: daemon → server: acknowledge the result of a :data:`MSG_ISSUE_COMMAND`.
#: Carries ``request_id`` (echoed from the command) and either ``ok=true``
#: or ``ok=false`` with an ``error`` message.
MSG_ISSUE_RESULT = "issue_result"

#: server → daemon: pull the *full text* of a single issue or pending call on
#: demand. STATUS_UPDATE now carries only truncated summaries (issue
#: descriptions / call prompts clipped for wire economy); when the operator
#: opens a detail view the server routes this request to the owning daemon,
#: which reads the untruncated content and replies with :data:`MSG_DETAIL_DATA`.
#: Revision 3; only used when the daemon advertises support.
MSG_DETAIL_REQUEST = "detail_request"

# -- detail-request kinds -------------------------------------------------
# The ``kind`` field of a MSG_DETAIL_REQUEST / MSG_DETAIL_DATA payload names
# which on-demand full-text artifact is being fetched, so the daemon knows
# whether to read an issue record or a pending call file.
DETAIL_KIND_ISSUE = "issue"
DETAIL_KIND_CALL = "call"
#: Every recognised detail-request kind.
DETAIL_KINDS: FrozenSet[str] = frozenset({DETAIL_KIND_ISSUE, DETAIL_KIND_CALL})

#: Valid values for the ``mode`` field of a :data:`MSG_HISTORY_DATA` payload.
HISTORY_MODE_FULL = "full"
HISTORY_MODE_APPEND = "append"
HISTORY_MODES: FrozenSet[str] = frozenset({HISTORY_MODE_FULL, HISTORY_MODE_APPEND})

# -- interaction-call kinds -----------------------------------------------
# Every human-in-the-loop interaction inside a running flow is carried by a
# single artifact: a JSON call file under ``<project>/se3/calls/``. Its
# ``kind`` field is one of the constants below, so the daemon aggregator and
# the web console can render and route each interaction without guessing.
# Legacy call files written before this field existed have no ``kind`` key
# and MUST be treated as :data:`CALL_KIND_CALL` for backward compatibility.
CALL_KIND_CALL = "call"
CALL_KIND_INTERJECTION = "interjection"
CALL_KIND_RETRY_DECISION = "retry_decision"
CALL_KIND_CLI_CONFIRM = "cli_confirm"
#: A non-interactive discovery confirmation gate: the flow has produced a
#: refined task description and is waiting for the user to confirm (reply with
#: the literal ``"1"``) before transitioning to ANALYZE. The call carries the
#: refined description in its prompt and a one-click confirm ``option`` whose
#: value is ``"1"`` so the web console can render both the ``输入 1 确认``
#: textual fallback and a GUI confirm button.
CALL_KIND_DISCOVERY_CONFIRM = "discovery_confirm"
#: A human review/approval gate for a completed step (plan / adjudicate / …):
#: the flow is PAUSED waiting for the operator to approve the reviewed step or
#: request changes. The call carries a human-readable ``prompt`` and, in its
#: ``context``, ``step_to_review_type`` / ``step_to_review_id`` (and, for an
#: ``adjudicate`` gate, the ruling's ``adjudication_rationale`` /
#: ``adjudicated_description`` / pre-ruling ``baseline``) so the web console can
#: render an Approve/Reject button pair plus the diff instead of forcing the
#: operator to guess a free-text answer. The structured reply travels back as
#: ``{"approved": bool, "feedback": ...}`` through the existing respond path.
CALL_KIND_CONFIRM = "confirm"
#: Every recognised interaction-call kind.
CALL_KINDS: FrozenSet[str] = frozenset(
    {
        CALL_KIND_CALL,
        CALL_KIND_INTERJECTION,
        CALL_KIND_RETRY_DECISION,
        CALL_KIND_CLI_CONFIRM,
        CALL_KIND_DISCOVERY_CONFIRM,
        CALL_KIND_CONFIRM,
    }
)

#: Messages a daemon is allowed to send to the server.
DAEMON_TO_SERVER: FrozenSet[str] = frozenset(
    {
        MSG_HELLO,
        MSG_STATUS_UPDATE,
        MSG_CALL_NOTIFICATION,
        MSG_PONG,
        MSG_HISTORY_INDEX,
        MSG_HISTORY_INDEX_DELTA,
        MSG_HISTORY_DATA,
        MSG_KEEPALIVE,
        MSG_DETAIL_DATA,
        MSG_ISSUE_RESULT,
        MSG_SPAWN_FAILED,
    }
)
#: Messages a server is allowed to send to a daemon.
SERVER_TO_DAEMON: FrozenSet[str] = frozenset(
    {
        MSG_WELCOME,
        MSG_SPAWN_FLOW,
        MSG_RESPOND_CALL,
        MSG_PING,
        MSG_HISTORY_REQUEST,
        MSG_HISTORY_INDEX_REQUEST,
        MSG_INTERJECT_FLOW,
        MSG_ISSUE_COMMAND,
        MSG_DETAIL_REQUEST,
        MSG_END_SESSION,
    }
)
#: Every known message type.
ALL_MESSAGE_TYPES: FrozenSet[str] = DAEMON_TO_SERVER | SERVER_TO_DAEMON


class ProtocolError(ValueError):
    """Raised when a frame cannot be parsed as a valid protocol message."""


@dataclass
class Message:
    """A single protocol frame.

    Attributes:
        type: One of the ``MSG_*`` constants.
        payload: Type-specific JSON-serializable object.
        seq: Per-connection sequence number assigned by the sender.
        timestamp: Unix epoch seconds at construction time.
    """

    type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Return the JSON-friendly dict form of this message."""
        return {
            "type": self.type,
            "seq": self.seq,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        """Serialize this message to a compact JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @classmethod
    def from_dict(cls, data: Any) -> "Message":
        """Build a :class:`Message` from a decoded JSON object.

        Raises :class:`ProtocolError` when *data* is not a well-formed frame.
        """
        if not isinstance(data, dict):
            raise ProtocolError(f"protocol frame must be an object, got {type(data).__name__}")
        msg_type = data.get("type")
        if not isinstance(msg_type, str) or not msg_type:
            raise ProtocolError("protocol frame is missing a string 'type'")
        if msg_type not in ALL_MESSAGE_TYPES:
            raise ProtocolError(f"unknown message type: {msg_type!r}")
        payload = data.get("payload", {})
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ProtocolError("protocol frame 'payload' must be an object")
        seq_raw = data.get("seq", 0)
        try:
            seq = int(seq_raw)
        except (TypeError, ValueError):
            seq = 0
        ts_raw = data.get("timestamp", time.time())
        try:
            timestamp = float(ts_raw)
        except (TypeError, ValueError):
            timestamp = time.time()
        return cls(type=msg_type, payload=payload, seq=seq, timestamp=timestamp)

    @classmethod
    def from_json(cls, raw: str) -> "Message":
        """Parse a JSON string into a :class:`Message`.

        Raises :class:`ProtocolError` on malformed JSON or an invalid frame.
        """
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"invalid JSON frame: {exc}") from exc
        return cls.from_dict(data)


def encode(msg_type: str, payload: Dict[str, Any], *, seq: int = 0) -> str:
    """Build and JSON-encode a message of *msg_type* in one call."""
    return Message(type=msg_type, payload=dict(payload), seq=seq).to_json()


def decode(raw: str) -> Message:
    """Parse a JSON wire string into a validated :class:`Message`."""
    return Message.from_json(raw)


# -- typed payload constructors -------------------------------------------
# Thin helpers so call sites do not hand-roll payload dicts; the server and
# daemon both build messages exclusively through these.


def make_hello(
    machine_id: str, hostname: str, se3_version: str, key: str = ""
) -> Message:
    """daemon → server: announce a daemon and identify its machine.

    *key* is the daemon credential the multi-tenant server resolves to an owner
    (``key → owner_id``) so it can bind the reporting machine to a trust domain.
    It is **optional on the wire**: when *key* is empty the ``key`` field is
    omitted entirely, so a daemon running purely locally (or against a legacy
    single-tenant server) produces the exact same payload as before — the field
    is additive and an older server simply ignores it. A server that requires a
    key treats a HELLO without one as unauthenticated and answers
    ``WELCOME(accepted=false)``.

    The key is a secret credential: it lives only in memory and on the wire here
    and MUST NOT be logged. Callers logging a HELLO must never echo this field.
    """
    payload: Dict[str, Any] = {
        "machine_id": machine_id,
        "hostname": hostname,
        "se3_version": se3_version,
        "protocol_version": PROTOCOL_VERSION,
    }
    if key:
        payload["key"] = key
    return Message(type=MSG_HELLO, payload=payload)


def make_welcome(server_version: str, accepted: bool = True, reason: str = "") -> Message:
    """server → daemon: acknowledge a HELLO."""
    return Message(
        type=MSG_WELCOME,
        payload={
            "server_version": server_version,
            "protocol_version": PROTOCOL_VERSION,
            "accepted": accepted,
            "reason": reason,
        },
    )


def make_status_update(snapshot: Dict[str, Any], *, seq: int = 0) -> Message:
    """daemon → server: report an aggregated machine-status snapshot."""
    return Message(type=MSG_STATUS_UPDATE, payload={"snapshot": snapshot}, seq=seq)


def make_call_notification(call: Dict[str, Any]) -> Message:
    """daemon → server: notify of a freshly-detected pending human call."""
    return Message(type=MSG_CALL_NOTIFICATION, payload={"call": call})


def make_spawn_flow(
    task_description: str,
    *,
    project_root: str = "",
    task_type: str = "feature",
    discover: bool = False,
    worktree: bool = False,
    resume_flow_id: str = "",
    from_issue_id: str = "",
) -> Message:
    """server → daemon: instruct a daemon to spawn a new ``se3 run`` flow.

    When *discover* is true the daemon's spawner appends ``--discover`` so the
    flow starts from the discovery step (see the spawner command assembly).

    When *worktree* is true the daemon's spawner appends ``--worktree`` so the
    flow runs in an isolated worktree and auto-merges back on success. The key
    is omitted from the wire when false, so a plain (non-isolated) fresh-spawn
    payload stays byte-for-byte backward compatible and ``PROTOCOL_VERSION`` is
    not bumped.

    When *resume_flow_id* is non-empty, the daemon resumes the named flow
    (``se3 run --resume --flow-id <id>``) instead of starting a fresh one.
    The ``task_description`` is ignored in this case — the flow's own
    persisted state supplies the task.

    When *from_issue_id* is non-empty, the daemon spawns the flow from an
    existing issue (``se3 run --from-issue <id>``); the issue's description
    becomes the task and the request's ``task_description`` is ignored. It may
    be combined with *discover* (the daemon then also appends ``--discover``).
    Like *resume_flow_id*, the field is omitted from the wire when empty, so a
    plain fresh-spawn payload stays byte-for-byte backward compatible and the
    ``PROTOCOL_VERSION`` is not bumped.
    """
    payload: Dict[str, Any] = {
        "task_description": task_description,
        "project_root": project_root,
        "task_type": task_type,
        "discover": bool(discover),
    }
    if worktree:
        payload["worktree"] = True
    if resume_flow_id:
        payload["resume_flow_id"] = resume_flow_id
    if from_issue_id:
        payload["from_issue_id"] = from_issue_id
    return Message(type=MSG_SPAWN_FLOW, payload=payload)


def make_spawn_failed(
    project_root: str,
    error: str,
    *,
    task_description: str = "",
    from_issue_id: str = "",
    resume_flow_id: str = "",
) -> Message:
    """daemon → server: report a failed spawn / resume / project-init.

    Sent when a server-dispatched :data:`MSG_SPAWN_FLOW` could not be carried
    out *after* the server already answered ``202 dispatched`` — e.g. the
    ``ensure_se3_project`` init failed, the fresh ``se3 run`` could not be
    launched, or a resume could not be started. *project_root* and *error*
    locate and explain the failure; the optional *task_description* /
    *from_issue_id* / *resume_flow_id* echo the originating request so the
    server / web UI can correlate the failure with the task the user just
    published instead of leaving it stuck on the "published" state.

    Empty optional fields are omitted from the wire so the payload stays
    compact; ``project_root`` and ``error`` are always present.
    """
    payload: Dict[str, Any] = {
        "project_root": project_root,
        "error": error,
    }
    if task_description:
        payload["task_description"] = task_description
    if from_issue_id:
        payload["from_issue_id"] = from_issue_id
    if resume_flow_id:
        payload["resume_flow_id"] = resume_flow_id
    return Message(type=MSG_SPAWN_FAILED, payload=payload)


def make_respond_call(
    call_id: str,
    response: Any,
    *,
    project_root: str = "",
) -> Message:
    """server → daemon: deliver a human response for a pending call."""
    return Message(
        type=MSG_RESPOND_CALL,
        payload={
            "call_id": call_id,
            "project_root": project_root,
            "response": response,
        },
    )


def make_interject_flow(
    flow_id: str,
    text: str,
    *,
    project_root: str = "",
) -> Message:
    """server → daemon: deliver a mid-flow user interjection for a running flow.

    *text* is the user-typed instruction to fold into the running flow (the
    same content a local operator would type at the Ctrl-C interjection
    prompt). The daemon writes *text* as an ``interjection``-kind call file
    under the flow's ``se3/calls/`` directory; the running ``se3 run`` process
    drains it at the next step boundary and folds it into ``user_interjections``.
    """
    return Message(
        type=MSG_INTERJECT_FLOW,
        payload={
            "flow_id": flow_id,
            "text": text,
            "project_root": project_root,
        },
    )


def make_end_session(
    flow_id: str,
    *,
    project_root: str = "",
    reason: str = "user terminated",
) -> Message:
    """server → daemon: end (terminate + archive) the session *flow_id*.

    The daemon locates *flow_id* among its supervised flows, then off-loads the
    actual work to an ``se3 end-session`` subprocess: it gracefully terminates
    the live ``se3 run`` process and, for a worktree session, archives it the
    way a normally-completed session is cleaned up (``se3/worktrees/.archive``
    + a promoted main-repo ``engine_<flow_id>.json`` + history sync + branch /
    worktree-metadata removal). The work is never done on the event loop.

    *project_root* is the main project root the daemon should pass through to
    the subprocess; when empty the daemon reverse-resolves it from its history
    index (mirroring the INTERJECT path). *reason* is free-form prose recorded
    for diagnostics.

    Empty optional fields are omitted from the wire so a payload carrying only a
    ``flow_id`` stays compact, and an older daemon that does not recognise the
    type simply ignores the frame — so no ``PROTOCOL_VERSION`` bump is needed.
    """
    payload: Dict[str, Any] = {"flow_id": flow_id}
    if project_root:
        payload["project_root"] = project_root
    if reason:
        payload["reason"] = reason
    return Message(type=MSG_END_SESSION, payload=payload)


def make_issue_command(
    operation: str,
    project_root: str,
    *,
    issue_id: str = "",
    description: Optional[str] = None,
    title: Optional[str] = None,
    priority: Optional[str] = None,
    type: Optional[str] = None,
    tags: Optional[List[str]] = None,
    reason: str = "",
    request_id: str = "",
) -> Message:
    """server → daemon: execute an issue write operation.

    *operation* is one of ``"create"``, ``"edit"``, ``"close"``, ``"reopen"``.
    ``project_root`` is required and must be an absolute path to a registered
    SE3 project.  The remaining fields are operation-specific:

    * ``create``: *description* is required; *title*, *priority*, *type*,
      *tags* are optional.
    * ``edit``: *issue_id* is required; *title*, *description*, *priority*,
      *type*, *tags* are optional.  ``None`` means "do not change";
      an empty string means "clear the field".
    * ``close``: *issue_id* is required; *reason* is optional.
    * ``reopen``: *issue_id* is required.

    When *request_id* is supplied the daemon will echo it back in its
    :data:`MSG_ISSUE_RESULT` reply so the server can correlate the response.
    """
    payload: Dict[str, Any] = {
        "operation": operation,
        "project_root": project_root,
    }
    if issue_id:
        payload["issue_id"] = issue_id
    if description is not None:
        payload["description"] = description
    if title is not None:
        payload["title"] = title
    if priority is not None:
        payload["priority"] = priority
    if type is not None:
        payload["type"] = type
    if tags is not None:
        payload["tags"] = list(tags)
    if reason:
        payload["reason"] = reason
    if request_id:
        payload["request_id"] = request_id
    return Message(type=MSG_ISSUE_COMMAND, payload=payload)


def make_issue_result(
    request_id: str,
    *,
    ok: bool = True,
    error: str = "",
    issue_id: str = "",
) -> Message:
    """daemon → server: acknowledge the result of an issue write command.

    *request_id* echoes the ``request_id`` from the originating
    :data:`MSG_ISSUE_COMMAND` so the server can correlate.  When *ok* is
    ``False`` the *error* string describes what went wrong.
    """
    payload: Dict[str, Any] = {
        "request_id": request_id,
        "ok": ok,
    }
    if error:
        payload["error"] = error
    if issue_id:
        payload["issue_id"] = issue_id
    return Message(type=MSG_ISSUE_RESULT, payload=payload)


def make_ping(*, seq: int = 0) -> Message:
    """server → daemon: heartbeat probe."""
    return Message(type=MSG_PING, payload={}, seq=seq)


def make_pong(*, seq: int = 0) -> Message:
    """daemon → server: heartbeat reply."""
    return Message(type=MSG_PONG, payload={}, seq=seq)


# -- history messages (protocol revision 2) -------------------------------
# These carry the per-machine `se3 history` records to the central server so
# the web UI can list and inspect historical sessions. The server is only an
# in-memory relay/cache — it does not persist history to disk.


def make_history_index(sessions: Any, *, seq: int = 0) -> Message:
    """daemon → server: report the index of known history sessions.

    *sessions* is a list of session-meta dicts (flow id, task description,
    status, timestamps, active flag, …) — one per ``se3 history`` entry the
    daemon can serve. Sent on connect and whenever the index changes.
    """
    return Message(
        type=MSG_HISTORY_INDEX,
        payload={"sessions": list(sessions)},
        seq=seq,
    )


def make_history_index_request(*, seq: int = 0) -> Message:
    """server → daemon: force a fresh rebuild + re-push of the history index.

    Carries no payload — it has no flow dimension and merely instructs the
    daemon to rebuild its index from disk and send a :data:`MSG_HISTORY_INDEX`
    immediately, even if the index has not changed since the last push (it
    bypasses the daemon's change-debounce via ``force_index``). The web
    ``GET /api/history`` broadcasts this to every connected daemon so the
    history list always reflects the latest sessions.
    """
    return Message(type=MSG_HISTORY_INDEX_REQUEST, payload={}, seq=seq)


def make_history_request(
    flow_id: str,
    *,
    project_root: str = "",
    cursor: Dict[str, Any] | None = None,
    seq: int = 0,
) -> Message:
    """server → daemon: pull the history records for *flow_id* on demand.

    *cursor* is an optional per-step file-cursor dict ``{step_id: position}``;
    when supplied the daemon may answer with an incremental ``append`` rather
    than a ``full`` snapshot. ``None`` requests a full snapshot.
    """
    return Message(
        type=MSG_HISTORY_REQUEST,
        payload={
            "flow_id": flow_id,
            "project_root": project_root,
            "cursor": dict(cursor) if cursor else {},
        },
        seq=seq,
    )


def make_history_data(
    flow_id: str,
    mode: str,
    records: Any,
    *,
    cursor: Dict[str, Any] | None = None,
    seq: int = 0,
) -> Message:
    """daemon → server: deliver history records for *flow_id*.

    *mode* is :data:`HISTORY_MODE_FULL` (a complete snapshot) or
    :data:`HISTORY_MODE_APPEND` (records newer than the requester's cursor).
    *records* is the list of history record dicts. *cursor* is the updated
    per-step file-cursor dict the recipient should send back on its next
    request to continue incrementally.

    Raises :class:`ProtocolError` when *mode* is not a recognized value.
    """
    if mode not in HISTORY_MODES:
        raise ProtocolError(
            f"history data mode must be one of {sorted(HISTORY_MODES)}, got {mode!r}"
        )
    return Message(
        type=MSG_HISTORY_DATA,
        payload={
            "flow_id": flow_id,
            "mode": mode,
            "records": list(records),
            "cursor": dict(cursor) if cursor else {},
        },
        seq=seq,
    )


# -- traffic-reduction messages (protocol revision 3) ---------------------
# These replace, in the steady state, the periodic *full* STATUS_UPDATE and
# HISTORY_INDEX frames with change-driven / incremental ones, so an idle daemon
# costs a keepalive rather than a ~573 KB snapshot every 5 s, and an active flow
# costs only the meta row that changed rather than the whole index. Only emitted
# to a peer that advertises protocol_version >= 3 (see supports_traffic_reduction).


def make_keepalive(signature: str = "", *, seq: int = 0) -> Message:
    """daemon → server: a minimal heartbeat sent when the status snapshot is
    unchanged.

    Emitted in place of a periodic :data:`MSG_STATUS_UPDATE` when the aggregated
    snapshot's content *signature* matches the last one pushed: nothing changed,
    so re-sending the (potentially large) snapshot is pure waste, but the server
    still needs a liveness signal to keep its offline-detection timer from
    tripping. The server treats a keepalive exactly like a STATUS_UPDATE for the
    purpose of the daemon's last-seen time and does **not** re-broadcast state to
    browsers. *signature* is the same content hash the daemon gates on, carried
    so the server can confirm both ends agree on "nothing changed".
    """
    return Message(type=MSG_KEEPALIVE, payload={"signature": signature}, seq=seq)


def make_history_index_delta(
    upserts: Any = (),
    removed: Any = (),
    *,
    seq: int = 0,
) -> Message:
    """daemon → server: an incremental history-index update.

    *upserts* is a list of SessionMeta dicts (each carrying a ``flow_id``) that
    were added or changed since the last index push; the server upserts them
    into its in-memory full index keyed by ``flow_id``. *removed* is a list of
    ``flow_id`` strings whose sessions disappeared and should be dropped. Sent
    instead of a whole :data:`MSG_HISTORY_INDEX` for the common case where only a
    few active flows' metas changed, so index traffic scales with the number of
    *changed* flows rather than the total flow count. The full index is still
    sent on connect / reconnect / HISTORY_INDEX_REQUEST as the baseline both
    sides reconcile against.
    """
    return Message(
        type=MSG_HISTORY_INDEX_DELTA,
        payload={
            "upserts": list(upserts),
            "removed": list(removed),
        },
        seq=seq,
    )


def make_detail_request(
    kind: str,
    target_id: str,
    *,
    project_root: str = "",
    request_id: str = "",
    seq: int = 0,
) -> Message:
    """server → daemon: fetch the full text of one issue or pending call.

    *kind* is :data:`DETAIL_KIND_ISSUE` or :data:`DETAIL_KIND_CALL`; *target_id*
    is the issue id or call id whose untruncated content is wanted (STATUS_UPDATE
    now carries only clipped summaries). *project_root* scopes the lookup to a
    specific SE3 project when the server knows it. *request_id* correlates the
    eventual :data:`MSG_DETAIL_DATA` reply back to the waiting REST request.

    Raises :class:`ProtocolError` when *kind* is not a recognised detail kind.
    """
    if kind not in DETAIL_KINDS:
        raise ProtocolError(
            f"detail kind must be one of {sorted(DETAIL_KINDS)}, got {kind!r}"
        )
    payload: Dict[str, Any] = {
        "kind": kind,
        "target_id": target_id,
    }
    if project_root:
        payload["project_root"] = project_root
    if request_id:
        payload["request_id"] = request_id
    return Message(type=MSG_DETAIL_REQUEST, payload=payload, seq=seq)


def make_detail_data(
    request_id: str,
    kind: str,
    *,
    detail: Optional[Dict[str, Any]] = None,
    ok: bool = True,
    error: str = "",
    seq: int = 0,
) -> Message:
    """daemon → server: deliver the full text for a :data:`MSG_DETAIL_REQUEST`.

    *request_id* echoes the request so the server can wake the correct waiter;
    *kind* echoes the requested :data:`DETAIL_KINDS` value. On success *detail*
    is the full-text record (e.g. the issue with its untruncated description, or
    the call with its full prompt). When *ok* is ``False`` *error* explains why
    the lookup failed (missing id, unreadable file, …) and *detail* is omitted.

    Raises :class:`ProtocolError` when *kind* is not a recognised detail kind.
    """
    if kind not in DETAIL_KINDS:
        raise ProtocolError(
            f"detail kind must be one of {sorted(DETAIL_KINDS)}, got {kind!r}"
        )
    payload: Dict[str, Any] = {
        "request_id": request_id,
        "kind": kind,
        "ok": ok,
    }
    if detail is not None:
        payload["detail"] = dict(detail)
    if error:
        payload["error"] = error
    return Message(type=MSG_DETAIL_DATA, payload=payload, seq=seq)
