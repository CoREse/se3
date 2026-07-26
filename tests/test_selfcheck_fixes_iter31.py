"""Regression tests for the two self-check fixes in fix iteration 31.

Locks in:

* **Strict main-checkout resolution for worktree merge-side steps
  (state_machine / config).** A worktree flow must run ``merge_integrate`` /
  ``version_reconcile`` in the *main* checkout under the merge lock. The
  resolver must POSITIVELY resolve that checkout or raise — it must never
  silently degrade to the isolation worktree when the git probe faults, which
  would land the branch and write the version/changelog outside master.
  ``config.probe_main_repo_root`` therefore raises ``MainRepoProbeError`` on a
  genuine probe failure while still returning ``None`` for the legitimate
  "not a worktree" case; the swallow-to-``None`` wrapper
  ``_resolve_main_repo_root`` keeps its lenient contract for everyone else.

* **Mode-independent own-replay detection in the version race guard
  (commit).** A direct-run flow's own already-accounted version advance must
  not be misclassified as concurrent drift — including in *script mode* (no
  reconstructable version-file blob) and under
  ``version.include_in_commit_message: false`` (no ``Version:`` trailer). The
  guard consults a durable ``flow_committed_version`` marker the commit step
  records on the flow's state, so a re-entered commit recognises its own bump.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import tianluo.config as cfg
from tianluo.config import (
    MainRepoProbeError,
    _resolve_main_repo_root,
    clear_main_repo_root_cache,
    probe_main_repo_root,
)
from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.state_machine import MergeCheckoutResolutionError, StateMachine
from tianluo.engine.steps.commit import (
    _guard_version_race,
    _record_flow_committed_version,
)


# --------------------------------------------------------------------------- #
# config.probe_main_repo_root — distinguishes probe failure from non-worktree
# --------------------------------------------------------------------------- #

class TestProbeMainRepoRootStrictness:
    def test_non_worktree_returns_none(self, tmp_path, monkeypatch):
        """Probe SUCCEEDS and common-dir == git-dir → not a worktree → None."""
        clear_main_repo_root_cache()

        def _fake_run(args, **_kwargs):
            # rev-parse --git-common-dir --git-dir on a plain repo emits the same
            # path twice.
            return subprocess.CompletedProcess(
                args, returncode=0, stdout=".git\n.git\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert probe_main_repo_root(tmp_path.resolve()) is None

    def test_git_missing_raises(self, tmp_path, monkeypatch):
        """git not installed is a genuine failure, not a non-worktree."""
        def _raise(*_a, **_k):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(subprocess, "run", _raise)
        with pytest.raises(MainRepoProbeError):
            probe_main_repo_root(tmp_path.resolve())

    def test_nonzero_exit_raises(self, tmp_path, monkeypatch):
        def _fake_run(args, **_kwargs):
            return subprocess.CompletedProcess(
                args, returncode=128, stdout="", stderr="not a git repository"
            )

        monkeypatch.setattr(subprocess, "run", _fake_run)
        with pytest.raises(MainRepoProbeError):
            probe_main_repo_root(tmp_path.resolve())

    def test_unparseable_output_raises(self, tmp_path, monkeypatch):
        """A path with an embedded newline (>2 lines) can't be parsed safely —
        must surface as a failure, NOT be treated as a non-worktree.
        """
        def _fake_run(args, **_kwargs):
            return subprocess.CompletedProcess(
                args, returncode=0, stdout="a\nb\nc\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", _fake_run)
        with pytest.raises(MainRepoProbeError):
            probe_main_repo_root(tmp_path.resolve())

    def test_worktree_but_toplevel_fails_raises(self, tmp_path, monkeypatch):
        """A worktree is detected but its main working tree cannot be derived —
        genuine failure (must not fall back to the worktree).
        """
        fake_common = tmp_path / "main_repo" / ".git"
        fake_git = tmp_path / "worktree" / ".git" / "worktrees" / "wt"
        fake_common.mkdir(parents=True)
        fake_git.mkdir(parents=True)

        calls = [0]

        def _fake_run(args, **_kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return subprocess.CompletedProcess(
                    args, returncode=0,
                    stdout=f"{fake_common}\n{fake_git}\n", stderr="",
                )
            return subprocess.CompletedProcess(
                args, returncode=128, stdout="", stderr="not a working tree"
            )

        monkeypatch.setattr(subprocess, "run", _fake_run)
        with pytest.raises(MainRepoProbeError):
            probe_main_repo_root(tmp_path.resolve())

    def test_resolve_wrapper_still_swallows_to_none(self, tmp_path, monkeypatch):
        """Backward-compat: the lenient wrapper keeps returning None on failure
        so existing callers (config-path lookup) are unaffected.
        """
        clear_main_repo_root_cache()

        def _raise(*_a, **_k):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(subprocess, "run", _raise)
        assert _resolve_main_repo_root(tmp_path) is None


# --------------------------------------------------------------------------- #
# StateMachine._resolve_main_checkout_root — strict for worktree merge steps
# --------------------------------------------------------------------------- #

class TestMergeCheckoutResolutionStrict:
    def test_probe_failure_raises_typed_error(self, tmp_path, monkeypatch):
        sm = StateMachine(tmp_path)

        def _raise(_root):
            raise MainRepoProbeError("boom")

        monkeypatch.setattr(cfg, "probe_main_repo_root", _raise)
        with pytest.raises(MergeCheckoutResolutionError):
            sm._resolve_main_checkout_root()

    def test_non_worktree_resolves_to_project_root(self, tmp_path, monkeypatch):
        """Legitimate None (project_root itself IS the main checkout) → use it,
        no error.
        """
        sm = StateMachine(tmp_path)
        monkeypatch.setattr(cfg, "probe_main_repo_root", lambda _root: None)
        assert sm._resolve_main_checkout_root() == tmp_path.resolve()

    def test_worktree_resolves_to_main(self, tmp_path, monkeypatch):
        main = tmp_path / "main"
        main.mkdir()
        sm = StateMachine(tmp_path)
        monkeypatch.setattr(cfg, "probe_main_repo_root", lambda _root: main)
        assert sm._resolve_main_checkout_root() == main

    def test_creation_tolerates_unresolvable_checkout_but_execution_is_strict(
        self, tmp_path, monkeypatch
    ):
        """Flow creation must not hard-fail on a transient probe fault, but it
        must NOT stash the worktree as the merge checkout; the merge-side step's
        cwd resolution then fails loudly BEFORE the step runs.
        """
        sm = StateMachine(tmp_path)

        def _raise(_root):
            raise MainRepoProbeError("timeout")

        monkeypatch.setattr(cfg, "probe_main_repo_root", _raise)
        flow = sm.create_flow(
            task_description="Add feature",
            task_type="feature",
            is_worktree_mode=True,
        )
        # Creation succeeded, but NO bad (worktree) fallback was stashed.
        assert "merge_checkout_root" not in flow.state.context

        # A merge-side step must not be given the worktree as cwd — resolution
        # is re-attempted strictly and raises rather than returning project_root.
        with pytest.raises(MergeCheckoutResolutionError):
            sm._merge_step_cwd(flow, StepType.MERGE_INTEGRATE)

        # A non-merge step still gets no cwd override (None), never raising.
        assert sm._merge_step_cwd(flow, StepType.COMMIT) is None

    def test_stashed_checkout_used_verbatim_without_reprobe(
        self, tmp_path, monkeypatch
    ):
        """When creation positively resolved and stashed the main checkout, the
        merge step uses it verbatim — no re-probe (stable across the merge step's
        transient project_root rebind).
        """
        main = tmp_path / "main"
        main.mkdir()
        sm = StateMachine(tmp_path)
        monkeypatch.setattr(cfg, "probe_main_repo_root", lambda _root: main)
        flow = sm.create_flow(
            task_description="Add feature",
            task_type="feature",
            is_worktree_mode=True,
        )
        assert flow.state.context["merge_checkout_root"] == str(main)
        assert sm._merge_step_cwd(flow, StepType.VERSION_RECONCILE) == str(main)

    def test_merge_step_without_cwd_still_acquires_lock(self, tmp_path, monkeypatch):
        """A merge-side step whose persisted header LOST its cwd override still
        mutates master via the handler's strict fallback, so it must NOT run
        unserialised: the override manager resolves the main checkout and
        acquires the merge lock regardless of ``step.cwd`` presence. Keying the
        lock on ``step.cwd`` (the old behaviour) would let a corrupted /
        reconstructed merge step race a concurrent ``se3 merge`` on master."""
        from unittest.mock import MagicMock

        main = tmp_path / "main"
        main.mkdir()
        sm = StateMachine(tmp_path)
        monkeypatch.setattr(cfg, "probe_main_repo_root", lambda _root: main)
        flow = sm.create_flow(
            task_description="Add feature",
            task_type="feature",
            is_worktree_mode=True,
        )

        step = Step(step_type=StepType.MERGE_INTEGRATE)
        step.cwd = None  # header lost the override (corrupted / reconstructed)

        fake_lock = MagicMock()
        acquired: list = []
        monkeypatch.setattr(
            "tianluo.commands.merge.merge_lock.is_lock_held_in_process",
            lambda root: False,
        )
        monkeypatch.setattr(
            "tianluo.commands.merge.merge_lock.MergeLock",
            lambda root, blocking=True: fake_lock,
        )
        monkeypatch.setattr(
            sm, "_acquire_merge_step_lock",
            lambda lock, f, s, r: acquired.append((lock, sm.project_root)),
        )

        with sm._step_cwd_override(flow, step):
            # Rebound to the strictly-resolved MAIN checkout inside the override.
            assert sm.project_root == main

        # The lock WAS acquired (serialised), and released on exit.
        assert acquired and acquired[0][0] is fake_lock
        fake_lock.release.assert_called_once()

    def test_non_merge_step_without_cwd_takes_no_lock(self, tmp_path, monkeypatch):
        """An ordinary (non-merge) step with no cwd yields unchanged and holds no
        lock — the overwhelmingly common path is untouched by the merge-step
        lock rule."""
        from unittest.mock import MagicMock

        sm = StateMachine(tmp_path)
        step = Step(step_type=StepType.COMMIT)
        step.cwd = None

        boom = MagicMock(side_effect=AssertionError("must not lock a non-merge step"))
        monkeypatch.setattr("tianluo.commands.merge.merge_lock.MergeLock", boom)

        with sm._step_cwd_override(flow=sm.create_flow(
            task_description="x", task_type="feature",
        ), step=step):
            # project_root untouched (no rebind for a plain step).
            assert sm.project_root == tmp_path


# --------------------------------------------------------------------------- #
# Version race guard — durable own-replay marker (script mode, no Version: line)
# --------------------------------------------------------------------------- #

class TestScriptModeOwnReplayGuard:
    def _flow_with_analyze(self, *, baseline: str, suggested: str) -> FlowInstance:
        flow = FlowInstance(task_description="Add feature")
        flow.is_worktree_mode = False
        va = Step(step_type=StepType.VERSION_ANALYZE)
        va.status = StepStatus.COMPLETED
        va.outputs["current_version"] = baseline
        va.outputs["suggested_version"] = suggested
        va.inputs["pre_session_version"] = baseline
        flow.state.add_step(va)
        return flow

    def test_record_then_guard_treats_as_replay(self):
        """Disk drifted 5.1.0 -> 5.2.0, but 5.2.0 is exactly what THIS flow
        recorded committing earlier → own replay, target returned verbatim,
        no re-analysis — works without any git repo / version file (script mode
        + include_in_commit_message: false).
        """
        flow = self._flow_with_analyze(baseline="5.1.0", suggested="5.2.0")
        # Simulate the prior successful commit recording its own bump.
        _record_flow_committed_version(flow, "5.2.0")

        step = Step(step_type=StepType.COMMIT)
        step.inputs["pre_session_version"] = "5.1.0"

        result = _guard_version_race(
            step, flow, disk_version="5.2.0", target_version="5.2.0",
            version_file=None, version_bumper=None,
        )
        assert result == "5.2.0"

    def test_marker_mismatch_is_not_a_replay(self, monkeypatch):
        """A durable marker for a DIFFERENT version than what is on disk must NOT
        be treated as this flow's replay — the guard must fall through to the
        drift path (here stubbed) rather than keep the stale target.
        """
        flow = self._flow_with_analyze(baseline="5.1.0", suggested="5.2.0")
        _record_flow_committed_version(flow, "5.2.0")  # we committed 5.2.0...

        step = Step(step_type=StepType.COMMIT)
        step.inputs["pre_session_version"] = "5.1.0"

        called = {}

        # Stub the re-analysis so no real LLM/repo is needed; assert we reach it.
        import tianluo.engine.steps.commit as commit_mod

        def _fake_reanalyze(_step, _flow, new_baseline):
            called["baseline"] = new_baseline
            return "9.10.0"

        monkeypatch.setattr(commit_mod, "_reanalyze_version_with_baseline",
                            _fake_reanalyze)

        # ...but disk is 9.9.9 (a concurrent flow), which our marker does NOT
        # match → drift path, not replay.
        result = _guard_version_race(
            step, flow, disk_version="9.9.9", target_version="5.2.0",
            version_file=None, version_bumper=None,
        )
        assert called["baseline"] == "9.9.9"
        assert result == "9.10.0"


# --------------------------------------------------------------------------- #
# _acquire_merge_step_lock — exception-symmetric waiting-flag cleanup
# --------------------------------------------------------------------------- #

class TestAcquireMergeStepLockWaitingCleanup:
    """The contention path persists ``waiting_for_lock=True`` before the blocking
    acquire. If the blocking acquire itself faults (OSError/stale-lock surfacing
    mid-wait, or a Ctrl+C), the flag must be cleared + re-persisted before the
    exception propagates — otherwise a dead process leaves engine.json with
    status=running + waiting_for_lock=True and the web console renders a stale
    "等待主分支锁" badge forever. Cleanup must be symmetric across ALL exception
    types, not just KeyboardInterrupt."""

    def _busy_then_raise_lock(self, exc):
        from tianluo.commands.merge.merge_lock import MergeLockBusy

        class _Lock:
            def __init__(self):
                self.blocking_attempted = False

            def acquire(self, blocking=False, break_stale=False):
                if not blocking:
                    # Non-blocking probe: genuinely held by someone else → drive
                    # the caller onto the contention (wait) path.
                    raise MergeLockBusy("held")
                self.blocking_attempted = True
                raise exc

        return _Lock()

    @pytest.mark.parametrize(
        "exc",
        [OSError("flock faulted mid-wait"), KeyboardInterrupt()],
        ids=["oserror", "keyboardinterrupt"],
    )
    def test_blocking_acquire_failure_clears_waiting_flag(
        self, tmp_path, monkeypatch, exc
    ):
        sm = StateMachine(tmp_path)
        flow = sm.create_flow(
            task_description="Add feature",
            task_type="feature",
            is_worktree_mode=True,
        )
        step = Step(step_type=StepType.MERGE_INTEGRATE)

        # Silence the streaming history anchors — bookkeeping is best-effort and
        # not what this test exercises.
        import tianluo.engine.chat_history as ch
        monkeypatch.setattr(ch, "record_waiting_for_lock", lambda **kw: None)
        monkeypatch.setattr(ch, "record_lock_acquired", lambda **kw: None)

        lock = self._busy_then_raise_lock(exc)

        with pytest.raises(type(exc)):
            sm._acquire_merge_step_lock(lock, flow, step, tmp_path)

        assert lock.blocking_attempted
        # Flag cleared in memory AND persisted, for every exception type.
        assert flow.waiting_for_lock is False
        reloaded = sm.persistence.load_flow_by_id(flow.flow_id)
        assert reloaded is not None
        assert reloaded.waiting_for_lock is False
