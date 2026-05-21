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


# ---------------------------------------------------------------------------
# Group G3: discovery confirmation entry point (prompt + GUI button).
#
# A non-interactive discovery pause at the programmatic confirmation gate must
# write a structured call file carrying:
#   * kind == discovery_confirm (consistent across engine/daemon/server),
#   * a one-click confirm option whose value is the literal "1" the gate's
#     ``== "1"`` check expects,
#   * context.flow_id so the daemon's per-flow filter scopes it,
#   * a prompt with the "输入 1 确认" textual fallback + refined description.
# Submitting the confirm action ("1") must drive the gate to continue.
# ---------------------------------------------------------------------------


def _make_discovery_step(step_id="01_discovery_xyz", outputs=None, inputs=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        step_id=step_id,
        outputs=dict(outputs or {}),
        inputs=dict(inputs or {}),
    )


def _make_discovery_flow(flow_id="F-disc"):
    from types import SimpleNamespace

    return SimpleNamespace(flow_id=flow_id)


class _NullPersistence:
    def save_flow(self, flow):  # noqa: D401 - test stub
        pass


def test_discovery_confirm_call_payload_kind_options_context():
    """The confirm call carries kind/options/context.flow_id and the textual
    "输入 1 确认" hint + refined description in its prompt."""
    from se3.commands.run import _write_discovery_call
    from se3.engine.interaction_calls import CALL_KIND_DISCOVERY_CONFIRM

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        flow = _make_discovery_flow("F-disc")
        step = _make_discovery_step(
            outputs={
                "awaiting_programmatic_confirm": True,
                "refined_description": "Add a /health endpoint",
            }
        )
        call_file = _write_discovery_call(flow, step, project_root)
        data = json.loads(call_file.read_text(encoding="utf-8"))

        assert data["kind"] == CALL_KIND_DISCOVERY_CONFIRM
        # context.flow_id is what the aggregator's per-flow filter inspects.
        assert data["context"]["flow_id"] == "F-disc"
        assert data["context"]["step_id"] == step.step_id
        assert data["context"]["refined_description"] == "Add a /health endpoint"
        # The textual fallback + refined description live in the prompt.
        assert "输入 1 确认" in data["prompt"]
        assert "Add a /health endpoint" in data["prompt"]
        # Exactly one confirm option, whose value is the gate's literal "1".
        assert len(data["options"]) == 1
        assert data["options"][0]["value"] == "1"


def test_discovery_confirm_call_surfaces_via_aggregator_scoped_with_options():
    """The daemon aggregator parses the confirm call into a flow-scoped
    PendingCall that keeps the kind and the confirm option."""
    from se3.commands.run import _write_discovery_call
    from se3.daemon.aggregator import DaemonAggregator
    from se3.engine.interaction_calls import CALL_KIND_DISCOVERY_CONFIRM

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        # engine.json so the snapshot has a flow_id to scope against.
        state_dir = project_root / "se3" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "engine.json").write_text(
            json.dumps(
                {
                    "flow_id": "F-disc",
                    "task_description": "t",
                    "task_type": "discovery",
                    "status": "PAUSED",
                    "state": {
                        "current_step_id": "01_discovery_xyz",
                        "selected_steps": ["discovery"],
                        "current_step_index": 0,
                        "steps": {"01_discovery_xyz": {"step_type": "discovery"}},
                    },
                }
            ),
            encoding="utf-8",
        )
        flow = _make_discovery_flow("F-disc")
        step = _make_discovery_step(
            outputs={
                "awaiting_programmatic_confirm": True,
                "refined_description": "Refine the thing",
            }
        )
        _write_discovery_call(flow, step, project_root)

        aggregator = DaemonAggregator()
        aggregator.add_project_root(project_root)
        snapshot = aggregator._snapshot_for_root(project_root)

        assert snapshot is not None
        assert snapshot.flow_id == "F-disc"
        # The confirm call survives the per-flow filter and keeps its metadata.
        assert len(snapshot.pending_calls) == 1
        call = snapshot.pending_calls[0]
        assert call.kind == CALL_KIND_DISCOVERY_CONFIRM
        assert call.context.get("flow_id") == "F-disc"
        assert call.options and call.options[0]["value"] == "1"
        assert "输入 1 确认" in call.prompt


