"""Tests for merge guardrails integration.

Covers:
- check_spec_diff pure function
- MergeGuardrailsCheck.check_merge_result with real git refs
- Orchestrator integration: rollback + HUMAN_CALL on violations
- Strategy matrix: default/strict/fast all enforce spec guardrails
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from se3.engine.merge.guardrails import (
    GuardrailViolation,
    MergeGuardrailsCheck,
    check_spec_diff,
)
from se3.engine.merge.orchestrator import MergeOrchestrator


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


# --------- check_spec_diff unit tests ---------


class TestCheckSpecDiff:
    def test_no_violations_identical_content(self) -> None:
        text = "# Spec\n\n## Requirement: Foo\n\n- SHALL do X\n- MUST verify all inputs\n"
        violations = check_spec_diff(text, text)
        assert violations == []

    def test_shall_to_should_detected(self) -> None:
        original = "The system SHALL validate inputs."
        new = "The system SHOULD validate inputs."
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "WEAKENING"
        assert "SHALL" in violations[0].message

    def test_must_to_should_detected(self) -> None:
        original = "The system MUST validate inputs."
        new = "The system SHOULD validate inputs."
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "WEAKENING"
        assert "MUST" in violations[0].message

    def test_required_to_recommended_detected(self) -> None:
        original = "It is REQUIRED to check."
        new = "It is RECOMMENDED to check."
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert "REQUIRED" in violations[0].message

    def test_all_to_some_quantifier_detected(self) -> None:
        original = "Validate all inputs."
        new = "Validate some inputs."
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert "quantifier" in violations[0].message
        assert "all" in violations[0].message

    def test_every_to_some_quantifier_detected(self) -> None:
        original = "Check every field."
        new = "Check some field."
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert "every" in violations[0].message

    def test_when_clause_deletion_detected(self) -> None:
        original = "#### Scenario: A\n- WHEN X\n- THEN Y\n\n#### Scenario: B\n- WHEN Z\n- THEN W\n"
        new = "#### Scenario: A\n- WHEN X\n- THEN Y\n"
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "DELETE"
        assert "WHEN" in violations[0].message

    def test_multiple_violations(self) -> None:
        original = "The system SHALL validate all inputs. MUST check every field."
        new = "The system SHOULD validate some inputs. MAY check some field."
        violations = check_spec_diff(original, new)
        assert len(violations) == 4  # SHALL→SHOULD, all→some, MUST→MAY, every→some
        types = [v.violation_type for v in violations]
        assert types.count("WEAKENING") == 4

    def test_file_path_passed_through(self) -> None:
        violations = check_spec_diff("SHALL x", "SHOULD x", file_path="se3/specs/test/spec.md")
        assert violations[0].file_path == "se3/specs/test/spec.md"

    def test_no_false_positive_when_both_present(self) -> None:
        """If both strong and weak are in new text, it's not a weakening."""
        original = "SHALL do X."
        new = "SHALL do X. SHOULD do Y."
        violations = check_spec_diff(original, new)
        assert violations == []

    def test_partial_weakening_one_of_n_shall_detected(self) -> None:
        """Changing one of multiple SHALL→SHOULD is now detected."""
        original = (
            "The system SHALL validate inputs.\n"
            "The system SHALL check permissions.\n"
        )
        new = (
            "The system SHALL validate inputs.\n"
            "The system SHOULD check permissions.\n"
        )
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "WEAKENING"
        assert "SHALL" in violations[0].message

    def test_when_swap_detected(self) -> None:
        """Deleting one WHEN while adding an unrelated WHEN is now detected."""
        original = (
            "#### Scenario: A\n"
            "- WHEN user logs in\n"
            "- THEN redirect\n\n"
            "#### Scenario: B\n"
            "- WHEN user registers\n"
            "- THEN welcome\n"
        )
        new = (
            "#### Scenario: A\n"
            "- WHEN user logs in\n"
            "- THEN redirect\n\n"
            "#### Scenario: C\n"
            "- WHEN user deletes account\n"
            "- THEN confirm\n"
        )
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "DELETE"
        assert "WHEN" in violations[0].message
        assert "1" in violations[0].message

    def test_same_line_partial_shall_weakening_detected(self) -> None:
        """When one SHALL on a line is weakened but another remains, the
        occurrence-level counting must detect it (per-line counting would miss)."""
        original = "SHALL validate inputs and SHALL check permissions.\n"
        new = "SHALL validate inputs and SHOULD check permissions.\n"
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "WEAKENING"
        assert "SHALL" in violations[0].message

    def test_net_zero_weakening_detected(self) -> None:
        """One SHALL→SHOULD plus a brand-new SHALL elsewhere: net-zero count
        but a real weakening occurred. The line-content fallback must catch it."""
        original = (
            "The system SHALL validate inputs.\n"
            "The system SHALL check permissions.\n"
        )
        new = (
            "The system SHALL validate inputs.\n"
            "The system SHOULD check permissions.\n"
            "The system SHALL log errors.\n"
        )
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "WEAKENING"
        assert "SHALL" in violations[0].message

    def test_net_zero_quantifier_weakening_detected(self) -> None:
        """One all→some plus a new all elsewhere: net-zero count but real weakening."""
        original = "Validate all inputs.\nCheck all fields.\n"
        new = "Validate some inputs.\nCheck all fields.\nLog all events.\n"
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "WEAKENING"
        assert "quantifier" in violations[0].message



