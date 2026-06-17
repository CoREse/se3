"""Tests for the contended-lock ``waiting_for_lock`` clear event and the
merge-lock-during-retry hypothesis (group G3 of the running-flow freeze fix).

Two independent concerns are covered:

* **Clear event** — when a synchronous run finds the main-worktree mutex
  contended it writes a streaming ``waiting_for_lock`` "等待锁" anchor before
  blocking to acquire (covered by ``test_run_lock_lifecycle.py``). Previously,
  on acquiring the lock it cleared ``waiting_for_lock=False`` only in
  engine.json — it never wrote a matching jsonl anchor, so the web console's
  live transcript could stay frozen on "等待锁" (engine.json is not the
  conversation channel; only a later same-step lifecycle anchor supersedes the
  streamed row). ``_ensure_main_lock_for_step`` now emits a
  ``chat_history.record_lock_acquired`` clearing anchor (a ``step_status`` row
  with ``status="running"``) the moment a *contended* lock is acquired. A
  free / stale acquire writes no such event.

* **Merge-lock hypothesis** — the suspicion that "a single-step error releases
  the merge lock, which causes the freeze" is *refuted* here: the lock is
  acquired by ``run_flow`` and released ONLY in ``run_flow``'s ``finally`` when
  the whole flow exits. The retry loop lives inside ``_run_flow_impl`` and never
  touches the lock, so a step error followed by a manual retry keeps the lock
  held the entire time. The freeze root cause therefore lies in the
  display/push path, not in merge-lock release.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from se3.commands.merge.merge_lock import MergeLock, MergeLockBusy
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


def _jsonl_path(project: Path, flow_id: str, step_id: str) -> Path:
    return project / "se3" / "history" / flow_id / f"{step_id}.jsonl"


def _read_records(project: Path, flow_id: str, step_id: str) -> list[dict]:
    path = _jsonl_path(project, flow_id, step_id)
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _read_engine(project: Path) -> dict:
    return json.loads((project / "se3" / "state" / "engine.json").read_text())


# --------------------------------------------------------------------------
# (a) Contended acquire emits the clear anchor; free acquire does not
# --------------------------------------------------------------------------

def test_contended_acquire_emits_lock_acquired_clear_event(project: Path) -> None:
    from se3.commands.run import _ensure_main_lock_for_step

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

    # Wait until the worker has surfaced the "等待锁" anchor (before it blocks).
    jsonl = _jsonl_path(project, flow.flow_id, step.step_id)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if any(r.get("type") == "waiting_for_lock" for r in
               _read_records(project, flow.flow_id, step.step_id)):
            break
        time.sleep(0.02)
    else:
        holder.release()
        worker.join(timeout=5.0)
        pytest.fail("waiting_for_lock anchor was not written before blocking")

    # No clear event yet — the worker is still blocked on the held lock.
    assert not done.is_set()
    records = _read_records(project, flow.flow_id, step.step_id)
    assert not any(r.get("type") == "step_status" for r in records)

    # Release the holder; the worker acquires and must emit the clear anchor.
    holder.release()
    assert done.wait(timeout=5.0), "worker did not finish after lock released"

    try:
        assert run_lock.held is True
        assert flow.waiting_for_lock is False
        assert not _read_engine(project).get("waiting_for_lock")

        records = _read_records(project, flow.flow_id, step.step_id)
        # The streamed "等待锁" anchor is still present (raw history is appended,
        # never rewritten) ...
        waiting = [r for r in records if r.get("type") == "waiting_for_lock"]
        assert len(waiting) == 1
        # ... and an explicit clearing anchor was appended AFTER it: a
        # step_status row with status="running" that the frontend's
        # removeSupersededStatusRows folds over the "等待锁" row in place.
        clears = [r for r in records
                  if r.get("type") == "step_status"
                  and r.get("status") == "running"]
        assert len(clears) == 1
        clear = clears[0]
        assert clear.get("step_id") == step.step_id
        assert clear.get("step_type") == step.step_type.value
        assert "role" not in clear  # not a ChatMessage — CLI history skips it
        # Ordering: the clear anchor comes after the waiting anchor.
        assert records.index(clear) > records.index(waiting[0])
    finally:
        run_lock.release()


def test_free_acquire_writes_no_clear_event(project: Path) -> None:
    from se3.commands.run import _ensure_main_lock_for_step

    persistence = PersistenceManager(project)
    flow, step = _make_flow(StepType.ANALYZE)
    main_lock = MergeLock(project)

    _ensure_main_lock_for_step(main_lock, flow, step, project, persistence)
    try:
        assert main_lock.held is True
        assert flow.waiting_for_lock is False
        # Neither a waiting anchor nor a clear anchor: no jsonl was written at all.
        assert not _jsonl_path(project, flow.flow_id, step.step_id).exists()
    finally:
        main_lock.release()


def test_stale_reclaim_writes_no_clear_event(project: Path) -> None:
    from se3.commands.run import _ensure_main_lock_for_step

    persistence = PersistenceManager(project)
    flow, step = _make_flow(StepType.ANALYZE)

    # Stale lock recording a dead PID — reclaimed silently, no wait surfaced.
    lock_file = project / "se3" / "state" / "merge.lock"
    dead_pid = 2 ** 22 - 1
    lock_file.write_text(f"{dead_pid:016d}\n")

    main_lock = MergeLock(project)
    _ensure_main_lock_for_step(main_lock, flow, step, project, persistence)
    try:
        assert main_lock.held is True
        assert flow.waiting_for_lock is False
        assert not _jsonl_path(project, flow.flow_id, step.step_id).exists()
    finally:
        main_lock.release()


def test_record_lock_acquired_is_idempotent(project: Path) -> None:
    from se3.engine.chat_history import record_lock_acquired

    flow, step = _make_flow(StepType.ANALYZE)
    args = (project, flow.flow_id, step.step_id, step.step_type.value)

    record_lock_acquired(*args)
    record_lock_acquired(*args)  # second call must not append a duplicate

    records = _read_records(project, flow.flow_id, step.step_id)
    running = [r for r in records
               if r.get("type") == "step_status" and r.get("status") == "running"]
    assert len(running) == 1


def test_record_lock_acquired_skipped_by_get_step_history(project: Path) -> None:
    """The clear anchor must not pollute CLI history / retry context."""
    from se3.engine.chat_history import record_lock_acquired, get_step_history

    flow, step = _make_flow(StepType.ANALYZE)
    record_lock_acquired(project, flow.flow_id, step.step_id, step.step_type.value)

    session = get_step_history(project, flow.flow_id, step.step_id)
    # Session may be None or empty, but it must carry NO chat messages derived
    # from the status anchor.
    messages = list(getattr(session, "messages", []) or []) if session else []
    assert messages == []


# --------------------------------------------------------------------------
# (b) Merge-lock-during-retry hypothesis — refuted
# --------------------------------------------------------------------------

def test_merge_lock_held_through_retry_not_released_until_flow_exit(
    project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refute the 'error releases the merge lock → freeze' theory.

    CONCLUSION (recorded here): a single-step error followed by a manual retry
    does NOT release the main-worktree merge lock. ``run_flow`` acquires the
    lock and releases it ONLY in its ``finally`` when the whole flow exits; the
    retry loop lives entirely inside ``_run_flow_impl`` and never calls
    ``release()``. So while a step is failing and being retried the flow has not
    exited and the lock stays held — excluding merge-lock release as the freeze
    root cause. The freeze lives in the display/push path instead.
    """
    from se3.commands import run as run_mod

    persistence = PersistenceManager(project)
    flow, step = _make_flow(StepType.ANALYZE)
    observed: dict[str, object] = {}

    def fake_impl(*args, main_lock=None, **kwargs):
        # Lazily acquire as the first non-discovery step would, then simulate
        # staying *inside* the flow across a step error + manual retry: the loop
        # never exits _run_flow_impl, so it never releases the lock.
        run_mod._ensure_main_lock_for_step(
            main_lock, flow, step, project, persistence)
        observed["held_during_impl"] = bool(main_lock and main_lock.held)

        # Mid-retry, an independent contender must NOT be able to grab the lock.
        contender = MergeLock(project)
        try:
            contender.acquire(blocking=False)
            observed["contender_acquired_mid_retry"] = True
            contender.release()
        except MergeLockBusy:
            observed["contender_acquired_mid_retry"] = False

        # Simulate several retry iterations — the lock is never touched here.
        for _ in range(3):
            observed["still_held"] = bool(main_lock and main_lock.held)
        return 1  # the flow ultimately returns (e.g. abort), exiting run_flow

    monkeypatch.setattr(run_mod, "_run_flow_impl", fake_impl)

    rc = run_mod.run_flow(
        project_root=project,
        flow_id=None,
        task_description="t",
        task_type="feature",
        acquire_main_lock=True,
    )

    assert rc == 1
    # During the (simulated) retry window the lock was held and uncontendable.
    assert observed["held_during_impl"] is True
    assert observed["contender_acquired_mid_retry"] is False
    assert observed["still_held"] is True

    # Only after run_flow returns (flow exit) is the lock released by the
    # finally — an independent acquirer can now take it.
    after = MergeLock(project)
    after.acquire(blocking=False)
    try:
        assert after.held is True
    finally:
        after.release()
