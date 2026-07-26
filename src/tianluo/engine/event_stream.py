"""Unified structured event stream for the SE3 flow engine.

This module defines a single, caller-agnostic event stream that ``luo run``
emits over its lifetime. The stream is the convergence point that previously
scattered structured artifacts (``state/engine.json``, ``state/summary-*.json``,
NDJSON chat history) now feed into a single in-memory pub/sub channel.

Rendering degrades to a pluggable *sink* at the tail of this stream:

* CLI mode hangs the existing Rich rendering sink (``CliSink``).
* daemon mode hangs a structured forwarding sink (``JsonSink``).

The emitter is a process-local, in-memory pub/sub object — it introduces no
IPC and no resident process model. ``luo run`` stays a one-shot foreground
command; the event stream lives only for the duration of that process.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .sink import Sink


class EventType(str, Enum):
    """The complete set of flow-lifecycle event types.

    Inherits from ``str`` so values are directly JSON-serializable and compare
    equal to their plain string form.
    """

    FLOW_STARTED = "flow_started"
    STEP_STARTED = "step_started"
    STEP_OUTPUT = "step_output"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    FLOW_PAUSED = "flow_paused"
    FLOW_COMPLETED = "flow_completed"
    FLOW_FAILED = "flow_failed"
    INTERJECTION_NEEDED = "interjection_needed"
    CALL_NEEDED = "call_needed"


@dataclass
class Event:
    """A single structured event in the flow event stream.

    Attributes:
        type: The :class:`EventType` of this event.
        timestamp: Unix epoch seconds when the event was created.
        flow_id: The owning flow's id, when known.
        step_id: The step's id, when the event is step-scoped.
        step_type: The step's type (string form), when the event is step-scoped.
        data: Arbitrary structured payload. For events consumed by ``CliSink``
            this MAY carry non-JSON-serializable objects (e.g. a ``Step``
            instance under the ``"step"`` key); ``JsonSink`` falls back to
            ``str`` for any such values when serializing.
    """

    type: EventType
    timestamp: float = field(default_factory=time.time)
    flow_id: Optional[str] = None
    step_id: Optional[str] = None
    step_type: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain-dict representation suitable for JSON serialization.

        The ``type`` enum is reduced to its string ``.value``. ``data`` is
        passed through verbatim — callers serializing the result SHOULD use
        ``json.dumps(..., default=str)`` as a defensive fallback for any
        non-serializable payload values.
        """
        return {
            "type": self.type.value,
            "timestamp": self.timestamp,
            "flow_id": self.flow_id,
            "step_id": self.step_id,
            "step_type": self.step_type,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Event":
        """Reconstruct an :class:`Event` from a :meth:`to_dict` payload."""
        return cls(
            type=EventType(payload["type"]),
            timestamp=payload.get("timestamp", time.time()),
            flow_id=payload.get("flow_id"),
            step_id=payload.get("step_id"),
            step_type=payload.get("step_type"),
            data=payload.get("data") or {},
        )


def new_event(
    event_type: EventType,
    *,
    flow_id: Optional[str] = None,
    step_id: Optional[str] = None,
    step_type: Optional[str] = None,
    timestamp: Optional[float] = None,
    **data: Any,
) -> Event:
    """Construct an :class:`Event`, the convenience factory used by ``run_flow``.

    Keyword payload arguments are collected into the event's ``data`` dict, so
    ``new_event(EventType.STEP_OUTPUT, flow_id=fid, step=step)`` is equivalent
    to building the dataclass with ``data={"step": step}``.
    """
    return Event(
        type=event_type,
        timestamp=time.time() if timestamp is None else timestamp,
        flow_id=flow_id,
        step_id=step_id,
        step_type=step_type,
        data=dict(data),
    )


class EventEmitter:
    """In-memory pub/sub hub: fans every emitted :class:`Event` out to sinks.

    The emitter holds an ordered list of subscribed sinks. ``emit()`` walks the
    list in subscription order and calls ``consume(event)`` on each. A failing
    sink does not abort delivery to the remaining sinks.
    """

    def __init__(self) -> None:
        self._sinks: List["Sink"] = []

    @property
    def sinks(self) -> List["Sink"]:
        """A copy of the currently subscribed sinks, in subscription order."""
        return list(self._sinks)

    def subscribe(self, sink: "Sink") -> None:
        """Register *sink* to receive subsequently emitted events.

        Subscribing the same sink instance twice is a no-op.
        """
        if sink not in self._sinks:
            self._sinks.append(sink)

    def unsubscribe(self, sink: "Sink") -> None:
        """Remove *sink* from the subscriber list. Unknown sinks are ignored."""
        try:
            self._sinks.remove(sink)
        except ValueError:
            pass

    def emit(self, event: Event) -> None:
        """Deliver *event* to every subscribed sink in subscription order.

        An exception raised by one sink is swallowed so it cannot prevent the
        remaining sinks from receiving the event; the event stream is best
        effort and MUST NOT let a rendering fault break the flow.
        """
        for sink in list(self._sinks):
            try:
                sink.consume(event)
            except Exception:  # pragma: no cover - defensive isolation
                pass

    @contextmanager
    def scope(self) -> Iterator["EventEmitter"]:
        """Scoped emitter: restores the subscriber list on exit.

        Sinks subscribed inside the ``with`` block are automatically dropped
        when the block exits, and any sinks unsubscribed inside the block are
        restored — leaving the emitter exactly as it was on entry::

            with emitter.scope() as em:
                em.subscribe(JsonSink())
                ...  # JsonSink active here
            # JsonSink automatically removed
        """
        snapshot = list(self._sinks)
        try:
            yield self
        finally:
            self._sinks = snapshot
