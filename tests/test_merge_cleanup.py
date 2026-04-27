"""Tests for CleanupManager (--delete-merged flag)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from se3.engine.merge.cleanup import (
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
            "se3.engine.merge.cleanup.exists_for_branch",
            lambda project_root, branch: True,
        )
        fake_wt_path = tmp_path / "fake_wt"
        monkeypatch.setattr(
            "se3.engine.merge.cleanup._get_worktree_path_for_branch",
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
            import se3.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "se3.engine.merge.cleanup._run_git", tracked_run_git
        )

        metadata_cleaned = []

        def track_metadata_cleanup(project_root, branch):
            metadata_cleaned.append(branch)

        monkeypatch.setattr(
            "se3.engine.merge.cleanup._cleanup_git_worktree_metadata",
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
