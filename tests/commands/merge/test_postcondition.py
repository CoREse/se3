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
    CorruptCommitGraphError,
    PostConditionViolated,
    assert_branch_merged,
    assert_head_is_merge_commit,
    assert_version_bumped,
    check_all,
    _count_parents,
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
        """A nonexistent branch triggers POSTCOND_BRANCH_UNRESOLVABLE
        (git returns 128 for bad object name) rather than the generic
        NOT_MERGED reason, so operators can distinguish "git state
        corruption" from "merge silently lost the branch".
        """
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_branch_merged(repo, "does-not-exist")
        assert exc_info.value.reason is FailureReason.POSTCOND_BRANCH_UNRESOLVABLE
        assert "git error" in exc_info.value.detail

    def test_branch_not_ancestor_detail(self, repo: Path) -> None:
        """Returncode 1 (not ancestor) uses POSTCOND_BRANCH_NOT_MERGED."""
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        # Do NOT merge feat/a.
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_branch_merged(repo, "feat/a")
        assert exc_info.value.reason is FailureReason.POSTCOND_BRANCH_NOT_MERGED
        assert "not an ancestor" in exc_info.value.detail


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

    def test_allow_fixup_parent_happy_path(self, repo: Path) -> None:
        """HEAD is a fix-up commit on top of a merge commit (fast-mode repair)."""
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        _merge_branch(repo, "feat/a")
        # Place a fix-up commit on top of the merge commit, mirroring
        # the orchestrator's fast-mode guardrail repair layout.
        _add_commit(repo, "fix.txt", "fix", "fix(specs): repair guardrail violations from 'feat/a'")
        # Without allow_fixup_parent, this would fail (HEAD has 1 parent).
        with pytest.raises(PostConditionViolated):
            assert_head_is_merge_commit(repo, "feat/a")
        # With allow_fixup_parent=True, HEAD^1 IS the merge commit so
        # the post-condition succeeds.
        assert_head_is_merge_commit(repo, "feat/a", allow_fixup_parent=True)

    def test_allow_fixup_parent_two_commits_above_merge(self, repo: Path) -> None:
        """allow_fixup_parent default depth=1 rejects two commits above merge."""
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        _merge_branch(repo, "feat/a")
        # Two commits above the merge: HEAD^1 is no longer a merge commit
        _add_commit(repo, "fix.txt", "fix", "fix1")
        _add_commit(repo, "fix2.txt", "fix2", "fix2")
        with pytest.raises(PostConditionViolated):
            assert_head_is_merge_commit(repo, "feat/a", allow_fixup_parent=True)

    def test_allow_fixup_parent_depth_2_accepts_stacked_layout(self, repo: Path) -> None:
        """Fix #8 (self-check): when fast-mode repair leaves a fix-up commit
        AND non-amend version aggregation stacks a bump commit on top, HEAD
        is two single-parent commits above the merge commit.  With
        ``allow_fixup_parent=True, max_fixup_depth=2`` the post-condition
        must accept this layout (depth=2)."""
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        _merge_branch(repo, "feat/a")
        # Layer 1: simulated guardrail-repair fix-up commit on top of merge.
        _add_commit(
            repo, "fix.txt", "fix",
            "fix(specs): repair guardrail violations from 'feat/a'",
        )
        # Layer 2: simulated non-amend version-bump commit on top of the
        # fix-up.  HEAD is now [bump → fix-up → merge_commit] (depth=2).
        _add_commit(repo, "version.txt", "1.2.3", "chore: bump version to 1.2.3")

        # Default depth (=1) rejects the layout: HEAD and HEAD^1 both have
        # 1 parent.
        with pytest.raises(PostConditionViolated):
            assert_head_is_merge_commit(
                repo, "feat/a", allow_fixup_parent=True,
            )
        # Explicit depth=2 walks back two parents and finds the merge commit.
        assert_head_is_merge_commit(
            repo, "feat/a",
            allow_fixup_parent=True,
            max_fixup_depth=2,
        )

    def test_allow_fixup_parent_depth_2_still_rejects_three_above(self, repo: Path) -> None:
        """Depth=2 must NOT silently accept three single-parent commits
        above the merge — that's outside the contract and should still
        raise so a stray hook commit is not absorbed into the post-condition.
        """
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        _merge_branch(repo, "feat/a")
        _add_commit(repo, "fix1.txt", "fix1", "fix1")
        _add_commit(repo, "fix2.txt", "fix2", "fix2")
        _add_commit(repo, "fix3.txt", "fix3", "fix3")
        with pytest.raises(PostConditionViolated):
            assert_head_is_merge_commit(
                repo, "feat/a",
                allow_fixup_parent=True,
                max_fixup_depth=2,
            )

    def test_corrupt_commit_graph_raises_typed_error(self, repo: Path, monkeypatch) -> None:
        """When _count_parents hits the safety cap, CorruptCommitGraphError is raised
        and converted to PostConditionViolated by assert_head_is_merge_commit."""
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        _merge_branch(repo, "feat/a")

        def fake_count_parents(project_root, ref, timeout):
            raise CorruptCommitGraphError(ref, 64)

        monkeypatch.setattr(
            "se3.commands.merge.postcondition._count_parents",
            fake_count_parents,
        )
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_head_is_merge_commit(repo, "feat/a")
        assert exc_info.value.reason is FailureReason.POSTCOND_HEAD_NOT_MERGE_COMMIT
        assert "corrupt" in exc_info.value.detail.lower()


