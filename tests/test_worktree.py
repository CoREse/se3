"""Tests for SE3 git worktree lifecycle management."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import call, patch

import pytest

from se3.engine.worktree import (
    WorktreeContext,
    _branch_safe_name,
    _cleanup_git_worktree_metadata,
    cleanup_loop,
    create_loop_branch,
    create_worktree,
    delete_branch,
    exists_for_branch,
    force_cleanup_worktree,
    get_current_branch,
    get_diff_stat,
    has_commits,
    has_new_commits,
    list_loop_branches,
    merge_loop_branch,
    remove_worktree,
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
    # Create initial commit
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


def _init_empty_repo(path: Path) -> None:
    """Initialize a git repo with NO commits (empty repo)."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )


class TestGetCurrentBranch:
    def test_returns_branch_name(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch = get_current_branch(tmp_path)
        # Could be "master" or "main" depending on git config
        assert branch in ("master", "main")

    def test_custom_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "dev"],
            check=True, capture_output=True,
        )
        assert get_current_branch(tmp_path) == "dev"

    def test_empty_repo_returns_branch(self, tmp_path: Path) -> None:
        """get_current_branch should work on repos with no commits."""
        _init_empty_repo(tmp_path)
        branch = get_current_branch(tmp_path)
        assert branch in ("master", "main")

    def test_detached_head_raises(self, tmp_path: Path) -> None:
        """get_current_branch should raise RuntimeError on detached HEAD."""
        _init_repo(tmp_path)
        # Detach HEAD by checking out a specific commit
        head_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", head_sha],
            capture_output=True, check=True,
        )
        with pytest.raises(RuntimeError, match="Detached HEAD"):
            get_current_branch(tmp_path)

    def test_empty_repo_custom_initial_branch(self, tmp_path: Path) -> None:
        """get_current_branch should work on empty repos with custom initial branch."""
        subprocess.run(
            ["git", "init", "--initial-branch=develop", str(tmp_path)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
            check=True, capture_output=True,
        )
        branch = get_current_branch(tmp_path)
        assert branch == "develop"


class TestCreateLoopBranch:
    def test_creates_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, original = create_loop_branch(tmp_path, timestamp="20260324-120000")

        assert branch_name == "se3-loop/20260324-120000"
        assert original in ("master", "main")

        # Verify branch exists
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--list", branch_name],
            capture_output=True, text=True,
        )
        assert branch_name in result.stdout

    def test_auto_timestamp(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path)
        assert branch_name.startswith("se3-loop/")

    def test_branch_points_to_head(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        _add_commit(tmp_path, "file.txt", "content", "second commit")

        branch_name, _ = create_loop_branch(tmp_path, timestamp="test")

        # Both HEAD and the new branch should point to the same commit
        head_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        branch_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", branch_name],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        assert head_sha == branch_sha


class TestCreateWorktree:
    def test_creates_worktree_directory(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="test")

        wt_path = create_worktree(tmp_path, branch_name)

        assert wt_path.exists()
        assert wt_path.is_dir()
        assert (wt_path / "README.md").exists()

    def test_worktree_at_expected_path(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="test")

        wt_path = create_worktree(tmp_path, branch_name)

        expected = tmp_path / "se3" / "worktrees" / "se3-loop-test"
        assert wt_path == expected

    def test_worktree_listed_by_git(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="test")
        create_worktree(tmp_path, branch_name)

        result = subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "list"],
            capture_output=True, text=True, check=True,
        )
        assert "se3-loop-test" in result.stdout