# --------- MergeGuardrailsCheck integration tests ---------


class TestMergeGuardrailsCheck:
    def test_no_spec_files_changed_passes(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        (tmp_path / "README.md").write_text("# Hello\n")
        _commit(tmp_path, "initial")
        base_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        (tmp_path / "README.md").write_text("# Hello World\n")
        _commit(tmp_path, "update readme")
        head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(base_sha, head_sha)
        assert report.passed is True
        assert report.violations == []

    def test_spec_weakening_detected(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text("## Requirement: X\n\nThe system SHALL validate all inputs.\n")
        _commit(tmp_path, "initial with spec")
        base_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        (spec_dir / "spec.md").write_text("## Requirement: X\n\nThe system SHOULD validate all inputs.\n")
        _commit(tmp_path, "weaken spec")
        head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(base_sha, head_sha)
        assert report.passed is False
        assert len(report.violations) == 1
        assert report.violations[0].violation_type == "WEAKENING"
        assert "SHALL" in report.violations[0].message

    def test_new_spec_file_no_violation(self, tmp_path: Path) -> None:
        """Adding a new spec file (no original) should not trigger violations."""
        _init_repo(tmp_path)
        (tmp_path / "README.md").write_text("# Hello\n")
        _commit(tmp_path, "initial")
        base_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        spec_dir = tmp_path / "se3" / "specs" / "new"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text("## Requirement: Y\n\nThe system SHALL do Y.\n")
        _commit(tmp_path, "add new spec")
        head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(base_sha, head_sha)
        assert report.passed is True

    def test_when_deletion_detected(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text(
            "## Requirement: Z\n\n"
            "#### Scenario: A\n- WHEN X\n- THEN Y\n\n"
            "#### Scenario: B\n- WHEN Z\n- THEN W\n"
        )
        _commit(tmp_path, "initial with scenarios")
        base_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        (spec_dir / "spec.md").write_text(
            "## Requirement: Z\n\n"
            "#### Scenario: A\n- WHEN X\n- THEN Y\n"
        )
        _commit(tmp_path, "delete scenario")
        head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(base_sha, head_sha)
        assert report.passed is False
        assert any(v.violation_type == "DELETE" for v in report.violations)

    def test_quantifier_weakening_detected(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text("## Requirement: Q\n\nValidate all inputs.\n")
        _commit(tmp_path, "initial")
        base_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        (spec_dir / "spec.md").write_text("## Requirement: Q\n\nValidate some inputs.\n")
        _commit(tmp_path, "weaken quantifier")
        head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(base_sha, head_sha)
        assert report.passed is False
        assert any("quantifier" in v.message for v in report.violations)


# --------- Orchestrator integration tests ---------


class TestOrchestratorGuardrailsIntegration:
    """Test that guardrails violations trigger rollback + HUMAN_CALL."""

    def _setup_repo_with_spec(self, tmp_path: Path) -> str:
        """Create repo with a spec file. Returns default branch name."""
        _init_repo(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHALL validate all user inputs.\n\n"
            "#### Scenario: Valid input\n"
            "- WHEN user provides valid data\n"
            "- THEN authentication succeeds\n"
        )
        (tmp_path / "code.py").write_text("def auth(): pass\n")
        _commit(tmp_path, "initial")
        return _current_branch(tmp_path)

    def _is_working_tree_clean(self, path: Path) -> bool:
        result = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, check=True,
        )
        return not result.stdout.strip()

    def test_clean_merge_spec_weakening_rollback_and_human_call(self, tmp_path: Path) -> None:
        """Clean merge that weakens a spec → rolled back + HUMAN_CALL."""
        default_branch = self._setup_repo_with_spec(tmp_path)

        # Create feature branch that weakens SHALL → SHOULD in spec
        _git(tmp_path, "checkout", "-b", "feature-weaken")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n\n"
            "#### Scenario: Valid input\n"
            "- WHEN user provides valid data\n"
            "- THEN authentication succeeds\n"
        )
        _commit(tmp_path, "weaken spec language")

        # Go back to default and merge
        _git(tmp_path, "checkout", default_branch)
        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature-weaken"])

        # Should fail with guardrail_violation
        assert report.success is False
        assert report.failed_branch == "feature-weaken"
        assert report.failure_reason == "guardrail_violation"
        assert report.pending_human is True

        # Working tree should be clean after rollback
        assert self._is_working_tree_clean(tmp_path) is True

        # HEAD should be back to pre-merge state
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head == pre_head

        # Spec should be back to original (SHALL)
        assert "SHALL" in spec_path.read_text()
        assert "SHOULD" not in spec_path.read_text()

        # Human call file should exist
        calls_dir = tmp_path / "se3" / "calls"
        call_files = list(calls_dir.glob("merge_*_guardrail.json"))
        assert len(call_files) == 1
        data = json.loads(call_files[0].read_text())
        assert data["type"] == "guardrail_violation"
        assert data["branch"] == "feature-weaken"
        assert len(data["violations"]) >= 1

    def test_conflict_accept_spec_weakening_rollback_and_human_call(self, tmp_path: Path, monkeypatch) -> None:
        """Conflict-ACCEPT path with spec weakening → rolled back + HUMAN_CALL."""
        default_branch = self._setup_repo_with_spec(tmp_path)

        # Set up a conflict on the spec file
        _git(tmp_path, "checkout", "-b", "feature-conflict")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n\n"  # weakened
            "#### Scenario: Valid input\n"
            "- WHEN user provides valid data\n"
            "- THEN authentication succeeds\n"
        )
        _commit(tmp_path, "weaken spec on feature")

        _git(tmp_path, "checkout", default_branch)
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHALL validate all user inputs.\n\n"
            "#### Scenario: Valid input\n"
            "- WHEN user provides valid data\n"
            "- THEN authentication succeeds\n"
            "#### Scenario: New\n"
            "- WHEN x\n- THEN y\n"
        )
        _commit(tmp_path, "extend spec on main")

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock LLM resolver to ACCEPT with resolved content that still has the weakening
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="se3/specs/base/spec.md",
                        resolved_content=spec_path.read_text().replace("SHALL", "SHOULD"),
                        hunks=[HunkResolution(1, 10, Confidence.HIGH, "merged")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=True,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature-conflict"])

        # Should fail with guardrail_violation
        assert report.success is False
        assert report.failed_branch == "feature-conflict"
        assert report.failure_reason == "guardrail_violation"
        assert report.pending_human is True

        # HEAD should be back to pre-merge state
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head == pre_head

        # Working tree should be clean
        assert self._is_working_tree_clean(tmp_path) is True

        # Human call file should exist
        calls_dir = tmp_path / "se3" / "calls"
        call_files = list(calls_dir.glob("merge_*_guardrail.json"))
        assert len(call_files) == 1

    def test_fast_strategy_spec_weakening_llm_repair_success(self, tmp_path: Path) -> None:
        """fast strategy + spec weakening → LLM repair → merge succeeds.

        In fast mode, guardrail violations are sent to the LLM for repair
        instead of rolling back and creating a human call. If the LLM
        successfully restores the weakened requirement, the merge proceeds.
        """
        default_branch = self._setup_repo_with_spec(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-fast")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n\n"
            "#### Scenario: Valid input\n"
            "- WHEN user provides valid data\n"
            "- THEN authentication succeeds\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)
        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature-fast"])

        # Fast mode: LLM repairs the violation, merge succeeds
        assert report.success is True, (
            f"Expected success after LLM repair, got failure_reason={report.failure_reason}"
        )
        assert "feature-fast" in report.merged_branches
        assert report.pending_human is False
        assert report.human_call_file is None

        # HEAD should have changed (merge commit created and amended)
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head != pre_head

        # Spec should have been repaired (SHALL restored)
        spec_content = spec_path.read_text()
        assert "SHALL" in spec_content
        assert "SHOULD" not in spec_content

    def test_fast_strategy_spec_weakening_llm_repair_failure_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """fast strategy + spec weakening + LLM repair fails → abort, no human call."""
        default_branch = self._setup_repo_with_spec(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-fast")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n\n"
            "#### Scenario: Valid input\n"
            "- WHEN user provides valid data\n"
            "- THEN authentication succeeds\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)
        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to always fail
        def mock_repair(self, branch, pre_sha, post_sha, violations, original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(success=False, error="LLM could not fix")

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature-fast"])

        # Fast mode: repair failed, merge aborts, no human call
        assert report.success is False
        assert report.failure_reason == "guardrail_repair_failed"
        assert report.pending_human is False
        assert report.human_call_file is None

        # HEAD restored
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head == pre_head

    def test_default_regular_text_accept_spec_weakening_rejected(self, tmp_path: Path, monkeypatch) -> None:
        """default strategy: regular file ACCEPT + spec weakening → spec rejected."""
        default_branch = self._setup_repo_with_spec(tmp_path)

        # Create feature that changes both a regular file and weakens spec
        _git(tmp_path, "checkout", "-b", "feature-mixed")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n\n"
            "#### Scenario: Valid input\n"
            "- WHEN user provides valid data\n"
            "- THEN authentication succeeds\n"
        )
        (tmp_path / "code.py").write_text("def auth(): return True\n")
        _commit(tmp_path, "mixed changes")

        _git(tmp_path, "checkout", default_branch)
        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature-mixed"])

        # Spec weakening should cause rollback even though regular file is fine
        assert report.success is False
        assert report.failure_reason == "guardrail_violation"
        assert report.pending_human is True

        # HEAD restored
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head == pre_head

        # code.py should NOT have the feature change (rolled back)
        assert "return True" not in (tmp_path / "code.py").read_text()

    def test_second_branch_violation_first_preserved(self, tmp_path: Path) -> None:
        """First branch merges cleanly, second has spec violation → first kept, second rolled back."""
        default_branch = self._setup_repo_with_spec(tmp_path)

        # feature-a: clean merge, no spec changes
        _git(tmp_path, "checkout", "-b", "feature-a")
        (tmp_path / "a.txt").write_text("a\n")
        _commit(tmp_path, "add a")
        _git(tmp_path, "checkout", default_branch)

        # feature-b: weakens spec
        _git(tmp_path, "checkout", "-b", "feature-b")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n\n"
            "#### Scenario: Valid input\n"
            "- WHEN user provides valid data\n"
            "- THEN authentication succeeds\n"
        )
        _commit(tmp_path, "weaken spec")
        _git(tmp_path, "checkout", default_branch)

        # After feature-a merges, HEAD moves; pre-head for feature-b is post-feature-a
        pre_all = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature-a", "feature-b"])

        assert report.success is False
        assert "feature-a" in report.merged_branches
        assert report.failed_branch == "feature-b"
        assert report.failure_reason == "guardrail_violation"

        # feature-a's file should still exist
        assert (tmp_path / "a.txt").exists()

        # But spec should be unchanged from before feature-b (SHALL preserved)
        assert "SHALL" in spec_path.read_text()
        assert "SHOULD" not in spec_path.read_text()

        # HEAD should be after feature-a merge (feature-b rolled back)
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        # HEAD should have changed from pre_all because feature-a was merged
        assert post_head != pre_all

    def test_clean_merge_no_spec_change_passes(self, tmp_path: Path) -> None:
        """Clean merge with no spec changes should succeed."""
        default_branch = self._setup_repo_with_spec(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-clean")
        (tmp_path / "new.py").write_text("def new(): pass\n")
        _commit(tmp_path, "add new file")

        _git(tmp_path, "checkout", default_branch)

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature-clean"])

        assert report.success is True
        assert "feature-clean" in report.merged_branches
        assert report.pending_human is False

    def test_llm_resolves_spec_conflict_but_when_deleted_rollback_and_human_call(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """LLM resolves a spec conflict but drops a WHEN scenario.
        Guardrails flag DELETE → rollback + human_call_file on report."""
        default_branch = self._setup_repo_with_spec(tmp_path)

        # To force a REAL conflict (not auto-merged by git), both branches must
        # modify the SAME line.  Feature weakens SHALL→SHOULD on line 3.
        # Main also modifies line 3 (adds text) and adds a new scenario at the end.
        _git(tmp_path, "checkout", "-b", "feature-delete-when")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n\n"
            "#### Scenario: Valid input\n"
            "- WHEN user provides valid data\n"
            "- THEN authentication succeeds\n"
        )
        _commit(tmp_path, "weaken spec on feature")

        _git(tmp_path, "checkout", default_branch)
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHALL validate all user inputs and log events.\n\n"
            "#### Scenario: Valid input\n"
            "- WHEN user provides valid data\n"
            "- THEN authentication succeeds\n"
            "#### Scenario: New\n"
            "- WHEN x\n- THEN y\n"
        )
        _commit(tmp_path, "extend spec on main")

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock LLM resolver: returns content that keeps SHALL but drops the New scenario.
        # Because both branches touched line 3, git will conflict; the LLM resolves it.
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="se3/specs/base/spec.md",
                        resolved_content=(
                            "## Requirement: Auth\n\n"
                            "The system SHALL validate all user inputs.\n\n"
                            "#### Scenario: Valid input\n"
                            "- WHEN user provides valid data\n"
                            "- THEN authentication succeeds\n"
                        ),
                        hunks=[HunkResolution(1, 12, Confidence.HIGH, "merged")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=True,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature-delete-when"])

        # Should fail with guardrail_violation
        assert report.success is False
        assert report.failed_branch == "feature-delete-when"
        assert report.failure_reason == "guardrail_violation"
        assert report.pending_human is True

        # HEAD should be rolled back to pre-merge state
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head == pre_head

        # Working tree should be clean
        assert self._is_working_tree_clean(tmp_path) is True

        # report.human_call_file must be set (the key fix)
        assert report.human_call_file is not None
        assert report.human_call_file.exists()
        data = json.loads(report.human_call_file.read_text())
        assert data["type"] == "guardrail_violation"
        assert data["branch"] == "feature-delete-when"
        # Should contain a DELETE violation for the WHEN scenario
        assert any(v["violation_type"] == "DELETE" for v in data["violations"])

    def test_llm_resolves_spec_conflict_but_shall_weakened_rollback_and_human_call(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """LLM resolves a spec conflict but weakens SHALL → SHOULD.
        Guardrails flag WEAKENING → rollback + human_call_file on report."""
        default_branch = self._setup_repo_with_spec(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-weaken-shall")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n\n"
            "#### Scenario: Valid input\n"
            "- WHEN user provides valid data\n"
            "- THEN authentication succeeds\n"
        )
        _commit(tmp_path, "weaken spec on feature")

        _git(tmp_path, "checkout", default_branch)
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHALL validate all user inputs.\n\n"
            "#### Scenario: Valid input\n"
            "- WHEN user provides valid data\n"
            "- THEN authentication succeeds\n"
            "#### Scenario: New\n"
            "- WHEN x\n- THEN y\n"
        )
        _commit(tmp_path, "extend spec on main")

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock LLM resolver: returns content with SHOULD (weakening)
        def mock_resolve(self, context, strategy):
            from se3.engine.merge.conflict_resolver import (
                Confidence, FileResolution, HunkResolution, LLMResolution,
            )
            return LLMResolution(
                files=[
                    FileResolution(
                        path="se3/specs/base/spec.md",
                        resolved_content=(
                            "## Requirement: Auth\n\n"
                            "The system SHOULD validate all user inputs.\n\n"
                            "#### Scenario: Valid input\n"
                            "- WHEN user provides valid data\n"
                            "- THEN authentication succeeds\n"
                            "#### Scenario: New\n"
                            "- WHEN x\n- THEN y\n"
                        ),
                        hunks=[HunkResolution(1, 14, Confidence.HIGH, "merged")],
                        overall_confidence=Confidence.HIGH,
                        flags={"requires_human_review": False, "spec_guardrail_concern": False},
                        is_spec=True,
                    ),
                ],
                overall_confidence=Confidence.HIGH,
                flags={"requires_human_review": False, "spec_guardrail_concern": False},
            )

        monkeypatch.setattr(
            "se3.engine.merge.orchestrator.ConflictResolver.resolve", mock_resolve
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="default")
        report = orch.execute(["feature-weaken-shall"])

        assert report.success is False
        assert report.failed_branch == "feature-weaken-shall"
        assert report.failure_reason == "guardrail_violation"
        assert report.pending_human is True
        assert report.human_call_file is not None

        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head == pre_head

        data = json.loads(report.human_call_file.read_text())
        assert any(v["violation_type"] == "WEAKENING" for v in data["violations"])

    def test_guardrail_check_uses_ref_not_worktree(self, tmp_path: Path) -> None:
        """Verify check_merge_result reads from git refs, not working tree."""
        _init_repo(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text("The system SHALL do X.\n")
        _commit(tmp_path, "initial")
        base_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        (spec_dir / "spec.md").write_text("The system SHOULD do X.\n")
        _commit(tmp_path, "weaken")
        head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Now modify working tree to something else
        (spec_dir / "spec.md").write_text("The system MUST do X.\n")

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(base_sha, head_sha)

        # Should still detect the SHALL→SHOULD weakening from the commits,
        # NOT be confused by the working tree MUST
        assert report.passed is False
        assert any("SHALL" in v.message for v in report.violations)
