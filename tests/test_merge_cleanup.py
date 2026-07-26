"""Tests for CleanupManager (--delete-merged flag)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tianluo.engine.merge.cleanup import (
    CleanupManager,
    CleanupReport,
    _get_worktree_path_for_branch,
    _is_worktree_clean,
)


def _init_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("# Test\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )


def _add_commit(path: Path, filename: str, content: str, message: str) -> None:
    """Add a file and commit."""
    (path / filename).write_text(content)
    subprocess.run(["git", "-C", str(path), "add", filename], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", message],
        check=True, capture_output=True,
    )


def _create_branch(path: Path, branch: str) -> None:
    """Create a new branch from current HEAD."""
    subprocess.run(
        ["git", "-C", str(path), "checkout", "-b", branch],
        check=True, capture_output=True,
    )


def _get_default_branch(path: Path) -> str:
    """Get the current branch name."""
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _branch_exists(path: Path, branch: str) -> bool:
    """Check if a local branch exists."""
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--verify", branch],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


def _create_worktree(path: Path, branch: str, wt_dir: Path) -> None:
    """Create a git worktree for *branch* at *wt_dir*."""
    subprocess.run(
        ["git", "-C", str(path), "worktree", "add", str(wt_dir), branch],
        check=True, capture_output=True,
    )


class TestGetWorktreePathForBranch:
    def test_no_worktree_returns_none(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        assert _get_worktree_path_for_branch(tmp_path, "feature") is None

    def test_finds_worktree_for_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        wt_dir = tmp_path / "wt_feature"
        _create_worktree(tmp_path, "feature", wt_dir)

        found = _get_worktree_path_for_branch(tmp_path, "feature")
        assert found is not None
        assert str(found) == str(wt_dir)


class TestIsWorktreeClean:
    def test_clean_worktree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        assert _is_worktree_clean(tmp_path) is True

    def test_dirty_worktree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "README.md").write_text("dirty readme")
        assert _is_worktree_clean(tmp_path) is False


class TestCleanupManagerDeleteMergedBranches:
    def test_deletes_merged_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        # Merge feature into default
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )
        assert _branch_exists(tmp_path, "feature") is True

        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["feature"])

        assert report.deleted == ["feature"]
        assert report.skipped_dirty == []
        assert report.skipped_protected == []
        assert report.skipped_not_merged == []
        assert _branch_exists(tmp_path, "feature") is False

    def test_skips_not_fully_merged_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        # Do NOT merge feature
        assert _branch_exists(tmp_path, "feature") is True

        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["feature"])

        assert report.deleted == []
        assert report.skipped_not_merged != []
        assert report.skipped_not_merged[0][0] == "feature"
        assert _branch_exists(tmp_path, "feature") is True

    def test_skips_protected_main(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        # Rename current branch to main
        subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "-m", "main"],
            check=True, capture_output=True,
        )
        # Create a secondary branch from main
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "main"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )

        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["main"])

        assert report.deleted == []
        assert report.skipped_protected == ["main"]
        assert _branch_exists(tmp_path, "main") is True

    def test_skips_protected_master(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "-m", "master"],
            check=True, capture_output=True,
        )
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "master"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )

        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["master"])

        assert report.deleted == []
        assert report.skipped_protected == ["master"]

    def test_skips_current_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        # Merge feature
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )
        # Now default contains feature; try deleting default itself
        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches([default])

        assert report.deleted == []
        assert report.skipped_protected == [default]
        assert _branch_exists(tmp_path, default) is True

    def test_deletes_branch_with_clean_worktree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        # Create worktree for feature
        wt_dir = tmp_path / "wt_feature"
        _create_worktree(tmp_path, "feature", wt_dir)
        # Merge feature into default
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )

        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["feature"])

        assert report.deleted == ["feature"]
        assert report.skipped_dirty == []
        assert _branch_exists(tmp_path, "feature") is False
        # Worktree directory should be removed
        assert not wt_dir.exists()

    def test_skips_dirty_worktree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        # Create worktree for feature
        wt_dir = tmp_path / "wt_feature"
        _create_worktree(tmp_path, "feature", wt_dir)
        # Dirty the worktree (modify a tracked file)
        (wt_dir / "feat.txt").write_text("modified uncommitted")
        # Merge feature into default
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )

        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["feature"])

        assert report.deleted == []
        assert len(report.skipped_dirty) == 1
        assert report.skipped_dirty[0][0] == "feature"
        assert "uncommitted" in report.skipped_dirty[0][1].lower() or "dirty" in report.skipped_dirty[0][1].lower()
        # Branch and worktree must both survive
        assert _branch_exists(tmp_path, "feature") is True
        assert wt_dir.exists()

    def test_multiple_branches_mixed(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        # Merge both
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature-a", "--no-edit"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature-b", "--no-edit"],
            check=True, capture_output=True,
        )

        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["feature-a", "feature-b"])

        assert set(report.deleted) == {"feature-a", "feature-b"}
        assert report.skipped_dirty == []
        assert report.skipped_protected == []

    def test_cleanup_report_empty(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches([])

        assert report.deleted == []
        assert report.skipped_dirty == []
        assert report.skipped_protected == []
        assert report.skipped_not_merged == []

    def test_retry_success_path_calls_metadata_cleanup(self, tmp_path: Path, monkeypatch) -> None:
        """When git branch -d fails because the branch is checked out in a
        worktree, the retry path (remove worktree then retry branch -d) must
        also call _cleanup_git_worktree_metadata.

        Regression test for the retry-success branch skipping metadata cleanup.
        """
        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        # Merge feature into default
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )

        # Mock exists_for_branch so the retry path is considered
        monkeypatch.setattr(
            "tianluo.engine.merge.cleanup.exists_for_branch",
            lambda project_root, branch: True,
        )
        fake_wt_path = tmp_path / "fake_wt"
        monkeypatch.setattr(
            "tianluo.engine.merge.cleanup._get_worktree_path_for_branch",
            lambda project_root, branch: fake_wt_path,
        )

        call_log = []

        def tracked_run_git(project_root, *args, check=True, timeout=30):
            call_log.append(args)
            # First branch -d call: simulate "checked out" failure
            if len(args) >= 3 and args[0] == "branch" and args[1] == "-d" and args[2] == "feature":
                branch_d_calls = [c for c in call_log if len(c) >= 3 and c[:3] == ("branch", "-d", "feature")]
                if len(branch_d_calls) == 1:
                    class FakeResult:
                        returncode = 1
                        stdout = ""
                        stderr = "error: Cannot delete branch 'feature' checked out at /some/path"
                    return FakeResult()
            # worktree remove: succeed
            if len(args) >= 2 and args[0] == "worktree" and args[1] == "remove":
                class FakeResult:
                    returncode = 0
                    stdout = ""
                    stderr = ""
                return FakeResult()
            # Fallback to real git for everything else
            import tianluo.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "tianluo.engine.merge.cleanup._run_git", tracked_run_git
        )

        metadata_cleaned = []

        def track_metadata_cleanup(project_root, branch):
            metadata_cleaned.append(branch)

        monkeypatch.setattr(
            "tianluo.engine.merge.cleanup._cleanup_git_worktree_metadata",
            track_metadata_cleanup,
        )

        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["feature"])

        # Should succeed via retry path
        assert report.deleted == ["feature"]
        # Metadata cleanup must have been called for the retry-success path
        assert "feature" in metadata_cleaned

    def test_deletes_branch_with_externally_removed_worktree(self, tmp_path: Path) -> None:
        """Worktree directory removed externally but .git/worktrees metadata survives.

        Regression test: _is_worktree_clean must return True when the worktree
        path no longer exists, so that delete_merged_branches proceeds to
        delete the branch and scrub the stale metadata.
        """
        import shutil

        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        # Create worktree for feature
        wt_dir = tmp_path / "wt_feature"
        _create_worktree(tmp_path, "feature", wt_dir)
        # Merge feature into default
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )
        # Externally remove the worktree directory (simulates manual deletion
        # or filesystem issue) while leaving .git/worktrees metadata intact.
        shutil.rmtree(wt_dir)
        assert not wt_dir.exists()
        # Verify metadata still exists before cleanup
        metadata_dir = tmp_path / ".git" / "worktrees" / "wt_feature"
        assert metadata_dir.exists()

        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["feature"])

        # Should delete the branch despite missing worktree directory
        assert report.deleted == ["feature"]
        assert report.skipped_dirty == []
        assert _branch_exists(tmp_path, "feature") is False
        # Stale metadata should be scrubbed
        assert not metadata_dir.exists()

    def test_retry_branch_delete_after_worktree_removal_cleans_metadata(
        self, tmp_path: Path
    ) -> None:
        """Branch checked out in a worktree forces retry path; metadata is scrubbed.

        Regression test: when ``git branch -d`` fails with "checked out" because the
        branch is bound to a worktree, CleanupManager removes the worktree, retries
        branch deletion, and must also call ``_cleanup_git_worktree_metadata`` so
        the stale ``.git/worktrees/<safe_name>`` directory is removed.
        """
        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        # Merge feature into default so the branch is fully merged
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )

        # Create a worktree for the feature branch — this checks it out
        wt_dir = tmp_path / "wt_feature"
        _create_worktree(tmp_path, "feature", wt_dir)

        # The metadata directory should exist before cleanup
        metadata_dir = tmp_path / ".git" / "worktrees" / "wt_feature"
        assert metadata_dir.exists()

        # Now try to delete the merged branch. Because the branch is still
        # checked out in the worktree, ``git branch -d`` will fail on the
        # first attempt. CleanupManager removes the worktree and retries.
        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["feature"])

        # Should succeed via the retry path
        assert report.deleted == ["feature"]
        assert report.skipped_dirty == []
        assert report.skipped_worktree_remove_failed == []
        assert report.skipped_not_merged == []
        assert _branch_exists(tmp_path, "feature") is False

        # Worktree directory must be gone
        assert not wt_dir.exists()
        # Stale metadata must also be scrubbed
        assert not metadata_dir.exists()


def _write_worktree_engine_json(
    wt_path: Path, flow_id: str, status: str = "completed"
) -> Path:
    """Write a worktree-mode ``engine.json`` under *wt_path*."""
    state_dir = wt_path / "tianluo" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    engine = state_dir / "engine.json"
    engine.write_text(
        json.dumps(
            {
                "flow_id": flow_id,
                "status": status,
                "task_description": "do the thing",
                "task_type": "feature",
                "is_worktree_mode": True,
                "created_at": "2026-06-14T10:00:00",
                "updated_at": "2026-06-14T10:30:00",
                "state": {"selected_steps": ["analyze", "plan"]},
            }
        ),
        encoding="utf-8",
    )
    return engine


class TestPromoteCompletedEngineState:
    """G7: promote a worktree's COMPLETED engine.json into the main archive."""

    def test_promotes_completed_state(self, tmp_path: Path) -> None:
        from tianluo.engine.merge.cleanup import _promote_completed_engine_state

        project_root = tmp_path / "main"
        project_root.mkdir()
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        _write_worktree_engine_json(wt_path, "flow-abc", status="completed")

        promoted = _promote_completed_engine_state(project_root, wt_path)

        assert promoted is not None
        assert promoted.name == "engine_flow-abc.json"
        archive = project_root / "tianluo" / "state" / "archive" / "engine_flow-abc.json"
        assert archive.exists()
        data = json.loads(archive.read_text(encoding="utf-8"))
        assert data["flow_id"] == "flow-abc"
        assert data["status"] == "completed"
        assert data["task_description"] == "do the thing"
        # The promoted snapshot is re-stamped with the MAIN project root so
        # the daemon's historical-root enumeration attributes it correctly.
        assert Path(data["project_root"]).resolve() == project_root.resolve()

    def test_skips_non_completed_status(self, tmp_path: Path) -> None:
        from tianluo.engine.merge.cleanup import _promote_completed_engine_state

        project_root = tmp_path / "main"
        project_root.mkdir()
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        _write_worktree_engine_json(wt_path, "flow-abc", status="failed")

        promoted = _promote_completed_engine_state(project_root, wt_path)

        assert promoted is None
        assert not (project_root / "tianluo" / "state" / "archive").exists()

    def test_skips_missing_engine_json(self, tmp_path: Path) -> None:
        from tianluo.engine.merge.cleanup import _promote_completed_engine_state

        project_root = tmp_path / "main"
        project_root.mkdir()
        wt_path = tmp_path / "wt"
        wt_path.mkdir()

        assert _promote_completed_engine_state(project_root, wt_path) is None

    def test_skips_missing_flow_id(self, tmp_path: Path) -> None:
        from tianluo.engine.merge.cleanup import _promote_completed_engine_state

        project_root = tmp_path / "main"
        project_root.mkdir()
        wt_path = tmp_path / "wt"
        state_dir = wt_path / "tianluo" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "engine.json").write_text(
            json.dumps({"status": "completed"}), encoding="utf-8"
        )

        assert _promote_completed_engine_state(project_root, wt_path) is None

    def test_integration_promotes_before_worktree_deletion(
        self, tmp_path: Path
    ) -> None:
        """``delete_merged_branches`` promotes the COMPLETED state, then deletes
        the worktree, recording the promotion in the report."""
        _init_repo(tmp_path)
        # Gitignore tianluo/ so the worktree's engine.json does not count as dirty.
        (tmp_path / ".gitignore").write_text("tianluo/\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", ".gitignore"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "ignore se3"],
            check=True, capture_output=True,
        )
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        wt_dir = tmp_path / "wt_feature"
        _create_worktree(tmp_path, "feature", wt_dir)
        # The worktree carries a COMPLETED worktree-mode engine.json.
        _write_worktree_engine_json(wt_dir, "flow-xyz", status="completed")
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )

        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["feature"])

        assert report.deleted == ["feature"]
        # Worktree is gone, but the promoted completed-state snapshot survives
        # in the MAIN project's archive.
        assert not wt_dir.exists()
        archive = tmp_path / "tianluo" / "state" / "archive" / "engine_flow-xyz.json"
        assert archive.exists()
        data = json.loads(archive.read_text(encoding="utf-8"))
        assert data["status"] == "completed"
        assert [b for b, _ in report.promoted_states] == ["feature"]


