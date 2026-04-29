"""Tests for se3 merge-respond command (merge_respond.py)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from se3.commands.merge_respond import process_merge_response


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
            from se3.engine.merge.guardrails import GuardrailReport
            return GuardrailReport(passed=True, violations=[])

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
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
        """After accept, guardrails finds violations — warn but return 0."""
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
            from se3.engine.merge.guardrails import GuardrailReport, GuardrailViolation
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
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        exit_code = process_merge_response(call_file, project_root=tmp_path)
        # Should return 0 (warning, not fatal)
        assert exit_code == 0
