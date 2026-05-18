"""Tests for the unified event stream and pluggable sinks (G1)."""

from __future__ import annotations

import io
import json

import pytest

from se3.engine import (
    CliSink,
    Event,
    EventEmitter,
    EventStream,
    EventType,
    JsonSink,
    Sink,
    new_event,
)


# ---------------------------------------------------------------------------
# EventType / Event
# ---------------------------------------------------------------------------


def test_event_type_covers_full_lifecycle():
    expected = {
        "FLOW_STARTED",
        "STEP_STARTED",
        "STEP_OUTPUT",
        "STEP_COMPLETED",
        "STEP_FAILED",
        "FLOW_PAUSED",
        "FLOW_COMPLETED",
        "FLOW_FAILED",
        "INTERJECTION_NEEDED",
        "CALL_NEEDED",
    }
    assert {e.name for e in EventType} == expected


def test_event_to_dict_is_json_serializable():
    ev = Event(
        type=EventType.STEP_STARTED,
        timestamp=123.0,
        flow_id="flow-1",
        step_id="step-1",
        step_type="analyze",
        data={"k": "v"},
    )
    payload = ev.to_dict()
    encoded = json.dumps(payload)
    assert json.loads(encoded)["type"] == "step_started"
    assert payload["flow_id"] == "flow-1"


def test_event_round_trips_through_dict():
    ev = new_event(EventType.FLOW_COMPLETED, flow_id="f", message="done")
    restored = Event.from_dict(ev.to_dict())
    assert restored.type == EventType.FLOW_COMPLETED
    assert restored.flow_id == "f"
    assert restored.data == {"message": "done"}


def test_new_event_collects_kwargs_into_data():
    ev = new_event(
        EventType.STEP_OUTPUT, flow_id="f", step_id="s", step_type="test", foo=1, bar=2
    )
    assert ev.type == EventType.STEP_OUTPUT
    assert ev.step_type == "test"
    assert ev.data == {"foo": 1, "bar": 2}
    assert ev.timestamp > 0


# ---------------------------------------------------------------------------
# EventEmitter
# ---------------------------------------------------------------------------


class _RecordingSink(Sink):
    def __init__(self):
        self.events = []

    def consume(self, event: Event) -> None:
        self.events.append(event)


def test_emit_notifies_all_subscribed_sinks():
    emitter = EventEmitter()
    s1, s2 = _RecordingSink(), _RecordingSink()
    emitter.subscribe(s1)
    emitter.subscribe(s2)

    ev = new_event(EventType.FLOW_STARTED, flow_id="f")
    emitter.emit(ev)

    assert s1.events == [ev]
    assert s2.events == [ev]


def test_subscribe_is_idempotent():
    emitter = EventEmitter()
    sink = _RecordingSink()
    emitter.subscribe(sink)
    emitter.subscribe(sink)
    emitter.emit(new_event(EventType.FLOW_STARTED))
    assert len(sink.events) == 1


def test_unsubscribe_stops_delivery():
    emitter = EventEmitter()
    sink = _RecordingSink()
    emitter.subscribe(sink)
    emitter.unsubscribe(sink)
    emitter.emit(new_event(EventType.FLOW_STARTED))
    assert sink.events == []


def test_unsubscribe_unknown_sink_is_noop():
    emitter = EventEmitter()
    emitter.unsubscribe(_RecordingSink())  # must not raise


def test_emit_isolates_failing_sink():
    class _Boom(Sink):
        def consume(self, event):
            raise RuntimeError("boom")

    emitter = EventEmitter()
    good = _RecordingSink()
    emitter.subscribe(_Boom())
    emitter.subscribe(good)
    emitter.emit(new_event(EventType.FLOW_STARTED))
    assert len(good.events) == 1


def test_scope_restores_subscriber_list():
    emitter = EventEmitter()
    base = _RecordingSink()
    emitter.subscribe(base)

    with emitter.scope() as em:
        assert em is emitter
        em.subscribe(_RecordingSink())
        assert len(em.sinks) == 2

    # Sink added inside the scope is dropped on exit.
    assert emitter.sinks == [base]


