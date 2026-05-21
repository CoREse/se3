"""Running-flow console end-to-end chain tests (group G1).

These tests pin the engine-side fix that lets the web console render a
default-expanded final report card for *every* finished step — including the
interactive CONFIRM / DISCOVERY steps, PLAN, and SUMMARIZE, which the
orchestrator used to exclude from the terminal-event emission.

Coverage:

* :func:`se3.commands.run.run_flow` now emits ``step_completed`` /
  ``step_failed`` for CONFIRM / DISCOVERY / PLAN / SUMMARIZE, so
  ``HistorySink`` persists their outputs to the per-step jsonl (the data the
  frontend turns into a report card).
* A step that has *not* reached a terminal state (PAUSED) does NOT emit a
  terminal event, so no premature card is written.
* ``CliSink`` keeps the CLI byte-identical: it is a no-op for
  CONFIRM/DISCOVERY/PLAN terminal events (their CLI output is owned by the
  interactive/special paths) while still rendering the others.
* ``record_step_event`` writes a JSON-serializable, stably-shaped record and
  ``get_step_history`` skips those event records on the CLI side.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from se3.commands.run import run_flow
from se3.daemon import protocol as daemon_protocol
from se3.daemon.history import DaemonHistoryReader
from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.persistence import PersistenceManager


# ---------------------------------------------------------------------------
# Harness: drive one step through the real run_flow loop with a mocked state
# machine + persistence, leaving the real (unconditionally-subscribed)
# HistorySink to write into the real project_root.
# ---------------------------------------------------------------------------


def _build_flow(step_type: StepType, *, outputs: dict, status: StepStatus) -> FlowInstance:
    flow = FlowInstance(
        flow_id="chain-flow-001",
        task_description="console chain task",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = [step_type]
    flow.state.current_step_index = 0
    step = Step(
        step_type=step_type,
        status=StepStatus.PENDING,
        step_id=f"01_{step_type.value}_abc12345",
        inputs={},
        outputs=dict(outputs),
    )
    step.status = status
    flow.state.add_step(step)
    flow.state.current_step_id = step.step_id
    return flow


def _run_step(project_root: Path, flow: FlowInstance, run_result: StepStatus):
    """Run the single step in *flow* through run_flow (cli mode).

    The state machine is mocked: ``run_step`` returns *run_result* and
    ``transition_to_next`` ends the loop by marking the flow COMPLETED.
    HistorySink is the real one wired up inside ``_run_flow_impl`` and writes
    to ``project_root``.
    """
    (project_root / "se3" / "state").mkdir(parents=True, exist_ok=True)

    with patch("se3.commands.run.PersistenceManager") as mock_pm_class, patch(
        "se3.commands.run.StateMachine"
    ) as mock_sm_class, patch("se3.commands.run.STEP_HANDLERS", {}), patch(
        "se3.commands.run.render_full"
    ), patch(
        # FAILED steps would otherwise hit the retry/abort decision path; force
        # a clean pause so the run returns without prompting. The terminal
        # STEP_FAILED event has already been emitted by then.
        "se3.commands.run._resolve_step_failure_action",
        return_value=("pause", str(project_root / "se3" / "calls" / "x.json")),
    ):
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = flow

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm
        mock_sm.run_step.return_value = run_result
        mock_sm.transition_to_next.side_effect = (
            lambda f: setattr(f, "status", FlowStatus.COMPLETED)
        )

        return run_flow(
            project_root=project_root,
            flow_id="chain-flow-001",
            output_format="cli",
        )


def _history_path(project_root: Path, flow: FlowInstance) -> Path:
    step_id = flow.state.current_step_id
    return project_root / "se3" / "history" / flow.flow_id / f"{step_id}.jsonl"


def _read_event_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if isinstance(rec, dict) and rec.get("type") in ("step_completed", "step_failed"):
            out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Task 1/2: the four previously-handled-or-excluded step types now emit and
# persist a terminal event.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "step_type, outputs",
    [
        (StepType.DISCOVERY, {"refined_description": "do the thing", "requirements_clarified": True}),
        (StepType.PLAN, {"task_groups": [{"group_id": "G1", "name": "g", "tasks": []}]}),
        (StepType.CONFIRM, {"review_result": {"approved": True}}),
        (StepType.SUMMARIZE, {"summary": "All work done."}),
    ],
)
def test_terminal_step_completed_is_persisted(step_type, outputs):
    """DISCOVERY / PLAN / CONFIRM / SUMMARIZE now land a step_completed record
    in the per-step jsonl when they finish."""
    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        flow = _build_flow(step_type, outputs=outputs, status=StepStatus.PENDING)
        exit_code = _run_step(project_root, flow, StepStatus.COMPLETED)

        assert exit_code == 0
        records = _read_event_records(_history_path(project_root, flow))
        assert len(records) == 1, f"expected one step_completed for {step_type.value}"
        rec = records[0]
        assert rec["type"] == "step_completed"
        assert rec["step_type"] == step_type.value
        # The structured outputs the frontend renders as a report card are
        # carried verbatim under data.step.outputs.
        assert rec["data"]["step"]["outputs"] == outputs


def test_failed_step_persists_step_failed():
    """A terminal FAILED result emits step_failed before the failure-decision
    path runs, so the web console still gets a card."""
    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        flow = _build_flow(
            StepType.PLAN, outputs={"error": "boom"}, status=StepStatus.PENDING
        )
        # Mark the step itself FAILED so the persisted record carries the error.
        flow.state.get_current_step().error_message = "boom"
        _run_step(project_root, flow, StepStatus.FAILED)

        records = _read_event_records(_history_path(project_root, flow))
        assert len(records) == 1
        assert records[0]["type"] == "step_failed"
        assert records[0]["data"]["step"]["error_message"] == "boom"


def test_paused_step_does_not_persist_terminal_event():
    """A non-terminal PAUSED result must NOT emit a terminal event — the step
    has not finished, so no premature report card is written."""
    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        # ANALYZE has no interactive pause handler; a PAUSED result simply falls
        # through and the loop ends. The point is: nothing terminal is persisted.
        flow = _build_flow(
            StepType.ANALYZE, outputs={"reasoning": "x"}, status=StepStatus.PENDING
        )
        _run_step(project_root, flow, StepStatus.PAUSED)

        records = _read_event_records(_history_path(project_root, flow))
        assert records == []


# ---------------------------------------------------------------------------
# Task 2: CliSink stays a no-op for CONFIRM/DISCOVERY/PLAN terminal events, but
# still renders the others (so the CLI is byte-identical to before the fix).
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


@pytest.mark.parametrize(
    "step_type", [StepType.CONFIRM, StepType.DISCOVERY, StepType.PLAN]
)
def test_cli_sink_noop_for_interactive_and_plan(captured_console, step_type):
    from se3.engine import CliSink, EventType, new_event

    step = Step(step_id=f"01_{step_type.value}_x", step_type=step_type)
    step.status = StepStatus.COMPLETED
    step.outputs = {"refined_description": "r", "task_groups": []}

    CliSink().consume(
        new_event(
            EventType.STEP_COMPLETED,
            step_id=step.step_id,
            step_type=step_type.value,
            step=step,
        )
    )
    # CLI output for these steps is owned by their interactive/special paths;
    # the sink must not double-render them.
    assert captured_console.export_text() == ""


def test_cli_sink_still_renders_summarize(captured_console):
    from se3.engine import CliSink, EventType, new_event

    step = Step(step_id="12_summarize_x", step_type=StepType.SUMMARIZE)
    step.status = StepStatus.COMPLETED
    step.outputs = {"summary": "Did the work."}

    CliSink().consume(
        new_event(
            EventType.STEP_COMPLETED,
            step_id=step.step_id,
            step_type="summarize",
            step=step,
        )
    )
    out = captured_console.export_text()
    assert "Work Summary" in out
    assert "Did the work." in out


def test_cli_sink_derives_skip_type_from_step_when_event_type_missing(captured_console):
    """Even if the event carries no step_type string, the skip decision falls
    back to the step object's own step_type."""
    from se3.engine import CliSink, EventType, new_event

    step = Step(step_id="01_plan_x", step_type=StepType.PLAN)
    step.status = StepStatus.COMPLETED
    step.outputs = {"task_groups": []}

    CliSink().consume(
        new_event(EventType.STEP_COMPLETED, step_id=step.step_id, step=step)
    )
    assert captured_console.export_text() == ""


