"""Tests for the ``step_started`` event-chain (group G1).

The running-flow / history web console needs each step's region to appear the
moment the step enters RUNNING — including the non-LLM TEST / COMMIT /
SPEC_GATE steps that emit no conversation records. This is achieved by
persisting the orchestrator's ``EventType.STEP_STARTED`` as a lightweight
``{type: 'step_started', status: 'running', ...}`` anchor line in the per-step
jsonl, riding the existing ``history_data`` push channel.

Covered here:

* ``record_step_started`` writes a well-shaped, serializable jsonl line.
* ``has_step_started_event`` detects (and only detects) that line.
* ``HistorySink`` persists STEP_STARTED, keeps the write idempotent across a
  re-emitted / resumed step, and skips it once a terminal event exists.
* ``get_step_history`` skips the ``step_started`` line (it is not a chat
  message), so CLI history and retry-context construction never ingest it.
* The full ``run_flow`` chain emits & persists ``step_started`` for a non-LLM
  step type (TEST), and resuming the same step does not duplicate the region.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)


# ---------------------------------------------------------------------------
# Task 1: record_step_started / has_step_started_event / get_step_history skip
# ---------------------------------------------------------------------------


def _step_path(project_root: Path, flow_id: str, step_id: str) -> Path:
    return project_root / "se3" / "history" / flow_id / f"{step_id}.jsonl"


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_record_step_started_writes_running_record():
    from se3.engine.chat_history import record_step_started

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        record_step_started(
            project_root, "flow-1", "01_test_abc", "test", timestamp=1_700_000_000.0
        )
        records = _read_lines(_step_path(project_root, "flow-1", "01_test_abc"))
        assert len(records) == 1
        rec = records[0]
        assert rec["type"] == "step_started"
        assert rec["status"] == "running"
        assert rec["step_id"] == "01_test_abc"
        assert rec["step_type"] == "test"
        # Timestamp is an ISO string derived from the epoch float.
        assert isinstance(rec["timestamp"], str) and rec["timestamp"]
        # It is NOT a chat message — no role field leaks in.
        assert "role" not in rec


def test_record_step_started_default_timestamp_is_iso_string():
    from se3.engine.chat_history import record_step_started

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        record_step_started(project_root, "flow-1", "01_commit_x", "commit")
        rec = _read_lines(_step_path(project_root, "flow-1", "01_commit_x"))[0]
        assert isinstance(rec["timestamp"], str) and rec["timestamp"]


def test_has_step_started_event_detection():
    from se3.engine.chat_history import (
        has_step_started_event,
        record_step_started,
    )

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        # Absent file → False.
        assert not has_step_started_event(project_root, "flow-1", "01_test_abc")
        record_step_started(project_root, "flow-1", "01_test_abc", "test")
        assert has_step_started_event(project_root, "flow-1", "01_test_abc")
        # A different step is unaffected.
        assert not has_step_started_event(project_root, "flow-1", "02_commit_y")


def test_get_step_history_skips_step_started():
    """A jsonl containing only a step_started line yields no chat session, and a
    step_started line interleaved with real messages is silently skipped."""
    from se3.engine.chat_history import (
        get_step_history,
        record_prompt,
        record_step_started,
    )

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        # step_started-only file → no chat messages → None session.
        record_step_started(project_root, "flow-1", "01_test_abc", "test")
        assert get_step_history(project_root, "flow-1", "01_test_abc") is None

        # step_started followed by a real user prompt → only the prompt surfaces.
        record_step_started(project_root, "flow-1", "02_analyze_z", "analyze")
        record_prompt(
            project_root, "flow-1", "02_analyze_z", "analyze", "hello", attempt=0
        )
        session = get_step_history(project_root, "flow-1", "02_analyze_z")
        assert session is not None
        assert len(session.messages) == 1
        assert session.messages[0].role == "user"
        assert session.messages[0].content == "hello"


# ---------------------------------------------------------------------------
# Task 2: HistorySink persists STEP_STARTED, is idempotent, and STEP_COMPLETED/
# STEP_FAILED/STEP_OUTPUT behavior is unchanged.
# ---------------------------------------------------------------------------


def _make_event(event_type, *, step=None, step_id="01_test_abc", step_type="test"):
    from se3.engine import new_event

    kwargs = dict(flow_id="flow-1", step_id=step_id, step_type=step_type)
    if step is not None:
        kwargs["step"] = step
    return new_event(event_type, **kwargs)


def test_history_sink_persists_step_started():
    from se3.engine import EventType, HistorySink

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        HistorySink(project_root).consume(_make_event(EventType.STEP_STARTED))
        records = _read_lines(_step_path(project_root, "flow-1", "01_test_abc"))
        assert len(records) == 1
        assert records[0]["type"] == "step_started"
        assert records[0]["status"] == "running"
        assert records[0]["step_type"] == "test"


def test_history_sink_step_started_is_idempotent():
    """A re-emitted / resumed STEP_STARTED for the same step_id must not append
    a second started record (no duplicate 'running' region)."""
    from se3.engine import EventType, HistorySink

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        sink = HistorySink(project_root)
        sink.consume(_make_event(EventType.STEP_STARTED))
        sink.consume(_make_event(EventType.STEP_STARTED))
        sink.consume(_make_event(EventType.STEP_STARTED))
        records = _read_lines(_step_path(project_root, "flow-1", "01_test_abc"))
        started = [r for r in records if r.get("type") == "step_started"]
        assert len(started) == 1


def test_history_sink_step_started_skipped_after_terminal():
    """Once a terminal event exists for the step, a late STEP_STARTED is a
    no-op — a finished step must never gain a 'running' anchor."""
    from se3.engine import EventType, HistorySink

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        sink = HistorySink(project_root)

        step = Step(step_id="01_test_abc", step_type=StepType.TEST)
        step.status = StepStatus.COMPLETED
        step.outputs = {"test_results": {"overall_passed": True}}
        sink.consume(_make_event(EventType.STEP_COMPLETED, step=step))

        # A stray STEP_STARTED arriving after completion is ignored.
        sink.consume(_make_event(EventType.STEP_STARTED))
        records = _read_lines(_step_path(project_root, "flow-1", "01_test_abc"))
        assert not [r for r in records if r.get("type") == "step_started"]
        assert [r for r in records if r.get("type") == "step_completed"]


def test_history_sink_terminal_events_unchanged():
    """STEP_COMPLETED / STEP_FAILED / STEP_OUTPUT keep persisting their full
    step record exactly as before (regression guard)."""
    from se3.engine import EventType, HistorySink

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        sink = HistorySink(project_root)
        step = Step(step_id="01_analyze_z", step_type=StepType.ANALYZE)
        step.status = StepStatus.COMPLETED
        step.outputs = {"reasoning": "because"}
        sink.consume(
            _make_event(
                EventType.STEP_COMPLETED,
                step=step,
                step_id="01_analyze_z",
                step_type="analyze",
            )
        )
        records = _read_lines(_step_path(project_root, "flow-1", "01_analyze_z"))
        assert len(records) == 1
        rec = records[0]
        assert rec["type"] == "step_completed"
        assert rec["data"]["step"]["outputs"] == {"reasoning": "because"}


# ---------------------------------------------------------------------------
# Task 3: full run_flow chain emits + persists step_started for a non-LLM step,
# and resume does not duplicate it.
# ---------------------------------------------------------------------------


def _build_flow(step_type: StepType) -> FlowInstance:
    flow = FlowInstance(
        flow_id="started-flow-001",
        task_description="step-started chain task",
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
        outputs={},
    )
    flow.state.add_step(step)
    flow.state.current_step_id = step.step_id
    return flow


def _run_single_step(project_root: Path, flow: FlowInstance, run_result: StepStatus):
    from se3.commands.run import run_flow

    (project_root / "se3" / "state").mkdir(parents=True, exist_ok=True)

    with patch("se3.commands.run.PersistenceManager") as mock_pm_class, patch(
        "se3.commands.run.StateMachine"
    ) as mock_sm_class, patch("se3.commands.run.STEP_HANDLERS", {}), patch(
        "se3.commands.run.render_full"
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
            flow_id="started-flow-001",
            output_format="cli",
        )


def test_run_flow_emits_step_started_for_non_llm_step():
    """A TEST step (non-LLM, produces no conversation) gets a step_started
    anchor persisted at RUNNING entry, alongside its terminal step_completed."""
    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        flow = _build_flow(StepType.TEST)
        exit_code = _run_single_step(project_root, flow, StepStatus.COMPLETED)
        assert exit_code == 0

        path = _step_path(project_root, flow.flow_id, flow.state.current_step_id)
        records = _read_lines(path)
        started = [r for r in records if r.get("type") == "step_started"]
        assert len(started) == 1
        assert started[0]["status"] == "running"
        assert started[0]["step_type"] == "test"
        # The terminal completion still lands too.
        assert [r for r in records if r.get("type") == "step_completed"]


def test_run_flow_resume_does_not_duplicate_step_started():
    """Running the same step twice (the original run plus a resume re-entry)
    must leave exactly one step_started region for that step_id."""
    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)

        # First pass: PAUSED so the step is not terminal and may be re-entered.
        flow = _build_flow(StepType.COMMIT)
        _run_single_step(project_root, flow, StepStatus.PAUSED)

        # Second pass: same flow/step re-entered. HistorySink dedups the
        # step_started by step_id (no terminal event was persisted yet).
        flow2 = _build_flow(StepType.COMMIT)
        _run_single_step(project_root, flow2, StepStatus.COMPLETED)

        path = _step_path(project_root, flow.flow_id, flow.state.current_step_id)
        records = _read_lines(path)
        started = [r for r in records if r.get("type") == "step_started"]
        assert len(started) == 1