class TestRemoveWorktree:
    def test_removes_worktree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="test")
        wt_path = create_worktree(tmp_path, branch_name)

        remove_worktree(tmp_path, wt_path)

        assert not wt_path.exists()

    def test_handles_already_removed(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        # Try removing a non-existent worktree path — should not raise
        fake_path = tmp_path / "se3" / "worktrees" / "nonexistent"
        remove_worktree(tmp_path, fake_path)  # Should not raise


class TestRemoveWorktreeLocked:
    """Tests for remove_worktree handling locked worktrees."""

    def test_removes_locked_worktree_with_double_force(self, tmp_path: Path) -> None:
        """Locked worktree should be removed via double-force retry."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="lock-test")
        wt_path = create_worktree(tmp_path, branch_name)

        # Lock the worktree
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "lock", str(wt_path)],
            check=True, capture_output=True,
        )

        # remove_worktree should handle the lock transparently
        remove_worktree(tmp_path, wt_path)

        assert not wt_path.exists()
        assert not exists_for_branch(tmp_path, branch_name)

    def test_cleans_stale_metadata_when_dir_gone(self, tmp_path: Path) -> None:
        """When worktree dir is manually deleted, metadata should still be cleaned."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="stale-test")
        wt_path = create_worktree(tmp_path, branch_name)

        # Manually delete the worktree directory (simulating crash)
        shutil.rmtree(wt_path)
        assert not wt_path.exists()

        # Git still tracks it
        assert exists_for_branch(tmp_path, branch_name)

        # remove_worktree should clean up the metadata
        remove_worktree(tmp_path, wt_path)

        assert not exists_for_branch(tmp_path, branch_name)

    def test_removes_locked_worktree_with_custom_reason(self, tmp_path: Path) -> None:
        """Locked worktree with 'initializing' reason (the actual bug scenario)."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="lock-reason")
        wt_path = create_worktree(tmp_path, branch_name)

        # Lock with the exact reason from the bug report
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "lock",
             "--reason", "initializing", str(wt_path)],
            check=True, capture_output=True,
        )

        remove_worktree(tmp_path, wt_path)

        assert not wt_path.exists()
        assert not exists_for_branch(tmp_path, branch_name)


class TestForceCleanupWorktree:
    """Tests for force_cleanup_worktree."""

    def test_cleans_normal_worktree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="fc-normal")
        create_worktree(tmp_path, branch_name)

        force_cleanup_worktree(tmp_path, branch_name)

        assert not exists_for_branch(tmp_path, branch_name)

    def test_cleans_locked_worktree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="fc-locked")
        wt_path = create_worktree(tmp_path, branch_name)

        # Lock the worktree
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "lock", str(wt_path)],
            check=True, capture_output=True,
        )

        force_cleanup_worktree(tmp_path, branch_name)

        assert not wt_path.exists()
        assert not exists_for_branch(tmp_path, branch_name)

    def test_cleans_missing_directory(self, tmp_path: Path) -> None:
        """Handles case where directory was already deleted but metadata remains."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="fc-missing")
        wt_path = create_worktree(tmp_path, branch_name)

        # Manually remove directory
        shutil.rmtree(wt_path)

        force_cleanup_worktree(tmp_path, branch_name)

        assert not exists_for_branch(tmp_path, branch_name)

    def test_cleans_locked_with_missing_directory(self, tmp_path: Path) -> None:
        """Handles combined state: locked worktree + directory already deleted."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="fc-lock-miss")
        wt_path = create_worktree(tmp_path, branch_name)

        # Lock, then delete directory (simulating interrupted initialization)
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "lock",
             "--reason", "initializing", str(wt_path)],
            check=True, capture_output=True,
        )
        shutil.rmtree(wt_path)

        # Git still tracks it as locked
        assert exists_for_branch(tmp_path, branch_name)

        force_cleanup_worktree(tmp_path, branch_name)

        assert not wt_path.exists()
        assert not exists_for_branch(tmp_path, branch_name)

    def test_noop_when_no_worktree(self, tmp_path: Path) -> None:
        """Should not raise when there's nothing to clean up."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="fc-noop")

        # No worktree created — should not raise
        force_cleanup_worktree(tmp_path, branch_name)

        assert not exists_for_branch(tmp_path, branch_name)

    def test_idempotent_double_call(self, tmp_path: Path) -> None:
        """Calling force_cleanup_worktree twice should not raise."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="fc-idem")
        wt_path = create_worktree(tmp_path, branch_name)

        # Lock it for good measure
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "lock", str(wt_path)],
            check=True, capture_output=True,
        )

        force_cleanup_worktree(tmp_path, branch_name)
        assert not exists_for_branch(tmp_path, branch_name)

        # Second call should be a no-op
        force_cleanup_worktree(tmp_path, branch_name)
        assert not exists_for_branch(tmp_path, branch_name)


class TestMergeLoopBranch:
    def test_fast_forward_merge(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        original = get_current_branch(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="test")
        wt_path = create_worktree(tmp_path, branch_name)

        # Add commit in worktree
        _add_commit(wt_path, "new_file.txt", "hello", "loop work")

        # Remove worktree before merging
        remove_worktree(tmp_path, wt_path)

        success = merge_loop_branch(tmp_path, branch_name, original)
        assert success

        # Verify the file exists on original branch
        assert (tmp_path / "new_file.txt").exists()

    def test_merge_with_no_new_commits(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        original = get_current_branch(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="test")

        # No new commits on loop branch
        success = merge_loop_branch(tmp_path, branch_name, original)
        assert success

    def test_merge_conflict_human_returns_pending(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        original = get_current_branch(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="test")
        wt_path = create_worktree(tmp_path, branch_name)

        # Create conflicting changes
        _add_commit(wt_path, "conflict.txt", "loop version", "loop change")

        # Remove worktree so we can switch branches in main repo
        remove_worktree(tmp_path, wt_path)

        # Add conflicting commit on original branch
        _add_commit(tmp_path, "conflict.txt", "main version", "main change")

        # Default conflict_strategy='human' returns 'pending_human'
        result = merge_loop_branch(tmp_path, branch_name, original)
        assert result == "pending_human"

        # Conflict state is preserved (not aborted) — repo has unmerged files
        status_result = subprocess.run(
            ["git", "-C", str(tmp_path), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        )
        assert "U" in status_result.stdout or "conflict.txt" in status_result.stdout

        # Call file should be created
        calls_dir = tmp_path / "se3" / "calls"
        call_files = list(calls_dir.glob("merge_conflict_*.json"))
        assert len(call_files) == 1

        # Abort merge for cleanup
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "--abort"],
            capture_output=True, check=False,
        )


class TestCleanupLoop:
    def test_cleanup_removes_worktree_keeps_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="test")
        wt_path = create_worktree(tmp_path, branch_name)

        cleanup_loop(tmp_path, branch_name, wt_path, delete_branch_flag=False)

        assert not wt_path.exists()
        # Branch should still exist
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--list", branch_name],
            capture_output=True, text=True,
        )
        assert branch_name in result.stdout

    def test_cleanup_removes_worktree_and_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="test")
        wt_path = create_worktree(tmp_path, branch_name)

        cleanup_loop(tmp_path, branch_name, wt_path, delete_branch_flag=True)

        assert not wt_path.exists()
        # Branch should be deleted
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--list", branch_name],
            capture_output=True, text=True,
        )
        assert branch_name not in result.stdout


class TestDeleteBranch:
    def test_deletes_existing_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="test")

        delete_branch(tmp_path, branch_name)

        result = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--list", branch_name],
            capture_output=True, text=True,
        )
        assert branch_name not in result.stdout

    def test_handles_nonexistent_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        # Should not raise
        delete_branch(tmp_path, "nonexistent-branch")


class TestHasNewCommits:
    def test_no_new_commits(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        original = get_current_branch(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="test")

        assert not has_new_commits(tmp_path, branch_name, original)

    def test_has_new_commits(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        original = get_current_branch(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="test")
        wt_path = create_worktree(tmp_path, branch_name)

        _add_commit(wt_path, "new.txt", "content", "new work")
        remove_worktree(tmp_path, wt_path)

        assert has_new_commits(tmp_path, branch_name, original)


class TestFlowInstanceWorktreeFields:
    """Test that FlowInstance serializes/deserializes worktree fields."""

    def test_round_trip_with_worktree_fields(self) -> None:
        from se3.engine.models import FlowInstance

        flow = FlowInstance(
            task_description="test",
            is_loop_mode=True,
            loop_branch="se3-loop/20260324-120000",
            loop_worktree_path="/tmp/worktree",
            loop_original_branch="master",
        )

        data = flow.to_dict()
        assert data["loop_worktree_path"] == "/tmp/worktree"
        assert data["loop_original_branch"] == "master"
        assert data["loop_branch"] == "se3-loop/20260324-120000"

        restored = FlowInstance.from_dict(data)
        assert restored.loop_worktree_path == "/tmp/worktree"
        assert restored.loop_original_branch == "master"
        assert restored.loop_branch == "se3-loop/20260324-120000"

    def test_round_trip_without_worktree_fields(self) -> None:
        from se3.engine.models import FlowInstance

        flow = FlowInstance(task_description="test")
        data = flow.to_dict()

        assert data["loop_worktree_path"] is None
        assert data["loop_original_branch"] is None

        restored = FlowInstance.from_dict(data)
        assert restored.loop_worktree_path is None
        assert restored.loop_original_branch is None

    def test_backward_compat_missing_fields(self) -> None:
        """Old persisted state without new fields should deserialize fine."""
        from se3.engine.models import FlowInstance, FlowStatus

        data = {
            "flow_id": "test-123",
            "status": "running",
            "task_description": "test",
            "state": {},
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }

        flow = FlowInstance.from_dict(data)
        assert flow.loop_worktree_path is None
        assert flow.loop_original_branch is None
        assert flow.loop_branch is None


class TestWorktreeContext:
    """Test WorktreeContext context manager."""

    def test_creates_and_removes_worktree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="ctx-test")

        with WorktreeContext(tmp_path, branch_name) as wt_path:
            assert wt_path.exists()
            assert (wt_path / "README.md").exists()

        # After exit, worktree should be removed
        assert not wt_path.exists()

    def test_cleanup_on_exception(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="exc-test")

        wt_path_ref = None
        with pytest.raises(RuntimeError):
            with WorktreeContext(tmp_path, branch_name) as wt_path:
                wt_path_ref = wt_path
                assert wt_path.exists()
                raise RuntimeError("simulated failure")

        # Worktree should be cleaned up even after exception
        assert not wt_path_ref.exists()

        # Branch should be preserved for recovery
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--list", branch_name],
            capture_output=True, text=True,
        )
        assert branch_name in result.stdout

    def test_rejects_duplicate_worktree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="dup-test")

        # Create first worktree
        wt_path = create_worktree(tmp_path, branch_name)
        assert wt_path.exists()

        try:
            # Attempting to create via context should fail
            with pytest.raises(RuntimeError, match="already exists"):
                with WorktreeContext(tmp_path, branch_name):
                    pass
        finally:
            remove_worktree(tmp_path, wt_path)


class TestExistsForBranch:
    def test_returns_false_when_no_worktree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="exists-test")
        assert not exists_for_branch(tmp_path, branch_name)

    def test_returns_true_when_worktree_exists(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="exists-test2")
        wt_path = create_worktree(tmp_path, branch_name)

        try:
            assert exists_for_branch(tmp_path, branch_name)
        finally:
            remove_worktree(tmp_path, wt_path)


class TestListLoopBranches:
    def test_no_loop_branches(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        result = list_loop_branches(tmp_path)
        assert result == []

    def test_lists_loop_branches(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch1, _ = create_loop_branch(tmp_path, timestamp="20260324-100000")
        branch2, _ = create_loop_branch(tmp_path, timestamp="20260324-110000")

        result = list_loop_branches(tmp_path)
        branch_names = [b["branch"] for b in result]
        assert branch1 in branch_names
        assert branch2 in branch_names

    def test_includes_commit_count(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="count-test")
        wt_path = create_worktree(tmp_path, branch_name)
        _add_commit(wt_path, "new.txt", "content", "loop commit")
        remove_worktree(tmp_path, wt_path)

        result = list_loop_branches(tmp_path)
        matching = [b for b in result if b["branch"] == branch_name]
        assert len(matching) == 1
        assert matching[0]["commit_count"] == 1


class TestGetDiffStat:
    def test_returns_diff_stat(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        original = get_current_branch(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="diff-test")
        wt_path = create_worktree(tmp_path, branch_name)
        _add_commit(wt_path, "new_file.txt", "hello world", "add file")
        remove_worktree(tmp_path, wt_path)

        stat = get_diff_stat(tmp_path, branch_name, original)
        assert "new_file.txt" in stat

    def test_empty_diff_stat(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        original = get_current_branch(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="empty-diff")

        stat = get_diff_stat(tmp_path, branch_name, original)
        assert stat == ""


class TestCreateWorktreeRetry:
    """Tests for retry logic on TimeoutExpired in create_worktree."""

    def test_succeeds_on_second_attempt(self, tmp_path: Path) -> None:
        """TimeoutExpired on first attempt should retry and succeed."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="retry-ok")

        original_run_git = __import__(
            "se3.engine.worktree", fromlist=["_run_git"]
        )._run_git

        call_count = 0

        def mock_run_git(project_root, *args, **kwargs):
            nonlocal call_count
            if args[:2] == ("worktree", "add"):
                call_count += 1
                if call_count == 1:
                    raise subprocess.TimeoutExpired(
                        cmd=["git", "worktree", "add"],
                        timeout=120,
                    )
            return original_run_git(project_root, *args, **kwargs)

        with patch("se3.engine.worktree._run_git", side_effect=mock_run_git):
            wt_path = create_worktree(tmp_path, branch_name)

        assert wt_path.exists()
        assert call_count == 2  # first failed, second succeeded
        # Clean up
        remove_worktree(tmp_path, wt_path)

    def test_raises_after_all_retries_exhausted(self, tmp_path: Path) -> None:
        """Should re-raise TimeoutExpired after max retries."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="retry-fail")

        original_run_git = __import__(
            "se3.engine.worktree", fromlist=["_run_git"]
        )._run_git

        call_count = 0

        def mock_run_git(project_root, *args, **kwargs):
            nonlocal call_count
            if args[:2] == ("worktree", "add"):
                call_count += 1
                raise subprocess.TimeoutExpired(
                    cmd=["git", "worktree", "add"],
                    timeout=kwargs.get("timeout", 120),
                )
            return original_run_git(project_root, *args, **kwargs)

        with patch("se3.engine.worktree._run_git", side_effect=mock_run_git):
            with pytest.raises(subprocess.TimeoutExpired):
                create_worktree(tmp_path, branch_name)

        # 1 initial + 2 retries = 3 total attempts
        assert call_count == 3

    def test_timeout_doubles_on_each_retry(self, tmp_path: Path) -> None:
        """Timeout should double on each retry attempt."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="retry-double")

        timeouts_seen = []

        def mock_run_git(project_root, *args, **kwargs):
            if args[:2] == ("worktree", "add"):
                timeouts_seen.append(kwargs.get("timeout", 30))
                raise subprocess.TimeoutExpired(
                    cmd=["git", "worktree", "add"],
                    timeout=kwargs.get("timeout", 30),
                )
            # Allow prune calls through without error
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        with patch("se3.engine.worktree._run_git", side_effect=mock_run_git):
            with pytest.raises(subprocess.TimeoutExpired):
                create_worktree(tmp_path, branch_name)

        assert timeouts_seen == [120, 240, 480]

    def test_partial_worktree_dir_cleaned_on_retry(self, tmp_path: Path) -> None:
        """Partial worktree directory should be removed before retry."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="retry-clean")

        safe_name = branch_name.replace("/", "-")
        worktree_path = tmp_path / "se3" / "worktrees" / safe_name

        original_run_git = __import__(
            "se3.engine.worktree", fromlist=["_run_git"]
        )._run_git

        call_count = 0

        def mock_run_git(project_root, *args, **kwargs):
            nonlocal call_count
            if args[:2] == ("worktree", "add"):
                call_count += 1
                if call_count == 1:
                    # Simulate partial directory creation before timeout
                    worktree_path.mkdir(parents=True, exist_ok=True)
                    (worktree_path / "partial_file").write_text("partial")
                    raise subprocess.TimeoutExpired(
                        cmd=["git", "worktree", "add"],
                        timeout=120,
                    )
            return original_run_git(project_root, *args, **kwargs)

        with patch("se3.engine.worktree._run_git", side_effect=mock_run_git):
            wt_path = create_worktree(tmp_path, branch_name)

        # The partial file should not exist (directory was cleaned)
        assert not (wt_path / "partial_file").exists()
        # But the worktree should be properly created
        assert wt_path.exists()
        assert (wt_path / "README.md").exists()
        # Clean up
        remove_worktree(tmp_path, wt_path)


class TestHasCommits:
    def test_repo_with_commits_returns_true(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        assert has_commits(tmp_path) is True

    def test_empty_repo_returns_false(self, tmp_path: Path) -> None:
        _init_empty_repo(tmp_path)
        assert has_commits(tmp_path) is False

    def test_after_adding_commit_returns_true(self, tmp_path: Path) -> None:
        _init_empty_repo(tmp_path)
        assert has_commits(tmp_path) is False
        # Add a commit
        (tmp_path / "file.txt").write_text("content")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "first"],
            check=True, capture_output=True,
        )
        assert has_commits(tmp_path) is True


class TestForceCleanupWorktreeFaultTolerance:
    """Tests for independent fault tolerance of each step in force_cleanup_worktree."""

    def test_unlock_timeout_does_not_block_subsequent_steps(self, tmp_path: Path) -> None:
        """Step 1 (unlock) timing out should not prevent Steps 2-6 from running."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="ft-unlock")
        wt_path = create_worktree(tmp_path, branch_name)

        original_run_git = __import__(
            "se3.engine.worktree", fromlist=["_run_git"]
        )._run_git

        steps_called = []

        def mock_run_git(project_root, *args, **kwargs):
            if args[:2] == ("worktree", "unlock"):
                steps_called.append("unlock")
                raise subprocess.TimeoutExpired(
                    cmd=["git", "worktree", "unlock"], timeout=60,
                )
            if args[:2] == ("worktree", "remove"):
                steps_called.append("remove")
            if args[:2] == ("worktree", "prune"):
                steps_called.append("prune")
            if args[:2] == ("worktree", "list"):
                steps_called.append("list")
            return original_run_git(project_root, *args, **kwargs)

        with patch("se3.engine.worktree._run_git", side_effect=mock_run_git):
            force_cleanup_worktree(tmp_path, branch_name)

        assert "unlock" in steps_called
        assert "remove" in steps_called
        assert "prune" in steps_called

    def test_remove_exception_does_not_block_subsequent_steps(self, tmp_path: Path) -> None:
        """Step 2 (remove) failing should not prevent Steps 3-6 from running."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="ft-remove")
        wt_path = create_worktree(tmp_path, branch_name)

        original_run_git = __import__(
            "se3.engine.worktree", fromlist=["_run_git"]
        )._run_git

        steps_called = []

        def mock_run_git(project_root, *args, **kwargs):
            if args[:2] == ("worktree", "unlock"):
                steps_called.append("unlock")
                return original_run_git(project_root, *args, **kwargs)
            if args[:2] == ("worktree", "remove"):
                steps_called.append("remove")
                raise RuntimeError("Simulated remove failure")
            if args[:2] == ("worktree", "prune"):
                steps_called.append("prune")
                return original_run_git(project_root, *args, **kwargs)
            if args[:2] == ("worktree", "list"):
                steps_called.append("list")
                return original_run_git(project_root, *args, **kwargs)
            return original_run_git(project_root, *args, **kwargs)

        with patch("se3.engine.worktree._run_git", side_effect=mock_run_git):
            force_cleanup_worktree(tmp_path, branch_name)

        assert "unlock" in steps_called
        assert "remove" in steps_called
        assert "prune" in steps_called
        # list is called by exists_for_branch in Step 6 (verify)
        assert "list" in steps_called

    def test_run_git_calls_use_timeout_60(self, tmp_path: Path) -> None:
        """All _run_git calls in force_cleanup_worktree should use timeout=60."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="ft-timeout")

        original_run_git = __import__(
            "se3.engine.worktree", fromlist=["_run_git"]
        )._run_git

        timeouts_seen = []

        def mock_run_git(project_root, *args, **kwargs):
            timeout = kwargs.get("timeout", 30)
            # Only track cleanup-related calls (not exists_for_branch which
            # uses the default timeout)
            if args[:2] in (
                ("worktree", "unlock"),
                ("worktree", "remove"),
                ("worktree", "prune"),
            ):
                timeouts_seen.append(timeout)
            return original_run_git(project_root, *args, **kwargs)

        with patch("se3.engine.worktree._run_git", side_effect=mock_run_git):
            force_cleanup_worktree(tmp_path, branch_name)

        # All cleanup _run_git calls should use timeout=60
        assert all(t == 60 for t in timeouts_seen), f"Expected all timeouts=60, got {timeouts_seen}"
        assert len(timeouts_seen) == 3  # unlock, remove, prune

    def test_metadata_cleanup_called(self, tmp_path: Path) -> None:
        """Step 5 (_cleanup_git_worktree_metadata) should be called."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="ft-meta")

        with patch(
            "se3.engine.worktree._cleanup_git_worktree_metadata"
        ) as mock_meta:
            force_cleanup_worktree(tmp_path, branch_name)

        mock_meta.assert_called_once_with(tmp_path, branch_name)


class TestCleanupGitWorktreeMetadata:
    """Tests for _cleanup_git_worktree_metadata."""

    def test_removes_existing_metadata_directory(self, tmp_path: Path) -> None:
        """Should remove .git/worktrees/<safe_name> when it exists."""
        _init_repo(tmp_path)
        branch_name = "se3-loop/meta-exists"
        safe_name = _branch_safe_name(branch_name)
        metadata_path = tmp_path / ".git" / "worktrees" / safe_name
        metadata_path.mkdir(parents=True)
        (metadata_path / "HEAD").write_text("ref: refs/heads/" + branch_name)

        _cleanup_git_worktree_metadata(tmp_path, branch_name)

        assert not metadata_path.exists()

    def test_noop_when_metadata_absent(self, tmp_path: Path) -> None:
        """Should do nothing when metadata directory doesn't exist."""
        _init_repo(tmp_path)
        branch_name = "se3-loop/meta-absent"
        safe_name = _branch_safe_name(branch_name)
        metadata_path = tmp_path / ".git" / "worktrees" / safe_name

        assert not metadata_path.exists()

        # Should not raise
        _cleanup_git_worktree_metadata(tmp_path, branch_name)

    def test_logs_warning_on_rmtree_failure(self, tmp_path: Path) -> None:
        """Should log warning but not raise when shutil.rmtree fails."""
        _init_repo(tmp_path)
        branch_name = "se3-loop/meta-fail"
        safe_name = _branch_safe_name(branch_name)
        metadata_path = tmp_path / ".git" / "worktrees" / safe_name
        metadata_path.mkdir(parents=True)

        with patch("se3.engine.worktree.shutil.rmtree", side_effect=OSError("permission denied")):
            # Should not raise
            _cleanup_git_worktree_metadata(tmp_path, branch_name)

        # Directory still exists because rmtree was mocked to fail
        assert metadata_path.exists()


class TestDeleteBranchWorktreeVerification:
    """Tests for delete_branch's worktree verification and retry logic."""

    def test_no_worktree_deletes_directly(self, tmp_path: Path) -> None:
        """When no worktree exists, branch is deleted directly without cleanup."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="db-direct")

        with patch("se3.engine.worktree.force_cleanup_worktree") as mock_cleanup:
            delete_branch(tmp_path, branch_name)

        mock_cleanup.assert_not_called()

        # Branch should be deleted
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--list", branch_name],
            capture_output=True, text=True,
        )
        assert branch_name not in result.stdout

    def test_worktree_exists_triggers_cleanup_then_deletes(self, tmp_path: Path) -> None:
        """When worktree exists, force_cleanup_worktree is called before delete."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="db-cleanup")
        wt_path = create_worktree(tmp_path, branch_name)

        # delete_branch should detect the worktree and clean it up
        delete_branch(tmp_path, branch_name)

        # Worktree should be gone
        assert not exists_for_branch(tmp_path, branch_name)
        # Branch should be deleted
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--list", branch_name],
            capture_output=True, text=True,
        )
        assert branch_name not in result.stdout

    def test_cleanup_fails_still_attempts_branch_delete(self, tmp_path: Path) -> None:
        """When cleanup fails and worktree persists, branch delete is still attempted."""
        _init_repo(tmp_path)
        branch_name, _ = create_loop_branch(tmp_path, timestamp="db-fail")

        original_exists = __import__(
            "se3.engine.worktree", fromlist=["exists_for_branch"]
        ).exists_for_branch

        call_count = {"exists": 0}

        def mock_exists(project_root, branch):
            call_count["exists"] += 1
            if call_count["exists"] <= 2:
                # First two calls: worktree "exists" (pre-check + post-cleanup check)
                return True
            return original_exists(project_root, branch)

        with patch("se3.engine.worktree.exists_for_branch", side_effect=mock_exists), \
             patch("se3.engine.worktree.force_cleanup_worktree") as mock_cleanup:
            delete_branch(tmp_path, branch_name)

        # force_cleanup_worktree should have been called
        mock_cleanup.assert_called_once_with(tmp_path, branch_name)

        # Branch should still be deleted despite worktree "persisting"
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--list", branch_name],
            capture_output=True, text=True,
        )
        assert branch_name not in result.stdout
