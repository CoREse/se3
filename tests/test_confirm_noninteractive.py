"""Tests for non-interactive (daemon-spawned) CONFIRM pause handling.

When a flow runs under a daemon (``se3 run --output-format json``, no terminal)
and reaches a human CONFIRM gate (plan approval, adjudicate ruling, …), the run
loop cannot block on a terminal prompt. The confirm handler has already written
the ``confirm_*.json`` call file; the only remaining job is to persist
``FlowStatus.PAUSED`` to the engine.json top-level status so the daemon's
``_resume_paused_flow`` (which only re-spawns a flow whose on-disk status ==
"PAUSED") can pick it back up once the web answer lands.

Before this fix the CONFIRM path had no json branch and fell through to the
interactive prompt, which returns ``None`` under a non-TTY and exited 130 with
the top-level status still "running" — so the daemon never re-spawned and the
flow wedged ("approved but nothing happens"). These tests pin the fixed
behavior and guard the interactive path from regressing.
"""

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tianluo.commands.run import (
    _CONFIRM_AWAITING,
    _handle_confirm_pause,
    _handle_confirm_pause_noninteractive,
    _run_flow_impl,
)
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.persistence import PersistenceManager


def _make_step(step_id="confirm-001", outputs=None, inputs=None):
    return SimpleNamespace(
        step_id=step_id,
        outputs=dict(outputs or {}),
        inputs=dict(inputs or {}),
    )


def _make_flow(flow_id="flow-xyz", status=FlowStatus.RUNNING):
    return SimpleNamespace(flow_id=flow_id, status=status)


class _RecordingPersistence:
    def __init__(self):
        self.saved = 0

    def save_flow(self, flow):
        self.saved += 1


# --- Unit tests: the pure helper ---------------------------------------------


def test_helper_with_call_file_pauses_and_saves(tmp_path):
    call_file = tmp_path / "confirm_x.json"
    call_file.write_text("{}")
    flow = _make_flow()
    step = _make_step(outputs={"call_file": str(call_file)})
    persistence = _RecordingPersistence()

    result = _handle_confirm_pause_noninteractive(flow, step, persistence, tmp_path)

    assert result is _CONFIRM_AWAITING
    assert flow.status is FlowStatus.PAUSED
    assert persistence.saved == 1


def test_helper_missing_call_file_still_pauses_failsafe(tmp_path):
    # Fail-safe: even without the call file on disk the helper must persist
    # PAUSED (never leave a non-PAUSED, un-resumable flow) — it just warns.
    flow = _make_flow()
    step = _make_step(outputs={})  # no call_file at all
    persistence = _RecordingPersistence()

    with patch("tianluo.commands.run.logger") as mock_logger:
        result = _handle_confirm_pause_noninteractive(flow, step, persistence, tmp_path)
        assert mock_logger.warning.called

    assert result is _CONFIRM_AWAITING
    assert flow.status is FlowStatus.PAUSED
    assert persistence.saved == 1


def test_helper_call_file_path_but_not_on_disk_warns(tmp_path):
    # call_file recorded in outputs but the file itself is gone → fail-safe warn.
    flow = _make_flow()
    step = _make_step(outputs={"call_file": str(tmp_path / "gone.json")})
    persistence = _RecordingPersistence()

    with patch("tianluo.commands.run.logger") as mock_logger:
        _handle_confirm_pause_noninteractive(flow, step, persistence, tmp_path)
        assert mock_logger.warning.called

    assert flow.status is FlowStatus.PAUSED


# --- Integration: engine.json top-level status contract ----------------------


