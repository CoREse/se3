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
  :data:`MSG_CALL_NOTIFICATION`, :data:`MSG_PONG`,
  :data:`MSG_HISTORY_INDEX`, :data:`MSG_HISTORY_DATA`.
* server → daemon: :data:`MSG_WELCOME`, :data:`MSG_SPAWN_FLOW`,
  :data:`MSG_RESPOND_CALL`, :data:`MSG_PING`, :data:`MSG_HISTORY_REQUEST`,
  :data:`MSG_HISTORY_INDEX_REQUEST`, :data:`MSG_INTERJECT_FLOW`.

Backward compatibility
----------------------
Protocol version 2 added the history messages. A peer speaking an older
revision will never *send* them; if it ever *receives* one it does not
recognise, the frame is rejected as an unknown type — callers decoding
untrusted frames should therefore tolerate :class:`ProtocolError` rather
than crash, so new and old peers can interoperate.

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
PROTOCOL_VERSION = "2"

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

#: server → daemon: instruct the daemon to execute an issue write operation
#: (create / edit / close / reopen). The daemon resolves the project root,
#: validates the operation and delegates to :class:`IssueManager`.
MSG_ISSUE_COMMAND = "issue_command"

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
#: Every recognised interaction-call kind.
CALL_KINDS: FrozenSet[str] = frozenset(
    {
        CALL_KIND_CALL,
        CALL_KIND_INTERJECTION,
        CALL_KIND_RETRY_DECISION,
        CALL_KIND_CLI_CONFIRM,
        CALL_KIND_DISCOVERY_CONFIRM,
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
        MSG_HISTORY_DATA,
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
    resume_flow_id: str = "",
) -> Message:
    """server → daemon: instruct a daemon to spawn a new ``se3 run`` flow.

    When *discover* is true the daemon's spawner appends ``--discover`` so the
    flow starts from the discovery step (see the spawner command assembly).

    When *resume_flow_id* is non-empty, the daemon resumes the named flow
    (``se3 run --resume --flow-id <id>``) instead of starting a fresh one.
    The ``task_description`` is ignored in this case — the flow's own
    persisted state supplies the task.
    """
    payload: Dict[str, Any] = {
        "task_description": task_description,
        "project_root": project_root,
        "task_type": task_type,
        "discover": bool(discover),
    }
    if resume_flow_id:
        payload["resume_flow_id"] = resume_flow_id
    return Message(type=MSG_SPAWN_FLOW, payload=payload)


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


def make_issue_command(
    operation: str,
    project_root: str,
    *,
    issue_id: str = "",
    description: str = "",
    title: str = "",
    priority: str = "",
    type: str = "",
    tags: Optional[List[str]] = None,
    reason: str = "",
) -> Message:
    """server → daemon: execute an issue write operation.

    *operation* is one of ``"create"``, ``"edit"``, ``"close"``, ``"reopen"``.
    ``project_root`` is required and must be an absolute path to a registered
    SE3 project.  The remaining fields are operation-specific:

    * ``create``: *description* is required; *title*, *priority*, *type*,
      *tags* are optional.
    * ``edit``: *issue_id* is required; *title*, *description*, *priority*,
      *type*, *tags* are optional (non-empty values overwrite the field).
    * ``close``: *issue_id* is required; *reason* is optional.
    * ``reopen``: *issue_id* is required.
    """
    payload: Dict[str, Any] = {
        "operation": operation,
        "project_root": project_root,
    }
    if issue_id:
        payload["issue_id"] = issue_id
    if description:
        payload["description"] = description
    if title:
        payload["title"] = title
    if priority:
        payload["priority"] = priority
    if type:
        payload["type"] = type
    if tags is not None:
        payload["tags"] = list(tags)
    if reason:
        payload["reason"] = reason
    return Message(type=MSG_ISSUE_COMMAND, payload=payload)


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
