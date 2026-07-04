"""Tests for the deferred main-worktree lock lifecycle in ``se3 run``.

Covers the (1a)+(1b) lock-regression fix implemented by
``_ensure_main_lock_for_step``:

* discovery steps never hold the main-worktree mutex (1a);
* a free lock is acquired immediately with no visible wait state;
* a contended lock makes the flow persist ``waiting_for_lock=True`` and write a
  streaming ``waiting_for_lock`` event BEFORE it blocks acquiring (1b);
* the flag is cleared once the lock is acquired;
* ``waiting_for_lock`` round-trips through engine.json and old files read False;
* a ``--worktree`` body (``main_lock is None``) is a pure no-op.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.commands.run import _ensure_main_lock_for_step
from se3.commands.merge.merge_lock import MergeLock
from se3.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType
from se3.engine.persistence import PersistenceManager


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "se3" / "state").mkdir(parents=True)
    return tmp_path


def _make_flow(step_type: StepType) -> tuple[FlowInstance, Step]:
    step = Step(step_type=step_type, step_id=f"01_{step_type.value}_abc123")
    step.status = StepStatus.RUNNING
    flow = FlowInstance(task_description="t", status=FlowStatus.RUNNING)
    flow.state.add_step(step)
    flow.state.current_step_id = step.step_id
    return flow, step


def _read_engine(project: Path) -> dict:
    return json.loads((project / "se3" / "state" / "engine.json").read_text())


def _jsonl_path(project: Path, flow_id: str, step_id: str) -> Path:
    return project / "se3" / "history" / flow_id / f"{step_id}.jsonl"


# --------------------------------------------------------------------------
# waiting_for_lock model round-trip (task 3)
# --------------------------------------------------------------------------

def test_waiting_for_lock_roundtrips_true() -> None:
    flow, _ = _make_flow(StepType.ANALYZE)
    flow.waiting_for_lock = True
    restored = FlowInstance.from_dict(flow.to_dict())
    assert restored.waiting_for_lock is True
    # And it is actually emitted when True.
    assert flow.to_dict().get("waiting_for_lock") is True


def test_waiting_for_lock_false_omitted_and_reads_false() -> None:
    flow, _ = _make_flow(StepType.ANALYZE)
    assert flow.waiting_for_lock is False
    # Default False is omitted from the serialized form (keeps worktree bodies
    # and legacy files clean).
    assert "waiting_for_lock" not in flow.to_dict()
    # A legacy engine.json with no such key reads back as False.
    data = flow.to_dict()
    assert "waiting_for_lock" not in data
    assert FlowInstance.from_dict(data).waiting_for_lock is False


# --------------------------------------------------------------------------
# Discovery never holds the lock (1a)
# --------------------------------------------------------------------------

def test_discovery_step_does_not_acquire_lock(project: Path) -> None:
    persistence = PersistenceManager(project)
    flow, step = _make_flow(StepType.DISCOVERY)
    main_lock = MergeLock(project)

    _ensure_main_lock_for_step(main_lock, flow, step, project, persistence)

    assert main_lock.held is False
    # The lock is genuinely free — an independent acquirer can take it.
    other = MergeLock(project)
    other.acquire(blocking=False)
    try:
        assert other.held is True
    finally:
        other.release()


# --------------------------------------------------------------------------
# Free lock — immediate acquire, no waiting state
# --------------------------------------------------------------------------

def test_free_lock_acquired_without_waiting_state(project: Path) -> None:
    persistence = PersistenceManager(project)
    flow, step = _make_flow(StepType.ANALYZE)
    main_lock = MergeLock(project)

    _ensure_main_lock_for_step(main_lock, flow, step, project, persistence)

    try:
        assert main_lock.held is True
        assert flow.waiting_for_lock is False
        # No waiting_for_lock event was written.
        assert not _jsonl_path(project, flow.flow_id, step.step_id).exists()
    finally:
        main_lock.release()


def test_already_held_lock_is_noop(project: Path) -> None:
    persistence = PersistenceManager(project)
    flow, step = _make_flow(StepType.ANALYZE)
    main_lock = MergeLock(project)
    main_lock.acquire(blocking=False)
    try:
        # Second call (e.g. a later non-discovery step) is a no-op.
        _ensure_main_lock_for_step(main_lock, flow, step, project, persistence)
        assert main_lock.held is True
        assert flow.waiting_for_lock is False
    finally:
        main_lock.release()


def test_worktree_body_none_lock_is_noop(project: Path) -> None:
    persistence = PersistenceManager(project)
    flow, step = _make_flow(StepType.ANALYZE)
    # main_lock is None for a --worktree flow body.
    _ensure_main_lock_for_step(None, flow, step, project, persistence)
    assert flow.waiting_for_lock is False


# --------------------------------------------------------------------------
# Contended lock — persist waiting_for_lock + event BEFORE blocking (1b)
# --------------------------------------------------------------------------

def test_busy_lock_surfaces_waiting_state_then_acquires(project: Path) -> None:
    persistence = PersistenceManager(project)
    flow, step = _make_flow(StepType.ANALYZE)

    # Hold the lock with an independent instance so the run's probe sees it busy.
    holder = MergeLock(project)
    holder.acquire(blocking=False)

    run_lock = MergeLock(project)
    done = threading.Event()

    def _run() -> None:
        _ensure_main_lock_for_step(run_lock, flow, step, project, persistence)
        done.set()

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()

    # Wait until the worker has surfaced the waiting state on disk (before it
    # blocks on the held lock).
    engine_path = project / "se3" / "state" / "engine.json"
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if engine_path.exists() and _read_engine(project).get("waiting_for_lock"):
            break
        time.sleep(0.02)
    else:
        holder.release()
        worker.join(timeout=5.0)
        pytest.fail("waiting_for_lock was not persisted before blocking")

    # The streaming waiting_for_lock event is on disk and incrementally readable.
    jsonl = _jsonl_path(project, flow.flow_id, step.step_id)
    assert jsonl.exists()
    records = [json.loads(ln) for ln in jsonl.read_text().splitlines() if ln.strip()]
    assert any(r.get("type") == "waiting_for_lock" for r in records)

    # The worker is still blocked (lock not yet acquired).
    assert not done.is_set()
    assert run_lock.held is False

    # Release the holder; the worker should now acquire and clear the flag.
    holder.release()
    assert done.wait(timeout=5.0), "worker did not finish after lock released"

    try:
        assert run_lock.held is True
        assert flow.waiting_for_lock is False
        # The cleared flag is persisted (omitted when False).
        assert not _read_engine(project).get("waiting_for_lock")
    finally:
        run_lock.release()


# --------------------------------------------------------------------------
# Stale lock — reclaimed without a visible wait
# --------------------------------------------------------------------------

def test_stale_lock_reclaimed_without_waiting(project: Path) -> None:
    persistence = PersistenceManager(project)
    flow, step = _make_flow(StepType.ANALYZE)

    # Write a stale lock file recording a PID that does not exist.
    lock_file = project / "se3" / "state" / "merge.lock"
    dead_pid = 2 ** 22 - 1  # implausibly high, almost certainly absent
    lock_file.write_text(f"{dead_pid:016d}\n")

    main_lock = MergeLock(project)
    _ensure_main_lock_for_step(main_lock, flow, step, project, persistence)
    try:
        assert main_lock.held is True
        # Reclaim is silent — no waiting state surfaced.
        assert flow.waiting_for_lock is False
        assert not _jsonl_path(project, flow.flow_id, step.step_id).exists()
    finally:
        main_lock.release()


# --------------------------------------------------------------------------
# Ctrl+C while queued on the blocking acquire — clear waiting_for_lock before
# persisting, so a dead process is not surfaced as a live "running·waiting"
# flow (the daemon/web reader keys "active waiting" off status=running +
# waiting_for_lock=True).
# --------------------------------------------------------------------------

def test_interrupt_while_waiting_clears_flag_before_persist(project: Path) -> None:
    from se3.commands.run import run_flow

    flow = FlowInstance(
        flow_id="interrupt-wait-001",
        task_description="t",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = [StepType.ANALYZE]
    flow.state.current_step_index = 0
    step = Step(
        step_type=StepType.ANALYZE,
        status=StepStatus.PENDING,
        step_id="01_analyze_abc12345",
        inputs={},
        outputs={},
    )
    flow.state.add_step(step)
    flow.state.current_step_id = step.step_id

    saved_flags: list[bool] = []

    def _ensure(main_lock, f, current_step, proot, persistence) -> None:
        # Simulate the contended path: mark waiting + persist, then the operator
        # presses Ctrl+C while blocked on acquire(blocking=True).
        f.waiting_for_lock = True
        persistence.save_flow(f)
        raise KeyboardInterrupt

    with patch("se3.commands.run.PersistenceManager") as mock_pm_class, patch(
        "se3.commands.run.StateMachine"
    ) as mock_sm_class, patch("se3.commands.run.STEP_HANDLERS", {}), patch(
        "se3.commands.run.render_full"
    ), patch(
        "se3.commands.run._ensure_main_lock_for_step", side_effect=_ensure
    ):
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = flow
        # Resume loads header-first via load_flow_by_id (issue #244 B4); peek the
        # active flow_id so the snapshot-recovery path is not falsely triggered.
        mock_pm.load_flow_by_id.return_value = flow
        mock_pm._peek_active_flow_id.return_value = flow.flow_id
        mock_pm.save_flow.side_effect = lambda f: saved_flags.append(f.waiting_for_lock)

        mock_sm_class.return_value = MagicMock()

        exit_code = run_flow(
            project_root=project,
            flow_id=flow.flow_id,
            output_format="cli",
            acquire_main_lock=False,
        )

    assert exit_code == 130
    # The interrupt handler cleared the flag before its persist, so the final
    # persisted value is False and a dead process is never left as "waiting".
    assert flow.waiting_for_lock is False
    assert True in saved_flags  # the contended save recorded waiting=True
    assert saved_flags[-1] is False  # the post-interrupt save recorded waiting=False
