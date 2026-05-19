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
  :data:`MSG_INTERJECT_FLOW`.

Backward compatibility
----------------------
Protocol version 2 added the history messages. A peer speaking an older
revision will never *send* them; if it ever *receives* one it does not
recognise, the frame is rejected as an unknown type — callers decoding
untrusted frames should therefore tolerate :class:`ProtocolError` rather
than crash, so new and old peers can interoperate.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet

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
MSG_INTERJECT_FLOW = "interject_flow"

# -- call-file kinds ------------------------------------------------------
# Every interaction that needs a human in the loop while a flow runs is
# unified onto one carrier: a JSON file under a project's ``se3/calls/``
# directory whose ``kind`` field tells the UI how to render it. These are the
# recognized values; the daemon aggregator falls back to :data:`CALL_KIND_CALL`
# for any legacy call file that carries no ``kind`` metadata.
CALL_KIND_CALL = "call"  # a pending MCP / human call (the original mechanism)
CALL_KIND_INTERJECTION = "interjection"  # a mid-flow Ctrl-C interjection request
CALL_KIND_RETRY_DECISION = "retry_decision"  # a retry / skip / abort failure decision
CALL_KIND_CLI_CONFIRM = "cli_confirm"  # a CLI subprocess confirmation prompt

#: Every recognized call-file ``kind``.
CALL_KINDS: FrozenSet[str] = frozenset(
    {
        CALL_KIND_CALL,
        CALL_KIND_INTERJECTION,
        CALL_KIND_RETRY_DECISION,
        CALL_KIND_CLI_CONFIRM,
    }
)

#: Valid values for the ``mode`` field of a :data:`MSG_HISTORY_DATA` payload.
HISTORY_MODE_FULL = "full"
HISTORY_MODE_APPEND = "append"
HISTORY_MODES: FrozenSet[str] = frozenset({HISTORY_MODE_FULL, HISTORY_MODE_APPEND})

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
        MSG_INTERJECT_FLOW,
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


def make_hello(machine_id: str, hostname: str, se3_version: str) -> Message:
    """daemon → server: announce a daemon and identify its machine."""
    return Message(
        type=MSG_HELLO,
        payload={
            "machine_id": machine_id,
            "hostname": hostname,
            "se3_version": se3_version,
            "protocol_version": PROTOCOL_VERSION,
        },
    )


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
) -> Message:
    """server → daemon: instruct a daemon to spawn a new ``se3 run`` flow.

    When *discover* is true the daemon's spawner appends ``--discover`` so the
    flow starts from the discovery step (see the spawner command assembly).
    """
    return Message(
        type=MSG_SPAWN_FLOW,
        payload={
            "task_description": task_description,
            "project_root": project_root,
            "task_type": task_type,
            "discover": bool(discover),
        },
    )


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
    """server → daemon: deliver a mid-flow interjection for a running flow.

    *text* is the user-typed instruction to fold into the running flow (the
    same content a local operator would type at the Ctrl-C interjection
    prompt). The daemon resolves *flow_id* to its project root and writes an
    interjection request file the running ``se3 run`` consumes at a step
    boundary.
    """
    return Message(
        type=MSG_INTERJECT_FLOW,
        payload={
            "flow_id": flow_id,
            "text": text,
            "project_root": project_root,
        },
    )


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
