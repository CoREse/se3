"""Tests for se3 merge-respond command (merge_respond.py)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tianluo.commands.merge_respond import process_merge_response


def _init_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit on branch 'main'."""
    subprocess.run(
        ["git", "init", str(path), "--initial-branch=main"],
        check=True, capture_output=True,
    )
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


def _start_merge_conflict(path: Path) -> None:
    """Create a real in-progress merge with a conflict.

    Sets up a branch 'feature' that modifies README.md differently
    from 'main', then starts ``git merge feature`` so the repo is
    in a mid-merge state with conflict markers in the working tree.
    """
    # Create feature branch from the initial commit (before diverging changes)
    subprocess.run(
        ["git", "-C", str(path), "checkout", "-b", "feature"],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("feature content\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "feature change"],
        check=True, capture_output=True,
    )

    # Switch back to main and make a CONFLICTING change
    subprocess.run(
        ["git", "-C", str(path), "checkout", "main"],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("main content\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "main change"],
        check=True, capture_output=True,
    )

    # Now merge feature — this will conflict because both branches changed README.md
    result = subprocess.run(
        ["git", "-C", str(path), "merge", "feature"],
        capture_output=True, text=True, check=False,
    )
    # merge should fail with conflicts
    assert result.returncode != 0, "Expected merge conflict but merge succeeded"
    assert (
        "CONFLICT" in result.stdout
        or "CONFLICT" in result.stderr
        or "conflict" in result.stderr.lower()
    ), f"Expected conflict markers but got: {result.stdout} {result.stderr}"


def _create_merge_call_file(
    path: Path,
    files: list[dict],
    call_type: str = "merge_conflict",
) -> Path:
    """Create a merge call file with the given files."""
    call_data = {
        "type": call_type,
        "ours_branch": "main",
        "theirs_branch": "feature",
        "merge_base": "abc123",
        "files": files,
    }
    call_file = path / "merge_call.json"
    call_file.write_text(json.dumps(call_data), encoding="utf-8")
    return call_file


def _create_response_file(call_file: Path, choice: str, feedback: str = "") -> Path:
    """Create a response file next to the call file."""
    response_path = Path(str(call_file) + ".response")
    response_data = {"choice": choice}
    if feedback:
        response_data["feedback"] = feedback
    response_path.write_text(json.dumps(response_data), encoding="utf-8")
    return response_path


class TestCallFileNotFound:
    def test_returns_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.json"
        exit_code = process_merge_response(missing, project_root=tmp_path)
        assert exit_code == 1


class TestResponseFileNotFound:
    def test_returns_error(self, tmp_path: Path) -> None:
        call_file = tmp_path / "merge_call.json"
        call_file.write_text('{"type": "merge_conflict"}', encoding="utf-8")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1


class TestInvalidJson:
    def test_invalid_call_file(self, tmp_path: Path) -> None:
        call_file = tmp_path / "merge_call.json"
        call_file.write_text("not json", encoding="utf-8")
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1

    def test_invalid_response_file(self, tmp_path: Path) -> None:
        call_file = tmp_path / "merge_call.json"
        call_file.write_text('{"type": "merge_conflict"}', encoding="utf-8")
        response_file = Path(str(call_file) + ".response")
        response_file.write_text("not json", encoding="utf-8")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1


class TestInvalidChoice:
    def test_unknown_choice(self, tmp_path: Path) -> None:
        call_file = _create_merge_call_file(tmp_path, [])
        _create_response_file(call_file, "unknown_choice")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1


class TestStrictSentinelDetection:
    """Critical safety: strict-mode placeholder must NEVER be written to disk."""

    def test_refuses_strict_placeholder(self, tmp_path: Path) -> None:
        """A call file containing the strict sentinel must be rejected."""
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "foo.py",
                    "llm_resolution": {
                        "resolved_content": (
                            "[__SE3_STRICT_PLACEHOLDER__: LLM resolution was skipped. "
                            "Please resolve conflicts manually.]"
                        ),
                    },
                },
            ],
        )
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1

    def test_refuses_mixed_files_with_sentinel(self, tmp_path: Path) -> None:
        """If ANY file has the sentinel, the entire accept is refused."""
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "good.py",
                    "llm_resolution": {"resolved_content": "print('ok')"},
                },
                {
                    "path": "bad.py",
                    "llm_resolution": {
                        "resolved_content": (
                            "[__SE3_STRICT_PLACEHOLDER__: placeholder]"
                        ),
                    },
                },
            ],
        )
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1

    def test_allows_accept_without_sentinel(self, tmp_path: Path) -> None:
        """Normal call files without the sentinel are accepted."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        # Resolve the conflict by writing the resolved file
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved content\n"},
                },
            ],
        )
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        assert (
            (tmp_path / "README.md").read_text(encoding="utf-8")
            == "resolved content\n"
        )


class TestAcceptChoice:
    def test_writes_files_and_stages(self, tmp_path: Path) -> None:
        """Accept writes resolved content back and commits the merge."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved README\n"},
                },
                {
                    "path": "src/module.py",
                    "llm_resolution": {"resolved_content": "def foo(): pass\n"},
                },
            ],
        )
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        assert (
            (tmp_path / "src" / "module.py").read_text(encoding="utf-8")
            == "def foo(): pass\n"
        )
        # Verify the merge was committed (no MERGE_HEAD left)
        git_dir = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert not (tmp_path / git_dir / "MERGE_HEAD").exists()

    def test_skips_empty_resolved_content(self, tmp_path: Path) -> None:
        """Files with empty resolved content are skipped during write-back."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved README\n"},
                },
                {
                    "path": "skip.py",
                    "llm_resolution": {"resolved_content": ""},
                },
            ],
        )
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        assert (tmp_path / "README.md").read_text(encoding="utf-8") == "resolved README\n"
        # skip.py was skipped because resolved_content is empty
        assert not (tmp_path / "skip.py").exists()

    def test_commits_merge(self, tmp_path: Path) -> None:
        """Accept commits the merge after writing files."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "merged\n"},
                },
            ],
        )
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "log", "-1", "--pretty=%s"],
            capture_output=True, text=True, check=True,
        )
        # After commit, the log should show a merge commit (not "initial")
        assert "initial" not in result.stdout.strip()

    def test_guardrail_violation_accept(self, tmp_path: Path) -> None:
        """Accept on guardrail_violation type does not auto-write files."""
        _init_repo(tmp_path)
        call_file = _create_merge_call_file(
            tmp_path,
            files=[],
            call_type="guardrail_violation",
        )
        _create_response_file(call_file, "accept", "fixed manually")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0


