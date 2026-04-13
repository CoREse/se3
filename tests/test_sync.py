"""Tests for SE3 Sync — data models, CLI registration, and IssueManager extensions."""

from __future__ import annotations

from datetime import datetime

import pytest
import yaml

from se3.engine.issue_manager import Issue, IssueManager, IssueStatus
from se3.engine.sync_engine import (
    Conflict,
    ConflictDecision,
    DiffType,
    SpecAnalysis,
    SpecDiff,
    SyncResult,
)


# ---------------------------------------------------------------------------
# Data model tests
# ---------------------------------------------------------------------------

class TestDiffType:
    def test_enum_values(self):
        assert DiffType.GAP.value == "gap"
        assert DiffType.EXTENSION.value == "extension"
        assert DiffType.CONFLICT.value == "conflict"

    def test_from_string(self):
        assert DiffType("gap") == DiffType.GAP
        assert DiffType("extension") == DiffType.EXTENSION
        assert DiffType("conflict") == DiffType.CONFLICT


class TestSpecDiff:
    def test_to_dict_roundtrip(self):
        diff = SpecDiff(
            diff_type=DiffType.GAP,
            spec_name="auth",
            description="Missing login endpoint",
            code_location="src/api/auth.py:42",
        )
        data = diff.to_dict()
        restored = SpecDiff.from_dict(data)

        assert restored.diff_type == DiffType.GAP
        assert restored.spec_name == "auth"
        assert restored.description == "Missing login endpoint"
        assert restored.code_location == "src/api/auth.py:42"

    def test_from_dict_defaults(self):
        data = {
            "diff_type": "extension",
            "spec_name": "base",
            "description": "New helper function",
        }
        diff = SpecDiff.from_dict(data)
        assert diff.code_location == ""

    def test_all_diff_types(self):
        for dt in DiffType:
            diff = SpecDiff(diff_type=dt, spec_name="test", description="d")
            data = diff.to_dict()
            assert data["diff_type"] == dt.value
            assert SpecDiff.from_dict(data).diff_type == dt


class TestSpecAnalysis:
    def _make_analysis(self):
        return SpecAnalysis(
            spec_name="auth",
            diffs=[
                SpecDiff(DiffType.GAP, "auth", "Missing feature A"),
                SpecDiff(DiffType.GAP, "auth", "Missing feature B"),
                SpecDiff(DiffType.EXTENSION, "auth", "Extra helper"),
                SpecDiff(DiffType.CONFLICT, "auth", "Inconsistent behavior"),
            ],
        )

    def test_gaps_property(self):
        a = self._make_analysis()
        assert len(a.gaps) == 2
        assert all(d.diff_type == DiffType.GAP for d in a.gaps)

    def test_extensions_property(self):
        a = self._make_analysis()
        assert len(a.extensions) == 1

    def test_conflicts_property(self):
        a = self._make_analysis()
        assert len(a.conflicts) == 1

    def test_is_in_sync_false(self):
        a = self._make_analysis()
        assert not a.is_in_sync

    def test_is_in_sync_true(self):
        a = SpecAnalysis(spec_name="clean", diffs=[])
        assert a.is_in_sync

    def test_to_dict_roundtrip(self):
        a = self._make_analysis()
        data = a.to_dict()
        restored = SpecAnalysis.from_dict(data)

        assert restored.spec_name == "auth"
        assert len(restored.diffs) == 4
        assert len(restored.gaps) == 2
        assert len(restored.extensions) == 1
        assert len(restored.conflicts) == 1

    def test_from_dict_empty_diffs(self):
        data = {"spec_name": "empty"}
        a = SpecAnalysis.from_dict(data)
        assert a.diffs == []
        assert a.is_in_sync


