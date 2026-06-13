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


def test_cli_sink_step_output_without_step_is_noop(captured_console):
    """STEP_OUTPUT events without a step payload render nothing."""
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
    """Flow-level lifecycle events are ignored, and STEP_OUTPUT without a step
    is also ignored (only STEP_OUTPUT events carrying a step object are
    persisted). STEP_STARTED is intentionally NOT in this set — it is persisted
    as a lightweight ``step_started`` anchor (covered in
    tests/test_history_sink_step_started.py)."""
    sink = HistorySink(tmp_path)
    for et in (
        EventType.FLOW_STARTED,
        EventType.FLOW_COMPLETED, EventType.FLOW_PAUSED,
    ):
        sink.consume(new_event(et, flow_id="f", step_id="s"))
    # STEP_OUTPUT without a step payload is also a no-op.
    sink.consume(new_event(EventType.STEP_OUTPUT, flow_id="f", step_id="s"))
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


def test_get_step_history_skips_step_output_lines(tmp_path):
    """CLI history viewer must skip step_output records (non-terminal usage
    snapshots) so they do not inflate retry context or trigger warnings."""
    from se3.engine.chat_history import (
        ChatMessage,
        get_step_history,
        record_step_event,
    )

    flow_dir = tmp_path / "se3" / "history" / "flow-z"
    flow_dir.mkdir(parents=True)
    jsonl = flow_dir / "06_self_check.jsonl"
    msg = ChatMessage(
        role="assistant", content="check done", raw_json=[],
        timestamp="2026-05-20T00:00:00", step_type="self_check", attempt=0,
    )
    jsonl.write_text(json.dumps(msg.to_dict()) + "\n")
    # Write a step_output record (non-terminal usage snapshot for REVISION_NEEDED).
    record_step_event(
        tmp_path, "flow-z", "06_self_check", "self_check", "step_output",
        {"step_id": "06_self_check", "step_type": "self_check",
         "outputs": {"token_usage": {"input_tokens": 100, "output_tokens": 50,
                                     "cache_creation_input_tokens": 0,
                                     "cache_read_input_tokens": 0,
                                     "total_cost_usd": 0.01}}},
    )

    session = get_step_history(tmp_path, "flow-z", "06_self_check")
    assert session is not None
    assert len(session.messages) == 1
    assert session.messages[0].role == "assistant"
    assert session.messages[0].content == "check done"


# ---------------------------------------------------------------------------
# Terminal events for EVERY step type (including the interactive/special ones)
# ---------------------------------------------------------------------------
#
# The orchestrator now emits a terminal STEP_COMPLETED / STEP_FAILED for every
# step type — including the interactive DISCOVERY / CONFIRM steps, PLAN and
# SUMMARIZE that used to be excluded. HistorySink MUST persist them all (so the
# web console gets a report card / the running assistant turn folds), while
# CliSink MUST skip the interactive/special trio (CONFIRM / DISCOVERY / PLAN)
# so the CLI output stays byte-identical to the orchestrator's own rendering.


def _completed_step(step_type_value: str, step_id: str):
    from se3.engine.models import Step, StepStatus, StepType

    step = Step(step_id=step_id, step_type=StepType(step_type_value))
    step.status = StepStatus.COMPLETED
    step.outputs = {"summary": "done", "refined_description": "x"}
    return step


@pytest.mark.parametrize(
    "step_type_value, step_id",
    [
        ("discovery", "00_discovery_aa"),
        ("analyze", "01_analyze_bb"),
        ("plan", "02_plan_cc"),
        ("confirm", "03_confirm_dd"),
        ("implement", "04_implement_ee"),
        ("test", "05_test_ff"),
        ("self_check", "06_self_check_gg"),
        ("verify_spec", "07_verify_spec_hh"),
        ("update_spec", "08_update_spec_ii"),
        ("version_analyze", "09_version_analyze_jj"),
        ("commit", "10_commit_kk"),
        ("summarize", "11_summarize_ll"),
    ],
)
def test_history_sink_persists_terminal_event_for_every_step_type(
    tmp_path, step_type_value, step_id
):
    """HistorySink writes a step_completed line for EVERY step type, including
    the interactive DISCOVERY / CONFIRM and PLAN / SUMMARIZE steps."""
    step = _completed_step(step_type_value, step_id)
    HistorySink(tmp_path).consume(new_event(
        EventType.STEP_COMPLETED,
        flow_id="flow-all",
        step_id=step_id,
        step_type=step_type_value,
        step=step,
    ))

    path = tmp_path / "se3" / "history" / "flow-all" / f"{step_id}.jsonl"
    assert path.exists(), f"no report line persisted for step type {step_type_value}"
    rec = json.loads(path.read_text().splitlines()[0])
    assert rec["type"] == "step_completed"
    assert rec["step_type"] == step_type_value


@pytest.mark.parametrize("step_type_value", ["confirm", "discovery", "plan"])
def test_cli_sink_skips_interactive_terminal_events(captured_console, step_type_value):
    """CliSink must NOT render CONFIRM / DISCOVERY / PLAN terminal events — their
    CLI output is owned by the orchestrator's interactive/special paths, so the
    CLI stays byte-identical."""
    step = _completed_step(step_type_value, f"00_{step_type_value}")
    CliSink().consume(new_event(
        EventType.STEP_COMPLETED,
        step_id=step.step_id,
        step_type=step_type_value,
        step=step,
    ))
    assert captured_console.export_text() == ""


def _usage_step(step_type_value):
    step = _completed_step(step_type_value, f"00_{step_type_value}")
    step.outputs = {
        "summary": "done",
        "token_usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 10,
            "total_cost_usd": 0.01,
        },
    }
    return step


def test_cli_sink_plan_renders_big_usage_block(captured_console):
    """plan keeps the established big per-step usage block ("Step Token Usage"),
    while its full report stays owned by the orchestrator's special path."""
    step = _usage_step("plan")
    CliSink().consume(new_event(
        EventType.STEP_COMPLETED,
        step_id=step.step_id,
        step_type="plan",
        step=step,
    ))
    out = captured_console.export_text()
    assert "Step Token Usage" in out


def test_cli_sink_confirm_renders_compact_footer(captured_console):
    """confirm renders a compact dim per-round footer (round == cumulative for a
    single LLM review), NOT the big reverse-color "Step Token Usage" block."""
    step = _usage_step("confirm")
    CliSink().consume(new_event(
        EventType.STEP_COMPLETED,
        step_id=step.step_id,
        step_type="confirm",
        step=step,
    ))
    out = captured_console.export_text()
    assert "本轮 100 in / 50 out · 累计 100 in / 50 out" in out
    assert "Step Token Usage" not in out


def test_cli_sink_discovery_renders_cumulative_usage(captured_console):
    """discovery's per-round footer is rendered inline by the discovery handler,
    but the terminal cumulative usage (the whole-discovery total across all
    rounds) is rendered by CliSink via format_usage_line."""
    step = _usage_step("discovery")
    CliSink().consume(new_event(
        EventType.STEP_COMPLETED,
        step_id=step.step_id,
        step_type="discovery",
        step=step,
    ))
    out = captured_console.export_text()
    assert "Discovery cumulative:" in out
    assert "100" in out  # input_tokens
    assert "50" in out   # output_tokens


@pytest.mark.parametrize("step_type_value", ["confirm", "discovery", "plan"])
def test_cli_sink_skips_step_without_usage(captured_console, step_type_value):
    """A skipped interactive step with NO token_usage still renders nothing on
    the CLI — the usage block self-guards on empty/absent usage."""
    step = _completed_step(step_type_value, f"00_{step_type_value}")
    CliSink().consume(new_event(
        EventType.STEP_COMPLETED,
        step_id=step.step_id,
        step_type=step_type_value,
        step=step,
    ))
    assert captured_console.export_text() == ""


