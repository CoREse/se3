"""A failure is announced ONCE per execution.

A pause-point dialog's ``continue`` means "go back to the Retry/Skip/Abort
wait", so the loop turns again with the step still FAILED and no handler ever
re-run. Replaying its terminal event there appends a second ``step_failed``
record — a duplicate failure card on the web console and the step's tokens
counted twice in the session badge — and re-prints the failure panel on the CLI.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tianluo.commands.run import _run_flow_impl
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.persistence import PersistenceManager


class _FakeStateMachine:
    def __init__(self, result):
        self._result = result
        self.run_step_calls = 0

    def register_handler(self, step_type, handler):
        pass

    def init_flow(self, flow):
        pass

    def run_step(self, flow, current_step, on_running=None):
        self.run_step_calls += 1
        current_step.status = self._result
        current_step.error_message = "boom"
        return self._result

    def transition_to_next(self, flow):
        pass


def _failed_flow(project_root, *, announced):
    (project_root / "tianluo" / "state").mkdir(parents=True, exist_ok=True)
    flow = FlowInstance(
        task_description="Test task",
        task_type="feature",
        change_name="test-change",
        change_path=project_root,
    )
    flow.state.selected_steps = [StepType.IMPLEMENT]
    step = Step(
        step_type=StepType.IMPLEMENT,
        status=StepStatus.FAILED,
        step_id="implement-001",
    )
    step.error_message = "boom"
    if announced:
        step.inputs["failure_announced"] = True
    flow.state.add_step(step)
    flow.state.current_step_id = step.step_id
    flow.status = FlowStatus.RUNNING
    persistence = PersistenceManager(project_root)
    persistence.save_flow(flow)
    return flow, persistence


def _run(project_root, flow, persistence):
    return _run_flow_impl(
        project_root=project_root,
        flow_id=flow.flow_id,
        task_description=None,
        task_type="feature",
        change_name=None,
        is_worktree_mode=False,
        persistence=persistence,
        state_machine=_FakeStateMachine(StepStatus.FAILED),
        output_format="json",
        main_lock=None,
    )


def _step_failed_events(out):
    events = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if data.get("type") == "step_failed":
            events.append(data)
    return events


def test_a_first_failure_is_announced(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        flow, persistence = _failed_flow(root, announced=False)
        assert _run(root, flow, persistence) == 0
        assert len(_step_failed_events(capsys.readouterr().out)) == 1


def test_a_failure_already_announced_is_not_replayed(capsys):
    """The marker is what a pause-point ``continue`` leaves behind."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        flow, persistence = _failed_flow(root, announced=True)
        assert _run(root, flow, persistence) == 0
        assert _step_failed_events(capsys.readouterr().out) == []


def test_run_step_clears_the_marker_so_a_re_failure_is_announced(tmp_path):
    """A genuine re-run announces its own outcome; only a loop-back is silent."""
    from tianluo.engine.state_machine import StateMachine

    flow, persistence = _failed_flow(tmp_path, announced=True)
    step = flow.state.steps["implement-001"]
    step.status = StepStatus.PENDING
    machine = StateMachine(project_root=tmp_path, persistence=persistence)
    machine._handlers[StepType.IMPLEMENT] = lambda *_a: StepStatus.COMPLETED
    machine.run_step(flow, step)

    assert "failure_announced" not in step.inputs
