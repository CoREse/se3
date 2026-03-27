"""Tests for SE3 git worktree lifecycle management."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from se3.engine.worktree import (
    WorktreeContext,
    cleanup_loop,
    create_loop_branch,
    create_worktree,
    delete_branch,
    exists_for_branch,
    get_current_branch,
    get_diff_stat,
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
