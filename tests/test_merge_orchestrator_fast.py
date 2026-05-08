"""Focused unit tests for MergeOrchestrator fast-mode guardrail repair loop.

Uses real git repos for SHA management but mocks GuardrailRepairer
and MergeGuardrailsCheck to precisely control the repair loop behavior.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from se3.engine.merge.conflict_resolver import MergeStrategy
from se3.engine.merge.guardrails import GuardrailReport, GuardrailViolation
from se3.engine.merge.orchestrator import (
    GuardrailRepairExhausted,
    GuardrailRepairFailed,
    GuardrailRepairStalled,
    GuardrailRollbackError,
    MergeOrchestrator,
)


# --------- helpers ---------


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")


def _commit(path: Path, message: str) -> None:
    _git(path, "add", "-A")
    _git(path, "commit", "-m", message)


def _current_branch(path: Path) -> str:
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _setup_spec_repo(tmp_path: Path) -> str:
    """Init repo with a spec file. Returns default branch name."""
    _init_repo(tmp_path)
    spec_dir = tmp_path / "se3" / "specs" / "base"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text(
        "## Requirement: Auth\n\n"
        "The system SHALL validate all user inputs.\n"
    )
    (tmp_path / "code.py").write_text("def auth(): pass\n")
    _commit(tmp_path, "initial")
    return _current_branch(tmp_path)


# --------- _run_guardrails fast branch tests ---------


class TestRunGuardrailsFastRepairLoop:
    """Tests for _run_guardrails fast strategy repair loop with mocked deps."""

    def test_repair_stalled_same_hash_twice_raises_with_call_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Repair returns same violations twice → GuardrailRepairStalled with call file."""
        default_branch = _setup_spec_repo(tmp_path)

        # Create feature branch that weakens spec
        _git(tmp_path, "checkout", "-b", "feature-stall")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _git(tmp_path, "merge", "feature-stall", "--no-edit", "-m", "Merge feature-stall")
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to always fail
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=False,
                error="LLM could not fix the weakening",
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock guardrails check to always return the SAME violation with evidence
        def mock_check(self, pre_sha: str, post_sha: str):
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/base/spec.md",
                        violation_type="WEAKENING",
                        message="SHALL weakened to SHOULD",
                        evidence={
                            "strong_line": "The system SHALL validate all user inputs.",
                            "weak_line": "The system SHOULD validate all user inputs.",
                            "strong_line_no": 3,
                            "weak_line_no": 3,
                            "pairing_score": 0.92,
                        },
                    ),
                ],
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        with pytest.raises(GuardrailRepairStalled) as exc_info:
            orch._run_guardrails(
                pre_head, post_head, "feature-stall", strategy=MergeStrategy.FAST,
            )

        # Should stall at iteration 1 because last_hash is initialised with
        # the initial violation-set hash, so a no-op repair on the first
        # iteration is immediately detected as a stall.
        assert exc_info.value.iteration_count == 1
        assert exc_info.value.call_file is not None
        assert exc_info.value.call_file.exists()

        # Call file type and content assertions
        data = json.loads(exc_info.value.call_file.read_text())
        assert data["type"] == "guardrail_repair_stalled"
        assert data["iteration_count"] == 1
        assert len(data["violations"]) >= 1
        assert data["branch"] == "feature-stall"
        assert data["pre_merge_sha"] == pre_head

        # Evidence substructure must survive the _violations_to_dicts
        # passthrough and write_guardrail_call JSON serialization.
        v0 = data["violations"][0]
        assert v0["evidence"]["strong_line"] == (
            "The system SHALL validate all user inputs."
        )
        assert v0["evidence"]["weak_line"] == (
            "The system SHOULD validate all user inputs."
        )
        assert v0["evidence"]["strong_line_no"] == 3
        assert v0["evidence"]["weak_line_no"] == 3
        assert v0["evidence"]["pairing_score"] == 0.92

        # HEAD should be rolled back to pre-merge state
        current_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert current_head == pre_head

    def test_repair_exhausted_max_iterations_raises_with_call_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Repair returns different hashes each round → exhausted path with call file."""
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-exhaust")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _git(tmp_path, "merge", "feature-exhaust", "--no-edit", "-m", "Merge feature-exhaust")
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to always fail
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=False,
                error="LLM could not fix the weakening",
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock guardrails check to return a DIFFERENT violation each time so
        # hashes are always novel and never stall.  Three calls total:
        #   Call 1 (initial):  evidence v1
        #   Call 2 (re-check after iter 1): evidence v2
        #   Call 3 (re-check after iter 2): evidence v3
        check_call_count = [0]

        def mock_check(self, pre_sha: str, post_sha: str):
            check_call_count[0] += 1
            evidence = {
                "strong_line": f"The system SHALL validate inputs v{check_call_count[0]}.",
                "weak_line": f"The system SHOULD validate inputs v{check_call_count[0]}.",
                "strong_line_no": check_call_count[0] + 2,
                "weak_line_no": check_call_count[0] + 2,
                "pairing_score": 0.9,
            }
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/base/spec.md",
                        violation_type="WEAKENING",
                        message="SHALL weakened to SHOULD",
                        evidence=evidence,
                    ),
                ],
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        with pytest.raises(GuardrailRepairStalled) as exc_info:
            orch._run_guardrails(
                pre_head, post_head, "feature-exhaust", strategy=MergeStrategy.FAST,
            )

        # Should exhaust at iteration 2 (max iterations)
        from se3.commands.merge.failure_reason import FailureReason
        assert exc_info.value.iteration_count == 2
        assert exc_info.value.failure_reason is FailureReason.GUARDRAIL_REPAIR_EXHAUSTED
        assert exc_info.value.last_violation_hash != ""
        assert exc_info.value.call_file is not None
        assert exc_info.value.call_file.exists()

        # Call file type and content assertions
        data = json.loads(exc_info.value.call_file.read_text())
        assert data["type"] == "guardrail_repair_exhausted"
        assert data["iteration_count"] == 2
        assert len(data["violations"]) >= 1
        assert data["branch"] == "feature-exhaust"
        assert data["pre_merge_sha"] == pre_head

        # Evidence substructure must survive the _violations_to_dicts
        # passthrough and write_guardrail_call JSON serialization.
        v0 = data["violations"][0]
        assert "v3" in v0["evidence"]["strong_line"]
        assert "v3" in v0["evidence"]["weak_line"]
        assert v0["evidence"]["strong_line_no"] == 5
        assert v0["evidence"]["weak_line_no"] == 5
        assert v0["evidence"]["pairing_score"] == 0.9

        # HEAD should be rolled back to pre-merge state
        current_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert current_head == pre_head

    def test_repair_hash_changes_then_exhausts_max_iterations(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Repair hash changes each round but repeats → exhausted after max iterations.

        The mock alternates between two violation hashes.  With last_hash
        tracking only the immediately previous iteration, the oscillation
        back to the initial hash is not detected as a stall within 2 iterations.
        Instead, max iterations are exhausted and GuardrailRepairExhausted is
        raised (subclass of GuardrailRepairStalled).
        """
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-change")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _git(tmp_path, "merge", "feature-change", "--no-edit", "-m", "Merge feature-change")
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to always fail
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=False,
                error="LLM could not fix",
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock guardrails check to return violations with different
        # strong_line evidence each time so the stable key (and thus hash)
        # changes, but violations are never empty.  Because the mock is also
        # used for the initial check, the first result (odd) sets the initial
        # hash; iteration 1 (even) produces a different hash; iteration 2
        # (odd) returns the SAME hash as the initial check.  Because the
        # initial hash is NOT seeded into last_hash, this oscillation is
        # NOT detected as a stall — the loop simply exhausts its max iterations.
        check_call_count = [0]

        def mock_check(self, pre_sha: str, post_sha: str):
            check_call_count[0] += 1
            if check_call_count[0] % 2 == 1:
                evidence = {
                    "strong_line": "The system SHALL validate inputs.",
                    "weak_line": "The system SHOULD validate inputs.",
                    "pairing_score": 0.9,
                }
            else:
                evidence = {
                    "strong_line": "The system SHALL check permissions.",
                    "weak_line": "The system SHOULD check permissions.",
                    "pairing_score": 0.9,
                }
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/base/spec.md",
                        violation_type="WEAKENING",
                        message="SHALL weakened to SHOULD",
                        evidence=evidence,
                    ),
                ],
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        with pytest.raises(GuardrailRepairStalled) as exc_info:
            orch._run_guardrails(
                pre_head, post_head, "feature-change", strategy=MergeStrategy.FAST,
            )

        # Exhausted at iteration 2 (oscillation not detected as stall within max
        # iterations because last_hash is not seeded with the initial hash).
        assert exc_info.value.iteration_count == 2
        assert exc_info.value.call_file is not None
        assert exc_info.value.call_file.exists()

        # HEAD should be rolled back to pre-merge state
        current_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert current_head == pre_head

    def test_repair_succeeds_at_second_iteration_returns_none(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Repair fails at iter 1, succeeds at iter 2 → returns None."""
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-fix")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _git(tmp_path, "merge", "feature-fix", "--no-edit", "-m", "Merge feature-fix")
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        repair_call_count = [0]

        # Mock repairer: fails first time, succeeds second time
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            repair_call_count[0] += 1
            from se3.engine.merge.guardrail_repair import RepairResult
            if repair_call_count[0] == 1:
                return RepairResult(
                    success=False,
                    error="LLM first attempt failed",
                )
            return RepairResult(
                success=True,
                repaired_files=["se3/specs/base/spec.md"],
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock guardrails check:
        #   Call 1 (initial check): violation A
        #   Call 2 (re-check after iter 1): violation B (different hash → no stall)
        #   (No re-check after iter 2 because repair succeeded and returned)
        check_call_count = [0]

        def mock_check(self, pre_sha: str, post_sha: str):
            check_call_count[0] += 1
            if check_call_count[0] == 1:
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
            elif check_call_count[0] == 2:
                return GuardrailReport(
                    passed=False,
                    violations=[
                        GuardrailViolation(
                            file_path="se3/specs/base/spec.md",
                            violation_type="WEAKENING",
                            message="MUST weakened to SHOULD",
                        ),
                    ],
                )
            return GuardrailReport(passed=True, violations=[])

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        result = orch._run_guardrails(
            pre_head, post_head, "feature-fix", strategy=MergeStrategy.FAST,
        )

        # Should return None (no violation after repair)
        assert result is None

        # Repairer should have been called twice (fail then succeed)
        assert repair_call_count[0] == 2
        # Guardrails check: initial + re-check after iter 1 = 2 calls
        assert check_call_count[0] == 2

    def test_repair_failure_but_violations_cleared_by_side_effect(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Repairer reports failure but re-check passes → return None.

        This is reachable when the repairer is mocked or when a future
        code path returns success=False after partial successful writes
        that happen to clear all violations as a side-effect.
        """
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-side-effect")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _git(tmp_path, "merge", "feature-side-effect", "--no-edit",
             "-m", "Merge feature-side-effect")
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer: always reports failure
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=False,
                error="repairer claims failure",
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer"
            ".repair_violations",
            mock_repair,
        )

        # Mock guardrails check: initial violation, re-check passes
        check_call_count = [0]

        def mock_check(self, pre_sha: str, post_sha: str):
            check_call_count[0] += 1
            if check_call_count[0] == 1:
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
            return GuardrailReport(passed=True, violations=[])

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck"
            ".check_merge_result",
            mock_check,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        result = orch._run_guardrails(
            pre_head, post_head, "feature-side-effect",
            strategy=MergeStrategy.FAST,
        )

        # Should return None (accept the side-effect clearance)
        assert result is None
        # Guardrails check: initial + re-check after iter 1 = 2 calls
        assert check_call_count[0] == 2

    def test_repair_stalled_rollback_fails_raises_rollback_error_with_call_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Stall path: rollback fails but call file is written -> GuardrailRollbackError."""
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-stall-rollback")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _git(tmp_path, "merge", "feature-stall-rollback", "--no-edit",
             "-m", "Merge feature-stall-rollback")
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(success=False, error="LLM could not fix")

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        def mock_check(self, pre_sha: str, post_sha: str):
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

        def mock_rollback_to(self, sha: str) -> None:
            raise RuntimeError("simulated rollback failure")

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.MergeOrchestrator._rollback_to",
            mock_rollback_to,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        with pytest.raises(GuardrailRollbackError) as exc_info:
            orch._run_guardrails(
                pre_head, post_head, "feature-stall-rollback",
                strategy=MergeStrategy.FAST,
            )

        assert exc_info.value.call_file is not None
        assert exc_info.value.call_file.exists()
        data = json.loads(exc_info.value.call_file.read_text())
        assert data["type"] == "guardrail_repair_stalled"
        assert len(data["violations"]) >= 1

    def test_repair_exhausted_rollback_fails_raises_rollback_error_with_call_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Exhausted path: rollback fails but call file is written -> GuardrailRollbackError."""
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-exhaust-rollback")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _git(tmp_path, "merge", "feature-exhaust-rollback", "--no-edit",
             "-m", "Merge feature-exhaust-rollback")
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(success=False, error="LLM could not fix")

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        check_call_count = [0]

        def mock_check(self, pre_sha: str, post_sha: str):
            check_call_count[0] += 1
            evidence = {
                "strong_line": f"The system SHALL validate inputs v{check_call_count[0]}.",
                "weak_line": f"The system SHOULD validate inputs v{check_call_count[0]}.",
                "pairing_score": 0.9,
            }
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/base/spec.md",
                        violation_type="WEAKENING",
                        message="SHALL weakened to SHOULD",
                        evidence=evidence,
                    ),
                ],
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        def mock_rollback_to(self, sha: str) -> None:
            raise RuntimeError("simulated rollback failure")

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.MergeOrchestrator._rollback_to",
            mock_rollback_to,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        with pytest.raises(GuardrailRollbackError) as exc_info:
            orch._run_guardrails(
                pre_head, post_head, "feature-exhaust-rollback",
                strategy=MergeStrategy.FAST,
            )

        assert exc_info.value.call_file is not None
        assert exc_info.value.call_file.exists()
        data = json.loads(exc_info.value.call_file.read_text())
        assert data["type"] == "guardrail_repair_exhausted"
        assert len(data["violations"]) >= 1

    def test_repair_exhausted_is_exactly_guardrail_repair_exhausted_type(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Hash changes every iteration → for...else clause runs →
        GuardrailRepairExhausted (exact type, not just the parent class).

        This catches a regression where a future contributor adds a
        ``break`` inside the repair loop, which would silently skip
        the ``else`` clause and the exhausted-iterations escalation.
        """
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-exhaust-type")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _git(tmp_path, "merge", "feature-exhaust-type", "--no-edit",
             "-m", "Merge feature-exhaust-type")
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(success=False, error="LLM could not fix")

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        check_call_count = [0]

        def mock_check(self, pre_sha: str, post_sha: str):
            check_call_count[0] += 1
            # Each call returns a different strong_line so the hash changes
            # every iteration, ensuring the loop reaches the else clause.
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/base/spec.md",
                        violation_type="WEAKENING",
                        message="SHALL weakened to SHOULD",
                        evidence={
                            "strong_line": (
                                f"The system SHALL validate inputs "
                                f"hash{check_call_count[0]}."
                            ),
                            "weak_line": (
                                f"The system SHOULD validate inputs "
                                f"hash{check_call_count[0]}."
                            ),
                            "pairing_score": 0.9,
                        },
                    ),
                ],
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        # Must use the exact subclass, not the parent GuardrailRepairStalled,
        # to prove the for...else clause executed.
        with pytest.raises(GuardrailRepairExhausted) as exc_info:
            orch._run_guardrails(
                pre_head, post_head, "feature-exhaust-type",
                strategy=MergeStrategy.FAST,
            )

        assert exc_info.value.iteration_count == 2
        assert exc_info.value.call_file is not None

        # HEAD should be rolled back to pre-merge state
        current_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert current_head == pre_head

    def test_topology_violation_fast_short_circuits_to_human_call(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """CHECK_FAILURE topology violations in fast mode skip LLM repair
        and route directly to rollback + human call."""
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-topo")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _git(tmp_path, "merge", "feature-topo", "--no-edit",
             "-m", "Merge feature-topo")
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Track whether the LLM repairer was invoked — it must NOT be.
        repair_invoked = [False]

        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            repair_invoked[0] = True
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(success=False, error="should not be called")

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        def mock_check(self, pre_sha: str, post_sha: str):
            return GuardrailReport(
                passed=False,
                violations=[
                    # Topology violation: CHECK_FAILURE with file_path="N/A"
                    GuardrailViolation(
                        file_path="N/A",
                        violation_type="CHECK_FAILURE",
                        message=(
                            "Merge topology violation: pre-merge SHA is NOT "
                            "an ancestor of post-merge SHA."
                        ),
                        evidence={
                            "pre_sha": pre_sha,
                            "post_sha": post_sha,
                            "topology_check": "ancestry",
                        },
                    ),
                    # Plus a normal spec violation
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

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        result = orch._run_guardrails(
            pre_head, post_head, "feature-topo",
            strategy=MergeStrategy.FAST,
        )

        # The LLM repairer must NOT have been invoked because topology
        # violations short-circuited to the default-strategy human-call path.
        assert repair_invoked[0] is False
        # _run_guardrails returns the call file Path for default/strict path
        assert result is not None
        assert isinstance(result, Path)
        assert result.exists()

        # HEAD should be rolled back to pre-merge state
        current_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert current_head == pre_head

    def test_incomplete_short_circuits_for_default_strategy(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """CHECK_INCOMPLETE in default strategy skips LLM repair and routes
        directly to rollback + human call."""
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-incomplete")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _git(tmp_path, "merge", "feature-incomplete", "--no-edit",
             "-m", "Merge feature-incomplete")
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Track whether the LLM repairer was invoked — it must NOT be.
        repair_invoked = [False]

        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            repair_invoked[0] = True
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(success=False, error="should not be called")

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        def mock_check(self, pre_sha: str, post_sha: str):
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/base/spec.md",
                        violation_type="CHECK_INCOMPLETE",
                        message="Spec file iteration error: OSError",
                        evidence={"exception_type": "OSError"},
                    ),
                ],
                incomplete=True,
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        result = orch._run_guardrails(
            pre_head, post_head, "feature-incomplete",
            strategy=MergeStrategy.DEFAULT,
        )

        # The LLM repairer must NOT have been invoked because incomplete
        # short-circuits to the default-strategy human-call path.
        assert repair_invoked[0] is False
        # _run_guardrails returns the call file Path for default/strict path
        assert result is not None
        assert isinstance(result, Path)
        assert result.exists()

        # HEAD should be rolled back to pre-merge state
        current_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert current_head == pre_head


class TestExecuteFastStalledEscalation:
    """Tests for execute() routing when fast mode repair stalls."""

    def test_execute_fast_stalled_sets_pending_human_and_call_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """execute() with fast strategy: stalled repair → report.pending_human=True."""
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-stall-exec")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)
        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to always fail
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=False,
                error="LLM could not fix",
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock guardrails check to always return the SAME violation
        def mock_check(self, pre_sha: str, post_sha: str):
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

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature-stall-exec"])

        # Should have escalated to human call (stall detected at iteration 2)
        assert report.success is False
        assert report.failure_reason == "guardrail_repair_stalled"
        assert report.pending_human is True
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # Call file should have correct type
        data = json.loads(report.human_call_file.read_text())
        assert data["type"] == "guardrail_repair_stalled"
        # Stall detected at iteration 1 because last_hash is seeded with
        # the initial violation-set hash.
        assert data["iteration_count"] == 1
        assert data["branch"] == "feature-stall-exec"

        # HEAD should be restored
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head == pre_head

    def test_execute_fast_hash_changes_exhausts_to_human_call(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """execute() with fast strategy: oscillating hash → exhausted, human call created.

        The mock alternates between two violation hashes.  With last_hash
        tracking only the immediately previous iteration, the oscillation
        back to the initial hash is not detected as a stall within 2 iterations.
        Instead, max iterations are exhausted and the report is escalated to a
        human call via GuardrailRepairExhausted (subclass of GuardrailRepairStalled).
        """
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-change-exec")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)
        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to always fail
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=False,
                error="LLM could not fix",
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock guardrails check to return violations with different
        # strong_line evidence each time so the stable key (and thus hash)
        # changes, but violations are never empty.  Because the mock is also
        # used for the initial check, the first result (odd) sets the initial
        # hash; iteration 1 (even) produces a different hash; iteration 2
        # (odd) returns the SAME hash as the initial check.  Because the
        # initial hash is NOT seeded into last_hash, this oscillation is
        # NOT detected as a stall — the loop simply exhausts its max iterations.
        check_call_count = [0]

        def mock_check(self, pre_sha: str, post_sha: str):
            check_call_count[0] += 1
            if check_call_count[0] % 2 == 1:
                evidence = {
                    "strong_line": "The system SHALL validate inputs.",
                    "weak_line": "The system SHOULD validate inputs.",
                    "pairing_score": 0.9,
                }
            else:
                evidence = {
                    "strong_line": "The system SHALL check permissions.",
                    "weak_line": "The system SHOULD check permissions.",
                    "pairing_score": 0.9,
                }
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/base/spec.md",
                        violation_type="WEAKENING",
                        message="SHALL weakened to SHOULD",
                        evidence=evidence,
                    ),
                ],
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature-change-exec"])

        # Oscillating hash not detected as stall within max iterations →
        # exhausted → human call (via GuardrailRepairExhausted subclass)
        assert report.success is False
        assert report.failure_reason == "guardrail_repair_exhausted"
        assert report.pending_human is True
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # HEAD should be restored
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head == pre_head

    def test_missing_post_sha_fast_raises_guardrail_repair_failed(
        self, tmp_path: Path
    ) -> None:
        """_run_guardrails in fast mode with missing post_sha raises
        GuardrailRepairFailed with failure_reason='guardrail_missing_post_sha'."""
        default_branch = _setup_spec_repo(tmp_path)

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        with pytest.raises(GuardrailRepairFailed) as exc_info:
            orch._run_guardrails(
                pre_sha=pre_head, post_sha="", branch="feature",
                strategy=MergeStrategy.FAST,
            )

        from se3.commands.merge.failure_reason import FailureReason
        assert exc_info.value.failure_reason is FailureReason.GUARDRAIL_MISSING_POST_SHA
        assert "missing post_sha" in str(exc_info.value)
        # No rollback needed when post_sha is missing (nothing to roll back to).
        current_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert current_head == pre_head

    def test_missing_pre_sha_fast_raises_guardrail_repair_failed(
        self, tmp_path: Path
    ) -> None:
        """_run_guardrails in fast mode with missing pre_sha raises
        GuardrailRepairFailed with failure_reason='guardrail_missing_pre_sha'."""
        default_branch = _setup_spec_repo(tmp_path)

        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        with pytest.raises(GuardrailRepairFailed) as exc_info:
            orch._run_guardrails(
                pre_sha="", post_sha=post_head, branch="feature",
                strategy=MergeStrategy.FAST,
            )

        from se3.commands.merge.failure_reason import FailureReason
        assert exc_info.value.failure_reason is FailureReason.GUARDRAIL_MISSING_PRE_SHA
        assert "missing pre_sha" in str(exc_info.value)
        # No rollback attempted because pre_merge_sha is missing.
        current_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert current_head == post_head

    def test_missing_both_shas_fast_raises_guardrail_repair_failed(
        self, tmp_path: Path
    ) -> None:
        """_run_guardrails in fast mode with both SHAs missing raises
        GuardrailRepairFailed with
        failure_reason='guardrail_missing_pre_and_post_sha'."""
        _setup_spec_repo(tmp_path)

        pre_head = ""
        post_head = ""

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        with pytest.raises(GuardrailRepairFailed) as exc_info:
            orch._run_guardrails(
                pre_sha=pre_head, post_sha=post_head, branch="feature",
                strategy=MergeStrategy.FAST,
            )

        from se3.commands.merge.failure_reason import FailureReason
        assert (
            exc_info.value.failure_reason
            is FailureReason.GUARDRAIL_MISSING_PRE_AND_POST_SHA
        )
        assert "pre and post SHA" in str(exc_info.value)

    def test_execute_fast_repair_success_no_violation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """execute() with fast strategy: repair succeeds → merge completes."""
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-success-exec")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)
        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to succeed immediately
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=True,
                repaired_files=["se3/specs/base/spec.md"],
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock guardrails check to pass after repair
        check_call_count = [0]

        def mock_check(self, pre_sha: str, post_sha: str):
            check_call_count[0] += 1
            if check_call_count[0] == 1:
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
            return GuardrailReport(passed=True, violations=[])

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature-success-exec"])

        # Should succeed
        assert report.success is True
        assert "feature-success-exec" in report.merged_branches
        assert report.pending_human is False
        assert report.human_call_file is None

        # HEAD should have changed (merge commit created)
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head != pre_head

    def test_execute_fast_stalled_call_file_write_fails_aborts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """execute() with fast strategy: stalled repair + call file write fails → abort."""
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-stall-call-fail")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)
        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to always fail
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=False,
                error="LLM could not fix",
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock guardrails check to always return the SAME violation
        def mock_check(self, pre_sha: str, post_sha: str):
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

        # Monkeypatch HumanCallWriter.write_guardrail_call to raise
        def mock_write_guardrail_call(self, branch, violations,
                                       pre_merge_sha, call_type=None,
                                       iteration_count=None):
            raise RuntimeError("disk full")

        monkeypatch.setattr(
            "se3.engine.merge.human_call.HumanCallWriter.write_guardrail_call",
            mock_write_guardrail_call,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature-stall-call-fail"])

        # Should abort without human call (call file could not be written)
        assert report.success is False
        assert report.failure_reason == "guardrail_repair_stalled_call_failed"
        assert report.pending_human is False
        assert report.human_call_file is None
        assert report.rollback_failed is False

        # HEAD should be restored
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head == pre_head

    def test_execute_fast_stalled_call_file_keyboard_interrupt_propagates(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """KeyboardInterrupt during call-file-write propagates; tree already rolled back.

        The bare ``except Exception`` at the stalled call-file-write path does
        NOT catch ``KeyboardInterrupt`` or ``SystemExit``. Those propagate
        upward. The working tree is already rolled back at that point, so the
        state is consistent even though the report.failure_reason is never set.
        This test documents and hardens that contract.
        """
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-stall-keyboard")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)
        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to always fail
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=False,
                error="LLM could not fix",
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock guardrails check to always return the SAME violation
        def mock_check(self, pre_sha: str, post_sha: str):
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

        # Monkeypatch HumanCallWriter.write_guardrail_call to raise KeyboardInterrupt
        def mock_write_guardrail_call(self, branch, violations,
                                       pre_merge_sha, call_type=None,
                                       iteration_count=None):
            raise KeyboardInterrupt("user pressed Ctrl+C")

        monkeypatch.setattr(
            "se3.engine.merge.human_call.HumanCallWriter.write_guardrail_call",
            mock_write_guardrail_call,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        with pytest.raises(KeyboardInterrupt):
            orch.execute(["feature-stall-keyboard"])

        # The exception propagated, but the working tree should already be
        # rolled back (rollback happens before the call-file-write attempt).
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head == pre_head

    def test_execute_fast_max_iterations_exhausted_escalates_to_human(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """execute() with fast strategy: each iteration produces a novel hash.

        When violation hashes keep changing but never clear, the loop reaches
        max iterations.  It should escalate to a human call (not abort
        outright), consistent with the stall path.
        """
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-exhaust")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)
        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to always fail
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=False,
                error="LLM could not fix",
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock guardrails check to return a DIFFERENT violation each time so
        # hashes are always novel and never stall.
        check_call_count = [0]

        def mock_check(self, pre_sha: str, post_sha: str):
            check_call_count[0] += 1
            evidence = {
                "strong_line": f"The system SHALL validate inputs v{check_call_count[0]}.",
                "weak_line": f"The system SHOULD validate inputs v{check_call_count[0]}.",
                "pairing_score": 0.9,
            }
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/base/spec.md",
                        violation_type="WEAKENING",
                        message="SHALL weakened to SHOULD",
                        evidence=evidence,
                    ),
                ],
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature-exhaust"])

        # Should escalate to human call (not fast_abort)
        assert report.success is False
        assert report.failure_reason == "guardrail_repair_exhausted"
        assert report.pending_human is True
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # Call file type
        data = json.loads(report.human_call_file.read_text())
        assert data["type"] == "guardrail_repair_exhausted"

        # HEAD should be restored
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head == pre_head

    def test_execute_fast_exhausted_call_file_write_fails_aborts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """execute() with fast strategy: exhausted repair + call file write fails -> abort.

        Mirrors test_execute_fast_stalled_call_file_write_fails_aborts for the
        exhausted path (lines 1972-1980 in orchestrator.py).
        """
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-exhaust-call-fail")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)
        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to always fail
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=False,
                error="LLM could not fix",
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock guardrails check to return a DIFFERENT violation each time so
        # hashes are always novel (never stall) and the exhausted path is reached.
        check_call_count = [0]

        def mock_check(self, pre_sha: str, post_sha: str):
            check_call_count[0] += 1
            evidence = {
                "strong_line": f"The system SHALL validate inputs v{check_call_count[0]}.",
                "weak_line": f"The system SHOULD validate inputs v{check_call_count[0]}.",
                "pairing_score": 0.9,
            }
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/base/spec.md",
                        violation_type="WEAKENING",
                        message="SHALL weakened to SHOULD",
                        evidence=evidence,
                    ),
                ],
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        # Monkeypatch HumanCallWriter.write_guardrail_call to raise ONLY for exhausted
        original_write = None

        def mock_write_guardrail_call(self, branch, violations,
                                       pre_merge_sha, call_type=None,
                                       iteration_count=None):
            if call_type == "guardrail_repair_exhausted":
                raise RuntimeError("disk full")
            # Fall through to the real implementation for other call types.
            # This should never happen in this test, but defends against drift.
            if original_write is None:
                raise RuntimeError("original_write not captured")
            return original_write(self, branch, violations, pre_merge_sha,
                                   call_type=call_type, iteration_count=iteration_count)

        monkeypatch.setattr(
            "se3.engine.merge.human_call.HumanCallWriter.write_guardrail_call",
            mock_write_guardrail_call,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature-exhaust-call-fail"])

        # Should abort without human call (call file could not be written)
        assert report.success is False
        assert report.failure_reason == "guardrail_repair_exhausted_call_failed"
        assert report.pending_human is False
        assert report.human_call_file is None
        assert report.rollback_failed is False

        # HEAD should be restored
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head == pre_head

    def test_execute_fast_stalled_rollback_fails_sets_rollback_failed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """execute() with fast strategy: stalled repair + rollback fails -> rollback_failed.

        When _rollback_to(pre_sha) raises in the stall handler, the code
        still writes the human call file with diagnostic evidence and raises
        GuardrailRollbackError (a RuntimeError) carrying the call_file.
        execute() routes this to rollback_failed with report.human_call_file set.
        """
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-stall-rollback-fail")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)
        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to always fail
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=False,
                error="LLM could not fix",
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock guardrails check to always return the SAME violation
        def mock_check(self, pre_sha: str, post_sha: str):
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/base/spec.md",
                        violation_type="WEAKENING",
                        message="SHALL weakened to SHOULD",
                        evidence={
                            "strong_line": "The system SHALL validate all user inputs.",
                            "weak_line": "The system SHOULD validate all user inputs.",
                            "pairing_score": 0.92,
                        },
                    ),
                ],
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        # Monkeypatch _rollback_to to raise when called from the stall path
        def mock_rollback_to(self, sha: str) -> None:
            raise RuntimeError(
                f"git reset --hard {sha} failed: simulated rollback failure"
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.MergeOrchestrator._rollback_to",
            mock_rollback_to,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature-stall-rollback-fail"])

        # Should report rollback_failed=True and surface the diagnostic call file
        assert report.success is False
        assert report.failure_reason == "rollback_failed"
        assert report.rollback_failed is True
        assert report.pending_human is False
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # Call file should contain the violation evidence
        data = json.loads(report.human_call_file.read_text())
        assert data["type"] == "guardrail_repair_stalled"
        assert len(data["violations"]) >= 1
        assert data["violations"][0]["evidence"]["strong_line"] == (
            "The system SHALL validate all user inputs."
        )

    def test_execute_fast_exhausted_rollback_fails_sets_rollback_failed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """execute() with fast strategy: exhausted repair + rollback fails -> rollback_failed.

        When _rollback_to(pre_sha) raises in the exhausted handler, the code
        still writes the human call file with diagnostic evidence and raises
        GuardrailRollbackError (a RuntimeError) carrying the call_file.
        execute() routes this to rollback_failed with report.human_call_file set.
        """
        default_branch = _setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-exhaust-rollback-fail")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)
        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to always fail
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=False,
                error="LLM could not fix",
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock guardrails check to return a DIFFERENT violation each time so
        # hashes are always novel (never stall) and the exhausted path is reached.
        check_call_count = [0]

        def mock_check(self, pre_sha: str, post_sha: str):
            check_call_count[0] += 1
            evidence = {
                "strong_line": f"The system SHALL validate inputs v{check_call_count[0]}.",
                "weak_line": f"The system SHOULD validate inputs v{check_call_count[0]}.",
                "pairing_score": 0.9,
            }
            return GuardrailReport(
                passed=False,
                violations=[
                    GuardrailViolation(
                        file_path="se3/specs/base/spec.md",
                        violation_type="WEAKENING",
                        message="SHALL weakened to SHOULD",
                        evidence=evidence,
                    ),
                ],
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrails.MergeGuardrailsCheck.check_merge_result",
            mock_check,
        )

        # Monkeypatch _rollback_to to raise when called from the exhausted path
        def mock_rollback_to(self, sha: str) -> None:
            raise RuntimeError(
                f"git reset --hard {sha} failed: simulated rollback failure"
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.MergeOrchestrator._rollback_to",
            mock_rollback_to,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature-exhaust-rollback-fail"])

        # Should report rollback_failed=True and surface the diagnostic call file
        assert report.success is False
        assert report.failure_reason == "rollback_failed"
        assert report.rollback_failed is True
        assert report.pending_human is False
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        # Call file should contain violation evidence
        data = json.loads(report.human_call_file.read_text())
        assert data["type"] == "guardrail_repair_exhausted"
        assert len(data["violations"]) >= 1


class TestMaxRepairIterationsClamp:
    """Regression tests for the orchestrator's defense-in-depth clamp on
    ``_max_repair_iterations``.

    The loader (`_load_max_repair_iterations`) already clamps invalid
    values to the module default, but the orchestrator's exhausted-path
    correctness depends on the for-loop body running at least once
    (otherwise the after-loop reference to the ``iteration`` variable
    would raise UnboundLocalError before reaching the explicit
    ``iteration_completed`` counter introduced for the same defense).
    These tests pin the clamp at the orchestrator level so a future
    refactor / monkeypatched loader that returns 0 still produces a
    clean repair-loop instead of an undefined-name crash.
    """

    def test_orchestrator_clamps_loader_returning_zero(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """If the loader returns 0 (clamp bypassed), the orchestrator
        re-clamps to the module default so the for-loop body runs at
        least once."""
        _setup_spec_repo(tmp_path)

        # Force the loader to return 0 — emulates a future refactor
        # that drops the loader's own ``< 1`` clamp.
        monkeypatch.setattr(
            "se3.engine.merge.orchestrator._load_max_repair_iterations",
            lambda project_root: 0,
        )

        from se3.engine.merge.orchestrator import (
            _DEFAULT_MAX_REPAIR_ITERATIONS,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        # The orchestrator's defensive clamp must promote the 0 to a
        # positive value (the module default).  A test that asserts
        # ``> 0`` rather than ``== _DEFAULT_MAX_REPAIR_ITERATIONS``
        # tolerates a future change to the default constant.
        assert orch._max_repair_iterations >= 1
        assert orch._max_repair_iterations == _DEFAULT_MAX_REPAIR_ITERATIONS

    def test_orchestrator_clamps_loader_returning_negative(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Negative values from a hypothetical buggy loader are also
        clamped — the for-loop body must execute at least once."""
        _setup_spec_repo(tmp_path)

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator._load_max_repair_iterations",
            lambda project_root: -3,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        assert orch._max_repair_iterations >= 1

    def test_orchestrator_preserves_loader_returning_positive(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A positive value from the loader is preserved verbatim — the
        clamp only kicks in for non-positive values."""
        _setup_spec_repo(tmp_path)

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator._load_max_repair_iterations",
            lambda project_root: 7,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        assert orch._max_repair_iterations == 7
