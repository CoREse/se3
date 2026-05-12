"""Tests for se3 merge command entry point (merge_cmd.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from se3.commands.merge_cmd import (
    _branch_exists,
    _failure_title_and_summary,
    _is_working_tree_clean,
    run_merge,
)


def _get_default_branch(path: Path) -> str:
    """Get the current branch name."""
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


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


class TestBranchExists:
    def test_branch_exists_true(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True, capture_output=True,
        )
        assert _branch_exists(tmp_path, "feature") is True

    def test_branch_exists_false(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        assert _branch_exists(tmp_path, "nonexistent") is False


class TestIsWorkingTreeClean:
    def test_clean_tree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        assert _is_working_tree_clean(tmp_path) is True

    def test_dirty_tree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        # Modify a tracked file to make the tree dirty
        (tmp_path / "README.md").write_text("dirty")
        assert _is_working_tree_clean(tmp_path) is False

    def test_worktree_in_progress_merge_detected(self, tmp_path: Path) -> None:
        """In a linked worktree, MERGE_HEAD lives under the per-worktree
        gitdir (resolved via `git rev-parse --git-dir`), NOT under
        <worktree>/.git which is a regular file pointer. Verify that an
        in-progress marker inside a worktree is still detected so the
        merge safety net works in the loop-worktree environment SE3
        promotes — even when `git status --porcelain` reports clean
        (the porcelain check would otherwise mask the broken marker
        probe).
        """
        from se3.commands.merge_cmd import _resolve_git_dir

        _init_repo(tmp_path)
        # Create a linked worktree on a fresh branch.
        worktree_path = tmp_path.parent / (tmp_path.name + "_wt")
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", "-b", "wt-branch",
             str(worktree_path)],
            check=True, capture_output=True,
        )
        try:
            # `.git` inside a linked worktree is a file pointer, not a dir.
            assert (worktree_path / ".git").is_file()

            # The worktree's actual gitdir lives under the main repo's
            # .git/worktrees/<name>/. Resolve it the way the production
            # code does.
            gitdir = _resolve_git_dir(worktree_path)
            assert gitdir is not None
            assert gitdir.is_dir()
            assert (worktree_path / ".git").resolve() != gitdir
            # Confirm we're under the main repo's .git/worktrees/ tree.
            assert ".git/worktrees" in str(gitdir).replace("\\", "/")

            # Worktree starts clean.
            assert _is_working_tree_clean(worktree_path) is True

            # Simulate an in-progress merge by writing MERGE_HEAD to the
            # per-worktree gitdir while leaving the working tree clean.
            # This is precisely the state the buggy code missed: the
            # marker exists but porcelain would say clean.
            merge_head = gitdir / "MERGE_HEAD"
            merge_head.write_text(
                "0000000000000000000000000000000000000000\n"
            )
            try:
                # `git status --porcelain --untracked-files=no` is still
                # clean here (no tracked-file edits), so only the marker
                # probe can catch this state.
                porcelain = subprocess.run(
                    ["git", "-C", str(worktree_path),
                     "status", "--porcelain", "--untracked-files=no"],
                    capture_output=True, text=True, check=True,
                )
                assert porcelain.stdout.strip() == ""

                # The fix must catch the marker even though porcelain
                # is clean — and even though <worktree>/.git/MERGE_HEAD
                # does NOT exist (it lives under the resolved gitdir).
                assert not (worktree_path / ".git" / "MERGE_HEAD").exists()
                assert _is_working_tree_clean(worktree_path) is False
            finally:
                merge_head.unlink(missing_ok=True)
        finally:
            subprocess.run(
                ["git", "-C", str(tmp_path), "worktree", "remove", "--force",
                 str(worktree_path)],
                capture_output=True,
            )


class TestRunMergeValidation:
    def test_dirty_working_tree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "dirty.txt").write_text("dirty")
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1

    def test_merge_current_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        current = _get_default_branch(tmp_path)
        exit_code = run_merge([current], project_root=tmp_path)
        assert exit_code == 1

    def test_merge_main_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        # Rename current branch to main
        subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "-M", "main"],
            check=True, capture_output=True,
        )
        exit_code = run_merge(["main"], project_root=tmp_path)
        assert exit_code == 1

    def test_merge_nonexistent_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        exit_code = run_merge(["nonexistent"], project_root=tmp_path)
        assert exit_code == 1


class TestMergeConfig:
    def test_merge_config_from_se3_yaml(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        se3_yaml = tmp_path / "se3.yaml"
        se3_yaml.write_text(
            "merge:\n"
            "  strategy: fast\n"
            "  delete_merged_default: true\n"
        )
        from se3.config import MergeConfig

        config = MergeConfig.load(tmp_path)
        assert config.strategy == "fast"
        assert config.delete_merged_default is True

    def test_merge_config_invalid_strategy_fallback(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        se3_yaml = tmp_path / "se3.yaml"
        se3_yaml.write_text(
            "merge:\n"
            "  strategy: invalid_strategy\n"
        )
        from se3.config import MergeConfig

        config = MergeConfig.load(tmp_path)
        assert config.strategy == "robust"

    def test_merge_config_defaults_when_missing(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        from se3.config import MergeConfig

        config = MergeConfig.load(tmp_path)
        assert config.strategy == "robust"
        # Default flipped in 4.12.x: delete-merged is on by default.
        assert config.delete_merged_default is True
        assert config.strict_runtime_sync is False

    def test_merge_config_strict_runtime_sync_true(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        se3_yaml = tmp_path / "se3.yaml"
        se3_yaml.write_text(
            "merge:\n"
            "  strict_runtime_sync: true\n"
        )
        from se3.config import MergeConfig

        config = MergeConfig.load(tmp_path)
        assert config.strict_runtime_sync is True

    def test_merge_config_strict_runtime_sync_string_true(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        se3_yaml = tmp_path / "se3.yaml"
        se3_yaml.write_text(
            "merge:\n"
            "  strict_runtime_sync: 'true'\n"
        )
        from se3.config import MergeConfig

        config = MergeConfig.load(tmp_path)
        assert config.strict_runtime_sync is True


class TestMergeConfigFromSubdirectory:
    """Verify config is found when cwd is a subdirectory of the project."""

    def test_load_merge_config_from_subdirectory(self, tmp_path: Path) -> None:
        """load_merge_config(project_root) finds se3.yaml even when cwd
        is a subdirectory — mirrors the fix in cli.py:merge_cmd.
        """
        _init_repo(tmp_path)
        subdir = tmp_path / "src"
        subdir.mkdir()
        se3_yaml = tmp_path / "se3.yaml"
        se3_yaml.write_text(
            "merge:\n"
            "  strategy: fast\n"
            "  delete_merged_default: true\n"
        )

        from se3.config import load_merge_config

        # With explicit project_root → finds the config
        config = load_merge_config(tmp_path)
        assert config.strategy == "fast"
        assert config.delete_merged_default is True

        # Without project_root (defaults to cwd) → misses config when
        # cwd is a subdirectory. This is the pre-fix behaviour we
        # document, not a bug we fix here.


class TestMergeCliFromSubdirectory:
    """End-to-end CLI test: Typer merge entry from subdirectory cwd."""

    def test_cli_merge_from_subdirectory_reads_se3_yaml(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When invoked from a subdirectory, ``merge_cmd`` resolves the
        project root (via ``get_project_root``) and loads ``se3.yaml``
        merge configuration so that strategy/delete-merged values flow
        through to ``run_merge``.
        """
        import os

        _init_repo(tmp_path)
        se3_yaml = tmp_path / "se3.yaml"
        se3_yaml.write_text(
            "merge:\n"
            "  strategy: fast\n"
            "  delete_merged_default: true\n"
            "  strict_runtime_sync: true\n"
        )
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )
        subdir = tmp_path / "src"
        subdir.mkdir()

        captured: dict = {}

        def mock_run_merge(
            branches, strategy="default", delete_merged=False,
            strict_runtime_sync=False, project_root=None,
        ):
            captured["strategy"] = strategy
            captured["delete_merged"] = delete_merged
            captured["strict_runtime_sync"] = strict_runtime_sync
            captured["project_root"] = str(project_root) if project_root else None
            return 0

        monkeypatch.setattr("se3.commands.merge_cmd.run_merge", mock_run_merge)

        old_cwd = os.getcwd()
        os.chdir(str(subdir))
        try:
            from typer.testing import CliRunner
            from se3.cli import app

            runner = CliRunner()
            result = runner.invoke(app, ["merge", "feature"])
            assert result.exit_code == 0, result.output
            assert captured.get("strategy") == "fast"
            assert captured.get("delete_merged") is True
            assert captured.get("strict_runtime_sync") is True
            # project_root is not passed explicitly — run_merge calls
            # get_project_root() internally when None. The key assertion
            # is that strategy/delete_merged/strict_runtime_sync from
            # se3.yaml were read.
        finally:
            os.chdir(old_cwd)