def _make_real_flow(project_root):
    flow = FlowInstance(
        task_description="Test task",
        task_type="feature",
        change_name="test-change",
        change_path=project_root,
    )
    flow.state.selected_steps = [StepType.PLAN, StepType.CONFIRM, StepType.IMPLEMENT]
    plan_step = Step(step_type=StepType.PLAN, status=StepStatus.COMPLETED, step_id="plan-001")
    flow.state.add_step(plan_step)
    confirm_step = Step(
        step_type=StepType.CONFIRM,
        status=StepStatus.PAUSED,
        step_id="confirm-001",
        inputs={"step_to_review_id": "plan-001", "step_to_review_type": "plan"},
    )
    flow.state.add_step(confirm_step)
    flow.state.current_step_id = "confirm-001"
    flow.status = FlowStatus.RUNNING
    return flow, confirm_step


def test_helper_persists_engine_json_top_level_status_paused():
    """The daemon keys resume off engine.json top-level status — assert it lands PAUSED."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)
        calls_dir = project_root / "tianluo" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)
        (project_root / "tianluo" / "state").mkdir(parents=True, exist_ok=True)

        flow, confirm_step = _make_real_flow(project_root)
        # Mimic the confirm handler having written the call file.
        call_file = calls_dir / "confirm_confirm-001_x.json"
        call_file.write_text(json.dumps({"step": "confirm-001"}))
        confirm_step.outputs["call_file"] = str(call_file)

        persistence = PersistenceManager(project_root)

        result = _handle_confirm_pause_noninteractive(
            flow, confirm_step, persistence, project_root
        )

        assert result is _CONFIRM_AWAITING
        assert flow.status is FlowStatus.PAUSED

        engine_json = project_root / "tianluo" / "state" / "engine.json"
        assert engine_json.exists()
        data = json.loads(engine_json.read_text())
        # find_existing_flows / the daemon read the top-level "status" — it must
        # be PAUSED so _resume_paused_flow re-spawns after the web answer lands.
        assert str(data.get("status")).upper() == "PAUSED"


# --- Guardrail: interactive path unchanged (user-exit → None → run loop 130) --


def test_interactive_confirm_pause_user_exit_returns_none():
    """output_format != 'json' still routes through _handle_confirm_pause.

    Selecting "Exit (pause flow)" returns None, which the run loop turns into a
    return-130 user-initiated exit — the semantics this fix must NOT change.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)
        (project_root / "tianluo" / "calls").mkdir(parents=True, exist_ok=True)
        (project_root / "tianluo" / "state").mkdir(parents=True, exist_ok=True)

        flow, confirm_step = _make_real_flow(project_root)
        persistence = PersistenceManager(project_root)

        # choice index 2 == "Exit (pause flow)"
        with patch("tianluo.commands.run.prompt_user_choice", return_value=2), \
                patch("tianluo.commands.run.render_full"):
            result = _handle_confirm_pause(
                flow, confirm_step, persistence, project_root, None
            )

        assert result is None