class TestAssertVersionBumped:
    """Version file post-condition."""

    def test_version_present_double_quote(self, tmp_path: Path) -> None:
        pp = tmp_path / "pyproject.toml"
        pp.write_text('[project]\nversion = "1.2.3"\n')
        assert_version_bumped(tmp_path, "1.2.3")

    def test_version_present_single_quote(self, tmp_path: Path) -> None:
        pp = tmp_path / "pyproject.toml"
        pp.write_text("[project]\nversion = '1.2.3'\n")
        assert_version_bumped(tmp_path, "1.2.3")

    def test_version_missing(self, tmp_path: Path) -> None:
        pp = tmp_path / "pyproject.toml"
        pp.write_text('[project]\nversion = "1.2.3"\n')
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_version_bumped(tmp_path, "1.2.4")
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED

    def test_version_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_version_bumped(tmp_path, "1.0.0")
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED
        assert "not found" in exc_info.value.detail

    def test_custom_version_file_plain_content(self, tmp_path: Path) -> None:
        """A plain VERSION file containing ONLY the version passes."""
        vf = tmp_path / "VERSION"
        vf.write_text("2.0.0\n")
        assert_version_bumped(tmp_path, "2.0.0", version_file=vf)

    def test_version_file_with_version_prefix_rejected(self, tmp_path: Path) -> None:
        """A file with a 'version' prefix line is now rejected.

        Only plain version files (sole content is the version string) or
        structured formats (TOML / JSON) are accepted.  Prefix matching
        produced false positives on CHANGELOG and README files.
        """
        vf = tmp_path / "VERSION"
        vf.write_text("version: 2.0.0\n")
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_version_bumped(tmp_path, "2.0.0", version_file=vf)
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED

    def test_version_file_case_insensitive_prefix_rejected(self, tmp_path: Path) -> None:
        """The 'version' prefix is no longer accepted — only exact match."""
        vf = tmp_path / "VERSION"
        vf.write_text("VERSION=2.0.0\n")
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_version_bumped(tmp_path, "2.0.0", version_file=vf)
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED

    def test_unstructured_file_with_spurious_version_rejected(self, tmp_path: Path) -> None:
        """A file that mentions the version in an unrelated context must NOT pass."""
        vf = tmp_path / "CHANGELOG.md"
        vf.write_text(
            "# Changelog\n\nReleased 2.0.0 on 2024-01-01.\n"
        )
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_version_bumped(tmp_path, "2.0.0", version_file=vf)
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED
        assert "not a supported structured format" in exc_info.value.detail

    def test_spurious_match_in_comment_rejected(self, tmp_path: Path) -> None:
        """A version string in a comment or dependency must NOT pass."""
        pp = tmp_path / "pyproject.toml"
        pp.write_text(
            '[project]\n'
            'name = "foo"\n'
            'version = "1.2.3"\n'
            '# changelog: 9.9.9 was a big release\n'
            'dependencies = ["bar==9.9.9"]\n'
        )
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_version_bumped(tmp_path, "9.9.9")
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED

    def test_poetry_version_parsed(self, tmp_path: Path) -> None:
        pp = tmp_path / "pyproject.toml"
        pp.write_text(
            '[tool.poetry]\n'
            'name = "foo"\n'
            'version = "3.0.0"\n'
        )
        assert_version_bumped(tmp_path, "3.0.0")

    def test_malformed_toml_raises_violation(self, tmp_path: Path) -> None:
        """A pyproject.toml that fails to parse must raise the violation."""
        pp = tmp_path / "pyproject.toml"
        # Unclosed string and unterminated table — pure garbage.
        pp.write_text('[project\nversion = "1.2.3\n')
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_version_bumped(tmp_path, "1.2.3")
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED
        assert "Failed to parse TOML" in exc_info.value.detail

    def test_project_version_takes_precedence_over_poetry(self, tmp_path: Path) -> None:
        pp = tmp_path / "pyproject.toml"
        pp.write_text(
            '[project]\n'
            'version = "2.0.0"\n'
            '[tool.poetry]\n'
            'version = "3.0.0"\n'
        )
        # When [project] exists, its version is authoritative.
        assert_version_bumped(tmp_path, "2.0.0")
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_version_bumped(tmp_path, "3.0.0")
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED


class TestCheckAll:
    """Convenience entry-point combining all checks."""

    def test_check_all_success(self, repo: Path) -> None:
        _create_branch(repo, "feat/a")
        _add_commit(repo, "a.txt", "a", "feat a")
        _git(repo, "checkout", "master")
        _merge_branch(repo, "feat/a")
        pp = repo / "pyproject.toml"
        pp.write_text('[project]\nversion = "1.1.0"\n')
        # Commit the pyproject.toml so HEAD contains it — the strict
        # commit-tree check (introduced to catch hook-driven rewrites
        # between the bump write and commit finalization) requires
        # the working-tree and HEAD versions to agree.  In real merge
        # flow the orchestrator amends pyproject.toml onto the merge
        # commit; this test mirrors that by committing it directly.
        _git(repo, "add", "pyproject.toml")
        _git(repo, "commit", "--amend", "--no-edit")
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
        pp.write_text('[project]\nversion = "1.0.0"\n')
        with pytest.raises(PostConditionViolated) as exc_info:
            check_all(repo, "feat/a", expected_version="1.1.0")
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED


class TestAssertCommittedVersionMatches:
    """Tamper-detection checks: HEAD's committed version must match
    the version written to disk by the bump.  Simulates a commit hook
    rewriting the version between the orchestrator's disk write and
    ``git commit --amend`` finalizing.
    """

    def test_disk_matches_head_passes(self, repo: Path) -> None:
        """Happy path: working-tree and HEAD versions agree."""
        pp = repo / "pyproject.toml"
        pp.write_text('[project]\nversion = "1.2.3"\n')
        _git(repo, "add", "pyproject.toml")
        _git(repo, "commit", "-m", "bump 1.2.3")
        assert_version_bumped(repo, "1.2.3", check_commit_tree=True)

    def test_hook_rewrote_version_string_detected(
        self, repo: Path
    ) -> None:
        """A commit hook that committed a different version string than
        what is on disk MUST raise PostConditionViolated.

        Simulation: write 1.2.3 to disk, but commit with a different
        version (1.0.0) — the working-tree assertion succeeds for 1.2.3,
        but the commit-tree check should observe the mismatch.
        """
        pp = repo / "pyproject.toml"
        # First commit a "tampered" version.
        pp.write_text('[project]\nversion = "1.0.0"\n')
        _git(repo, "add", "pyproject.toml")
        _git(repo, "commit", "-m", "tampered version")
        # Now simulate the orchestrator overwriting the file on disk
        # with the expected version, but HEAD still has the tampered
        # commit.
        pp.write_text('[project]\nversion = "1.2.3"\n')
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_version_bumped(repo, "1.2.3", check_commit_tree=True)
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED
        assert "HEAD" in exc_info.value.detail or "committed" in exc_info.value.detail

    def test_hook_stripped_version_field_detected(
        self, repo: Path
    ) -> None:
        """A commit hook that stripped the version field at HEAD MUST
        raise PostConditionViolated.
        """
        pp = repo / "pyproject.toml"
        # Commit a TOML file that parses cleanly but has no version.
        pp.write_text('[project]\nname = "foo"\n')
        _git(repo, "add", "pyproject.toml")
        _git(repo, "commit", "-m", "no version field")
        # Now write the expected version on disk only.
        pp.write_text('[project]\nname = "foo"\nversion = "1.2.3"\n')
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_version_bumped(repo, "1.2.3", check_commit_tree=True)
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED

    def test_hook_corrupted_committed_toml_detected(
        self, repo: Path
    ) -> None:
        """A commit hook that broke TOML parsing at HEAD MUST raise
        PostConditionViolated.

        Strategy: commit clean TOML, then rewrite-history so HEAD has
        garbage TOML.  Working tree contains valid TOML so the primary
        assertion succeeds; the commit-tree check catches the corruption.
        """
        pp = repo / "pyproject.toml"
        # Add a clean version first so the disk file parses.
        pp.write_text('[project]\nversion = "1.2.3"\n')
        _git(repo, "add", "pyproject.toml")
        _git(repo, "commit", "-m", "clean version")
        # Now create a commit whose tree contains corrupt TOML, then
        # reset the working tree to the clean version while leaving
        # HEAD pointing at the corrupt commit.
        pp.write_text('[project\nversion = "1.2.3\n')  # unclosed string
        _git(repo, "add", "pyproject.toml")
        _git(repo, "commit", "-m", "corrupt commit")
        # Working tree gets the clean content again.
        pp.write_text('[project]\nversion = "1.2.3"\n')
        with pytest.raises(PostConditionViolated) as exc_info:
            assert_version_bumped(repo, "1.2.3", check_commit_tree=True)
        assert exc_info.value.reason is FailureReason.POSTCOND_VERSION_NOT_BUMPED
        assert "parseable" in exc_info.value.detail or "TOML" in exc_info.value.detail

    def test_check_commit_tree_disabled_skips_tamper_check(
        self, repo: Path
    ) -> None:
        """When ``check_commit_tree=False`` (or default behaviour
        consistent with no commit), the tamper check does not run —
        operators can opt out for environments where HEAD is not yet
        a commit (initial bump).
        """
        pp = repo / "pyproject.toml"
        pp.write_text('[project]\nversion = "1.2.3"\n')
        # No commit at all; ``check_commit_tree=False`` should not raise.
        assert_version_bumped(repo, "1.2.3", check_commit_tree=False)
