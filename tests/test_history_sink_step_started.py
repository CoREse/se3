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
* ``HistorySink`` persists STEP_STARTED, suppresses a duplicate running anchor
  while the step is already running, RE-ARMS a running anchor when a paused
  step resumes (so the region switches back from 已暂停 to 进行中), and skips it
  once a terminal event exists.
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

from tianluo.engine.models import (
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
    from tianluo.engine.chat_history import record_step_started

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
    from tianluo.engine.chat_history import record_step_started

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        record_step_started(project_root, "flow-1", "01_commit_x", "commit")
        rec = _read_lines(_step_path(project_root, "flow-1", "01_commit_x"))[0]
        assert isinstance(rec["timestamp"], str) and rec["timestamp"]


def test_has_step_started_event_detection():
    from tianluo.engine.chat_history import (
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
    from tianluo.engine.chat_history import (
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
    from tianluo.engine import new_event

    kwargs = dict(flow_id="flow-1", step_id=step_id, step_type=step_type)
    if step is not None:
        kwargs["step"] = step
    return new_event(event_type, **kwargs)


def test_history_sink_persists_step_started():
    from tianluo.engine import EventType, HistorySink

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        HistorySink(project_root).consume(_make_event(EventType.STEP_STARTED))
        records = _read_lines(_step_path(project_root, "flow-1", "01_test_abc"))
        assert len(records) == 1
        assert records[0]["type"] == "step_started"
        assert records[0]["status"] == "running"
        assert records[0]["step_type"] == "test"


def test_history_sink_step_started_is_idempotent_while_running():
    """A re-emitted STEP_STARTED while the step is ALREADY running must not
    append a second started record (no stacked duplicate '进行中' anchor)."""
    from tianluo.engine import EventType, HistorySink

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        sink = HistorySink(project_root)
        sink.consume(_make_event(EventType.STEP_STARTED))
        sink.consume(_make_event(EventType.STEP_STARTED))
        sink.consume(_make_event(EventType.STEP_STARTED))
        records = _read_lines(_step_path(project_root, "flow-1", "01_test_abc"))
        started = [r for r in records if r.get("type") == "step_started"]
        assert len(started) == 1


def test_history_sink_step_started_rearms_after_pause():
    """A step that paused and then resumes (its last lifecycle anchor is
    'paused') MUST re-arm a fresh 'running' anchor, so the web region switches
    back from 已暂停 to 进行中 instead of staying frozen on the paused state."""
    from tianluo.engine import EventType, HistorySink
    from tianluo.engine.chat_history import (
        last_step_lifecycle_status,
        record_step_status,
    )

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        sink = HistorySink(project_root)

        # Enter RUNNING, then pause.
        sink.consume(_make_event(EventType.STEP_STARTED))
        record_step_status(
            project_root, "flow-1", "01_test_abc", "test", "paused"
        )
        assert last_step_lifecycle_status(
            project_root, "flow-1", "01_test_abc"
        ) == "paused"

        # Resume: STEP_STARTED re-emitted while the last anchor is 'paused' →
        # a fresh running anchor is written.
        sink.consume(_make_event(EventType.STEP_STARTED))
        records = _read_lines(_step_path(project_root, "flow-1", "01_test_abc"))
        started = [r for r in records if r.get("type") == "step_started"]
        assert len(started) == 2, "resume after pause must re-arm a running anchor"
        # The latest lifecycle state is running again.
        assert last_step_lifecycle_status(
            project_root, "flow-1", "01_test_abc"
        ) == "running"

        # A further STEP_STARTED while already running again does NOT stack.
        sink.consume(_make_event(EventType.STEP_STARTED))
        records = _read_lines(_step_path(project_root, "flow-1", "01_test_abc"))
        assert len([r for r in records if r.get("type") == "step_started"]) == 2


def test_history_sink_step_started_skipped_after_terminal():
    """Once a terminal event exists for the step, a late STEP_STARTED is a
    no-op — a finished step must never gain a 'running' anchor."""
    from tianluo.engine import EventType, HistorySink

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
    from tianluo.engine import EventType, HistorySink

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
    from tianluo.commands.run import run_flow

    (project_root / "se3" / "state").mkdir(parents=True, exist_ok=True)

    with patch("tianluo.commands.run.PersistenceManager") as mock_pm_class, patch(
        "tianluo.commands.run.StateMachine"
    ) as mock_sm_class, patch("tianluo.commands.run.STEP_HANDLERS", {}), patch(
        "tianluo.commands.run.render_full"
    ):
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = flow
        mock_pm.load_flow_by_id.return_value = flow
        mock_pm._peek_active_flow_id.return_value = flow.flow_id

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        # The real StateMachine.run_step emits STEP_STARTED via its on_running
        # callback only AFTER the step is marked RUNNING. Mirror that contract so
        # the mock invokes on_running, exercising the orchestrator's
        # callback-driven step_started persistence.
        def _run_step(f, step, on_running=None):
            if on_running is not None:
                on_running(step)
            return run_result

        mock_sm.run_step.side_effect = _run_step
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


def test_run_flow_resume_after_pause_rearms_running_then_completes():
    """Running the same step twice — first PAUSED, then resumed to COMPLETED —
    re-arms a 'running' anchor on resume (so the region shows 进行中 again, not a
    stale 已暂停) and still lands its terminal completion.

    The lifecycle sequence on disk is started(running) → status(paused) →
    started(running) → step_completed: the resume re-arms running, and the
    frontend's removeSupersededStatusRows drops every status anchor once the
    terminal report exists."""
    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)

        # First pass: PAUSED so the step is not terminal and may be re-entered.
        flow = _build_flow(StepType.COMMIT)
        _run_single_step(project_root, flow, StepStatus.PAUSED)

        # Second pass: same flow/step re-entered and completes.
        flow2 = _build_flow(StepType.COMMIT)
        _run_single_step(project_root, flow2, StepStatus.COMPLETED)

        path = _step_path(project_root, flow.flow_id, flow.state.current_step_id)
        records = _read_lines(path)
        started = [r for r in records if r.get("type") == "step_started"]
        paused = [r for r in records if r.get("type") == "step_status"
                  and r.get("status") == "paused"]
        completed = [r for r in records if r.get("type") == "step_completed"]
        # Resume re-arms a second running anchor after the pause.
        assert len(started) == 2, "resume after pause re-arms a running anchor"
        assert len(paused) == 1
        assert len(completed) == 1
        # The re-armed running anchor lands AFTER the paused row (so the latest
        # lifecycle state before completion is running, i.e. 进行中).
        types_in_order = [r.get("type") for r in records]
        assert types_in_order.index("step_status") < \
            max(i for i, t in enumerate(types_in_order) if t == "step_started")


# ---------------------------------------------------------------------------
# Task 4: step_status anchor for non-terminal SETTLED states (PAUSED/RETRYING).
# A paused step must surface a "已暂停" status row rather than staying frozen on
# its "进行中" running anchor (the DISCOVERY-pause symptom).
# ---------------------------------------------------------------------------


def test_record_step_status_writes_status_record():
    from tianluo.engine.chat_history import record_step_status

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        record_step_status(
            project_root, "flow-1", "01_discovery_ab", "discovery", "paused",
            timestamp=1_700_000_000.0,
        )
        rec = _read_lines(_step_path(project_root, "flow-1", "01_discovery_ab"))[0]
        assert rec["type"] == "step_status"
        assert rec["status"] == "paused"
        assert rec["step_type"] == "discovery"
        assert "role" not in rec
        assert isinstance(rec["timestamp"], str) and rec["timestamp"]


def test_has_step_status_event_is_status_specific():
    from tianluo.engine.chat_history import (
        has_step_status_event,
        record_step_status,
    )

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        sid = "01_discovery_ab"
        assert not has_step_status_event(project_root, "flow-1", sid, "paused")
        record_step_status(project_root, "flow-1", sid, "discovery", "paused")
        assert has_step_status_event(project_root, "flow-1", sid, "paused")
        # A different status token is not matched (case-insensitively).
        assert has_step_status_event(project_root, "flow-1", sid, "PAUSED")
        assert not has_step_status_event(project_root, "flow-1", sid, "retrying")


def test_get_step_history_skips_step_status():
    from tianluo.engine.chat_history import (
        get_step_history,
        record_prompt,
        record_step_status,
    )

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        record_step_status(project_root, "flow-1", "01_discovery_ab", "discovery", "paused")
        # step_status-only file → no chat messages.
        assert get_step_history(project_root, "flow-1", "01_discovery_ab") is None
        # Interleaved with a real prompt → only the prompt surfaces.
        record_prompt(project_root, "flow-1", "01_discovery_ab", "discovery", "hi", attempt=0)
        session = get_step_history(project_root, "flow-1", "01_discovery_ab")
        assert session is not None and len(session.messages) == 1
        assert session.messages[0].content == "hi"


def _run_discovery_pause(project_root: Path, flow: FlowInstance):
    """Drive run_flow for a DISCOVERY step that returns PAUSED, with the
    interactive pause handler stubbed to "user exits" so the loop returns
    cleanly (130) right after the orchestrator persists the step_status."""
    from tianluo.commands.run import run_flow

    (project_root / "se3" / "state").mkdir(parents=True, exist_ok=True)

    with patch("tianluo.commands.run.PersistenceManager") as mock_pm_class, patch(
        "tianluo.commands.run.StateMachine"
    ) as mock_sm_class, patch("tianluo.commands.run.STEP_HANDLERS", {}), patch(
        "tianluo.commands.run.render_full"
    ), patch(
        "tianluo.commands.run._handle_discovery_pause", return_value=None
    ):
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = flow
        mock_pm.load_flow_by_id.return_value = flow
        mock_pm._peek_active_flow_id.return_value = flow.flow_id

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        def _run_step(f, step, on_running=None):
            if on_running is not None:
                on_running(step)
            return StepStatus.PAUSED

        mock_sm.run_step.side_effect = _run_step
        return run_flow(
            project_root=project_root,
            flow_id="started-flow-001",
            output_format="cli",
        )


def test_run_flow_persists_step_status_on_pause():
    """A DISCOVERY step that returns PAUSED gets a 'paused' step_status anchor
    persisted alongside its 'running' step_started anchor, so the web region
    shows the real state instead of staying frozen on 进行中."""
    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        flow = _build_flow(StepType.DISCOVERY)
        _run_discovery_pause(project_root, flow)

        path = _step_path(project_root, flow.flow_id, flow.state.current_step_id)
        records = _read_lines(path)
        status_rows = [r for r in records if r.get("type") == "step_status"]
        assert len(status_rows) == 1
        assert status_rows[0]["status"] == "paused"
        assert status_rows[0]["step_type"] == "discovery"
        # The running anchor is still present (the frontend supersedes it).
        assert [r for r in records if r.get("type") == "step_started"]


def test_run_flow_step_status_records_paused_after_running_rearm():
    """A multi-round step (running → paused → running → paused, one step_id)
    records the SECOND 'paused' after the intervening 'running' re-arm, so the
    latest persisted lifecycle status reflects the step's REAL state (paused),
    not a stale 'running'.

    Re-entry re-runs the step, which emits STEP_STARTED → HistorySink re-arms a
    'running' anchor (see test_history_sink_step_started_rearms_after_pause).
    The 'paused' that follows is a genuinely new lifecycle transition, so the
    dedup — which compares against the LATEST lifecycle status, not "this status
    appeared anywhere earlier" — records it. Suppressing it (the old bug) would
    leave 'running' as the latest persisted status while the step is paused,
    making the web region wrongly show 进行中."""
    from tianluo.engine.chat_history import last_step_lifecycle_status

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        flow = _build_flow(StepType.DISCOVERY)
        _run_discovery_pause(project_root, flow)
        # Re-enter the same paused step (same step_id) — a fresh round.
        flow2 = _build_flow(StepType.DISCOVERY)
        _run_discovery_pause(project_root, flow2)

        step_id = flow.state.current_step_id
        path = _step_path(project_root, flow.flow_id, step_id)
        records = _read_lines(path)
        status_rows = [r for r in records if r.get("type") == "step_status"]
        # Each round's 'paused' is recorded once the intervening 'running'
        # re-armed the lifecycle.
        assert len(status_rows) == 2
        assert all(r["status"] == "paused" for r in status_rows)
        # The bug fix: the latest persisted lifecycle status is 'paused'.
        assert last_step_lifecycle_status(
            project_root, flow.flow_id, step_id) == "paused"


def test_run_flow_step_status_dedups_back_to_back_paused():
    """Without an intervening 'running' anchor, a re-recorded 'paused' is
    suppressed — the dedup still prevents stacking two identical status rows
    when the latest lifecycle anchor is ALREADY that status."""
    from tianluo.engine.chat_history import (
        last_step_lifecycle_status,
        record_step_started,
        record_step_status,
    )

    with tempfile.TemporaryDirectory() as td:
        project_root = Path(td)
        flow_id, step_id = "flow-dedup", "01_discovery_dedup"
        record_step_started(project_root, flow_id, step_id, "discovery")
        # First paused: latest is 'running' → recorded.
        if last_step_lifecycle_status(project_root, flow_id, step_id) != "paused":
            record_step_status(
                project_root=project_root, flow_id=flow_id,
                step_id=step_id, step_type="discovery", status="paused")
        # Re-record paused with NO intervening running: latest is already
        # 'paused' → the run.py guard would skip it.
        if last_step_lifecycle_status(project_root, flow_id, step_id) != "paused":
            record_step_status(
                project_root=project_root, flow_id=flow_id,
                step_id=step_id, step_type="discovery", status="paused")

        path = _step_path(project_root, flow_id, step_id)
        status_rows = [
            r for r in _read_lines(path) if r.get("type") == "step_status"]
        assert len(status_rows) == 1