class TestConflict:
    def test_to_dict_roundtrip(self):
        c = Conflict(
            spec_name="auth",
            description="Token format mismatch",
            spec_content="JWT tokens",
            code_content="Session tokens",
            code_location="src/auth.py:10",
            decision=ConflictDecision.UPDATE_SPEC,
        )
        data = c.to_dict()
        restored = Conflict.from_dict(data)

        assert restored.spec_name == "auth"
        assert restored.description == "Token format mismatch"
        assert restored.spec_content == "JWT tokens"
        assert restored.code_content == "Session tokens"
        assert restored.code_location == "src/auth.py:10"
        assert restored.decision == ConflictDecision.UPDATE_SPEC

    def test_defaults(self):
        c = Conflict(spec_name="x", description="d")
        assert c.spec_content == ""
        assert c.code_content == ""
        assert c.code_location == ""
        assert c.decision == ConflictDecision.PENDING

    def test_all_decisions(self):
        assert ConflictDecision.PENDING.value == "pending"
        assert ConflictDecision.UPDATE_SPEC.value == "update_spec"
        assert ConflictDecision.CREATE_ISSUE.value == "create_issue"


class TestSyncResult:
    def _make_result(self):
        return SyncResult(
            analyses=[
                SpecAnalysis(
                    spec_name="auth",
                    diffs=[
                        SpecDiff(DiffType.GAP, "auth", "g1"),
                        SpecDiff(DiffType.EXTENSION, "auth", "e1"),
                        SpecDiff(DiffType.CONFLICT, "auth", "c1"),
                    ],
                ),
                SpecAnalysis(
                    spec_name="base",
                    diffs=[
                        SpecDiff(DiffType.GAP, "base", "g2"),
                    ],
                ),
            ],
            issues_created=2,
            issues_closed=1,
            specs_updated=1,
            conflicts=[Conflict(spec_name="auth", description="c1")],
        )

    def test_total_gaps(self):
        r = self._make_result()
        assert r.total_gaps == 2

    def test_total_extensions(self):
        r = self._make_result()
        assert r.total_extensions == 1

    def test_total_conflicts(self):
        r = self._make_result()
        assert r.total_conflicts == 1

    def test_all_in_sync_false(self):
        r = self._make_result()
        assert not r.all_in_sync

    def test_all_in_sync_true(self):
        r = SyncResult(analyses=[
            SpecAnalysis(spec_name="clean1"),
            SpecAnalysis(spec_name="clean2"),
        ])
        assert r.all_in_sync

    def test_to_dict_roundtrip(self):
        r = self._make_result()
        data = r.to_dict()
        restored = SyncResult.from_dict(data)

        assert len(restored.analyses) == 2
        assert restored.issues_created == 2
        assert restored.issues_closed == 1
        assert restored.specs_updated == 1
        assert len(restored.conflicts) == 1

    def test_to_json(self):
        r = self._make_result()
        json_str = r.to_json()
        assert '"auth"' in json_str
        assert '"gap"' in json_str

    def test_empty_result(self):
        r = SyncResult()
        assert r.total_gaps == 0
        assert r.total_extensions == 0
        assert r.total_conflicts == 0
        assert r.all_in_sync
        assert r.issues_created == 0
        assert r.call_file is None


# ---------------------------------------------------------------------------
# IssueManager extension tests
# ---------------------------------------------------------------------------