class TestPromoteColdPartition:
    """Issue #244 B5: a new-format worktree flow's cold step/context partition
    must be promoted into the main archive alongside its KB-scale header, so the
    archived flow retains full step inputs/outputs after the worktree is deleted.
    """

    @staticmethod
    def _save_new_format_completed_flow(wt_path: Path) -> str:
        """Save a hot/cold split COMPLETED flow into *wt_path*'s state; return id."""
        from tianluo.engine.models import (
            FlowInstance,
            FlowStatus,
            Step,
            StepStatus,
            StepType,
        )
        from tianluo.engine.persistence import PersistenceManager

        flow = FlowInstance(
            task_description="worktree split-format flow",
            status=FlowStatus.COMPLETED,
        )
        flow.task_type = "feature"
        flow.is_worktree_mode = True
        blob = "Z" * 40_000
        for i in range(4):
            step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED)
            step.inputs = {"test_results": blob, "idx": i}
            step.outputs = {"artifact_blob": blob, "ok": True}
            flow.state.add_step(step)
        flow.state.selected_steps = [StepType.IMPLEMENT]
        flow.state.current_step_id = flow.state.step_history[-1]
        flow.state.context = {"spec_content": blob, "resolved_type": "feature"}
        PersistenceManager(wt_path).save_flow(flow)
        return flow.flow_id

    def test_promotes_cold_partition_with_header(self, tmp_path: Path) -> None:
        from tianluo.engine.merge.cleanup import _promote_completed_engine_state
        from tianluo.engine.persistence import PersistenceManager

        project_root = tmp_path / "main"
        project_root.mkdir()
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        flow_id = self._save_new_format_completed_flow(wt_path)

        promoted = _promote_completed_engine_state(project_root, wt_path)
        assert promoted is not None

        # The cold partition (per-step inputs/outputs + _context.json) is copied
        # into the MAIN project's archive/steps/<flow_id>/, mirroring clear_state.
        archive_steps = (
            project_root / "tianluo" / "state" / "archive" / "steps" / flow_id
        )
        assert archive_steps.is_dir()
        assert (archive_steps / "_context.json").exists()
        assert list(archive_steps.glob("*.json"))

        # Simulate the destructive worktree removal that follows promotion.
        shutil = __import__("shutil")
        shutil.rmtree(wt_path)

        # The archived flow still reloads at FULL fidelity — every step keeps its
        # inputs/outputs and the shared context is intact (no empty degradation).
        loaded = PersistenceManager(project_root).load_archived_flow_by_id(flow_id)
        assert loaded is not None
        for sid in loaded.state.step_history:
            step = loaded.state.steps[sid]
            assert step.inputs.get("test_results")
            assert step.outputs.get("artifact_blob")
        assert loaded.state.context.get("spec_content")

    def test_cold_partition_collision_records_suffixed_partition(
        self, tmp_path: Path
    ) -> None:
        from tianluo.engine.merge.cleanup import _promote_completed_engine_state
        from tianluo.engine.persistence import PersistenceManager

        project_root = tmp_path / "main"
        project_root.mkdir()
        # A prior archive already owns archive/steps/<flow_id>/ with different data.
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        flow_id = self._save_new_format_completed_flow(wt_path)
        prior = project_root / "tianluo" / "state" / "archive" / "steps" / flow_id
        prior.mkdir(parents=True)
        (prior / "sentinel.json").write_text("{}", encoding="utf-8")

        promoted = _promote_completed_engine_state(project_root, wt_path)
        assert promoted is not None

        # The prior partition is untouched; this flow's cold files land in a
        # suffixed partition recorded in the promoted header so its cold_ref
        # entries resolve to its own data.
        assert (prior / "sentinel.json").exists()
        data = json.loads(promoted.read_text(encoding="utf-8"))
        partition = data["state"]["cold_partition"]
        assert partition != flow_id and partition.startswith(flow_id)
        suffixed = (
            project_root / "tianluo" / "state" / "archive" / "steps" / partition
        )
        assert suffixed.is_dir()

        shutil = __import__("shutil")
        shutil.rmtree(wt_path)
        loaded = PersistenceManager(project_root).load_archived_flow_by_id(flow_id)
        assert loaded is not None
        for sid in loaded.state.step_history:
            assert loaded.state.steps[sid].inputs.get("test_results")

    def test_legacy_inline_worktree_has_no_cold_partition(
        self, tmp_path: Path
    ) -> None:
        """A legacy inline worktree engine.json promotes byte-for-byte with no
        cold partition (nothing to copy) — the pre-#244 behavior is preserved."""
        from tianluo.engine.merge.cleanup import _promote_completed_engine_state

        project_root = tmp_path / "main"
        project_root.mkdir()
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        _write_worktree_engine_json(wt_path, "flow-legacy", status="completed")

        promoted = _promote_completed_engine_state(project_root, wt_path)
        assert promoted is not None
        assert not (
            project_root / "tianluo" / "state" / "archive" / "steps"
        ).exists()


