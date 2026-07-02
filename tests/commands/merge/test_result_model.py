"""Tests for MergeOutcome and MergeReport typed result models."""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.commands.merge.failure_reason import FailureReason
from se3.commands.merge.result_model import MergeOutcome, MergeReport


class TestMergeOutcome:
    """Unit tests for per-branch outcome records."""

    def test_defaults(self) -> None:
        o = MergeOutcome(branch="feat/a")
        assert o.branch == "feat/a"
        assert o.success is False
        assert o.failure_reason is None
        assert o.already_ancestor is False
        assert o.warnings_repaired is False
        assert o.merge_commit_sha is None

    def test_success_no_warnings(self) -> None:
        o = MergeOutcome(
            branch="feat/a",
            success=True,
            merge_commit_sha="abc1234",
        )
        assert o.success is True
        assert o.merge_commit_sha == "abc1234"

    def test_success_with_warnings(self) -> None:
        o = MergeOutcome(
            branch="feat/a",
            success=True,
            warnings_repaired=True,
            merge_commit_sha="abc1234",
        )
        assert o.warnings_repaired is True

    def test_already_ancestor(self) -> None:
        o = MergeOutcome(
            branch="feat/a",
            already_ancestor=True,
        )
        assert o.already_ancestor is True
        assert o.success is False  # not a "success" per se

    def test_failure_with_reason(self) -> None:
        o = MergeOutcome(
            branch="feat/a",
            success=False,
            failure_reason=FailureReason.MERGE_CONFLICT,
            failure_detail="conflict in src/main.py",
        )
        assert o.failure_reason is FailureReason.MERGE_CONFLICT
        assert o.failure_detail == "conflict in src/main.py"

    def test_pending_human(self) -> None:
        o = MergeOutcome(
            branch="feat/a",
            success=False,
            failure_reason=FailureReason.PENDING_HUMAN,
        )
        assert o.failure_reason is FailureReason.PENDING_HUMAN


class TestMergeReportDefaults:
    """Default construction."""

    def test_empty_report(self) -> None:
        r = MergeReport()
        assert r.success is False
        assert r.newly_merged_branches == []
        assert r.already_ancestor_branches == []
        assert r.merged_with_warnings == []
        assert r.failed_branch is None
        assert r.failure_reason is None
        assert r.call_file_str is None

    def test_none_call_file_str(self) -> None:
        r = MergeReport()
        assert r.call_file_str is None
        assert r.human_call_file is None

    def test_call_file_str_present(self) -> None:
        r = MergeReport(human_call_file=Path("/tmp/call.json"))
        assert r.call_file_str == "/tmp/call.json"


class TestMergeReportAddOutcome:
    """Semantic bucket population via add_outcome."""

    def test_add_newly_merged(self) -> None:
        r = MergeReport()
        r.add_outcome(MergeOutcome(branch="feat/a", success=True))
        assert "feat/a" in r.newly_merged_branches
        assert "feat/a" not in r.already_ancestor_branches
        assert "feat/a" not in r.merged_with_warnings
        assert r.all_merged_branches == ["feat/a"]
        # Alias property
        assert r.newly_merged == ["feat/a"]

    def test_add_already_ancestor(self) -> None:
        r = MergeReport()
        r.add_outcome(MergeOutcome(branch="feat/a", already_ancestor=True))
        assert "feat/a" in r.already_ancestor_branches
        assert "feat/a" not in r.newly_merged_branches
        assert r.all_merged_branches == ["feat/a"]
        # Alias property
        assert r.already_merged == ["feat/a"]

    def test_add_merged_with_warnings(self) -> None:
        r = MergeReport()
        r.add_outcome(
            MergeOutcome(branch="feat/a", success=True, warnings_repaired=True)
        )
        assert "feat/a" in r.merged_with_warnings
        assert "feat/a" not in r.newly_merged_branches
        assert r.all_merged_branches == ["feat/a"]

    def test_add_failure_sets_failed_branch(self) -> None:
        r = MergeReport()
        r.add_outcome(
            MergeOutcome(branch="feat/a", failure_reason=FailureReason.MERGE_CONFLICT)
        )
        assert r.failed_branch == "feat/a"
        assert "feat/a" not in r.newly_merged_branches
        assert "feat/a" not in r.already_ancestor_branches
        assert "feat/a" not in r.merged_with_warnings
        assert r.all_merged_branches == []

    def test_add_failure_not_in_any_bucket(self) -> None:
        r = MergeReport()
        r.add_outcome(
            MergeOutcome(branch="feat/a", failure_reason=FailureReason.MERGE_CONFLICT)
        )
        assert "feat/a" not in r.newly_merged_branches
        assert "feat/a" not in r.already_ancestor_branches
        assert "feat/a" not in r.merged_with_warnings
        assert r.all_merged_branches == []

    def test_add_pending_human_not_in_any_bucket(self) -> None:
        r = MergeReport()
        r.add_outcome(
            MergeOutcome(branch="feat/a", failure_reason=FailureReason.PENDING_HUMAN)
        )
        assert "feat/a" not in r.newly_merged_branches
        assert "feat/a" not in r.already_ancestor_branches
        assert r.all_merged_branches == []
        # Pending-human does NOT set failed_branch
        assert r.failed_branch is None

    def test_multiple_outcomes(self) -> None:
        r = MergeReport()
        r.add_outcome(MergeOutcome(branch="feat/a", success=True))
        r.add_outcome(MergeOutcome(branch="feat/b", already_ancestor=True))
        r.add_outcome(
            MergeOutcome(branch="feat/c", success=True, warnings_repaired=True)
        )
        r.add_outcome(
            MergeOutcome(branch="feat/d", failure_reason=FailureReason.MERGE_CONFLICT)
        )
        assert r.newly_merged_branches == ["feat/a"]
        assert r.already_ancestor_branches == ["feat/b"]
        assert r.merged_with_warnings == ["feat/c"]
        assert r.failed_branch == "feat/d"


class TestMergeReportLegacyCompatibility:
    """Backward-compatible dict serialization."""

    def test_to_legacy_dict_shape(self) -> None:
        r = MergeReport(
            success=True,
            newly_merged_branches=["feat/a"],
            already_ancestor_branches=["feat/b"],
            merged_with_warnings=["feat/c"],
            pre_merge_version="1.0.0",
            final_version="1.1.0",
            bump_type="minor",
        )
        d = r.to_legacy_dict()
        assert d["success"] is True
        assert d["newly_merged_branches"] == ["feat/a"]
        assert d["already_ancestor_branches"] == ["feat/b"]
        assert d["merged_with_warnings"] == ["feat/c"]
        assert d["merged_branches"] == ["feat/a", "feat/b", "feat/c"]
        assert d["pre_merge_version"] == "1.0.0"
        assert d["failure_reason"] == ""  # NONE -> empty string

    def test_to_legacy_dict_with_failure(self) -> None:
        r = MergeReport(
            success=False,
            failed_branch="feat/x",
            failure_reason=FailureReason.MERGE_CONFLICT,
            failure_detail="conflict in main.py",
        )
        d = r.to_legacy_dict()
        assert d["failure_reason"] == "merge_conflict"
        assert d["failure_detail"] == "conflict in main.py"
        assert d["failed_branch"] == "feat/x"

    def test_to_legacy_dict_outcomes(self) -> None:
        r = MergeReport()
        r.add_outcome(MergeOutcome(branch="feat/a", success=True))
        d = r.to_legacy_dict()
        assert len(d["outcomes"]) == 1
        assert d["outcomes"][0]["branch"] == "feat/a"
        assert d["outcomes"][0]["success"] is True
        assert d["outcomes"][0]["failure_reason"] == ""

    def test_to_legacy_dict_committed_issue_renumbers(self) -> None:
        """Committed-issue renumbers survive serialization as plain dicts."""
        from se3.engine.merge.runtime_sync import IssueMergeRecord

        r = MergeReport()
        r.committed_issue_renumbers.append(
            IssueMergeRecord(old_id="005", new_id="011", status_dir="open")
        )
        d = r.to_legacy_dict()
        assert d["committed_issue_renumbers"] == [
            {"old_id": "005", "new_id": "011", "status_dir": "open"}
        ]

    def test_merged_branches_alias(self) -> None:
        r = MergeReport()
        r.add_outcome(MergeOutcome(branch="feat/a", success=True))
        r.add_outcome(MergeOutcome(branch="feat/b", already_ancestor=True))
        # When merged_branches field is empty, all_merged_branches returns
        # the semantic buckets.
        assert r.all_merged_branches == ["feat/a", "feat/b"]
        # Legacy field is also accessible for backward compatibility.
        assert r.merged_branches == []
