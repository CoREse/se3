"""Tests for MergeOrchestrator sequential merge logic."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from se3.engine.merge.orchestrator import MergeOrchestrator, MergeReport


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


def _is_working_tree_clean(path: Path) -> bool:
    """Check if working tree has no tracked uncommitted changes."""
    result = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, text=True, check=True,
    )
    return not result.stdout.strip()


def _create_worktree(path: Path, branch: str, wt_dir: Path) -> None:
    """Create a git worktree for *branch* at *wt_dir*."""
    subprocess.run(
        ["git", "-C", str(path), "worktree", "add", str(wt_dir), branch],
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
    """Stage files and commit."""
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


class TestMergeOrchestrator:
    def test_merge_single_clean_branch(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature"])

        assert report.success is True
        assert report.merged_branches == ["feature"]
        assert report.failed_branch is None
        assert report.log_file is not None
        assert report.log_file.exists()

    def test_merge_multiple_clean_branches(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        _create_branch(tmp_path, "feature-b")
        _add_commit(tmp_path, "b.txt", "b", "Add B")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature-a", "feature-b"])

        assert report.success is True
        assert report.merged_branches == ["feature-a", "feature-b"]

        # Verify both branches' content is present
        assert (tmp_path / "a.txt").exists()
        assert (tmp_path / "b.txt").exists()

    def test_conflict_aborts_and_restores(self, tmp_path: Path, monkeypatch) -> None:
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
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

        pre_merge_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Mock LLM resolver to fail, triggering abort path
        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve",
            lambda self, ctx, strategy: (_ for _ in ()).throw(RuntimeError("mock llm fail")),
        )

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "merge_conflict"

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged (pre-merge state preserved)
        post_abort_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_abort_head == pre_merge_head

    def test_second_branch_fails_first_preserved(self, tmp_path: Path, monkeypatch) -> None:
        """If the second branch has a conflict, the first merge is preserved."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        # feature-a: clean merge
        _create_branch(tmp_path, "feature-a")
        _add_commit(tmp_path, "a.txt", "a", "Add A")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        # feature-b: will conflict
        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature-b")
        (tmp_path / "shared.txt").write_text("b content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on b"],
            check=True, capture_output=True,
        )
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

        # Mock LLM resolver to fail, triggering abort path on feature-b
        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve",
            lambda self, ctx, strategy: (_ for _ in ()).throw(RuntimeError("mock llm fail")),
        )

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature-a", "feature-b"])

        assert report.success is False
        assert "feature-a" in report.merged_branches
        assert report.failed_branch == "feature-b"

        # Verify feature-a is merged (a.txt should exist)
        assert (tmp_path / "a.txt").exists()

        # Working tree should be clean
        assert _is_working_tree_clean(tmp_path) is True

    def test_log_file_created(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature"])

        assert report.log_file is not None
        assert report.log_file.exists()
        log_content = report.log_file.read_text()
        assert "Merge orchestrator starting" in log_content
        assert "feature" in log_content
        assert "merged successfully" in log_content

    def test_already_up_to_date_merge_does_not_amend(self, tmp_path: Path) -> None:
        """Merging an already-merged branch must not create a commit or bump version."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "1.0.0")
        _commit(tmp_path, "Add pyproject")

        # Create a feature branch and merge it into main FIRST
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        # Merge feature into main so it's already an ancestor
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            check=True, capture_output=True,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        pre_version = subprocess.run(
            ["git", "-C", str(tmp_path), "show", f"{pre_head}:pyproject.toml"],
            capture_output=True, text=True, check=True,
        ).stdout
        assert 'version = "1.0.0"' in pre_version

        # Now run orchestrator to merge the already-merged branch
        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature"])

        assert report.success is True
        assert report.merged_branches == ["feature"]

        # HEAD must NOT have changed — no new commit was produced
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # pyproject.toml version must still be 1.0.0 (no amend corruption)
        pyproject = tmp_path / "pyproject.toml"
        assert 'version = "1.0.0"' in pyproject.read_text()

        # No version aggregation should have occurred
        assert report.pre_merge_version == "1.0.0"
        assert report.final_version is None
        assert report.version_aggregation_skipped is True

    def test_rev_parse_head_failure_guardrails_fail_closed(self, tmp_path: Path, monkeypatch) -> None:
        """If the initial rev-parse HEAD in _merge_single_branch fails, the merge
        is still attempted but guardrails fail-closed due to empty pre_sha."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        call_count = 0
        original_run_git = None

        def patch_run_git(project_root, *args, check=True, timeout=30):
            nonlocal call_count
            # Make the SECOND "rev-parse HEAD" call fail (the one inside
            # _merge_single_branch, lines 312-316). The first is in execute().
            if len(args) >= 2 and args[0] == "rev-parse" and args[1] == "HEAD":
                call_count += 1
                if call_count == 2:
                    class FakeResult:
                        returncode = 1
                        stdout = ""
                        stderr = "mock rev-parse failure"
                    return FakeResult()
            # Fall through to real _run_git for everything else
            import se3.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator._run_git", patch_run_git
        )

        orch = MergeOrchestrator(project_root=tmp_path)
        report = orch.execute(["feature"])

        # The merge itself succeeds (git merge feature works), but because
        # pre_merge_sha in _merge_single_branch was empty, guardrails
        # fail-closed with CHECK_FAILURE. Rollback is attempted using
        # the captured pre_sha from _merge_single_branch (which is empty),
        # so it becomes a no-op. The orchestrator now reports
        # rollback_failed so the user sees an honest diagnosis.
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "rollback_failed"
        assert report.rollback_failed is True
        assert report.human_call_file is not None

        # The merge commit IS still on HEAD because rollback couldn't be
        # attempted (no pre_sha). The call file should mention this.
        call_data = json.loads(report.human_call_file.read_text())
        assert any(
            "could not roll back" in v["message"].lower()
            for v in call_data["violations"]
        )

    def test_merge_report_defaults(self) -> None:
        report = MergeReport()
        assert report.success is False
        assert report.merged_branches == []
        assert report.failed_branch is None
        assert report.failure_reason is None
        assert report.pending_human is False

    def test_rollback_failed_sets_flag(self, tmp_path: Path, monkeypatch) -> None:
        """If guardrails detect violations but rollback fails, report.rollback_failed=True."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHALL validate all user inputs.\n"
        )
        (tmp_path / "code.py").write_text("def auth(): pass\n")
        _commit(tmp_path, "initial")

        # Create feature branch that weakens a spec (SHALL -> SHOULD)
        _create_branch(tmp_path, "feature")
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Mock _rollback_to to simulate git reset --hard failure
        def mock_rollback(self, sha):
            raise RuntimeError("git reset --hard failed: mock failure")

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.MergeOrchestrator._rollback_to",
            mock_rollback,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.rollback_failed is True
        assert report.failure_reason == "rollback_failed"
        assert report.failed_branch == "feature"

    def test_guardrail_print_instructions_failure_not_rollback(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """If write_guardrail_call succeeds but print_instructions raises,
        report.failure_reason must be 'guardrail_violation', not
        'rollback_failed'. The call file on disk must still exist.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHALL validate all user inputs.\n"
        )
        (tmp_path / "code.py").write_text("def auth(): pass\n")
        _commit(tmp_path, "initial")

        # Create feature branch that weakens a spec (SHALL -> SHOULD)
        _create_branch(tmp_path, "feature")
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Monkeypatch print_instructions to simulate broken pipe
        def mock_print_instructions(self, call_file):
            raise BrokenPipeError("mock broken pipe")

        monkeypatch.setattr(
            "se3.engine.merge.human_call.HumanCallWriter.print_instructions",
            mock_print_instructions,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.rollback_failed is False
        assert report.failure_reason == "guardrail_violation"
        assert report.failed_branch == "feature"
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

    def test_conflict_human_call_print_instructions_failure_preserved(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """If write_call succeeds but print_instructions raises in the HUMAN_CALL
        branch of _handle_conflict, report.failure_reason must be 'pending_human',
        and the call file on disk must still exist.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
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
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver: low confidence -> triggers HUMAN_CALL
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",
                        hunks=[HunkResolution(1, 3, Confidence.LOW, "uncertain")],
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

        # Monkeypatch print_instructions to simulate broken pipe
        def mock_print_instructions(self, call_file):
            raise BrokenPipeError("mock broken pipe")

        monkeypatch.setattr(
            "se3.engine.merge.human_call.HumanCallWriter.print_instructions",
            mock_print_instructions,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.pending_human is True
        assert report.failure_reason == "pending_human"
        assert report.failed_branch == "feature"
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # HEAD should be unchanged (no merge commit)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_guardrail_write_call_failure_after_rollback_not_rollback_failed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """If write_guardrail_call raises AFTER a successful rollback,
        report.failure_reason must be 'guardrail_violation_call_failed', not
        'rollback_failed', and report.rollback_failed must be False.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHALL validate all user inputs.\n"
        )
        (tmp_path / "code.py").write_text("def auth(): pass\n")
        _commit(tmp_path, "initial")

        # Create feature branch that weakens a spec (SHALL -> SHOULD)
        _create_branch(tmp_path, "feature")
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Monkeypatch write_guardrail_call to simulate disk-full after rollback
        def mock_write_guardrail_call(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(
            "se3.engine.merge.human_call.HumanCallWriter.write_guardrail_call",
            mock_write_guardrail_call,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        # The true failure mode is "rollback succeeded, call file write failed"
        assert report.success is False
        assert report.rollback_failed is False
        assert report.failure_reason == "guardrail_violation_call_failed"
        assert report.failed_branch == "feature"

        # HEAD should be restored to pre-merge state (rollback succeeded)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head


class TestMergeOrchestratorConflictResolution:
    """Integration tests for conflict resolution via mocked LLM."""

    def _create_conflict_repo(self, tmp_path: Path) -> tuple[str, str]:
        """Create repo with conflicting branches. Returns (default_branch, feature_branch)."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "line1\nline2\nline3\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("line1\nFEATURE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("line1\nBASE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on base"],
            check=True, capture_output=True,
        )
        return default_branch, "feature"

    def test_default_accept_resolves_and_commits(self, tmp_path: Path, monkeypatch) -> None:
        """default strategy + high confidence → ACCEPT → write back → commit."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        call_count = 0

        def mock_resolve(self, context, strategy):
            nonlocal call_count
            call_count += 1
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="line1\nRESOLVED\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "merged both")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute([feature_branch])

        # Should succeed with one LLM call
        assert call_count == 1, f"Expected 1 LLM call, got {call_count}"
        assert report.success is True
        assert feature_branch in report.merged_branches

        # Working tree should be clean
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should have changed (merge commit created)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head != pre_head

        # File should contain resolved content
        assert (tmp_path / "shared.txt").read_text() == "line1\nRESOLVED\nline3\n"

    def test_default_human_call_preserves_markers(self, tmp_path: Path, monkeypatch) -> None:
        """default strategy + low confidence → HUMAN_CALL → conflict markers preserved."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

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

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute([feature_branch])

        assert report.success is False
        assert report.pending_human is True
        assert report.failed_branch == feature_branch

        # Working tree should still have conflict markers
        assert (tmp_path / "shared.txt").exists()
        content = (tmp_path / "shared.txt").read_text()
        assert "<<<<<<<" in content
        assert "=======" in content
        assert ">>>>>>>" in content

        # HEAD should be unchanged (no merge commit)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # Call file should exist and be set on report
        assert report.human_call_file is not None
        calls_dir = tmp_path / "se3" / "calls"
        call_files = list(calls_dir.glob("merge_*.json"))
        assert len(call_files) >= 1

    def test_reject_aborts_and_restores(self, tmp_path: Path, monkeypatch) -> None:
        """Any strategy where decision is REJECT → git merge --abort."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="bad",
                        hunks=[HunkResolution(1, 5, Confidence.LOW, "bad")],
                        overall_confidence=Confidence.LOW,
                        flags={"requires_human_review": True, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.LOW,
                flags={"requires_human_review": True, "spec_guardrail_concern": False},
            )

        # Force REJECT by using strict strategy where low confidence = HUMAN_CALL
        # Actually strict returns HUMAN_CALL not REJECT. Let me use a mock decider instead.
        def mock_decide(self, resolution, has_spec_files, strategy):
            from se3.engine.merge.strategy import DecisionAction, StrategyDecision
            return StrategyDecision(
                action=DecisionAction.REJECT,
                reason="mock reject",
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )
        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.StrategyDecider.decide", mock_decide
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute([feature_branch])

        assert report.success is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "merge_conflict"

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_fast_spec_guardrail_concern_accepted(self, tmp_path: Path, monkeypatch) -> None:
        """fast strategy + spec_guardrail_concern → ACCEPT (deferred to post-merge guardrails).

        The LLM's spec_guardrail_concern flag is ignored in _decide_fast;
        post-merge guardrails handle real violations. Since shared.txt is not
        a real spec file, guardrails pass and the merge succeeds.
        """
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="line1\nRESOLVED\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "ok")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": True},
                        is_spec=True,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": True},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute([feature_branch])

        # spec_guardrail_concern is deferred; merge succeeds
        assert report.success is True
        assert feature_branch in report.merged_branches
        assert (tmp_path / "shared.txt").read_text() == "line1\nRESOLVED\nline3\n"

    def test_fast_strategy_conflict_markers_in_non_spec_file_abort(self, tmp_path: Path, monkeypatch) -> None:
        """fast strategy + non-spec file with conflict markers in resolved_content → abort.

        Even in fast mode, resolved content that still contains git conflict
        markers must be rejected before being committed. The first-pass
        validation detects the markers and aborts the merge.
        """
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        # LLM erroneously left conflict markers in the resolution
                        resolved_content="line1\n<<<<<<< HEAD\nBASE\n=======\nFEATURE\n>>>>>>> branch\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "merged both")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute([feature_branch])

        # Merge must fail — markers were detected and aborted
        assert report.success is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "fast_abort"

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged (no merge commit)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # File on disk must NOT contain the marker-laden content
        content = (tmp_path / "shared.txt").read_text()
        # After abort, working tree is restored to pre-merge state (ours)
        assert "BASE" in content
        # LLM resolution with markers was NOT written
        assert "RESOLVED" not in content

    def test_strict_low_hunk_human_call(self, tmp_path: Path, monkeypatch) -> None:
        """strict strategy + one low-confidence hunk → HUMAN_CALL."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="line1\nRESOLVED\nline3\n",
                        hunks=[
                            HunkResolution(1, 3, Confidence.HIGH, "clear"),
                            HunkResolution(4, 8, Confidence.LOW, "unclear"),
                        ],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="strict")
        report = orch.execute([feature_branch])

        assert report.success is False
        assert report.pending_human is True

    def test_llm_called_once_per_merge(self, tmp_path: Path, monkeypatch) -> None:
        """Verify exactly one LLM call is made per conflicting merge."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        call_count = 0

        def mock_resolve(self, context, strategy):
            nonlocal call_count
            call_count += 1
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="line1\nRESOLVED\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "ok")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute([feature_branch])

        assert call_count == 1, f"Expected exactly 1 LLM call, got {call_count}"
        assert report.success is True

    def test_valid_llm_deletion_tracked_text_file(self, tmp_path: Path, monkeypatch) -> None:
        """LLM returns empty resolved_content for tracked text file → file deleted via git rm -f.

        To reach the deletion path the working-tree file must NOT have conflict
        markers (the safety check rejects deletion of files that still contain
        markers).  We therefore start a normal text conflict and then overwrite
        the working-tree file with marker-free content before the orchestrator
        runs, simulating a "resolve by deletion" decision.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "deleteme.txt", "delete me please\n", "Add deleteme")
        _create_branch(tmp_path, "feature")
        (tmp_path / "deleteme.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "deleteme.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change deleteme on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "deleteme.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "deleteme.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change deleteme on main"],
            check=True, capture_output=True,
        )

        # Start the merge so the file enters the unmerged state, then remove
        # conflict markers so the deletion path is allowed.
        merge_result = subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        # The merge should have produced a conflict
        assert "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr, (
            f"Expected conflict but got: {merge_result.stdout} {merge_result.stderr}"
        )
        # Remove conflict markers — simulate a human pre-resolving to "delete"
        (tmp_path / "deleteme.txt").write_text("resolved content without markers\n")

        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="deleteme.txt",
                        resolved_content="",  # empty → deletion
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "delete it")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        # Should succeed — file was legitimately deleted
        assert report.success is True
        assert "feature" in report.merged_branches

        # File should NOT exist anymore
        assert not (tmp_path / "deleteme.txt").exists()

        # Working tree should be clean
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should have changed (merge commit created)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head != pre_head

    def test_incomplete_llm_resolution_routes_to_human_call(self, tmp_path: Path, monkeypatch) -> None:
        """LLM omits one of two conflict files → HUMAN_CALL with report.human_call_file set."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # Create two files that will conflict
        _add_commit(tmp_path, "a.txt", "a base\n", "Add a")
        _add_commit(tmp_path, "b.txt", "b base\n", "Add b")
        _create_branch(tmp_path, "feature")
        (tmp_path / "a.txt").write_text("a feature\n")
        (tmp_path / "b.txt").write_text("b feature\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "a.txt").write_text("a main\n")
        (tmp_path / "b.txt").write_text("b main\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver: only resolves a.txt, omits b.txt
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="a.txt",
                        resolved_content="a resolved\n",
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "merged a")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                    # b.txt intentionally omitted
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        # Should fail with pending_human due to incomplete resolution
        assert report.success is False
        assert report.pending_human is True
        assert report.failed_branch == "feature"
        assert report.failure_reason == "pending_human"
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # HEAD should be unchanged (no merge commit)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # Call file should mention incomplete resolution
        data = json.loads(report.human_call_file.read_text())
        assert "incomplete" in data["decision_reason"].lower()
        assert "b.txt" in data["decision_reason"]
        # The instructions text must reference the actual call-file name
        assert report.human_call_file.name + ".response" in data["instructions"]

    def test_binary_conflict_aborts_not_deletes(self, tmp_path: Path, monkeypatch) -> None:
        """Binary file + empty LLM resolved_content → abort, do NOT delete the file."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # Create a file with null bytes so our heuristic treats it as binary
        binary_content = b"line1\n\x00\x01\x02\nline3\n"
        (tmp_path / "data.bin").write_bytes(binary_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Add binary file"],
            check=True, capture_output=True,
        )

        # Create feature branch with different binary content
        _create_branch(tmp_path, "feature")
        feature_content = b"line1\n\x00\xfe\xff\nfeature_line\n"
        (tmp_path / "data.bin").write_bytes(feature_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change binary on feature"],
            check=True, capture_output=True,
        )

        # Switch back to main, change binary differently
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        main_content = b"line1\n\x00\xaa\xbb\nmain_line\n"
        (tmp_path / "data.bin").write_bytes(main_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change binary on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver: returns empty resolved_content (triggers deletion path)
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="data.bin",
                        resolved_content="",  # empty → would trigger deletion path
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "binary")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        # Should fail — binary files cannot be auto-resolved
        assert report.success is False
        assert report.failed_branch == "feature"

        # The binary file should still exist on disk (not deleted by git rm)
        assert (tmp_path / "data.bin").exists()

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged (pre-merge state preserved)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_path_validation_rejects_bogus_paths(self, tmp_path: Path, monkeypatch) -> None:
        """LLM returns path traversal → extra-files pre-check routes to HUMAN_CALL."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
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
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    # Include the legitimate conflict file …
                    FileResolution(
                        path="shared.txt",
                        resolved_content="resolved\n",
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "ok")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                    # … and a bogus path that the extra-files pre-check catches.
                    FileResolution(
                        path="../escape.txt",
                        resolved_content="evil content",
                        hunks=[HunkResolution(1, 1, Confidence.HIGH, "bad")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        assert report.success is False
        assert report.failed_branch == "feature"
        # Extra files are now caught in the pre-check and routed to HUMAN_CALL
        assert report.failure_reason == "pending_human"
        assert report.human_call_file is not None

        # Bogus path must NOT have been written outside the project root
        assert not (tmp_path.parent / "escape.txt").exists()

        # HEAD should be unchanged (pre-merge state preserved)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head


class TestMergeOrchestratorCleanupInteraction:
    """Tests for --delete-merged interaction with aggregation failures."""

    def test_aggregation_failure_keeps_success_and_runs_cleanup(self, tmp_path: Path, monkeypatch) -> None:
        """If aggregate_and_apply fails, merges remain durable (success=True)
        and cleanup still runs because the merge itself succeeded."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _write_pyproject(tmp_path, "4.4.0")
        _commit(tmp_path, "Add pyproject")

        # feature: clean merge with a version bump so aggregate_and_apply is invoked
        _create_branch(tmp_path, "feature")
        _write_pyproject(tmp_path, "4.4.1")
        (tmp_path / "feat.txt").write_text("feature")
        _commit(tmp_path, "Bump version on feature", "pyproject.toml", "feat.txt")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Mock aggregate_and_apply to fail (simulate amend failure)
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

        orch = MergeOrchestrator(
            project_root=tmp_path,
            strategy="default",
            delete_merged=True,
        )
        report = orch.execute(["feature"])

        # Merges succeeded → report.success remains True, error surfaced separately
        assert report.success is True
        assert report.version_aggregation_error is not None
        assert "amend failed" in report.version_aggregation_error

        # Cleanup should still run because the merge succeeded
        assert report.cleanup_skipped is False
        assert report.cleanup_report is not None

        # Branch should be deleted (cleanup ran)
        branch_exists = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--verify", "feature"],
            capture_output=True, text=True, check=False,
        )
        assert branch_exists.returncode != 0

    def test_guardrails_exception_fails_closed(self, tmp_path: Path, monkeypatch) -> None:
        """If MergeGuardrailsCheck.check_merge_result raises, the merge is
        treated as a violation (fail closed), rolled back, and a human call
        file is written."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHALL validate all user inputs.\n"
        )
        (tmp_path / "code.py").write_text("def auth(): pass\n")
        _commit(tmp_path, "initial")

        # Create a feature branch that changes a regular file (no spec changes)
        _create_branch(tmp_path, "feature")
        (tmp_path / "code.py").write_text("def auth(): return True\n")
        _commit(tmp_path, "change code")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Mock check_merge_result to raise — simulates a bug in the checker
        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.MergeGuardrailsCheck.check_merge_result",
            lambda self, pre, post: (_ for _ in ()).throw(RuntimeError("mock diff parser blowup")),
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        # Should be treated as guardrail violation (fail closed)
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "guardrail_violation"
        assert report.pending_human is True

        # HEAD should be restored to pre-merge state
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # Human call file should exist (generic CHECK_FAILURE)
        calls_dir = tmp_path / "se3" / "calls"
        call_files = list(calls_dir.glob("merge_*_guardrail.json"))
        assert len(call_files) == 1
        data = json.loads(call_files[0].read_text())
        assert data["type"] == "guardrail_violation"
        assert any(v["violation_type"] == "CHECK_FAILURE" for v in data["violations"])

    def test_delete_merged_with_dirty_worktree_skips_and_reports_success(self, tmp_path: Path) -> None:
        """Successful merge + delete_merged=True with dirty worktree → success=True, skipped_dirty populated."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # Create feature branch with a tracked file
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Create worktree for feature branch
        wt_dir = tmp_path / "wt_feature"
        _create_worktree(tmp_path, "feature", wt_dir)

        # Dirty the worktree by modifying a tracked file
        (wt_dir / "feat.txt").write_text("modified uncommitted")

        # Run merge with delete_merged=True
        orch = MergeOrchestrator(project_root=tmp_path, delete_merged=True)
        report = orch.execute(["feature"])

        # Merge itself should succeed
        assert report.success is True
        assert report.merged_branches == ["feature"]

        # Cleanup should have run but skipped due to dirty worktree
        assert report.cleanup_skipped is False
        assert report.cleanup_report is not None
        assert len(report.cleanup_report.skipped_dirty) == 1
        assert report.cleanup_report.skipped_dirty[0][0] == "feature"

        # Branch should still exist (not deleted because worktree is dirty)
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--verify", "feature"],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0

        # Worktree should still exist
        assert wt_dir.exists()

    def test_binary_conflict_non_empty_resolution_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """Binary file conflict + LLM returns non-empty content → abort via is_binary check.

        The first-pass validation in ``_apply_resolution`` detects that the
        conflict file is binary and aborts before writing anything back.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        # Create a file with null bytes so our heuristic treats it as binary
        binary_content = b"line1\n\x00\x01\x02\nline3\n"
        (tmp_path / "data.bin").write_bytes(binary_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Add binary file"],
            check=True, capture_output=True,
        )

        # Create feature branch with different binary content
        _create_branch(tmp_path, "feature")
        feature_content = b"line1\n\x00\xfe\xff\nfeature_line\n"
        (tmp_path / "data.bin").write_bytes(feature_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change binary on feature"],
            check=True, capture_output=True,
        )

        # Switch back to main, change binary differently
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        main_content = b"line1\n\x00\xaa\xbb\nmain_line\n"
        (tmp_path / "data.bin").write_bytes(main_content)
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "data.bin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change binary on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver: returns NON-empty resolved_content for binary file
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="data.bin",
                        # Non-empty content for a binary file — should be rejected
                        resolved_content="some text that claims to resolve binary",
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "binary")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        # Should fail — binary files cannot be auto-resolved
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "merge_conflict"

        # The binary file should still have the main content (not overwritten by LLM)
        assert (tmp_path / "data.bin").read_bytes() == main_content

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged (pre-merge state preserved)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_git_rm_failure_in_apply_resolution_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """Second-pass ``git rm -f`` failure in _apply_resolution → abort merge.

        When LLM returns empty resolved_content for a text file (deletion),
        the second pass calls ``git rm -f``. If that fails, the merge must be
        aborted and "conflict" returned.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "deleteme.txt", "delete me please\n", "Add deleteme")
        _create_branch(tmp_path, "feature")
        (tmp_path / "deleteme.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "deleteme.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change deleteme on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "deleteme.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "deleteme.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change deleteme on main"],
            check=True, capture_output=True,
        )

        # Start the merge so the file enters the unmerged state, then remove
        # conflict markers so the deletion path is allowed.
        merge_result = subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        assert "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr, (
            f"Expected conflict but got: {merge_result.stdout} {merge_result.stderr}"
        )
        # Remove conflict markers — simulate a human pre-resolving to "delete"
        (tmp_path / "deleteme.txt").write_text("resolved content without markers\n")

        # Mock LLM resolver: returns empty resolved_content → deletion path
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="deleteme.txt",
                        resolved_content="",  # empty → deletion
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "delete it")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        # Track whether abort was called
        abort_calls = []
        original_abort = MergeOrchestrator._abort_merge

        def tracked_abort(self):
            abort_calls.append(True)
            return original_abort(self)

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.MergeOrchestrator._abort_merge", tracked_abort
        )

        # Monkeypatch _run_git to fail the "rm -f" call
        original_run_git = None

        def patch_run_git(project_root, *args, check=True, timeout=30):
            if len(args) >= 2 and args[0] == "rm" and args[1] == "-f":
                class FakeResult:
                    returncode = 1
                    stdout = ""
                    stderr = "mock rm failure"
                return FakeResult()
            # Fall through to real _run_git for everything else
            import se3.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator._run_git", patch_run_git
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        # Should fail — git rm -f failed
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "merge_conflict"

        # Abort must have been called
        assert len(abort_calls) >= 1

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged (pre-merge state preserved)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_rename_conflict_absent_working_tree_path_deletes_via_git_rm(self, tmp_path: Path, monkeypatch) -> None:
        """Rename/delete conflict where the path is absent from working tree but
        has unmerged index entries — empty resolved_content triggers
        ``git rm -f --ignore-unmatch`` and the merge succeeds.

        Regression test for the branch in _apply_resolution lines 661-667.
        """
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Start the merge so the file enters the unmerged state
        merge_result = subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        assert "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr, (
            f"Expected conflict but got: {merge_result.stdout} {merge_result.stderr}"
        )

        # Remove the file from the working tree while preserving unmerged index entries.
        # This simulates a rename/delete conflict where the original path is absent
        # from the working tree but still present in the index with stages 1/2/3.
        (tmp_path / "shared.txt").unlink()

        # Verify the index still lists the file as unmerged
        diff_result = subprocess.run(
            ["git", "-C", str(tmp_path), "diff", "--name-only", "--diff-filter=U"],
            capture_output=True, text=True, check=True,
        )
        assert "shared.txt" in diff_result.stdout

        # Verify the working-tree path is absent
        assert not (tmp_path / "shared.txt").exists()

        # Mock LLM resolver: returns empty resolved_content -> deletion path
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",  # empty -> deletion
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "delete it")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        # Should succeed — git rm -f --ignore-unmatch handled the absent path
        assert report.success is True
        assert "feature" in report.merged_branches

        # File should NOT exist anymore
        assert not (tmp_path / "shared.txt").exists()

        # Working tree should be clean
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should have changed (merge commit created)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head != pre_head

    def test_write_call_failure_aborts_merge(self, tmp_path: Path, monkeypatch) -> None:
        """If HumanCallWriter.write_call raises, the merge must abort and
        return 'human_call_write_failed' (not 'pending_human') so the user
        knows manual recovery is required."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
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
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Mock LLM resolver: low confidence -> strategy returns HUMAN_CALL
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",
                        hunks=[HunkResolution(1, 3, Confidence.LOW, "uncertain")],
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

        # Mock HumanCallWriter.write_call to raise
        def mock_write_call(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.HumanCallWriter.write_call",
            mock_write_call,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        # Must abort, not return pending_human without a call file
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "human_call_write_failed"
        assert report.human_call_file is None

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_deletion_low_confidence_rejected_for_modify_delete(self, tmp_path: Path, monkeypatch) -> None:
        """LLM returns empty resolved_content with LOW confidence for a file that
        has non-empty content in ours/theirs -> deletion is rejected and merge
        aborts."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Start merge, remove conflict markers to reach deletion validation
        merge_result = subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        assert "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr
        (tmp_path / "shared.txt").write_text("resolved content without markers\n")

        # Mock LLM resolver: HIGH overall confidence (strategy accepts) but
        # LOW hunk confidence — the deletion gate should reject because not all
        # hunks have HIGH confidence for a file with content in ours/theirs.
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",  # empty -> deletion
                        hunks=[HunkResolution(1, 3, Confidence.LOW, "maybe delete")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        # Should fail — low-confidence deletion of a file with content rejected
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "merge_conflict"

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_deletion_empty_hunks_high_confidence_accepted(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Empty hunks + overall_confidence=HIGH → deletion accepted."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Start merge, remove conflict markers to reach deletion validation
        merge_result = subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        assert "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr
        (tmp_path / "shared.txt").write_text("resolved content without markers\n")

        # Mock LLM: empty hunks list, HIGH overall confidence → should accept deletion
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",  # empty → deletion
                        hunks=[],  # no hunks → fall back to overall_confidence
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # Should succeed — empty hunks + HIGH overall → accepted
        assert report.success is True
        assert "feature" in report.merged_branches
        assert not (tmp_path / "shared.txt").exists()
        assert _is_working_tree_clean(tmp_path) is True

    def test_deletion_empty_hunks_low_confidence_rejected(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Empty hunks + overall_confidence=LOW → deletion rejected (not HIGH)."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "shared.txt", "base content\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("feature version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("main version\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on main"],
            check=True, capture_output=True,
        )

        # Start merge, remove conflict markers to reach deletion validation
        merge_result = subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature", "--no-edit"],
            capture_output=True, text=True, check=False,
        )
        assert "CONFLICT" in merge_result.stdout or "CONFLICT" in merge_result.stderr
        (tmp_path / "shared.txt").write_text("resolved content without markers\n")

        # Mock LLM: empty hunks list, LOW overall confidence → should reject deletion
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="",  # empty → deletion
                        hunks=[],  # no hunks → fall back to overall_confidence
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

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # Should fail — empty hunks + LOW overall → rejected (fast_abort in fast mode)
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "fast_abort"

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head


class TestStrictShortCircuit:
    """Tests for strict strategy short-circuit: skip LLM, direct human call."""

    def _create_conflict_repo(self, tmp_path: Path) -> tuple[str, str]:
        """Create repo with conflicting branches."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "line1\nline2\nline3\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("line1\nFEATURE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("line1\nBASE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on base"],
            check=True, capture_output=True,
        )
        return default_branch, "feature"

    def test_strict_skips_llm_direct_human_call(self, tmp_path: Path, monkeypatch) -> None:
        """strict mode must NOT call LLM; instead it creates a human call file."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        llm_call_count = 0

        def mock_resolve(self, context, strategy):
            nonlocal llm_call_count
            llm_call_count += 1
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="line1\nRESOLVED\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "merged both")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="strict")
        report = orch.execute([feature_branch])

        # LLM should NOT have been called
        assert llm_call_count == 0, f"Expected 0 LLM calls in strict mode, got {llm_call_count}"

        # Should be pending_human
        assert report.success is False
        assert report.pending_human is True
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "pending_human"

        # Call file should exist
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # Working tree should still have conflict markers (not aborted)
        content = (tmp_path / "shared.txt").read_text()
        assert "<<<<<<<" in content
        assert "=======" in content
        assert ">>>>>>>" in content

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # Log should mention strict short-circuit
        assert orch.log_file is not None
        log_text = orch.log_file.read_text()
        assert "skipping LLM resolution" in log_text.lower() or "strict" in log_text.lower()

    def test_strict_call_file_contains_placeholder_resolution(self, tmp_path: Path, monkeypatch) -> None:
        """strict mode call file must contain placeholder resolution data."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        orch = MergeOrchestrator(project_root=tmp_path, strategy="strict")
        report = orch.execute([feature_branch])

        assert report.human_call_file is not None
        call_data = json.loads(report.human_call_file.read_text())

        # Should be merge_conflict type
        assert call_data["type"] == "merge_conflict"

        # Should have file entries with working tree content as resolved_content
        assert len(call_data["files"]) == 1
        file_entry = call_data["files"][0]
        assert file_entry["path"] == "shared.txt"

        # LLM resolution should have LOW confidence (placeholder)
        assert call_data["llm_overall_confidence"] == "low"

        # Decision reason should mention strict
        assert "strict" in call_data["decision_reason"].lower()

    def test_strict_write_call_failure_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """If write_call raises in strict mode, the merge must abort."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_write_call(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.HumanCallWriter.write_call",
            mock_write_call,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="strict")
        report = orch.execute([feature_branch])

        # Must abort
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "human_call_write_failed"
        assert report.human_call_file is None

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD should be unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_strict_skips_llm_and_writes_call(self, tmp_path: Path, monkeypatch) -> None:
        """strict mode: LLMCaller.call never invoked; human call file created."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        llm_call_count = 0

        def mock_llm_call(self, prompt, require_json=False):
            nonlocal llm_call_count
            llm_call_count += 1
            return ""

        monkeypatch.setattr(
            "se3.engine.llm_caller.LLMCaller.call",
            mock_llm_call,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="strict")
        report = orch.execute([feature_branch])

        # LLM should NOT have been called at all
        assert llm_call_count == 0, (
            f"Expected 0 LLM calls in strict mode, got {llm_call_count}"
        )

        # Should be pending_human with call file
        assert report.success is False
        assert report.pending_human is True
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "pending_human"
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head


class TestFastAbortBehavior:
    """Tests for fast mode: human-escalation paths become abort+fail."""

    def _create_conflict_repo(self, tmp_path: Path) -> tuple[str, str]:
        """Create repo with conflicting branches."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _add_commit(tmp_path, "shared.txt", "line1\nline2\nline3\n", "Add shared")
        _create_branch(tmp_path, "feature")
        (tmp_path / "shared.txt").write_text("line1\nFEATURE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "shared.txt").write_text("line1\nBASE\nline3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "shared.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change shared on base"],
            check=True, capture_output=True,
        )
        return default_branch, "feature"

    def test_fast_human_call_decision_aborts_no_call_file(self, tmp_path: Path, monkeypatch) -> None:
        """fast + REJECT decision -> abort, no call file, no pending_human."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="line1\nRESOLVED\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "ok")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": True, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": True, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        calls_before = list((tmp_path / "se3" / "calls").glob("merge_*.json")) if (tmp_path / "se3" / "calls").exists() else []

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute([feature_branch])

        # Must fail with fast_abort, NOT pending_human
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "fast_abort"
        assert report.human_call_file is None

        # Working tree should be clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # No new call files should have been created
        calls_after = list((tmp_path / "se3" / "calls").glob("merge_*.json")) if (tmp_path / "se3" / "calls").exists() else []
        assert len(calls_after) == len(calls_before)

    def test_fast_incomplete_resolution_aborts_no_call_file(self, tmp_path: Path, monkeypatch) -> None:
        """fast + incomplete LLM resolution -> abort, no call file."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "a.txt", "a base\n", "Add a")
        _add_commit(tmp_path, "b.txt", "b base\n", "Add b")
        _create_branch(tmp_path, "feature")
        (tmp_path / "a.txt").write_text("a feature\n")
        (tmp_path / "b.txt").write_text("b feature\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "a.txt").write_text("a main\n")
        (tmp_path / "b.txt").write_text("b main\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on main"],
            check=True, capture_output=True,
        )

        # Mock LLM: only resolves a.txt, omits b.txt
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="a.txt",
                        resolved_content="a resolved\n",
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "merged a")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # Must fail with fast_abort
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "fast_abort"
        assert report.human_call_file is None

        # Working tree clean
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_fast_resolution_commit_timeout_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """fast + commit timeout after resolution -> fast_abort."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        resolved_content="line1\nRESOLVED\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "merged both")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        # Monkeypatch _run_git to make "commit" time out
        import subprocess as _sp

        def patch_run_git(project_root, *args, check=True, timeout=30):
            if len(args) >= 1 and args[0] == "commit":
                raise _sp.TimeoutExpired(cmd=["git", "commit"], timeout=30)
            import se3.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator._run_git", patch_run_git
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute([feature_branch])

        # Must fail with fast_abort
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "fast_abort"

        # Working tree clean after abort
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_fast_apply_resolution_validation_failure_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """fast + validation failure in _apply_resolution -> fast_abort."""
        default_branch, feature_branch = self._create_conflict_repo(tmp_path)

        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="shared.txt",
                        # Contains conflict markers -> validation failure
                        resolved_content="line1\n<<<<<<< HEAD\nBASE\n=======\nFEATURE\n>>>>>>> branch\nline3\n",
                        hunks=[HunkResolution(1, 5, Confidence.HIGH, "merged both")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute([feature_branch])

        # Must fail with fast_abort
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == feature_branch
        assert report.failure_reason == "fast_abort"

        # Working tree clean
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_default_incomplete_resolution_still_human_call(self, tmp_path: Path, monkeypatch) -> None:
        """default + incomplete resolution -> still writes human call (not fast_abort)."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "a.txt", "a base\n", "Add a")
        _add_commit(tmp_path, "b.txt", "b base\n", "Add b")
        _create_branch(tmp_path, "feature")
        (tmp_path / "a.txt").write_text("a feature\n")
        (tmp_path / "b.txt").write_text("b feature\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "a.txt").write_text("a main\n")
        (tmp_path / "b.txt").write_text("b main\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on main"],
            check=True, capture_output=True,
        )

        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="a.txt",
                        resolved_content="a resolved\n",
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "merged a")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        # default mode should still create human call
        assert report.success is False
        assert report.pending_human is True
        assert report.failure_reason == "pending_human"
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

    def test_fast_incomplete_llm_resolution_aborts_no_call(self, tmp_path: Path, monkeypatch) -> None:
        """fast + incomplete LLM resolution -> abort, no call file created."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)

        _add_commit(tmp_path, "a.txt", "a base\n", "Add a")
        _add_commit(tmp_path, "b.txt", "b base\n", "Add b")
        _create_branch(tmp_path, "feature")
        (tmp_path / "a.txt").write_text("a feature\n")
        (tmp_path / "b.txt").write_text("b feature\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on feature"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )
        (tmp_path / "a.txt").write_text("a main\n")
        (tmp_path / "b.txt").write_text("b main\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "a.txt", "b.txt"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "Change both on main"],
            check=True, capture_output=True,
        )

        # Mock LLM: only resolves a.txt, omits b.txt
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="a.txt",
                        resolved_content="a resolved\n",
                        hunks=[HunkResolution(1, 3, Confidence.HIGH, "merged a")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=False,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        calls_before = list((tmp_path / "se3" / "calls").glob("merge_*.json")) if (tmp_path / "se3" / "calls").exists() else []

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # Must fail with fast_abort
        assert report.success is False
        assert report.pending_human is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "fast_abort"
        assert report.human_call_file is None

        # Working tree clean
        assert _is_working_tree_clean(tmp_path) is True

        # HEAD unchanged
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # No new call files
        calls_after = list((tmp_path / "se3" / "calls").glob("merge_*.json")) if (tmp_path / "se3" / "calls").exists() else []
        assert len(calls_after) == len(calls_before)


class TestGuardrailsStrategyAware:
    """Tests for strategy-aware _run_guardrails behavior."""

    def _setup_spec_repo(self, tmp_path: Path) -> str:
        """Init repo with a spec file. Returns default branch name."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHALL validate all user inputs.\n"
        )
        (tmp_path / "code.py").write_text("def auth(): pass\n")
        _commit(tmp_path, "initial")
        return default_branch

    def test_fast_guardrail_violation_llm_repair_success(self, tmp_path: Path, monkeypatch) -> None:
        """fast + SHALL->SHOULD guardrail violation -> LLM repair -> merge succeeds."""
        default_branch = self._setup_spec_repo(tmp_path)

        # Create feature branch that weakens spec
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True, capture_output=True,
        )
        spec_dir = tmp_path / "se3" / "specs" / "base"
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "weaken spec"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Mock GuardrailRepairer to succeed
        def mock_repair(self, branch, pre_sha, post_sha, violations, original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(success=True, repaired_files=["se3/specs/base/spec.md"])

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock check_merge_result to pass (since repairer is mocked)
        def mock_check_merge_result(self, pre_sha: str, post_sha: str):
            from se3.engine.merge.guardrails import GuardrailReport
            return GuardrailReport(passed=True, violations=[])

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check_merge_result,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # Should succeed because repairer fixed the violation
        assert report.success is True, f"Expected success, got failure_reason={report.failure_reason}"
        assert "feature" in report.merged_branches

        # HEAD should have changed (merge commit created)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head != pre_head

    def test_fast_guardrail_violation_llm_repair_failure_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """fast + guardrail violation + LLM repair fails -> rollback + fast_abort."""
        default_branch = self._setup_spec_repo(tmp_path)

        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True, capture_output=True,
        )
        spec_dir = tmp_path / "se3" / "specs" / "base"
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "weaken spec"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Mock GuardrailRepairer to fail
        def mock_repair(self, branch, pre_sha, post_sha, violations, original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=False,
                error="LLM could not fix the weakening",
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # Must fail
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "guardrail_repair_failed"
        assert report.pending_human is False
        assert report.human_call_file is None

        # HEAD should be restored to pre-merge state
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # No call files should exist
        calls_dir = tmp_path / "se3" / "calls"
        if calls_dir.exists():
            call_files = list(calls_dir.glob("merge_*.json"))
            assert len(call_files) == 0

    def test_default_guardrail_violation_unchanged(self, tmp_path: Path) -> None:
        """default + guardrail violation -> still rollback + human call (unchanged)."""
        default_branch = self._setup_spec_repo(tmp_path)

        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True, capture_output=True,
        )
        spec_dir = tmp_path / "se3" / "specs" / "base"
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "weaken spec"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature"])

        # Should fail with guardrail_violation and human call
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "guardrail_violation"
        assert report.pending_human is True
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # HEAD should be restored
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

    def test_fast_clean_merge_guardrail_violation_repair(self, tmp_path: Path, monkeypatch) -> None:
        """fast + clean merge that touches spec + guardrail violation -> LLM repair."""
        default_branch = self._setup_spec_repo(tmp_path)

        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True, capture_output=True,
        )
        spec_dir = tmp_path / "se3" / "specs" / "base"
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "weaken spec"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Mock repairer to succeed
        def mock_repair(self, branch, pre_sha, post_sha, violations, original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(success=True, repaired_files=["se3/specs/base/spec.md"])

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock check_merge_result to pass (since repairer is mocked)
        def mock_check_merge_result(self, pre_sha: str, post_sha: str):
            from se3.engine.merge.guardrails import GuardrailReport
            return GuardrailReport(passed=True, violations=[])

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check_merge_result,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        assert report.success is True
        assert "feature" in report.merged_branches

    def test_fast_guardrail_repair_missing_sha_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """fast + missing pre_sha/post_sha in guardrails -> GuardrailRepairFailed."""
        _init_repo(tmp_path)
        default_branch = _get_default_branch(tmp_path)
        _create_branch(tmp_path, "feature")
        _add_commit(tmp_path, "feat.txt", "feature", "Add feature")
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        # Patch rev-parse HEAD to return empty on the second call (inside _merge_single_branch)
        call_count = 0

        def patch_rev_parse(project_root, *args, check=True, timeout=30):
            nonlocal call_count
            call_count += 1
            if call_count == 2 and len(args) >= 2 and args[0] == "rev-parse" and args[1] == "HEAD":
                class FakeResult:
                    returncode = 1
                    stdout = ""
                    stderr = "mock failure"
                return FakeResult()
            import se3.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator._run_git", patch_rev_parse
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # Missing SHA in fast mode -> GuardrailRepairFailed -> fast_abort
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "guardrail_repair_failed"
        assert report.pending_human is False
        assert report.human_call_file is None

    def test_fast_repairs_guardrail_violation(self, tmp_path: Path, monkeypatch) -> None:
        """fast + SHALL->SHOULD: mock repairer's LLM to return fix; merge succeeds with amend."""
        default_branch = self._setup_spec_repo(tmp_path)

        # Create feature branch that weakens spec
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True, capture_output=True,
        )
        spec_dir = tmp_path / "se3" / "specs" / "base"
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "weaken spec"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Mock GuardrailRepairer's LLM call to return corrected content
        def mock_call_llm(self, prompt):
            import json
            return json.dumps({
                "files": [{
                    "path": "se3/specs/base/spec.md",
                    "corrected_content": (
                        "## Requirement: Auth\n\n"
                        "The system SHALL validate all user inputs.\n"
                    ),
                }]
            }, ensure_ascii=False, indent=2)

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer._call_llm",
            mock_call_llm,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # Should succeed because repairer fixed the violation
        assert report.success is True, (
            f"Expected success, got failure_reason={report.failure_reason}"
        )
        assert "feature" in report.merged_branches
        assert report.pending_human is False
        assert report.human_call_file is None

        # HEAD should have changed (merge commit created)
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head != pre_head

        # Spec should have been repaired (SHALL restored)
        spec_content = (spec_dir / "spec.md").read_text()
        assert "SHALL" in spec_content
        assert "SHOULD" not in spec_content

    def test_fast_repair_failure_aborts_no_call(self, tmp_path: Path, monkeypatch) -> None:
        """fast + guardrail violation + repair fails -> abort, no human call file."""
        default_branch = self._setup_spec_repo(tmp_path)

        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True, capture_output=True,
        )
        spec_dir = tmp_path / "se3" / "specs" / "base"
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "weaken spec"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default_branch],
            check=True, capture_output=True,
        )

        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Count call files before
        calls_dir = tmp_path / "se3" / "calls"
        calls_before = list(calls_dir.glob("merge_*.json")) if calls_dir.exists() else []

        # Mock repairer to fail
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(success=False, error="LLM could not fix")

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature"])

        # Must fail with guardrail_repair_failed
        assert report.success is False
        assert report.failed_branch == "feature"
        assert report.failure_reason == "guardrail_repair_failed"
        assert report.pending_human is False
        assert report.human_call_file is None

        # HEAD restored
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head

        # No new call files
        calls_after = list(calls_dir.glob("merge_*.json")) if calls_dir.exists() else []
        assert len(calls_after) == len(calls_before)