def test_discovery_confirm_submission_gates_on_one():
    """Submitting the confirm action ("1") drives the gate to continue
    (programmatic_confirmed + sentinel); any other reply keeps refining."""
    from se3.commands.run import (
        _PROGRAMMATIC_CONFIRM,
        _handle_discovery_pause_noninteractive,
    )

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        persistence = _NullPersistence()

        # --- "1" confirms and advances ---
        flow = _make_discovery_flow("F-disc")
        step = _make_discovery_step(
            outputs={
                "awaiting_programmatic_confirm": True,
                "refined_description": "Do X",
            }
        )
        _handle_discovery_pause_noninteractive(flow, step, persistence, project_root)
        call_file = Path(step.outputs["discovery_call_file"])
        (call_file.parent / f"{call_file.stem}.response.json").write_text(
            json.dumps({"response": "1"}), encoding="utf-8"
        )
        result = _handle_discovery_pause_noninteractive(
            flow, step, persistence, project_root
        )
        assert result is _PROGRAMMATIC_CONFIRM
        assert step.inputs.get("programmatic_confirmed") is True

        # --- any other reply keeps refining (clears the confirm flag) ---
        flow2 = _make_discovery_flow("F-disc")
        step2 = _make_discovery_step(
            outputs={
                "awaiting_programmatic_confirm": True,
                "refined_description": "Do X",
            }
        )
        _handle_discovery_pause_noninteractive(
            flow2, step2, persistence, project_root
        )
        call_file2 = Path(step2.outputs["discovery_call_file"])
        (call_file2.parent / f"{call_file2.stem}.response.json").write_text(
            json.dumps({"response": "also handle Y"}), encoding="utf-8"
        )
        result2 = _handle_discovery_pause_noninteractive(
            flow2, step2, persistence, project_root
        )
        assert result2 == "also handle Y"
        assert step2.inputs.get("programmatic_confirmed") is None
        assert "awaiting_programmatic_confirm" not in step2.outputs


# ---------------------------------------------------------------------------
# Group G5: summarize landing + incremental push (standard 5).
#
# These pin the summarize-specific leg of the chain:
#
# * Task 1 — the summarize step's USER prompt AND ASSISTANT markdown result
#   must land in se3/history/{flow_id}/{step_id}.jsonl via the *real* LLMCaller
#   record_prompt/record_response path. ``summarize_handler`` calls
#   ``caller.call(json_mode="off")`` with ``flow_id`` / ``step_id`` /
#   ``step_type`` wired through, so the recording is not bypassed and the IDs
#   are set. We drive the handler with a mocked agent runner so the actual
#   chat_history write path executes end-to-end.
# * Task 2 — those records, together with the ``step_completed`` card carrying
#   ``outputs.summary``, must be readable by the daemon's incremental cursor in
#   the exact shape the frontend's ``renderSummarizeReport`` consumes
#   (``message.data.step.outputs.summary``), so the web console shows
#   ``user + assistant + Work Summary report card``.
# ---------------------------------------------------------------------------


def _ndjson_assistant(text: str) -> str:
    """A minimal stream-json transcript carrying one assistant text block.

    Mirrors what Claude CLI streams: an ``assistant`` message with a text
    content block, followed by a terminal ``result`` line. Both
    ``LLMCaller._extract_text_from_ndjson`` (the value returned to the
    handler) and ``chat_history.extract_assistant_text`` (the recorded
    assistant content) pull the text out of the assistant block.
    """
    return "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": text}]},
                }
            ),
            json.dumps({"type": "result", "result": text}),
        ]
    )


def test_summarize_records_user_and_assistant_to_jsonl():
    """Task 1: summarize_handler lands BOTH the user prompt and the assistant
    markdown summary in the per-step jsonl through the real LLMCaller path."""
    from se3.engine.steps.summarize import summarize_handler
    from se3.engine.chat_history import get_step_history

    summary_md = "## Work Summary\n\n- Did the thing\n- Tests pass"
    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        flow = FlowInstance(
            flow_id="sum-flow-1",
            task_description="summarize task",
            task_type="feature",
            status=FlowStatus.RUNNING,
        )
        # change_path.parent resolves to project_root so history lands under it.
        flow.change_path = project_root / "dummy"
        step = Step(
            step_type=StepType.SUMMARIZE,
            status=StepStatus.PENDING,
            step_id="12_summarize_abc12345",
            inputs={"task_description": "summarize task"},
            outputs={},
        )

        with patch("se3.engine.llm_caller.ClaudeCodeRunner") as MockRunner:
            mock_runner = MagicMock()
            mock_runner.run_with_monitor.return_value = MagicMock(
                success=True,
                output=_ndjson_assistant(summary_md),
                returncode=0,
                interrupted=False,
                cmd_used="claude",
            )
            MockRunner.return_value = mock_runner

            result = summarize_handler(step, flow)

        assert result == StepStatus.COMPLETED
        # The handler stores the LLM-produced markdown as the summary output.
        assert step.outputs["summary"] == summary_md

        jsonl = (
            project_root
            / "se3"
            / "history"
            / "sum-flow-1"
            / "12_summarize_abc12345.jsonl"
        )
        assert jsonl.exists(), "summarize did not write the per-step jsonl"

        session = get_step_history(project_root, "sum-flow-1", "12_summarize_abc12345")
        assert session is not None
        roles = [m.role for m in session.messages]
        assert "user" in roles, "summarize user prompt was not persisted"
        assert "assistant" in roles, "summarize assistant result was not persisted"
        # The assistant turn carries the markdown summary the web bubble shows.
        assistant_msgs = [m for m in session.messages if m.role == "assistant"]
        assert any(summary_md in (m.content or "") for m in assistant_msgs)