# ---------------------------------------------------------------------------
# Task 3: record_step_event shape + get_step_history skips event records.
# ---------------------------------------------------------------------------


def test_record_step_event_shape_is_stable_and_serializable():
    from se3.engine.chat_history import record_step_event

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        record_step_event(
            project_root=project_root,
            flow_id="flow-z",
            step_id="03_plan_x",
            step_type="plan",
            event_type="step_completed",
            step_dict={
                "step_id": "03_plan_x",
                "step_type": "plan",
                "outputs": {"task_groups": [], "nested": {"k": [1, 2, 3]}},
            },
        )
        path = project_root / "se3" / "history" / "flow-z" / "03_plan_x.jsonl"
        rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

        # Stable top-level shape the frontend's normalizeRecord depends on.
        assert set(rec) == {"type", "step_id", "step_type", "timestamp", "data"}
        assert rec["type"] == "step_completed"
        assert rec["step_id"] == "03_plan_x"
        assert rec["step_type"] == "plan"
        assert rec["data"]["step"]["outputs"]["task_groups"] == []
        # Round-trips through JSON as plain primitives.
        assert json.loads(json.dumps(rec)) == rec


def test_get_step_history_skips_step_event_records():
    from se3.engine.chat_history import (
        ChatMessage,
        get_step_history,
        record_step_event,
    )

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        flow_dir = project_root / "se3" / "history" / "flow-y"
        flow_dir.mkdir(parents=True)
        jsonl = flow_dir / "01_discovery_x.jsonl"
        msg = ChatMessage(
            role="assistant",
            content="hello",
            raw_json=[],
            timestamp="2026-05-21T00:00:00",
            step_type="discovery",
            attempt=0,
        )
        jsonl.write_text(json.dumps(msg.to_dict()) + "\n", encoding="utf-8")
        record_step_event(
            project_root,
            "flow-y",
            "01_discovery_x",
            "discovery",
            "step_completed",
            {"step_id": "01_discovery_x", "step_type": "discovery", "outputs": {}},
        )

        session = get_step_history(project_root, "flow-y", "01_discovery_x")
        assert session is not None
        # The CLI viewer surfaces only the chat turn, not the report-card event.
        assert len(session.messages) == 1
        assert session.messages[0].role == "assistant"


# ---------------------------------------------------------------------------
# Task 5 (group G2): daemon incremental read + history-data push chain.
#
# The engine-side fix (G1) writes a ``step_completed`` line into the per-step
# jsonl. These tests pin the next two hops — ``DaemonHistoryReader`` and
# ``protocol.make_history_data`` — so the report card actually reaches the
# frontend:
#
# * ``read_active_flows`` returns the ``step_completed`` line of an active flow
#   WITHOUT filtering it out as a non-chat record (the bug behind "even a
#   finished step shows no final card");
# * the record rides ``make_history_data`` in the exact ``{step_id, message:
#   {type, data:{step:{outputs}}}}`` shape ``app.js`` ``normalizeRecord``
#   unwraps;
# * the per-step line cursor advances so repeated polls never re-push the same
#   line, while a freshly-appended ``step_completed`` line (same file or a new
#   step file) is surfaced on the next poll.
# ---------------------------------------------------------------------------


def _seed_active_flow(project_root: Path, flow_id: str, step_id: str, *, lines):
    """Write engine.json (active) + a per-step jsonl with *lines* raw records.

    Returns the path to the seeded jsonl so a test can append to it, mimicking
    how ``HistorySink`` appends a ``step_completed`` line when the step
    finishes mid-flow.
    """
    state_dir = project_root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "engine.json").write_text(
        json.dumps(
            {
                "flow_id": flow_id,
                "task_description": "console chain task",
                "task_type": "feature",
                "status": "running",  # not terminal -> active source
                "project_root": str(project_root),
            }
        ),
        encoding="utf-8",
    )
    hist_dir = project_root / "se3" / "history" / flow_id
    hist_dir.mkdir(parents=True, exist_ok=True)
    jsonl = hist_dir / f"{step_id}.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return jsonl


def _step_completed_line(step_id: str, step_type: str, outputs: dict) -> dict:
    """The exact line shape ``record_step_event`` writes for a finished step."""
    return {
        "type": "step_completed",
        "step_id": step_id,
        "step_type": step_type,
        "timestamp": "2026-05-21T01:00:00",
        "data": {
            "step": {
                "step_id": step_id,
                "step_type": step_type,
                "status": "completed",
                "outputs": outputs,
            }
        },
    }


def _frontend_outputs(record: dict) -> dict:
    """Mirror ``app.js`` ``normalizeRecord``'s outputs extraction for an event.

    A pushed record is ``{step_id, message:{...}}``; the frontend reads the
    step output from ``message.data.step.outputs``. Asserting through this
    helper proves the daemon payload is consumable by the report-card path.
    """
    msg = record["message"]
    assert msg["type"] in ("step_completed", "step_failed")
    return msg["data"]["step"]["outputs"]


def _step_events(read) -> list:
    return [
        r
        for r in read.records
        if r["message"].get("type") in ("step_completed", "step_failed")
    ]


