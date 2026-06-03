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
from pathlib import Path
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
    ``step_renderers.py``; it adds no rendering logic of its own. This is what
    keeps CLI output byte-for-byte identical to today's ``se3 run``: the same
    renderers, called with the same step objects.

    Step-scoped completion/failure events whose ``data`` carries a ``"step"``
    object are routed to ``step_renderers.render_step_output(step)`` — the same
    single entry point the current CLI uses.

    Flow-level lifecycle events (``FLOW_STARTED`` / ``FLOW_COMPLETED`` /
    ``FLOW_FAILED`` / ``FLOW_PAUSED`` / ``INTERJECTION_NEEDED`` /
    ``CALL_NEEDED``) are intentionally a **no-op** here: in CLI mode the
    ``se3 run`` orchestrator already renders the human-facing "New Flow"
    panel, the per-step ``✓ completed`` line, and the closing
    ``display_success`` / ``display_error`` summary directly. Having the sink
    also render these would double the output and regress the CLI. The sink's
    sole visible responsibility is step-output rendering; flow-level events
    exist on the stream purely for the structured (``JsonSink``) consumer.

    Raw ``STEP_OUTPUT`` and ``STEP_STARTED`` events are likewise a no-op — the
    per-step renderer already presents the full output once the step finishes.

    Finally, ``STEP_COMPLETED`` / ``STEP_FAILED`` events for the interactive
    CONFIRM and DISCOVERY steps and for PLAN are a **no-op** here: their CLI
    output is owned by the ``se3 run`` orchestrator's interactive/special paths
    (the discovery message panel, the confirm approval prompt, …), so routing
    them through ``render_step_output`` too would double-render the CLI. These
    steps now *do* emit terminal events (so HistorySink can persist them for
    the web report cards and JsonSink can forward them to the daemon); CliSink
    is the layer that keeps the CLI output unchanged by skipping them.
    """

    #: Step types whose terminal events CliSink must NOT render — their CLI
    #: output is presented by the orchestrator's interactive/special paths, so
    #: rendering them here would duplicate the CLI output. The values match
    #: ``StepType.CONFIRM/DISCOVERY/PLAN`` ``.value`` strings (compared as
    #: strings to avoid importing the heavier models module into this sink).
    _CLI_SKIP_STEP_TYPES = frozenset({"confirm", "discovery", "plan"})

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
        if event.type in (EventType.STEP_COMPLETED, EventType.STEP_FAILED):
            self._render_step(event)
        # All other event types — flow-level lifecycle, STEP_STARTED,
        # STEP_OUTPUT — are deliberately a no-op (see the class docstring):
        # the CLI orchestrator owns that rendering directly.

    # -- internals ---------------------------------------------------------

    def _render_step(self, event: Event) -> None:
        """Route a step event to the existing step-output renderer.

        Interactive steps (CONFIRM/DISCOVERY) and PLAN have their full report
        skipped: that output is presented by the orchestrator's interactive/
        special paths, so rendering it here too would double the CLI output.
        Their events still reach HistorySink (web report cards) and JsonSink
        (daemon NDJSON).

        However, the orchestrator's interactive/special paths never render the
        per-step token-usage block, so for these skipped step types CliSink
        still renders just the usage block directly. This keeps per-step usage
        symmetric on the CLI: token-heavy steps like ``plan`` and ``discovery``
        show their consumption exactly as ``analyze`` / ``test`` / etc. do
        (and the WebUI report cards already surface it). The block self-guards
        on empty ``token_usage``, so steps that made no LLM call print nothing.
        """
        step = event.data.get("step")
        if step is None:
            return
        step_type = event.step_type
        if step_type is None:
            st = getattr(step, "step_type", None)
            step_type = getattr(st, "value", st)
        if step_type in self._CLI_SKIP_STEP_TYPES:
            from .step_renderers import render_step_usage

            render_step_usage(step)
            return
        from .step_renderers import render_step_output

        render_step_output(step)


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


class HistorySink(Sink):
    """Persist step-lifecycle events into the per-step chat history jsonl.

    ``STEP_COMPLETED`` and ``STEP_FAILED`` events carry the step's full
    structured output (the same data the CLI's ``step_renderers`` Panel
    renders). Writing them into ``se3/history/<flow_id>/<step_id>.jsonl``
    makes them flow naturally to the web console: the daemon's
    ``DaemonHistoryReader`` already streams every line in those files to the
    server via the ``history_data`` channel, and the frontend's
    ``normalizeRecord`` is already wired to recognise ``type ==
    "step_completed" / "step_failed"`` records and render them as default-
    expanded report cards.

    All other event types are a no-op here. The sink is safe to subscribe
    alongside :class:`CliSink` and :class:`JsonSink`; failures are swallowed
    so a flaky filesystem cannot break the running flow.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root)

    def consume(self, event: Event) -> None:
        if event.type not in (EventType.STEP_COMPLETED, EventType.STEP_FAILED):
            return
        step = event.data.get("step")
        if step is None:
            return
        step_dict = step.to_dict() if hasattr(step, "to_dict") else step
        if not isinstance(step_dict, dict):
            return
        # Lazy import: keeps the sink module free of heavier engine deps.
        from .chat_history import record_step_event

        record_step_event(
            project_root=self.project_root,
            flow_id=event.flow_id or "",
            step_id=event.step_id or (step_dict.get("step_id") or ""),
            step_type=event.step_type or (step_dict.get("step_type") or ""),
            event_type=event.type.value,
            step_dict=step_dict,
            timestamp=event.timestamp,
        )