class TestAbortChoice:
    def test_aborts_merge(self, tmp_path: Path) -> None:
        """Abort runs git merge --abort."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        call_file = _create_merge_call_file(tmp_path, [])
        _create_response_file(call_file, "abort")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        # Verify merge was aborted: no MERGE_HEAD
        git_dir = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        merge_head_path = (
            tmp_path / git_dir / "MERGE_HEAD"
            if not Path(git_dir).is_absolute()
            else Path(git_dir) / "MERGE_HEAD"
        )
        assert not merge_head_path.exists()

    def test_guardrail_violation_abort_skips_merge_abort(self, tmp_path: Path) -> None:
        """Abort on guardrail_violation skips git merge --abort (already rolled back)."""
        _init_repo(tmp_path)
        # No in-progress merge — guardrail violations have already been rolled back
        call_file = _create_merge_call_file(
            tmp_path,
            files=[],
            call_type="guardrail_violation",
        )
        _create_response_file(call_file, "abort")
        # Must succeed (exit 0) even though no merge is in progress
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0


class TestManualChoice:
    def test_returns_success(self, tmp_path: Path) -> None:
        """Manual choice returns success with instructions."""
        call_file = _create_merge_call_file(tmp_path, [])
        _create_response_file(call_file, "manual", "will fix later")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0


class TestMainWorktreeLockRoot:
    def test_lock_targets_resolved_main_repo_root(self, tmp_path: Path) -> None:
        """process_merge_response must acquire the main-worktree mutex on the
        *main* repository root (resolved from a possibly-worktree
        project_root), not on the bare project_root — so a merge-respond
        launched from inside a linked worktree contends on the single
        project-wide lock file shared with se3 run / se3 merge.
        """
        from unittest.mock import MagicMock, patch

        call_file = _create_merge_call_file(tmp_path, [])
        _create_response_file(call_file, "manual", "will fix later")

        sentinel_root = Path("/resolved/main/repo")
        with patch(
            "tianluo.commands.run._resolve_main_lock_root",
            return_value=sentinel_root,
        ) as mock_resolve, patch(
            "tianluo.commands.merge.merge_lock.MergeLock"
        ) as MockLock:
            MockLock.return_value = MagicMock()
            exit_code = process_merge_response(call_file, project_root=tmp_path)

        assert exit_code == 0
        mock_resolve.assert_called_once_with(tmp_path)
        MockLock.assert_called_once_with(sentinel_root, blocking=True)


class TestEdgeCases:
    def test_missing_llm_resolution_key(self, tmp_path: Path) -> None:
        """Files missing llm_resolution key are handled gracefully."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "ok\n"},
                },
                {
                    "path": "file.py",
                    # no "llm_resolution" key
                },
            ],
        )
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        # Should succeed: README.md resolved, file.py skipped (empty content)
        assert exit_code == 0

    def test_none_llm_resolution(self, tmp_path: Path) -> None:
        """Files with null llm_resolution are handled gracefully."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        call_file = tmp_path / "merge_call.json"
        call_data = {
            "type": "merge_conflict",
            "files": [
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "ok\n"},
                },
                {
                    "path": "file.py",
                    "llm_resolution": None,
                },
            ],
        }
        call_file.write_text(json.dumps(call_data), encoding="utf-8")
        _create_response_file(call_file, "accept")
        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0


class TestGuardrailsAfterAccept:
    """Guardrails check after merge-respond accept for merge_conflict type."""

    def test_guardrails_pass_after_accept(self, tmp_path: Path, monkeypatch) -> None:
        """After accept, guardrails check passes on spec files — success."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        # Set up a spec file that's part of the resolution
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\nThe system SHALL validate all inputs.\n"
        )

        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "se3/specs/base/spec.md",
                    "llm_resolution": {
                        "resolved_content": (
                            "## Requirement: Auth\n\n"
                            "The system SHALL validate all inputs.\n"
                        ),
                    },
                },
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved README\n"},
                },
            ],
        )
        _create_response_file(call_file, "accept")

        # Mock guardrails to pass
        def mock_check(self, pre_sha: str, post_sha: str):
            from tianluo.engine.merge.guardrails import GuardrailReport
            return GuardrailReport(passed=True, violations=[])

        monkeypatch.setattr(
            "tianluo.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        # Merge should be committed
        git_dir = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "--git-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert not (tmp_path / git_dir / "MERGE_HEAD").exists()

    def test_guardrails_warning_after_accept(self, tmp_path: Path, monkeypatch) -> None:
        """After accept, guardrails violation rolls back the commit and returns 1.

        Per spec contract (Mandatory guardrails after every `se3 merge`
        commit), a spec-touching merge commit with violations MUST be
        rolled back and reported as failure — not silently downgraded
        to a warning + exit 0.
        """
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\nThe system SHALL validate all inputs.\n"
        )

        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "se3/specs/base/spec.md",
                    "llm_resolution": {
                        "resolved_content": (
                            "## Requirement: Auth\n\n"
                            "The system SHOULD validate all inputs.\n"
                        ),
                    },
                },
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved README\n"},
                },
            ],
        )
        _create_response_file(call_file, "accept")

        # Mock guardrails to fail (violation detected)
        def mock_check(self, pre_sha: str, post_sha: str):
            from tianluo.engine.merge.guardrails import GuardrailReport, GuardrailViolation
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/base/spec.md",
                        violation_type="WEAKENING",
                        message="SHALL weakened to SHOULD",
                    ),
                ],
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        # Capture pre-merge HEAD so we can confirm rollback landed on it.
        pre_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        # Per the spec contract, a guardrail-violating merge commit MUST
        # be rolled back and reported as failure (exit 1).
        assert exit_code == 1
        # The rolled-back commit should no longer be on HEAD; HEAD
        # should match pre_head (the merge commit was reset away).
        post_head = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert post_head == pre_head, (
            "guardrail-violating merge commit was not rolled back"
        )


