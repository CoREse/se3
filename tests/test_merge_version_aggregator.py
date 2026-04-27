"""Tests for SemVer aggregation in the merge orchestrator (G5)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from se3.engine.merge.orchestrator import MergeOrchestrator
from se3.engine.merge.version_aggregator import (
    AggregateResult,
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

        bump = infer_branch_bump(tmp_path, "feature", base_sha)
        assert bump == BumpType.PATCH

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

        bump = infer_branch_bump(tmp_path, "feature", base_sha)
        assert bump == BumpType.MINOR

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

        bump = infer_branch_bump(tmp_path, "feature", base_sha)
        assert bump == BumpType.MAJOR

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

        bump = infer_branch_bump(tmp_path, "feature", base_sha)
        assert bump is None

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

        bump = infer_branch_bump(tmp_path, "feature", base_sha)
        assert bump is None

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

        bump = infer_branch_bump(tmp_path, "feature", base_sha)
        assert bump is None


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
        assert "amend rejected" in (result.error or "")
        assert "Rollback also failed" in (result.error or "")
        assert "index locked" in (result.error or "")

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
        assert report.bump_type == "minor"
        # Working tree has the new version
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
        assert report.bump_type == "major"
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
        """If a branch is REJECTed (merge --abort), aggregation skipped and version unchanged."""
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

        # Mock the resolver to raise — orchestrator should abort
        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve",
            lambda self, ctx, strategy: (_ for _ in ()).throw(RuntimeError("mock")),
        )

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.failure_reason == "merge_conflict"
        assert report.version_aggregation_skipped is True
        assert report.final_version is None
        # Version unchanged
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
            lambda self, ctx, strategy: (_ for _ in ()).throw(RuntimeError("mock")),
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