class TestIssueManagerFindOpenByTitle:
    def test_finds_matching_issue(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Fix login bug", "Login fails on submit")
        mgr.create("Add signup page", "Need registration form")

        found = mgr.find_open_by_title("login")
        assert found is not None
        assert found.title == "Fix login bug"

    def test_case_insensitive(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Fix LOGIN Bug", "desc")

        found = mgr.find_open_by_title("login bug")
        assert found is not None
        assert found.title == "Fix LOGIN Bug"

    def test_returns_none_when_no_match(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Fix login bug", "desc")

        found = mgr.find_open_by_title("signup")
        assert found is None

    def test_ignores_closed_issues(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Fix login bug", "desc")
        mgr.update_status("001", IssueStatus.WONT_FIX)

        found = mgr.find_open_by_title("login")
        assert found is None

    def test_returns_first_match(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Login error A", "desc")
        mgr.create("Login error B", "desc")

        found = mgr.find_open_by_title("Login error")
        assert found is not None
        assert found.id == "001"

    def test_empty_directory(self, tmp_path):
        mgr = IssueManager(tmp_path)
        found = mgr.find_open_by_title("anything")
        assert found is None


class TestIssueManagerCloseIssue:
    def test_close_open_issue(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test issue", "desc")

        closed = mgr.close_issue("001", reason="Gap resolved")
        assert closed.status == IssueStatus.CLOSED
        assert len(list(mgr.closed_dir.glob("001_*"))) == 1
        assert len(list(mgr.open_dir.glob("001_*"))) == 0

    def test_close_in_progress_issue(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test issue", "desc")
        mgr.update_status("001", IssueStatus.IN_PROGRESS)

        closed = mgr.close_issue("001", reason="Done")
        assert closed.status == IssueStatus.RESOLVED
        assert len(list(mgr.closed_dir.glob("001_*"))) == 1

    def test_close_already_closed_is_noop(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test issue", "desc")
        mgr.update_status("001", IssueStatus.WONT_FIX)

        result = mgr.close_issue("001")
        assert result.status == IssueStatus.WONT_FIX

    def test_close_nonexistent_raises(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr._ensure_dirs()

        with pytest.raises(ValueError, match="not found"):
            mgr.close_issue("999")


class TestIssueManagerListByTags:
    def test_filter_by_single_tag(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Issue A", "d", tags=["source:sync", "area:auth"])
        mgr.create("Issue B", "d", tags=["source:manual"])
        mgr.create("Issue C", "d", tags=["source:sync", "area:db"])

        result = mgr.list_by_tags(["source:sync"])
        assert len(result) == 2
        assert {i.title for i in result} == {"Issue A", "Issue C"}

    def test_filter_by_multiple_tags(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Issue A", "d", tags=["source:sync", "area:auth"])
        mgr.create("Issue B", "d", tags=["source:sync"])
        mgr.create("Issue C", "d", tags=["source:sync", "area:auth", "priority:high"])

        result = mgr.list_by_tags(["source:sync", "area:auth"])
        assert len(result) == 2
        assert {i.title for i in result} == {"Issue A", "Issue C"}

    def test_no_matching_tags(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Issue A", "d", tags=["source:manual"])

        result = mgr.list_by_tags(["source:sync"])
        assert result == []

    def test_empty_tags_returns_all(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Issue A", "d", tags=["x"])
        mgr.create("Issue B", "d", tags=["y"])

        result = mgr.list_by_tags([])
        assert len(result) == 2

    def test_includes_closed_when_requested(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Open", "d", tags=["source:sync"])
        mgr.create("To close", "d", tags=["source:sync"])
        mgr.update_status("002", IssueStatus.WONT_FIX)

        open_only = mgr.list_by_tags(["source:sync"], include_closed=False)
        assert len(open_only) == 1

        with_closed = mgr.list_by_tags(["source:sync"], include_closed=True)
        assert len(with_closed) == 2

    def test_sorted_by_id(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("C", "d", tags=["t"])
        mgr.create("A", "d", tags=["t"])
        mgr.create("B", "d", tags=["t"])

        result = mgr.list_by_tags(["t"])
        assert [i.id for i in result] == ["001", "002", "003"]


# ---------------------------------------------------------------------------
# CLI registration test
# ---------------------------------------------------------------------------

class TestSyncCLI:
    def test_sync_command_imports(self):
        from se3.commands.sync import SyncMode, sync_command
        assert SyncMode.DEFAULT.value == "default"
        assert SyncMode.STRICT.value == "strict"
        assert SyncMode.FAST.value == "fast"
        assert callable(sync_command)

    def test_sync_mode_enum(self):
        from se3.commands.sync import SyncMode
        assert SyncMode("default") == SyncMode.DEFAULT
        assert SyncMode("strict") == SyncMode.STRICT
        assert SyncMode("fast") == SyncMode.FAST

        with pytest.raises(ValueError):
            SyncMode("invalid")