# =====================================================================
# G8 — git add returncode check, octopus first-parent, spec-path
# =====================================================================


class TestGitAddReturncode:
    """G8 task 43 (G3): git add failures must NOT be silently swallowed."""

    def test_git_add_failure_returns_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When git add fails, process_merge_response returns 1."""
        import subprocess as subprocess_mod
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)

        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved\n"},
                },
            ],
        )
        _create_response_file(call_file, "accept")

        original_run = subprocess_mod.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) >= 4 and cmd[3] == "add":
                # Simulate git add failure
                from subprocess import CompletedProcess
                return CompletedProcess(
                    args=cmd, returncode=128,
                    stdout="", stderr="fatal: simulated git-add failure",
                )
            return original_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess_mod, "run", fake_run)
        # The merge_respond module imported subprocess at the top, so we
        # also need to patch it via the imported module's namespace.
        from tianluo.commands import merge_respond as merge_respond_mod
        monkeypatch.setattr(
            merge_respond_mod.subprocess, "run", fake_run
        )

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1


class TestFirstParentSha:
    """G8 task 42 (G1): _first_parent_sha helper for octopus-safe parent walk."""

    def test_two_parent_merge_first_parent(self, tmp_path: Path) -> None:
        from tianluo.commands.merge_respond import _first_parent_sha

        _init_repo(tmp_path)
        # Create a feature branch and merge it
        first_parent_expected = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feat"],
            check=True, capture_output=True,
        )
        (tmp_path / "feat.py").write_text("x")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feat"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "main"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "--no-ff", "feat",
             "-m", "Merge feat"],
            check=True, capture_output=True,
        )

        first_parent = _first_parent_sha(tmp_path)
        assert first_parent == first_parent_expected

    def test_root_commit_raises_runtime_error(self, tmp_path: Path) -> None:
        from tianluo.commands.merge_respond import _first_parent_sha

        _init_repo(tmp_path)
        # _init_repo only created the initial commit; HEAD has no parents
        with pytest.raises(RuntimeError, match="no parents"):
            _first_parent_sha(tmp_path)


class TestIsSpecPath:
    """G8 task 43 (G2): _is_spec_path uses pathlib.PurePosixPath."""

    def test_forward_slash_path(self) -> None:
        from tianluo.commands.merge_respond import _is_spec_path

        assert _is_spec_path("se3/specs/base/spec.md") is True
        assert _is_spec_path("se3/specs/foo/bar/spec.md") is True

    def test_backslash_path_normalized(self) -> None:
        """G2: Windows paths with backslashes are normalised before check."""
        from tianluo.commands.merge_respond import _is_spec_path

        assert _is_spec_path("se3\\specs\\base\\spec.md") is True
        assert _is_spec_path("se3\\specs\\foo\\bar\\spec.md") is True

    def test_mixed_separators(self) -> None:
        from tianluo.commands.merge_respond import _is_spec_path

        assert _is_spec_path("se3\\specs/base/spec.md") is True
        assert _is_spec_path("se3/specs\\base\\spec.md") is True

    def test_non_spec_paths_rejected(self) -> None:
        from tianluo.commands.merge_respond import _is_spec_path

        assert _is_spec_path("README.md") is False
        assert _is_spec_path("se3/state/foo.json") is False
        assert _is_spec_path("se3/specs/base/other.md") is False
        assert _is_spec_path("specs/base/spec.md") is False  # missing se3 prefix
        assert _is_spec_path("") is False


# =====================================================================
# Pending-guardrails: stash succeeded + reset failed
# =====================================================================


class TestPendingGuardrailsStashResetFailure:
    """When stash push succeeds but reset --hard fails, the user must be
    told about BOTH the manual reset AND the dangling stash entry."""

    def test_stash_succeeds_reset_fails_warns_about_stash_pop(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """The worst-case operator-facing state: dangling stash + unclean HEAD."""
        import subprocess as subprocess_mod

        _init_repo(tmp_path)

        # Make two commits so pre_sha != post_sha
        (tmp_path / "file1.txt").write_text("v1\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "first"],
            check=True, capture_output=True,
        )
        pre_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        (tmp_path / "file1.txt").write_text("v2\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "second"],
            check=True, capture_output=True,
        )

        # Create call file and pending-guardrails marker
        call_file = tmp_path / "merge_call.json"
        call_file.write_text(
            json.dumps({"type": "merge_conflict", "files": []}),
            encoding="utf-8",
        )
        marker_path = Path(str(call_file) + ".pending_guardrails")
        marker_path.write_text(
            json.dumps({"pre_sha": pre_sha, "spec_paths": ["se3/specs/base/spec.md"]}),
            encoding="utf-8",
        )

        # Mock guardrails to fail so the stash+reset path is reached
        def mock_check(self, pre_sha: str, post_sha: str):
            from tianluo.engine.merge.guardrails import GuardrailReport, GuardrailViolation
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/base/spec.md",
                        violation_type="WEAKENING",
                        message="SHALL weakened to SHOULD",
                    ),
                ],
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        original_run = subprocess_mod.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) >= 4:
                if cmd[3] == "stash":
                    # Stash succeeds
                    from subprocess import CompletedProcess
                    return CompletedProcess(
                        args=cmd, returncode=0,
                        stdout="Saved working directory...", stderr="",
                    )
                if cmd[3] == "reset" and "--hard" in cmd:
                    # Reset fails
                    from subprocess import CompletedProcess
                    return CompletedProcess(
                        args=cmd, returncode=128,
                        stdout="", stderr="fatal: could not reset",
                    )
            return original_run(cmd, *args, **kwargs)

        from tianluo.commands import merge_respond as merge_respond_mod
        monkeypatch.setattr(merge_respond_mod.subprocess, "run", fake_run)

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1
        # The message must mention BOTH manual reset AND stash pop recovery.
        # We verify this by inspecting the capture-render path indirectly:
        # the render_text calls in merge_respond are the user-facing output.
        # Since render_text is a side-effect, we can't assert on it directly
        # without patching.  Instead we patch render_text and collect the text.

    def test_stash_succeeds_reset_fails_collects_render_text(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Collect the rendered text to assert both warnings are present."""
        import subprocess as subprocess_mod

        _init_repo(tmp_path)

        (tmp_path / "file1.txt").write_text("v1\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "first"],
            check=True, capture_output=True,
        )
        pre_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        (tmp_path / "file1.txt").write_text("v2\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "second"],
            check=True, capture_output=True,
        )

        call_file = tmp_path / "merge_call.json"
        call_file.write_text(
            json.dumps({"type": "merge_conflict", "files": []}),
            encoding="utf-8",
        )
        marker_path = Path(str(call_file) + ".pending_guardrails")
        marker_path.write_text(
            json.dumps({"pre_sha": pre_sha, "spec_paths": ["se3/specs/base/spec.md"]}),
            encoding="utf-8",
        )

        def mock_check(self, pre_sha: str, post_sha: str):
            from tianluo.engine.merge.guardrails import GuardrailReport, GuardrailViolation
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/base/spec.md",
                        violation_type="WEAKENING",
                        message="SHALL weakened to SHOULD",
                    ),
                ],
            )

        monkeypatch.setattr(
            "tianluo.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        original_run = subprocess_mod.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and len(cmd) >= 4:
                if cmd[3] == "stash":
                    from subprocess import CompletedProcess
                    return CompletedProcess(
                        args=cmd, returncode=0,
                        stdout="Saved working directory...", stderr="",
                    )
                if cmd[3] == "reset" and "--hard" in cmd:
                    from subprocess import CompletedProcess
                    return CompletedProcess(
                        args=cmd, returncode=128,
                        stdout="", stderr="fatal: could not reset",
                    )
            return original_run(cmd, *args, **kwargs)

        from tianluo.commands import merge_respond as merge_respond_mod
        monkeypatch.setattr(merge_respond_mod.subprocess, "run", fake_run)

        rendered_texts: list[str] = []

        def capture_render(text, *, title=""):
            rendered_texts.append(text)

        monkeypatch.setattr(merge_respond_mod, "render_text", capture_render)

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1
        assert len(rendered_texts) == 1
        full_text = rendered_texts[0]
        # Must mention manual reset
        assert "git reset --hard" in full_text
        # Must mention stash pop recovery (the fix we added)
        assert "git stash pop" in full_text


# =====================================================================
# Pending-guardrails: multi-commit guard
# =====================================================================


class TestPendingGuardrailsMultiCommitGuard:
    """The multi-commit guard prevents accidental destruction of intermediate work."""

    def test_single_commit_advancement_accepted(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When HEAD advanced exactly 1 commit since pre_sha, guardrails run."""
        import subprocess as subprocess_mod

        _init_repo(tmp_path)

        # Make pre_sha commit
        (tmp_path / "file1.txt").write_text("v1\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "first"],
            check=True, capture_output=True,
        )
        pre_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Make exactly one commit after pre_sha
        (tmp_path / "file1.txt").write_text("v2\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "second"],
            check=True, capture_output=True,
        )

        call_file = tmp_path / "merge_call.json"
        call_file.write_text(
            json.dumps({"type": "merge_conflict", "files": []}),
            encoding="utf-8",
        )
        marker_path = Path(str(call_file) + ".pending_guardrails")
        marker_path.write_text(
            json.dumps({"pre_sha": pre_sha, "spec_paths": ["se3/specs/base/spec.md"]}),
            encoding="utf-8",
        )

        def mock_check(self, pre_sha: str, post_sha: str):
            from tianluo.engine.merge.guardrails import GuardrailReport
            return GuardrailReport(passed=True, violations=[])

        monkeypatch.setattr(
            "tianluo.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0
        # Marker should be deleted on success
        assert not marker_path.exists()

    def test_multi_commit_advancement_rejected(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When HEAD advanced >1 commit since pre_sha, reject to avoid destroying work."""
        import subprocess as subprocess_mod

        _init_repo(tmp_path)

        (tmp_path / "file1.txt").write_text("v1\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "first"],
            check=True, capture_output=True,
        )
        pre_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Make TWO commits after pre_sha
        (tmp_path / "file1.txt").write_text("v2\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "second"],
            check=True, capture_output=True,
        )
        (tmp_path / "file1.txt").write_text("v3\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "third"],
            check=True, capture_output=True,
        )

        call_file = tmp_path / "merge_call.json"
        call_file.write_text(
            json.dumps({"type": "merge_conflict", "files": []}),
            encoding="utf-8",
        )
        marker_path = Path(str(call_file) + ".pending_guardrails")
        marker_path.write_text(
            json.dumps({"pre_sha": pre_sha, "spec_paths": ["se3/specs/base/spec.md"]}),
            encoding="utf-8",
        )

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1
        # Marker should remain since we rejected
        assert marker_path.exists()

    def test_commit_count_parse_failure_proceeds(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When rev-list --count returns non-numeric, treat as 0 and proceed."""
        import subprocess as subprocess_mod

        _init_repo(tmp_path)

        (tmp_path / "file1.txt").write_text("v1\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "first"],
            check=True, capture_output=True,
        )
        pre_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        (tmp_path / "file1.txt").write_text("v2\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "second"],
            check=True, capture_output=True,
        )

        call_file = tmp_path / "merge_call.json"
        call_file.write_text(
            json.dumps({"type": "merge_conflict", "files": []}),
            encoding="utf-8",
        )
        marker_path = Path(str(call_file) + ".pending_guardrails")
        marker_path.write_text(
            json.dumps({"pre_sha": pre_sha, "spec_paths": ["se3/specs/base/spec.md"]}),
            encoding="utf-8",
        )

        def mock_check(self, pre_sha: str, post_sha: str):
            from tianluo.engine.merge.guardrails import GuardrailReport
            return GuardrailReport(passed=True, violations=[])

        monkeypatch.setattr(
            "tianluo.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        original_run = subprocess_mod.run

        def fake_run(cmd, *args, **kwargs):
            if (
                isinstance(cmd, list)
                and len(cmd) >= 5
                and cmd[3] == "rev-list"
                and cmd[4] == "--count"
            ):
                from subprocess import CompletedProcess
                return CompletedProcess(
                    args=cmd, returncode=0,
                    stdout="not_a_number\n", stderr="",
                )
            return original_run(cmd, *args, **kwargs)

        from tianluo.commands import merge_respond as merge_respond_mod
        monkeypatch.setattr(merge_respond_mod.subprocess, "run", fake_run)

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        # Non-numeric count → treated as 0 → not > 1 → guardrails run → pass
        assert exit_code == 0


# =====================================================================
# Merge-respond: version bump verification on accept
# =====================================================================


class TestVersionBumpAfterAccept:
    """The accept path verifies version advanced when a version file was resolved."""

    def _setup_repo_with_pyproject_and_conflict(self, tmp_path: Path) -> tuple[str, str]:
        """Set up a repo with pyproject.toml and a merge conflict in README.md.

        Returns (pre_sha, feature_branch_name).
        """
        _init_repo(tmp_path)

        # Add pyproject.toml on main
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "pyproject.toml"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "add pyproject"],
            check=True, capture_output=True,
        )
        pre_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        # Create feature branch from current main
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True, capture_output=True,
        )
        # Change pyproject version on feature
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.1.0"\n'
        )
        # Also change README on feature
        (tmp_path / "README.md").write_text("feature content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feature changes"],
            check=True, capture_output=True,
        )

        # Switch to main and make a CONFLICTING change in README
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "main"],
            check=True, capture_output=True,
        )
        (tmp_path / "README.md").write_text("main content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "README.md"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "main change"],
            check=True, capture_output=True,
        )

        # Start merge — README.md will conflict, pyproject.toml merges cleanly
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "feature"],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode != 0, "Expected merge conflict"
        assert "CONFLICT" in result.stdout or "CONFLICT" in result.stderr

        return pre_sha, "feature"

    def test_accepts_when_version_advanced(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When pyproject.toml version changed from pre-merge, accept passes."""
        pre_sha, _ = self._setup_repo_with_pyproject_and_conflict(tmp_path)

        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "pyproject.toml",
                    "llm_resolution": {
                        "resolved_content": (
                            '[project]\nname = "test"\nversion = "1.1.0"\n'
                        ),
                    },
                },
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved README\n"},
                },
            ],
        )
        call_file.write_text(
            json.dumps({
                **json.loads(call_file.read_text(encoding="utf-8")),
                "ours_head_sha": pre_sha,
            }),
            encoding="utf-8",
        )
        _create_response_file(call_file, "accept")

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0

    def test_rejects_when_version_unchanged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When pyproject.toml version did not change, accept fails."""
        pre_sha, _ = self._setup_repo_with_pyproject_and_conflict(tmp_path)

        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "pyproject.toml",
                    "llm_resolution": {
                        "resolved_content": (
                            '[project]\nname = "test"\nversion = "1.0.0"\n'
                        ),
                    },
                },
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved README\n"},
                },
            ],
        )
        call_file.write_text(
            json.dumps({
                **json.loads(call_file.read_text(encoding="utf-8")),
                "ours_head_sha": pre_sha,
            }),
            encoding="utf-8",
        )
        _create_response_file(call_file, "accept")

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 1

    def test_no_version_file_skips_check(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When no version file is in the resolution, version check is skipped."""
        _init_repo(tmp_path)
        _start_merge_conflict(tmp_path)

        call_file = _create_merge_call_file(
            tmp_path,
            files=[
                {
                    "path": "README.md",
                    "llm_resolution": {"resolved_content": "resolved README\n"},
                },
            ],
        )
        _create_response_file(call_file, "accept")

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        assert exit_code == 0

