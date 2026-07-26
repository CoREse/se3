"""Tests for implement step session-commit collection helpers.

Covers `_collect_session_commits` and `_read_pre_session_version` from
`tianluo.engine.steps.implement`. These helpers feed `version_analyze` with
the pre-implement project version and the list of commits implement
introduced on the main branch (worktree path), letting `version_analyze`
discount any inadvertent version-file bumps inside those commits.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tianluo.engine.steps.implement import (
    _collect_session_commits,
    _read_pre_session_version,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(repo),
        check=False,
    )


def _git_ok(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = _git(repo, *args)
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed: rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return result


@pytest.fixture
def git_repo(tmp_path):
    """Initialise a git repo with one baseline commit and return its path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    if _git(repo, "--version").returncode != 0:
        pytest.skip("git not available")
    _git_ok(repo, "init")
    _git_ok(repo, "config", "user.email", "test@example.com")
    _git_ok(repo, "config", "user.name", "Test User")
    _git_ok(repo, "config", "commit.gpgsign", "false")
    _git_ok(repo, "config", "init.defaultBranch", "master")
    (repo / "README.md").write_text("hello\n")
    _git_ok(repo, "add", "README.md")
    _git_ok(repo, "commit", "-m", "baseline")
    return repo


def _head(repo: Path) -> str:
    return _git_ok(repo, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# _collect_session_commits
# ---------------------------------------------------------------------------


class TestCollectSessionCommits:
    def test_collects_new_commits_with_files(self, git_repo):
        baseline = _head(git_repo)

        # Simulate worktree merge-back: two new commits on the main branch.
        (git_repo / "pyproject.toml").write_text(
            'version = "5.2.0"\n'
        )
        _git_ok(git_repo, "add", "pyproject.toml")
        _git_ok(git_repo, "commit", "-m", "bump version to 5.2.0")

        (git_repo / "src.py").write_text("print('hi')\n")
        _git_ok(git_repo, "add", "src.py")
        _git_ok(git_repo, "commit", "-m", "add src.py")

        commits = _collect_session_commits(git_repo, baseline)
        assert len(commits) == 2

        # git log is newest-first; the latest commit is the src.py add.
        latest, first = commits
        assert latest["subject"] == "add src.py"
        assert latest["files"] == ["src.py"]
        assert latest["sha"] and len(latest["sha"]) == 40

        assert first["subject"] == "bump version to 5.2.0"
        assert first["files"] == ["pyproject.toml"]
        assert first["sha"] and len(first["sha"]) == 40

    def test_excludes_merge_commits(self, git_repo):
        baseline = _head(git_repo)

        # Create a side branch with a commit
        _git_ok(git_repo, "checkout", "-b", "side")
        (git_repo / "side.py").write_text("x = 1\n")
        _git_ok(git_repo, "add", "side.py")
        _git_ok(git_repo, "commit", "-m", "side change")

        # Back to baseline branch and add a divergent commit
        default_branch = "master"
        _git_ok(git_repo, "checkout", default_branch)
        (git_repo / "main.py").write_text("y = 2\n")
        _git_ok(git_repo, "add", "main.py")
        _git_ok(git_repo, "commit", "-m", "main change")

        # Force a real merge commit (no fast-forward)
        merge_result = _git(
            git_repo, "merge", "--no-ff", "side", "-m", "merge side",
        )
        assert merge_result.returncode == 0, merge_result.stderr

        commits = _collect_session_commits(git_repo, baseline)
        subjects = [c["subject"] for c in commits]
        # The merge commit must be filtered out; the two underlying commits
        # remain.
        assert "merge side" not in subjects
        assert "side change" in subjects
        assert "main change" in subjects

    def test_baseline_none_returns_empty(self, git_repo):
        assert _collect_session_commits(git_repo, None) == []

    def test_no_new_commits_returns_empty(self, git_repo):
        baseline = _head(git_repo)
        assert _collect_session_commits(git_repo, baseline) == []

    def test_git_failure_returns_empty(self, tmp_path):
        # Pointing at a non-git directory: git log will fail; helper
        # must return [] without raising.
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        assert _collect_session_commits(not_a_repo, "deadbeef") == []


# ---------------------------------------------------------------------------
# _read_pre_session_version
# ---------------------------------------------------------------------------


class TestReadPreSessionVersion:
    def test_reads_pyproject_version(self, tmp_path):
        project_root = tmp_path / "proj"
        project_root.mkdir()
        (project_root / "pyproject.toml").write_text(
            '[project]\nname = "demo"\nversion = "5.1.0"\n'
        )
        assert _read_pre_session_version(project_root) == "5.1.0"

    def test_missing_version_file_returns_none(self, tmp_path):
        project_root = tmp_path / "proj"
        project_root.mkdir()
        assert _read_pre_session_version(project_root) is None

    def test_failure_does_not_raise(self, tmp_path):
        project_root = tmp_path / "proj"
        project_root.mkdir()
        # Force VersionBumper to blow up; helper must swallow and return None.
        with patch(
            "tianluo.engine.version_bumper.VersionBumper.detect_version_file",
            side_effect=RuntimeError("boom"),
        ):
            assert _read_pre_session_version(project_root) is None