def test_read_active_flows_returns_step_completed_record():
    """An active flow's ``step_completed`` line is returned by the incremental
    reader (interleaved with chat turns, not filtered) and rides
    ``make_history_data`` in the frontend-consumable shape."""
    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        outputs = {
            "task_type": "feature",
            "complexity": "medium",
            "reasoning": "ok",
            "relevant_specs": ["base"],
        }
        _seed_active_flow(
            project_root,
            "F1",
            "01_analyze_abc",
            lines=[
                {
                    "role": "user",
                    "content": "prompt",
                    "step_type": "analyze",
                    "timestamp": "2026-05-21T00:00:00",
                },
                {
                    "role": "assistant",
                    "content": "resp",
                    "step_type": "analyze",
                    "timestamp": "2026-05-21T00:30:00",
                },
                _step_completed_line("01_analyze_abc", "analyze", outputs),
            ],
        )
        reader = DaemonHistoryReader(lambda: [str(project_root)])

        reads = reader.read_active_flows()
        assert len(reads) == 1
        read = reads[0]
        assert read.flow_id == "F1"
        assert read.mode == daemon_protocol.HISTORY_MODE_FULL
        # The chat turns AND the step_completed line are all present.
        assert len(read.records) == 3
        events = _step_events(read)
        assert len(events) == 1
        assert events[0]["step_id"] == "01_analyze_abc"
        assert _frontend_outputs(events[0]) == outputs

        # The record rides make_history_data verbatim in the right shape.
        msg = daemon_protocol.make_history_data(
            read.flow_id, read.mode, read.records, cursor=read.cursor
        )
        assert msg.type == daemon_protocol.MSG_HISTORY_DATA
        assert msg.payload["flow_id"] == "F1"
        assert msg.payload["mode"] == daemon_protocol.HISTORY_MODE_FULL
        pushed_events = [
            r
            for r in msg.payload["records"]
            if r["message"].get("type") == "step_completed"
        ]
        assert len(pushed_events) == 1
        assert _frontend_outputs(pushed_events[0]) == outputs
        # The cursor counts every consumed line so the next poll is incremental.
        assert msg.payload["cursor"]["01_analyze_abc.jsonl"] == 3


def test_active_flow_incremental_does_not_duplicate_step_completed():
    """Repeated polling with the returned cursor never re-pushes a line; a
    NEW step's step_completed line (a fresh jsonl) is surfaced exactly once."""
    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        _seed_active_flow(
            project_root,
            "F2",
            "01_analyze_abc",
            lines=[
                {
                    "role": "assistant",
                    "content": "r",
                    "step_type": "analyze",
                    "timestamp": "2026-05-21T00:00:00",
                },
                _step_completed_line("01_analyze_abc", "analyze", {"reasoning": "first"}),
            ],
        )
        reader = DaemonHistoryReader(lambda: [str(project_root)])
        cursors: dict = {}

        # First poll: full snapshot includes the step_completed line.
        reads = reader.read_active_flows(cursors)
        cursors[reads[0].flow_id] = reads[0].cursor
        assert len(_step_events(reads[0])) == 1

        # Second poll, same cursor, no new lines: nothing re-pushed.
        reads = reader.read_active_flows(cursors)
        cursors[reads[0].flow_id] = reads[0].cursor
        assert reads[0].records == []
        assert reads[0].mode == daemon_protocol.HISTORY_MODE_APPEND

        # A new step finishes -> a new per-step jsonl with its own card line.
        new_jsonl = project_root / "se3" / "history" / "F2" / "03_plan_def.jsonl"
        new_jsonl.write_text(
            json.dumps(_step_completed_line("03_plan_def", "plan", {"task_groups": []}))
            + "\n",
            encoding="utf-8",
        )
        reads = reader.read_active_flows(cursors)
        cursors[reads[0].flow_id] = reads[0].cursor
        events = _step_events(reads[0])
        assert len(events) == 1
        assert events[0]["step_id"] == "03_plan_def"
        assert _frontend_outputs(events[0]) == {"task_groups": []}

        # And it is not re-pushed on the subsequent poll.
        reads = reader.read_active_flows(cursors)
        assert reads[0].records == []


def test_step_completed_appended_to_existing_step_file_is_picked_up():
    """The real "step finished" path: ``HistorySink`` appends the
    ``step_completed`` line to the SAME jsonl that already holds the step's
    chat turns. The next incremental poll must surface exactly that line."""
    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        jsonl = _seed_active_flow(
            project_root,
            "F3",
            "01_analyze_abc",
            lines=[
                {
                    "role": "assistant",
                    "content": "r",
                    "step_type": "analyze",
                    "timestamp": "2026-05-21T00:00:00",
                },
            ],
        )
        reader = DaemonHistoryReader(lambda: [str(project_root)])
        cursors: dict = {}

        reads = reader.read_active_flows(cursors)
        cursors[reads[0].flow_id] = reads[0].cursor
        assert _step_events(reads[0]) == []  # no card yet

        # Step finishes: append the terminal-event line to the same file.
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    _step_completed_line("01_analyze_abc", "analyze", {"reasoning": "done"})
                )
                + "\n"
            )

        reads = reader.read_active_flows(cursors)
        cursors[reads[0].flow_id] = reads[0].cursor
        events = _step_events(reads[0])
        assert len(events) == 1
        assert _frontend_outputs(events[0]) == {"reasoning": "done"}

        # Not re-pushed afterwards.
        reads = reader.read_active_flows(cursors)
        assert reads[0].records == []
