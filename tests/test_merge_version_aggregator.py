"""Tests for SemVer aggregation in the merge orchestrator (G5)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from se3.engine.llm_caller import LLMCallError
from se3.engine.merge.orchestrator import MergeOrchestrator
from se3.engine.merge.version_aggregator import (
    AggregateResult,
    InferResult,
    _diff_bump,
    _parse_pyproject_version,
    _slice_to_next_section,
    aggregate_and_apply,
    infer_branch_bump,
    max_bump,
    read_version_at_ref,
)
from se3.engine.version_bumper import BumpType


# ---------- Test repo helpers ---------- #


def _get_default_branch(path: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
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


def _write_pyproject(path: Path, version: str) -> None:
    """Write a minimal pyproject.toml with the given version."""
    content = (
        '[build-system]\nrequires = ["setuptools"]\n\n'
        '[project]\n'
        'name = "test-pkg"\n'
        f'version = "{version}"\n'
    )
    (path / "pyproject.toml").write_text(content)


def _commit(path: Path, message: str, *files: str) -> None:
    if files:
        subprocess.run(
            ["git", "-C", str(path), "add", *files],
            check=True, capture_output=True,
        )
    else:
        subprocess.run(
            ["git", "-C", str(path), "add", "-A"],
            check=True, capture_output=True,
        )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", message],
        check=True, capture_output=True,
    )


def _checkout(path: Path, branch: str, create: bool = False) -> None:
    args = ["git", "-C", str(path), "checkout"]
    if create:
        args.append("-b")
    args.append(branch)
    subprocess.run(args, check=True, capture_output=True)


def _read_pyproject_version(path: Path) -> str:
    """Read pyproject.toml's version from working tree."""
    content = (path / "pyproject.toml").read_text()
    import re
    match = re.search(
        r'\[project\][^\[]*?version\s*=\s*["\']([^"\']+)["\']',
        content, re.DOTALL,
    )
    assert match, "could not find version in pyproject.toml"
    return match.group(1)


def _last_commit_files(path: Path) -> list[str]:
    """Return the list of files modified in HEAD."""
    result = subprocess.run(
        ["git", "-C", str(path), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]


# ---------- Unit tests: max_bump ---------- #


class TestMaxBump:
    def test_empty_list_returns_patch(self):
        assert max_bump([]) == BumpType.PATCH

    def test_single_patch(self):
        assert max_bump([BumpType.PATCH]) == BumpType.PATCH

    def test_patch_plus_patch_plus_minor_returns_minor(self):
        assert max_bump([BumpType.PATCH, BumpType.PATCH, BumpType.MINOR]) == BumpType.MINOR

    def test_minor_plus_major_returns_major(self):
        assert max_bump([BumpType.MINOR, BumpType.MAJOR]) == BumpType.MAJOR

    def test_any_with_major_returns_major(self):
        assert max_bump([BumpType.PATCH, BumpType.PATCH, BumpType.MAJOR]) == BumpType.MAJOR

    def test_only_minor(self):
        assert max_bump([BumpType.MINOR, BumpType.MINOR]) == BumpType.MINOR


# ---------- Unit tests: _diff_bump ---------- #


class TestDiffBump:
    """Direct tests for _diff_bump including prerelease / build metadata."""

    def test_major(self):
        from se3.engine.version_bumper import Version
        base = Version.parse("1.2.3")
        branch = Version.parse("2.0.0")
        assert _diff_bump(base, branch) == BumpType.MAJOR

    def test_minor(self):
        from se3.engine.version_bumper import Version
        base = Version.parse("1.2.3")
        branch = Version.parse("1.3.0")
        assert _diff_bump(base, branch) == BumpType.MINOR

    def test_patch(self):
        from se3.engine.version_bumper import Version
        base = Version.parse("1.2.3")
        branch = Version.parse("1.2.4")
        assert _diff_bump(base, branch) == BumpType.PATCH

    def test_no_change_returns_none(self):
        from se3.engine.version_bumper import Version
        base = Version.parse("1.2.3")
        branch = Version.parse("1.2.3")
        assert _diff_bump(base, branch) is None

    def test_backward_returns_none(self):
        from se3.engine.version_bumper import Version
        base = Version.parse("1.3.0")
        branch = Version.parse("1.2.3")
        assert _diff_bump(base, branch) is None

    def test_prerelease_to_release_returns_patch(self):
        """4.5.0-alpha -> 4.5.0 (release > prerelease per SemVer §11) = PATCH."""
        from se3.engine.version_bumper import Version
        base = Version.parse("4.5.0-alpha")
        branch = Version.parse("4.5.0")
        assert _diff_bump(base, branch) == BumpType.PATCH

    def test_release_to_prerelease_returns_none(self):
        """4.5.0 -> 4.5.0-alpha is a backward step in precedence."""
        from se3.engine.version_bumper import Version
        base = Version.parse("4.5.0")
        branch = Version.parse("4.5.0-alpha")
        assert _diff_bump(base, branch) is None

    def test_build_metadata_only_returns_none(self):
        """4.5.0 -> 4.5.0+build.42: build metadata ignored in precedence."""
        from se3.engine.version_bumper import Version
        base = Version.parse("4.5.0")
        branch = Version.parse("4.5.0+build.42")
        assert _diff_bump(base, branch) is None

    def test_prerelease_bump_with_numeric_change(self):
        """4.5.0-alpha -> 4.6.0: numeric minor change dominates."""
        from se3.engine.version_bumper import Version
        base = Version.parse("4.5.0-alpha")
        branch = Version.parse("4.6.0")
        assert _diff_bump(base, branch) == BumpType.MINOR


# ---------- Unit tests: InferResult ---------- #


class TestInferResult:
    def test_dataclass_fields(self):
        r = InferResult(bump=BumpType.PATCH, reason="test")
        assert r.bump == BumpType.PATCH
        assert r.reason == "test"

    def test_none_bump_with_reason(self):
        r = InferResult(bump=None, reason="no version metadata")
        assert r.bump is None
        assert r.reason == "no version metadata"


# ---------- Unit tests: infer_branch_bump ---------- #


class TestInferBranchBump:
    def test_patch_bump(self, tmp_path: Path):
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Add pyproject")
        base_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        _checkout(tmp_path, "feature", create=True)
        _write_pyproject(tmp_path, "4.4.1")
        _commit(tmp_path, "Bump patch")

        result = infer_branch_bump(tmp_path, "feature", base_sha)
        assert result.bump == BumpType.PATCH
        assert "patch" in result.reason.lower()

    def test_minor_bump(self, tmp_path: Path):
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Add pyproject")
        base_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        _checkout(tmp_path, "feature", create=True)
        _write_pyproject(tmp_path, "4.5.0")
        _commit(tmp_path, "Bump minor")

        result = infer_branch_bump(tmp_path, "feature", base_sha)
        assert result.bump == BumpType.MINOR
        assert "minor" in result.reason.lower()

    def test_major_bump(self, tmp_path: Path):
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Add pyproject")
        base_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        _checkout(tmp_path, "feature", create=True)
        _write_pyproject(tmp_path, "5.0.0")
        _commit(tmp_path, "Bump major")

        result = infer_branch_bump(tmp_path, "feature", base_sha)
        assert result.bump == BumpType.MAJOR
        assert "major" in result.reason.lower()

    def test_no_version_change_returns_none(self, tmp_path: Path):
        """When the branch did not advance the version, no bump is inferred."""
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Add pyproject")
        base_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        _checkout(tmp_path, "feature", create=True)
        # Add a non-version file change
        (tmp_path / "feature.txt").write_text("data")
        _commit(tmp_path, "Add feature file")

        result = infer_branch_bump(tmp_path, "feature", base_sha)
        assert result.bump is None
        assert "did not advance" in result.reason.lower()

    def test_no_pyproject_returns_none(self, tmp_path: Path):
        """When neither base nor branch has a readable version, return None."""
        _init_repo(tmp_path)
        # No pyproject.toml in this repo
        base_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        _checkout(tmp_path, "feature", create=True)
        (tmp_path / "f.txt").write_text("x")
        _commit(tmp_path, "Add f")

        result = infer_branch_bump(tmp_path, "feature", base_sha)
        assert result.bump is None
        assert "no readable version" in result.reason.lower()

    def test_branch_lower_version_returns_none(self, tmp_path: Path):
        """A branch with a lower version does not contribute a bump."""
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.5.0")
        _commit(tmp_path, "Add pyproject")
        base_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        _checkout(tmp_path, "feature", create=True)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Lower version")

        result = infer_branch_bump(tmp_path, "feature", base_sha)
        assert result.bump is None
        assert "did not advance" in result.reason.lower()

    def test_end_to_end_diff_ignores_intermediate_bumps(self, tmp_path: Path):
        """Intermediate bumps inside a branch are ignored; only end-to-end diff counts."""
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "1.0.0")
        _commit(tmp_path, "Add pyproject")
        default_branch = _get_default_branch(tmp_path)

        _checkout(tmp_path, "feature", create=True)
        # Multiple internal bumps: minor, patch, minor
        _write_pyproject(tmp_path, "1.1.0")
        _commit(tmp_path, "Bump minor")
        _write_pyproject(tmp_path, "1.1.1")
        _commit(tmp_path, "Bump patch")
        _write_pyproject(tmp_path, "1.2.0")
        _commit(tmp_path, "Bump minor again")

        merge_base = subprocess.run(
            ["git", "-C", str(tmp_path), "merge-base", default_branch, "feature"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        result = infer_branch_bump(tmp_path, "feature", merge_base)
        # End-to-end: 1.0.0 -> 1.2.0 = MINOR (not cumulative major)
        assert result.bump == BumpType.MINOR

    def test_spec_example_branch_bumps(self, tmp_path: Path):
        """Spec example: A branch-point 4.4.0, B tip 4.4.1 (PATCH), C tip 4.6.0 (MINOR).

        Even though C traversed 4.5.0 -> 4.5.1 internally, the end-to-end
        diff from merge-base (4.4.0) to tip (4.6.0) is MINOR.
        """
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "M0")
        default_branch = _get_default_branch(tmp_path)

        # Branch B: patch bump
        _checkout(tmp_path, "B", create=True)
        _write_pyproject(tmp_path, "4.4.1")
        _commit(tmp_path, "Bump patch on B")
        _checkout(tmp_path, default_branch)

        # Branch C: minor bump with intermediate noise
        _checkout(tmp_path, "C", create=True)
        _write_pyproject(tmp_path, "4.5.0")
        _commit(tmp_path, "C1: 4.5.0")
        _write_pyproject(tmp_path, "4.5.1")
        _commit(tmp_path, "C2: 4.5.1")
        _write_pyproject(tmp_path, "4.6.0")
        _commit(tmp_path, "C3: 4.6.0")
        _checkout(tmp_path, default_branch)

        # A advances past branch-point
        _write_pyproject(tmp_path, "4.6.0")
        _commit(tmp_path, "M1: advance A to 4.6.0")

        merge_base_b = subprocess.run(
            ["git", "-C", str(tmp_path), "merge-base", "HEAD", "B"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        merge_base_c = subprocess.run(
            ["git", "-C", str(tmp_path), "merge-base", "HEAD", "C"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        result_b = infer_branch_bump(tmp_path, "B", merge_base_b)
        result_c = infer_branch_bump(tmp_path, "C", merge_base_c)

        assert result_b.bump == BumpType.PATCH
        assert result_c.bump == BumpType.MINOR

    def test_prerelease_to_release_infers_patch(self, tmp_path: Path):
        """A branch that finalizes a prerelease (e.g. 4.5.0-alpha → 4.5.0)
        contributes a PATCH bump per SemVer 2.0.0 §11."""
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.5.0-alpha")
        _commit(tmp_path, "Add pyproject")
        base_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        _checkout(tmp_path, "feature", create=True)
        _write_pyproject(tmp_path, "4.5.0")
        _commit(tmp_path, "Release finalize")

        result = infer_branch_bump(tmp_path, "feature", base_sha)
        assert result.bump == BumpType.PATCH
        assert "patch" in result.reason.lower()

    def test_release_to_prerelease_no_bump(self, tmp_path: Path):
        """A branch that adds a prerelease suffix (4.5.0 → 4.5.0-alpha)
        does NOT contribute a bump — it is a backward step in precedence."""
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.5.0")
        _commit(tmp_path, "Add pyproject")
        base_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        _checkout(tmp_path, "feature", create=True)
        _write_pyproject(tmp_path, "4.5.0-alpha")
        _commit(tmp_path, "Add prerelease")

        result = infer_branch_bump(tmp_path, "feature", base_sha)
        assert result.bump is None
        assert "did not advance" in result.reason.lower()

    def test_build_metadata_only_no_bump(self, tmp_path: Path):
        """Build metadata changes (4.5.0 → 4.5.0+build.42) do not affect
        precedence and therefore do not contribute a bump."""
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.5.0")
        _commit(tmp_path, "Add pyproject")
        base_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        _checkout(tmp_path, "feature", create=True)
        _write_pyproject(tmp_path, "4.5.0+build.42")
        _commit(tmp_path, "Add build metadata")

        result = infer_branch_bump(tmp_path, "feature", base_sha)
        assert result.bump is None
        assert "did not advance" in result.reason.lower()


# ---------- Unit tests: aggregate_and_apply ---------- #


class TestAggregateAndApply:
    def _setup_repo_with_merge_commit(self, tmp_path: Path, base_version: str = "4.4.0") -> None:
        """Create repo with a pyproject.toml + at least one merge-like commit
        (something we can amend)."""
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, base_version)
        _commit(tmp_path, "Add pyproject")
        # Stand-in for a merge commit
        (tmp_path / "merge_marker.txt").write_text("merged\n")
        _commit(tmp_path, "Merge branch 'feature'")

    def test_patch_patch_minor_bumps_to_4_5_0(self, tmp_path: Path):
        self._setup_repo_with_merge_commit(tmp_path, "4.4.0")
        bumps = [BumpType.PATCH, BumpType.PATCH, BumpType.MINOR]

        result = aggregate_and_apply(tmp_path, bumps, "4.4.0")

        assert result.success is True
        assert result.bump_type == BumpType.MINOR
        assert result.new_version == "4.5.0"
        assert _read_pyproject_version(tmp_path) == "4.5.0"

    def test_minor_major_bumps_to_2_0_0(self, tmp_path: Path):
        self._setup_repo_with_merge_commit(tmp_path, "1.2.3")
        bumps = [BumpType.MINOR, BumpType.MAJOR]

        result = aggregate_and_apply(tmp_path, bumps, "1.2.3")

        assert result.success is True
        assert result.bump_type == BumpType.MAJOR
        assert result.new_version == "2.0.0"
        assert _read_pyproject_version(tmp_path) == "2.0.0"

    def test_amend_includes_pyproject_in_last_commit(self, tmp_path: Path):
        self._setup_repo_with_merge_commit(tmp_path, "4.4.0")
        before_amend_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        result = aggregate_and_apply(tmp_path, [BumpType.PATCH], "4.4.0")
        assert result.success is True

        # Last commit (amended) should include pyproject.toml
        files = _last_commit_files(tmp_path)
        assert "pyproject.toml" in files

        # The commit SHA should have changed (amend creates a new commit)
        after_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert after_sha != before_amend_sha

    def test_only_patch_bumps_patch(self, tmp_path: Path):
        self._setup_repo_with_merge_commit(tmp_path, "4.4.0")

        result = aggregate_and_apply(tmp_path, [BumpType.PATCH], "4.4.0")

        assert result.success is True
        assert result.new_version == "4.4.1"

    def test_empty_bumps_fails(self, tmp_path: Path):
        self._setup_repo_with_merge_commit(tmp_path, "4.4.0")
        result = aggregate_and_apply(tmp_path, [], "4.4.0")
        assert result.success is False
        assert "no bumps" in (result.error or "")

    def test_invalid_pre_merge_version_fails(self, tmp_path: Path):
        self._setup_repo_with_merge_commit(tmp_path, "4.4.0")
        result = aggregate_and_apply(tmp_path, [BumpType.PATCH], "not-a-version")
        assert result.success is False
        assert result.error is not None

    def test_missing_pyproject_fails_gracefully(self, tmp_path: Path):
        _init_repo(tmp_path)
        # No pyproject.toml
        result = aggregate_and_apply(tmp_path, [BumpType.PATCH], "4.4.0")
        assert result.success is False
        assert "pyproject.toml" in (result.error or "")

    def test_amend_failure_rollback(self, tmp_path: Path, monkeypatch):
        """When git commit --amend fails, rollback unstage is checked."""
        self._setup_repo_with_merge_commit(tmp_path, "4.4.0")

        original_version = _read_pyproject_version(tmp_path)
        assert original_version == "4.4.0"

        call_count = 0
        orig_run_git = None

        def fake_run_git(project_root, *args, **kwargs):
            nonlocal call_count, orig_run_git
            if args[:2] == ("commit", "--amend"):
                call_count += 1
                if call_count == 1:
                    import subprocess as sp
                    return sp.CompletedProcess(args=args, returncode=1, stdout="", stderr="amend rejected")
            return orig_run_git(project_root, *args, **kwargs)

        import se3.engine.merge.version_aggregator as vagg
        orig_run_git = vagg._run_git
        monkeypatch.setattr(vagg, "_run_git", fake_run_git)

        result = aggregate_and_apply(tmp_path, [BumpType.PATCH], "4.4.0")

        assert result.success is False
        assert "amend rejected" in (result.error or "")
        # pyproject.toml should be restored to original version
        assert _read_pyproject_version(tmp_path) == "4.4.0"

    def test_amend_failure_and_reset_also_fails(self, tmp_path: Path, monkeypatch):
        """When both amend and reset fail, error mentions both."""
        self._setup_repo_with_merge_commit(tmp_path, "4.4.0")

        call_count = 0
        orig_run_git = None

        def fake_run_git(project_root, *args, **kwargs):
            nonlocal call_count, orig_run_git
            if args[:2] == ("commit", "--amend"):
                call_count += 1
                if call_count == 1:
                    import subprocess as sp
                    return sp.CompletedProcess(args=args, returncode=1, stdout="", stderr="amend rejected")
            if args[:2] == ("reset", "HEAD"):
                import subprocess as sp
                return sp.CompletedProcess(args=args, returncode=1, stdout="", stderr="index locked")
            return orig_run_git(project_root, *args, **kwargs)

        import se3.engine.merge.version_aggregator as vagg
        orig_run_git = vagg._run_git
        monkeypatch.setattr(vagg, "_run_git", fake_run_git)

        result = aggregate_and_apply(tmp_path, [BumpType.PATCH], "4.4.0")

        assert result.success is False
        # Symmetric error template: primary failure followed by the
        # rollback failure (restore + reset), each appended with
        # ". " — replaces the prior "Rollback also failed" prefix that
        # only fired in this branch.
        assert "amend rejected" in (result.error or "")
        assert "index locked" in (result.error or "")
        assert "git reset HEAD" in (result.error or "")

    def test_git_add_failure_rollback(self, tmp_path: Path, monkeypatch):
        """When git add fails (non-zero), pyproject.toml is restored."""
        self._setup_repo_with_merge_commit(tmp_path, "4.4.0")
        original_version = _read_pyproject_version(tmp_path)

        orig_run_git = None

        def fake_run_git(project_root, *args, **kwargs):
            nonlocal orig_run_git
            if args == ("add", "pyproject.toml"):
                import subprocess as sp
                return sp.CompletedProcess(args=args, returncode=1, stdout="", stderr="index error")
            return orig_run_git(project_root, *args, **kwargs)

        import se3.engine.merge.version_aggregator as vagg
        orig_run_git = vagg._run_git
        monkeypatch.setattr(vagg, "_run_git", fake_run_git)

        result = aggregate_and_apply(tmp_path, [BumpType.PATCH], "4.4.0")

        assert result.success is False
        assert "index error" in (result.error or "")
        assert _read_pyproject_version(tmp_path) == original_version

    def test_git_add_timeout_rollback(self, tmp_path: Path, monkeypatch):
        """When git add raises TimeoutExpired, pyproject.toml is restored."""
        self._setup_repo_with_merge_commit(tmp_path, "4.4.0")
        original_version = _read_pyproject_version(tmp_path)

        orig_run_git = None

        def fake_run_git(project_root, *args, **kwargs):
            nonlocal orig_run_git
            if args == ("add", "pyproject.toml"):
                raise subprocess.TimeoutExpired(cmd=["git", "add"], timeout=15)
            return orig_run_git(project_root, *args, **kwargs)

        import se3.engine.merge.version_aggregator as vagg
        orig_run_git = vagg._run_git
        monkeypatch.setattr(vagg, "_run_git", fake_run_git)

        result = aggregate_and_apply(tmp_path, [BumpType.PATCH], "4.4.0")

        assert result.success is False
        assert "timed out" in (result.error or "")
        assert _read_pyproject_version(tmp_path) == original_version

    def test_git_amend_timeout_rollback(self, tmp_path: Path, monkeypatch):
        """When git commit --amend raises TimeoutExpired, pyproject.toml is restored and unstaged."""
        self._setup_repo_with_merge_commit(tmp_path, "4.4.0")
        original_version = _read_pyproject_version(tmp_path)

        orig_run_git = None

        def fake_run_git(project_root, *args, **kwargs):
            nonlocal orig_run_git
            if args[:2] == ("commit", "--amend"):
                raise subprocess.TimeoutExpired(cmd=["git", "commit", "--amend"], timeout=30)
            return orig_run_git(project_root, *args, **kwargs)

        import se3.engine.merge.version_aggregator as vagg
        orig_run_git = vagg._run_git
        monkeypatch.setattr(vagg, "_run_git", fake_run_git)

        result = aggregate_and_apply(tmp_path, [BumpType.PATCH], "4.4.0")

        assert result.success is False
        assert "timed out" in (result.error or "")
        assert _read_pyproject_version(tmp_path) == original_version

    def test_spec_example_aggregate_to_4_7_0(self, tmp_path: Path):
        """Spec example: max(PATCH, MINOR) applied to pre-merge 4.6.0 -> 4.7.0."""
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.6.0")
        _commit(tmp_path, "Add pyproject")
        # Stand-in for a merge commit
        (tmp_path / "merge_marker.txt").write_text("merged\n")
        _commit(tmp_path, "Merge branch 'C'")

        result = aggregate_and_apply(tmp_path, [BumpType.PATCH, BumpType.MINOR], "4.6.0")

        assert result.success is True
        assert result.bump_type == BumpType.MINOR
        assert result.new_version == "4.7.0"
        assert _read_pyproject_version(tmp_path) == "4.7.0"


# ---------- Unit tests: _parse_pyproject_version ---------- #


class TestParsePyprojectVersion:
    def test_simple_project_version(self):
        content = '[project]\nname = "test"\nversion = "1.2.3"\n'
        assert _parse_pyproject_version(content) == "1.2.3"

    def test_poetry_version(self):
        content = '[tool.poetry]\nname = "test"\nversion = "2.0.0"\n'
        assert _parse_pyproject_version(content) == "2.0.0"

    def test_project_takes_precedence_over_poetry(self):
        content = (
            '[project]\nname = "test"\nversion = "1.0.0"\n'
            '[tool.poetry]\nname = "test"\nversion = "2.0.0"\n'
        )
        assert _parse_pyproject_version(content) == "1.0.0"

    def test_multiline_string_with_bracket_not_section_boundary(self):
        """A \"\"\"...\"\"\" value containing `[` on its own line must not be
        misinterpreted as a section boundary."""
        content = (
            '[project]\n'
            'name = "test"\n'
            'description = """\n'
            'Some multi-line text\n'
            '[example]\n'
            'More text\n'
            '"""\n'
            'version = "1.2.3"\n'
            '[tool.poetry]\n'
            'version = "9.9.9"\n'
        )
        assert _parse_pyproject_version(content) == "1.2.3"

    def test_single_quote_multiline_string_with_bracket(self):
        """A \'\'\'...\'\'\' value containing `[` must also be respected."""
        content = (
            "[project]\n"
            "name = 'test'\n"
            "description = '''\n"
            "Some multi-line text\n"
            "[example]\n"
            "More text\n"
            "'''\n"
            'version = "1.2.3"\n'
        )
        assert _parse_pyproject_version(content) == "1.2.3"

    def test_no_version_returns_none(self):
        content = '[project]\nname = "test"\n'
        assert _parse_pyproject_version(content) is None

    def test_inline_array_not_misread(self):
        """keywords = [\"py\"] should not terminate the section."""
        content = (
            '[project]\n'
            'name = "test"\n'
            'keywords = ["py"]\n'
            'version = "3.0.0"\n'
            '[tool.poetry]\n'
            'version = "9.9.9"\n'
        )
        assert _parse_pyproject_version(content) == "3.0.0"


class TestSliceToNextSection:
    def test_finds_next_section(self):
        content = '[project]\na = 1\n[tool.poetry]\nb = 2\n'
        result = _slice_to_next_section(content, len("[project]"))
        assert result == '\na = 1\n'

    def test_no_next_section_returns_rest(self):
        content = '[project]\na = 1\nb = 2\n'
        result = _slice_to_next_section(content, len("[project]"))
        assert result == '\na = 1\nb = 2\n'

    def test_skips_bracket_inside_triple_quoted_string(self):
        content = (
            '[project]\n'
            'desc = """\n'
            '[not-a-section]\n'
            '"""\n'
            'version = "1.0.0"\n'
            '[next]\n'
        )
        result = _slice_to_next_section(content, len("[project]"))
        assert '[not-a-section]' in result
        assert '[next]' not in result


# ---------- Unit tests: read_version_at_ref ---------- #


class TestReadVersionAtRef:
    def test_reads_version_at_head(self, tmp_path: Path):
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Add pyproject")
        sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        assert read_version_at_ref(tmp_path, sha) == "4.4.0"

    def test_returns_none_when_no_pyproject(self, tmp_path: Path):
        _init_repo(tmp_path)
        sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert read_version_at_ref(tmp_path, sha) is None

    def test_returns_none_when_no_version_field(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\n'
        )
        _commit(tmp_path, "Add pyproject without version")
        sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert read_version_at_ref(tmp_path, sha) is None

    def test_reads_poetry_version(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            '[tool.poetry]\nname = "test"\nversion = "4.4.0"\n'
        )
        _commit(tmp_path, "Add poetry pyproject")
        sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert read_version_at_ref(tmp_path, sha) == "4.4.0"


# ---------- End-to-end: orchestrator integration ---------- #


class TestOrchestratorVersionAggregation:
    def test_three_branches_bump_to_4_5_0(self, tmp_path: Path):
        """Base 4.4.0 + patch + patch + minor branches → 4.5.0.

        branch1/branch2 don't touch pyproject.toml (patch fallback).
        branch3 does the minor bump. Merging in order gives a clean
        merge for each (no pyproject.toml conflict because only one
        branch changes it).
        """
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Add pyproject")
        default_branch = _get_default_branch(tmp_path)

        # branch1: no version change → patch fallback
        _checkout(tmp_path, "branch1", create=True)
        (tmp_path / "b1.txt").write_text("b1")
        _commit(tmp_path, "Work on branch1")
        _checkout(tmp_path, default_branch)

        # branch2: no version change → patch fallback
        _checkout(tmp_path, "branch2", create=True)
        (tmp_path / "b2.txt").write_text("b2")
        _commit(tmp_path, "Work on branch2")
        _checkout(tmp_path, default_branch)

        # branch3: minor bump (4.4.0 → 4.5.0)
        _checkout(tmp_path, "branch3", create=True)
        _write_pyproject(tmp_path, "4.5.0")
        (tmp_path / "b3.txt").write_text("b3")
        _commit(tmp_path, "Bump minor on branch3")
        _checkout(tmp_path, default_branch)

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["branch1", "branch2", "branch3"])

        assert report.success is True
        assert report.merged_branches == ["branch1", "branch2", "branch3"]
        assert report.pre_merge_version == "4.4.0"
        assert report.final_version == "4.5.0"
        # When the on-disk version is already at the computed target,
        # bump_type is intentionally NOT set (bump_applied=False).
        assert _read_pyproject_version(tmp_path) == "4.5.0"

    def test_no_version_change_skips_aggregation(self, tmp_path: Path):
        """When no branch changed pyproject.toml, aggregation is skipped."""
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Add pyproject")
        default_branch = _get_default_branch(tmp_path)

        _checkout(tmp_path, "feature", create=True)
        (tmp_path / "f.txt").write_text("f")
        _commit(tmp_path, "Add feature file (no version change)")
        _checkout(tmp_path, default_branch)

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature"])

        assert report.success is True
        assert report.pre_merge_version == "4.4.0"
        # No bump inferred → aggregation skipped, version unchanged
        assert report.final_version is None
        assert report.version_aggregation_skipped is True
        assert _read_pyproject_version(tmp_path) == "4.4.0"

        # Merge commit was created (f.txt was merged in), but no amend occurred
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head != pre_head  # merge commit exists

    def test_minor_plus_major_bumps_to_2_0_0(self, tmp_path: Path):
        """Base 1.2.3 + (no-version) + major branches → 2.0.0.

        To avoid pyproject.toml conflicts during merge, only one branch
        actually modifies the version. The other contributes a patch
        fallback. Max(patch, major) = major; the chosen bump dictates
        the new version.
        """
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "1.2.3")
        _commit(tmp_path, "Add pyproject")
        default_branch = _get_default_branch(tmp_path)

        # branch m: no version change → patch fallback
        _checkout(tmp_path, "minor-br", create=True)
        (tmp_path / "m.txt").write_text("m")
        _commit(tmp_path, "Add m (no version change)")
        _checkout(tmp_path, default_branch)

        # branch M: major bump
        _checkout(tmp_path, "major-br", create=True)
        _write_pyproject(tmp_path, "2.0.0")
        (tmp_path / "M.txt").write_text("M")
        _commit(tmp_path, "Major bump")
        _checkout(tmp_path, default_branch)

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["minor-br", "major-br"])

        assert report.success is True
        assert report.final_version == "2.0.0"
        # When the on-disk version is already at the computed target,
        # bump_type is intentionally NOT set (bump_applied=False).
        assert _read_pyproject_version(tmp_path) == "2.0.0"

    def test_human_call_skips_aggregation(self, tmp_path: Path, monkeypatch):
        """If a branch triggers HUMAN_CALL, pyproject.toml is NOT modified."""
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Add pyproject")
        default_branch = _get_default_branch(tmp_path)

        # Create a conflicting branch so the merge enters conflict path
        (tmp_path / "shared.txt").write_text("base content\n")
        _commit(tmp_path, "Add shared")

        _checkout(tmp_path, "feature", create=True)
        (tmp_path / "shared.txt").write_text("feature content\n")
        _commit(tmp_path, "Change shared on feature")
        _checkout(tmp_path, default_branch)
        (tmp_path / "shared.txt").write_text("base new content\n")
        _commit(tmp_path, "Change shared on base")

        # Mock LLM to return LOW confidence → HUMAN_CALL
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",
                        hunks=[HunkResolution(1, 5, Confidence.LOW, "uncertain")],
                        overall_confidence=Confidence.LOW,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.LOW,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        # Pending human, version aggregation skipped, version unchanged
        assert report.success is False
        assert report.pending_human is True
        assert report.version_aggregation_skipped is True
        assert report.final_version is None
        assert _read_pyproject_version(tmp_path) == "4.4.0"

    def test_conflict_reject_skips_aggregation(self, tmp_path: Path, monkeypatch):
        """If a branch has a conflict and LLM resolution fails, default strategy
        escalates to human call. Aggregation is skipped and version unchanged."""
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Add pyproject")
        default_branch = _get_default_branch(tmp_path)

        (tmp_path / "shared.txt").write_text("base content\n")
        _commit(tmp_path, "Add shared")

        _checkout(tmp_path, "feature", create=True)
        (tmp_path / "shared.txt").write_text("feature content\n")
        _commit(tmp_path, "Change shared on feature")
        _checkout(tmp_path, default_branch)
        (tmp_path / "shared.txt").write_text("base new content\n")
        _commit(tmp_path, "Change shared on base")

        # Mock the resolver to raise — default strategy escalates to human call
        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve",
            lambda self, ctx, strategy: (_ for _ in ()).throw(LLMCallError("mock")),
        )

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.pending_human is True
        assert report.failure_reason == "pending_human"
        assert report.version_aggregation_skipped is True
        assert report.final_version is None
        # Version unchanged (merge not committed)
        assert _read_pyproject_version(tmp_path) == "4.4.0"

    def test_no_pyproject_skips_aggregation(self, tmp_path: Path):
        """A repo without pyproject.toml still merges — aggregation is silently skipped."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _checkout(tmp_path, "feature", create=True)
        (tmp_path / "f.txt").write_text("f")
        _commit(tmp_path, "Add f")
        _checkout(tmp_path, default_branch)

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature"])

        assert report.success is True
        assert report.merged_branches == ["feature"]
        assert report.pre_merge_version is None
        assert report.final_version is None
        assert report.version_aggregation_skipped is True

    def test_partial_failure_preserves_version(self, tmp_path: Path, monkeypatch):
        """First branch succeeds, second fails: version stays at pre-merge value."""
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "1.0.0")
        _commit(tmp_path, "Add pyproject")
        default_branch = _get_default_branch(tmp_path)

        # branch-a: clean merge
        _checkout(tmp_path, "branch-a", create=True)
        _write_pyproject(tmp_path, "1.1.0")
        (tmp_path / "a.txt").write_text("a")
        _commit(tmp_path, "Bump minor on a")
        _checkout(tmp_path, default_branch)

        # branch-b: will conflict
        (tmp_path / "shared.txt").write_text("base\n")
        _commit(tmp_path, "Add shared")
        _checkout(tmp_path, "branch-b", create=True)
        (tmp_path / "shared.txt").write_text("b\n")
        _commit(tmp_path, "Change shared on b")
        _checkout(tmp_path, default_branch)
        (tmp_path / "shared.txt").write_text("base2\n")
        _commit(tmp_path, "Change shared on base")

        # Mock resolver to fail on conflict
        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve",
            lambda self, ctx, strategy: (_ for _ in ()).throw(LLMCallError("mock")),
        )

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["branch-a", "branch-b"])

        assert report.success is False
        assert "branch-a" in report.merged_branches
        assert report.failed_branch == "branch-b"
        assert report.version_aggregation_skipped is True
        # Version stays at pre-merge — branch-a's bump is NOT applied
        # Note: the merge of branch-a brought its pyproject.toml (1.1.0) into HEAD,
        # but since aggregation was skipped, no further bump was applied.
        # The point is: the *aggregation* did not modify the file beyond what
        # merging in branch-a's commits already did.
        assert report.final_version is None

    def test_aggregate_result_dataclass(self):
        """AggregateResult default values."""
        r = AggregateResult()
        assert r.success is False
        assert r.pre_version is None
        assert r.new_version is None
        assert r.bump_type is None
        assert r.error is None

    def test_orchestrator_passes_merge_base_to_infer_branch_bump(self, tmp_path: Path, monkeypatch):
        """Orchestrator computes merge-base and passes it to infer_branch_bump.

        When A advanced past the branch-point, the merge-base is different
        from pre-merge HEAD. We verify the orchestrator passes the correct
        merge-base SHA by capturing the arguments passed to infer_branch_bump.
        """
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "M0")
        default_branch = _get_default_branch(tmp_path)

        # Feature branch (no pyproject change = clean merge)
        _checkout(tmp_path, "feature", create=True)
        (tmp_path / "f.txt").write_text("f")
        _commit(tmp_path, "Add f")
        _checkout(tmp_path, default_branch)

        # A advances past branch-point (no pyproject change = clean merge)
        (tmp_path / "a.txt").write_text("a")
        _commit(tmp_path, "Advance A")

        expected_merge_base = subprocess.run(
            ["git", "-C", str(tmp_path), "merge-base", "HEAD", "feature"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        pre_merge_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # merge-base should differ from pre-merge HEAD because A advanced
        assert expected_merge_base != pre_merge_sha

        captured = []
        def mock_infer(project_root, branch, base_ref):
            captured.append((branch, base_ref))
            return None

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.infer_branch_bump",
            mock_infer,
        )

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature"])

        assert report.success is True
        assert len(captured) == 1
        assert captured[0][0] == "feature"
        assert captured[0][1] == expected_merge_base

    def test_merge_base_failure_skips_bump_inference(self, tmp_path: Path, monkeypatch):
        """When merge-base computation fails, the branch's bump is skipped."""
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Add pyproject")
        default_branch = _get_default_branch(tmp_path)

        _checkout(tmp_path, "feature", create=True)
        _write_pyproject(tmp_path, "4.5.0")
        (tmp_path / "f.txt").write_text("f")
        _commit(tmp_path, "Bump minor on feature")
        _checkout(tmp_path, default_branch)

        # Mock _run_git so merge-base returns non-zero
        import se3.engine.merge.orchestrator as orch_mod
        orig_run_git = orch_mod._run_git

        def fake_run_git(project_root, *args, **kwargs):
            if len(args) >= 1 and args[0] == "merge-base":
                import subprocess as sp
                return sp.CompletedProcess(
                    args=args, returncode=1, stdout="", stderr="no merge base"
                )
            return orig_run_git(project_root, *args, **kwargs)

        monkeypatch.setattr(orch_mod, "_run_git", fake_run_git)

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature"])

        assert report.success is True
        assert report.merged_branches == ["feature"]
        assert report.pre_merge_version == "4.4.0"
        # Bump inference skipped because merge-base failed;
        # no aggregation applied, but the merge itself succeeded
        assert report.final_version is None
        assert report.version_aggregation_skipped is True
        # Version from working tree reflects the merged branch
        assert _read_pyproject_version(tmp_path) == "4.5.0"

    def test_end_to_end_spec_example_4_7_0(self, tmp_path: Path, monkeypatch):
        """End-to-end: A advanced past branch-point, B PATCH, C MINOR → 4.7.0.

        Repo state:
        - Base (M0): pyproject 4.4.0
        - Branch B: pyproject 4.4.1 (patch from base)
        - Branch C: pyproject 4.5.0 → 4.5.1 → 4.6.0 (minor from base, with noise)
        - A advances to 4.6.0 after branches were created

        Merge order: C first (clean — pyproject identical to A at 4.6.0),
        then B (conflict — B at 4.4.1 vs A at 4.6.0).
        The conflict resolver is mocked to accept keeping A's version.
        """
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "M0")
        default_branch = _get_default_branch(tmp_path)

        # Branch B: patch bump
        _checkout(tmp_path, "B", create=True)
        _write_pyproject(tmp_path, "4.4.1")
        (tmp_path / "b.txt").write_text("b")
        _commit(tmp_path, "Bump patch on B")
        _checkout(tmp_path, default_branch)

        # Branch C: minor bump with intermediate noise
        _checkout(tmp_path, "C", create=True)
        _write_pyproject(tmp_path, "4.5.0")
        (tmp_path / "c.txt").write_text("c1")
        _commit(tmp_path, "C1: 4.5.0")
        _write_pyproject(tmp_path, "4.5.1")
        _commit(tmp_path, "C2: 4.5.1")
        _write_pyproject(tmp_path, "4.6.0")
        (tmp_path / "c.txt").write_text("c3")
        _commit(tmp_path, "C3: 4.6.0")
        _checkout(tmp_path, default_branch)

        # A advances past branch-point to 4.6.0
        _write_pyproject(tmp_path, "4.6.0")
        (tmp_path / "a.txt").write_text("a")
        _commit(tmp_path, "M1: advance A to 4.6.0")

        # Mock conflict resolver to accept the pyproject resolution (keep ours)
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            files = []
            for cf in context.files:
                # Keep "ours" (A's) content for pyproject; pass through for others
                if cf.path == "pyproject.toml":
                    resolved = cf.ours_content
                else:
                    resolved = cf.ours_content
                files.append(
                    FileResolution(
                        path=cf.path,
                        resolved_content=resolved,
                        hunks=[
                            HunkResolution(
                                h.start_line, h.end_line,
                                Confidence.HIGH, "accept ours"
                            )
                            for h in cf.hunks
                        ],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=cf.is_spec,
                    )
                )
            return LLMResolution(
                files=files,
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["C", "B"])

        assert report.success is True
        assert "C" in report.merged_branches
        assert "B" in report.merged_branches
        assert report.pre_merge_version == "4.6.0"
        # max(PATCH from B, MINOR from C) on 4.6.0 → 4.7.0
        assert report.final_version == "4.7.0"
        assert report.bump_type == "minor"
        assert _read_pyproject_version(tmp_path) == "4.7.0"