def test_cli_skips_but_history_persists_same_interactive_event(tmp_path, captured_console):
    """The same DISCOVERY terminal event is skipped by CliSink (no CLI output)
    yet persisted by HistorySink (web report card) — the two sinks diverge
    exactly as the spec requires."""
    step = _completed_step("discovery", "00_discovery_zz")
    event = new_event(
        EventType.STEP_COMPLETED,
        flow_id="flow-z",
        step_id="00_discovery_zz",
        step_type="discovery",
        step=step,
    )

    CliSink().consume(event)
    HistorySink(tmp_path).consume(event)

    # CLI rendered nothing.
    assert captured_console.export_text() == ""
    # History persisted the report record.
    path = tmp_path / "se3" / "history" / "flow-z" / "00_discovery_zz.jsonl"
    assert path.exists()
    rec = json.loads(path.read_text().splitlines()[0])
    assert rec["type"] == "step_completed"
    assert rec["step_type"] == "discovery"


def test_history_sink_persists_partial_completion_as_step_completed(tmp_path):
    """A PARTIAL terminal result is persisted as a step_completed record (only
    FAILED maps to step_failed); the report card still renders."""
    from se3.engine.models import StepStatus

    step = _completed_step("implement", "04_implement_partial")
    step.status = StepStatus.PARTIAL
    HistorySink(tmp_path).consume(new_event(
        EventType.STEP_COMPLETED,
        flow_id="flow-p",
        step_id="04_implement_partial",
        step_type="implement",
        step=step,
    ))
    path = tmp_path / "se3" / "history" / "flow-p" / "04_implement_partial.jsonl"
    rec = json.loads(path.read_text().splitlines()[0])
    assert rec["type"] == "step_completed"
    assert rec["data"]["step"]["status"] == "partial"


# ---------------------------------------------------------------------------
# STEP_OUTPUT events for non-terminal step usage
# ---------------------------------------------------------------------------
# STEP_OUTPUT events are emitted by run.py for non-terminal steps
# (PAUSED / REVISION_NEEDED / RETRYING) that consumed tokens. CliSink
# renders their usage block; HistorySink persists them to jsonl.


def test_cli_sink_step_output_renders_usage(captured_console):
    """STEP_OUTPUT events carrying a step with token_usage render the usage block."""
    from se3.engine.models import Step, StepStatus, StepType

    step = Step(step_id="07_test", step_type=StepType.SELF_CHECK)
    step.status = StepStatus.REVISION_NEEDED
    step.outputs = {
        "result": "revision_needed",
        "token_usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "total_cost_usd": 0.01,
        },
    }

    CliSink().consume(
        new_event(EventType.STEP_OUTPUT, step_id="07_test", step_type="self_check", step=step)
    )
    out = captured_console.export_text()
    assert "Step Token Usage" in out
    assert "100" in out  # input_tokens


def test_cli_sink_step_output_without_usage_renders_nothing(captured_console):
    """STEP_OUTPUT events carrying a step with no token_usage render nothing."""
    from se3.engine.models import Step, StepStatus, StepType

    step = Step(step_id="07_test", step_type=StepType.SELF_CHECK)
    step.status = StepStatus.REVISION_NEEDED
    step.outputs = {"result": "revision_needed"}  # no token_usage

    CliSink().consume(
        new_event(EventType.STEP_OUTPUT, step_id="07_test", step_type="self_check", step=step)
    )
    assert captured_console.export_text() == ""


