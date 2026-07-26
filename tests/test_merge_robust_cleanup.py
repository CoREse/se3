"""Tests for ``--delete-merged`` archive-before-delete behavior.

Commit 5 (this file): every successful deletion archives the worktree
under ``<project_root>/se3/worktrees/.archive/<slug>-<ts>/`` BEFORE running
``git worktree remove`` + ``git branch -d``. Archive failures preserve
the worktree + branch so an operator can recover. The archive lands inside
the sole ignored runtime root ``se3/`` (covered by ``/se3/*``) so it can
never leak into git.

Archive is strategy-agnostic — these tests use the default robust
strategy but exercise the cleanup behavior directly via
``CleanupManager``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tianluo.engine.merge.cleanup import (
    CleanupManager,
    CleanupReport,
    _archive_worktree,
)


def _init_repo_with_worktree_branch(
    tmp_path: Path, leave_untracked_file: bool = False,
) -> tuple[str, Path, str]:
    """Set up a repo with a branch that is fully merged into master AND has
    a bound worktree on disk. Returns (branch_name, worktree_path,
    base_branch).

    The branch is "merged" (ancestor of HEAD) so CleanupManager.delete
    will accept it. When ``leave_untracked_file=True`` an untracked file
    is left in the worktree — useful for ``_archive_worktree`` helper
    tests but incompatible with ``CleanupManager.delete_merged_branches``
    integration tests because the existing dirty-worktree check refuses
    to destroy unclean worktrees (orthogonal safety).
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True,
    )
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."], check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"],
        check=True,
    )
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    # Create branch with one extra commit
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", "-q", "-b", "feat"],
        check=True,
    )
    (tmp_path / "tracked.py").write_text("print('hi')\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."], check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "add tracked"],
        check=True,
    )
    # Merge feat into base so CleanupManager sees feat as ancestor of HEAD.
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", "-q", base], check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "merge", "--no-ff", "feat",
         "-m", "merge feat", "-q"],
        check=True,
    )

    # Bind a worktree for feat (this only works if feat is NOT currently
    # checked out anywhere; we checked back out base above).
    wt_path = tmp_path / "feat-wt"
    subprocess.run(
        ["git", "-C", str(tmp_path), "worktree", "add", "-q",
         str(wt_path), "feat"],
        check=True,
    )
    if leave_untracked_file:
        # Helper-level tests use this to verify the archive captures
        # untracked content. Integration tests with the full
        # ``delete_merged_branches`` path MUST NOT leave one because
        # the dirty-worktree refusal would skip the delete entirely.
        (wt_path / "scratch.txt").write_text("untracked content\n")
    return "feat", wt_path, base


class TestArchiveWorktreeHelper:
    def test_archive_copies_tracked_and_untracked_excludes_dot_git(
        self, tmp_path: Path,
    ) -> None:
        branch, wt_path, _ = _init_repo_with_worktree_branch(
            tmp_path, leave_untracked_file=True,
        )
        archive_path = _archive_worktree(tmp_path, branch, wt_path)
        assert archive_path.is_dir()
        # New落点: archive is the hidden ``.archive`` subdir of se3/worktrees/.
        assert archive_path.parent.name == ".archive"
        # The whole archive path is anchored inside the ignored runtime root
        # ``se3/worktrees/.archive`` — never under a leaking ``.se3/``.
        assert archive_path.parent == tmp_path / "se3" / "worktrees" / ".archive"
        rel = archive_path.relative_to(tmp_path)
        assert rel.parts[:3] == ("se3", "worktrees", ".archive")
        assert ".se3" not in rel.parts
        # Tracked file present in archive
        assert (archive_path / "tracked.py").exists()
        # Untracked file present
        assert (archive_path / "scratch.txt").exists()
        # .git deliberately excluded (worktree's .git is a pointer file,
        # archiving it would be misleading)
        assert not (archive_path / ".git").exists()
        # Metadata file present and parseable
        meta_file = archive_path / ".se3-archive-meta.json"
        assert meta_file.exists()
        meta = json.loads(meta_file.read_text())
        assert meta["branch"] == branch
        assert meta["worktree_path"] == str(wt_path)
        assert "head_sha" in meta and len(meta["head_sha"]) >= 7
        assert isinstance(meta["ts"], int)

    def test_archive_collision_appends_seq_suffix(
        self, tmp_path: Path,
    ) -> None:
        branch, wt_path, _ = _init_repo_with_worktree_branch(
            tmp_path, leave_untracked_file=False,
        )
        # First archive
        a1 = _archive_worktree(tmp_path, branch, wt_path)
        # Force a same-timestamp collision by pre-creating the slot
        with patch("tianluo.engine.merge.cleanup.time.time", return_value=int(
            a1.name.rsplit("-", 1)[-1]
        )):
            a2 = _archive_worktree(tmp_path, branch, wt_path)
        assert a2 != a1
        assert a2.name.endswith("-1") or a2.name.endswith("-2")


