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

from se3.engine.merge.conflict_resolver import MergeStrategy
from se3.engine.merge.guardrails import (
    GuardrailViolation,
    MergeGuardrailsCheck,
    _get_changed_spec_files,
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

    # --- Pairing-based corner-case tests ---

    def test_extend_shall_plus_unrelated_weak_no_false_positive(self) -> None:
        """Extending a SHALL + pre-existing unrelated weak line elsewhere.

        Original has a weak line (SHOULD B) and two strong lines (SHALL A, SHALL C).
        New text extends SHALL A to SHALL A. and adds a new MUST B (strong).
        The original SHOULD B is still present. This is a legitimate extension,
        not a weakening — the detector must NOT report WEAKENING.
        """
        original = (
            "The system SHALL validate inputs.\n"
            "The system SHOULD check permissions.\n"
            "The system SHALL log errors.\n"
        )
        new = (
            "The system SHALL validate inputs and sanitize them.\n"
            "The system SHOULD check permissions.\n"
            "The system SHALL log errors.\n"
            "The system MUST audit events.\n"
        )
        violations = check_spec_diff(original, new)
        assert violations == []

    def test_real_shall_to_may_weakening_detected(self) -> None:
        """A genuine SHALL→MAY weakening without any offsetting addition.

        The detector should report WEAKENING via the fast path
        (strong count drops, weak count rises).
        """
        original = "The system SHALL validate inputs.\n"
        new = "The system MAY validate inputs.\n"
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "WEAKENING"
        assert "SHALL" in violations[0].message
        assert violations[0].evidence is not None
        assert "strong_line" in violations[0].evidence
        assert "weak_line" in violations[0].evidence
        assert "pairing_score" in violations[0].evidence
        assert violations[0].evidence["strong_line_no"] == 1
        assert violations[0].evidence["weak_line_no"] == 1

    def test_weakening_plus_new_shall_offset_caught_with_evidence(self) -> None:
        """One SHALL→SHOULD weakening + one new SHALL elsewhere.

        Net-zero count → corner-case path. The pairing logic must match the
        missing strong line with the weak-only line and fill evidence.
        """
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
        assert violations[0].evidence is not None
        assert "SHALL check permissions" in violations[0].evidence["strong_line"]
        assert "SHOULD check permissions" in violations[0].evidence["weak_line"]
        assert violations[0].evidence["pairing_score"] >= 0.5
        assert violations[0].evidence["strong_line_no"] == 2
        assert violations[0].evidence["weak_line_no"] == 2

    def test_extend_shall_only_with_preexisting_weak_no_false_positive(self) -> None:
        """Extending a SHALL line + pre-existing weak-only line elsewhere.

        Original has SHALL A and an unrelated SHOULD B. New text only extends
        SHALL A to a longer sentence. No new strong/weak lines are added.
        The pre-existing SHOULD B is unchanged. Detector must NOT report.
        """
        original = (
            "The system SHALL validate inputs.\n"
            "The system SHOULD check permissions.\n"
        )
        new = (
            "The system SHALL validate inputs and sanitize them.\n"
            "The system SHOULD check permissions.\n"
        )
        violations = check_spec_diff(original, new)
        assert violations == []

    def test_quantifier_all_to_some_plus_unrelated_some_no_false_positive(self) -> None:
        """Extending 'all' + pre-existing unrelated 'some' line elsewhere.

        Original has an unrelated 'some' line. New text adds another 'all' line.
        No actual quantifier weakening occurred — detector must NOT report.
        """
        original = (
            "Validate all inputs.\n"
            "Do some logging.\n"
        )
        new = (
            "Validate all inputs.\n"
            "Do some logging.\n"
            "Check all fields.\n"
        )
        violations = check_spec_diff(original, new)
        assert violations == []

    def test_extend_all_only_with_preexisting_some_no_false_positive(self) -> None:
        """Extending an 'all' line + pre-existing 'some' line elsewhere.

        Original has 'all A' and an unrelated 'some B'. New text only extends
        'all A' to a longer sentence. No new quantifier lines are added.
        The pre-existing 'some B' is unchanged. Detector must NOT report.
        """
        original = (
            "Validate all inputs.\n"
            "Do some logging.\n"
        )
        new = (
            "Validate all inputs and check boundaries.\n"
            "Do some logging.\n"
        )
        violations = check_spec_diff(original, new)
        assert violations == []

    def test_quantifier_real_all_to_some_weakening_detected(self) -> None:
        """Genuine all→some weakening with no offset."""
        original = "Validate all inputs.\n"
        new = "Validate some inputs.\n"
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "WEAKENING"
        assert "quantifier" in violations[0].message
        assert violations[0].evidence is not None
        assert violations[0].evidence["strong_line_no"] == 1
        assert violations[0].evidence["weak_line_no"] == 1

    def test_quantifier_weakening_plus_new_all_offset_caught(self) -> None:
        """One all→some weakening + one new all elsewhere.

        Net-zero count → corner-case path. Pairing logic must catch it.
        """
        original = (
            "Validate all inputs.\n"
            "Check all fields.\n"
        )
        new = (
            "Validate some inputs.\n"
            "Check all fields.\n"
            "Log all events.\n"
        )
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "WEAKENING"
        assert "quantifier" in violations[0].message
        assert violations[0].evidence is not None
        assert "all inputs" in violations[0].evidence["strong_line"]
        assert "some inputs" in violations[0].evidence["weak_line"]
        assert violations[0].evidence["strong_line_no"] == 1
        assert violations[0].evidence["weak_line_no"] == 1

    def test_preexisting_weak_line_sharing_tokens_no_false_positive(self) -> None:
        """Pre-existing weak line shares content tokens with extended strong line.

        Original has `SHALL validate inputs.` plus pre-existing
        `SHOULD validate inputs format.`. New extends to
        `SHALL validate inputs and sanitize them.` (SHOULD line unchanged).
        The missing-strong line tokens {validate, inputs} vs the pre-existing
        weak line tokens {validate, inputs, format} yield Jaccard 2/3 ≈ 0.667
        > 0.5. Without filtering out pre-existing weak lines, this would
        trigger a spurious WEAKENING.
        """
        original = (
            "The system SHALL validate inputs.\n"
            "The system SHOULD validate inputs format.\n"
        )
        new = (
            "The system SHALL validate inputs and sanitize them.\n"
            "The system SHOULD validate inputs format.\n"
        )
        violations = check_spec_diff(original, new)
        assert violations == []

    def test_preexisting_some_quantifier_sharing_tokens_no_false_positive(self) -> None:
        """Pre-existing 'some' line shares content tokens with extended 'all' line.

        Original has `Validate all inputs.` plus pre-existing
        `Check some input formats.`. New extends to
        `Validate all inputs and check boundaries.` (some line unchanged).
        Without filtering out pre-existing weak quantifier lines, this could
        trigger a spurious WEAKENING.
        """
        original = (
            "Validate all inputs.\n"
            "Check some input formats.\n"
        )
        new = (
            "Validate all inputs and check boundaries.\n"
            "Check some input formats.\n"
        )
        violations = check_spec_diff(original, new)
        assert violations == []

    def test_minimally_edited_weak_line_no_false_positive(self) -> None:
        """A pre-existing weak line with only whitespace/punctuation change
        should not re-enter the candidate set and trigger a false positive.
        """
        original = (
            "The system SHALL validate inputs.\n"
            "The system SHOULD validate inputs format.\n"
        )
        new = (
            "The system SHALL validate inputs and sanitize them.\n"
            "The system SHOULD validate inputs format. \n"  # note trailing space
        )
        violations = check_spec_diff(original, new)
        assert violations == []

    def test_corner_case_mixed_line_weakening_with_offsetting_new_shall(self) -> None:
        """In-place partial weakening (mixed line) + offsetting new strong.

        Original: one line with two SHALLs. New: same line with one weakened
        to SHOULD, plus a brand-new SHALL elsewhere. Net-zero count (2→2)
        but a real weakening occurred on the mixed line. The corner-case
        branch with allow_mixed_lines=True must detect it.
        """
        original = (
            "SHALL validate inputs and SHALL check permissions.\n"
        )
        new = (
            "SHALL validate inputs and SHOULD check permissions.\n"
            "SHALL log errors.\n"
        )
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "WEAKENING"
        assert "SHALL" in violations[0].message
        # Evidence should show the mixed-line pairing
        assert violations[0].evidence is not None
        assert "SHALL check permissions" in violations[0].evidence["strong_line"]
        assert "SHOULD check permissions" in violations[0].evidence["weak_line"]

    def test_corner_case_mixed_line_quantifier_with_offsetting_new_all(self) -> None:
        """In-place partial quantifier weakening (mixed line) + offsetting new 'all'.

        Original: one line with two 'all' quantifiers. New: same line with one
        weakened to 'some', plus a brand-new 'all' elsewhere. Net-zero count
        but real weakening on the mixed line.
        """
        original = (
            "Validate all inputs and check all fields.\n"
        )
        new = (
            "Validate all inputs and check some fields.\n"
            "Log all events.\n"
        )
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "WEAKENING"
        assert "quantifier" in violations[0].message
        assert violations[0].evidence is not None
        assert "check all fields" in violations[0].evidence["strong_line"]
        assert "check some fields" in violations[0].evidence["weak_line"]

    def test_fast_path_deleted_strong_with_preexisting_weak_reports_delete(self) -> None:
        """SHALL deleted; pre-existing unrelated SHOULD remains.

        The fast path (strong count drops, weak count > 0) must NOT fire
        WEAKENING when _compute_pairing_evidence returns None — this means
        there is no new weak-only line paired with the deleted strong line.
        The actual change is a deletion, so the violation type must be DELETE.
        """
        original = (
            "The system SHALL validate inputs.\n"
            "The system SHOULD check permissions.\n"
        )
        new = (
            "The system SHOULD check permissions.\n"
        )
        violations = check_spec_diff(original, new)
        # No new weak line paired with the deleted SHALL → DELETE.
        assert len(violations) == 1
        assert violations[0].violation_type == "DELETE"
        assert "SHALL" in violations[0].message
        # Evidence now includes the deleted line for diagnostic purposes.
        assert violations[0].evidence is not None
        assert "deleted_line" in violations[0].evidence
        assert "SHALL validate inputs" in violations[0].evidence["deleted_line"]
        assert violations[0].evidence["deleted_line_no"] == 1

    # --- Phase 2 (mixed-line) boundary tests ---

    def test_mixed_line_phase2_above_threshold_detected(self) -> None:
        """In-place partial weakening with Jaccard ~0.67 > 0.65 threshold.

        Original: one line with two SHALLs. New: mixed line (1 SHALL + 1 SHOULD)
        plus a new SHALL elsewhere. Net-zero strong count (2→2) forces
        corner-case branch. Phase 2 Jaccard = 4/6 ≈ 0.667 > 0.65 → detected.
        """
        original = (
            "SHALL validate user inputs and SHALL check fields.\n"
        )
        new = (
            "SHALL validate user inputs and SHOULD check permissions.\n"
            "SHALL log errors.\n"
        )
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "WEAKENING"
        assert violations[0].evidence is not None
        assert "pairing_score" in violations[0].evidence
        assert violations[0].evidence["pairing_score"] >= 0.65

    def test_mixed_line_phase2_below_threshold_not_detected(self) -> None:
        """In-place partial weakening with Jaccard ~0.6 < 0.65 threshold.

        Same structural pattern as above but with fewer shared tokens,
        pushing Jaccard below the 0.65 mixed-line threshold. The trade-off
        deliberately misses this to avoid false positives from genuine
        extensions that add unrelated tokens.
        """
        original = (
            "SHALL validate inputs and SHALL check fields.\n"
        )
        new = (
            "SHALL validate inputs and SHOULD check permissions.\n"
            "SHALL log errors.\n"
        )
        violations = check_spec_diff(original, new)
        # Below threshold → Phase 2 returns None, corner case not detected,
        # fast path doesn't fire (2 SHALL → 2 SHALL).
        assert violations == []

    def test_mixed_line_quantifier_phase2_above_threshold_detected(self) -> None:
        """Quantifier mixed-line weakening with Jaccard ~0.67 > 0.65 threshold."""
        original = (
            "Validate all user inputs and check all fields.\n"
        )
        new = (
            "Validate all user inputs and check some permissions.\n"
            "Log all events.\n"
        )
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "WEAKENING"
        assert "quantifier" in violations[0].message
        assert violations[0].evidence is not None
        assert violations[0].evidence["pairing_score"] >= 0.65

    def test_mixed_line_quantifier_phase2_below_threshold_not_detected(self) -> None:
        """Quantifier mixed-line weakening with Jaccard ~0.6 < 0.65 threshold."""
        original = (
            "Validate all inputs and check all fields.\n"
        )
        new = (
            "Validate all inputs and check some permissions.\n"
            "Log all events.\n"
        )
        violations = check_spec_diff(original, new)
        assert violations == []

    def test_keyword_position_swap_not_detected(self) -> None:
        """Keyword-position swap: strong keyword moves, weak keyword appears elsewhere.

        Documented asymmetry at guardrails.py:354-359: a swap like
        "SHALL log" -> "log SHOULD" produces orig_prefix_tokens=[] and
        mixed_prefix_tokens=["log"], falling through to prefix_score=0.
        This is intentional — position swaps indicate broader restructuring,
        not in-place weakening.

        To reach Phase 2 we need net-zero strong count (corner-case path).
        Original: 1 SHALL.  New: 1 SHALL + 1 SHOULD on a mixed line.
        The missing strong line and the mixed line pair via Jaccard (>0.65),
        but the prefix mismatch blocks the WEAKENING classification.
        """
        original = "SHALL log requests.\n"
        new = "log SHOULD requests and SHALL check.\n"
        violations = check_spec_diff(original, new)
        # No violation reported because the prefix mismatch treats the swap
        # as structural restructuring rather than in-place weakening.
        assert violations == []


    def test_weak_line_tokens_match_preexisting_weak_but_fast_path_catches(self) -> None:
        """Genuine weakening dropped by Phase 1 because new weak line shares
        content tokens with a pre-existing weak line (modulo role words and
        punctuation). The fast-path count-based detection still catches the
        violation because strong count decreases.

        Original has SHALL validate inputs. plus pre-existing
        SHOULD validate, inputs! (same tokens after stripping punctuation).
        New weakens to SHOULD validate inputs. — token-set matches the
        pre-existing weak line, so _weak_line_is_new returns False.
        Phase 1 produces empty weak_only. But fast path
        (strong_orig=1 > strong_new=0 and weak_new=2 > 0) still fires.
        """
        original = (
            "The system SHALL validate inputs.\n"
            "The system SHOULD validate, inputs!\n"
        )
        new = (
            "The system SHOULD validate inputs.\n"
            "The system SHOULD validate, inputs!\n"
        )
        violations = check_spec_diff(original, new)
        # Phase 1 filters out the new SHOULD because its tokens match the
        # pre-existing SHOULD. But fast path detects strong count drop.
        assert len(violations) >= 1
        # Without pairing evidence, the fast path classifies as DELETE.
        assert violations[0].violation_type in ("WEAKENING", "DELETE")

    def test_weak_line_tokens_match_preexisting_corner_case_not_detected(self) -> None:
        """Corner case: net-zero strong count + token-set dedup false negative.

        Original has SHALL validate inputs. plus pre-existing
        SHOULD validate, inputs! (same tokens after stripping punctuation).
        New weakens to SHOULD validate inputs. but adds a new SHALL elsewhere.
        strong_orig=1, strong_new=1, weak_new=2.

        Fast path (strong_orig > strong_new) is False.
        Corner case: _weak_line_is_new filters out the new SHOULD because
        its token set matches the pre-existing SHOULD. Phase 1 weak_only is
        empty. No pairing → no WEAKENING detected.

        This is a known defensive limitation of token-set deduplication.
        The fast path still catches the typical case (no offsetting strong).
        """
        original = (
            "The system SHALL validate inputs.\n"
            "The system SHOULD validate, inputs!\n"
        )
        new = (
            "The system SHOULD validate inputs.\n"
            "The system SHOULD validate, inputs!\n"
            "The system SHALL check permissions.\n"
        )
        violations = check_spec_diff(original, new)
        # Known limitation: corner case with offsetting strong + token-set
        # deduplication may produce no violation.
        # This test documents the current behavior for regression awareness.
        assert violations == []


class TestPairStrongWeakLines:
    """Unit tests for the _pair_strong_weak_lines helper."""

    def test_exact_equivalent_pairing(self) -> None:
        """Identical content (modulo role word) should pair perfectly."""
        from se3.engine.merge.guardrails import _pair_strong_weak_lines

        strong = ["The system SHALL validate inputs."]
        weak = ["The system SHOULD validate inputs."]
        pairings = _pair_strong_weak_lines(strong, weak)
        assert len(pairings) == 1
        assert "SHALL" in pairings[0][2]
        assert "SHOULD" in pairings[0][3]
        assert pairings[0][4] >= 0.5

    def test_extension_scenario_no_cross_pairing(self) -> None:
        """SHALL A → SHALL A. SHOULD B should NOT pair with SHOULD B."""
        from se3.engine.merge.guardrails import _pair_strong_weak_lines

        # If we extend SHALL A to SHALL A. and the new text has SHOULD B,
        # the missing strong is "SHALL A" and weak-only is "SHOULD B".
        # They are not similar enough to pair.
        strong = ["The system SHALL validate inputs."]
        weak = ["The system SHOULD check permissions."]
        pairings = _pair_strong_weak_lines(strong, weak)
        assert len(pairings) == 0

    def test_unrelated_weak_line_no_pairing(self) -> None:
        """An unrelated weak line in the file should not pair with missing strong."""
        from se3.engine.merge.guardrails import _pair_strong_weak_lines

        strong = ["The system SHALL validate inputs."]
        weak = ["Developers MAY use shortcuts."]
        pairings = _pair_strong_weak_lines(strong, weak)
        assert len(pairings) == 0

    def test_same_sentence_shall_to_should_pairs(self) -> None:
        """A direct SHALL→SHOULD on the same sentence should pair."""
        from se3.engine.merge.guardrails import _pair_strong_weak_lines

        strong = ["The system SHALL validate all user inputs."]
        weak = ["The system SHOULD validate all user inputs."]
        pairings = _pair_strong_weak_lines(strong, weak)
        assert len(pairings) == 1
        assert pairings[0][4] >= 0.5

    def test_multiple_strong_one_weakened_pairs_best_match(self) -> None:
        """When multiple strong lines exist and one is weakened, pair the weakened one."""
        from se3.engine.merge.guardrails import _pair_strong_weak_lines

        strong = [
            "The system SHALL validate inputs.",
            "The system SHALL check permissions.",
        ]
        weak = [
            "The system SHOULD check permissions.",
        ]
        pairings = _pair_strong_weak_lines(strong, weak)
        assert len(pairings) == 1
        assert "SHALL check permissions" in pairings[0][2]
        assert "SHOULD check permissions" in pairings[0][3]

    def test_global_best_match_preferred_over_local_greedy(self) -> None:
        """When two strong lines compete for one weak line, the globally best
        pairing must win, not the first-processed strong line.

        strong A: "SHALL foo bar baz qux." (tokens: {foo, bar, baz, qux})
        strong B: "SHALL foo bar baz qux extra." (tokens: {foo, bar, baz, qux, extra})
        weak X:   "SHOULD foo bar baz qux extra." (tokens: {foo, bar, baz, qux, extra})

        Jaccard(A, X) = 4/5 = 0.80
        Jaccard(B, X) = 5/5 = 1.00

        The old per-strong greedy loop would process A first, claim X with 0.80,
        and leave B unmatched.  The new sorted-global approach assigns B↔X
        (1.00) first, which is the optimal pairing.
        """
        from se3.engine.merge.guardrails import _pair_strong_weak_lines

        strong = [
            "SHALL foo bar baz qux.",
            "SHALL foo bar baz qux extra.",
        ]
        weak = [
            "SHOULD foo bar baz qux extra.",
        ]
        pairings = _pair_strong_weak_lines(strong, weak)
        assert len(pairings) == 1
        # The globally best match (B↔X, score 1.0) must be selected
        assert "extra" in pairings[0][2]
        assert pairings[0][4] == 1.0

    def test_no_tokens_after_filtering_returns_empty(self) -> None:
        """If a line has only role words and stop words, pairing returns empty."""
        from se3.engine.merge.guardrails import _pair_strong_weak_lines

        strong = ["SHALL"]
        weak = ["SHOULD"]
        pairings = _pair_strong_weak_lines(strong, weak)
        assert len(pairings) == 0

    @pytest.mark.parametrize(
        "strong_line,weak_line,expected_pairs,description",
        [
            # Jaccard = 4/6 ≈ 0.667 >= 0.65 → must pair
            (
                "SHALL foo bar baz qux.",
                "SHOULD foo bar baz qux extra more.",
                1,
                "above_0.65_threshold_pairs",
            ),
            # Jaccard = 3/5 = 0.6 < 0.65 → must NOT pair
            (
                "SHALL foo bar baz qux.",
                "SHOULD foo bar baz extra.",
                0,
                "below_0.65_threshold_no_pair",
            ),
        ],
    )
    def test_mixed_line_threshold_boundary_parametrized(
        self,
        strong_line: str,
        weak_line: str,
        expected_pairs: int,
        description: str,
    ) -> None:
        """Pin the _PAIR_SIMILARITY_THRESHOLD_MIXED = 0.65 contract.

        These cases exercise the threshold parameter directly so that
        changing the constant regresses the test suite.
        """
        from se3.engine.merge.guardrails import (
            _PAIR_SIMILARITY_THRESHOLD_MIXED,
            _pair_strong_weak_lines,
        )

        pairings = _pair_strong_weak_lines(
            [strong_line],
            [weak_line],
            threshold=_PAIR_SIMILARITY_THRESHOLD_MIXED,
        )
        assert len(pairings) == expected_pairs, description


# --------- MergeGuardrailsCheck integration tests ---------


class TestViolationSetHash:
    """Unit tests for violation_set_hash and _normalize_message."""

    def test_same_violations_different_order_same_hash(self) -> None:
        """Order independence: same violations in different order → same hash."""
        from se3.engine.merge.guardrails import violation_set_hash

        v1 = GuardrailViolation(
            file_path="se3/specs/a/spec.md",
            violation_type="WEAKENING",
            message="SHALL weakened to SHOULD",
        )
        v2 = GuardrailViolation(
            file_path="se3/specs/b/spec.md",
            violation_type="DELETE",
            message="Scenarios deleted: 1 WHEN clause(s) removed",
        )
        hash_ab = violation_set_hash([v1, v2])
        hash_ba = violation_set_hash([v2, v1])
        assert hash_ab == hash_ba

    def test_different_messages_different_hash(self) -> None:
        """Different violation messages produce different hashes."""
        from se3.engine.merge.guardrails import violation_set_hash

        v1 = GuardrailViolation(
            file_path="se3/specs/a/spec.md",
            violation_type="WEAKENING",
            message="SHALL weakened to SHOULD",
        )
        v2 = GuardrailViolation(
            file_path="se3/specs/a/spec.md",
            violation_type="WEAKENING",
            message="MUST weakened to MAY",
        )
        assert violation_set_hash([v1]) != violation_set_hash([v2])

    def test_count_digits_preserved_in_hash(self) -> None:
        """Different counts produce different hashes (counts are preserved)."""
        from se3.engine.merge.guardrails import violation_set_hash

        v3 = GuardrailViolation(
            file_path="se3/specs/a/spec.md",
            violation_type="DELETE",
            message="Scenarios deleted: 3 WHEN clause(s) removed",
        )
        v4 = GuardrailViolation(
            file_path="se3/specs/a/spec.md",
            violation_type="DELETE",
            message="Scenarios deleted: 4 WHEN clause(s) removed",
        )
        assert violation_set_hash([v3]) != violation_set_hash([v4])

    def test_line_numbers_normalized_away(self) -> None:
        """Line number differences are normalized away."""
        from se3.engine.merge.guardrails import violation_set_hash

        v5 = GuardrailViolation(
            file_path="se3/specs/a/spec.md",
            violation_type="WEAKENING",
            message="SHALL weakened to SHOULD at line 42",
        )
        v6 = GuardrailViolation(
            file_path="se3/specs/a/spec.md",
            violation_type="WEAKENING",
            message="SHALL weakened to SHOULD at line 99",
        )
        assert violation_set_hash([v5]) == violation_set_hash([v6])

    def test_empty_violations_hash(self) -> None:
        """Empty violation list produces a deterministic hash."""
        from se3.engine.merge.guardrails import violation_set_hash

        h1 = violation_set_hash([])
        h2 = violation_set_hash([])
        assert h1 == h2
        assert len(h1) == 40  # sha1 hex length

    def test_normalize_message_strips_whitespace(self) -> None:
        from se3.engine.merge.guardrails import _normalize_message

        assert _normalize_message("  hello world  ") == "hello world"

    def test_normalize_message_removes_line_numbers(self) -> None:
        from se3.engine.merge.guardrails import _normalize_message

        assert _normalize_message("Error at line 42") == "Error"
        assert _normalize_message("Error at Line 99 here") == "Error here"

    def test_normalize_message_preserves_counts(self) -> None:
        from se3.engine.merge.guardrails import _normalize_message

        assert _normalize_message("3 WHEN clauses removed") == "3 WHEN clauses removed"
        assert _normalize_message("4 WHEN clauses removed") == "4 WHEN clauses removed"

    def test_normalize_message_strips_attempt_counters(self) -> None:
        from se3.engine.merge.guardrails import _normalize_message

        assert _normalize_message("Error (attempt 3)") == "Error"
        assert _normalize_message("Error (Attempt 5)") == "Error"
        assert _normalize_message("Error (try 2)") == "Error"

    def test_normalize_message_strips_hex_shas(self) -> None:
        from se3.engine.merge.guardrails import _normalize_message

        # Bare hex without a SHA context cue — preserved (relying on
        # evidence-derived stable keys for stable hashing; message
        # normalization only strips context-anchored hex).
        assert _normalize_message("Error at abc1234") == "Error at abc1234"
        # Context-anchored uppercase hex — stripped
        assert _normalize_message(
            "Error at commit ABCDEF1234567890ABCDEF1234567890ABCDEF12"
        ) == "Error at"
        assert _normalize_message(
            "Error at sha: ABCDEF1234"
        ) == "Error at"
        # Uppercase hex without a SHA context cue — preserved
        assert _normalize_message(
            "Error at ABCDEF1234567890ABCDEF1234567890ABCDEF12"
        ) == "Error at ABCDEF1234567890ABCDEF1234567890ABCDEF12"

    def test_stable_key_from_evidence_overrides_message(self) -> None:
        """When evidence contains strong_line, hash is stable even if message drifts."""
        from se3.engine.merge.guardrails import GuardrailViolation, violation_set_hash

        v1 = GuardrailViolation(
            file_path="se3/specs/a/spec.md",
            violation_type="WEAKENING",
            message="SHALL weakened to SHOULD at line 42",
            evidence={
                "strong_line": "The system SHALL validate inputs.",
                "weak_line": "The system SHOULD validate inputs.",
                "pairing_score": 0.95,
            },
        )
        v2 = GuardrailViolation(
            file_path="se3/specs/a/spec.md",
            violation_type="WEAKENING",
            message="SHALL weakened to SHOULD at line 99 (attempt 2)",
            evidence={
                "strong_line": "The system SHALL validate inputs.",
                "weak_line": "The system SHOULD validate inputs.",
                "pairing_score": 0.95,
            },
        )
        # Same evidence-derived key → same hash despite different messages
        assert violation_set_hash([v1]) == violation_set_hash([v2])

    def test_stable_key_fallback_to_message_when_no_evidence(self) -> None:
        """Without evidence, hash falls back to normalized message."""
        from se3.engine.merge.guardrails import GuardrailViolation, violation_set_hash

        v1 = GuardrailViolation(
            file_path="se3/specs/a/spec.md",
            violation_type="DELETE",
            message="Scenarios deleted: 3 WHEN clause(s) removed",
        )
        v2 = GuardrailViolation(
            file_path="se3/specs/a/spec.md",
            violation_type="DELETE",
            message="Scenarios deleted: 4 WHEN clause(s) removed",
        )
        # Different messages, no evidence → different hashes
        assert violation_set_hash([v1]) != violation_set_hash([v2])

    def test_stable_key_from_deleted_line_evidence(self) -> None:
        """DELETE violations with deleted_line evidence use it as stable key."""
        from se3.engine.merge.guardrails import GuardrailViolation, violation_set_hash

        v1 = GuardrailViolation(
            file_path="se3/specs/a/spec.md",
            violation_type="DELETE",
            message="Requirement deleted: SHALL line removed",
            evidence={
                "deleted_line": "The system SHALL validate inputs.",
                "deleted_line_no": 3,
            },
        )
        v2 = GuardrailViolation(
            file_path="se3/specs/a/spec.md",
            violation_type="DELETE",
            message="Requirement deleted: SHALL line removed (attempt 2)",
            evidence={
                "deleted_line": "The system SHALL validate inputs.",
                "deleted_line_no": 3,
            },
        )
        assert violation_set_hash([v1]) == violation_set_hash([v2])

    def test_thrash_pattern_two_independent_weakenings_different_hashes(self) -> None:
        """Alternating fixes on independent lines produce different hashes.

        The per-round violation_set_hash changes when the strong_line set
        switches between {A} and {B}.  With last_hash tracking only the
        immediately previous iteration, this oscillation is NOT detected
        as a stall within 2 iterations (the current max).  This test
        documents that the hash function itself correctly distinguishes
        the two states.
        """
        from se3.engine.merge.guardrails import GuardrailViolation, violation_set_hash

        # Round 1: violation on line A
        round1 = [
            GuardrailViolation(
                file_path="se3/specs/a/spec.md",
                violation_type="WEAKENING",
                message="SHALL weakened to SHOULD",
                evidence={
                    "strong_line": "The system SHALL validate inputs.",
                    "weak_line": "The system SHOULD validate inputs.",
                    "pairing_score": 0.9,
                },
            ),
        ]
        # Round 2: violation on line B (different strong line)
        round2 = [
            GuardrailViolation(
                file_path="se3/specs/a/spec.md",
                violation_type="WEAKENING",
                message="SHALL weakened to SHOULD",
                evidence={
                    "strong_line": "The system SHALL check permissions.",
                    "weak_line": "The system SHOULD check permissions.",
                    "pairing_score": 0.9,
                },
            ),
        ]
        # Different strong lines → different hashes. The orchestrator
        # compares only the immediately previous hash (last_hash), so
        # non-consecutive repeats are not detected as stalls.
        h1 = violation_set_hash(round1)
        h2 = violation_set_hash(round2)
        assert h1 != h2, (
            "Expected different hashes for different strong_line evidence"
        )

    def test_mixed_line_whitespace_rewrite_not_false_positive(self) -> None:
        """Original mixed line rewritten with only whitespace change should not
        be treated as a new mixed line (Phase 2 token-set normalization).

        The original has a mixed line (both SHALL and SHOULD) plus a pure-strong
        line.  The new text removes the pure-strong line and rewrites the mixed
        line with only a trailing-space change.  Without token-set normalization
        in Phase 2, the whitespace-rewritten mixed line would enter the candidate
        set and falsely pair with the missing strong line.
        """
        original = (
            "SHALL validate inputs and SHOULD check permissions.\n"
            "SHALL log errors.\n"
        )
        new = (
            "SHALL validate inputs and SHOULD check permissions. \n"  # trailing space
        )
        violations = check_spec_diff(original, new)
        # The actual violation is a DELETE (SHALL log errors removed), not a
        # spurious WEAKENING from the whitespace-rewritten mixed line.
        assert len(violations) == 1
        assert violations[0].violation_type == "DELETE"

    def test_genuine_delete_with_unrelated_mixed_line_no_false_positive(self) -> None:
        """A genuine strong line is deleted and an unrelated mixed line is added.

        The deleted strong line and the new mixed line share only generic
        domain words.  The token-set Jaccard is well below the 0.65 mixed-line
        threshold, so no false WEAKENING should be reported.  The fast path
        (strong count drops) still fires, so the deleted line surfaces as DELETE.
        """
        original = (
            "SHALL monitor infrastructure.\n"
            "SHALL validate inputs.\n"
        )
        new = (
            "SHOULD monitor system data.\n"
            "SHALL handle errors and SHOULD log events.\n"
        )
        violations = check_spec_diff(original, new)
        # Fast path fires (2 SHALL → 1 SHALL + 2 SHOULD).
        # Mixed-line pairing fails (Jaccard ≈ 0) so no false WEAKENING.
        # The genuine deletion is reported instead.
        assert len(violations) == 1
        assert violations[0].violation_type == "DELETE"
        assert "SHALL" in violations[0].message

    def test_when_whitespace_reformat_not_false_positive(self) -> None:
        """WHEN clause with only whitespace/punctuation change should not be
        reported as deleted.

        Token-set normalization in the WHEN-clause check means that
        'WHEN user logs in' and 'WHEN   user logs in' are treated as the
        same clause.
        """
        original = (
            "#### Scenario: A\n"
            "- WHEN user logs in\n"
            "- THEN redirect\n"
        )
        new = (
            "#### Scenario: A\n"
            "- WHEN   user logs in\n"  # extra spaces
            "- THEN redirect\n"
        )
        violations = check_spec_diff(original, new)
        assert violations == []

    def test_when_punctuation_reformat_not_false_positive(self) -> None:
        """WHEN clause with only punctuation change should not be reported as deleted."""
        original = (
            "#### Scenario: A\n"
            "- WHEN user logs in.\n"
            "- THEN redirect\n"
        )
        new = (
            "#### Scenario: A\n"
            "- WHEN user logs in!\n"
            "- THEN redirect\n"
        )
        violations = check_spec_diff(original, new)
        assert violations == []

    def test_when_blank_line_between_clause_and_continuation_joined(self) -> None:
        """Blank lines between a WHEN line and its indented continuation are skipped.

        Without blank-line skipping, the unjoined fragment would be reported
        as a spurious DELETE violation.
        """
        original = (
            "#### Scenario: A\n"
            "- WHEN user logs in\n"
            "  with valid credentials\n"
            "- THEN redirect\n"
        )
        new = (
            "#### Scenario: A\n"
            "- WHEN user logs in\n"
            "\n"  # blank line introduced
            "  with valid credentials\n"
            "- THEN redirect\n"
        )
        violations = check_spec_diff(original, new)
        assert violations == []

    def test_should_to_may_weakening_net_zero_detected(self) -> None:
        """SHOULD→MAY weakening with offsetting new SHALL (net-zero count).

        The original has one SHALL and one SHOULD on the same semantic content.
        The new text removes the SHALL, changes SHOULD→MAY, and adds a new
        SHALL elsewhere. The net strong count is unchanged, so the fast path
        doesn't fire. The corner-case path must detect the weakening via
        role-word comparison in _weak_line_is_new.
        """
        original = (
            "The system SHALL check permissions.\n"
            "The system SHOULD check permissions.\n"
        )
        new = (
            "The system MAY check permissions.\n"
            "The system SHALL log errors.\n"
        )
        violations = check_spec_diff(original, new)
        assert len(violations) == 1
        assert violations[0].violation_type == "WEAKENING"
        assert violations[0].evidence is not None
        assert "SHALL" in violations[0].evidence["strong_line"]
        assert "MAY" in violations[0].evidence["weak_line"]
        assert violations[0].evidence["strong_line_no"] == 1
        assert violations[0].evidence["weak_line_no"] == 1

    def test_quantifier_blank_line_between_clause_and_continuation_joined(self) -> None:
        """Quantifier WHEN variant: blank line between WHEN and continuation.

        Same as test_when_blank_line_between_clause_and_continuation_joined
        but for the quantifier branch's WHEN-clause deletion check.
        """
        original = (
            "#### Scenario: A\n"
            "- WHEN user requests data\n"
            "  from the API\n"
            "- THEN return results\n"
        )
        new = (
            "#### Scenario: A\n"
            "- WHEN user requests data\n"
            "\n"
            "  from the API\n"
            "- THEN return results\n"
        )
        violations = check_spec_diff(original, new)
        assert violations == []


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
        report = checker.check_merge_result(base_sha, head_sha, enforce_topology=False)
        assert report.passed is True
        assert report.violations == []

    def test_check_merge_result_empty_ref_fails_closed(self, tmp_path: Path) -> None:
        """check_merge_result with empty ref must fail closed, not silently pass."""
        _init_repo(tmp_path)
        checker = MergeGuardrailsCheck(tmp_path)

        report = checker.check_merge_result("abc123", "")
        assert report.passed is False
        assert len(report.violations) == 1
        assert report.violations[0].violation_type == "CHECK_FAILURE"
        assert "missing ref" in report.violations[0].message

        report = checker.check_merge_result("", "def456")
        assert report.passed is False
        assert len(report.violations) == 1
        assert report.violations[0].violation_type == "CHECK_FAILURE"

        report = checker.check_merge_result("", "")
        assert report.passed is False
        assert len(report.violations) == 1
        assert report.violations[0].violation_type == "CHECK_FAILURE"

    def test_get_changed_spec_files_empty_ref_raises(self, tmp_path: Path) -> None:
        """_get_changed_spec_files must raise ValueError when refs are empty."""
        _init_repo(tmp_path)
        with pytest.raises(ValueError, match="empty ref"):
            _get_changed_spec_files(tmp_path, "abc123", "")
        with pytest.raises(ValueError, match="empty ref"):
            _get_changed_spec_files(tmp_path, "", "def456")
        with pytest.raises(ValueError, match="empty ref"):
            _get_changed_spec_files(tmp_path, "", "")

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
        report = checker.check_merge_result(base_sha, head_sha, enforce_topology=False)
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
        report = checker.check_merge_result(base_sha, head_sha, enforce_topology=False)
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
        report = checker.check_merge_result(base_sha, head_sha, enforce_topology=False)
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
        report = checker.check_merge_result(base_sha, head_sha, enforce_topology=False)
        assert report.passed is False
        assert any("quantifier" in v.message for v in report.violations)

    def test_capitalized_quantifier_weakening_detected(self, tmp_path: Path) -> None:
        """Capitalized quantifiers at sentence start are detected (case-insensitive)."""
        _init_repo(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text("## Requirement: Q\n\nAll inputs SHALL be valid.\n")
        _commit(tmp_path, "initial")
        base_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        (spec_dir / "spec.md").write_text("## Requirement: Q\n\nSome inputs SHALL be valid.\n")
        _commit(tmp_path, "weaken quantifier")
        head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(base_sha, head_sha, enforce_topology=False)
        assert report.passed is False
        assert any("quantifier" in v.message for v in report.violations)

    def test_capitalized_every_quantifier_weakening_detected(self, tmp_path: Path) -> None:
        """Capitalized 'Every' quantifier at sentence start is detected."""
        _init_repo(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text("## Requirement: Q\n\nEvery field MUST be checked.\n")
        _commit(tmp_path, "initial")
        base_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        (spec_dir / "spec.md").write_text("## Requirement: Q\n\nSome field MUST be checked.\n")
        _commit(tmp_path, "weaken quantifier")
        head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(base_sha, head_sha, enforce_topology=False)
        assert report.passed is False
        assert any("quantifier" in v.message for v in report.violations)

    def test_extend_shall_with_preexisting_weak_no_false_positive_via_refs(self, tmp_path: Path) -> None:
        """Scn incident through check_merge_result: extend SHALL + pre-existing weak + new MUST.

        Exercises the wiring between check_merge_result -> _check_spec_file_against_ref
        -> check_spec_diff for the exact diff shape that caused the original scn
        branch false positive. A regression that only manifests through the full
        ref-fetching pipeline would not be caught by pure-function unit tests.
        """
        _init_repo(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHALL validate inputs.\n\n"
            "The system SHOULD check permissions.\n\n"
            "The system SHALL log errors.\n"
        )
        _commit(tmp_path, "initial spec with pre-existing weak line")
        base_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        (spec_dir / "spec.md").write_text(
            "## Requirement: Auth\n\n"
            "The system SHALL validate inputs and sanitize them.\n\n"
            "The system SHOULD check permissions.\n\n"
            "The system SHALL log errors.\n\n"
            "The system MUST audit events.\n"
        )
        _commit(tmp_path, "extend shall + add must")
        head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(base_sha, head_sha, enforce_topology=False)
        assert report.passed is True, (
            f"Expected no violations, got: {report.violations}"
        )
        assert report.violations == []

    def test_quantifier_extend_all_with_preexisting_some_no_false_positive_via_refs(
        self, tmp_path: Path
    ) -> None:
        """Quantifier variant of the scn incident through check_merge_result."""
        _init_repo(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text(
            "## Requirement: Data\n\n"
            "Validate all inputs.\n\n"
            "Do some logging.\n\n"
            "Check all fields.\n"
        )
        _commit(tmp_path, "initial spec with pre-existing some line")
        base_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        (spec_dir / "spec.md").write_text(
            "## Requirement: Data\n\n"
            "Validate all inputs and check boundaries.\n\n"
            "Do some logging.\n\n"
            "Check all fields.\n\n"
            "Log all events.\n"
        )
        _commit(tmp_path, "extend all + add new all")
        head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(base_sha, head_sha, enforce_topology=False)
        assert report.passed is True, (
            f"Expected no violations, got: {report.violations}"
        )
        assert report.violations == []


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

    def test_fast_strategy_spec_weakening_llm_repair_success(self, tmp_path: Path, monkeypatch) -> None:
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

        # Mock LLM to return corrected spec (SHALL restored) so the test
        # does not depend on a live LLM service.
        def mock_call_llm(self, prompt: str) -> dict:
            return {
                "files": [
                    {
                        "path": "se3/specs/base/spec.md",
                        "corrected_content": (
                            "## Requirement: Auth\n\n"
                            "The system SHALL validate all user inputs.\n\n"
                            "#### Scenario: Valid input\n"
                            "- WHEN user provides valid data\n"
                            "- THEN authentication succeeds\n"
                        ),
                    },
                ],
            }

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer._call_llm",
            mock_call_llm,
        )

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

    def test_fast_strategy_repair_stalled_escalates_to_human(self, tmp_path: Path, monkeypatch) -> None:
        """fast strategy + repair stalled (same hash twice) → pending_human with call file."""
        default_branch = self._setup_repo_with_spec(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-stall")
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

        # Mock repairer to always fail without changing violations
        def mock_repair(self, branch, pre_sha, post_sha, violations, original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(success=False, error="LLM could not fix")

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature-stall"])

        # Fast mode: repair stalled (same hash in two consecutive iterations)
        # → escalated to human call at iteration 1 (last_hash is seeded with
        # the initial violation-set hash, so a no-op repair on the first
        # iteration is immediately detected as a stall).
        assert report.success is False
        assert report.failure_reason == "guardrail_repair_stalled"
        assert report.pending_human is True
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

        data = json.loads(report.human_call_file.read_text())
        assert data["type"] == "guardrail_repair_stalled"
        assert data["iteration_count"] == 1
        assert len(data["violations"]) >= 1

        # HEAD restored
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head == pre_head

    def test_fast_strategy_repair_hash_changes_aborts(self, tmp_path: Path, monkeypatch) -> None:
        """fast strategy + repair hash changes each round → exhausted after max iterations."""
        default_branch = self._setup_repo_with_spec(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-change")
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

        check_call_count = [0]

        # Mock repairer to always fail
        def mock_repair(self, branch, pre_sha, post_sha, violations, original_spec_contents, merged_spec_contents):
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(success=False, error="LLM could not fix")

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock guardrails re-check to return violations with different
        # strong_line evidence each time so the stable key (and thus hash)
        # changes, but violations are never empty.  Because the mock is also
        # used for the initial guardrails check, the first result (odd) sets
        # the initial hash; iteration 1 (even) produces a different hash;
        # iteration 2 (odd) returns the SAME hash as the initial check.  With
        # last_hash not seeded with the initial hash, the oscillation is not
        # detected as a stall within max iterations; instead it is exhausted.
        def mock_check(self, pre_sha: str, post_sha: str):
            check_call_count[0] += 1
            from se3.engine.merge.guardrails import GuardrailReport, GuardrailViolation
            # Alternate between two different strong lines to force hash change
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
        report = orch.execute(["feature-change"])

        # Fast mode: oscillating hash not detected as stall within max iterations
        # → exhausted → human call via GuardrailRepairExhausted subclass.
        assert report.success is False
        assert report.failure_reason == "guardrail_repair_exhausted"
        assert report.pending_human is True
        assert report.human_call_file is not None
        assert report.human_call_file.exists()

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
        report = checker.check_merge_result(base_sha, head_sha, enforce_topology=False)

        # Should still detect the SHALL→SHOULD weakening from the commits,
        # NOT be confused by the working tree MUST
        assert report.passed is False
        assert any("SHALL" in v.message for v in report.violations)


    def test_fast_strategy_extend_shall_with_preexisting_weak_no_false_positive(
        self, tmp_path: Path
    ) -> None:
        """End-to-end: extend SHALL + pre-existing unrelated weak line → no WEAKENING.

        This is the scn-branch incident scenario reproduced through a real
        git merge and the full orchestrator execute() path.  It exercises the
        wiring between check_merge_result, _check_spec_file_against_ref, and
        check_spec_diff that unit tests alone would not catch.
        """
        default_branch = self._setup_repo_with_spec(tmp_path)

        # Rewrite the spec to match the exact scn scenario:
        #   Original: SHALL A, SHOULD B (pre-existing weak), SHALL C
        #   Feature:  SHALL A. (extended), SHOULD B (unchanged), SHALL C, MUST D (new strong)
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHALL validate inputs.\n\n"
            "The system SHOULD check permissions.\n\n"
            "The system SHALL log errors.\n"
        )
        _commit(tmp_path, "initial spec with pre-existing weak line")

        _git(tmp_path, "checkout", "-b", "feature-scn")
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHALL validate inputs and sanitize them.\n\n"
            "The system SHOULD check permissions.\n\n"
            "The system SHALL log errors.\n\n"
            "The system MUST audit events.\n"
        )
        _commit(tmp_path, "extend shall + add must")

        _git(tmp_path, "checkout", default_branch)

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature-scn"])

        # No weakening occurred — merge should succeed with no violations
        assert report.success is True, (
            f"Expected success, got failure_reason={report.failure_reason}"
        )
        assert "feature-scn" in report.merged_branches
        assert report.pending_human is False
        assert report.human_call_file is None

        # Verify the merged spec contains all expected content
        merged_spec = spec_path.read_text()
        assert "SHALL validate inputs and sanitize them" in merged_spec
        assert "SHOULD check permissions" in merged_spec
        assert "SHALL log errors" in merged_spec
        assert "MUST audit events" in merged_spec

    def test_fast_strategy_quantifier_extend_all_with_preexisting_some_no_false_positive(
        self, tmp_path: Path
    ) -> None:
        """End-to-end: extend 'all' + pre-existing unrelated 'some' line → no WEAKENING.

        Quantifier variant of the scn-branch incident, run through the full
        orchestrator execute() path to catch wiring regressions.
        """
        default_branch = self._setup_repo_with_spec(tmp_path)

        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Data\n\n"
            "Validate all inputs.\n\n"
            "Do some logging.\n\n"
            "Check all fields.\n"
        )
        _commit(tmp_path, "initial spec with pre-existing some line")

        _git(tmp_path, "checkout", "-b", "feature-quant-scn")
        spec_path.write_text(
            "## Requirement: Data\n\n"
            "Validate all inputs and check boundaries.\n\n"
            "Do some logging.\n\n"
            "Check all fields.\n\n"
            "Log all events.\n"
        )
        _commit(tmp_path, "extend all + add new all")

        _git(tmp_path, "checkout", default_branch)

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        report = orch.execute(["feature-quant-scn"])

        assert report.success is True, (
            f"Expected success, got failure_reason={report.failure_reason}"
        )
        assert "feature-quant-scn" in report.merged_branches
        assert report.pending_human is False
        assert report.human_call_file is None


class TestRunGuardrailsFastBranch:
    """Tests for _run_guardrails fast strategy branch.

    Uses real git repos + mock GuardrailRepairer to verify:
    - branch name passed correctly to repairer
    - amend is performed by the repairer
    - final guardrails re-check is executed after amend
    """

    def _setup_spec_repo(self, tmp_path: Path) -> str:
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

    def test_fast_run_guardrails_calls_repairer_with_correct_branch(self, tmp_path: Path, monkeypatch) -> None:
        """_run_guardrails(strategy=fast) calls repairer with correct branch name."""
        default_branch = self._setup_spec_repo(tmp_path)

        # Create feature branch that weakens spec
        _git(tmp_path, "checkout", "-b", "feature-fast")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)

        # Merge feature-fast (creates merge commit with violation)
        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _git(tmp_path, "merge", "--no-ff", "feature-fast", "--no-edit", "-m", "Merge feature-fast")
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Track repairer calls
        repairer_calls = []

        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            repairer_calls.append({
                "branch": branch,
                "pre_sha": pre_sha,
                "post_sha": post_sha,
                "violation_count": len(violations),
            })
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(success=True, repaired_files=["se3/specs/base/spec.md"])

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        result = orch._run_guardrails(
            pre_head, post_head, "feature-fast", strategy=MergeStrategy.FAST,
        )

        # Should return None (no violation after repair)
        assert result is None

        # Repairer should have been called once with correct branch
        assert len(repairer_calls) == 1
        assert repairer_calls[0]["branch"] == "feature-fast"
        assert repairer_calls[0]["pre_sha"] == pre_head
        assert repairer_calls[0]["post_sha"] == post_head
        assert repairer_calls[0]["violation_count"] >= 1

    def test_fast_run_guardrails_repairer_amends_and_rechecks(self, tmp_path: Path, monkeypatch) -> None:
        """_run_guardrails(strategy=fast): repairer amends commit and guardrails re-check."""
        default_branch = self._setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-amend")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        (tmp_path / "code.py").write_text("def auth(): return True\n")
        _commit(tmp_path, "weaken spec and update code")

        _git(tmp_path, "checkout", default_branch)

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _git(tmp_path, "merge", "--no-ff", "feature-amend", "--no-edit", "-m", "Merge feature-amend")
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to actually fix the file and create a fix-up commit
        # (the preferred repair path since amend on merge commits can lose
        # merge parents).
        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            # Fix the spec file
            content = spec_path.read_text()
            content = content.replace("SHOULD", "SHALL")
            spec_path.write_text(content)
            # Stage and create fix-up commit
            _git(self.project_root, "add", "se3/specs/base/spec.md")
            fixup_result = _git(
                self.project_root, "commit", "-m", "fix(specs): repair guardrail violations", check=False,
            )
            if fixup_result.returncode != 0:
                raise RuntimeError(f"git fix-up commit failed: {fixup_result.stderr}")
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(success=True, repaired_files=["se3/specs/base/spec.md"])

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        result = orch._run_guardrails(
            pre_head, post_head, "feature-amend", strategy=MergeStrategy.FAST,
        )

        # Should return None (repair succeeded)
        assert result is None

        # HEAD should have changed due to fix-up commit
        new_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert new_head != post_head

        # The original merge commit must still be an ancestor
        ancestor_check = _git(
            tmp_path, "merge-base", "--is-ancestor", post_head, "HEAD", check=False,
        )
        assert ancestor_check.returncode == 0, (
            "Original merge commit must still be an ancestor after fix-up"
        )

        # Spec should be fixed
        assert "SHALL" in spec_path.read_text()
        assert "SHOULD" not in spec_path.read_text()

    def test_fast_run_guardrails_repair_stalled_raises(self, tmp_path: Path, monkeypatch) -> None:
        """_run_guardrails(strategy=fast): same hash twice → GuardrailRepairStalled."""
        default_branch = self._setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-stall")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _git(tmp_path, "merge", "--no-ff", "feature-stall", "--no-edit", "-m", "Merge feature-stall")
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Mock repairer to always fail without changing violations
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

        orch = MergeOrchestrator(project_root=tmp_path, strategy="fast")
        from se3.engine.merge.orchestrator import GuardrailRepairStalled
        with pytest.raises(GuardrailRepairStalled) as exc_info:
            orch._run_guardrails(
                pre_head, post_head, "feature-stall", strategy=MergeStrategy.FAST,
            )

        # Should stall at iteration 1 (last_hash is seeded with the initial
        # violation-set hash, so a no-op repair on the first iteration is
        # immediately detected as a stall).
        assert exc_info.value.iteration_count == 1
        assert exc_info.value.call_file is not None
        assert exc_info.value.call_file.exists()

        # Call file should be the stalled type with evidence
        data = json.loads(exc_info.value.call_file.read_text())
        assert data["type"] == "guardrail_repair_stalled"
        assert data["iteration_count"] == 1
        assert len(data["violations"]) >= 1

        # HEAD should be rolled back to pre-merge state
        current_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert current_head == pre_head

    def test_fast_run_guardrails_repair_hash_changes_then_exhausts(self, tmp_path: Path, monkeypatch) -> None:
        """_run_guardrails(strategy=fast): oscillating hash → exhausted after max iterations.

        The mock alternates between two violation hashes.  With last_hash
        tracking only the immediately previous iteration, the oscillation
        back to the initial hash is not detected as a stall within 2 iterations.
        Instead, max iterations are exhausted and GuardrailRepairExhausted is
        raised (subclass of GuardrailRepairStalled).
        """
        default_branch = self._setup_spec_repo(tmp_path)

        _git(tmp_path, "checkout", "-b", "feature-change")
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        spec_path.write_text(
            "## Requirement: Auth\n\n"
            "The system SHOULD validate all user inputs.\n"
        )
        _commit(tmp_path, "weaken spec")

        _git(tmp_path, "checkout", default_branch)

        pre_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _git(tmp_path, "merge", "--no-ff", "feature-change", "--no-edit", "-m", "Merge feature-change")
        post_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        call_count = [0]

        def mock_repair(self, branch, pre_sha, post_sha, violations,
                        original_spec_contents, merged_spec_contents):
            call_count[0] += 1
            from se3.engine.merge.guardrail_repair import RepairResult
            return RepairResult(
                success=False,
                error=f"LLM fix attempt {call_count[0]} failed",
            )

        monkeypatch.setattr(
            "se3.engine.merge.guardrail_repair.GuardrailRepairer.repair_violations",
            mock_repair,
        )

        # Mock check_merge_result to alternate between two violation hashes.
        # Because the mock is also called for the initial check, the sequence is:
        #   initial check → odd → hash A
        #   iteration 1   → even → hash B
        #   iteration 2   → odd → hash A (seen before → stall)
        check_call_count = [0]

        def mock_check(self, pre_sha: str, post_sha: str):
            check_call_count[0] += 1
            from se3.engine.merge.guardrails import GuardrailReport, GuardrailViolation
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
        from se3.engine.merge.orchestrator import GuardrailRepairStalled
        with pytest.raises(GuardrailRepairStalled) as exc_info:
            orch._run_guardrails(
                pre_head, post_head, "feature-change", strategy=MergeStrategy.FAST,
            )

        # Exhausted at iteration 2 (oscillation not detected as stall within
        # max iterations because last_hash is not seeded with the initial hash).
        assert exc_info.value.iteration_count == 2
        assert exc_info.value.call_file is not None
        assert exc_info.value.call_file.exists()

        # HEAD should be rolled back to pre-merge state
        current_head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert current_head == pre_head


# =====================================================================
# G8 — Topology validation, EvidenceRecord, WHEN-clause bounds, H5 errors
# =====================================================================


class TestMergeTopologyValidation:
    """G8 task 38 (H1/H2): check_merge_result enforces merge topology.

    The topology check catches the class of disasters where the merge
    commit was silently dropped (e.g. ``git reset --soft HEAD~1`` on an
    amended merge): even if the spec content matches, the underlying
    commit is no longer a merge.
    """

    def _make_two_branches(self, tmp_path: Path) -> tuple[str, str, str]:
        """Set up a repo with master and a feature branch.

        Returns ``(default_branch, feature_branch, pre_merge_sha)``.
        """
        _init_repo(tmp_path)
        (tmp_path / "README.md").write_text("# initial\n")
        _commit(tmp_path, "initial")
        default = _current_branch(tmp_path)
        pre = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        _git(tmp_path, "checkout", "-b", "feature")
        (tmp_path / "feature.py").write_text("# feature\n")
        _commit(tmp_path, "add feature")

        _git(tmp_path, "checkout", default)
        return default, "feature", pre

    def test_real_merge_topology_passes(self, tmp_path: Path) -> None:
        """A real --no-ff merge satisfies both ancestry and parent-count."""
        default, feature, pre = self._make_two_branches(tmp_path)
        _git(tmp_path, "merge", "--no-ff", feature, "-m", "Merge feature")
        post = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(pre, post)
        assert report.passed is True, f"violations: {report.violations}"

    def test_lost_merge_detected_by_ancestry_check(self, tmp_path: Path) -> None:
        """Simulate the G8 disaster: merge commit lost via reset.

        Sequence:
          1. Real merge → post_sha1 has 2 parents.
          2. ``git reset --hard pre_sha`` (the disaster) → HEAD == pre_sha.
          3. check_merge_result(pre, post_sha1) where post_sha1 is dangling.
             Actually, we test the inverse: present a post_sha that is NOT
             a descendant of pre_sha.
        """
        default, feature, pre = self._make_two_branches(tmp_path)

        # Make an unrelated commit on default that is not ancestor of feature
        (tmp_path / "other.py").write_text("# other\n")
        _commit(tmp_path, "unrelated commit")
        unrelated = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Now imagine pre-merge SHA was the unrelated commit, and post-merge
        # is on a different branch (feature). feature is NOT a descendant of
        # unrelated. Topology check should fail.
        feature_sha = _git(tmp_path, "rev-parse", feature).stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(unrelated, feature_sha)
        assert report.passed is False
        assert any(
            v.violation_type == "CHECK_FAILURE"
            and "NOT an ancestor" in v.message
            for v in report.violations
        ), f"violations: {report.violations}"

    def test_single_parent_commit_fails_parent_count(self, tmp_path: Path) -> None:
        """A commit with only 1 parent fails the merge-commit check."""
        default, feature, pre = self._make_two_branches(tmp_path)
        # Make a regular fast-forward commit on default (not a merge)
        (tmp_path / "fastforward.py").write_text("# ff\n")
        _commit(tmp_path, "fast forward")
        post = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(pre, post)
        assert report.passed is False
        assert any(
            v.violation_type == "CHECK_FAILURE"
            and "1 parent(s)" in v.message
            and "expected >= 2" in v.message
            for v in report.violations
        ), f"violations: {report.violations}"

    def test_octopus_merge_passes_topology(self, tmp_path: Path) -> None:
        """A 3-parent octopus merge satisfies parent_count >= 2."""
        _init_repo(tmp_path)
        (tmp_path / "README.md").write_text("# initial\n")
        _commit(tmp_path, "initial")
        default = _current_branch(tmp_path)
        pre = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Create three feature branches diverged from initial commit
        for branch in ("f1", "f2", "f3"):
            _git(tmp_path, "checkout", "-b", branch, default)
            (tmp_path / f"{branch}.py").write_text(f"# {branch}\n")
            _commit(tmp_path, f"add {branch}")

        _git(tmp_path, "checkout", default)
        # Octopus merge of all three (all touch unique files, no conflicts)
        _git(tmp_path, "merge", "--no-ff", "f1", "f2", "f3", "-m", "Octopus")
        post = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Verify it is indeed an octopus (>2 parents).
        # `git merge f1 f2 f3` produces a merge commit with 4 parents:
        # the current branch + the 3 merged branches.
        parents_result = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-list", "--parents", "-n", "1", post],
            capture_output=True, text=True, check=True,
        )
        parts = parents_result.stdout.strip().split()
        assert len(parts) - 1 == 4  # current + f1 + f2 + f3 = 4 parents

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(pre, post)
        assert report.passed is True, f"violations: {report.violations}"

    def test_topology_fails_loud_when_pre_equals_post(self, tmp_path: Path) -> None:
        """When pre_sha == post_sha with topology enforcement on, fail loud.

        G3 fix (high): the prior behaviour was to silently skip the
        topology check on equal SHAs and return ``passed=True`` (the
        diff would also be empty, so the spec-content check trivially
        passed). That hid a class of silent-success bugs — a caller that
        accidentally passed the pre-merge SHA twice would get a green
        light. The contract now requires callers to either filter the
        already-ancestor no-op path before calling ``check_merge_result``
        or invoke it with ``enforce_topology=False``. Reaching the
        topology branch with equal SHAs is treated as a contract
        violation and surfaces as ``CHECK_FAILURE``.
        """
        _init_repo(tmp_path)
        (tmp_path / "README.md").write_text("# initial\n")
        _commit(tmp_path, "initial")
        sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(sha, sha)
        assert report.passed is False
        assert any(
            v.violation_type == "CHECK_FAILURE"
            and "pre_sha == post_sha" in v.message
            for v in report.violations
        ), [v.message for v in report.violations]

    def test_topology_skipped_when_pre_equals_post_and_enforce_false(
        self, tmp_path: Path,
    ) -> None:
        """Equal SHAs with enforce_topology=False still pass."""
        _init_repo(tmp_path)
        (tmp_path / "README.md").write_text("# initial\n")
        _commit(tmp_path, "initial")
        sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(sha, sha, enforce_topology=False)
        assert report.passed is True

    def test_enforce_topology_false_skips_check(self, tmp_path: Path) -> None:
        """enforce_topology=False bypasses the merge topology assertions."""
        default, feature, pre = self._make_two_branches(tmp_path)
        # Regular non-merge commit
        (tmp_path / "fastforward.py").write_text("# ff\n")
        _commit(tmp_path, "fast forward")
        post = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(pre, post, enforce_topology=False)
        assert report.passed is True


class TestWhenClauseBounds:
    """G8 task 39 (H3): _extract_when_clauses must not IndexError on
    unusual whitespace patterns.
    """

    def test_when_clause_followed_by_blank_lines(self) -> None:
        """A WHEN clause followed by trailing blank lines does not crash."""
        from se3.engine.merge.guardrails import _extract_when_clauses
        lines = [
            "#### Scenario: A",
            "- WHEN something happens",
            "",
            "",
            "",
        ]
        clauses = _extract_when_clauses(lines)
        # Should extract the WHEN line without crashing
        assert len(clauses) == 1
        assert "WHEN something happens" in clauses[0]

    def test_when_clause_continuation_with_blank_lines_between(self) -> None:
        """Blank lines between WHEN and indented continuations are skipped."""
        from se3.engine.merge.guardrails import _extract_when_clauses
        lines = [
            "- WHEN user does X",
            "",
            "  with detail Y",
            "",
            "  and Z",
        ]
        clauses = _extract_when_clauses(lines)
        assert len(clauses) == 1
        assert "with detail Y" in clauses[0]
        assert "and Z" in clauses[0]

    def test_only_blank_lines_after_when_no_crash(self) -> None:
        """File ending with only whitespace lines after a WHEN doesn't crash."""
        from se3.engine.merge.guardrails import _extract_when_clauses
        lines = [
            "- WHEN end of file",
            "   ",
            "\t",
            "",
        ]
        # This MUST NOT raise IndexError or any other exception
        clauses = _extract_when_clauses(lines)
        assert len(clauses) == 1

    def test_empty_string_lines_handled(self) -> None:
        """Lines that are exactly empty strings don't crash."""
        from se3.engine.merge.guardrails import _extract_when_clauses
        lines = ["- WHEN x", "", "", "  cont"]
        clauses = _extract_when_clauses(lines)
        assert len(clauses) == 1
        assert "cont" in clauses[0]


class TestEvidenceRecordTypoFailFast:
    """G8 task 40 (H4): EvidenceRecord rejects unknown fields at construction."""

    def test_known_field_construction_succeeds(self) -> None:
        from se3.commands.merge.result_model import EvidenceRecord

        rec = EvidenceRecord(strong_line="x", weak_line="y", pairing_score=0.9)
        assert rec.strong_line == "x"
        assert rec.weak_line == "y"
        assert rec.pairing_score == 0.9

    def test_unknown_field_raises_typeerror(self) -> None:
        """Typo in field name → TypeError at construction (fail-fast)."""
        from se3.commands.merge.result_model import EvidenceRecord

        with pytest.raises(TypeError):
            EvidenceRecord(strng_line="x")  # typo: strng_line vs strong_line

    def test_to_dict_omits_none_values(self) -> None:
        """to_dict() drops None-valued fields for compact serialization."""
        from se3.commands.merge.result_model import EvidenceRecord

        rec = EvidenceRecord(strong_line="x", pairing_score=0.5)
        d = rec.to_dict()
        assert d == {"strong_line": "x", "pairing_score": 0.5}
        assert "weak_line" not in d
        assert "deleted_line" not in d

    def test_from_dict_with_unknown_key_raises(self) -> None:
        """from_dict({}) with unknown key → TypeError."""
        from se3.commands.merge.result_model import EvidenceRecord

        with pytest.raises(TypeError):
            EvidenceRecord.from_dict({"unknown_key": "x"})

    def test_from_dict_empty_returns_none(self) -> None:
        from se3.commands.merge.result_model import EvidenceRecord

        assert EvidenceRecord.from_dict(None) is None
        assert EvidenceRecord.from_dict({}) is None

    def test_from_dict_round_trip(self) -> None:
        from se3.commands.merge.result_model import EvidenceRecord

        original = EvidenceRecord(
            strong_line="SHALL X",
            weak_line="SHOULD X",
            pairing_score=0.8,
            strong_line_no=42,
            weak_line_no=44,
        )
        d = original.to_dict()
        roundtrip = EvidenceRecord.from_dict(d)
        assert roundtrip == original

    def test_evidence_dict_helper_validates_keys(self) -> None:
        """_evidence_dict() typos fail fast."""
        from se3.engine.merge.guardrails import _evidence_dict

        with pytest.raises(TypeError):
            _evidence_dict(strng_line="x")

    def test_topology_evidence_fields_recognized(self) -> None:
        """Topology check fields (pre_sha, post_sha, etc.) are valid."""
        from se3.engine.merge.guardrails import _evidence_dict

        d = _evidence_dict(
            pre_sha="abc",
            post_sha="def",
            parent_count=1,
            min_parents=2,
            topology_check="parent_count",
        )
        assert d == {
            "pre_sha": "abc",
            "post_sha": "def",
            "parent_count": 1,
            "min_parents": 2,
            "topology_check": "parent_count",
        }


class TestSpecIterationExceptionHandling:
    """G8 task 41 (H5): per-file iteration errors do not silently abort."""

    def test_file_read_error_returns_check_incomplete(self, tmp_path: Path, monkeypatch) -> None:
        """An OSError during file read → CHECK_INCOMPLETE, report.incomplete=True."""
        from se3.engine.merge import guardrails as guardrails_mod

        _init_repo(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text("The system SHALL X.\n")
        _commit(tmp_path, "initial")
        base_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        (spec_dir / "spec.md").write_text("The system SHALL Y.\n")
        _commit(tmp_path, "update")
        head_sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Monkey-patch _check_spec_file_against_ref to raise OSError
        original = guardrails_mod._check_spec_file_against_ref

        def fake_check(*args, **kwargs):
            raise OSError("simulated read error")

        monkeypatch.setattr(
            guardrails_mod, "_check_spec_file_against_ref", fake_check
        )

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(
            base_sha, head_sha, enforce_topology=False,
        )
        assert report.passed is False
        assert report.incomplete is True
        assert any(
            v.violation_type == "CHECK_INCOMPLETE" for v in report.violations
        )
        # The exception type should be in the evidence
        check_incomplete = [
            v for v in report.violations
            if v.violation_type == "CHECK_INCOMPLETE"
        ][0]
        assert check_incomplete.evidence.get("exception_type") == "OSError"


class TestMergeRespondSpecPath:
    """G8 task 43 (G2): merge_respond._is_spec_path uses pathlib."""

    def test_forward_slash_path_detected(self) -> None:
        from se3.commands.merge_respond import _is_spec_path

        assert _is_spec_path("se3/specs/base/spec.md") is True
        assert _is_spec_path("se3/specs/foo/bar/spec.md") is True

    def test_backslash_path_detected(self) -> None:
        """Windows-style backslash paths are normalized and detected."""
        from se3.commands.merge_respond import _is_spec_path

        assert _is_spec_path("se3\\specs\\base\\spec.md") is True
        assert _is_spec_path("se3\\specs\\foo\\bar\\spec.md") is True

    def test_non_spec_path_rejected(self) -> None:
        from se3.commands.merge_respond import _is_spec_path

        assert _is_spec_path("README.md") is False
        assert _is_spec_path("se3/state/foo.json") is False
        assert _is_spec_path("se3/specs/base/other.md") is False
        assert _is_spec_path("") is False


class TestMergeRespondFirstParent:
    """G8 task 42 (G1): merge_respond._first_parent_sha handles octopus."""

    def test_first_parent_of_two_parent_merge(self, tmp_path: Path) -> None:
        from se3.commands.merge_respond import _first_parent_sha

        _init_repo(tmp_path)
        (tmp_path / "a.py").write_text("a")
        _commit(tmp_path, "initial")
        default = _current_branch(tmp_path)
        first_parent_expected = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        _git(tmp_path, "checkout", "-b", "feature")
        (tmp_path / "b.py").write_text("b")
        _commit(tmp_path, "feature")

        _git(tmp_path, "checkout", default)
        _git(tmp_path, "merge", "--no-ff", "feature", "-m", "Merge feature")

        first_parent = _first_parent_sha(tmp_path)
        assert first_parent == first_parent_expected

    def test_first_parent_of_octopus_merge(self, tmp_path: Path) -> None:
        from se3.commands.merge_respond import _first_parent_sha

        _init_repo(tmp_path)
        (tmp_path / "a.py").write_text("a")
        _commit(tmp_path, "initial")
        default = _current_branch(tmp_path)
        first_parent_expected = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Create 3 branches
        for branch in ("f1", "f2", "f3"):
            _git(tmp_path, "checkout", "-b", branch, default)
            (tmp_path / f"{branch}.py").write_text(branch)
            _commit(tmp_path, f"add {branch}")

        _git(tmp_path, "checkout", default)
        _git(tmp_path, "merge", "--no-ff", "f1", "f2", "f3", "-m", "Octopus")

        # First parent must be the pre-merge HEAD (default branch)
        first_parent = _first_parent_sha(tmp_path)
        assert first_parent == first_parent_expected

    def test_first_parent_root_commit_raises(self, tmp_path: Path) -> None:
        """A root commit (no parents) → RuntimeError."""
        from se3.commands.merge_respond import _first_parent_sha

        _init_repo(tmp_path)
        (tmp_path / "a.py").write_text("a")
        _commit(tmp_path, "initial")

        with pytest.raises(RuntimeError, match="no parents"):
            _first_parent_sha(tmp_path)


class TestGuardrailReportIncomplete:
    """The new ``incomplete`` field on GuardrailReport tracks unresolved checks."""

    def test_default_incomplete_is_false(self) -> None:
        from se3.engine.merge.guardrails import GuardrailReport

        report = GuardrailReport()
        assert report.incomplete is False
        assert report.passed is True

    def test_incomplete_field_set_when_iteration_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from se3.engine.merge import guardrails as guardrails_mod

        _init_repo(tmp_path)
        spec_dir = tmp_path / "se3" / "specs" / "base"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text("The system SHALL X.\n")
        _commit(tmp_path, "initial")
        base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        (spec_dir / "spec.md").write_text("The system SHALL Y.\n")
        _commit(tmp_path, "update")
        head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        def boom(*args, **kwargs):
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "test")

        monkeypatch.setattr(
            guardrails_mod, "_check_spec_file_against_ref", boom
        )

        checker = MergeGuardrailsCheck(tmp_path)
        report = checker.check_merge_result(base, head, enforce_topology=False)
        assert report.incomplete is True
        assert report.passed is False