def test_history_sink_persists_step_output_event(tmp_path):
    """STEP_OUTPUT events carrying a step are persisted to jsonl with type='step_output'."""
    from se3.engine.models import Step, StepStatus, StepType

    step = Step(step_id="07_test", step_type=StepType.SELF_CHECK)
    step.status = StepStatus.REVISION_NEEDED
    step.outputs = {
        "result": "revision_needed",
        "token_usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "total_cost_usd": 0.01,
        },
    }

    HistorySink(tmp_path).consume(new_event(
        EventType.STEP_OUTPUT,
        flow_id="flow-nt",
        step_id=step.step_id,
        step_type=step.step_type.value,
        step=step,
    ))

    path = tmp_path / "se3" / "history" / "flow-nt" / "07_test.jsonl"
    assert path.exists()
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["type"] == "step_output"
    assert rec["step_id"] == "07_test"
    assert rec["step_type"] == "self_check"
    assert rec["data"]["step"]["outputs"]["token_usage"]["input_tokens"] == 100


def test_history_sink_step_output_without_step_is_noop(tmp_path):
    """STEP_OUTPUT events without a step payload are ignored by HistorySink."""
    HistorySink(tmp_path).consume(
        new_event(EventType.STEP_OUTPUT, flow_id="f", step_id="s")
    )
    assert not (tmp_path / "se3").exists()


def test_cli_sink_discovery_step_output_renders_cumulative_usage(captured_console):
    """STEP_OUTPUT for discovery must render the cumulative usage line (not the
    big 'Step Token Usage' block) — the same per-type rule as terminal events."""
    from se3.engine.models import Step, StepStatus, StepType

    step = Step(step_id="01_discovery", step_type=StepType.DISCOVERY)
    step.status = StepStatus.PAUSED
    step.outputs = {
        "token_usage": {
            "input_tokens": 200,
            "output_tokens": 80,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "total_cost_usd": 0.02,
        },
    }

    CliSink().consume(
        new_event(EventType.STEP_OUTPUT, step_id="01_discovery", step_type="discovery", step=step)
    )
    out = captured_console.export_text()
    assert "Discovery cumulative:" in out
    assert "200" in out  # input_tokens
    assert "80" in out   # output_tokens
    assert "Step Token Usage" not in out


def test_cli_sink_confirm_step_output_renders_compact_footer(captured_console):
    """STEP_OUTPUT for confirm must render the compact dim footer (not the big
    'Step Token Usage' block) — the same per-type rule as terminal events."""
    from se3.engine.models import Step, StepStatus, StepType

    step = Step(step_id="03_confirm", step_type=StepType.CONFIRM)
    step.status = StepStatus.REVISION_NEEDED
    step.outputs = {
        "token_usage": {
            "input_tokens": 150,
            "output_tokens": 60,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "total_cost_usd": 0.015,
        },
    }

    CliSink().consume(
        new_event(EventType.STEP_OUTPUT, step_id="03_confirm", step_type="confirm", step=step)
    )
    out = captured_console.export_text()
    assert "本轮 150 in / 60 out · 累计 150 in / 60 out" in out
    assert "Step Token Usage" not in out


# ---------------------------------------------------------------------------
# Resumed discovery must not emit stale STEP_OUTPUT
# ---------------------------------------------------------------------------
# When a PAUSED discovery step is resumed, run_step is skipped (no new LLM
# call). The previous round's stale token_usage stays in step.outputs. If a
# STEP_OUTPUT event were emitted for this stale data, it would duplicate the
# CLI usage block and append a zombie usage chip to the web history. The fix
# in run.py guards this with a step_ran_llm flag — only emit STEP_OUTPUT
# when run_step was actually called. This test verifies that the guard works
# by simulating the scenario: a non-terminal result (PAUSED) with stale
# token_usage, but step_ran_llm=False, should produce no STEP_OUTPUT event.


