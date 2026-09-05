"""DAG-parallel interruption: the stop signal reaches worker threads.

This is the case the cooperative stop signal exists for. Every group of a
parallel implement step runs in a ``ThreadPoolExecutor`` worker, and CPython
delivers ``KeyboardInterrupt`` only to the main thread — so before the signal
existed a Ctrl-C (or a web interjection) simply could not reach a running
group. The scheduler stops feeding the pool, lets the in-flight groups wind
their own children down, and only then presents the interruption to the
scheduling thread.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from tianluo.engine.dag_scheduler import DAGScheduler, GroupResult
from tianluo.stop_signal import get_stop_signal


@pytest.fixture(autouse=True)
def _clean_signal():
    get_stop_signal().clear()
    yield
    get_stop_signal().clear()


def _flow_stub(flow_id="f1"):
    import types

    return types.SimpleNamespace(flow_id=flow_id)


def _groups(*ids, deps=None):
    deps = deps or {}
    return [
        {"group_id": gid, "depends_on": list(deps.get(gid, []))} for gid in ids
    ]


class TestNoStop:
    def test_all_groups_run_when_nothing_asks_to_stop(self):
        scheduler = DAGScheduler(_groups("G1", "G2"), max_workers=2)
        seen = []

        def _execute(group, _deps, _relay):
            seen.append(group["group_id"])
            return GroupResult(group_id=group["group_id"], status="completed")

        results = scheduler.run(_execute)
        assert sorted(seen) == ["G1", "G2"]
        assert all(r.status == "completed" for r in results)


class TestStopReachesWorkers:
    def test_a_running_group_observes_the_signal(self):
        """The flag is the ONLY channel that reaches a worker thread."""
        observed = threading.Event()
        entered = threading.Event()

        def _execute(group, _deps, _relay):
            entered.set()
            deadline = time.time() + 5
            while time.time() < deadline:
                if get_stop_signal().is_set():
                    observed.set()
                    break
                time.sleep(0.01)
            return GroupResult(group_id=group["group_id"], status="completed")

        scheduler = DAGScheduler(_groups("G1"), max_workers=1)
        stopper = threading.Thread(
            target=lambda: (entered.wait(5), get_stop_signal().request())
        )
        stopper.start()
        with pytest.raises(KeyboardInterrupt):
            scheduler.run(_execute)
        stopper.join(5)
        assert observed.is_set()

    def test_queued_groups_are_not_launched_after_a_stop(self):
        """A group the user just asked to stop must not be started."""
        started = []
        first_running = threading.Event()

        def _execute(group, _deps, _relay):
            started.append(group["group_id"])
            if group["group_id"] == "G1":
                first_running.set()
                # Hold G1 open long enough for the stop to be published while
                # G2 is still blocked on it.
                time.sleep(0.5)
            return GroupResult(group_id=group["group_id"], status="completed")

        # G2 depends on G1, so it is still in `pending` (not yet submitted)
        # when the stop lands — the scheduler-side half of the guard.
        scheduler = DAGScheduler(
            _groups("G1", "G2", deps={"G2": ["G1"]}), max_workers=2
        )
        stopper = threading.Thread(
            target=lambda: (first_running.wait(5), get_stop_signal().request())
        )
        stopper.start()
        with pytest.raises(KeyboardInterrupt):
            scheduler.run(_execute)
        stopper.join(5)
        assert "G1" in started
        assert "G2" not in started

    def test_the_interruption_is_raised_only_after_the_pool_converges(self):
        """No child may outlive the raise, so the raise waits for the pool."""
        finished = threading.Event()
        first_running = threading.Event()

        def _execute(group, _deps, _relay):
            first_running.set()
            time.sleep(0.3)
            finished.set()
            return GroupResult(group_id=group["group_id"], status="completed")

        scheduler = DAGScheduler(_groups("G1"), max_workers=1)
        stopper = threading.Thread(
            target=lambda: (first_running.wait(5), get_stop_signal().request())
        )
        stopper.start()
        with pytest.raises(KeyboardInterrupt):
            scheduler.run(_execute)
        stopper.join(5)
        assert finished.is_set()

    def test_a_stop_before_the_run_starts_launches_nothing(self):
        """Every ready group is submitted at once, so the "do not start" guard
        has to live on the worker, at the moment it picks the task up."""
        started = []

        def _execute(group, _deps, _relay):
            started.append(group["group_id"])
            return GroupResult(group_id=group["group_id"], status="completed")

        get_stop_signal().request()
        scheduler = DAGScheduler(_groups("G1", "G2"), max_workers=2)
        with pytest.raises(KeyboardInterrupt):
            scheduler.run(_execute)
        assert started == []

    def test_a_stop_while_the_last_group_finishes_still_interrupts(self):
        """The groups were cut short, so the run was stopped — even though the
        pool converged without another scheduling tick."""
        entered = threading.Event()

        def _execute(group, _deps, _relay):
            entered.set()
            # Return as soon as the stop lands, the way a runner does.
            deadline = time.time() + 5
            while time.time() < deadline and not get_stop_signal().is_set():
                time.sleep(0.01)
            return GroupResult(group_id=group["group_id"], status="completed")

        scheduler = DAGScheduler(_groups("G1"), max_workers=1)
        stopper = threading.Thread(
            target=lambda: (entered.wait(5), get_stop_signal().request())
        )
        stopper.start()
        with pytest.raises(KeyboardInterrupt):
            scheduler.run(_execute)
        stopper.join(5)

    def test_a_fast_group_is_never_submitted_twice(self):
        """``add_done_callback`` fires inline for an already-finished future and
        re-enters the submit loop; a group must still run exactly once."""
        seen = []

        def _execute(group, _deps, _relay):
            seen.append(group["group_id"])
            return GroupResult(group_id=group["group_id"], status="completed")

        scheduler = DAGScheduler(
            _groups("G1", "G2", "G3", deps={"G3": ["G1", "G2"]}), max_workers=3
        )
        scheduler.run(_execute)
        assert sorted(seen) == ["G1", "G2", "G3"]


class TestInterruptedResultsAreSalvaged:
    def test_completed_and_interrupted_groups_are_reported_on_the_exception(self):
        """The results ride ON the interruption: without them the implement
        handler keeps its empty list, cannot salvage the group worktrees, and a
        continuation re-runs (and force-cleans) work that was already done."""
        from pathlib import Path

        from tianluo.engine.dag_scheduler import DAGInterrupted

        entered = threading.Event()
        g1_done = threading.Event()

        def _execute(group, _deps, _relay):
            gid = group["group_id"]
            if gid == "G1":
                result = GroupResult(
                    group_id=gid, status="completed",
                    files_changed=["a.py"], branch_name="impl/f/G1",
                    worktree_path=Path("/tmp/wt-g1"),
                )
                g1_done.set()
                return result
            # Deterministic ordering: the stop is requested only once G1 has
            # genuinely finished, so this asserts "completed before the stop"
            # rather than racing the pool.
            g1_done.wait(5)
            entered.set()
            deadline = time.time() + 5
            while time.time() < deadline and not get_stop_signal().is_set():
                time.sleep(0.01)
            return GroupResult(
                group_id=gid, status="interrupted",
                branch_name="impl/f/G2", worktree_path=Path("/tmp/wt-g2"),
            )

        scheduler = DAGScheduler(_groups("G1", "G2"), max_workers=2)
        stopper = threading.Thread(
            target=lambda: (entered.wait(5), get_stop_signal().request())
        )
        stopper.start()
        with pytest.raises(DAGInterrupted) as excinfo:
            scheduler.run(_execute)
        stopper.join(5)

        by_id = {r.group_id: r for r in excinfo.value.results}
        assert by_id["G1"].status == "completed"
        assert by_id["G1"].files_changed == ["a.py"]
        # The interrupted group keeps the worktree its session is bound to.
        assert by_id["G2"].status == "interrupted"
        assert by_id["G2"].worktree_path == Path("/tmp/wt-g2")

    def test_a_worker_that_raises_the_interrupt_is_not_reported_as_failed(self):
        from tianluo.engine.dag_scheduler import DAGInterrupted

        def _execute(group, _deps, _relay):
            get_stop_signal().request()
            raise KeyboardInterrupt

        scheduler = DAGScheduler(_groups("G1"), max_workers=1)
        with pytest.raises(DAGInterrupted) as excinfo:
            scheduler.run(_execute)
        assert excinfo.value.results[0].status == "interrupted"

    def test_the_interruption_is_still_a_keyboard_interrupt(self):
        """Every existing handler up the stack catches KeyboardInterrupt; the
        results payload must be purely additive."""
        from tianluo.engine.dag_scheduler import DAGInterrupted

        assert issubclass(DAGInterrupted, KeyboardInterrupt)


class TestImplementSalvagesInterruptedState:
    """The scheduler hands the results over; the implement handler has to keep
    them, or a continuation re-runs completed groups and force-cleans the
    worktree an interrupted group's session is bound to."""

    def _step(self, tmp_path):
        from tianluo.engine.models import Step, StepType

        return Step(step_id="05_implement_x", step_type=StepType.IMPLEMENT)

    def test_completed_groups_are_persisted_and_worktrees_recorded(self, tmp_path):
        from pathlib import Path

        from tianluo.engine.steps.implement import (
            DAG_PRESERVED_WORKTREES_KEY,
            _persist_interrupted_dag_state,
        )

        step = self._step(tmp_path)
        results = [
            GroupResult(
                group_id="G1", status="completed", files_changed=["a.py"],
                tests_added=["t.py"], summary="did G1",
                branch_name="impl/f/G1", worktree_path=Path("/tmp/wt-g1"),
            ),
            GroupResult(
                group_id="G2", status="interrupted",
                branch_name="impl/f/G2", worktree_path=Path("/tmp/wt-g2"),
            ),
        ]
        _persist_interrupted_dag_state(step, results, None)

        assert step.outputs["implemented_groups"] == ["G1"]
        assert step.outputs["files_changed"] == ["a.py"]
        assert step.outputs["group_summaries"] == [
            {"group_id": "G1", "summary": "did G1"}
        ]
        preserved = step.outputs[DAG_PRESERVED_WORKTREES_KEY]
        assert preserved["G2"]["worktree"] == "/tmp/wt-g2"
        assert preserved["G2"]["status"] == "interrupted"

    def test_a_preserved_worktree_is_reused_only_when_it_is_still_real(
        self, tmp_path,
    ):
        from tianluo.engine.steps.implement import _reuse_preserved_worktree

        wt = tmp_path / "wt-g2"
        (wt / ".git").mkdir(parents=True)
        lock = threading.Lock()
        preserved = {
            "G2": {"branch": "impl/f/G2", "worktree": str(wt)},
            "G3": {"branch": "impl/f/G3", "worktree": str(tmp_path / "gone")},
        }
        assert _reuse_preserved_worktree(preserved, "G2", "impl/f/G2", lock) == (
            wt, "impl/f/G2",
        )
        # A stale record must never make a group run in a non-checkout.
        assert _reuse_preserved_worktree(preserved, "G3", "impl/f/G3", lock) is None
        assert _reuse_preserved_worktree({}, "G2", "impl/f/G2", lock) is None

    def test_a_mid_merge_worktree_is_recovered_before_reuse(
        self, tmp_path, monkeypatch,
    ):
        """A stop landing inside the convergence conflict resolution can leave
        MERGE_HEAD and conflict-marked files behind. Resuming the agent there
        would have the end-of-group ``git add -A && git commit`` complete that
        merge with whatever is on disk."""
        from tianluo.engine.steps import implement as impl

        wt = tmp_path / "wt-g3"
        (wt / ".git").mkdir(parents=True)
        state = {"merging": True}
        aborts: list[tuple] = []

        monkeypatch.setattr(impl, "merge_in_progress", lambda _p: state["merging"])

        def _git(root, *args, **_kw):
            import types as _types

            if args[:1] == ("merge",):
                aborts.append((root, args))
                state["merging"] = False
            return _types.SimpleNamespace(returncode=0, stdout="impl/f/G3", stderr="")

        monkeypatch.setattr(impl, "_run_git", _git)

        preserved = {"G3": {"branch": "impl/f/G3", "worktree": str(wt)}}
        assert impl._reuse_preserved_worktree(
            preserved, "G3", "impl/f/G3", threading.Lock(),
        ) == (wt, "impl/f/G3")
        assert aborts and aborts[0][1] == ("merge", "--abort")

    def test_an_unabortable_merge_refuses_the_reuse(self, tmp_path, monkeypatch):
        """A merge that cannot be aborted is not resumable work at all."""
        from tianluo.engine.steps import implement as impl

        wt = tmp_path / "wt-g3"
        (wt / ".git").mkdir(parents=True)
        monkeypatch.setattr(impl, "merge_in_progress", lambda _p: True)

        def _git(_root, *_args, **_kw):
            import types as _types

            return _types.SimpleNamespace(returncode=0, stdout="impl/f/G3", stderr="")

        monkeypatch.setattr(impl, "_run_git", _git)
        preserved = {"G3": {"branch": "impl/f/G3", "worktree": str(wt)}}
        assert impl._reuse_preserved_worktree(
            preserved, "G3", "impl/f/G3", threading.Lock(),
        ) is None

    def test_a_completed_groups_partial_verdict_survives_the_interruption(
        self, tmp_path,
    ):
        """A group completed before the stop is SKIPPED on the continuation, so
        this record is the only surviving copy of its verdict. Dropping it made
        the step report ``complete`` and the review/fix loop never learned of
        the unfinished work."""
        from tianluo.engine.steps.implement import (
            DAG_GROUP_COMPLETION_KEY,
            _persist_interrupted_dag_state,
        )

        step = self._step(tmp_path)
        results = [
            GroupResult(
                group_id="G1", status="completed", summary="did G1",
                completion_status="partial",
                incomplete_tasks=["wire the CLI", "add tests"],
                branch_name="impl/f/G1",
            ),
            GroupResult(group_id="G2", status="interrupted"),
        ]
        _persist_interrupted_dag_state(step, results, None)

        assert step.outputs[DAG_GROUP_COMPLETION_KEY]["G1"] == {
            "completion_status": "partial",
            "incomplete_tasks": ["wire the CLI", "add tests"],
        }
        assert "G2" not in step.outputs[DAG_GROUP_COMPLETION_KEY]

    def test_a_relay_descendant_reuses_its_predecessors_branch(self, tmp_path):
        """An interrupted relay descendant never had a branch of its own: it
        inherited the predecessor's worktree AND branch. Requiring the
        generated ``impl/<flow>/<group>`` name here rejected exactly the groups
        the reuse exists for, sending them to a fresh worktree and losing both
        their uncommitted work and their resumable session."""
        from tianluo.engine.steps.implement import _reuse_preserved_worktree

        wt = tmp_path / "wt-g1"
        (wt / ".git").mkdir(parents=True)
        lock = threading.Lock()
        preserved = {
            # G2 relayed into G1's worktree before it was interrupted.
            "G2": {"branch": "impl/f/G1", "worktree": str(wt)},
        }
        assert _reuse_preserved_worktree(preserved, "G2", "impl/f/G2", lock) == (
            wt, "impl/f/G1",
        )