class TestParseWorktreePorcelain:
    """Defect J1: porcelain parser rejects malformed blocks deterministically."""

    def test_parses_valid_record(self) -> None:
        from tianluo.engine.merge.cleanup import _parse_worktree_porcelain

        sample = (
            "worktree /path/to/wt\n"
            "HEAD abc123\n"
            "branch refs/heads/feature\n"
            "\n"
        )
        records = _parse_worktree_porcelain(sample)
        assert len(records) == 1
        assert records[0].path == "/path/to/wt"
        assert records[0].head == "abc123"
        assert records[0].branch == "feature"
        assert records[0].detached is False

    def test_parses_detached_record(self) -> None:
        from tianluo.engine.merge.cleanup import _parse_worktree_porcelain

        sample = (
            "worktree /path/to/wt\n"
            "HEAD abc123\n"
            "detached\n"
            "\n"
        )
        records = _parse_worktree_porcelain(sample)
        assert len(records) == 1
        assert records[0].path == "/path/to/wt"
        assert records[0].detached is True
        assert records[0].branch is None

    def test_drops_block_without_branch_or_detached(self, caplog) -> None:
        """A worktree block missing both branch and detached MUST be dropped.

        Regression guard against silent skip — if git's porcelain output
        ever changes, we want a WARNING in the log so the test/operator
        notices instead of a silently mis-parsed record.
        """
        from tianluo.engine.merge.cleanup import _parse_worktree_porcelain

        sample = (
            "worktree /path/to/wt\n"
            "HEAD abc123\n"
            "\n"
        )
        with caplog.at_level("WARNING"):
            records = _parse_worktree_porcelain(sample)
        assert records == []
        assert any(
            "neither 'branch' nor 'detached'/'bare' marker" in rec.message
            for rec in caplog.records
        )

    def test_parses_multiple_blocks_one_malformed(self, caplog) -> None:
        from tianluo.engine.merge.cleanup import _parse_worktree_porcelain

        sample = (
            "worktree /good\n"
            "HEAD abc\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /bad\n"
            "HEAD def\n"
            "\n"
            "worktree /also-good\n"
            "HEAD ghi\n"
            "branch refs/heads/feature\n"
            "\n"
        )
        with caplog.at_level("WARNING"):
            records = _parse_worktree_porcelain(sample)
        # The good ones survive.
        assert {r.path for r in records} == {"/good", "/also-good"}
        # The bad one is logged.
        assert any("/bad" in rec.message for rec in caplog.records)

    def test_handles_non_heads_ref(self) -> None:
        """A branch ref that is not under refs/heads is preserved verbatim."""
        from tianluo.engine.merge.cleanup import _parse_worktree_porcelain

        sample = (
            "worktree /path\n"
            "HEAD abc\n"
            "branch refs/remotes/origin/main\n"
            "\n"
        )
        records = _parse_worktree_porcelain(sample)
        assert len(records) == 1
        # Branch is preserved verbatim (callers compare against local
        # branch names so non-heads refs harmlessly fail to match).
        assert records[0].branch == "refs/remotes/origin/main"