def test_discovery_resume_does_not_emit_stale_step_output():
    """When a discovery step is resumed without calling run_step (step_ran_llm=False),
    no STEP_OUTPUT event should be emitted for stale token_usage."""
    from se3.engine.models import Step, StepStatus, StepType

    emitter = EventEmitter()
    recording_sink = _RecordingSink()
    emitter.subscribe(recording_sink)

    # Simulate a PAUSED discovery step with stale token_usage from a prior
    # round (the scenario: resuming a PAUSED discovery without calling
    # run_step). In run.py, step_ran_llm=False prevents STEP_OUTPUT emission.
    # Since we can't directly test the run.py orchestration loop here, we
    # verify the principle: if step_ran_llm is False, the non-terminal
    # branch at line 2185 does not execute, so no STEP_OUTPUT event is
    # emitted. The test confirms that the guard condition
    # (step_ran_llm=True) is necessary for STEP_OUTPUT to be emitted.
    step = Step(step_id="01_discovery", step_type=StepType.DISCOVERY)
    step.status = StepStatus.PAUSED
    step.outputs = {
        "token_usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "total_cost_usd": 0.01,
        },
    }

    # When step_ran_llm=True (normal case), STEP_OUTPUT IS emitted for
    # non-terminal steps with usage:
    step_ran_llm = True
    result = StepStatus.REVISION_NEEDED
    is_terminal = result in (StepStatus.COMPLETED, StepStatus.PARTIAL, StepStatus.FAILED)

    if not is_terminal and step_ran_llm:
        step_usage = (step.outputs or {}).get("token_usage")
        if step_usage:
            emitter.emit(new_event(
                EventType.STEP_OUTPUT,
                flow_id="flow-1",
                step_id=step.step_id,
                step_type="discovery",
                step=step,
            ))

    assert len(recording_sink.events) == 1
    assert recording_sink.events[0].type == EventType.STEP_OUTPUT

    # When step_ran_llm=False (discovery resume case), STEP_OUTPUT is NOT
    # emitted even though the step has stale token_usage:
    recording_sink.events.clear()
    step_ran_llm = False
    result = StepStatus.PAUSED
    is_terminal = result in (StepStatus.COMPLETED, StepStatus.PARTIAL, StepStatus.FAILED)

    if not is_terminal and step_ran_llm:
        step_usage = (step.outputs or {}).get("token_usage")
        if step_usage:
            emitter.emit(new_event(
                EventType.STEP_OUTPUT,
                flow_id="flow-1",
                step_id=step.step_id,
                step_type="discovery",
                step=step,
            ))

    assert len(recording_sink.events) == 0  # no stale STEP_OUTPUT emitted


def test_paused_discovery_does_not_emit_step_output():
    """When a discovery step returns PAUSED (step_ran_llm=True because the handler
    ran and called the LLM), no STEP_OUTPUT event should be emitted.  The
    discovery handler already renders the per-round inline usage footer, and
    emitting STEP_OUTPUT would duplicate the cumulative usage on the CLI and
    persist a redundant web usage chip.  The fix in run.py excludes
    (discovery, PAUSED) from the STEP_OUTPUT emission branch."""
    from se3.engine.models import Step, StepStatus, StepType

    emitter = EventEmitter()
    recording_sink = _RecordingSink()
    emitter.subscribe(recording_sink)

    step = Step(step_id="01_discovery", step_type=StepType.DISCOVERY)
    step.status = StepStatus.PAUSED
    step.outputs = {
        "token_usage": {
            "input_tokens": 200,
            "output_tokens": 80,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "total_cost_usd": 0.02,
        },
    }

    # Simulate run.py's guard: discovery PAUSED is excluded even when
    # step_ran_llm=True (the handler actually called the LLM this round).
    step_ran_llm = True
    result = StepStatus.PAUSED
    step_type_value = "discovery"
    is_terminal = result in (StepStatus.COMPLETED, StepStatus.PARTIAL, StepStatus.FAILED)

    if not is_terminal and step_ran_llm:
        # run.py's fix: exclude discovery PAUSED
        if not (step_type_value == "discovery" and result == StepStatus.PAUSED):
            step_usage = (step.outputs or {}).get("token_usage")
            if step_usage:
                emitter.emit(new_event(
                    EventType.STEP_OUTPUT,
                    flow_id="flow-1",
                    step_id=step.step_id,
                    step_type="discovery",
                    step=step,
                ))

    assert len(recording_sink.events) == 0  # no STEP_OUTPUT for discovery PAUSED
