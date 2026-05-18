"""Pluggable event-stream sinks.

A *sink* is the tail of the unified event stream defined in
``event_stream.py``: it consumes :class:`~se3.engine.event_stream.Event`
objects and turns them into some concrete output. The choice between CLI and
daemon operation degrades to a single sink selection at the outermost layer.

Two concrete sinks ship here:

* :class:`CliSink` — hangs the existing Rich rendering chain
  (``display.py`` / ``step_renderers.py``). It produces the same visual output
  ``se3 run`` produces today; it is a thin dispatch over those renderers and
  does NOT reimplement any rendering logic.
* :class:`JsonSink` — serializes each event to a single line of JSON (NDJSON
  style), for daemon consumption.
"""

from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from typing import IO, Optional

from .event_stream import Event, EventType


class Sink(ABC):
    """Abstract base class for an event-stream consumer.

    A sink subscribes to an
    :class:`~se3.engine.event_stream.EventEmitter` and receives every emitted
    event through :meth:`consume`.
    """

    @abstractmethod
    def consume(self, event: Event) -> None:
        """Consume a single event. Concrete sinks define the side effect."""
        raise NotImplementedError


class CliSink(Sink):
    """Rich-rendering sink — the CLI-mode tail of the event stream.

    ``CliSink`` delegates entirely to the pre-existing rendering functions in
    ``display.py`` and ``step_renderers.py``; it adds no rendering logic of its
    own. This is what keeps CLI output byte-for-byte identical to today's
    ``se3 run``: the same renderers, called with the same step objects.

    Step-scoped completion/failure events whose ``data`` carries a ``"step"``
    object are routed to ``step_renderers.render_step_output(step)`` — the same
    single entry point the current CLI uses. Flow-level lifecycle events render
    a concise status line; raw ``STEP_OUTPUT`` events are intentionally a no-op
    (the per-step renderer already presents the full output on completion).
    """

    def __init__(self, console: Optional[object] = None) -> None:
        """Create a CLI sink.

        Args:
            console: Optional Rich ``Console`` override. When supplied it is
                installed as the global display console so all delegated
                renderers target it; when omitted the shared console is used.
        """
        if console is not None:
            from . import display

            display.set_console(console)

    def consume(self, event: Event) -> None:
        et = event.type

        if et in (EventType.STEP_COMPLETED, EventType.STEP_FAILED):
            self._render_step(event)
        elif et == EventType.FLOW_STARTED:
            self._render_status(event, "Flow started", "blue")
        elif et == EventType.FLOW_COMPLETED:
            self._render_status(event, "Flow completed", "green")
        elif et == EventType.FLOW_FAILED:
            self._render_status(event, "Flow failed", "red")
        elif et == EventType.FLOW_PAUSED:
            self._render_status(event, "Flow paused", "yellow")
        elif et in (EventType.INTERJECTION_NEEDED, EventType.CALL_NEEDED):
            self._render_status(event, et.value.replace("_", " "), "yellow")
        # STEP_STARTED / STEP_OUTPUT: no-op — the per-step renderer presents
        # the complete output once the step finishes, matching current CLI.

    # -- internals ---------------------------------------------------------

    def _render_step(self, event: Event) -> None:
        """Route a step event to the existing step-output renderer."""
        step = event.data.get("step")
        if step is None:
            return
        from .step_renderers import render_step_output

        render_step_output(step)

    def _render_status(self, event: Event, label: str, color: str) -> None:
        """Render a flow-level lifecycle line via the shared display block."""
        from .display import get_console, render_block_footer, render_block_header

        message = event.data.get("message", "")
        render_block_header(label, color)
        if message:
            get_console().print(message)
            get_console().print("")
        render_block_footer(color)


class JsonSink(Sink):
    """Structured NDJSON sink — the daemon-mode tail of the event stream.

    Each consumed event is serialized via
    :meth:`~se3.engine.event_stream.Event.to_dict` and written as one line of
    JSON terminated by a newline (NDJSON). Two modes are supported:

    * ``compact`` (default) — one event per physical line; the format the
      daemon consumes.
    * ``pretty`` — ``indent=2`` for human debugging; still newline-terminated.

    ``default=str`` is passed to ``json.dumps`` so a non-serializable payload
    value (e.g. a ``Step`` object) degrades to its ``str()`` form rather than
    raising.
    """

    def __init__(self, file: Optional[IO[str]] = None, pretty: bool = False) -> None:
        """Create a JSON sink.

        Args:
            file: Destination text stream. Defaults to ``sys.stdout``.
            pretty: When True, emit indented JSON for debugging; when False
                (default), emit compact single-line NDJSON.
        """
        self.file: IO[str] = file if file is not None else sys.stdout
        self.pretty = pretty

    def consume(self, event: Event) -> None:
        payload = event.to_dict()
        if self.pretty:
            line = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        else:
            line = json.dumps(
                payload, separators=(",", ":"), ensure_ascii=False, default=str
            )
        self.file.write(line + "\n")
        try:
            self.file.flush()
        except (ValueError, OSError):  # pragma: no cover - closed/unflushable stream
            pass
