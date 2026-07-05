"""Production-side tests for the worktree merge-back ``merging`` sub-state (G2).

Two surfaces are covered:

* ``merge_cmd._acquire_merge_lock_with_callbacks`` — the queue-and-wait hook
  that lets a worktree merge-back surface "等待主分支锁". A live-held lock fires
  ``on_lock_wait`` once (before blocking) and ``on_lock_acquired`` once (after);
  a free / stale lock fires neither; and the no-callback path is exactly the
  legacy unconditional blocking acquire.
* ``run._finalize_worktree_merge`` — flags the worktree flow ``merging=True``
  (with a ``record_merging`` anchor) before handing off to ``run_merge`` and
  clears the flag on the failure path (worktree preserved) while leaving a
  successful --delete-merged path alone (worktree archived away).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import se3.commands.run as run
from se3.commands.merge.merge_lock import MergeLockBusy, MergeLockStale
from se3.commands.merge_cmd import _acquire_merge_lock_with_callbacks
from se3.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType
from se3.engine.persistence import PersistenceManager


# --------------------------------------------------------------------------
# _acquire_merge_lock_with_callbacks — the queue-and-wait hook
# --------------------------------------------------------------------------
class _FakeLock:
    """Records each ``acquire`` call's ``blocking`` flag.

    ``probe_exc`` (if set) is raised by the first non-blocking acquire to
    simulate a contended (MergeLockBusy) or stale (MergeLockStale) lock.
    """

    def __init__(self, probe_exc=None):
        self.calls: list[bool] = []
        self._probe_exc = probe_exc

    def acquire(self, blocking: bool = False) -> None:
        self.calls.append(blocking)
        if not blocking and self._probe_exc is not None:
            raise self._probe_exc


class TestAcquireMergeLockWithCallbacks:
    def test_none_callback_is_plain_blocking_acquire(self):
        # No callback → no probe, exactly the legacy unconditional blocking
        # acquire (queue-and-wait) so `se3 merge` is unchanged.
        lock = _FakeLock()
        _acquire_merge_lock_with_callbacks(lock, None, None)
        assert lock.calls == [True]

    def test_free_lock_fires_no_wait(self):
        lock = _FakeLock()  # non-blocking probe succeeds
        wait, acq = MagicMock(), MagicMock()
        _acquire_merge_lock_with_callbacks(lock, wait, acq)
        wait.assert_not_called()
        acq.assert_not_called()
        # Only the non-blocking probe ran; no second blocking acquire needed.
        assert lock.calls == [False]

    def test_contended_lock_fires_wait_then_acquired_once(self):
        lock = _FakeLock(probe_exc=MergeLockBusy(Path("x"), 123))
        wait, acq = MagicMock(), MagicMock()
        _acquire_merge_lock_with_callbacks(lock, wait, acq)
        wait.assert_called_once()
        acq.assert_called_once()
        # Probe (non-blocking) raised busy → block on the second acquire.
        assert lock.calls == [False, True]

    def test_stale_lock_reclaims_without_wait(self):
        # A dead holder: the blocking acquire reclaims immediately, so no
        # human-visible wait is surfaced.
        lock = _FakeLock(probe_exc=MergeLockStale(Path("x"), 123))
        wait, acq = MagicMock(), MagicMock()
        _acquire_merge_lock_with_callbacks(lock, wait, acq)
        wait.assert_not_called()
        acq.assert_not_called()
        assert lock.calls == [False, True]

    def test_wait_callback_exception_never_propagates(self):
        # A raising display hook must not break the merge's lock acquisition.
        lock = _FakeLock(probe_exc=MergeLockBusy(Path("x"), 1))
        wait = MagicMock(side_effect=RuntimeError("boom"))
        acq = MagicMock()
        _acquire_merge_lock_with_callbacks(lock, wait, acq)
        # Still blocked-acquired despite the wait hook raising.
        assert lock.calls == [False, True]
        acq.assert_called_once()


# --------------------------------------------------------------------------
# _finalize_worktree_merge — merging flag lifecycle
# --------------------------------------------------------------------------
def _make_worktree_flow(worktree_path: Path) -> tuple[FlowInstance, Step]:
    """Persist a COMPLETED worktree flow (one commit step) to its engine.json."""
    flow = FlowInstance(
        task_description="do it",
        status=FlowStatus.COMPLETED,
        is_worktree_mode=True,
    )
    step = Step(step_type=StepType.COMMIT, status=StepStatus.COMPLETED)
    flow.state.add_step(step)
    flow.state.current_step_id = step.step_id
    PersistenceManager(worktree_path).save_flow(flow)
    return flow, step


def _read_merging(worktree_path: Path):
    engine = worktree_path / "se3" / "state" / "engine.json"
    if not engine.exists():
        return None
    return json.loads(engine.read_text(encoding="utf-8")).get("merging", False)


def _anchor_events(worktree_path: Path, flow_id: str, step_id: str) -> list[dict]:
    path = (
        worktree_path / "se3" / "history" / flow_id / f"{step_id}.jsonl"
    )
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestFinalizeWorktreeMergeStatus:
    def _run(self, tmp_path: Path, merge_rc: int, delete_on_merge: bool):
        worktree_path = tmp_path / "wt"
        worktree_path.mkdir()
        flow, step = _make_worktree_flow(worktree_path)

        captured: dict = {}

        def fake_run_merge(branches, project_root, on_lock_wait=None,
                           on_lock_acquired=None):
            # Observe the state visible to the merge orchestrator: the flag must
            # already be True and the "合并中" anchor already written.
            captured["merging_during"] = _read_merging(worktree_path)
            captured["anchor_during"] = _anchor_events(
                worktree_path, flow.flow_id, step.step_id
            )
            captured["on_lock_wait"] = on_lock_wait
            captured["on_lock_acquired"] = on_lock_acquired
            if delete_on_merge:
                # A successful --delete-merged archives + removes the worktree.
                shutil.rmtree(worktree_path)
            return merge_rc

        with patch("se3.commands.merge_cmd.run_merge", side_effect=fake_run_merge), \
                patch("se3.commands.run.render_full"), \
                patch("se3.commands.run.get_console"), \
                patch("se3.commands.run.display_success"), \
                patch("se3.commands.run.display_error"), \
                patch("se3.commands.run.find_worktree_source_issue_by_branch",
                      return_value=None):
            rc = run._finalize_worktree_merge(
                tmp_path, "worktree/x", "master", worktree_path
            )
        return rc, worktree_path, flow, step, captured

    def test_merging_flag_and_anchor_set_before_merge(self, tmp_path):
        rc, worktree_path, flow, step, captured = self._run(
            tmp_path, merge_rc=0, delete_on_merge=True
        )
        assert rc == 0
        # Flag was True at the moment run_merge ran.
        assert captured["merging_during"] is True
        # A "merging" lifecycle anchor was written onto the last step.
        kinds = {e.get("type") for e in captured["anchor_during"]}
        assert "merging" in kinds
        # Lock-wait callbacks were wired through for the queue-and-wait display.
        assert callable(captured["on_lock_wait"])
        assert callable(captured["on_lock_acquired"])

    def test_success_archives_worktree_no_clear_needed(self, tmp_path):
        rc, worktree_path, flow, step, captured = self._run(
            tmp_path, merge_rc=0, delete_on_merge=True
        )
        assert rc == 0
        # --delete-merged removed the worktree (engine.json and all); the flag
        # vanished with it rather than being explicitly cleared.
        assert not worktree_path.exists()

    def test_failure_clears_merging_flag_and_writes_clear_anchor(self, tmp_path):
        rc, worktree_path, flow, step, captured = self._run(
            tmp_path, merge_rc=1, delete_on_merge=False
        )
        assert rc == 1
        assert captured["merging_during"] is True
        # Failure preserves the worktree → the flag must be cleared to False.
        assert _read_merging(worktree_path) is False
        # A clearing anchor (step_status carrying the step's terminal status)
        # supersedes the streamed "合并中" row.
        events = _anchor_events(worktree_path, flow.flow_id, step.step_id)
        assert any(
            e.get("type") == "step_status" and e.get("status") == "completed"
            for e in events
        )

    def test_status_bookkeeping_never_changes_exit_code(self, tmp_path):
        # Even if persistence blows up mid-flag, the merge's rc is returned as-is.
        worktree_path = tmp_path / "wt"
        worktree_path.mkdir()
        flow, step = _make_worktree_flow(worktree_path)

        with patch("se3.commands.merge_cmd.run_merge", return_value=7), \
                patch("se3.commands.run.render_full"), \
                patch("se3.commands.run.get_console"), \
                patch("se3.commands.run.display_success"), \
                patch("se3.commands.run.display_error"), \
                patch("se3.commands.run.find_worktree_source_issue_by_branch",
                      return_value=None), \
                patch.object(PersistenceManager, "save_flow",
                             side_effect=OSError("disk full")):
            rc = run._finalize_worktree_merge(
                tmp_path, "worktree/x", "master", worktree_path
            )
        assert rc == 7


class TestFinalizeWorktreeMergeMissingState:
    def test_no_engine_json_still_merges(self, tmp_path):
        # A worktree whose engine.json cannot be loaded must not break the merge:
        # callbacks fall back to None and run_merge still runs.
        worktree_path = tmp_path / "wt"
        worktree_path.mkdir()
        captured: dict = {}

        def fake_run_merge(branches, project_root, on_lock_wait=None,
                           on_lock_acquired=None):
            captured["on_lock_wait"] = on_lock_wait
            captured["on_lock_acquired"] = on_lock_acquired
            return 0

        with patch("se3.commands.merge_cmd.run_merge", side_effect=fake_run_merge), \
                patch("se3.commands.run.render_full"), \
                patch("se3.commands.run.get_console"), \
                patch("se3.commands.run.display_success"):
            rc = run._finalize_worktree_merge(
                tmp_path, "worktree/x", "master", worktree_path
            )
        assert rc == 0
        # No flow → no callbacks; run_merge acquires the lock the legacy way.
        assert captured["on_lock_wait"] is None
        assert captured["on_lock_acquired"] is None
