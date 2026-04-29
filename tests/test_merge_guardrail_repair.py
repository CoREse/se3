"""Tests for GuardrailRepairer.

Covers:
- Successful LLM repair that passes guardrails
- LLM repair still contains violations (guardrails re-check fails)
- JSON parse failure from LLM
- LLM call raises exception
- Attempt to write non-spec path is rejected

All tests mock LLMCaller to avoid real subprocess calls.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from se3.engine.merge.guardrail_repair import GuardrailRepairer, RepairResult
from se3.engine.merge.guardrails import GuardrailViolation, GuardrailReport


# --------- helpers ---------


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


def _make_violations() -> list[GuardrailViolation]:
    return [
        GuardrailViolation(
            file_path="se3/specs/base/spec.md",
            violation_type="WEAKENING",
            message="SHALL weakened to SHOULD",
        ),
    ]


def _make_original_specs() -> dict[str, str]:
    return {
        "se3/specs/base/spec.md": (
            "## Requirement: Auth\n\n"
            "The system SHALL validate all user inputs.\n"
        ),
    }


def _make_merged_specs() -> dict[str, str]:
    return {
        "se3/specs/base/spec.md": (
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        ),
    }


def _setup_spec_files(tmp_path: Path) -> None:
    spec_dir = tmp_path / "se3" / "specs" / "base"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text(
        "## Requirement: Auth\n\n"
        "The system SHOULD validate all user inputs.\n"
    )


# --------- unit tests ---------


class TestGuardrailRepairerSuccessfulRepair:
    def test_successful_repair_passes_guardrails(self, tmp_path: Path, monkeypatch) -> None:
        """LLM returns corrected spec, guardrails re-check passes."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        # Mock _call_llm to return corrected spec content
        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "se3/specs/base/spec.md",
                        "corrected_content": (
                            "## Requirement: Auth\n\n"
                            "The system SHALL validate all user inputs.\n"
                        ),
                    },
                ],
            })

        monkeypatch.setattr(
            GuardrailRepairer, "_call_llm", mock_call_llm,
        )

        # Mock check_merge_result to pass after repair
        check_calls: list[tuple[str, str]] = []

        def mock_check_merge_result(self, pre_sha: str, post_sha: str) -> GuardrailReport:
            check_calls.append((pre_sha, post_sha))
            return GuardrailReport(passed=True, violations=[])

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check_merge_result,
        )

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        pre_sha = "abc123"
        post_sha = "def456"

        result = repairer.repair_violations(
            branch="feature",
            pre_sha=pre_sha,
            post_sha=post_sha,
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is True
        assert result.error is None
        assert "se3/specs/base/spec.md" in result.repaired_files

        # File should have been written with corrected content
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        assert "SHALL" in spec_path.read_text()
        assert "SHOULD" not in spec_path.read_text()

        # Guardrails should have been re-checked
        assert len(check_calls) == 1
        assert check_calls[0][0] == pre_sha

    def test_repair_multiple_files(self, tmp_path: Path, monkeypatch) -> None:
        """LLM repairs multiple spec files at once."""
        _init_repo(tmp_path)

        # Set up two spec files
        spec_dir1 = tmp_path / "se3" / "specs" / "base"
        spec_dir1.mkdir(parents=True)
        (spec_dir1 / "spec.md").write_text("SHOULD do X\n")

        spec_dir2 = tmp_path / "se3" / "specs" / "config"
        spec_dir2.mkdir(parents=True)
        (spec_dir2 / "spec.md").write_text("MAY do Y\n")

        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "add specs"],
            check=True, capture_output=True,
        )

        repairer = GuardrailRepairer(tmp_path)

        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "se3/specs/base/spec.md",
                        "corrected_content": "SHALL do X\n",
                    },
                    {
                        "path": "se3/specs/config/spec.md",
                        "corrected_content": "MUST do Y\n",
                    },
                ],
            })

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        def mock_check_merge_result(self, pre_sha: str, post_sha: str) -> GuardrailReport:
            return GuardrailReport(passed=True, violations=[])

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check_merge_result,
        )

        violations = [
            GuardrailViolation(
                file_path="se3/specs/base/spec.md",
                violation_type="WEAKENING",
                message="SHALL weakened to SHOULD",
            ),
            GuardrailViolation(
                file_path="se3/specs/config/spec.md",
                violation_type="WEAKENING",
                message="MUST weakened to MAY",
            ),
        ]
        original_specs = {
            "se3/specs/base/spec.md": "SHALL do X\n",
            "se3/specs/config/spec.md": "MUST do Y\n",
        }
        merged_specs = {
            "se3/specs/base/spec.md": "SHOULD do X\n",
            "se3/specs/config/spec.md": "MAY do Y\n",
        }

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is True
        assert len(result.repaired_files) == 2
        assert "se3/specs/base/spec.md" in result.repaired_files
        assert "se3/specs/config/spec.md" in result.repaired_files