class TestRunMergeDetachedHead:
    def test_merge_in_detached_head_shows_clean_error(self, tmp_path: Path) -> None:
        """Detached HEAD state → clean error message, not unhandled traceback."""
        _init_repo(tmp_path)
        # Create a commit we can checkout to enter detached HEAD
        _add_commit(tmp_path, "file.txt", "content", "commit")
        # Get the commit SHA
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        sha = result.stdout.strip()
        # Checkout the SHA directly → detached HEAD
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", sha],
            check=True, capture_output=True,
        )

        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1


class TestRunMergeSuccess:
    def test_merge_single_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feature.txt", "feature content", "Add feature")
        # Go back to default branch
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 0

        # Verify merged content is present
        assert (tmp_path / "feature.txt").exists()
        assert (tmp_path / "feature.txt").read_text() == "feature content"

    def test_merge_multiple_branches(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        # Create feature-a
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a content", "Add A")
        # Go back to default branch and create feature-b
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b content", "Add B")
        # Go back to default branch
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        exit_code = run_merge(["feature-a", "feature-b"], project_root=tmp_path)
        assert exit_code == 0

        # Verify both branches' content is present
        assert (tmp_path / "a.txt").exists()
        assert (tmp_path / "b.txt").exists()

    def test_merge_with_conflict_aborts(self, tmp_path: Path, monkeypatch) -> None:
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        # Create a file on default branch
        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        # Create feature branch that changes the same file
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        # Go back to default branch and change the same file differently
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("base new content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on base"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver to fail — default strategy escalates to human call
        from se3.engine.llm_caller import LLMCallError
        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve",
            lambda self, ctx, strategy: (_ for _ in ()).throw(LLMCallError("mock llm fail")),
        )

        # Pin to ``default`` strategy: the robust default would take-theirs
        # and commit successfully instead of escalating to human call.
        exit_code = run_merge(
            ["feature"], strategy="default", project_root=tmp_path,
        )
        # pending_human returns 130 (interrupted by user)
        assert exit_code == 130

        # Working tree should have conflict markers (default strategy leaves them for human)
        content = (tmp_path / "shared.txt").read_text()
        assert "<<<<<<<" in content
        assert "=======" in content
        assert ">>>>>>>" in content


class TestMergeDeleteMergedTristate:
    """Verify --delete-merged/--no-delete-merged tri-state merging with config."""

    def _run_cli_tristate(
        self, tmp_path: Path, monkeypatch, extra_args: list[str]
    ) -> dict:
        """Run se3 merge CLI and return the captured delete_merged value."""
        import os

        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )

        captured: dict = {}

        def mock_run_merge(
            branches, strategy="default", delete_merged=False,
            strict_runtime_sync=False, project_root=None,
        ):
            captured["delete_merged"] = delete_merged
            return 0

        monkeypatch.setattr("se3.commands.merge_cmd.run_merge", mock_run_merge)

        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            from typer.testing import CliRunner
            from se3.cli import app

            runner = CliRunner()
            result = runner.invoke(app, ["merge", "feature"] + extra_args)
            captured["exit_code"] = result.exit_code
            captured["output"] = result.output
        finally:
            os.chdir(old_cwd)
        return captured

    def test_omit_flag_uses_config_true(self, tmp_path: Path, monkeypatch) -> None:
        se3_yaml = tmp_path / "se3.yaml"
        se3_yaml.write_text("merge:\n  delete_merged_default: true\n")
        captured = self._run_cli_tristate(tmp_path, monkeypatch, [])
        assert captured["exit_code"] == 0, captured.get("output")
        assert captured["delete_merged"] is True

    def test_no_delete_overrides_config_true(self, tmp_path: Path, monkeypatch) -> None:
        se3_yaml = tmp_path / "se3.yaml"
        se3_yaml.write_text("merge:\n  delete_merged_default: true\n")
        captured = self._run_cli_tristate(tmp_path, monkeypatch, ["--no-delete-merged"])
        assert captured["exit_code"] == 0
        assert captured["delete_merged"] is False

    def test_delete_overrides_config_false(self, tmp_path: Path, monkeypatch) -> None:
        se3_yaml = tmp_path / "se3.yaml"
        se3_yaml.write_text("merge:\n  delete_merged_default: false\n")
        captured = self._run_cli_tristate(tmp_path, monkeypatch, ["--delete-merged"])
        assert captured["exit_code"] == 0
        assert captured["delete_merged"] is True

    def test_no_config_no_flag_defaults_to_true(self, tmp_path: Path, monkeypatch) -> None:
        """Regression for the default flip: absent se3.yaml and absent CLI
        flag, ``se3 merge`` MUST delete merged branches by default.
        """
        # Intentionally write no se3.yaml — MergeConfig.load() falls back
        # to the dataclass default.
        captured = self._run_cli_tristate(tmp_path, monkeypatch, [])
        assert captured["exit_code"] == 0, captured.get("output")
        assert captured["delete_merged"] is True

    def test_no_config_no_delete_flag_overrides_default(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Regression: --no-delete-merged still wins even after the
        default flip (no se3.yaml present)."""
        captured = self._run_cli_tristate(
            tmp_path, monkeypatch, ["--no-delete-merged"],
        )
        assert captured["exit_code"] == 0
        assert captured["delete_merged"] is False


class TestMergeVersionAggregationWarning:
    """Verify version_aggregation_error is rendered in success-path output."""

    def test_success_with_version_aggregation_error_warns(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """All merges succeed but aggregate_and_apply fails → exit 0 with warning."""
        _init_repo(tmp_path)
        default = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )

        # Mock aggregate_and_apply to return failure
        def mock_aggregate(project_root, bumps, pre_version):
            from se3.engine.merge.version_aggregator import AggregateResult
            return AggregateResult(
                success=False,
                error="git commit --amend failed: mock failure",
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.aggregate_and_apply",
            mock_aggregate,
        )

        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 0
        assert (tmp_path / "feat.txt").exists()


class TestFailureTitleAndSummary:
    """Unit tests for _failure_title_and_summary mapping."""

    def test_merge_conflict(self) -> None:
        title, summary = _failure_title_and_summary("merge_conflict")
        assert title == "Merge failed"
        assert "git merge conflict" in summary

    def test_guardrail_violation(self) -> None:
        title, summary = _failure_title_and_summary("guardrail_violation")
        assert title == "Merge failed"
        assert "post-merge guardrails violation" in summary

    def test_guardrail_repair_failed(self) -> None:
        title, summary = _failure_title_and_summary("guardrail_repair_failed")
        assert title == "Merge aborted"
        assert "fast strategy could not auto-repair" in summary

    def test_guardrail_repair_stalled(self) -> None:
        title, summary = _failure_title_and_summary("guardrail_repair_stalled")
        assert title == "Merge paused for human review"
        assert "fast strategy could not auto-repair" in summary
        assert "repair stalled" in summary

    def test_guardrail_repair_exhausted(self) -> None:
        title, summary = _failure_title_and_summary("guardrail_repair_exhausted")
        assert title == "Merge paused for human review"
        assert "fast strategy could not auto-repair" in summary
        assert "repair exhausted" in summary

    def test_merge_timed_out(self) -> None:
        title, summary = _failure_title_and_summary("merge_timed_out")
        assert title == "Merge aborted"
        assert "git merge timed out" in summary

    def test_merge_failed(self) -> None:
        title, summary = _failure_title_and_summary("merge_failed")
        assert title == "Merge failed"
        assert "git merge operation failed" in summary

    def test_fast_abort(self) -> None:
        title, summary = _failure_title_and_summary("fast_abort")
        assert title == "Merge aborted"
        assert "fast strategy could not resolve conflict" in summary

    def test_fast_abort_with_detail(self) -> None:
        title, summary = _failure_title_and_summary(
            "fast_abort: fatal: refusing to merge unrelated histories"
        )
        assert title == "Merge aborted"
        assert "fast strategy could not resolve conflict" in summary
        assert "refusing to merge unrelated histories" in summary

    def test_fast_failure(self) -> None:
        title, summary = _failure_title_and_summary("fast_failure")
        assert title == "Merge aborted"
        assert "fast strategy merge failed" in summary
        assert "conflict" not in summary.lower()

    def test_fast_failure_with_detail(self) -> None:
        title, summary = _failure_title_and_summary(
            "fast_failure: fatal: refusing to merge unrelated histories"
        )
        assert title == "Merge aborted"
        assert "fast strategy merge failed" in summary
        assert "refusing to merge unrelated histories" in summary
        assert "conflict" not in summary.lower()

    def test_merge_failed_with_detail(self) -> None:
        title, summary = _failure_title_and_summary(
            "merge_failed: fatal: refusing to merge unrelated histories"
        )
        assert title == "Merge failed"
        assert "git merge operation failed" in summary
        assert "refusing to merge unrelated histories" in summary

    def test_binary_file_conflict(self) -> None:
        title, summary = _failure_title_and_summary("binary_file_conflict")
        assert title == "Merge aborted"
        assert "binary file conflict requires human review" in summary
        assert "fast strategy" not in summary  # must not hardcode strategy

    def test_guardrail_missing_post_sha(self) -> None:
        title, summary = _failure_title_and_summary("guardrail_missing_post_sha")
        assert title == "Merge aborted"
        assert "post-merge commit SHA was unavailable" in summary

    def test_guardrail_missing_pre_sha(self) -> None:
        title, summary = _failure_title_and_summary("guardrail_missing_pre_sha")
        assert title == "Merge aborted"
        assert "pre-merge commit SHA was unavailable" in summary
        assert "merge commit may still be in HEAD" in summary

    def test_guardrail_missing_pre_and_post_sha(self) -> None:
        title, summary = _failure_title_and_summary("guardrail_missing_pre_and_post_sha")
        assert title == "Merge aborted"
        assert "both pre-merge and post-merge commit SHAs were unavailable" in summary
        assert "merge commit may still be in HEAD" in summary

    def test_resolution_commit_timeout(self) -> None:
        title, summary = _failure_title_and_summary("resolution_commit_timeout")
        assert title == "Merge aborted"
        assert "git commit timed out" in summary

    def test_pending_human(self) -> None:
        title, summary = _failure_title_and_summary("pending_human")
        assert title == "Merge paused for human review"
        assert "requires your decision" in summary

    def test_guardrail_violation_no_rollback(self) -> None:
        title, summary = _failure_title_and_summary("guardrail_violation_no_rollback")
        assert title == "Merge failed"
        assert "post-merge guardrails violation" in summary
        assert "could not roll back" in summary

    def test_conflict_context_failed(self) -> None:
        title, summary = _failure_title_and_summary("conflict_context_failed")
        assert title == "Merge aborted"
        assert "failed to build conflict context" in summary
        assert "fast strategy" not in summary.lower()

    def test_conflict_context_failed_pending_human(self) -> None:
        title, summary = _failure_title_and_summary(
            "conflict_context_failed", pending_human=True
        )
        assert title == "Merge failed"
        assert "failed to build conflict context" in summary
        assert "paused for human review" in summary

    def test_guardrail_violation_call_failed(self) -> None:
        title, summary = _failure_title_and_summary("guardrail_violation_call_failed")
        assert title == "Merge failed"
        assert "guardrails violation" in summary
        assert "call file" in summary

    def test_rollback_failed(self) -> None:
        title, summary = _failure_title_and_summary("rollback_failed")
        assert title == "Merge failed"
        assert "git rollback failed after guardrail violation" in summary

    def test_conflict_context_failed_call_file_write_failed(self) -> None:
        title, summary = _failure_title_and_summary(
            "conflict_context_failed_call_file_write_failed"
        )
        assert title == "Merge failed"
        assert "failed to build conflict context" in summary
        assert "could not write human call file" in summary

    def test_unknown_reason_fallback(self) -> None:
        title, summary = _failure_title_and_summary("some_unknown_reason")
        assert title == "Merge failed"
        assert "some_unknown_reason" in summary

    def test_none_reason_fallback(self) -> None:
        title, summary = _failure_title_and_summary(None)
        assert title == "Merge failed"
        assert summary == "Merge failed."


class TestFailureReasonRendering:
    """Integration tests: verify run_merge renders distinct titles/summaries."""

    def _mock_orchestrator_report(
        self, monkeypatch, report
    ) -> list[dict]:
        """Mock MergeOrchestrator.execute to return the given report.

        Returns a list that will be populated with captured render_text calls.
        """
        captured: list[dict] = []

        def capture_render_text(content, title=None, style=None):
            captured.append({"content": content, "title": title})

        monkeypatch.setattr(
            "se3.commands.merge_cmd.render_text", capture_render_text,
        )

        class MockOrchestrator:
            def __init__(self, **kwargs):
                pass

            def execute(self, branches):
                return report

        # MergeOrchestrator is imported inside run_merge from the engine
        # module, so we patch the source class there.
        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.MergeOrchestrator", MockOrchestrator,
        )
        # Bypass branch-existence check so validation reaches the orchestrator.
        monkeypatch.setattr(
            "se3.commands.merge_cmd._branch_exists", lambda _root, _branch: True,
        )
        return captured

    def test_merge_conflict_rendering(self, tmp_path: Path, monkeypatch) -> None:
        """failure_reason='merge_conflict' → 'Merge failed' title + git conflict summary."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="merge_conflict",
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert captured[0]["title"] == "Merge failed"
        assert "git merge conflict" in captured[0]["content"]

    def test_guardrail_violation_rendering(self, tmp_path: Path, monkeypatch) -> None:
        """failure_reason='guardrail_violation' → 'Merge failed' title + guardrails summary."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="guardrail_violation",
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert captured[0]["title"] == "Merge failed"
        assert "post-merge guardrails violation" in captured[0]["content"]

    def test_guardrail_repair_failed_rendering(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """failure_reason='guardrail_repair_failed' → 'Merge aborted' title."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="guardrail_repair_failed",
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert captured[0]["title"] == "Merge aborted"
        assert "auto-repair guardrails violation" in captured[0]["content"]

    def test_fast_abort_rendering(self, tmp_path: Path, monkeypatch) -> None:
        """failure_reason='fast_abort' → 'Merge aborted' title + fast summary."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="fast_abort",
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert captured[0]["title"] == "Merge aborted"
        assert "fast strategy could not resolve conflict" in captured[0]["content"]

    def test_fast_failure_rendering(self, tmp_path: Path, monkeypatch) -> None:
        """failure_reason='fast_failure' → 'Merge aborted' title + merge-failed summary."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="fast_failure",
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert captured[0]["title"] == "Merge aborted"
        assert "fast strategy merge failed" in captured[0]["content"]
        assert "conflict" not in captured[0]["content"].lower()

    def test_guardrail_violation_call_failed_rendering(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """failure_reason='guardrail_violation_call_failed' → correct title in failure branch.

        No call file was written, so pending_human must be False and exit code is 1 (not 130).
        """
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="guardrail_violation_call_failed",
            pending_human=False,
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert captured[0]["title"] == "Merge failed"
        assert "guardrails violation" in captured[0]["content"]
        assert "call file" in captured[0]["content"]

    def test_guardrail_violation_pending_human_rendering(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """pending_human + guardrail_violation → 'Merge failed' title (not generic 'Merge Paused')."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="guardrail_violation",
            pending_human=True,
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 130
        assert len(captured) == 1
        assert captured[0]["title"] == "Merge failed"
        assert "post-merge guardrails violation" in captured[0]["content"]

    def test_guardrail_missing_post_sha_rendering(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """failure_reason='guardrail_missing_post_sha' -> 'Merge aborted' title."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="guardrail_missing_post_sha",
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert captured[0]["title"] == "Merge aborted"
        assert "post-merge commit SHA was unavailable" in captured[0]["content"]

    def test_guardrail_missing_pre_sha_rendering(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """failure_reason='guardrail_missing_pre_sha' -> 'Merge aborted' title with HEAD warning."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="guardrail_missing_pre_sha",
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert captured[0]["title"] == "Merge aborted"
        assert "pre-merge commit SHA was unavailable" in captured[0]["content"]
        assert "merge commit may still be in HEAD" in captured[0]["content"]

    def test_binary_file_conflict_rendering(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """failure_reason='binary_file_conflict' -> 'Merge aborted' title without strategy mention."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="binary_file_conflict",
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert captured[0]["title"] == "Merge aborted"
        assert "binary file conflict requires human review" in captured[0]["content"]
        assert "fast strategy" not in captured[0]["content"]

    def test_generic_failure_reason_shown(self, tmp_path: Path, monkeypatch) -> None:
        """Unknown failure_reason still appears as 'Reason: ...' in output."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="some_custom_reason",
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert captured[0]["title"] == "Merge failed"
        assert "Reason: some_custom_reason" in captured[0]["content"]

    def test_fast_abort_with_rollback_failed_shows_critical(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """fast_abort path with rollback_failed=True → CRITICAL 'repository may be corrupted' message."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="fast_abort",
            rollback_failed=True,
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert "CRITICAL" in captured[0]["content"] or "corrupted" in captured[0]["content"].lower()
        assert "INCONSISTENT" in captured[0]["content"]

    def test_rollback_failed_with_call_file_rendering(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """rollback_failed=True with human_call_file set → CRITICAL message includes call file path."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="guardrail_repair_stalled",
            rollback_failed=True,
            human_call_file="se3/calls/merge_20260101_000000_feature.json",
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert "CRITICAL" in captured[0]["content"]
        assert "Call file: se3/calls/merge_20260101_000000_feature.json" in captured[0]["content"]

    def test_fast_binary_file_conflict_rendering(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """failure_reason='binary_file_conflict_fast_abort' -> strategy-appropriate message without human review promise."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="binary_file_conflict_fast_abort",
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert captured[0]["title"] == "Merge aborted"
        assert "fast strategy" in captured[0]["content"]
        assert "binary file conflict" in captured[0]["content"]
        assert "cannot be auto-resolved" in captured[0]["content"]
        assert "human review" not in captured[0]["content"].lower()
        assert "requires" not in captured[0]["content"].lower()

    def test_conflict_context_failed_call_file_write_failed_rendering(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """failure_reason='conflict_context_failed_call_file_write_failed' → correct title/summary."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="conflict_context_failed_call_file_write_failed",
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert captured[0]["title"] == "Merge failed"
        assert "failed to build conflict context" in captured[0]["content"]
        assert "could not write human call file" in captured[0]["content"]

    def test_success_with_runtime_sync_collisions_rendered(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Success path with runtime_sync_collisions renders collision summary."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport
        from se3.engine.merge.runtime_sync import BypassedCollision

        report = MergeReport(
            success=True,
            merged_branches=["feature"],
            runtime_sync_collisions=[
                BypassedCollision(
                    branch="feature",
                    original_rel_path="history/flow1.log",
                    sidecar_rel_path="history/flow1.log.from-feature",
                    src_hash="abcd1234" * 8,
                    dest_hash="efgh5678" * 8,
                ),
            ],
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 0
        assert len(captured) == 1
        assert "Runtime sync collisions (sidecar bypass):" in captured[0]["content"]
        assert "history/flow1.log" in captured[0]["content"]
        assert "history/flow1.log.from-feature" in captured[0]["content"]
        assert "src_hash=abcd1234" in captured[0]["content"]
        assert "dest_hash=efgh5678" in captured[0]["content"]

    def test_pending_human_with_runtime_sync_collisions_rendered(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Pending-human path with runtime_sync_collisions renders collision summary."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport
        from se3.engine.merge.runtime_sync import BypassedCollision

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="pending_human",
            pending_human=True,
            runtime_sync_collisions=[
                BypassedCollision(
                    branch="feature",
                    original_rel_path="history/flow1.log",
                    sidecar_rel_path="history/flow1.log.from-feature",
                    src_hash="abcd1234" * 8,
                    dest_hash="efgh5678" * 8,
                ),
            ],
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 130
        assert len(captured) == 1
        assert "Runtime sync collisions (sidecar bypass):" in captured[0]["content"]
        assert "history/flow1.log" in captured[0]["content"]

    def test_generic_failure_with_runtime_sync_collisions_rendered(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Generic failure path with runtime_sync_collisions renders collision summary."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport
        from se3.engine.merge.runtime_sync import BypassedCollision

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="runtime_sync_os_error",
            runtime_sync_collisions=[
                BypassedCollision(
                    branch="feature-a",
                    original_rel_path="history/flow1.log",
                    sidecar_rel_path="history/flow1.log.from-feature-a",
                    src_hash="abcd1234" * 8,
                    dest_hash="efgh5678" * 8,
                ),
            ],
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert "Runtime sync collisions (sidecar bypass):" in captured[0]["content"]
        assert "history/flow1.log" in captured[0]["content"]

    def test_rollback_failed_with_runtime_sync_collisions_rendered(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Rollback-failed path with runtime_sync_collisions renders collision summary.

        Defense-in-depth: collisions and rollback_failed are orthogonal in
        practice, but the branch must still render them consistently.
        """
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport
        from se3.engine.merge.runtime_sync import BypassedCollision

        report = MergeReport(
            success=False,
            failed_branch="feature",
            failure_reason="rollback_failed",
            rollback_failed=True,
            runtime_sync_collisions=[
                BypassedCollision(
                    branch="feature",
                    original_rel_path="history/flow1.log",
                    sidecar_rel_path="history/flow1.log.from-feature",
                    src_hash="abcd1234" * 8,
                    dest_hash="efgh5678" * 8,
                ),
            ],
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert "INCONSISTENT" in captured[0]["content"]
        assert "Runtime sync collisions (sidecar bypass):" in captured[0]["content"]
        assert "history/flow1.log" in captured[0]["content"]

    def test_runtime_sync_collision_shows_colliding_path(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Strict-mode runtime_sync_collision surfaces the colliding path."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=False,
            failed_branch="feature-b",
            failure_reason="runtime_sync_collision",
            runtime_sync_collision_path="history/run-007.json",
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature-b"], project_root=tmp_path)
        assert exit_code == 1
        assert len(captured) == 1
        assert "Failed branch: feature-b" in captured[0]["content"]
        assert "Colliding path: se3/history/run-007.json" in captured[0]["content"]

    def test_audit_only_collision_renders_distinct_section(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Audit-only collision (written=False) uses a distinct rendered header.

        Regression guard: an audit-only row in ``runtime_sync_collisions``
        must NOT be presented under the "sidecar bypass" header that
        implies the source data was preserved on disk.  The renderer must
        split the two lists by ``written`` so operators can tell at a
        glance which collisions are recoverable from disk and which are
        bookkeeping-only.
        """
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport
        from se3.engine.merge.runtime_sync import BypassedCollision

        report = MergeReport(
            success=True,
            merged_branches=["feature"],
            runtime_sync_collisions=[
                BypassedCollision(
                    branch="feature",
                    original_rel_path="history/lost.log",
                    sidecar_rel_path="history/lost.log.from-feature",
                    src_hash="deadbeef" * 8,
                    dest_hash="unavailable",
                    written=False,
                ),
            ],
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 0
        assert len(captured) == 1
        body = captured[0]["content"]
        # Must NOT label this row as a successful sidecar bypass.
        assert "Runtime sync collisions (sidecar bypass):" not in body
        # Must surface the audit-only header so operators don't believe the
        # source data is recoverable from disk.
        assert (
            "Runtime sync collisions (audit-only — sidecar NOT written; "
            "source data is NOT recoverable from disk):"
        ) in body
        # Branch + paths still rendered for operators.
        assert "feature" in body
        assert "history/lost.log" in body
        assert "history/lost.log.from-feature" in body
        # The 'unavailable' dest_hash placeholder is rendered verbatim
        # rather than truncated to 'unavail..' — the renderer should
        # detect the placeholder and pass it through.
        assert "dest_hash=unavailable" in body
        assert "src_hash=deadbeef" in body

    def test_mixed_written_and_audit_only_collisions_render_both_sections(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Both sections render when collisions contain a mix of written and audit-only rows."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport
        from se3.engine.merge.runtime_sync import BypassedCollision

        report = MergeReport(
            success=True,
            merged_branches=["feature-a", "feature-b"],
            runtime_sync_collisions=[
                BypassedCollision(
                    branch="feature-a",
                    original_rel_path="history/saved.log",
                    sidecar_rel_path="history/saved.log.from-feature-a",
                    src_hash="11111111" * 8,
                    dest_hash="22222222" * 8,
                    written=True,
                ),
                BypassedCollision(
                    branch="feature-b",
                    original_rel_path="history/lost.log",
                    sidecar_rel_path="history/lost.log.from-feature-b",
                    src_hash="33333333" * 8,
                    dest_hash="unavailable",
                    written=False,
                ),
            ],
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature-a", "feature-b"], project_root=tmp_path)
        assert exit_code == 0
        body = captured[0]["content"]
        # Both headers are present.
        assert "Runtime sync collisions (sidecar bypass):" in body
        assert (
            "Runtime sync collisions (audit-only — sidecar NOT written; "
            "source data is NOT recoverable from disk):"
        ) in body
        # Each row appears under the correct branch label.
        assert "feature-a" in body
        assert "history/saved.log.from-feature-a" in body
        assert "feature-b" in body
        assert "history/lost.log.from-feature-b" in body
        # Order: the 'sidecar bypass' (written=True) section MUST appear
        # before the 'audit-only' section, so operators read the
        # recoverable rows first.
        assert body.index(
            "Runtime sync collisions (sidecar bypass):"
        ) < body.index(
            "Runtime sync collisions (audit-only — sidecar NOT written"
        )

    def test_only_written_collisions_render_only_sidecar_bypass_section(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When all collisions succeeded (written=True), the audit-only header is suppressed."""
        _init_repo(tmp_path)
        from se3.engine.merge.orchestrator import MergeReport
        from se3.engine.merge.runtime_sync import BypassedCollision

        report = MergeReport(
            success=True,
            merged_branches=["feature"],
            runtime_sync_collisions=[
                BypassedCollision(
                    branch="feature",
                    original_rel_path="history/saved.log",
                    sidecar_rel_path="history/saved.log.from-feature",
                    src_hash="11111111" * 8,
                    dest_hash="22222222" * 8,
                    written=True,
                ),
            ],
        )
        captured = self._mock_orchestrator_report(monkeypatch, report)
        exit_code = run_merge(["feature"], project_root=tmp_path)
        assert exit_code == 0
        body = captured[0]["content"]
        assert "Runtime sync collisions (sidecar bypass):" in body
        # Audit-only header MUST be absent when no audit-only rows exist.
        assert "audit-only" not in body