def test_summarize_records_incrementally_readable_in_frontend_shape():
    """Task 2: the summarize user/assistant turns AND the step_completed card
    (carrying ``outputs.summary``) are surfaced by the daemon incremental reader
    in the shape ``renderSummarizeReport`` consumes."""
    summary_md = "## Work Summary\n\n- shipped"
    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        jsonl = _seed_active_flow(
            project_root,
            "SUMF",
            "12_summarize_abc",
            lines=[
                {
                    "role": "user",
                    "content": "summarize prompt",
                    "step_type": "summarize",
                    "timestamp": "2026-05-21T02:00:00",
                },
                {
                    "role": "assistant",
                    "content": summary_md,
                    "step_type": "summarize",
                    "timestamp": "2026-05-21T02:30:00",
                },
                _step_completed_line(
                    "12_summarize_abc", "summarize", {"summary": summary_md}
                ),
            ],
        )
        reader = DaemonHistoryReader(lambda: [str(project_root)])

        reads = reader.read_active_flows()
        assert len(reads) == 1
        read = reads[0]
        assert read.flow_id == "SUMF"
        # user + assistant + step_completed are all surfaced together.
        assert len(read.records) == 3
        roles = [
            r["message"].get("role")
            for r in read.records
            if r["message"].get("role")
        ]
        assert "user" in roles and "assistant" in roles

        events = _step_events(read)
        assert len(events) == 1
        # The Work Summary report card reads outputs.summary; prove the payload
        # carries it through make_history_data in the frontend-consumable shape.
        assert _frontend_outputs(events[0]) == {"summary": summary_md}

        msg = daemon_protocol.make_history_data(
            read.flow_id, read.mode, read.records, cursor=read.cursor
        )
        pushed_events = [
            r
            for r in msg.payload["records"]
            if r["message"].get("type") == "step_completed"
        ]
        assert len(pushed_events) == 1
        assert _frontend_outputs(pushed_events[0])["summary"] == summary_md
        # Cursor counts all three lines so the next poll is incremental.
        assert msg.payload["cursor"]["12_summarize_abc.jsonl"] == 3


# ---------------------------------------------------------------------------
# Group G2: stale "待回复" chip lifecycle.
#
# Interactive confirm / human calls answered in the CLI terminal never get a
# sibling ``.response`` file (the run loop consumes the answer and advances),
# so the response-file heuristic in ``_enumerate_calls`` cannot clear them and
# the chip would otherwise linger for the entire run. The aggregator now also
# judges a call against the flow's *progress*: a call whose owning step the
# flow has already walked past (no longer ``current_step_id``, or the step
# reached a processed status) is dropped from ``FlowSnapshot.pending_calls``,
# while a call for the step the flow is genuinely waiting on is kept. The
# machine-level ``MachineStatus.pending_calls`` stays unfiltered.
# ---------------------------------------------------------------------------


def _seed_engine_with_steps(
    project_root: Path,
    *,
    flow_id: str,
    current_step_id: str,
    steps: dict,
    status: str = "RUNNING",
) -> None:
    """Write an engine.json whose ``state`` carries a steps map + current step."""
    state_dir = project_root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "engine.json").write_text(
        json.dumps(
            {
                "flow_id": flow_id,
                "task_description": "t",
                "task_type": "feature",
                "status": status,
                "state": {
                    "current_step_id": current_step_id,
                    "selected_steps": [
                        v.get("step_type", "") for v in steps.values()
                    ],
                    "current_step_index": 0,
                    "steps": steps,
                },
            }
        ),
        encoding="utf-8",
    )


