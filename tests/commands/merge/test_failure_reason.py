"""Tests for FailureReason enum and legacy-string compatibility."""

from __future__ import annotations

import pytest

from se3.commands.merge.failure_reason import (
    FailureReason,
    from_legacy_string,
    to_legacy_string,
)


class TestFailureReasonValues:
    """Verify enum member existence and numeric grouping."""

    def test_none(self) -> None:
        assert FailureReason.NONE == 0

    def test_pending_human(self) -> None:
        assert FailureReason.PENDING_HUMAN == 100

    def test_merge_conflict(self) -> None:
        assert FailureReason.MERGE_CONFLICT == 200

    def test_fast_abort(self) -> None:
        assert FailureReason.FAST_ABORT == 400

    def test_guardrail_violation(self) -> None:
        assert FailureReason.GUARDRAIL_VIOLATION == 500

    def test_rollback_failed(self) -> None:
        assert FailureReason.ROLLBACK_FAILED == 600

    def test_runtime_sync_collision(self) -> None:
        assert FailureReason.RUNTIME_SYNC_COLLISION == 800

    def test_postcond_branch_not_merged(self) -> None:
        assert FailureReason.POSTCOND_BRANCH_NOT_MERGED == 900

    def test_lock_busy(self) -> None:
        assert FailureReason.LOCK_BUSY == 990

    def test_unexpected(self) -> None:
        assert FailureReason.UNEXPECTED == 9999


class TestLegacyString:
    """Round-trip between enum and legacy string forms."""

    def test_legacy_string_property(self) -> None:
        assert FailureReason.MERGE_CONFLICT.legacy_string == "merge_conflict"
        assert FailureReason.GUARDRAIL_VIOLATION.legacy_string == "guardrail_violation"
        assert FailureReason.FAST_ABORT.legacy_string == "fast_abort"

    def test_from_legacy_string_exact_match(self) -> None:
        reason, detail = from_legacy_string("merge_conflict")
        assert reason is FailureReason.MERGE_CONFLICT
        assert detail is None

    def test_from_legacy_string_none(self) -> None:
        reason, detail = from_legacy_string(None)
        assert reason is None
        assert detail is None

    def test_from_legacy_string_empty(self) -> None:
        reason, detail = from_legacy_string("")
        assert reason is FailureReason.NONE
        assert detail is None

    def test_from_legacy_string_whitespace(self) -> None:
        reason, detail = from_legacy_string("  ")
        assert reason is FailureReason.NONE
        assert detail is None

    def test_from_legacy_string_unknown(self) -> None:
        reason, detail = from_legacy_string("totally_unknown_reason")
        assert reason is FailureReason.UNEXPECTED
        assert detail == "totally_unknown_reason"

    def test_from_legacy_string_compound_fast_abort(self) -> None:
        reason, detail = from_legacy_string("fast_abort: subprocess timed out")
        assert reason is FailureReason.FAST_ABORT
        assert detail == "subprocess timed out"

    def test_from_legacy_string_compound_fast_failure(self) -> None:
        reason, detail = from_legacy_string("fast_failure: LLM refused")
        assert reason is FailureReason.FAST_FAILURE
        assert detail == "LLM refused"

    def test_from_legacy_string_compound_merge_failed(self) -> None:
        reason, detail = from_legacy_string("merge_failed: exit code 1")
        assert reason is FailureReason.MERGE_FAILED
        assert detail == "exit code 1"

    def test_from_legacy_string_bare_prefix_no_separator(self) -> None:
        # "fast_abort_extra" should NOT match the fast_abort prefix
        reason, detail = from_legacy_string("fast_abort_extra")
        assert reason is FailureReason.UNEXPECTED
        assert detail == "fast_abort_extra"

    def test_to_legacy_string_none(self) -> None:
        assert to_legacy_string(None) is None

    def test_to_legacy_string_none_reason(self) -> None:
        assert to_legacy_string(FailureReason.NONE) == ""

    def test_to_legacy_string_normal(self) -> None:
        assert to_legacy_string(FailureReason.MERGE_CONFLICT) == "merge_conflict"

    def test_round_trip_all_members(self) -> None:
        """Every member except UNEXPECTED round-trips exactly."""
        for member in FailureReason:
            if member is FailureReason.UNEXPECTED:
                continue
            legacy = to_legacy_string(member)
            parsed, detail = from_legacy_string(legacy)
            assert parsed is member, f"Round-trip failed for {member.name}"
            assert detail is None


class TestFailureReasonComparison:
    """IntEnum supports numeric comparison."""

    def test_lt(self) -> None:
        assert FailureReason.NONE < FailureReason.MERGE_CONFLICT

    def test_gt(self) -> None:
        assert FailureReason.UNEXPECTED > FailureReason.LOCK_BUSY

    def test_equality(self) -> None:
        assert FailureReason.MERGE_CONFLICT == FailureReason.MERGE_CONFLICT
