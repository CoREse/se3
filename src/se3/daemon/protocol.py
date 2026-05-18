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
  :data:`MSG_CALL_NOTIFICATION`, :data:`MSG_PONG`.
* server → daemon: :data:`MSG_WELCOME`, :data:`MSG_SPAWN_FLOW`,
  :data:`MSG_RESPOND_CALL`, :data:`MSG_PING`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet

# Protocol revision. Bumped only on a breaking wire change; both daemon and
# server advertise it in HELLO / WELCOME so a mismatch can be surfaced.
PROTOCOL_VERSION = "1"

# -- message types: daemon -> server --------------------------------------
MSG_HELLO = "hello"
MSG_STATUS_UPDATE = "status_update"
MSG_CALL_NOTIFICATION = "call_notification"
MSG_PONG = "pong"

# -- message types: server -> daemon --------------------------------------
MSG_WELCOME = "welcome"
MSG_SPAWN_FLOW = "spawn_flow"
MSG_RESPOND_CALL = "respond_call"
MSG_PING = "ping"

#: Messages a daemon is allowed to send to the server.
DAEMON_TO_SERVER: FrozenSet[str] = frozenset(
    {MSG_HELLO, MSG_STATUS_UPDATE, MSG_CALL_NOTIFICATION, MSG_PONG}
)
#: Messages a server is allowed to send to a daemon.
SERVER_TO_DAEMON: FrozenSet[str] = frozenset(
    {MSG_WELCOME, MSG_SPAWN_FLOW, MSG_RESPOND_CALL, MSG_PING}
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
) -> Message:
    """server → daemon: instruct a daemon to spawn a new ``se3 run`` flow."""
    return Message(
        type=MSG_SPAWN_FLOW,
        payload={
            "task_description": task_description,
            "project_root": project_root,
            "task_type": task_type,
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


def make_ping(*, seq: int = 0) -> Message:
    """server → daemon: heartbeat probe."""
    return Message(type=MSG_PING, payload={}, seq=seq)


def make_pong(*, seq: int = 0) -> Message:
    """daemon → server: heartbeat reply."""
    return Message(type=MSG_PONG, payload={}, seq=seq)