def _write_flow_call(
    project_root: Path,
    *,
    call_id: str,
    flow_id: str,
    step_id: str,
    kind: str = "call",
    prompt: str = "Need a human?",
) -> Path:
    """Write a flow-scoped call file (kind-tagged, with context.flow_id/step_id)."""
    calls_dir = project_root / "se3" / "calls"
    calls_dir.mkdir(parents=True, exist_ok=True)
    path = calls_dir / f"{call_id}.json"
    path.write_text(
        json.dumps(
            {
                "call_id": call_id,
                "kind": kind,
                "prompt": prompt,
                "context": {"flow_id": flow_id, "step_id": step_id},
                "step_id": step_id,
                "options": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_stale_chip_dropped_after_flow_walks_past_its_step():
    """A confirm call whose step is no longer current is dropped from the
    flow snapshot, even though no .response sibling was ever written."""
    from se3.daemon.aggregator import DaemonAggregator

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        _seed_engine_with_steps(
            project_root,
            flow_id="F1",
            current_step_id="02_plan_x",
            steps={
                "01_discovery_x": {"step_type": "discovery", "status": "completed"},
                "02_plan_x": {"step_type": "plan", "status": "running"},
            },
        )
        # An unanswered confirm call left over from the (now-finished) discovery
        # step — exactly the file that used to keep a stale chip showing.
        _write_flow_call(
            project_root,
            call_id="discovery_01_discovery_x_001",
            flow_id="F1",
            step_id="01_discovery_x",
            kind="discovery_confirm",
        )

        aggregator = DaemonAggregator()
        aggregator.add_project_root(project_root)
        snapshot = aggregator._snapshot_for_root(project_root)

        assert snapshot is not None
        assert snapshot.flow_id == "F1"
        # The flow has moved on to plan -> the discovery chip is cleared.
        assert snapshot.pending_calls == []


def test_real_pending_chip_for_current_step_is_kept():
    """A call for the step the flow is genuinely waiting on survives the
    progress filter."""
    from se3.daemon.aggregator import DaemonAggregator

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        _seed_engine_with_steps(
            project_root,
            flow_id="F1",
            current_step_id="01_discovery_x",
            steps={
                "01_discovery_x": {"step_type": "discovery", "status": "paused"},
            },
            status="PAUSED",
        )
        _write_flow_call(
            project_root,
            call_id="discovery_01_discovery_x_002",
            flow_id="F1",
            step_id="01_discovery_x",
            kind="discovery_confirm",
        )

        aggregator = DaemonAggregator()
        aggregator.add_project_root(project_root)
        snapshot = aggregator._snapshot_for_root(project_root)

        assert snapshot is not None
        assert len(snapshot.pending_calls) == 1
        assert snapshot.pending_calls[0].step_id == "01_discovery_x"


def test_chip_dropped_when_current_step_already_processed():
    """Even while still ``current_step_id``, a call whose step reached a
    processed status (completed/partial/failed/revision_needed) is stale."""
    from se3.daemon.aggregator import DaemonAggregator

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        _seed_engine_with_steps(
            project_root,
            flow_id="F1",
            current_step_id="01_discovery_x",
            steps={
                "01_discovery_x": {"step_type": "discovery", "status": "completed"},
            },
        )
        _write_flow_call(
            project_root,
            call_id="discovery_01_discovery_x_003",
            flow_id="F1",
            step_id="01_discovery_x",
            kind="discovery_confirm",
        )

        aggregator = DaemonAggregator()
        aggregator.add_project_root(project_root)
        snapshot = aggregator._snapshot_for_root(project_root)

        assert snapshot is not None
        assert snapshot.pending_calls == []


def test_unresolvable_step_call_is_kept():
    """A call whose step_id is absent from the flow's steps map is kept — the
    progress heuristic must never drop a call it cannot attribute."""
    from se3.daemon.aggregator import DaemonAggregator

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        _seed_engine_with_steps(
            project_root,
            flow_id="F1",
            current_step_id="02_plan_x",
            steps={"02_plan_x": {"step_type": "plan", "status": "running"}},
        )
        _write_flow_call(
            project_root,
            call_id="mcp_call_004",
            flow_id="F1",
            step_id="99_unknown_step",
        )

        aggregator = DaemonAggregator()
        aggregator.add_project_root(project_root)
        snapshot = aggregator._snapshot_for_root(project_root)

        assert snapshot is not None
        assert len(snapshot.pending_calls) == 1
        assert snapshot.pending_calls[0].call_id == "mcp_call_004"


def test_machine_level_pending_calls_unaffected_by_progress_filter():
    """The stale-call progress filter is flow-scoped only — the machine-wide
    ``MachineStatus.pending_calls`` still enumerates the call unfiltered."""
    from se3.daemon.aggregator import DaemonAggregator

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        _seed_engine_with_steps(
            project_root,
            flow_id="F1",
            current_step_id="02_plan_x",
            steps={
                "01_discovery_x": {"step_type": "discovery", "status": "completed"},
                "02_plan_x": {"step_type": "plan", "status": "running"},
            },
        )
        _write_flow_call(
            project_root,
            call_id="discovery_01_discovery_x_005",
            flow_id="F1",
            step_id="01_discovery_x",
            kind="discovery_confirm",
        )

        aggregator = DaemonAggregator()
        aggregator.add_project_root(project_root)
        status = aggregator.get_snapshot()

        # Flow-scoped view drops the stale chip ...
        assert len(status.flows) == 1
        assert status.flows[0].pending_calls == []
        # ... but the machine-level view keeps every queued call file.
        assert [c.call_id for c in status.pending_calls] == [
            "discovery_01_discovery_x_005"
        ]
