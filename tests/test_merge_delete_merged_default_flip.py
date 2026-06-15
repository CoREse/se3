"""Regression tests for the delete-merged default flip (G7 task 3 / task x).

When ``se3 merge`` is invoked without ``--delete-merged`` or
``--no-delete-merged``, the new default behaviour is to:

* delete the merged branch (lowercase ``git branch -d``)
* archive its bound worktree under
  ``<project_root>/se3/worktrees/.archive/<slug>-<ts>/`` before the
  destructive removal.

When ``--no-delete-merged`` is supplied, the branch and worktree are
both preserved and no archive directory is created.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("base\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _make_feature_branch(path: Path, name: str, file_name: str) -> None:
    _git(path, "checkout", "-b", name)
    (path / file_name).write_text(f"content for {name}\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", f"add {file_name}")


def _branch_exists(path: Path, name: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--verify", "--quiet",
         f"refs/heads/{name}"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _archive_dirs(path: Path) -> list[Path]:
    archive_root = path / "se3" / "worktrees" / ".archive"
    if not archive_root.exists():
        return []
    return sorted(p for p in archive_root.iterdir() if p.is_dir())


# ---------------------------------------------------------------------
# Task (x) — default behaviour deletes branch and archives worktree
# ---------------------------------------------------------------------


def test_run_merge_default_deletes_branch_and_archives_worktree(
    tmp_path: Path, monkeypatch,
) -> None:
    """``run_merge`` without overriding ``delete_merged`` deletes the
    merged branch.  When the branch had a bound worktree, the worktree
    is archived to ``se3/worktrees/.archive/`` before deletion.
    """
    default = _init_repo(tmp_path)
    _make_feature_branch(tmp_path, "feature", "feat.txt")
    _git(tmp_path, "checkout", default)

    # Bind a worktree to the feature branch.
    worktree_path = tmp_path / ".." / f"wt-{tmp_path.name}-feature"
    worktree_path = worktree_path.resolve()
    _git(tmp_path, "worktree", "add", str(worktree_path), "feature")

    _git(tmp_path, "checkout", default)

    # Confirm preconditions.
    assert _branch_exists(tmp_path, "feature") is True
    assert worktree_path.exists()
    assert _archive_dirs(tmp_path) == []

    # Run the merge with default delete_merged (True).
    from se3.commands.merge_cmd import run_merge

    exit_code = run_merge(
        branches=["feature"],
        project_root=tmp_path,
    )

    assert exit_code == 0
    # Branch should be deleted by default now.
    assert _branch_exists(tmp_path, "feature") is False
    # Archive should exist under se3/worktrees/.archive/feature-<ts>/.
    archives = _archive_dirs(tmp_path)
    assert archives, (
        "expected an archive directory under se3/worktrees/.archive/"
    )
    archived_for_feature = [d for d in archives if d.name.startswith("feature-")]
    assert archived_for_feature, (
        f"no feature-* archive found under se3/worktrees/.archive/; "
        f"saw: {archives}"
    )
    # Worktree directory itself should be gone.
    assert not worktree_path.exists()


def test_run_merge_no_delete_merged_preserves_branch_and_skips_archive(
    tmp_path: Path,
) -> None:
    """Passing ``delete_merged=False`` preserves both branch and its
    bound worktree, and no archive directory is created.
    """
    default = _init_repo(tmp_path)
    _make_feature_branch(tmp_path, "feature", "feat.txt")
    _git(tmp_path, "checkout", default)

    worktree_path = tmp_path / ".." / f"wt-{tmp_path.name}-feature2"
    worktree_path = worktree_path.resolve()
    _git(tmp_path, "worktree", "add", str(worktree_path), "feature")

    _git(tmp_path, "checkout", default)

    from se3.commands.merge_cmd import run_merge

    exit_code = run_merge(
        branches=["feature"],
        delete_merged=False,
        project_root=tmp_path,
    )

    assert exit_code == 0
    # Branch preserved.
    assert _branch_exists(tmp_path, "feature") is True
    # Worktree preserved.
    assert worktree_path.exists()
    # No archive directory.
    assert _archive_dirs(tmp_path) == []


def test_cli_no_delete_merged_flag_overrides_default(
    tmp_path: Path, monkeypatch,
) -> None:
    """The ``--no-delete-merged`` CLI flag turns off the default-on
    deletion behaviour."""
    _init_repo(tmp_path)
    captured: dict[str, object] = {}

    def stub_run_merge(
        branches, strategy="fast", delete_merged=True,
        strict_runtime_sync=False, project_root=None,
    ):
        captured["delete_merged"] = delete_merged
        captured["strategy"] = strategy
        return 0

    monkeypatch.setattr("se3.commands.merge_cmd.run_merge", stub_run_merge)

    import os
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        from typer.testing import CliRunner
        from se3.cli import app

        runner = CliRunner()
        result = runner.invoke(
            app, ["merge", "feature", "--no-delete-merged"],
        )
        assert result.exit_code == 0, result.output
        assert captured.get("delete_merged") is False
    finally:
        os.chdir(old_cwd)


def test_cli_default_delete_merged_is_on(
    tmp_path: Path, monkeypatch,
) -> None:
    """Without ``--no-delete-merged``, the CLI passes
    ``delete_merged=True`` (resolved from ``MergeConfig`` default).
    """
    _init_repo(tmp_path)
    captured: dict[str, object] = {}

    def stub_run_merge(
        branches, strategy="fast", delete_merged=True,
        strict_runtime_sync=False, project_root=None,
    ):
        captured["delete_merged"] = delete_merged
        return 0

    monkeypatch.setattr("se3.commands.merge_cmd.run_merge", stub_run_merge)

    import os
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        from typer.testing import CliRunner
        from se3.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["merge", "feature"])
        assert result.exit_code == 0, result.output
        assert captured.get("delete_merged") is True
    finally:
        os.chdir(old_cwd)