class TestGuardrailRepairerFailures:
    def test_repair_still_violates_after_fix(self, tmp_path: Path, monkeypatch) -> None:
        """LLM returns spec that still has violations — guardrails re-check fails."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        # Mock LLM to return content that still has the violation
        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "se3/specs/base/spec.md",
                        "corrected_content": (
                            "## Requirement: Auth\n\n"
                            "The system SHOULD validate all user inputs.\n"
                        ),
                    },
                ],
            })

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        # Mock guardrails to still fail after repair
        def mock_check_merge_result(self, pre_sha: str, post_sha: str) -> GuardrailReport:
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
            mock_check_merge_result,
        )

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert result.error is not None
        assert "Still" in result.error or "violations" in result.error.lower()
        # Should still report which files were written
        assert "se3/specs/base/spec.md" in result.repaired_files

    def test_json_parse_failure(self, tmp_path: Path, monkeypatch) -> None:
        """LLM returns garbage that cannot be parsed as JSON."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        def mock_call_llm(self, prompt: str) -> str:
            return "This is not JSON at all, just prose."

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert "parse" in result.error.lower() or "JSON" in (result.error or "")

    def test_llm_call_exception(self, tmp_path: Path, monkeypatch) -> None:
        """LLM call raises an exception (e.g. subprocess failure)."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        def mock_call_llm(self, prompt: str) -> str:
            raise RuntimeError("mock LLM subprocess failure")

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert "LLM call failed" in result.error
        assert "mock LLM subprocess failure" in result.error

    def test_non_spec_path_rejected(self, tmp_path: Path, monkeypatch) -> None:
        """LLM attempts to write a file outside se3/specs/**/spec.md — rejected."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "README.md",
                        "corrected_content": "# Evil\n",
                    },
                ],
            })

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert "non-spec" in result.error.lower() or "spec" in result.error.lower()

        # README.md should NOT have been modified
        assert (tmp_path / "README.md").read_text() == "# Test\n"

    def test_path_traversal_rejected(self, tmp_path: Path, monkeypatch) -> None:
        """LLM attempts path traversal — rejected."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "se3/specs/base/../../evil.md",
                        "corrected_content": "evil",
                    },
                ],
            })

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert "non-spec" in result.error.lower() or "outside" in result.error.lower()

    def test_empty_files_list(self, tmp_path: Path, monkeypatch) -> None:
        """LLM returns valid JSON but empty files list."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({"files": []})

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert "no valid spec files" in result.error.lower()

    def test_files_not_a_list(self, tmp_path: Path, monkeypatch) -> None:
        """LLM returns 'files' as a string instead of a list."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({"files": "not a list"})

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert "not a list" in result.error.lower()

    def test_empty_llm_response(self, tmp_path: Path, monkeypatch) -> None:
        """LLM returns empty string."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        def mock_call_llm(self, prompt: str) -> str:
            return ""

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert "empty" in result.error.lower()

    def test_missing_corrected_content_field(self, tmp_path: Path, monkeypatch) -> None:
        """LLM returns file entry without 'corrected_content' — skip it, no files written."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "se3/specs/base/spec.md",
                        # missing corrected_content
                    },
                ],
            })

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert "without corrected_content" in result.error.lower()
        assert "se3/specs/base/spec.md" in result.error

    def test_repair_result_defaults(self) -> None:
        """RepairResult dataclass has correct defaults."""
        r = RepairResult()
        assert r.success is False
        assert r.repaired_files == []
        assert r.error is None

    def test_path_traversal_via_dotdot_to_spec_dir(self, tmp_path: Path, monkeypatch) -> None:
        """Path that passes regex but resolves outside specs/ is rejected."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        # This path passes the regex (^se3/specs/.+/spec\.md$) but resolves
        # to se3/tools/spec.md — outside the se3/specs/ directory.
        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "se3/specs/base/../../tools/spec.md",
                        "corrected_content": "evil",
                    },
                ],
            })

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert "outside spec dir" in result.error.lower()

        # se3/tools/spec.md must NOT have been created
        assert not (tmp_path / "se3" / "tools" / "spec.md").exists()

    def test_repair_recheck_crash_restores_content(self, tmp_path: Path, monkeypatch) -> None:
        """Guardrails re-check crashes after files written — merged content restored."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        # Mock LLM to return corrected content
        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "se3/specs/base/spec.md",
                        "corrected_content": (
                            "## Requirement: Auth\n\n"
                            "The system SHALL validate all user inputs.\n"
                        ),
                    },
                ],
            })

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        # Mock guardrails re-check to crash (raise exception)
        def mock_check_crash(self, pre_sha: str, post_sha: str):
            raise RuntimeError("Simulated guardrails re-check crash")

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check_crash,
        )

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        # Add the merged spec to the repo so amend works
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "merged"],
            check=True, capture_output=True,
        )

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        # Repair should report failure
        assert result.success is False
        assert "guardrails" in result.error.lower() or "re-check" in result.error.lower()

        # Merged content should be restored (SHOULD, not SHALL)
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        restored_content = spec_path.read_text()
        assert "SHOULD" in restored_content
        assert "SHALL" not in restored_content

    def test_repair_amend_timeout_restores_content(self, tmp_path: Path, monkeypatch) -> None:
        """git commit --amend times out — merged content restored, HEAD un-amended."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        # Add merged spec and create a merge commit
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "merged"],
            check=True, capture_output=True,
        )

        repairer = GuardrailRepairer(tmp_path)

        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "se3/specs/base/spec.md",
                        "corrected_content": (
                            "## Requirement: Auth\n\n"
                            "The system SHALL validate all user inputs.\n"
                        ),
                    },
                ],
            })

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        # Mock _run_git to raise TimeoutExpired on amend
        original_run_git = None

        def mock_run_git(project_root, *args, check=True, timeout=30):
            if len(args) >= 2 and args[0] == "commit" and args[1] == "--amend":
                raise subprocess.TimeoutExpired(
                    cmd=["git", "commit", "--amend"], timeout=30,
                )
            # Fall through to real _run_git
            import se3.engine.worktree as _wt
            return _wt._run_git(project_root, *args, check=check, timeout=timeout)

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair._run_git", mock_run_git,
        )

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert "timeout" in result.error.lower()

        # Merged content should be restored (SHOULD, not SHALL)
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        restored_content = spec_path.read_text()
        assert "SHOULD" in restored_content
        assert "SHALL" not in restored_content

    def test_partial_repair_one_fixed_one_still_violates(self, tmp_path: Path, monkeypatch) -> None:
        """LLM fixes file A but file B still violates — report remaining violations."""
        _init_repo(tmp_path)

        # Set up two spec files
        spec_dir1 = tmp_path / "se3" / "specs" / "base"
        spec_dir1.mkdir(parents=True)
        (spec_dir1 / "spec.md").write_text("SHOULD do X\n")

        spec_dir2 = tmp_path / "se3" / "specs" / "config"
        spec_dir2.mkdir(parents=True)
        (spec_dir2 / "spec.md").write_text("MAY do Y\n")

        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "add specs"],
            check=True, capture_output=True,
        )

        repairer = GuardrailRepairer(tmp_path)

        # Mock LLM to fix only file A (base/spec.md)
        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "se3/specs/base/spec.md",
                        "corrected_content": "SHALL do X\n",
                    },
                ],
            })

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        # Mock guardrails: file A passes, file B still violates
        def mock_check_merge_result(self, pre_sha: str, post_sha: str) -> GuardrailReport:
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/config/spec.md",
                        violation_type="WEAKENING",
                        message="MUST weakened to MAY",
                    ),
                ],
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check_merge_result,
        )

        violations = [
            GuardrailViolation(
                file_path="se3/specs/base/spec.md",
                violation_type="WEAKENING",
                message="SHALL weakened to SHOULD",
            ),
            GuardrailViolation(
                file_path="se3/specs/config/spec.md",
                violation_type="WEAKENING",
                message="MUST weakened to MAY",
            ),
        ]
        original_specs = {
            "se3/specs/base/spec.md": "SHALL do X\n",
            "se3/specs/config/spec.md": "MUST do Y\n",
        }
        merged_specs = {
            "se3/specs/base/spec.md": "SHOULD do X\n",
            "se3/specs/config/spec.md": "MAY do Y\n",
        }

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        # Should report that file A was repaired
        assert "se3/specs/base/spec.md" in result.repaired_files
        # Should report remaining violations from file B
        assert "se3/specs/config/spec.md" in result.error
        assert "MUST weakened to MAY" in result.error
        assert "Remaining violations" in result.error

        # File A should be restored to merged content (not the repaired content)
        # because the repair failed overall
        base_content = (tmp_path / "se3" / "specs" / "base" / "spec.md").read_text()
        assert "SHOULD" in base_content

    def test_conflict_markers_in_repaired_content_rejected(self, tmp_path: Path, monkeypatch) -> None:
        """LLM returns corrected content containing conflict markers — rejected."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "se3/specs/base/spec.md",
                        "corrected_content": (
                            "## Requirement: Auth\n\n"
                            "The system SHALL validate all user inputs.\n"
                            "<<<<<<< HEAD\n"
                            "old line\n"
                            "=======\n"
                            "new line\n"
                            ">>>>>>> branch\n"
                        ),
                    },
                ],
            })

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert "conflict markers" in result.error.lower()

        # Original file should be unchanged (no write occurred)
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        assert "SHOULD" in spec_path.read_text()
        assert "<<<<<<<" not in spec_path.read_text()

    def test_is_spec_path_method(self, tmp_path: Path) -> None:
        """_is_spec_path accepts only se3/specs/**/spec.md."""
        from se3.engine.merge.guardrails import _is_spec_path
        assert _is_spec_path("se3/specs/base/spec.md") is True
        assert _is_spec_path("se3/specs/nested/deep/spec.md") is True
        assert _is_spec_path("README.md") is False
        assert _is_spec_path("se3/specs/base/other.txt") is False
        assert _is_spec_path("src/main.py") is False
        assert _is_spec_path("se3\\specs\\base\\spec.md") is True

    def test_empty_allowed_paths_rejects_repair(self, tmp_path: Path, monkeypatch) -> None:
        """Empty allowed_paths (no spec contents provided) rejects repair."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "se3/specs/base/spec.md",
                        "corrected_content": "SHALL do X\n",
                    },
                ],
            })

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        violations = _make_violations()

        # Pass empty dicts for both spec contents — this simulates a race
        # condition where _get_changed_spec_files returned nothing.
        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents={},
            merged_spec_contents={},
        )

        assert result.success is False
        assert "not in the changed spec set" in result.error.lower() or "unexpected" in result.error.lower()

    def test_multifile_valid_then_conflict_markers_restores_first(self, tmp_path: Path, monkeypatch) -> None:
        """File A written, file B rejected for conflict markers — file A restored.

        Verifies the invariant that partial writes inside the validation loop
        are always rolled back when a later file fails validation.
        """
        _init_repo(tmp_path)

        # Set up two spec files
        spec_dir1 = tmp_path / "se3" / "specs" / "base"
        spec_dir1.mkdir(parents=True)
        (spec_dir1 / "spec.md").write_text("SHOULD do X\n")

        spec_dir2 = tmp_path / "se3" / "specs" / "config"
        spec_dir2.mkdir(parents=True)
        (spec_dir2 / "spec.md").write_text("MAY do Y\n")

        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "add specs"],
            check=True, capture_output=True,
        )

        repairer = GuardrailRepairer(tmp_path)

        # Mock LLM: file A is valid, file B contains conflict markers
        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "se3/specs/base/spec.md",
                        "corrected_content": "SHALL do X\n",
                    },
                    {
                        "path": "se3/specs/config/spec.md",
                        "corrected_content": (
                            "MUST do Y\n"
                            "<<<<<<< HEAD\n"
                            "old line\n"
                            "=======\n"
                            "new line\n"
                            ">>>>>>> branch\n"
                        ),
                    },
                ],
            })

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        violations = [
            GuardrailViolation(
                file_path="se3/specs/base/spec.md",
                violation_type="WEAKENING",
                message="SHALL weakened to SHOULD",
            ),
            GuardrailViolation(
                file_path="se3/specs/config/spec.md",
                violation_type="WEAKENING",
                message="MUST weakened to MAY",
            ),
        ]
        original_specs = {
            "se3/specs/base/spec.md": "SHALL do X\n",
            "se3/specs/config/spec.md": "MUST do Y\n",
        }
        merged_specs = {
            "se3/specs/base/spec.md": "SHOULD do X\n",
            "se3/specs/config/spec.md": "MAY do Y\n",
        }

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert "conflict markers" in result.error.lower()

        # File A should have been restored to merged content (not repaired)
        base_content = (tmp_path / "se3" / "specs" / "base" / "spec.md").read_text()
        assert "SHOULD" in base_content
        assert "SHALL" not in base_content

        # File B should still have merged content (never written)
        config_content = (tmp_path / "se3" / "specs" / "config" / "spec.md").read_text()
        assert "MAY" in config_content
        assert "MUST" not in config_content

    def test_extract_json_two_phase_exception(self, tmp_path: Path, monkeypatch) -> None:
        """extract_json_two_phase raises → caught, returns specific parse error."""
        _init_repo(tmp_path)
        _setup_spec_files(tmp_path)

        repairer = GuardrailRepairer(tmp_path)

        # Mock LLMCaller.call so the real LLM is not hit; the real _call_llm
        # still runs and reaches extract_json_two_phase, which we then mock to
        # raise.
        def mock_llm_call(self, **kwargs) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "se3/specs/base/spec.md",
                        "corrected_content": "SHALL do X\n",
                    },
                ],
            })

        monkeypatch.setattr(
            "se3.engine.llm_caller.LLMCaller.call",
            mock_llm_call,
        )

        def mock_extract_json(raw, project_root=None, schema_hint=None, required_keys=None):
            raise ValueError("simulated two-phase extraction crash")

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.extract_json_two_phase",
            mock_extract_json,
        )

        violations = _make_violations()
        original_specs = _make_original_specs()
        merged_specs = _make_merged_specs()

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert "parsing failed" in result.error.lower() or "LLM call failed" in result.error
        assert "simulated two-phase extraction crash" in result.error


    def test_partial_write_then_second_file_write_fails_restores_first(self, tmp_path: Path, monkeypatch) -> None:
        """File A is written, then file B's write fails — file A is restored.

        The write loop appends to repaired_files only after all validations
        pass. If a later file's write raises an exception, the except block
        calls _restore_merged_content with all previously-written files.
        This test verifies the invariant that partial writes are always
        rolled back on failure.
        """
        _init_repo(tmp_path)

        # Set up two spec files
        spec_dir1 = tmp_path / "se3" / "specs" / "base"
        spec_dir1.mkdir(parents=True)
        (spec_dir1 / "spec.md").write_text("SHOULD do X\n")

        spec_dir2 = tmp_path / "se3" / "specs" / "config"
        spec_dir2.mkdir(parents=True)
        (spec_dir2 / "spec.md").write_text("MAY do Y\n")

        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "add specs"],
            check=True, capture_output=True,
        )

        repairer = GuardrailRepairer(tmp_path)

        # Mock LLM to return corrections for both files
        def mock_call_llm(self, prompt: str) -> str:
            return json.dumps({
                "files": [
                    {
                        "path": "se3/specs/base/spec.md",
                        "corrected_content": "SHALL do X\n",
                    },
                    {
                        "path": "se3/specs/config/spec.md",
                        "corrected_content": "MUST do Y\n",
                    },
                ],
            })

        monkeypatch.setattr(GuardrailRepairer, "_call_llm", mock_call_llm)

        # Make the second write fail by monkeypatching Path.write_text
        original_write_text = Path.write_text
        call_count = [0]

        def mock_write_text(self, content, encoding=None):
            call_count[0] += 1
            if call_count[0] == 2:
                raise OSError("simulated disk full")
            return original_write_text(self, content, encoding=encoding)

        monkeypatch.setattr(Path, "write_text", mock_write_text)

        violations = [
            GuardrailViolation(
                file_path="se3/specs/base/spec.md",
                violation_type="WEAKENING",
                message="SHALL weakened to SHOULD",
            ),
            GuardrailViolation(
                file_path="se3/specs/config/spec.md",
                violation_type="WEAKENING",
                message="MUST weakened to MAY",
            ),
        ]
        original_specs = {
            "se3/specs/base/spec.md": "SHALL do X\n",
            "se3/specs/config/spec.md": "MUST do Y\n",
        }
        merged_specs = {
            "se3/specs/base/spec.md": "SHOULD do X\n",
            "se3/specs/config/spec.md": "MAY do Y\n",
        }

        result = repairer.repair_violations(
            branch="feature",
            pre_sha="abc",
            post_sha="def",
            violations=violations,
            original_spec_contents=original_specs,
            merged_spec_contents=merged_specs,
        )

        assert result.success is False
        assert "disk full" in result.error or "Failed to write" in result.error

        # File A should have been restored to merged content (not repaired)
        base_content = (tmp_path / "se3" / "specs" / "base" / "spec.md").read_text()
        assert "SHOULD" in base_content
        assert "SHALL" not in base_content

        # File B should still have merged content (never written)
        config_content = (tmp_path / "se3" / "specs" / "config" / "spec.md").read_text()
        assert "MAY" in config_content
        assert "MUST" not in config_content


class TestParseResponseDictPath:
    """_parse_response handles dict input from _call_llm (two-phase extractor)."""

    def test_dict_with_files_key(self, tmp_path: Path) -> None:
        """Dict with 'files' key is returned as-is."""
        repairer = GuardrailRepairer(tmp_path)
        payload = {"files": [{"path": "se3/specs/base/spec.md", "corrected_content": "x"}]}
        result = repairer._parse_response(payload)
        assert result is not None
        assert "files" in result
        assert result["files"][0]["path"] == "se3/specs/base/spec.md"

    def test_dict_without_files_key(self, tmp_path: Path) -> None:
        """Dict missing 'files' key returns None."""
        repairer = GuardrailRepairer(tmp_path)
        result = repairer._parse_response({"other": "value"})
        assert result is None
