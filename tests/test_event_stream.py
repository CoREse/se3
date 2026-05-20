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
    HistorySink,
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


# ---------------------------------------------------------------------------
# HistorySink
# ---------------------------------------------------------------------------


def _make_step(step_id: str = "07_test", step_type_value: str = "test"):
    from se3.engine.models import Step, StepStatus, StepType

    step = Step(step_id=step_id, step_type=StepType(step_type_value))
    step.status = StepStatus.COMPLETED
    step.outputs = {"test_results": {"overall_passed": True}}
    return step


def test_history_sink_writes_step_completed_to_jsonl(tmp_path):
    step = _make_step()
    sink = HistorySink(tmp_path)
    sink.consume(new_event(
        EventType.STEP_COMPLETED,
        flow_id="flow-1",
        step_id=step.step_id,
        step_type=step.step_type.value,
        step=step,
    ))

    path = tmp_path / "se3" / "history" / "flow-1" / "07_test.jsonl"
    assert path.exists()
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["type"] == "step_completed"
    assert rec["step_id"] == "07_test"
    assert rec["step_type"] == "test"
    assert rec["data"]["step"]["outputs"]["test_results"]["overall_passed"] is True


def test_history_sink_writes_step_failed(tmp_path):
    from se3.engine.models import StepStatus

    step = _make_step(step_id="04_implement", step_type_value="implement")
    step.status = StepStatus.FAILED
    step.error_message = "boom"

    sink = HistorySink(tmp_path)
    sink.consume(new_event(
        EventType.STEP_FAILED,
        flow_id="flow-2",
        step_id=step.step_id,
        step_type=step.step_type.value,
        step=step,
    ))

    path = tmp_path / "se3" / "history" / "flow-2" / "04_implement.jsonl"
    rec = json.loads(path.read_text().splitlines()[0])
    assert rec["type"] == "step_failed"
    assert rec["data"]["step"]["error_message"] == "boom"


def test_history_sink_ignores_non_step_events(tmp_path):
    sink = HistorySink(tmp_path)
    for et in (
        EventType.FLOW_STARTED, EventType.STEP_STARTED, EventType.STEP_OUTPUT,
        EventType.FLOW_COMPLETED, EventType.FLOW_PAUSED,
    ):
        sink.consume(new_event(et, flow_id="f", step_id="s"))
    # No files written.
    assert not (tmp_path / "se3").exists()


def test_history_sink_no_op_when_step_payload_missing(tmp_path):
    HistorySink(tmp_path).consume(
        new_event(EventType.STEP_COMPLETED, flow_id="f", step_id="s")
    )
    assert not (tmp_path / "se3").exists()


def test_history_reader_surfaces_step_event_records(tmp_path):
    """End-to-end: HistorySink writes a line; the daemon's history reader picks
    it up as an unmodified ``{step_id, message}`` record so the frontend's
    normalizeRecord can route it to renderStepReport."""
    from se3.daemon.history import DaemonHistoryReader

    step = _make_step()
    HistorySink(tmp_path).consume(new_event(
        EventType.STEP_COMPLETED,
        flow_id="flow-x",
        step_id=step.step_id,
        step_type=step.step_type.value,
        step=step,
    ))

    reader = DaemonHistoryReader(lambda: [str(tmp_path)])
    read = reader.read_flow("flow-x", project_root=str(tmp_path))
    messages = [r["message"] for r in read.records]
    step_events = [m for m in messages if m.get("type") == "step_completed"]
    assert len(step_events) == 1
    assert step_events[0]["data"]["step"]["step_type"] == "test"


def test_get_step_history_skips_step_event_lines(tmp_path):
    """CLI history viewer must ignore step_event records mixed into the jsonl."""
    from se3.engine.chat_history import (
        ChatMessage,
        get_step_history,
        record_step_event,
    )

    # Write one assistant ChatMessage and one step_event record into the same
    # jsonl, then verify the CLI viewer surfaces only the chat message.
    flow_dir = tmp_path / "se3" / "history" / "flow-y"
    flow_dir.mkdir(parents=True)
    jsonl = flow_dir / "07_test.jsonl"
    msg = ChatMessage(
        role="assistant", content="hi", raw_json=[],
        timestamp="2026-05-20T00:00:00", step_type="test", attempt=0,
    )
    jsonl.write_text(json.dumps(msg.to_dict()) + "\n")
    record_step_event(
        tmp_path, "flow-y", "07_test", "test", "step_completed",
        {"step_id": "07_test", "step_type": "test", "outputs": {}},
    )

    session = get_step_history(tmp_path, "flow-y", "07_test")
    assert session is not None
    assert len(session.messages) == 1
    assert session.messages[0].role == "assistant"
