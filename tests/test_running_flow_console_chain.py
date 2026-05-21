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
