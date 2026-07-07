"""Regression test for ``fast`` strategy inheriting the original
``robust`` strategy's dirty-worktree stash behavior (G7 task 4 /
task vii).

A dirty working tree at merge start should be auto-stashed; after a
successful merge the stash is popped so the user's WIP is restored.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from se3.commands.merge_cmd import (
    _fast_stash_dirty,
    _fast_stash_pop,
    _has_user_uncommitted_changes,
    run_merge,
)


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


def _porcelain(path: Path) -> str:
    return _git(path, "status", "--porcelain").stdout


def test_fast_stash_pop_round_trip(tmp_path: Path) -> None:
    """``_fast_stash_dirty`` followed by ``_fast_stash_pop`` returns the
    working tree to the pre-stash dirty state on a non-conflicting
    intermediate operation.
    """
    default = _init_repo(tmp_path)

    # Make the working tree dirty: modify a tracked file AND add an
    # untracked file.
    (tmp_path / "README.md").write_text("modified base\n")
    (tmp_path / "wip.txt").write_text("untracked WIP\n")
    pre_status = _porcelain(tmp_path)
    assert pre_status != ""  # confirm preconditions

    # The pre-stash dirty check should agree.
    assert _has_user_uncommitted_changes(tmp_path) is True

    audit: list[str] = []
    label = _fast_stash_dirty(tmp_path, audit)
    assert label is not None
    assert label.startswith("se3-pre-fast-merge-")
    # After stash, working tree is clean.
    assert _porcelain(tmp_path).strip() == ""
    # An audit message was recorded.
    assert any("Auto-stashed" in m for m in audit)

    # No intermediate operation needed for the round-trip test — we
    # just pop and confirm restoration.
    _fast_stash_pop(tmp_path, label, audit)

    # The dirty state is restored bit-for-bit (or close).
    post_status = _porcelain(tmp_path)
    assert post_status == pre_status, (
        f"post-pop status differs:\nbefore:\n{pre_status!r}\n"
        f"after:\n{post_status!r}"
    )


def test_fast_strategy_run_merge_stashes_dirty_tree(
    tmp_path: Path,
) -> None:
    """End-to-end: ``run_merge`` in fast mode on a dirty working tree
    auto-stashes the dirty state, performs the merge, and pops the
    stash on the way out.
    """
    default = _init_repo(tmp_path)
    _make_feature_branch(tmp_path, "feature", "feat.txt")
    _git(tmp_path, "checkout", default)

    # Now make the working tree dirty on the *target* branch — this is
    # what the fast strategy's stash path is designed for.
    (tmp_path / "README.md").write_text("dirty WIP content\n")
    (tmp_path / "scratch.txt").write_text("untracked WIP\n")
    pre_status = _porcelain(tmp_path)
    assert pre_status != ""

    exit_code = run_merge(
        branches=["feature"],
        strategy="fast",
        delete_merged=False,
        project_root=tmp_path,
    )
    assert exit_code == 0

    # After fast merge, the user's WIP should be back in the tree.
    post_status = _porcelain(tmp_path)
    # The merge brought in feat.txt; the WIP files should also be back.
    assert "README.md" in post_status
    assert "scratch.txt" in post_status
    # feat.txt should now be committed (no entry in porcelain).
    assert (tmp_path / "feat.txt").exists()


def test_incomplete_stash_pop_preserves_branch_under_delete_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the post-merge stash-pop does NOT finalize, ``run_merge``
    skips ``reconcile()`` and returns non-zero, advertising a
    whole-command rerun. The deferred ``--delete-merged`` cleanup MUST
    therefore preserve the source branch: no version-reconcile commit was
    created, so deleting the branch would make the rerun fail branch
    validation and strand the merged intent on master with no reconcile
    bump/changelog.
    """
    import se3.commands.merge_cmd as merge_cmd

    default = _init_repo(tmp_path)
    _make_feature_branch(tmp_path, "feature", "feat.txt")
    _git(tmp_path, "checkout", default)

    # Dirty target tree so the fast strategy takes the stash path.
    (tmp_path / "README.md").write_text("dirty WIP content\n")
    (tmp_path / "scratch.txt").write_text("untracked WIP\n")

    # Force the stash-pop to report "incomplete" — the failure mode this
    # regression guards. The branch merge still lands; only WIP restoration
    # is unfinalised.
    monkeypatch.setattr(
        merge_cmd, "_fast_stash_pop", lambda *a, **k: True,
    )

    exit_code = run_merge(
        branches=["feature"],
        strategy="fast",
        delete_merged=True,
        project_root=tmp_path,
    )
    # Stash-pop-incomplete is a recoverable failure.
    assert exit_code != 0

    # The branch survives so the documented `se3 merge feature` rerun can
    # re-attempt the version decision against it.
    branches = _git(tmp_path, "branch", "--list", "feature").stdout
    assert "feature" in branches, (
        "source branch was deleted despite reconcile being skipped — "
        "the advertised rerun path would fail branch validation"
    )