class TestStopArrivingAsAnExceptionOnTheSchedulingThread:
    """The scheduling thread IS the main thread, so a stop can reach it as an
    exception rather than as the flag: the SIGINT handler and the interjection
    watcher both raise whenever no LLM call is in flight, which is exactly the
    case while the workers are still creating their worktrees. Unwinding on it
    would hand the implement handler an empty result list, and every group's
    worktree — its uncommitted work and the cwd its provider session is bound
    to — would then be force-cleaned and re-run from scratch.
    """

    def _raise_once_on_this_thread(self, monkeypatch, armed):
        """Make the scheduling thread's own stop-flag poll raise once."""
        signal_obj = get_stop_signal()
        scheduling_thread = threading.current_thread()
        real_is_set = signal_obj.is_set
        state = {"raised": False}

        def _is_set():
            if (
                not state["raised"]
                and armed.is_set()
                and threading.current_thread() is scheduling_thread
            ):
                state["raised"] = True
                raise KeyboardInterrupt
            return real_is_set()

        monkeypatch.setattr(signal_obj, "is_set", _is_set)
        return real_is_set, state

    def test_the_pool_converges_and_the_results_ride_on_the_interruption(
        self, monkeypatch,
    ):
        from pathlib import Path

        from tianluo.engine.dag_scheduler import DAGInterrupted

        started = threading.Event()
        real_is_set, state = self._raise_once_on_this_thread(monkeypatch, started)
        returned = threading.Event()

        def _execute(group, _deps, _relay):
            gid = group["group_id"]
            started.set()
            deadline = time.time() + 5
            while time.time() < deadline and not real_is_set():
                time.sleep(0.01)
            returned.set()
            return GroupResult(
                group_id=gid, status="interrupted",
                branch_name=f"impl/f/{gid}",
                worktree_path=Path(f"/tmp/wt-{gid}"),
            )

        scheduler = DAGScheduler(_groups("G1"), max_workers=1)
        with pytest.raises(DAGInterrupted) as excinfo:
            scheduler.run(_execute)

        assert state["raised"], "the interrupt never reached the scheduler"
        # Converged BEFORE the interruption surfaced: no group child outlives
        # the raise, and the group's real result (not a bare placeholder) is
        # what the caller receives.
        assert returned.is_set()
        by_id = {r.group_id: r for r in excinfo.value.results}
        assert by_id["G1"].status == "interrupted"
        assert by_id["G1"].worktree_path == Path("/tmp/wt-G1")
        assert by_id["G1"].branch_name == "impl/f/G1"

    def test_the_stop_is_published_so_running_groups_wind_down(
        self, monkeypatch,
    ):
        """The exception reaches only the scheduling thread; the workers see a
        stop at all only because the scheduler publishes it on the signal."""
        from tianluo.engine.dag_scheduler import DAGInterrupted

        started = threading.Event()
        real_is_set, _state = self._raise_once_on_this_thread(monkeypatch, started)
        observed = threading.Event()

        def _execute(group, _deps, _relay):
            started.set()
            deadline = time.time() + 5
            while time.time() < deadline:
                if real_is_set():
                    observed.set()
                    break
                time.sleep(0.01)
            return GroupResult(group_id=group["group_id"], status="interrupted")

        scheduler = DAGScheduler(_groups("G1"), max_workers=1)
        with pytest.raises(DAGInterrupted):
            scheduler.run(_execute)
        assert observed.is_set()

    def test_queued_groups_are_dropped_rather_than_launched(self, monkeypatch):
        """Same treatment as the flag path: a run the user just stopped must
        not start new groups behind the interrupt."""
        from tianluo.engine.dag_scheduler import DAGInterrupted

        started = threading.Event()
        real_is_set, _state = self._raise_once_on_this_thread(monkeypatch, started)
        launched = []

        def _execute(group, _deps, _relay):
            gid = group["group_id"]
            launched.append(gid)
            if gid == "G1":
                started.set()
                deadline = time.time() + 5
                while time.time() < deadline and not real_is_set():
                    time.sleep(0.01)
            return GroupResult(group_id=gid, status="interrupted")

        # G2 depends on G1, so it is still queued when the interrupt lands.
        scheduler = DAGScheduler(
            _groups("G1", "G2", deps={"G2": ["G1"]}), max_workers=2,
        )
        with pytest.raises(DAGInterrupted) as excinfo:
            scheduler.run(_execute)

        assert launched == ["G1"]
        by_id = {r.group_id: r for r in excinfo.value.results}
        assert by_id["G2"].status in ("skipped", "interrupted")

    def test_a_repeat_interrupt_does_not_abandon_a_running_group(
        self, monkeypatch,
    ):
        """A second Ctrl-C while converging must not leave a child running past
        the point the caller believes the run has stopped."""
        from pathlib import Path

        from tianluo.engine.dag_scheduler import DAGInterrupted

        started = threading.Event()
        self._raise_once_on_this_thread(monkeypatch, started)

        # The convergence loop polls nothing — its only intercept point is its
        # own ``condition.wait``, so that is where the repeat Ctrl-C is
        # delivered. Gated on the scheduling thread and fired once, after the
        # first interrupt has already opened the convergence loop.
        scheduling_thread = threading.current_thread()
        real_wait = threading.Condition.wait
        second = {"delivered": False}
        in_convergence_wait = threading.Event()

        def _wait(self_cond, timeout=None):
            if (
                not second["delivered"]
                and get_stop_signal().is_set()
                and threading.current_thread() is scheduling_thread
            ):
                second["delivered"] = True
                in_convergence_wait.set()
                raise KeyboardInterrupt
            return real_wait(self_cond, timeout)

        monkeypatch.setattr(threading.Condition, "wait", _wait)
        finished = threading.Event()

        def _execute(group, _deps, _relay):
            gid = group["group_id"]
            started.set()
            # Still running when the repeat interrupt lands — a fact the worker
            # waits for rather than a timing hope, so the assertions below
            # really cover "the loop kept waiting for a live child".
            in_convergence_wait.wait(timeout=5)
            time.sleep(0.05)
            finished.set()
            return GroupResult(
                group_id=gid, status="interrupted",
                branch_name=f"impl/f/{gid}",
                worktree_path=Path(f"/tmp/wt-{gid}"),
            )

        scheduler = DAGScheduler(_groups("G1"), max_workers=1)
        try:
            scheduler.run(_execute)
        except DAGInterrupted as exc:
            results = exc.results
        except KeyboardInterrupt:
            pytest.fail(
                "the repeat interrupt escaped the convergence loop, "
                "abandoning a running group child"
            )
        else:
            pytest.fail("the scheduler never reported the interruption")

        assert second["delivered"], (
            "the repeat interrupt never reached the convergence wait"
        )
        assert finished.is_set()
        # Kept waiting after the second interrupt: the worker's OWN result
        # (worktree and branch included) is what the caller receives, not the
        # bare placeholder a group cut short mid-flight would get.
        by_id = {r.group_id: r for r in results}
        assert by_id["G1"].status == "interrupted"
        assert by_id["G1"].worktree_path == Path("/tmp/wt-G1")
        assert by_id["G1"].branch_name == "impl/f/G1"

    def test_an_interrupt_while_submitting_is_handled_the_same_way(
        self, monkeypatch,
    ):
        """The stop can land anywhere on the scheduling thread, not only in the
        wait — submission runs there too."""
        from tianluo.engine.dag_scheduler import DAGInterrupted

        scheduler = DAGScheduler(_groups("G1"), max_workers=1)
        real_build = scheduler._build_relay_context

        def _build(gid, completed):
            raise KeyboardInterrupt

        monkeypatch.setattr(scheduler, "_build_relay_context", _build)

        def _execute(group, _deps, _relay):  # pragma: no cover - never reached
            return GroupResult(group_id=group["group_id"], status="completed")

        with pytest.raises(DAGInterrupted) as excinfo:
            scheduler.run(_execute)
        assert real_build is not None
        # Claimed but never submitted: nothing ran for it, and the scheduler
        # must not converge on a worker that does not exist.
        assert excinfo.value.results[0].status == "skipped"


