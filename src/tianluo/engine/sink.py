"""Pluggable event-stream sinks.

A *sink* is the tail of the unified event stream defined in
``event_stream.py``: it consumes :class:`~tianluo.engine.event_stream.Event`
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
    :class:`~tianluo.engine.event_stream.EventEmitter` and receives every emitted
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

    ``STEP_OUTPUT`` events (emitted for non-terminal steps that consumed tokens
    but haven't reached COMPLETED / PARTIAL / FAILED) are routed to
    ``step_renderers.render_step_usage(step)`` — the same usage-only renderer
    that the terminal path calls as part of ``render_step_output``. This ensures
    non-terminal steps (self_check REVISION_NEEDED, discovery PAUSED, etc.)
    show their token usage on the CLI even though no terminal event is emitted
    for them. In the fix loop, a REVISION_NEEDED self_check is abandoned and
    never reaches a terminal event, so this intermediate render is the sole
    CLI surface for its usage.

    Flow-level lifecycle events (``FLOW_STARTED`` / ``FLOW_COMPLETED`` /
    ``FLOW_FAILED`` / ``FLOW_PAUSED`` / ``INTERJECTION_NEEDED`` /
    ``CALL_NEEDED``) are intentionally a **no-op** here: in CLI mode the
    ``se3 run`` orchestrator already renders the human-facing "New Flow"
    panel, the per-step ``✓ completed`` line, and the closing
    ``display_success`` / ``display_error`` summary directly. Having the sink
    also render these would double the output and regress the CLI. The sink's
    sole visible responsibility is step-output rendering; flow-level events
    exist on the stream purely for the structured (``JsonSink``) consumer.

    ``STEP_STARTED`` events are a no-op — the per-step renderer presents
    the full output once the step finishes.

    ``STEP_OUTPUT`` events are emitted for non-terminal steps (PAUSED /
    REVISION_NEEDED / RETRYING) that consumed tokens but haven't reached
    COMPLETED / PARTIAL / FAILED. They carry the step data so
    ``CliSink`` can render the usage block via ``render_step_usage(step)``.
    This is the sole CLI surface for usage of steps that are abandoned in
    the fix loop (e.g. self_check returning REVISION_NEEDED that will
    never be re-run). Steps that later reach a terminal status also
    receive a ``STEP_COMPLETED`` / ``STEP_FAILED`` event, so their final
    usage block appears twice — but that is acceptable because the
    intermediate usage is a live update, not a duplicate of the final one.

    Finally, ``STEP_COMPLETED`` / ``STEP_FAILED`` events for the interactive
    CONFIRM and DISCOVERY steps and for PLAN are handled with per-type rules:
    their full report is skipped (owned by the orchestrator's interactive/
    special paths), but token usage is surfaced where appropriate:

    * ``discovery`` — renders a cumulative usage line (``format_usage_line``)
      from ``step.outputs['token_usage']`` when non-empty. The per-round
      inline footer (i18n-rendered, e.g. "this round … · total …") is rendered
      by the discovery handler during each round, but the terminal cumulative
      showing the
      whole-discovery total (across all rounds including the programmatic
      confirmation round that issues no LLM call) is rendered here.
    * ``confirm`` — renders a compact dim single-line footer (NOT the big
      ``Step Token Usage`` block) from ``step.outputs['token_usage']``.
    * ``plan`` — keeps the full ``render_step_usage`` block unchanged.
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
        elif event.type == EventType.STEP_OUTPUT:
            self._render_step_usage(event)
        # All other event types — flow-level lifecycle, STEP_STARTED —
        # are deliberately a no-op (see the class docstring): the CLI
        # orchestrator owns that rendering directly.

    # -- internals ---------------------------------------------------------

    def _render_step(self, event: Event) -> None:
        """Route a step event to the existing step-output renderer.

        Interactive steps (CONFIRM/DISCOVERY) and PLAN have their full report
        skipped: that output is presented by the orchestrator's interactive/
        special paths, so rendering it here too would double the CLI output.
        Their events still reach HistorySink (web report cards) and JsonSink
        (daemon NDJSON).

        The orchestrator's interactive/special paths never render token usage,
        so CliSink still surfaces per-step usage for the skipped types — but
        with a per-type rule, because the interactive multi-round steps want a
        compact inline footer rather than the big reverse-color block:

        * ``discovery`` — render a cumulative usage line via
          ``format_usage_line`` from ``step.outputs['token_usage']`` when
          non-empty. The per-round inline footer is rendered by the discovery
          handler during each round; this terminal line shows the
          whole-discovery cumulative (including the confirmation round that
          issues no LLM call). Empty / absent usage renders nothing.
        * ``confirm`` — render a compact dim single-line footer (NOT the big
          ``Step Token Usage`` block) from ``step.outputs['token_usage']``. The
          confirm LLM review runs once per confirm step, so this step's total
          is both the round and the cumulative figure; a human-mode confirm
          makes no LLM call, leaving ``token_usage`` empty so nothing renders.
        * ``plan`` — keep the full ``render_step_usage`` block unchanged.
        """
        step = event.data.get("step")
        if step is None:
            return
        step_type = event.step_type
        if step_type is None:
            st = getattr(step, "step_type", None)
            step_type = getattr(st, "value", st)
        if step_type in self._CLI_SKIP_STEP_TYPES:
            if step_type == "discovery":
                self._render_discovery_cumulative_usage(step)
                return
            if step_type == "confirm":
                self._render_confirm_usage_footer(step)
                return
            # plan keeps the established big per-step usage block.
            from .step_renderers import render_step_usage

            render_step_usage(step)
            return
        from .step_renderers import render_step_output

        render_step_output(step)

    def _render_step_usage(self, event: Event) -> None:
        """Render per-step token usage for a STEP_OUTPUT event.

        STEP_OUTPUT events are emitted by ``run.py`` for non-terminal steps
        (PAUSED / REVISION_NEEDED / RETRYING) that consumed tokens. Their
        purpose is to surface the step's ``token_usage`` before the flow
        transitions away — in the fix loop a REVISION_NEEDED self_check is
        abandoned and never reaches a terminal event, so its usage would be
        invisible without this intermediate render.

        The same per-type rules that ``_render_step`` applies to terminal
        events are applied here: discovery and confirm get their compact
        renderers rather than the big ``Step Token Usage`` block, so a
        non-terminal STEP_OUTPUT for discovery or confirm does not produce
        a second big usage block.
        """
        step = event.data.get("step")
        if step is None:
            return
        # Resolve step_type from the event or the step object.
        step_type = event.step_type
        if step_type is None:
            st = getattr(step, "step_type", None)
            step_type = getattr(st, "value", st)
        # Apply the same per-type rules as _render_step for terminal events.
        if step_type == "discovery":
            self._render_discovery_cumulative_usage(step)
            return
        if step_type == "confirm":
            self._render_confirm_usage_footer(step)
            return
        from .step_renderers import render_step_usage

        render_step_usage(step)

    @staticmethod
    def _render_confirm_usage_footer(step: object) -> None:
        """Render confirm's per-round usage as a compact dim single-line footer.

        Reuses the shared ``format_round_usage_footer`` so the wording / number
        format stays identical to discovery's inline footer and the rest of the
        project. Because the confirm reviewer calls the LLM at most once per
        confirm step, the round increment equals the cumulative total, so the
        same ``UsageTotals`` is passed for both. Nothing is rendered when the
        step made no LLM call (empty / absent ``token_usage`` — e.g. the human
        reviewer path).
        """
        usage = (getattr(step, "outputs", None) or {}).get("token_usage")
        if not usage:
            return
        from .token_usage import UsageTotals, format_round_usage_footer

        totals = UsageTotals.from_dict(usage)
        if totals.is_empty():
            return
        from rich.text import Text

        from .display import get_console

        footer = format_round_usage_footer(totals, totals)
        get_console().print(Text(footer, style="dim"))

    @staticmethod
    def _render_discovery_cumulative_usage(step: object) -> None:
        """Render the whole-discovery cumulative usage as a dim single-line.

        When the discovery step completes (after multi-round dialogue and the
        programmatic confirmation gate), ``run_step``'s ``finally`` block
        writes the cumulative ``token_usage`` into ``step.outputs``. This
        method reads that value and renders it via ``format_usage_line``
        (covering input/output/cache(r/w)/cost fields). Nothing is rendered
        when the discovery had no LLM calls at all (empty / absent usage).
        """
        usage = (getattr(step, "outputs", None) or {}).get("token_usage")
        if not usage:
            return
        from ..i18n import t
        from .token_usage import UsageTotals, format_usage_line

        totals = UsageTotals.from_dict(usage)
        if totals.is_empty():
            return
        from rich.text import Text

        from .display import get_console

        line = t("engine.usage.discovery_cumulative", usage=format_usage_line(totals))
        get_console().print(Text(line, style="dim"))


class JsonSink(Sink):
    """Structured NDJSON sink — the daemon-mode tail of the event stream.

    Each consumed event is serialized via
    :meth:`~tianluo.engine.event_stream.Event.to_dict` and written as one line of
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

    ``STEP_STARTED`` events are persisted as a lightweight
    ``{type: 'step_started', status: 'running', ...}`` anchor line the moment a
    step enters the RUNNING state, so the web console can show the step's
    region (with a "进行中" status) immediately — including the non-LLM
    TEST / COMMIT / SPEC_GATE steps that emit no conversation records and would
    otherwise stay blank until their final ``step_completed`` lands. The write
    is kept idempotent (guarded by ``has_step_started_event`` /
    ``has_step_terminal_event``) so a step re-entered on resume, or a
    re-emitted STEP_STARTED, never appends a second started record for the same
    step_id.

    ``STEP_COMPLETED`` and ``STEP_FAILED`` events carry the step's full
    structured output (the same data the CLI's ``step_renderers`` Panel
    renders). Writing them into ``se3/history/<flow_id>/<step_id>.jsonl``
    makes them flow naturally to the web console: the daemon's
    ``DaemonHistoryReader`` already streams every line in those files to the
    server via the ``history_data`` channel, and the frontend's
    ``normalizeRecord`` is already wired to recognise ``type ==
    "step_completed" / "step_failed"`` records and render them as default-
    expanded report cards.

    ``STEP_OUTPUT`` events are emitted by ``run.py`` for non-terminal steps
    (PAUSED / REVISION_NEEDED / RETRYING) that consumed tokens but have not
    reached a terminal status. Persisting them ensures the web console can
    include their ``token_usage`` in the session-total badge and render a
    per-step usage footnote even for steps that are abandoned in the fix loop
    (e.g. self_check returning REVISION_NEEDED that will never be re-run).
    The web console's ``accumulateSessionUsage`` de-duplicates: when a
    ``step_completed`` / ``step_failed`` record also exists for the same
    ``step_id``, it is preferred over the ``step_output`` record, so there
    is no double-counting.

    All other event types are a no-op here. The sink is safe to subscribe
    alongside :class:`CliSink` and :class:`JsonSink`; failures are swallowed
    so a flaky filesystem cannot break the running flow.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root)

    def consume(self, event: Event) -> None:
        if event.type == EventType.STEP_STARTED:
            self._record_started(event)
            return
        if event.type not in (
            EventType.STEP_COMPLETED,
            EventType.STEP_FAILED,
            EventType.STEP_OUTPUT,
        ):
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

    def _record_started(self, event: Event) -> None:
        """Persist a STEP_STARTED event as a state-aware ``step_started`` anchor.

        Unlike the terminal/output events, STEP_STARTED carries no ``step``
        object — only ``flow_id`` / ``step_id`` / ``step_type`` on the event
        itself.

        The write is decided by the step's CURRENT lifecycle state rather than a
        blanket "started already exists" guard, so the web region always tracks
        the step's latest state:

        * Skip once a terminal (``step_completed`` / ``step_failed``) record
          exists — a finished step must never re-gain a "进行中" anchor.
        * Skip when the LAST lifecycle anchor is already ``running`` — a
          re-emitted STEP_STARTED with no intervening pause must not stack a
          duplicate running anchor (no duplicate "进行中" row).
        * Otherwise (no anchor yet, or the last anchor is a non-running settled
          state such as ``paused`` / ``retrying``) write a fresh ``running``
          anchor. This is what re-arms "进行中" when a paused step resumes, so
          the region switches back from "已暂停" to "进行中" instead of staying
          frozen on the stale paused state.

        Failures are swallowed so a flaky filesystem cannot break the running
        flow.
        """
        flow_id = event.flow_id or ""
        step_id = event.step_id or ""
        if not step_id:
            return
        # Lazy import: keeps the sink module free of heavier engine deps.
        from .chat_history import (
            has_step_terminal_event,
            last_step_lifecycle_status,
            record_step_started,
        )

        if has_step_terminal_event(self.project_root, flow_id, step_id):
            return
        if last_step_lifecycle_status(self.project_root, flow_id, step_id) == "running":
            return
        record_step_started(
            project_root=self.project_root,
            flow_id=flow_id,
            step_id=step_id,
            step_type=event.step_type or "",
            timestamp=event.timestamp,
        )