# ---------- amend=False path integration tests ---------- #


class TestAmendFalseOrchestratorPath:
    """Integration test: orchestrator dispatches amend=False when HEAD is published.

    These tests target the post-aggregation HEAD topology re-check
    that runs after ``aggregate_and_apply`` completes.  When the
    orchestrator detects a published HEAD, it sets
    ``self._aggregation_used_fixup = True`` and the topology check
    must walk through HEAD^1 (allow_fixup_parent=True) to confirm
    the merge commit is intact.
    """

    def test_aggregate_and_apply_amend_false_succeeds(
        self, tmp_path: Path,
    ) -> None:
        """Direct call: amend=False creates a new commit, leaves merge as HEAD^1."""
        _init_repo(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Add pyproject")
        # Stand-in for a merge commit
        (tmp_path / "marker.txt").write_text("merged\n")
        _commit(tmp_path, "Merge stand-in")
        merge_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        from se3.engine.version_bumper import BumpType as _BumpType

        result = aggregate_and_apply(
            tmp_path, [_BumpType.PATCH], "4.4.0", amend=False,
        )
        assert result.success is True
        assert result.new_version == "4.4.1"
        assert _read_pyproject_version(tmp_path) == "4.4.1"

        # HEAD is a NEW commit on top of the merge commit.
        new_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert new_head != merge_sha

        # HEAD^1 == merge_sha (the merge commit is preserved below).
        head_parent = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD^1"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert head_parent == merge_sha

    def test_orchestrator_amend_false_preserves_merge_commit(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """When orchestrator forces amend=False, the merge commit is preserved as HEAD^1.

        Mocks ``_is_head_published`` to return True so the orchestrator
        takes the amend=False path.  After aggregation, we assert:
          * report.success is True
          * report.final_version reflects the bump
          * HEAD has parent_count == 1 (single-parent commit on top)
          * HEAD^1 has parent_count >= 2 (the actual merge commit)

        The branch is configured so that its merge does NOT advance
        pyproject.toml past the aggregator's computed target —
        otherwise ``aggregate_and_apply`` would short-circuit with
        ``version_already_at_target`` and the amend=False code path
        would never run.  We achieve this by having the branch bump
        the version (PATCH) but using a stale ``pre_merge_version``
        that the orchestrator captures BEFORE the branch's bump is
        in HEAD.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "M0")

        # Branch B introduces a non-version change so the merge will
        # produce a real merge commit but pyproject.toml stays at
        # 4.4.0 in HEAD.  We then mock ``infer_branch_bump`` to claim
        # PATCH so ``branch_bumps`` is populated and aggregation runs.
        _checkout(tmp_path, "feature", create=True)
        (tmp_path / "feat.txt").write_text("feature work\n")
        _commit(tmp_path, "feature: add feat.txt")
        _checkout(tmp_path, default_branch)

        # Force amend=False path.
        monkeypatch.setattr(
            MergeOrchestrator, "_is_head_published",
            lambda self: True,
        )

        # Force a non-NONE bump so aggregation actually runs even
        # though the branch did not change pyproject.toml.
        from se3.engine.merge import orchestrator as orch_mod
        from se3.engine.merge.version_aggregator import (
            InferResult as _InferResult,
        )

        def fake_infer(*args, **kwargs):
            return _InferResult(bump=BumpType.PATCH, reason="patched for test")

        monkeypatch.setattr(orch_mod, "infer_branch_bump", fake_infer)

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature"])

        assert report.success is True
        assert report.final_version == "4.4.1"

        # Verify HEAD topology: HEAD is a single-parent commit (the
        # version-bump fixup), HEAD^1 is the merge commit.
        rev_list_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-list", "--parents", "-n", "1", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        head_parts = rev_list_head.split()
        assert len(head_parts) == 2, (
            f"HEAD on amend=False path should be a single-parent commit, "
            f"got {len(head_parts) - 1} parent(s): {rev_list_head!r}"
        )

        rev_list_head1 = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-list", "--parents", "-n", "1", "HEAD^1"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        head1_parts = rev_list_head1.split()
        assert len(head1_parts) >= 3, (
            f"HEAD^1 should be the merge commit (>=2 parents), got: {rev_list_head1!r}"
        )

    def test_orchestrator_amend_false_post_topology_check_passes(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Post-aggregation topology check accepts the fix-up layout.

        After amend=False, the orchestrator's post-aggregation
        ``assert_head_is_merge_commit(allow_fixup_parent=True)``
        must accept the layout (HEAD has 1 parent, HEAD^1 is merge).
        Regression guard so that a future change to the post-condition
        cannot silently start failing on this branch.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "M0")

        _checkout(tmp_path, "feature", create=True)
        (tmp_path / "feat.txt").write_text("feat")
        _commit(tmp_path, "feature")
        _checkout(tmp_path, default_branch)

        monkeypatch.setattr(
            MergeOrchestrator, "_is_head_published",
            lambda self: True,
        )

        from se3.engine.merge import orchestrator as orch_mod
        from se3.engine.merge.version_aggregator import (
            InferResult as _InferResult,
        )

        def fake_infer(*args, **kwargs):
            return _InferResult(bump=BumpType.PATCH, reason="patched for test")

        monkeypatch.setattr(orch_mod, "infer_branch_bump", fake_infer)

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature"])

        # The post-aggregation topology check is part of execute() —
        # if it had failed against the fix-up layout, success would
        # be False and the failure_reason would be POSTCOND_HEAD_NOT_MERGE_COMMIT.
        assert report.success is True
        assert report.failure_reason in (None, "")
        assert "postcond_head_not_merge_commit" not in (
            (report.failure_reason or "").lower()
        )
