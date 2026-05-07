"""Tests for postcondition assertions.

Uses real git repositories (subprocess) to exercise the assertions
against actual git state.  This is intentional: postconditions are
security-critical and mocking git responses would undermine the test
value.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from se3.commands.merge.failure_reason import FailureReason
from se3.commands.merge.postcondition import (
    PostConditionViolated,
    assert_branch_merged,
    assert_head_is_merge_commit,
    assert_version_bumped,
    check_all,
)


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path)] + list(args), check=True, capture_output=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# Test\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")


def _create_branch(path: Path, branch: str) -> None:
    _git(path, "checkout", "-b", branch)


def _add_commit(path: Path, filename: str, content: str, message: str) -> None:
    (path / filename).write_text(content)
    _git(path, "add", filename)
    _git(path, "commit", "-m", message)


def _merge_branch(path: Path, branch: str, message: str = "merge") -> None:
    _git(path, "merge", "--no-ff", "-m", message, branch)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    _init_repo(path)
    return path


class TestAssertBranchMerged:
    """Ancestry post-condition."""

    def test_branch_is_ancestor(self, repo: Path) -> None:
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        _merge_branch(repo, "feat/a")
        # Should not raise.
        assert_branch_merged(repo, "feat/a")

    def test_branch_not_ancestor(self, repo: Path) -> None:
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        # Do NOT merge feat/a.
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_branch_merged(repo, "feat/a")
        assert exc_info.value.reason is FailureReason.POSTCOND_BRANCH_NOT_MERGED
        assert exc_info.value.branch == "feat/a"

    def test_nonexistent_branch(self, repo: Path) -> None:
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_branch_merged(repo, "does-not-exist")
        assert exc_info.value.reason is FailureReason.POSTCOND_BRANCH_NOT_MERGED


class TestAssertHeadIsMergeCommit:
    """HEAD merge-commit post-condition."""

    def test_head_is_merge_commit(self, repo: Path) -> None:
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        _merge_branch(repo, "feat/a")
        # Should not raise.
        assert_head_is_merge_commit(repo, "feat/a")

    def test_head_is_not_merge_commit(self, repo: Path) -> None:
        _add_commit(repo, "solo.txt", "solo", "solo commit")
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_head_is_merge_commit(repo, "feat/a")
        assert exc_info.value.reason is FailureReason.POSTCOND_HEAD_NOT_MERGE_COMMIT
        assert "1 parent(s)" in exc_info.value.detail

    def test_octopus_merge(self, repo: Path) -> None:
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        _create_branch(repo, "feat/b")
        _add_commit(repo, "b.txt", "b", "feat b")
        _git(repo, "checkout", "master")
        _create_branch(repo, "feat/c")
        _add_commit(repo, "c.txt", "c", "feat c")
        _git(repo, "checkout", "master")
        # Octopus merge.
        _git(repo, "merge", "-m", "octopus", "feat/a", "feat/b", "feat/c")
        # HEAD has 4 parents (initial + 3 branches).
        assert_head_is_merge_commit(repo, "feat/a", min_parents=2)
        assert_head_is_merge_commit(repo, "feat/a", min_parents=3)
        # But not 4.
        with pytest.raises(PostConditionViolated):
            assert_head_is_merge_commit(repo, "feat/a", min_parents=4)

    def test_empty_repo(self, tmp_path: Path) -> None:
        """An empty repo has no HEAD."""
        path = tmp_path / "empty"
        path.mkdir()
        _git(path, "init")
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_head_is_merge_commit(path, "any")
        assert exc_info.value.reason is FailureReason.POSTCOND_HEAD_NOT_MERGE_COMMIT


class TestAssertVersionBumped:
    """Version file post-condition."""

    def test_version_present_double_quote(self, tmp_path: Path) -> None:
        pp = tmp_path / "pyproject.toml"
        pp.write_text('version = "1.2.3"\n')
        assert_version_bumped(tmp_path, "1.2.3")

    def test_version_present_single_quote(self, tmp_path: Path) -> None:
        pp = tmp_path / "pyproject.toml"
        pp.write_text("version = '1.2.3'\n")
        assert_version_bumped(tmp_path, "1.2.3")

    def test_version_missing(self, tmp_path: Path) -> None:
        pp = tmp_path / "pyproject.toml"
        pp.write_text('version = "1.2.3"\n')
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_version_bumped(tmp_path, "1.2.4")
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED

    def test_version_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_version_bumped(tmp_path, "1.0.0")
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED
        assert "not found" in exc_info.value.detail

    def test_custom_version_file(self, tmp_path: Path) -> None:
        vf = tmp_path / "VERSION"
        vf.write_text("2.0.0\n")
        assert_version_bumped(tmp_path, "2.0.0", version_file=vf)


class TestCheckAll:
    """Convenience entry-point combining all checks."""

    def test_check_all_success(self, repo: Path) -> None:
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        _merge_branch(repo, "feat/a")
        pp = repo / "pyproject.toml"
        pp.write_text('version = "1.1.0"\n')
        # Should not raise.
        check_all(repo, "feat/a", expected_version="1.1.0")

    def test_check_all_skips_merge_commit_for_ancestor(self, repo: Path) -> None:
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        # Fast-forward (no merge commit).
        _git(repo, "merge", "--ff-only", "feat/a")
        # With already_ancestor=True, skip merge-commit check.
        check_all(repo, "feat/a", already_ancestor=True)

    def test_check_all_fails_on_branch_not_merged(self, repo: Path) -> None:
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        # Do not merge.
        with pytest.raises(PostConditionViolated) as exc_info:
            check_all(repo, "feat/a")
        assert exc_info.value.reason is FailureReason.POSTCOND_BRANCH_NOT_MERGED

    def test_check_all_fails_on_version_mismatch(self, repo: Path) -> None:
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        _merge_branch(repo, "feat/a")
        pp = repo / "pyproject.toml"
        pp.write_text('version = "1.0.0"\n')
        with pytest.raises(PostConditionViolated) as exc_info:
            check_all(repo, "feat/a", expected_version="1.1.0")
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED
