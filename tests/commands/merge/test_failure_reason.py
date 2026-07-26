"""Tests for FailureReason enum and legacy-string compatibility."""

from __future__ import annotations

import pytest

from tianluo.commands.merge.failure_reason import (
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

    def test_dirty_working_tree(self) -> None:
        assert FailureReason.DIRTY_WORKING_TREE == 934

    def test_dirty_working_tree_in_repo_state_group(self) -> None:
        # Belongs to the 93x repo-state fail-fast family, ordered after
        # the REPO_* states and before the 98x input-validation group.
        assert (
            FailureReason.REPO_UNSUPPORTED_STATE
            < FailureReason.DIRTY_WORKING_TREE
            < FailureReason.NO_BRANCHES
        )

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

    def test_dirty_working_tree_legacy_string_property(self) -> None:
        assert (
            FailureReason.DIRTY_WORKING_TREE.legacy_string == "dirty_working_tree"
        )

    def test_from_legacy_string_dirty_working_tree(self) -> None:
        reason, detail = from_legacy_string("dirty_working_tree")
        assert reason is FailureReason.DIRTY_WORKING_TREE
        assert detail is None

    def test_dirty_working_tree_round_trips(self) -> None:
        legacy = to_legacy_string(FailureReason.DIRTY_WORKING_TREE)
        assert legacy == "dirty_working_tree"
        parsed, detail = from_legacy_string(legacy)
        assert parsed is FailureReason.DIRTY_WORKING_TREE
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

    def test_every_member_has_legacy_map_entry(self) -> None:
        """Each enum member's ``legacy_string`` MUST be a key in
        ``_LEGACY_STRING_MAP`` and resolve back to the same member.

        Catches typos in future enum additions: a member named
        ``POSTCOND_VERSION_NOT_BUMPPED`` (typo) whose
        ``legacy_string`` of ``"postcond_version_not_bumpped"`` is not
        in the map would silently resolve to ``FailureReason.UNEXPECTED``
        at runtime. This test fails loudly on such drift.
        """
        from tianluo.commands.merge.failure_reason import _LEGACY_STRING_MAP

        for member in FailureReason:
            if member is FailureReason.NONE:
                # NONE has both "" and "none" entries; legacy_string
                # returns "none" but to_legacy_string returns "".
                assert _LEGACY_STRING_MAP.get("none") is FailureReason.NONE
                assert _LEGACY_STRING_MAP.get("") is FailureReason.NONE
                continue
            if member is FailureReason.UNEXPECTED:
                # UNEXPECTED is the catch-all; its legacy_string must
                # round-trip too so explicitly recorded "unexpected"
                # diagnostics survive serialization.
                assert (
                    _LEGACY_STRING_MAP.get(member.legacy_string) is member
                ), f"UNEXPECTED missing legacy map entry"
                continue
            legacy = member.legacy_string
            assert legacy in _LEGACY_STRING_MAP, (
                f"FailureReason.{member.name}.legacy_string={legacy!r} is "
                f"not in _LEGACY_STRING_MAP — adding the enum without the "
                f"corresponding map entry would silently route to "
                f"FailureReason.UNEXPECTED at runtime."
            )
            assert _LEGACY_STRING_MAP[legacy] is member, (
                f"FailureReason.{member.name}.legacy_string={legacy!r} "
                f"maps to {_LEGACY_STRING_MAP[legacy].name}, not "
                f"{member.name} — the map and the enum disagree."
            )


class TestFailureReasonComparison:
    """IntEnum supports numeric comparison."""

    def test_lt(self) -> None:
        assert FailureReason.NONE < FailureReason.MERGE_CONFLICT

    def test_gt(self) -> None:
        assert FailureReason.UNEXPECTED > FailureReason.LOCK_BUSY

    def test_equality(self) -> None:
        assert FailureReason.MERGE_CONFLICT == FailureReason.MERGE_CONFLICT