def test_event_stream_alias_is_event_emitter():
    assert EventStream is EventEmitter


# ---------------------------------------------------------------------------
# JsonSink
# ---------------------------------------------------------------------------


def test_json_sink_emits_ndjson():
    buf = io.StringIO()
    sink = JsonSink(file=buf)
    sink.consume(new_event(EventType.FLOW_STARTED, flow_id="f1"))
    sink.consume(new_event(EventType.FLOW_COMPLETED, flow_id="f1"))

    lines = buf.getvalue().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["type"] == "flow_started"
    assert first["flow_id"] == "f1"
    assert set(first) == {"type", "timestamp", "flow_id", "step_id", "step_type", "data"}


def test_json_sink_compact_is_single_line():
    buf = io.StringIO()
    JsonSink(file=buf).consume(new_event(EventType.STEP_OUTPUT, data={"a": 1}))
    assert buf.getvalue().count("\n") == 1
    assert ": " not in buf.getvalue()  # compact separators


def test_json_sink_pretty_is_indented():
    buf = io.StringIO()
    JsonSink(file=buf, pretty=True).consume(new_event(EventType.STEP_OUTPUT))
    out = buf.getvalue()
    assert "\n  " in out  # indent=2 present


def test_json_sink_falls_back_to_str_for_non_serializable():
    buf = io.StringIO()
    sentinel = object()
    JsonSink(file=buf).consume(new_event(EventType.STEP_COMPLETED, step=sentinel))
    parsed = json.loads(buf.getvalue())
    assert isinstance(parsed["data"]["step"], str)


# ---------------------------------------------------------------------------
# CliSink
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_console():
    from rich.console import Console

    from se3.engine import display

    prev = display.get_console()
    console = Console(record=True, force_terminal=False, width=100)
    display.set_console(console)
    yield console
    display.set_console(prev)


def test_cli_sink_is_a_sink():
    assert issubclass(CliSink, Sink)


def test_cli_sink_flow_lifecycle_is_noop(captured_console):
    """Flow-level events are a no-op in CliSink — the CLI orchestrator renders
    the New Flow panel / summary directly, so the sink must not double-render.
    """
    sink = CliSink()
    for et in (
        EventType.FLOW_STARTED,
        EventType.FLOW_COMPLETED,
        EventType.FLOW_FAILED,
        EventType.FLOW_PAUSED,
        EventType.INTERJECTION_NEEDED,
        EventType.CALL_NEEDED,
    ):
        sink.consume(new_event(et, message="ignored"))
    assert captured_console.export_text() == ""


def test_cli_sink_step_output_is_noop(captured_console):
    """Raw STEP_OUTPUT events render nothing — the per-step renderer owns it."""
    CliSink().consume(new_event(EventType.STEP_OUTPUT, data={"x": 1}))
    assert captured_console.export_text() == ""


def test_cli_sink_step_started_is_noop(captured_console):
    """STEP_STARTED renders nothing — output is presented on completion."""
    CliSink().consume(new_event(EventType.STEP_STARTED, step_id="s1"))
    assert captured_console.export_text() == ""


def test_cli_sink_renders_completed_step(captured_console):
    from se3.engine.models import Step, StepStatus, StepType

    step = Step(step_id="s1", step_type=StepType.SUMMARIZE)
    step.status = StepStatus.COMPLETED
    step.outputs = {"summary": "Did the work."}

    CliSink().consume(
        new_event(EventType.STEP_COMPLETED, step_id="s1", step=step)
    )
    out = captured_console.export_text()
    assert "Work Summary" in out
    assert "Did the work." in out


def test_cli_sink_completed_step_without_step_object_is_safe(captured_console):
    CliSink().consume(new_event(EventType.STEP_COMPLETED, step_id="s1"))
    # No step payload -> nothing rendered, no exception.
    assert captured_console.export_text() == ""