class TestCleanupLocaleIndependence:
    """Defect J2: cleanup git invocations must be locale-pinned to LC_ALL=C.

    A user with a localized git build (e.g. ``LANG=zh_CN.UTF-8``) would
    otherwise see translated stderr, which the previous substring matcher
    would silently miss. The wrapper ``_run_git_locale`` forces ``LC_ALL=C``
    so error matching stays deterministic.
    """

    def test_run_git_locale_sets_lc_all_c(self, tmp_path: Path, monkeypatch) -> None:
        """``_run_git_locale`` invocations override LC_ALL to C."""
        from tianluo.engine.merge import cleanup

        captured_envs: list[dict] = []

        original_run = subprocess.run

        def tracking_run(*args, **kwargs):
            env = kwargs.get("env")
            captured_envs.append(env)
            return original_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", tracking_run)

        _init_repo(tmp_path)
        cleanup._run_git_locale(
            tmp_path, "rev-parse", "--abbrev-ref", "HEAD",
            check=False, timeout=15,
        )

        assert len(captured_envs) >= 1
        env = captured_envs[-1]
        assert env is not None
        assert env.get("LC_ALL") == "C"
        assert env.get("LANG") == "C"
        # LANGUAGE must be cleared (it can override LC_ALL on some systems).
        assert "LANGUAGE" not in env

    def test_localized_git_environment_does_not_leak(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Even when caller env has LANG=zh_CN.UTF-8, cleanup git runs LC_ALL=C."""
        from tianluo.engine.merge import cleanup

        # Simulate a localized parent environment.
        monkeypatch.setenv("LANG", "zh_CN.UTF-8")
        monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
        monkeypatch.setenv("LANGUAGE", "zh_CN")

        env = cleanup._build_locale_env()
        assert env["LC_ALL"] == "C"
        assert env["LANG"] == "C"
        assert "LANGUAGE" not in env


class TestAncestorPreCheck:
    """Defect J4: explicit ancestor verification before ``git branch -d``."""

    def test_unmerged_branch_is_skipped_without_branch_d(
        self, tmp_path: Path
    ) -> None:
        """An unmerged branch lands in skipped_not_merged via the ancestor check.

        We verify by introspection: the branch must NOT be deleted, and
        the report must record the locale-independent rejection reason
        ``"not an ancestor of HEAD"`` (rather than git's localized
        "not fully merged" message).
        """
        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        # Add a different commit on default so feature is not an ancestor.
        _add_commit(tmp_path, "other.txt", "other", "Add other")

        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["feature"])

        assert report.deleted == []
        assert len(report.skipped_not_merged) == 1
        assert report.skipped_not_merged[0][0] == "feature"
        assert "ancestor" in report.skipped_not_merged[0][1].lower()
        assert _branch_exists(tmp_path, "feature") is True

    def test_indeterminate_ancestry_is_skipped_unknown_state(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """If merge-base --is-ancestor times out, fail closed with skipped_unknown_state.

        Defect J4: we must not call ``branch -d`` when the ancestor check
        cannot prove the branch is merged. The expected outcome is
        ``skipped_unknown_state`` so an operator can investigate.
        """
        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )

        from tianluo.engine.merge import cleanup as cleanup_mod

        # Force the ancestor check to return None (cannot decide).
        monkeypatch.setattr(
            cleanup_mod, "_is_branch_ancestor_of_head",
            lambda project_root, branch: None,
        )

        mgr = cleanup_mod.CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["feature"])
        assert report.deleted == []
        assert len(report.skipped_unknown_state) == 1
        assert report.skipped_unknown_state[0][0] == "feature"
        # Branch must still exist — refusing to delete on indeterminacy.
        assert _branch_exists(tmp_path, "feature") is True


class TestWorktreeRemoveFailureResilience:
    """Defect J3: worktree-remove failure must not skip the ``branch -d`` retry.

    Regression guard for the scenario where a worktree removal fails (lock
    held, filesystem error) but the branch itself is fully merged. The
    previous implementation bailed out without retrying ``branch -d``,
    leaving a merged branch behind even though git's safe ``-d`` flag would
    have completed cleanup.
    """

    def test_worktree_remove_failure_followed_by_successful_branch_d(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Branch is deleted via retry even when worktree removal fails."""
        from tianluo.engine.merge import cleanup as cleanup_mod

        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )

        # Pretend a worktree exists at a fake path.
        fake_wt = tmp_path / "fake_wt"
        monkeypatch.setattr(
            cleanup_mod, "exists_for_branch",
            lambda project_root, branch: True,
        )
        monkeypatch.setattr(
            cleanup_mod, "_get_worktree_path_for_branch",
            lambda project_root, branch: fake_wt,
        )
        # Treat the imaginary worktree as clean.
        monkeypatch.setattr(
            cleanup_mod, "_is_worktree_clean",
            lambda wt_path: True,
        )

        run_calls: list[tuple] = []

        def scripted_run_git_locale(
            project_root, *args, check=False, timeout=30
        ):
            run_calls.append(args)

            class FakeResult:
                returncode = 0
                stdout = ""
                stderr = ""

            res = FakeResult()
            if args[:1] == ("rev-parse",):
                res.stdout = default
                res.returncode = 0
                return res
            if args[:3] == ("merge-base", "--is-ancestor", "feature"):
                res.returncode = 0  # is an ancestor
                return res
            if args[:3] == ("branch", "-d", "feature"):
                # First attempt: simulate "checked out" rejection so we
                # take the worktree-removal branch. Second attempt
                # (after worktree-remove fails): succeed via retry.
                attempts = sum(
                    1 for c in run_calls if c[:3] == ("branch", "-d", "feature")
                )
                if attempts == 1:
                    res.returncode = 1
                    res.stderr = (
                        "error: Cannot delete branch 'feature' "
                        "checked out at /tmp"
                    )
                    return res
                # Second attempt — succeed
                res.returncode = 0
                return res
            if args[:2] == ("worktree", "remove"):
                # Simulate a transient worktree removal failure.
                res.returncode = 128
                res.stderr = "fatal: cannot remove worktree (lock held)"
                return res
            return res

        monkeypatch.setattr(
            cleanup_mod, "_run_git_locale", scripted_run_git_locale,
        )
        # No-op for metadata cleanup so the test does not touch real
        # ``.git/worktrees`` of the repo.
        monkeypatch.setattr(
            cleanup_mod, "_cleanup_git_worktree_metadata",
            lambda project_root, branch: None,
        )

        mgr = cleanup_mod.CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["feature"])

        # The retry must succeed and place the branch in `deleted`.
        assert report.deleted == ["feature"]
        assert report.skipped_worktree_remove_failed == []
        # Verify both branch -d attempts and the worktree-remove call were
        # actually invoked, so the resilience path is exercised.
        branch_d_attempts = [
            c for c in run_calls if c[:3] == ("branch", "-d", "feature")
        ]
        assert len(branch_d_attempts) == 2
        assert any(c[:2] == ("worktree", "remove") for c in run_calls)

    def test_worktree_remove_failure_and_branch_d_retry_failure_records_worktree_skip(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Both worktree removal AND retry branch -d fail → surface worktree error.

        Preserves the existing behaviour under defect J3 — the resilience
        addition only changes the case where the retry succeeds.
        """
        from tianluo.engine.merge import cleanup as cleanup_mod

        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )

        fake_wt = tmp_path / "fake_wt"
        monkeypatch.setattr(
            cleanup_mod, "exists_for_branch",
            lambda project_root, branch: True,
        )
        monkeypatch.setattr(
            cleanup_mod, "_get_worktree_path_for_branch",
            lambda project_root, branch: fake_wt,
        )
        monkeypatch.setattr(
            cleanup_mod, "_is_worktree_clean",
            lambda wt_path: True,
        )

        def scripted_run_git_locale(
            project_root, *args, check=False, timeout=30
        ):
            class FakeResult:
                returncode = 0
                stdout = ""
                stderr = ""

            res = FakeResult()
            if args[:1] == ("rev-parse",):
                res.stdout = default
                res.returncode = 0
                return res
            if args[:3] == ("merge-base", "--is-ancestor", "feature"):
                res.returncode = 0
                return res
            if args[:3] == ("branch", "-d", "feature"):
                # All branch -d attempts fail with "checked out" rejection.
                res.returncode = 1
                res.stderr = (
                    "error: Cannot delete branch 'feature' "
                    "checked out at /tmp"
                )
                return res
            if args[:2] == ("worktree", "remove"):
                res.returncode = 128
                res.stderr = "fatal: cannot remove worktree (lock held)"
                return res
            return res

        monkeypatch.setattr(
            cleanup_mod, "_run_git_locale", scripted_run_git_locale,
        )
        monkeypatch.setattr(
            cleanup_mod, "_cleanup_git_worktree_metadata",
            lambda project_root, branch: None,
        )

        mgr = cleanup_mod.CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["feature"])

        assert report.deleted == []
        assert len(report.skipped_worktree_remove_failed) == 1
        assert report.skipped_worktree_remove_failed[0][0] == "feature"
        assert "lock held" in report.skipped_worktree_remove_failed[0][1]