def test_interactive_confirm_pause_approve_writes_response():
    """Guardrail: interactive approve still writes a plain .response sibling."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)
        calls_dir = project_root / "tianluo" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)
        (project_root / "tianluo" / "state").mkdir(parents=True, exist_ok=True)

        flow, confirm_step = _make_real_flow(project_root)
        call_file = calls_dir / "confirm_confirm-001_x.json"
        call_file.write_text(json.dumps({"step": "confirm-001"}))
        confirm_step.outputs["call_file"] = str(call_file)
        persistence = PersistenceManager(project_root)

        # choice index 0 == "Approve and continue"
        with patch("tianluo.commands.run.prompt_user_choice", return_value=0), \
                patch("tianluo.commands.run.render_full"):
            result = _handle_confirm_pause(
                flow, confirm_step, persistence, project_root, None
            )

        assert result is True
        response_file = call_file.parent / f"{call_file.stem}.response"
        assert response_file.exists()
        data = json.loads(response_file.read_text())
        assert data["approved"] is True


# --- Run-loop dispatch: the wiring between the loop branch and the helper -----
#
# The helper-level tests above prove _handle_confirm_pause_noninteractive works
# in isolation, but they bypass the CONFIRM PAUSED dispatch in _run_flow_impl.
# These tests drive that branch through _run_flow_impl so the wiring itself —
# json → helper + FLOW_PAUSED emit + return 0; non-json user-exit → return 130 —
# is guarded against a future edit silently dropping the emit or flipping the
# return code.


class _FakeStateMachine:
    """Minimal StateMachine stand-in: run_step reports the given result.

    Using a fake avoids driving a real state machine (which would run a git
    baseline commit and an actual LLM step) — we only need _run_flow_impl to
    reach the CONFIRM PAUSED dispatch with a controllable ``run_step`` result.
    """

    def __init__(self, step_result):
        self._step_result = step_result
        self.run_step_calls = 0

    def register_handler(self, step_type, handler):
        pass

    def init_flow(self, flow):
        pass

    def run_step(self, flow, current_step, on_running=None):
        self.run_step_calls += 1
        return self._step_result

    def transition_to_next(self, flow):
        pass


def _prepare_persisted_confirm_flow(project_root):
    """Persist a flow parked at a PAUSED CONFIRM step, call file on disk."""
    calls_dir = project_root / "tianluo" / "calls"
    calls_dir.mkdir(parents=True, exist_ok=True)
    (project_root / "tianluo" / "state").mkdir(parents=True, exist_ok=True)

    flow, confirm_step = _make_real_flow(project_root)
    call_file = calls_dir / "confirm_confirm-001_x.json"
    call_file.write_text(json.dumps({"step": "confirm-001"}))
    confirm_step.outputs["call_file"] = str(call_file)

    persistence = PersistenceManager(project_root)
    persistence.save_flow(flow)
    return flow, persistence


def test_run_loop_json_confirm_pause_returns_0_and_emits_flow_paused(capsys):
    """json mode: the CONFIRM PAUSED branch routes to the helper, emits
    FLOW_PAUSED, and exits 0 with engine.json top-level status == PAUSED."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)
        flow, persistence = _prepare_persisted_confirm_flow(project_root)
        fake_sm = _FakeStateMachine(StepStatus.PAUSED)

        rc = _run_flow_impl(
            project_root=project_root,
            flow_id=flow.flow_id,
            task_description=None,
            task_type="feature",
            change_name=None,
            is_worktree_mode=False,
            persistence=persistence,
            state_machine=fake_sm,
            output_format="json",
            main_lock=None,
        )

        assert rc == 0
        assert fake_sm.run_step_calls == 1

        # JsonSink writes NDJSON to stdout — the FLOW_PAUSED event must be there.
        out = capsys.readouterr().out
        assert '"flow_paused"' in out

        # engine.json top-level status must land PAUSED so the daemon re-spawns.
        engine_json = project_root / "tianluo" / "state" / "engine.json"
        data = json.loads(engine_json.read_text())
        assert str(data.get("status")).upper() == "PAUSED"


def test_run_loop_non_json_confirm_pause_user_exit_returns_130():
    """Guardrail: non-json mode still routes through _handle_confirm_pause; a
    user-exit (None) is turned into a return-130 by the run loop, unchanged."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir)
        flow, persistence = _prepare_persisted_confirm_flow(project_root)
        fake_sm = _FakeStateMachine(StepStatus.PAUSED)

        # Interactive path returns None ("Exit (pause flow)") — the run loop must
        # NOT call the noninteractive helper here and must return 130.
        with patch(
            "tianluo.commands.run._handle_confirm_pause", return_value=None
        ) as mock_interactive, patch(
            "tianluo.commands.run._handle_confirm_pause_noninteractive"
        ) as mock_helper:
            rc = _run_flow_impl(
                project_root=project_root,
                flow_id=flow.flow_id,
                task_description=None,
                task_type="feature",
                change_name=None,
                is_worktree_mode=False,
                persistence=persistence,
                state_machine=fake_sm,
                output_format="cli",
                main_lock=None,
            )

        assert rc == 130
        assert mock_interactive.called
        assert not mock_helper.called