class TestPoolShutdownIgnoresInterrupts:
    def test_shutdown_keeps_waiting_when_an_interrupt_lands_in_it(self):
        from concurrent.futures import ThreadPoolExecutor

        from tianluo.engine.dag_scheduler import _shutdown_pool

        executor = ThreadPoolExecutor(max_workers=1)
        done = threading.Event()
        executor.submit(lambda: (time.sleep(0.2), done.set()))

        real_shutdown = executor.shutdown
        calls = {"n": 0}

        def _shutdown(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt
            return real_shutdown(*args, **kwargs)

        executor.shutdown = _shutdown  # type: ignore[assignment]
        _shutdown_pool(executor)

        assert calls["n"] == 2
        assert done.is_set()


class TestSalvagedResults:
    """The last-resort accessor for a caller that catches a stop the scheduler
    did not convert itself: an empty list there costs the group worktrees."""

    def test_it_reports_what_the_run_achieved(self):
        from pathlib import Path

        def _execute(group, _deps, _relay):
            return GroupResult(
                group_id=group["group_id"], status="completed",
                branch_name="impl/f/G1", worktree_path=Path("/tmp/wt-g1"),
            )

        scheduler = DAGScheduler(_groups("G1"), max_workers=1)
        scheduler.run(_execute)
        salvaged = {r.group_id: r for r in scheduler.salvaged_results()}
        assert salvaged["G1"].worktree_path == Path("/tmp/wt-g1")

    def test_groups_with_no_result_are_interrupted_not_dropped(self):
        scheduler = DAGScheduler(_groups("G1", "G2"), max_workers=1)
        salvaged = scheduler.salvaged_results()
        assert sorted(r.group_id for r in salvaged) == ["G1", "G2"]
        assert all(r.status == "interrupted" for r in salvaged)


class TestInterruptedDAGRunPreservesEverything:
    """Driving ``_run_dag_parallel`` itself: a stop must clean NOTHING and must
    persist what the run achieved, or the continuation re-runs completed groups
    in worktrees that no longer exist."""

    def _step(self):
        from tianluo.engine.models import Step, StepType

        return Step(step_id="05_implement_x", step_type=StepType.IMPLEMENT)

    def _patch_environment(self, monkeypatch, tmp_path, scheduler_factory):
        import types as _types

        from tianluo.engine.dag_scheduler import DAGInterrupted
        from tianluo.engine.steps import implement as impl

        cleaned: list[str] = []
        monkeypatch.setattr(impl, "get_current_branch", lambda *_a, **_k: "master")
        monkeypatch.setattr(impl, "merge_in_progress", lambda *_a, **_k: False)
        monkeypatch.setattr(
            impl, "recover_stale_unmerged_paths", lambda *_a, **_k: ([], [])
        )
        monkeypatch.setattr(
            impl, "force_cleanup_worktree",
            lambda root, branch: cleaned.append(branch),
        )
        monkeypatch.setattr(impl, "_salvage_results_history", lambda *_a, **_k: None)
        monkeypatch.setattr(
            impl, "_run_git",
            lambda *_a, **_k: _types.SimpleNamespace(
                returncode=0, stdout="", stderr=""
            ),
        )
        monkeypatch.setattr(impl, "_make_execute_fn", lambda **_k: (lambda *_a: None))
        monkeypatch.setattr(impl, "DAGScheduler", scheduler_factory)
        return cleaned, DAGInterrupted

    def test_an_interrupted_run_cleans_no_worktree_and_persists_its_state(
        self, monkeypatch, tmp_path,
    ):
        from pathlib import Path

        from tianluo.engine.steps import implement as impl

        results = [
            GroupResult(
                group_id="G1", status="completed", files_changed=["a.py"],
                summary="did G1", branch_name="impl/f1/G1",
                worktree_path=Path("/tmp/wt-g1"), estimated_test_duration=600,
            ),
            GroupResult(
                group_id="G2", status="interrupted", branch_name="impl/f1/G2",
                worktree_path=Path("/tmp/wt-g2"),
            ),
        ]

        from tianluo.engine.dag_scheduler import DAGInterrupted

        class _Scheduler:
            def __init__(self, *_a, **_k):
                pass

            def run(self, _execute_fn):
                raise DAGInterrupted("stopped", results)

            def get_fallback_leaves(self):
                return []

        cleaned, _ = self._patch_environment(monkeypatch, tmp_path, _Scheduler)

        step = self._step()
        flow = _flow_stub()
        with pytest.raises(DAGInterrupted):
            impl._run_dag_parallel(
                groups=[{"group_id": "G1"}, {"group_id": "G2", "depends_on": ["G1"]}],
                step=step, flow=flow, project_root=tmp_path,
                task_description="t", task_type="feature", injection=None,
                retry_count=0,
            )

        # INVARIANT: nothing is cleaned on an interrupted run.
        assert cleaned == []
        assert step.outputs["implemented_groups"] == ["G1"]
        preserved = step.outputs[impl.DAG_PRESERVED_WORKTREES_KEY]
        assert preserved["G2"]["status"] == "interrupted"
        # A skipped group's whole-suite estimate has to survive too, or TEST
        # sizes its timeout from the re-run groups alone.
        assert step.outputs["estimated_test_duration"] == 600


class TestResumeClassification:
    def test_an_interrupted_fork_group_is_never_read_as_completed(self):
        """A fork branch carries its predecessor's commits from birth.

        Probing it with ``has_new_commits`` answers "completed" for a group
        that implemented nothing, so a group recorded as interrupted must not
        be probed at all.
        """
        from tianluo.engine.steps.implement import (
            DAG_PRESERVED_WORKTREES_KEY,
            _groups_to_scan_for_survivors,
        )

        outputs = {
            "implemented_groups": ["G1"],
            DAG_PRESERVED_WORKTREES_KEY: {
                "G1": {"branch": "impl/f/G1", "status": "completed"},
                "G2": {"branch": "impl/f/G1", "status": "interrupted"},
                "G3": {"branch": "impl/f/G3", "status": "interrupted"},
            },
        }
        assert _groups_to_scan_for_survivors(
            outputs, {"G1", "G2", "G3", "G4"}, {"G1"},
        ) == {"G4"}

    def test_groups_with_no_preserved_record_are_still_scanned(self):
        """Disaster recovery from a crash (no persisted state) is unchanged."""
        from tianluo.engine.steps.implement import _groups_to_scan_for_survivors

        assert _groups_to_scan_for_survivors({}, {"G1", "G2"}, set()) == {"G1", "G2"}


class TestRewindCleansGroupWorktrees:
    def _patch_cleanup(
        self, monkeypatch, residue=None, cleanup_error=None,
        ref_gone=True, delete_error=None,
    ):
        """Stub the two removal helpers plus the residue probe they are verified by.

        The probe is real git; the fake project root here is not a repo, so it
        is stubbed to answer "nothing left" unless a test says otherwise.
        """
        from tianluo.engine import worktree as worktree_mod

        calls = {"cleaned": [], "deleted": []}

        def _force(root, branch):
            if cleanup_error:
                raise cleanup_error
            calls["cleaned"].append(branch)

        monkeypatch.setattr(worktree_mod, "force_cleanup_worktree", _force)
        def _delete(root, branch):
            calls["deleted"].append(branch)
            if delete_error:
                raise delete_error
            return ref_gone

        monkeypatch.setattr(worktree_mod, "delete_branch", _delete)
        monkeypatch.setattr(
            worktree_mod, "worktree_path_for_branch",
            lambda root, branch: Path("/nonexistent-tianluo-wt") / branch,
        )
        import tianluo.engine.flow_workspace as fw

        monkeypatch.setattr(
            fw, "group_cleanup_residue",
            lambda root, branch: list((residue or {}).get(branch, [])),
        )
        return calls

    def _branches(self):
        from tianluo.engine.models import Step, StepType

        step = Step(step_id="05_implement_x", step_type=StepType.IMPLEMENT)
        step.inputs = {"task_groups": [{"group_id": "G1"}, {"group_id": "G2"}]}
        step.outputs = {"implemented_groups": ["G1", "G3"]}
        from tianluo.engine import rewind as rewind_mod

        return rewind_mod._group_branches_for_step(_flow_stub(), step)

    def test_planned_and_implemented_group_branches_are_removed(self, monkeypatch):
        from tianluo.engine import rewind as rewind_mod

        calls = self._patch_cleanup(monkeypatch)
        branches = self._branches()
        result = rewind_mod._cleanup_branches(_flow_stub(), branches, "/tmp/proj")

        assert calls["cleaned"] == calls["deleted"] == [
            "impl/f1/G1", "impl/f1/G2", "impl/f1/G3",
        ]
        assert result == calls["cleaned"]

    def test_a_surviving_branch_refuses_the_rewind(self, monkeypatch):
        """A cleanup helper that logs-and-returns must not pass for success."""
        import pytest

        from tianluo.engine import rewind as rewind_mod

        self._patch_cleanup(
            monkeypatch, residue={"impl/f1/G2": ["branch impl/f1/G2"]}
        )
        branches = self._branches()
        with pytest.raises(rewind_mod.RewindError) as excinfo:
            rewind_mod._cleanup_branches(_flow_stub(), branches, "/tmp/proj")
        assert "impl/f1/G2" in str(excinfo.value)

    def test_an_unanswerable_probe_refuses_the_rewind(self, monkeypatch):
        """git failing to say whether the branch is gone is not proof it is."""
        import pytest

        from tianluo.engine import rewind as rewind_mod
        import tianluo.engine.flow_workspace as fw

        self._patch_cleanup(monkeypatch)

        def _boom(root, branch):
            raise RuntimeError("git worktree list failed")

        monkeypatch.setattr(fw, "group_cleanup_residue", _boom)
        with pytest.raises(rewind_mod.RewindError):
            rewind_mod._cleanup_branches(_flow_stub(), self._branches(), "/tmp/proj")

    def test_a_surviving_worktree_directory_refuses_the_rewind(self, monkeypatch, tmp_path):
        import pytest

        from tianluo.engine import rewind as rewind_mod
        from tianluo.engine import worktree as worktree_mod

        self._patch_cleanup(monkeypatch)
        stale = tmp_path / "wt"
        stale.mkdir()
        monkeypatch.setattr(
            worktree_mod, "worktree_path_for_branch",
            lambda root, branch: stale if branch == "impl/f1/G3" else tmp_path / "gone",
        )
        with pytest.raises(rewind_mod.RewindError) as excinfo:
            rewind_mod._cleanup_branches(_flow_stub(), self._branches(), "/tmp/proj")
        assert "impl/f1/G3" in str(excinfo.value)

    def test_an_unanswerable_probe_still_reports_the_deleted_refs(self, monkeypatch):
        """The refusal stands, but a ref that really went away must still be
        reported as deleted — otherwise the caller hands the flow back with a
        group recorded as done whose branch no longer exists."""
        import pytest

        from tianluo.engine import rewind as rewind_mod
        import tianluo.engine.flow_workspace as fw

        self._patch_cleanup(monkeypatch)

        def _boom(root, branch):
            raise RuntimeError("git for-each-ref timed out")

        monkeypatch.setattr(fw, "group_cleanup_residue", _boom)
        with pytest.raises(rewind_mod.RewindError) as excinfo:
            rewind_mod._cleanup_branches(_flow_stub(), self._branches(), "/tmp/proj")
        assert excinfo.value.cleaned_branches == []
        assert excinfo.value.deleted_branches == [
            "impl/f1/G1", "impl/f1/G2", "impl/f1/G3",
        ]

    def test_a_surviving_directory_still_reports_the_deleted_ref(
        self, monkeypatch, tmp_path,
    ):
        """A worktree directory ``rmtree`` could not finish refuses the rewind,
        yet its branch ref is gone and its group state dangles."""
        import pytest

        from tianluo.engine import rewind as rewind_mod
        from tianluo.engine import worktree as worktree_mod

        self._patch_cleanup(monkeypatch)
        stale = tmp_path / "wt"
        stale.mkdir()
        monkeypatch.setattr(
            worktree_mod, "worktree_path_for_branch",
            lambda root, branch: stale if branch == "impl/f1/G3" else tmp_path / "gone",
        )
        with pytest.raises(rewind_mod.RewindError) as excinfo:
            rewind_mod._cleanup_branches(_flow_stub(), self._branches(), "/tmp/proj")
        assert excinfo.value.cleaned_branches == ["impl/f1/G1", "impl/f1/G2"]
        assert "impl/f1/G3" in excinfo.value.deleted_branches

    def test_a_ref_that_never_went_away_is_not_reported_as_deleted(self, monkeypatch):
        """``git branch -D`` refusing the deletion discards nothing, so the
        group's recorded results still reach the tree and must be kept."""
        import pytest

        from tianluo.engine import rewind as rewind_mod

        self._patch_cleanup(
            monkeypatch, ref_gone=False,
            residue={"impl/f1/G2": ["branch impl/f1/G2"]},
        )
        with pytest.raises(rewind_mod.RewindError) as excinfo:
            rewind_mod._cleanup_branches(_flow_stub(), self._branches(), "/tmp/proj")
        assert "impl/f1/G2" not in excinfo.value.deleted_branches

    def test_an_unanswerable_deletion_counts_as_deleted(self, monkeypatch):
        """``git branch -D`` that neither confirmed nor denied itself errs
        towards re-running the group, never towards skipping it."""
        import pytest

        from tianluo.engine import rewind as rewind_mod

        self._patch_cleanup(monkeypatch, delete_error=RuntimeError("git hung"))
        with pytest.raises(rewind_mod.RewindError) as excinfo:
            rewind_mod._cleanup_branches(_flow_stub(), self._branches(), "/tmp/proj")
        assert excinfo.value.cleaned_branches == []
        assert excinfo.value.deleted_branches == [
            "impl/f1/G1", "impl/f1/G2", "impl/f1/G3",
        ]

    def test_a_failed_worktree_cleanup_deletes_nothing(self, monkeypatch):
        """The branch is never reached, so nothing about it dangles."""
        import pytest

        from tianluo.engine import rewind as rewind_mod

        calls = self._patch_cleanup(monkeypatch, cleanup_error=RuntimeError("locked"))
        with pytest.raises(rewind_mod.RewindError) as excinfo:
            rewind_mod._cleanup_branches(_flow_stub(), self._branches(), "/tmp/proj")
        assert calls["deleted"] == []
        assert excinfo.value.deleted_branches == []


class TestExecuteFnResumesAGroupInPlace:
    """``continue`` after an interruption must put each group back where it was:
    same worktree, same branch, and an LLMCaller addressed at that worktree so
    the group's own recorded session is the one resumed."""

    def _step(self):
        from tianluo.engine.models import Step, StepType

        step = Step(step_id="05_implement_x", step_type=StepType.IMPLEMENT)
        step.inputs = {}
        return step

    def _patch(self, monkeypatch, *, changes=False, head_branch=""):
        import types as _types

        from tianluo.engine import context_builder
        from tianluo.engine.steps import implement as impl

        calls = {"llm": [], "created": [], "cleaned": [], "git": []}

        class _Caller:
            def __init__(self, root, **kwargs):
                calls["llm"].append({"root": root, **kwargs})

            def call(self, **_kw):
                return "{}"

        def _git(root, *args, **_kw):
            calls["git"].append((str(root), args))
            stdout = ""
            if args and args[0] == "status" and changes:
                stdout = " M x.py\n"
            elif args and args[0] == "rev-parse" and "HEAD" in args:
                # What the worktree currently has checked out: the reuse guard
                # rejects a worktree whose HEAD has moved elsewhere.
                stdout = head_branch
            return _types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(impl, "LLMCaller", _Caller)
        monkeypatch.setattr(impl, "_run_git", _git)
        monkeypatch.setattr(
            impl, "_restore_history_to_worktree", lambda *_a, **_k: None
        )
        monkeypatch.setattr(
            context_builder, "get_runtime_context_injection", lambda *_a, **_k: ""
        )
        monkeypatch.setattr(impl, "parse_json_response", lambda *_a, **_k: {})
        monkeypatch.setattr(
            impl, "force_cleanup_worktree",
            lambda root, branch: calls["cleaned"].append(branch),
        )
        monkeypatch.setattr(
            impl, "create_worktree",
            lambda root, branch: calls["created"].append(branch) or (
                Path(root) / "fresh" / branch.replace("/", "-")
            ),
        )
        monkeypatch.setattr(impl, "record_group_status", lambda *_a, **_k: None)
        return calls

    def test_a_preserved_worktree_is_reused_and_addressed(
        self, monkeypatch, tmp_path,
    ):
        from tianluo.engine.dag_scheduler import RelayContext
        from tianluo.engine.steps import implement as impl

        calls = self._patch(monkeypatch, head_branch="impl/f1/G1")
        wt = tmp_path / "wt-g1"
        (wt / ".git").mkdir(parents=True)

        execute_fn = impl._make_execute_fn(
            project_root=tmp_path, original_branch="master", flow=_flow_stub(),
            step=self._step(), task_description="t", task_type="feature",
            injection=None, retry_count=2,
            preserved_worktrees={
                "G1": {"branch": "impl/f1/G1", "worktree": str(wt),
                       "status": "interrupted"},
            },
        )
        result = execute_fn({"group_id": "G1"}, {}, RelayContext())

        assert result.status == "completed"
        # The worktree is reused, never recreated.
        assert calls["created"] == []
        assert calls["cleaned"] == []
        llm = calls["llm"][0]
        assert llm["root"] == wt
        assert llm["step_id"] == "05_implement_x_G1"
        assert llm["external_attempt"] == 2

    def test_a_stop_inside_the_convergence_resolution_aborts_the_merge(
        self, monkeypatch, tmp_path,
    ):
        """INVARIANT: the convergence block never leaves a half-merged worktree.

        A stop request raises KeyboardInterrupt straight out of the LLM conflict
        resolution — past every ``except Exception`` in the group closure — and
        the group then returns interrupted with MERGE_HEAD and conflict markers
        on disk. The continuation reuses that worktree, and the end-of-group
        ``git add -A && git commit`` completes the merge with whatever is there.
        """
        from tianluo.engine.dag_scheduler import RelayContext
        from tianluo.engine.steps import implement as impl

        calls = self._patch(monkeypatch, head_branch="impl/f1/G3")
        wt = tmp_path / "wt-g3"
        (wt / ".git").mkdir(parents=True)

        # The secondary merge conflicts; the resolver is cut short by the stop.
        real_git = impl._run_git
        merging = {"on": False}

        def _git(root, *args, **kw):
            real_git(root, *args, **kw)
            import types as _types

            if args[:2] == ("merge", "--abort"):
                merging["on"] = False
                return _types.SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[:1] == ("merge",):
                merging["on"] = True
                return _types.SimpleNamespace(
                    returncode=1, stdout="CONFLICT (content): x.py", stderr="",
                )
            if args and args[0] == "rev-parse" and "HEAD" in args:
                return _types.SimpleNamespace(
                    returncode=0, stdout="impl/f1/G3", stderr="",
                )
            return _types.SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(impl, "_run_git", _git)
        monkeypatch.setattr(
            impl, "get_conflicting_files", lambda *_a, **_k: ["x.py"]
        )
        # The worktree is CLEAN when it is picked up (so the reuse guard has
        # nothing to abort) and only goes mid-merge inside the block under test.
        monkeypatch.setattr(impl, "merge_in_progress", lambda *_a, **_k: merging["on"])

        def _boom(*_a, **_k):
            raise KeyboardInterrupt

        monkeypatch.setattr(impl, "_resolve_convergence_conflicts", _boom)

        ctx = RelayContext()
        ctx.worktree_path = wt
        ctx.convergence_merges = ["impl/f1/G2"]

        execute_fn = impl._make_execute_fn(
            project_root=tmp_path, original_branch="master", flow=_flow_stub(),
            step=self._step(), task_description="t", task_type="feature",
            injection=None, retry_count=1,
            preserved_worktrees={
                "G3": {"branch": "impl/f1/G3", "worktree": str(wt),
                       "status": "interrupted"},
            },
        )
        result = execute_fn({"group_id": "G3"}, {}, ctx)

        assert result.status == "interrupted"
        # The group keeps its worktree/branch so the continuation resumes it...
        assert result.worktree_path == wt
        merge_args = [a for _root, a in calls["git"] if a[:1] == ("merge",)]
        assert ("merge", "--abort") in merge_args, (
            "the interrupted convergence merge was left on disk"
        )
        # ...and it is handed back CLEAN, not mid-merge.
        assert merging["on"] is False

    def test_failed_reuse_rebuilds_on_the_recorded_branch(
        self, monkeypatch, tmp_path,
    ):
        """The recorded branch holds real work — the predecessor's completed
        commits for a relay heir, whose merge was DEFERRED because this group
        was expected to keep building on them. Branching off the base branch
        would drop that work out of everything the step later merges."""
        from tianluo.engine.dag_scheduler import RelayContext
        from tianluo.engine.steps import implement as impl

        calls = self._patch(monkeypatch)

        execute_fn = impl._make_execute_fn(
            project_root=tmp_path, original_branch="master", flow=_flow_stub(),
            step=self._step(), task_description="t", task_type="feature",
            injection=None, retry_count=0,
            preserved_worktrees={
                # G2 relayed into G1's worktree; that directory is gone now.
                "G2": {"branch": "impl/f1/G1", "worktree": str(tmp_path / "gone"),
                       "status": "interrupted"},
            },
        )
        execute_fn({"group_id": "G2"}, {}, RelayContext())

        branch_cmds = [a for _root, a in calls["git"] if a and a[0] == "branch"]
        assert ("branch", "impl/f1/G2", "impl/f1/G1") in branch_cmds

    def test_a_group_with_no_preserved_record_branches_off_the_base(
        self, monkeypatch, tmp_path,
    ):
        from tianluo.engine.dag_scheduler import RelayContext
        from tianluo.engine.steps import implement as impl

        calls = self._patch(monkeypatch)
        execute_fn = impl._make_execute_fn(
            project_root=tmp_path, original_branch="master", flow=_flow_stub(),
            step=self._step(), task_description="t", task_type="feature",
            injection=None, retry_count=0,
        )
        execute_fn({"group_id": "G9"}, {}, RelayContext())

        branch_cmds = [a for _root, a in calls["git"] if a and a[0] == "branch"]
        assert ("branch", "impl/f1/G9", "master") in branch_cmds
        assert calls["created"] == ["impl/f1/G9"]


class TestAllRecoveredCompletionStatus:
    """Every group recovered = nothing left to run, but NOT automatically
    "complete": a group that finished ``partial`` before the interruption is
    skipped here, so hard-coding a clean verdict hid its unfinished work from
    the downstream review/fix loop entirely."""

    def _step(self):
        from tianluo.engine.models import Step, StepType

        step = Step(step_id="05_implement_x", step_type=StepType.IMPLEMENT)
        step.inputs = {}
        return step

    def _run(self, monkeypatch, tmp_path, prior_outputs):
        import types as _types

        from tianluo.engine.steps import implement as impl

        monkeypatch.setattr(impl, "get_current_branch", lambda *_a, **_k: "master")
        monkeypatch.setattr(impl, "merge_in_progress", lambda *_a, **_k: False)
        monkeypatch.setattr(
            impl, "recover_stale_unmerged_paths", lambda *_a, **_k: ([], [])
        )
        monkeypatch.setattr(
            impl, "_run_git",
            lambda *_a, **_k: _types.SimpleNamespace(
                returncode=1, stdout="", stderr=""
            ),
        )
        step = self._step()
        status = impl._run_dag_parallel(
            groups=[], step=step, flow=_flow_stub(), project_root=tmp_path,
            task_description="t", task_type="feature", injection=None,
            retry_count=1, prior_outputs=prior_outputs,
        )
        return step, status

    def test_a_recovered_partial_group_keeps_the_step_partial(
        self, monkeypatch, tmp_path,
    ):
        from tianluo.engine.steps.implement import DAG_GROUP_COMPLETION_KEY

        step, _status = self._run(monkeypatch, tmp_path, {
            "implemented_groups": ["G1"],
            "group_summaries": [{"group_id": "G1", "summary": "did most of G1"}],
            DAG_GROUP_COMPLETION_KEY: {
                "G1": {
                    "completion_status": "partial",
                    "incomplete_tasks": ["wire the CLI"],
                },
            },
        })

        assert step.outputs["completion_status"] == "partial"
        assert step.outputs["incomplete_tasks"] == ["wire the CLI"]

    def test_recovered_complete_groups_still_report_complete(
        self, monkeypatch, tmp_path,
    ):
        from tianluo.engine.steps.implement import DAG_GROUP_COMPLETION_KEY

        step, _status = self._run(monkeypatch, tmp_path, {
            "implemented_groups": ["G1"],
            DAG_GROUP_COMPLETION_KEY: {
                "G1": {"completion_status": "complete", "incomplete_tasks": []},
            },
        })

        assert step.outputs["completion_status"] == "complete"
        assert step.outputs["incomplete_tasks"] == []

    def test_a_flow_recorded_before_the_key_existed_still_reports_complete(
        self, monkeypatch, tmp_path,
    ):
        step, _status = self._run(monkeypatch, tmp_path, {
            "implemented_groups": ["G1"],
            "group_summaries": [{"group_id": "G1", "summary": "did G1"}],
        })

        assert step.outputs["completion_status"] == "complete"
        assert step.outputs["incomplete_tasks"] == []


class TestDeferredPredecessorMerge:
    """A completed predecessor whose branch an interrupted heir still holds has
    its merge deferred to step end. If the heir then could NOT reuse that
    branch, nothing else would ever merge it — the predecessor's work would sit
    on an orphan branch while ``implemented_groups`` claims it landed."""

    def _step(self):
        from tianluo.engine.models import Step, StepType

        step = Step(step_id="05_implement_x", step_type=StepType.IMPLEMENT)
        step.inputs = {}
        return step

    def test_the_deferred_branch_is_merged_when_the_heir_moved_elsewhere(
        self, monkeypatch, tmp_path,
    ):
        import types as _types

        from tianluo.engine import worktree as worktree_mod
        from tianluo.engine.dag_scheduler import RelayPlan
        from tianluo.engine.steps import implement as impl

        merged: list[str] = []

        class _Scheduler:
            def __init__(self, *_a, **_k):
                pass

            def run(self, _execute_fn):
                # The heir ran on a branch of its own (its reuse of G1's
                # worktree failed), so G1's branch is not among the results.
                return [
                    GroupResult(
                        group_id="G2", status="completed",
                        branch_name="impl/f1/G2",
                    )
                ]

            def get_fallback_leaves(self):
                return []

        monkeypatch.setattr(impl, "get_current_branch", lambda *_a, **_k: "master")
        monkeypatch.setattr(impl, "merge_in_progress", lambda *_a, **_k: False)
        monkeypatch.setattr(
            impl, "recover_stale_unmerged_paths", lambda *_a, **_k: ([], [])
        )
        monkeypatch.setattr(impl, "_salvage_results_history", lambda *_a, **_k: None)
        monkeypatch.setattr(
            impl, "_run_git",
            lambda *_a, **_k: _types.SimpleNamespace(
                returncode=0, stdout="", stderr=""
            ),
        )
        monkeypatch.setattr(impl, "_make_execute_fn", lambda **_k: (lambda *_a: None))
        monkeypatch.setattr(impl, "DAGScheduler", _Scheduler)
        monkeypatch.setattr(
            impl, "classify_chains",
            lambda groups: RelayPlan(
                relay_map={}, fork_from={}, root_nodes={"G2"},
                leaf_nodes={"G2"}, convergence_points={},
            ),
        )
        monkeypatch.setattr(worktree_mod, "has_new_commits", lambda *_a, **_k: True)
        monkeypatch.setattr(
            impl, "_merge_leaf_branch",
            lambda root, branch, target, *_a, **_k: merged.append(branch) or True,
        )
        monkeypatch.setattr(
            impl, "_is_branch_reachable_from",
            lambda root, branch, target: branch in merged,
        )
        monkeypatch.setattr(impl, "delete_branch", lambda *_a, **_k: None)
        monkeypatch.setattr(impl, "record_group_status", lambda *_a, **_k: None)
        monkeypatch.setattr(
            impl, "_resolve_files_changed", lambda *_a, **_k: None
        )

        step = self._step()
        impl._run_dag_parallel(
            groups=[{"group_id": "G2"}],
            step=step, flow=_flow_stub(), project_root=tmp_path,
            task_description="t", task_type="feature", injection=None,
            retry_count=1,
            prior_outputs={
                "implemented_groups": ["G1"],
                impl.DAG_PRESERVED_WORKTREES_KEY: {
                    # G2 (interrupted heir) was holding G1's branch.
                    "G2": {"branch": "impl/f1/G1", "worktree": "/tmp/gone",
                           "status": "interrupted"},
                },
            },
        )

        assert "impl/f1/G2" in merged
        assert "impl/f1/G1" in merged, "the deferred predecessor never merged"

    def test_a_held_predecessor_is_merged_before_its_dependents_run(
        self, monkeypatch, tmp_path,
    ):
        """Only the CLEANUP is deferred, never the merge.

        ``_prune_recovered_dependencies`` strips a completed group from every
        retained group's ``depends_on`` on the premise that its commits are in
        ``original_branch``. A dependent that never started before the stop is
        therefore a root and branches straight off ``original_branch`` — so if
        the predecessor's merge waits for step end, that dependent implements
        and tests against a tree missing its dependency.
        """
        import types as _types

        from tianluo.engine import worktree as worktree_mod
        from tianluo.engine.dag_scheduler import RelayPlan
        from tianluo.engine.steps import implement as impl

        events: list[str] = []

        class _Scheduler:
            def __init__(self, *_a, **_k):
                pass

            def run(self, _execute_fn):
                events.append("groups-run")
                return [
                    GroupResult(
                        group_id="G3", status="completed",
                        branch_name="impl/f1/G3",
                    )
                ]

            def get_fallback_leaves(self):
                return []

        monkeypatch.setattr(impl, "get_current_branch", lambda *_a, **_k: "master")
        monkeypatch.setattr(impl, "merge_in_progress", lambda *_a, **_k: False)
        monkeypatch.setattr(
            impl, "recover_stale_unmerged_paths", lambda *_a, **_k: ([], [])
        )
        monkeypatch.setattr(impl, "_salvage_results_history", lambda *_a, **_k: None)
        monkeypatch.setattr(
            impl, "_run_git",
            lambda *_a, **_k: _types.SimpleNamespace(
                returncode=0, stdout="", stderr=""
            ),
        )
        monkeypatch.setattr(impl, "_make_execute_fn", lambda **_k: (lambda *_a: None))
        monkeypatch.setattr(impl, "DAGScheduler", _Scheduler)
        monkeypatch.setattr(
            impl, "classify_chains",
            lambda groups: RelayPlan(
                relay_map={}, fork_from={}, root_nodes={"G3"},
                leaf_nodes={"G3"}, convergence_points={},
            ),
        )
        monkeypatch.setattr(worktree_mod, "has_new_commits", lambda *_a, **_k: True)

        merged: list[str] = []

        def _merge(_root, branch, _target, *_a, **_k):
            merged.append(branch)
            events.append("merge:" + branch)
            return True

        monkeypatch.setattr(impl, "_merge_leaf_branch", _merge)
        monkeypatch.setattr(
            impl, "_is_branch_reachable_from",
            lambda root, branch, target: branch in merged,
        )
        monkeypatch.setattr(impl, "delete_branch", lambda *_a, **_k: None)
        monkeypatch.setattr(impl, "record_group_status", lambda *_a, **_k: None)
        monkeypatch.setattr(impl, "_resolve_files_changed", lambda *_a, **_k: None)

        step = self._step()
        impl._run_dag_parallel(
            # G3 depends on G1, which _prune_recovered_dependencies already
            # dropped from its depends_on — so it runs as a root off master.
            groups=[{"group_id": "G3", "depends_on": []}],
            step=step, flow=_flow_stub(), project_root=tmp_path,
            task_description="t", task_type="feature", injection=None,
            retry_count=1,
            prior_outputs={
                "implemented_groups": ["G1"],
                impl.DAG_PRESERVED_WORKTREES_KEY: {
                    # G2 (interrupted relay heir) still holds G1's branch.
                    "G2": {"branch": "impl/f1/G1", "worktree": str(tmp_path / "wt"),
                           "status": "interrupted"},
                },
            },
        )

        assert "merge:impl/f1/G1" in events
        assert events.index("merge:impl/f1/G1") < events.index("groups-run"), (
            "G3 branched off a tree that did not contain G1's commits"
        )
        # The branch is NOT deleted here — its worktree is still the heir's.
        assert step.outputs.get("preserved_branches") in (None, [])
