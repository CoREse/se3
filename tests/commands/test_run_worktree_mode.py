"""Tests for `se3 run --worktree` isolation mode (run.py).

Covers:
- the main-worktree lock acquired by a synchronous run (acquire_main_lock),
- run_worktree_mode orchestration (fork → run_flow → merge-back / preserve),
- discovery + resume of an interrupted worktree run.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import se3.commands.run as run


# --------------------------------------------------------------------------
# branch-name helpers
# --------------------------------------------------------------------------
class TestBranchNameHelpers:
    def test_slugify_basic(self):
        assert run._slugify_for_branch("Implement User Login!") == "implement-user-login"

    def test_slugify_collapses_and_trims(self):
        assert run._slugify_for_branch("  --A___B  ") == "a-b"

    def test_slugify_empty_falls_back_to_task(self):
        assert run._slugify_for_branch("") == "task"
        assert run._slugify_for_branch("!!!") == "task"

    def test_slugify_truncates_to_30(self):
        slug = run._slugify_for_branch("x" * 100)
        assert len(slug) <= 30

    def test_generate_worktree_branch_name_shape(self):
        name = run._generate_worktree_branch_name("Fix the bug")
        assert name.startswith("worktree/fix-the-bug-")
        # no slashes beyond the single prefix separator
        assert name.count("/") == 1


# --------------------------------------------------------------------------
# run_flow main-worktree lock
# --------------------------------------------------------------------------
class TestRunFlowMainLock:
    @patch("se3.commands.run._resolve_main_lock_root", return_value=Path("/main"))
    @patch("se3.commands.run._run_flow_impl", return_value=0)
    @patch("se3.commands.run.StateMachine")
    @patch("se3.commands.run.PersistenceManager")
    @patch("se3.commands.merge.merge_lock.MergeLock")
    def test_sync_run_acquires_and_releases_blocking_lock(
        self, MockLock, _MockPersist, _MockSM, mock_impl, mock_resolve
    ):
        lock = MockLock.return_value
        rc = run.run_flow(
            project_root=Path("/repo"),
            task_description="do it",
            acquire_main_lock=True,
        )
        assert rc == 0
        # Lock targets the resolved MAIN repo root, blocking acquisition, and
        # is released on exit.
        MockLock.assert_called_once_with(Path("/main"))
        lock.acquire.assert_called_once_with(blocking=True)
        lock.release.assert_called_once()

    @patch("se3.commands.run._run_flow_impl", return_value=0)
    @patch("se3.commands.run.StateMachine")
    @patch("se3.commands.run.PersistenceManager")
    @patch("se3.commands.merge.merge_lock.MergeLock")
    def test_worktree_body_takes_no_lock(
        self, MockLock, _MockPersist, _MockSM, mock_impl
    ):
        rc = run.run_flow(
            project_root=Path("/wt"),
            task_description="do it",
            acquire_main_lock=False,
        )
        assert rc == 0
        MockLock.assert_not_called()

    @patch("se3.commands.run._resolve_main_lock_root", return_value=Path("/main"))
    @patch("se3.commands.run._run_flow_impl")
    @patch("se3.commands.run.StateMachine")
    @patch("se3.commands.run.PersistenceManager")
    @patch("se3.commands.merge.merge_lock.MergeLock")
    def test_lock_released_even_when_impl_raises(
        self, MockLock, _MockPersist, _MockSM, mock_impl, _mock_resolve
    ):
        lock = MockLock.return_value
        mock_impl.side_effect = RuntimeError("boom")
        try:
            run.run_flow(
                project_root=Path("/repo"),
                task_description="do it",
                acquire_main_lock=True,
            )
        except RuntimeError:
            pass
        lock.release.assert_called_once()


# --------------------------------------------------------------------------
# run_worktree_mode orchestration
# --------------------------------------------------------------------------
class TestRunWorktreeMode:
    @patch("se3.commands.run._worktree_flow_status", return_value="completed")
    @patch("se3.commands.merge_cmd.run_merge", return_value=0)
    @patch("se3.commands.run.run_flow", return_value=0)
    @patch("se3.engine.worktree.fork_worktree", return_value=Path("/repo/se3/worktrees/worktree-x"))
    @patch("se3.engine.worktree.get_current_branch", return_value="main")
    @patch("se3.commands.run.clear_main_repo_root_cache")
    def test_success_merges_back(
        self, _mock_clear, mock_branch, mock_fork, mock_run_flow, mock_merge,
        _mock_status,
    ):
        rc = run.run_worktree_mode(
            project_root=Path("/repo"),
            task="Add feature",
            task_type="feature",
        )
        assert rc == 0
        # fork a worktree off the current branch
        mock_fork.assert_called_once()
        args, _ = mock_fork.call_args
        assert args[0] == Path("/repo")
        assert args[1] == "main"
        wt_branch = args[2]
        assert wt_branch.startswith("worktree/")
        # run_flow runs IN the worktree, lock-free, in worktree mode
        _, kwargs = mock_run_flow.call_args
        assert kwargs["project_root"] == Path("/repo/se3/worktrees/worktree-x")
        assert kwargs["acquire_main_lock"] is False
        assert kwargs["is_worktree_mode"] is True
        assert kwargs["worktree_branch"] == wt_branch
        assert kwargs["worktree_original_branch"] == "main"
        # success → merge the isolation branch back into main repo
        mock_merge.assert_called_once_with(branches=[wt_branch], project_root=Path("/repo"))

    @patch("se3.commands.merge_cmd.run_merge")
    @patch("se3.commands.run.run_flow", return_value=2)
    @patch("se3.engine.worktree.fork_worktree", return_value=Path("/repo/se3/worktrees/wt"))
    @patch("se3.engine.worktree.get_current_branch", return_value="main")
    @patch("se3.commands.run.clear_main_repo_root_cache")
    def test_failure_preserves_and_skips_merge(
        self, _mock_clear, _mock_branch, _mock_fork, _mock_run_flow, mock_merge
    ):
        rc = run.run_worktree_mode(project_root=Path("/repo"), task="Add feature")
        assert rc == 2
        mock_merge.assert_not_called()

    @patch("se3.commands.run._worktree_flow_status", return_value="PAUSED")
    @patch("se3.commands.merge_cmd.run_merge")
    @patch("se3.commands.run.run_flow", return_value=0)
    @patch("se3.engine.worktree.fork_worktree", return_value=Path("/repo/se3/worktrees/wt"))
    @patch("se3.engine.worktree.get_current_branch", return_value="main")
    @patch("se3.commands.run.clear_main_repo_root_cache")
    def test_paused_json_flow_skips_merge(
        self, _mock_clear, _mock_branch, _mock_fork, _mock_run_flow, mock_merge,
        _mock_status,
    ):
        """A daemon-spawned --worktree --discover run that PAUSES returns 0 from
        run_flow (json mode), but its on-disk status is PAUSED, not COMPLETED.
        No merge must be attempted — otherwise the branch is deleted and the
        worktree (engine.json + call files) archived, losing the paused flow."""
        rc = run.run_worktree_mode(project_root=Path("/repo"), task="Add feature")
        assert rc == 0
        mock_merge.assert_not_called()

    @patch("se3.engine.worktree.fork_worktree", side_effect=RuntimeError("git fail"))
    @patch("se3.engine.worktree.get_current_branch", return_value="main")
    def test_worktree_creation_failure_returns_error(
        self, _mock_branch, _mock_fork
    ):
        rc = run.run_worktree_mode(project_root=Path("/repo"), task="x")
        assert rc == 1


# --------------------------------------------------------------------------
# worktree-run discovery + resume
# --------------------------------------------------------------------------
def _write_worktree_engine_json(root: Path, *, flow_id, status, is_worktree_mode=True,
                                branch="worktree/x-1", original="main"):
    state_dir = root / "se3" / "worktrees" / "worktree-x-1" / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "flow_id": flow_id,
        "status": status,
        "task_description": "isolated task",
        "is_worktree_mode": is_worktree_mode,
        "worktree_branch": branch,
        "worktree_original_branch": original,
        "worktree_path": str(root / "se3" / "worktrees" / "worktree-x-1"),
        "state": {"current_step_id": "implement"},
    }
    (state_dir / "engine.json").write_text(json.dumps(data))
    return data


class TestWorktreeRunDiscovery:
    def test_find_resumable_worktree_runs(self, tmp_path):
        _write_worktree_engine_json(tmp_path, flow_id="wt-1", status="failed")
        runs = run.find_resumable_worktree_runs(tmp_path)
        assert len(runs) == 1
        r = runs[0]
        assert r["id"] == "wt-1"
        assert r["is_worktree_run"] is True
        assert r["worktree_branch"] == "worktree/x-1"
        assert r["worktree_original_branch"] == "main"

    def test_completed_worktree_run_excluded(self, tmp_path):
        _write_worktree_engine_json(tmp_path, flow_id="wt-1", status="completed")
        assert run.find_resumable_worktree_runs(tmp_path) == []

    def test_non_worktree_engine_json_ignored(self, tmp_path):
        _write_worktree_engine_json(
            tmp_path, flow_id="wt-1", status="failed", is_worktree_mode=False
        )
        assert run.find_resumable_worktree_runs(tmp_path) == []

    def test_no_worktrees_dir(self, tmp_path):
        assert run.find_resumable_worktree_runs(tmp_path) == []


class TestResumeRun:
    @patch("se3.commands.run.run_flow", return_value=0)
    def test_resume_main_repo_flow(self, mock_run_flow, tmp_path):
        # No worktree runs → plain main-repo resume (lock acquired by default).
        rc = run.resume_run(tmp_path, "main-flow", output_format="cli")
        assert rc == 0
        _, kwargs = mock_run_flow.call_args
        assert kwargs["flow_id"] == "main-flow"
        assert kwargs["project_root"] == tmp_path
        # acquire_main_lock not overridden → default True (a sync resume)
        assert "acquire_main_lock" not in kwargs or kwargs["acquire_main_lock"] is True

    @patch("se3.commands.run._worktree_flow_status", return_value="completed")
    @patch("se3.commands.run._finalize_worktree_merge", return_value=0)
    @patch("se3.commands.run.run_flow", return_value=0)
    def test_resume_worktree_run_then_merge(
        self, mock_run_flow, mock_merge, _mock_status, tmp_path
    ):
        _write_worktree_engine_json(tmp_path, flow_id="wt-1", status="failed")
        rc = run.resume_run(tmp_path, "wt-1", output_format="cli")
        assert rc == 0
        # flow body re-dispatched in the worktree, lock-free
        _, kwargs = mock_run_flow.call_args
        assert kwargs["acquire_main_lock"] is False
        assert kwargs["flow_id"] == "wt-1"
        assert Path(kwargs["project_root"]).name == "worktree-x-1"
        # success → trailing merge back
        mock_merge.assert_called_once_with(tmp_path, "worktree/x-1", "main")

    @patch("se3.commands.run._finalize_worktree_merge")
    @patch("se3.commands.run.run_flow", return_value=3)
    def test_resume_worktree_run_failure_skips_merge(
        self, mock_run_flow, mock_merge, tmp_path
    ):
        _write_worktree_engine_json(tmp_path, flow_id="wt-1", status="failed")
        rc = run.resume_run(tmp_path, "wt-1", output_format="cli")
        assert rc == 3
        mock_merge.assert_not_called()

    @patch("se3.commands.run._finalize_worktree_merge")
    @patch("se3.commands.run.run_flow", return_value=0)
    def test_resume_worktree_run_paused_again_skips_merge(
        self, mock_run_flow, mock_merge, tmp_path
    ):
        """Resuming a worktree run that PAUSES again (json mode) returns 0 from
        run_flow, but the on-disk status stays PAUSED — no merge must fire, or
        the worktree would be archived mid-resume and the flow lost."""
        _write_worktree_engine_json(tmp_path, flow_id="wt-1", status="paused")
        rc = run.resume_run(tmp_path, "wt-1", output_format="cli")
        assert rc == 0
        mock_merge.assert_not_called()

    @patch("se3.commands.run._worktree_flow_status", return_value="completed")
    @patch("se3.commands.run._resolve_main_lock_root", return_value=Path("/main"))
    @patch("se3.commands.run._finalize_worktree_merge", return_value=0)
    @patch("se3.commands.run.run_flow", return_value=0)
    def test_resume_when_project_root_is_the_worktree_itself(
        self, mock_run_flow, mock_merge, mock_resolve, _mock_status, tmp_path
    ):
        """Daemon resumes a worktree run with cwd set to the worktree itself.

        The flow body must still run lock-free in the worktree, and the merge
        must be driven from the resolved MAIN repo (not from inside the
        worktree).
        """
        # The worktree dir IS the project_root here; its own engine.json is the
        # worktree-mode flow (one level deeper than find_resumable scans).
        wt_root = tmp_path / "se3" / "worktrees" / "worktree-x-1"
        state_dir = wt_root / "se3" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "engine.json").write_text(
            json.dumps(
                {
                    "flow_id": "wt-1",
                    "status": "paused",
                    "task_description": "isolated task",
                    "is_worktree_mode": True,
                    "worktree_branch": "worktree/x-1",
                    "worktree_original_branch": "main",
                    "worktree_path": str(wt_root),
                    "state": {"current_step_id": "implement"},
                }
            )
        )

        rc = run.resume_run(wt_root, "wt-1", output_format="cli")
        assert rc == 0
        # Body re-dispatched lock-free in the worktree itself.
        _, kwargs = mock_run_flow.call_args
        assert kwargs["acquire_main_lock"] is False
        assert kwargs["flow_id"] == "wt-1"
        assert Path(kwargs["project_root"]) == wt_root
        # Merge driven from the resolved MAIN repo, not the worktree.
        mock_merge.assert_called_once_with(Path("/main"), "worktree/x-1", "main")

    @patch("se3.commands.run.run_flow", return_value=0)
    def test_self_worktree_run_ignores_completed(self, mock_run_flow, tmp_path):
        """A COMPLETED worktree engine.json at project_root is not self-resumed."""
        wt_root = tmp_path / "se3" / "worktrees" / "worktree-x-1"
        state_dir = wt_root / "se3" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "engine.json").write_text(
            json.dumps(
                {
                    "flow_id": "wt-1",
                    "status": "completed",
                    "is_worktree_mode": True,
                    "worktree_branch": "worktree/x-1",
                    "worktree_original_branch": "main",
                    "worktree_path": str(wt_root),
                }
            )
        )
        # Falls through to a plain main-repo resume (no self-worktree routing).
        rc = run.resume_run(wt_root, "wt-1", output_format="cli")
        assert rc == 0
        _, kwargs = mock_run_flow.call_args
        assert kwargs["project_root"] == wt_root
        assert "acquire_main_lock" not in kwargs or kwargs["acquire_main_lock"] is True