class TestCleanupArchiveIntegration:
    def test_delete_merged_archives_before_destroying(
        self, tmp_path: Path,
    ) -> None:
        # Clean worktree — the existing dirty-worktree check refuses
        # to destroy unclean worktrees, so this integration scenario
        # uses a tracked-only worktree.
        branch, wt_path, _ = _init_repo_with_worktree_branch(
            tmp_path, leave_untracked_file=False,
        )
        # Sanity: branch + worktree exist before
        assert wt_path.exists()

        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches([branch])

        assert report.deleted == [branch]
        # Archive recorded
        assert len(report.archived) == 1
        archived_branch, archive_path = report.archived[0]
        assert archived_branch == branch
        assert archive_path.exists()
        # Archive lands inside the ignored runtime root se3/worktrees/.archive,
        # not a leaking .se3/.
        assert archive_path.parent == tmp_path / "se3" / "worktrees" / ".archive"
        assert ".se3" not in archive_path.relative_to(tmp_path).parts
        # Archive contains tracked content
        assert (archive_path / "tracked.py").exists()
        # Worktree is gone, branch is gone
        assert not wt_path.exists()
        branch_check = subprocess.run(
            ["git", "-C", str(tmp_path), "show-ref", "--verify",
             "--quiet", f"refs/heads/{branch}"],
            check=False,
        )
        assert branch_check.returncode != 0  # branch deleted

    def test_archive_failure_preserves_worktree_and_branch(
        self, tmp_path: Path,
    ) -> None:
        branch, wt_path, _ = _init_repo_with_worktree_branch(
            tmp_path, leave_untracked_file=False,
        )

        mgr = CleanupManager(tmp_path)
        # Mock copytree to fail.
        with patch(
            "tianluo.engine.merge.cleanup.shutil.copytree",
            side_effect=PermissionError("simulated disk full"),
        ):
            report = mgr.delete_merged_branches([branch])

        assert report.deleted == []
        assert len(report.skipped_archive_failed) == 1
        skipped_branch, reason = report.skipped_archive_failed[0]
        assert skipped_branch == branch
        assert "PermissionError" in reason or "simulated" in reason
        # Worktree + branch preserved
        assert wt_path.exists()
        branch_check = subprocess.run(
            ["git", "-C", str(tmp_path), "show-ref", "--verify",
             "--quiet", f"refs/heads/{branch}"],
            check=False,
        )
        assert branch_check.returncode == 0  # branch still present

    def test_delete_without_worktree_skips_archive_cleanly(
        self, tmp_path: Path,
    ) -> None:
        """Branches without a bound worktree get deleted without an
        archive entry — there's nothing to archive."""
        # Set up branch with no worktree
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "t@t"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "t"],
            check=True,
        )
        (tmp_path / "README.md").write_text("x\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"],
            check=True,
        )
        base = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--abbrev-ref",
             "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-q", "-b", "feat"],
            check=True,
        )
        (tmp_path / "f.py").write_text("x\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "-m", "feat"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-q", base], check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "--no-ff", "feat",
             "-m", "merge", "-q"], check=True,
        )

        mgr = CleanupManager(tmp_path)
        report = mgr.delete_merged_branches(["feat"])
        assert report.deleted == ["feat"]
        assert report.archived == []
        assert report.skipped_archive_failed == []


class TestCleanupReportFields:
    def test_cleanup_report_has_archive_fields(self) -> None:
        r = CleanupReport()
        assert r.archived == []
        assert r.skipped_archive_failed == []
