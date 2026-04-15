"""Tests for SE3 Sync — data models, CLI registration, IssueManager extensions, and SyncAnalyzer."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

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
from se3.engine.sync_analyzer import SyncAnalyzer


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
    def test_finds_exact_matching_issue(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Fix login bug", "Login fails on submit")
        mgr.create("Add signup page", "Need registration form")

        found = mgr.find_open_by_title("Fix login bug")
        assert found is not None
        assert found.title == "Fix login bug"

    def test_substring_no_longer_matches(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Fix login bug", "Login fails on submit")

        found = mgr.find_open_by_title("login")
        assert found is None

    def test_case_insensitive(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Fix LOGIN Bug", "desc")

        found = mgr.find_open_by_title("fix login bug")
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

        found = mgr.find_open_by_title("Fix login bug")
        assert found is None

    def test_returns_first_match(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Login error", "desc")
        mgr.create("Login error", "desc2")

        found = mgr.find_open_by_title("Login error")
        assert found is not None
        assert found.id == "001"

    def test_empty_directory(self, tmp_path):
        mgr = IssueManager(tmp_path)
        found = mgr.find_open_by_title("anything")
        assert found is None

    def test_empty_title_returns_none(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Some issue", "desc")
        found = mgr.find_open_by_title("")
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
# IssueManager extension boundary tests
# ---------------------------------------------------------------------------

class TestFindOpenByTitleBoundary:
    def test_exact_match(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: Missing login", "desc")

        found = mgr.find_open_by_title("[sync] auth: Missing login")
        assert found is not None
        assert found.title == "[sync] auth: Missing login"

    def test_partial_match_at_start_no_longer_matches(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: Missing login", "desc")

        found = mgr.find_open_by_title("[sync]")
        assert found is None

    def test_partial_match_at_end_no_longer_matches(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: Missing login", "desc")

        found = mgr.find_open_by_title("Missing login")
        assert found is None

    def test_special_characters_exact_match(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("[sync-conflict] auth: Token (JWT)", "desc")

        found = mgr.find_open_by_title("[sync-conflict] auth: Token (JWT)")
        assert found is not None

    def test_unicode_exact_match(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("修复登录缺陷", "描述")

        found = mgr.find_open_by_title("修复登录缺陷")
        assert found is not None


class TestCloseIssueBoundary:
    def test_close_preserves_issue_data(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test issue", "detailed description", priority="high", tags=["source:sync"])

        closed = mgr.close_issue("001", reason="resolved")
        assert closed.title == "Test issue"
        assert closed.description == "detailed description"
        assert closed.priority == "high"
        assert "source:sync" in closed.tags

    def test_close_moves_to_closed_dir(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Test issue", "desc")

        mgr.close_issue("001")
        assert len(list(mgr.closed_dir.glob("001_*"))) == 1
        assert len(list(mgr.open_dir.glob("001_*"))) == 0

    def test_close_sets_updated_at(self, tmp_path):
        mgr = IssueManager(tmp_path)
        issue = mgr.create("Test issue", "desc")
        original_updated = issue.updated_at

        closed = mgr.close_issue("001")
        assert closed.updated_at >= original_updated


class TestListByTagsBoundary:
    def test_subset_tag_matching(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("A", "d", tags=["source:sync", "auto-discovered", "extra"])

        result = mgr.list_by_tags(["source:sync", "auto-discovered"])
        assert len(result) == 1

    def test_superset_tags_does_not_match(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("A", "d", tags=["source:sync"])

        result = mgr.list_by_tags(["source:sync", "extra-tag-not-on-issue"])
        assert len(result) == 0

    def test_empty_issue_tags_does_not_match(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("A", "d", tags=[])

        result = mgr.list_by_tags(["source:sync"])
        assert len(result) == 0

    def test_many_issues_performance(self, tmp_path):
        mgr = IssueManager(tmp_path)
        for i in range(20):
            tags = ["source:sync"] if i % 2 == 0 else ["manual"]
            mgr.create(f"Issue {i}", "d", tags=tags)

        result = mgr.list_by_tags(["source:sync"])
        assert len(result) == 10


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

    @patch("se3.engine.sync_engine.SyncEngine.run")
    def test_sync_command_calls_engine_run(self, mock_run, tmp_path):
        from se3.commands.sync import SyncMode, sync_command

        mock_run.return_value = SyncResult()
        sync_command(mode=SyncMode.DEFAULT, project_root=tmp_path)
        mock_run.assert_called_once()

    @patch("se3.engine.sync_engine.SyncEngine.run")
    def test_sync_command_passes_mode_to_engine(self, mock_run, tmp_path):
        from se3.commands.sync import SyncMode, sync_command

        mock_run.return_value = SyncResult()
        sync_command(mode=SyncMode.FAST, project_root=tmp_path)

    @patch("se3.engine.sync_engine.SyncEngine.run")
    def test_sync_command_renders_all_in_sync(self, mock_run, tmp_path, capsys):
        from se3.commands.sync import SyncMode, sync_command

        mock_run.return_value = SyncResult(
            analyses=[SpecAnalysis(spec_name="base", diffs=[])],
        )
        sync_command(mode=SyncMode.DEFAULT, project_root=tmp_path)

    @patch("se3.engine.sync_engine.SyncEngine.run")
    def test_sync_command_renders_diffs_found(self, mock_run, tmp_path):
        from se3.commands.sync import SyncMode, sync_command

        mock_run.return_value = SyncResult(
            analyses=[
                SpecAnalysis(
                    spec_name="auth",
                    diffs=[
                        SpecDiff(DiffType.GAP, "auth", "Missing login"),
                        SpecDiff(DiffType.EXTENSION, "auth", "Extra feature"),
                    ],
                ),
            ],
            issues_created=1,
            specs_updated=1,
        )
        sync_command(mode=SyncMode.DEFAULT, project_root=tmp_path)

    @patch("se3.engine.sync_engine.SyncEngine.run")
    def test_sync_command_renders_conflicts_with_call_file(self, mock_run, tmp_path):
        from se3.commands.sync import SyncMode, sync_command

        mock_run.return_value = SyncResult(
            analyses=[
                SpecAnalysis(
                    spec_name="auth",
                    diffs=[SpecDiff(DiffType.CONFLICT, "auth", "Token mismatch")],
                ),
            ],
            conflicts=[Conflict(spec_name="auth", description="Token mismatch")],
            call_file="/tmp/se3/calls/sync_conflicts_123.json",
        )
        sync_command(mode=SyncMode.STRICT, project_root=tmp_path)


# ---------------------------------------------------------------------------
# SyncAnalyzer tests
# ---------------------------------------------------------------------------

class TestSyncAnalyzerInit:
    def test_init_stores_attributes(self, tmp_path):
        caller = MagicMock()
        analyzer = SyncAnalyzer(tmp_path, caller)
        assert analyzer.project_root == tmp_path
        assert analyzer.llm_caller is caller


class TestBuildAnalysisPrompt:
    def test_prompt_contains_spec_name(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        prompt = analyzer._build_analysis_prompt("auth", "spec text", "context")
        assert "auth" in prompt

    def test_prompt_contains_spec_content(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        prompt = analyzer._build_analysis_prompt("x", "SHALL validate inputs", "ctx")
        assert "SHALL validate inputs" in prompt

    def test_prompt_contains_project_context(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        prompt = analyzer._build_analysis_prompt("x", "spec", "project files here")
        assert "project files here" in prompt

    def test_prompt_defines_three_diff_types(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        prompt = analyzer._build_analysis_prompt("x", "s", "c")
        assert "gap" in prompt.lower()
        assert "extension" in prompt.lower()
        assert "conflict" in prompt.lower()

    def test_prompt_contains_json_schema(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        prompt = analyzer._build_analysis_prompt("x", "s", "c")
        assert '"diffs"' in prompt
        assert '"type"' in prompt
        assert '"description"' in prompt
        assert '"code_location"' in prompt


class TestParseAnalysisResponse:
    def test_parse_valid_response(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        response = json.dumps({
            "diffs": [
                {
                    "type": "gap",
                    "description": "Missing login endpoint",
                    "spec_requirement": "SHALL provide login",
                    "code_location": "src/auth.py",
                },
                {
                    "type": "extension",
                    "description": "Extra helper function",
                    "spec_requirement": "",
                    "code_location": "src/utils.py:10",
                },
            ]
        })
        result = analyzer._parse_analysis_response("auth", response)
        assert result.spec_name == "auth"
        assert len(result.diffs) == 2
        assert result.diffs[0].diff_type == DiffType.GAP
        assert result.diffs[0].description == "Missing login endpoint"
        assert result.diffs[1].diff_type == DiffType.EXTENSION

    def test_parse_empty_diffs(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        response = json.dumps({"diffs": []})
        result = analyzer._parse_analysis_response("clean", response)
        assert result.is_in_sync
        assert result.diffs == []

    def test_parse_all_diff_types(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        response = json.dumps({
            "diffs": [
                {"type": "gap", "description": "g"},
                {"type": "extension", "description": "e"},
                {"type": "conflict", "description": "c"},
            ]
        })
        result = analyzer._parse_analysis_response("spec", response)
        assert len(result.gaps) == 1
        assert len(result.extensions) == 1
        assert len(result.conflicts) == 1

    def test_parse_skips_unknown_diff_type(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        response = json.dumps({
            "diffs": [
                {"type": "gap", "description": "valid"},
                {"type": "unknown_type", "description": "invalid"},
            ]
        })
        result = analyzer._parse_analysis_response("spec", response)
        assert len(result.diffs) == 1
        assert result.diffs[0].diff_type == DiffType.GAP

    def test_parse_invalid_json(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        result = analyzer._parse_analysis_response("spec", "not json at all")
        assert len(result.diffs) == 1
        assert result.diffs[0].diff_type == DiffType.CONFLICT
        assert "JSON parse error" in result.diffs[0].description

    def test_parse_missing_diffs_key(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        response = json.dumps({"something_else": True})
        result = analyzer._parse_analysis_response("spec", response)
        assert result.is_in_sync

    def test_parse_preserves_code_location(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        response = json.dumps({
            "diffs": [
                {
                    "type": "gap",
                    "description": "missing",
                    "code_location": "src/api/routes.py:42",
                }
            ]
        })
        result = analyzer._parse_analysis_response("spec", response)
        assert result.diffs[0].code_location == "src/api/routes.py:42"

    def test_parse_defaults_code_location(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        response = json.dumps({
            "diffs": [{"type": "gap", "description": "missing"}]
        })
        result = analyzer._parse_analysis_response("spec", response)
        assert result.diffs[0].code_location == ""


class TestAnalyzeSpec:
    def test_successful_analysis(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = json.dumps({
            "diffs": [
                {"type": "gap", "description": "Missing feature X"},
            ]
        })
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec("auth", "spec content", "context")

        assert result.spec_name == "auth"
        assert len(result.diffs) == 1
        assert result.diffs[0].diff_type == DiffType.GAP
        caller.call.assert_called_once()

    def test_uses_extract_json_mode(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = json.dumps({"diffs": []})
        analyzer = SyncAnalyzer(tmp_path, caller)
        analyzer.analyze_spec("x", "spec", "ctx")

        call_kwargs = caller.call.call_args
        assert call_kwargs.kwargs.get("json_mode") == "extract"

    def test_retries_on_llm_error(self, tmp_path):
        from se3.engine.llm_caller import LLMCallError

        caller = MagicMock()
        caller.call.side_effect = [
            LLMCallError("rate limit"),
            json.dumps({"diffs": [{"type": "gap", "description": "found"}]}),
        ]
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec("auth", "spec", "ctx")

        assert caller.call.call_count == 2
        assert len(result.diffs) == 1
        assert result.diffs[0].diff_type == DiffType.GAP

    def test_retries_on_generic_exception(self, tmp_path):
        caller = MagicMock()
        caller.call.side_effect = [
            RuntimeError("unexpected"),
            json.dumps({"diffs": []}),
        ]
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec("x", "s", "c")

        assert caller.call.call_count == 2
        assert result.is_in_sync

    def test_returns_error_analysis_after_max_retries(self, tmp_path):
        from se3.engine.llm_caller import LLMCallError

        caller = MagicMock()
        caller.call.side_effect = LLMCallError("persistent failure")
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec("broken", "spec", "ctx")

        assert caller.call.call_count == 3
        assert len(result.diffs) == 1
        assert result.diffs[0].diff_type == DiffType.CONFLICT
        assert "3 attempts" in result.diffs[0].description
        assert "persistent failure" in result.diffs[0].description

    def test_all_in_sync_result(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = json.dumps({"diffs": []})
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec("clean", "spec", "ctx")

        assert result.is_in_sync
        assert result.spec_name == "clean"

    def test_prompt_includes_spec_content(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = json.dumps({"diffs": []})
        analyzer = SyncAnalyzer(tmp_path, caller)
        analyzer.analyze_spec("auth", "SHALL provide authentication", "ctx")

        prompt_arg = caller.call.call_args.kwargs.get("prompt") or caller.call.call_args[0][0]
        assert "SHALL provide authentication" in prompt_arg


class TestGenerateBaseSpec:
    def test_generates_and_writes_base_spec(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = "# My Project — Base Specification\n\n## Purpose\nTest project."
        analyzer = SyncAnalyzer(tmp_path, caller)
        content = analyzer.generate_base_spec("project context here")

        assert "Base Specification" in content
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        assert spec_path.exists()
        assert spec_path.read_text(encoding="utf-8") == content

    def test_creates_directories(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = "# Spec"
        analyzer = SyncAnalyzer(tmp_path, caller)
        analyzer.generate_base_spec("ctx")

        assert (tmp_path / "se3" / "specs" / "base").is_dir()

    def test_uses_off_json_mode(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = "# Spec"
        analyzer = SyncAnalyzer(tmp_path, caller)
        analyzer.generate_base_spec("ctx")

        call_kwargs = caller.call.call_args
        assert call_kwargs.kwargs.get("json_mode") == "off"

    def test_prompt_includes_project_context(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = "# Spec"
        analyzer = SyncAnalyzer(tmp_path, caller)
        analyzer.generate_base_spec("my special project context")

        prompt_arg = caller.call.call_args.kwargs.get("prompt") or caller.call.call_args[0][0]
        assert "my special project context" in prompt_arg

    def test_raises_on_llm_failure(self, tmp_path):
        from se3.engine.llm_caller import LLMCallError

        caller = MagicMock()
        caller.call.side_effect = LLMCallError("timeout")
        analyzer = SyncAnalyzer(tmp_path, caller)

        with pytest.raises(LLMCallError):
            analyzer.generate_base_spec("ctx")

    def test_overwrites_existing_base_spec(self, tmp_path):
        base_dir = tmp_path / "se3" / "specs" / "base"
        base_dir.mkdir(parents=True)
        (base_dir / "spec.md").write_text("old content")

        caller = MagicMock()
        caller.call.return_value = "# New Spec"
        analyzer = SyncAnalyzer(tmp_path, caller)
        content = analyzer.generate_base_spec("ctx")

        assert content == "# New Spec"
        assert (base_dir / "spec.md").read_text() == "# New Spec"

    def test_strips_response_whitespace(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = "\n\n  # Spec Content  \n\n"
        analyzer = SyncAnalyzer(tmp_path, caller)
        content = analyzer.generate_base_spec("ctx")

        assert content == "# Spec Content"


# ---------------------------------------------------------------------------
# SyncEngine tests
# ---------------------------------------------------------------------------

from se3.engine.sync_engine import SyncEngine, SYNC_TAGS


def _create_spec(tmp_path, name, content="# Spec\n## Purpose\nTest spec."):
    """Helper: create a spec directory with spec.md."""
    spec_dir = tmp_path / "se3" / "specs" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(content, encoding="utf-8")
    return spec_dir


class TestSyncEngineInit:
    def test_init_stores_attributes(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="strict")
        assert engine.project_root == tmp_path
        assert engine.mode == "strict"

    def test_default_mode(self, tmp_path):
        engine = SyncEngine(tmp_path)
        assert engine.mode == "default"


class TestSyncEngineLoadSpecs:
    def test_loads_base_spec_first(self, tmp_path):
        _create_spec(tmp_path, "base", "# Base spec content")
        _create_spec(tmp_path, "auth", "# Auth spec content")

        engine = SyncEngine(tmp_path)
        specs = engine._load_specs()

        assert "base" in specs
        assert "auth" in specs
        assert list(specs.keys())[0] == "base"
        assert specs["base"]["content"] == "# Base spec content"

    def test_loads_specs_without_base(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")
        _create_spec(tmp_path, "config", "# Config spec")

        engine = SyncEngine(tmp_path)
        specs = engine._load_specs()

        assert "base" not in specs
        assert len(specs) == 2

    def test_empty_specs_directory(self, tmp_path):
        (tmp_path / "se3" / "specs").mkdir(parents=True, exist_ok=True)

        engine = SyncEngine(tmp_path)
        specs = engine._load_specs()

        assert specs == {}

    def test_no_specs_directory(self, tmp_path):
        engine = SyncEngine(tmp_path)
        specs = engine._load_specs()

        assert specs == {}

    def test_spec_content_is_read(self, tmp_path):
        _create_spec(tmp_path, "my-spec", "Hello world spec content")

        engine = SyncEngine(tmp_path)
        specs = engine._load_specs()

        assert specs["my-spec"]["content"] == "Hello world spec content"


class TestSyncEngineLoadExistingIssues:
    def test_loads_open_issues(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Issue A", "desc", tags=["source:sync", "auto-discovered"])
        mgr.create("Issue B", "desc", tags=["other"])

        engine = SyncEngine(tmp_path)
        issues = engine._load_existing_issues()

        assert len(issues) == 2
        assert len(engine._sync_issues) == 1
        assert engine._sync_issues[0].title == "Issue A"

    def test_empty_issues(self, tmp_path):
        engine = SyncEngine(tmp_path)
        issues = engine._load_existing_issues()

        assert issues == []
        assert engine._sync_issues == []


class TestSyncEngineProcessGaps:
    def test_creates_issues_for_gaps(self, tmp_path):
        engine = SyncEngine(tmp_path)
        gaps = [
            SpecDiff(DiffType.GAP, "auth", "Missing login endpoint", "src/auth.py"),
            SpecDiff(DiffType.GAP, "auth", "Missing signup endpoint"),
        ]

        created = engine._process_gaps(gaps)

        assert created == 2
        mgr = IssueManager(tmp_path)
        issues = mgr.list_issues()
        assert len(issues) == 2
        assert all("source:sync" in i.tags for i in issues)
        assert all("auto-discovered" in i.tags for i in issues)
        assert issues[0].type == "task"

    def test_idempotent_no_duplicates(self, tmp_path):
        engine = SyncEngine(tmp_path)
        gaps = [SpecDiff(DiffType.GAP, "auth", "Missing login endpoint")]

        engine._process_gaps(gaps)
        created = engine._process_gaps(gaps)

        assert created == 0
        mgr = IssueManager(tmp_path)
        assert len(mgr.list_issues()) == 1

    def test_empty_gaps(self, tmp_path):
        engine = SyncEngine(tmp_path)
        created = engine._process_gaps([])
        assert created == 0

    def test_issue_description_includes_gap_details(self, tmp_path):
        engine = SyncEngine(tmp_path)
        gaps = [SpecDiff(DiffType.GAP, "config", "Missing validation", "src/config.py:42")]

        engine._process_gaps(gaps)

        mgr = IssueManager(tmp_path)
        issues = mgr.list_issues()
        assert len(issues) == 1
        assert "config" in issues[0].description
        assert "Missing validation" in issues[0].description
        assert "src/config.py:42" in issues[0].description

    def test_issue_title_format(self, tmp_path):
        engine = SyncEngine(tmp_path)
        gaps = [SpecDiff(DiffType.GAP, "auth", "Missing login")]

        engine._process_gaps(gaps)

        mgr = IssueManager(tmp_path)
        issues = mgr.list_issues()
        assert issues[0].title == "[sync] auth: Missing login"


class TestSyncEngineProcessExtensions:
    def test_updates_spec_file(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Old content")

        engine = SyncEngine(tmp_path)
        llm_caller = MagicMock()
        llm_caller.call.return_value = "# Updated auth spec with extensions"

        spec_info = {
            "name": "auth",
            "content": "# Old content",
            "path": tmp_path / "se3" / "specs" / "auth" / "spec.md",
        }
        extensions = [
            SpecDiff(DiffType.EXTENSION, "auth", "Extra helper function", "src/utils.py:10"),
        ]

        updated = engine._process_extensions(extensions, spec_info, llm_caller)

        assert updated == 1
        actual = (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text()
        assert actual == "# Updated auth spec with extensions"

    def test_no_extensions_returns_zero(self, tmp_path):
        engine = SyncEngine(tmp_path)
        updated = engine._process_extensions([], {}, MagicMock())
        assert updated == 0

    def test_llm_failure_returns_zero(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Original content")

        engine = SyncEngine(tmp_path)
        llm_caller = MagicMock()
        llm_caller.call.side_effect = RuntimeError("LLM timeout")

        spec_info = {
            "name": "auth",
            "content": "# Original content",
            "path": tmp_path / "se3" / "specs" / "auth" / "spec.md",
        }
        extensions = [SpecDiff(DiffType.EXTENSION, "auth", "Extra helper")]

        updated = engine._process_extensions(extensions, spec_info, llm_caller)

        assert updated == 0
        actual = (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text()
        assert actual == "# Original content"

    def test_empty_llm_response_returns_zero(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Original")

        engine = SyncEngine(tmp_path)
        llm_caller = MagicMock()
        llm_caller.call.return_value = "   "

        spec_info = {
            "name": "auth",
            "content": "# Original",
            "path": tmp_path / "se3" / "specs" / "auth" / "spec.md",
        }
        extensions = [SpecDiff(DiffType.EXTENSION, "auth", "Something")]

        updated = engine._process_extensions(extensions, spec_info, llm_caller)
        assert updated == 0

    def test_prompt_includes_extension_descriptions(self, tmp_path):
        _create_spec(tmp_path, "base", "# Base")

        engine = SyncEngine(tmp_path)
        llm_caller = MagicMock()
        llm_caller.call.return_value = "# Updated"

        spec_info = {
            "name": "base",
            "content": "# Base",
            "path": tmp_path / "se3" / "specs" / "base" / "spec.md",
        }
        extensions = [
            SpecDiff(DiffType.EXTENSION, "base", "New helper A", "src/a.py"),
            SpecDiff(DiffType.EXTENSION, "base", "New helper B"),
        ]

        engine._process_extensions(extensions, spec_info, llm_caller)

        prompt = llm_caller.call.call_args.kwargs.get("prompt") or llm_caller.call.call_args[0][0]
        assert "New helper A" in prompt
        assert "New helper B" in prompt
        assert "src/a.py" in prompt

    def test_uses_off_json_mode(self, tmp_path):
        _create_spec(tmp_path, "x", "# X")

        engine = SyncEngine(tmp_path)
        llm_caller = MagicMock()
        llm_caller.call.return_value = "# Updated"

        spec_info = {
            "name": "x",
            "content": "# X",
            "path": tmp_path / "se3" / "specs" / "x" / "spec.md",
        }

        engine._process_extensions(
            [SpecDiff(DiffType.EXTENSION, "x", "ext")], spec_info, llm_caller
        )

        assert llm_caller.call.call_args.kwargs.get("json_mode") == "off"


class TestSyncEngineIssueLifecycle:
    def test_auto_closes_resolved_gaps(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create(
            "[sync] auth: Missing login",
            "desc",
            tags=["auto-discovered", "source:sync"],
        )
        mgr.create(
            "[sync] config: Missing validation",
            "desc",
            tags=["auto-discovered", "source:sync"],
        )

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        current_gaps = {"[sync] auth: Missing login"}
        closed = engine._manage_issue_lifecycle(current_gaps)

        assert closed == 1
        remaining = mgr.list_issues()
        assert len(remaining) == 1
        assert remaining[0].title == "[sync] auth: Missing login"

    def test_no_sync_issues_closes_nothing(self, tmp_path):
        engine = SyncEngine(tmp_path)
        engine._sync_issues = []

        closed = engine._manage_issue_lifecycle(set())
        assert closed == 0

    def test_all_gaps_still_present(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create(
            "[sync] auth: Missing login",
            "desc",
            tags=["auto-discovered", "source:sync"],
        )

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        current_gaps = {"[sync] auth: Missing login"}
        closed = engine._manage_issue_lifecycle(current_gaps)

        assert closed == 0
        assert len(mgr.list_issues()) == 1

    def test_non_sync_issues_not_affected(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Manual issue", "desc", tags=["manual"])

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        closed = engine._manage_issue_lifecycle(set())
        assert closed == 0
        assert len(mgr.list_issues()) == 1


class TestSyncEngineGatherAllConflicts:
    def _make_analyses(self):
        return [
            SpecAnalysis(
                spec_name="auth",
                diffs=[
                    SpecDiff(DiffType.CONFLICT, "auth", "Token mismatch", "src/auth.py:10", confidence="high"),
                    SpecDiff(DiffType.GAP, "auth", "Missing feature"),
                ],
            ),
            SpecAnalysis(
                spec_name="config",
                diffs=[
                    SpecDiff(DiffType.CONFLICT, "config", "Format mismatch", confidence="low"),
                ],
            ),
        ]

    def test_gathers_all_conflicts(self):
        engine = SyncEngine(Path("/fake"), mode="default")
        conflicts = engine._gather_all_conflicts(self._make_analyses())
        assert len(conflicts) == 2
        assert conflicts[0].spec_name == "auth"
        assert conflicts[1].spec_name == "config"

    def test_preserves_confidence(self):
        engine = SyncEngine(Path("/fake"))
        conflicts = engine._gather_all_conflicts(self._make_analyses())
        assert conflicts[0].confidence == "high"
        assert conflicts[1].confidence == "low"

    def test_excludes_non_conflicts(self):
        engine = SyncEngine(Path("/fake"))
        conflicts = engine._gather_all_conflicts(self._make_analyses())
        assert all(c.description != "Missing feature" for c in conflicts)

    def test_no_conflicts_in_analysis(self):
        engine = SyncEngine(Path("/fake"))
        analyses = [SpecAnalysis(spec_name="clean", diffs=[])]
        conflicts = engine._gather_all_conflicts(analyses)
        assert conflicts == []


class TestSyncEngineGenerateCallFile:
    def test_generates_call_file(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="default")
        conflicts = [
            Conflict(spec_name="auth", description="Token mismatch", code_location="src/a.py"),
            Conflict(spec_name="config", description="Format mismatch"),
        ]

        call_path = engine._generate_call_file(conflicts)

        assert call_path.exists()
        assert call_path.parent == tmp_path / "se3" / "calls"

        data = json.loads(call_path.read_text())
        assert data["type"] == "sync_conflicts"
        assert data["mode"] == "default"
        assert len(data["conflicts"]) == 2
        assert data["conflicts"][0]["id"] == 1
        assert data["conflicts"][0]["spec_name"] == "auth"
        assert data["conflicts"][0]["decision"] == "pending"
        assert data["conflicts"][1]["id"] == 2

    def test_creates_calls_directory(self, tmp_path):
        engine = SyncEngine(tmp_path)
        conflicts = [Conflict(spec_name="x", description="d")]

        call_path = engine._generate_call_file(conflicts)
        assert (tmp_path / "se3" / "calls").is_dir()

    def test_call_file_contains_options(self, tmp_path):
        engine = SyncEngine(tmp_path)
        conflicts = [Conflict(spec_name="x", description="d")]

        call_path = engine._generate_call_file(conflicts)
        data = json.loads(call_path.read_text())

        assert data["conflicts"][0]["options"] == ["update_spec", "create_issue"]


class TestSyncEngineRun:
    @patch("se3.engine.sync_engine.SyncEngine._load_specs")
    @patch("se3.engine.sync_engine.SyncEngine._load_existing_issues")
    @patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec")
    @patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None)
    @patch("se3.engine.project_context.ProjectContextCollector.collect")
    def test_run_orchestrates_workflow(
        self, mock_collect, mock_llm_init, mock_analyze, mock_load_issues, mock_load_specs, tmp_path
    ):
        mock_collect.return_value = {"git": {}, "flow_engine": None, "backlog": [], "specs": []}

        mock_load_specs.return_value = {
            "base": {
                "name": "base",
                "path": tmp_path / "se3" / "specs" / "base" / "spec.md",
                "content": "# Base spec",
            },
        }

        mock_analyze.return_value = SpecAnalysis(spec_name="base", diffs=[])
        mock_load_issues.return_value = []

        engine = SyncEngine(tmp_path)
        result = engine.run()

        assert isinstance(result, SyncResult)
        assert len(result.analyses) == 1
        mock_analyze.assert_called_once()
        mock_load_issues.assert_called_once()

    @patch("se3.engine.sync_engine.SyncEngine._load_specs")
    @patch("se3.engine.sync_engine.SyncEngine._load_existing_issues")
    @patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec")
    @patch("se3.engine.sync_analyzer.SyncAnalyzer.generate_base_spec")
    @patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None)
    @patch("se3.engine.project_context.ProjectContextCollector.collect")
    def test_run_generates_base_spec_if_missing(
        self, mock_collect, mock_llm_init, mock_gen_base, mock_analyze,
        mock_load_issues, mock_load_specs, tmp_path
    ):
        mock_collect.return_value = {"git": {}, "flow_engine": None, "backlog": [], "specs": []}
        mock_load_specs.side_effect = [
            {},
            {"base": {"name": "base", "path": Path("f"), "content": "# Base"}},
        ]
        mock_analyze.return_value = SpecAnalysis(spec_name="base", diffs=[])
        mock_load_issues.return_value = []

        engine = SyncEngine(tmp_path)
        result = engine.run()

        mock_gen_base.assert_called_once()
        assert len(result.analyses) == 1

    def test_run_with_gaps_creates_issues(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")

        with patch("se3.engine.sync_engine.SyncEngine._load_existing_issues", return_value=[]), \
             patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None), \
             patch("se3.engine.llm_caller.LLMCaller.call") as mock_llm_call, \
             patch("se3.engine.project_context.ProjectContextCollector.collect",
                   return_value={"git": {}, "flow_engine": None, "backlog": [], "specs": []}), \
             patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec") as mock_analyze:

            mock_analyze.return_value = SpecAnalysis(
                spec_name="auth",
                diffs=[SpecDiff(DiffType.GAP, "auth", "Missing login endpoint")],
            )
            mock_llm_call.return_value = json.dumps({
                "decision": "create_issue", "confidence": "high", "reasoning": "needed"
            })

            engine = SyncEngine(tmp_path)
            engine._sync_issues = []
            result = engine.run()

            assert result.issues_created == 1
            mgr = IssueManager(tmp_path)
            issues = mgr.list_issues()
            assert len(issues) == 1

    def test_run_fast_mode_no_call_file(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")

        with patch("se3.engine.sync_engine.SyncEngine._load_existing_issues", return_value=[]), \
             patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None), \
             patch("se3.engine.project_context.ProjectContextCollector.collect",
                   return_value={"git": {}, "flow_engine": None, "backlog": [], "specs": []}), \
             patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec") as mock_analyze, \
             patch("se3.engine.sync_engine.SyncEngine._handle_conflicts_fast") as mock_fast:

            mock_analyze.return_value = SpecAnalysis(
                spec_name="auth",
                diffs=[SpecDiff(DiffType.CONFLICT, "auth", "Token mismatch")],
            )
            mock_fast.return_value = {"specs_updated": 1, "issues_created": 0}

            engine = SyncEngine(tmp_path, mode="fast")
            engine._sync_issues = []
            result = engine.run()

            assert result.call_file is None
            mock_fast.assert_called_once()


# ---------------------------------------------------------------------------
# SpecDiff confidence field tests
# ---------------------------------------------------------------------------

class TestSpecDiffConfidence:
    def test_confidence_default_empty(self):
        diff = SpecDiff(DiffType.CONFLICT, "auth", "mismatch")
        assert diff.confidence == ""

    def test_confidence_roundtrip(self):
        diff = SpecDiff(DiffType.CONFLICT, "auth", "mismatch", confidence="high")
        data = diff.to_dict()
        assert data["confidence"] == "high"
        restored = SpecDiff.from_dict(data)
        assert restored.confidence == "high"

    def test_confidence_omitted_in_dict_when_empty(self):
        diff = SpecDiff(DiffType.GAP, "auth", "gap")
        data = diff.to_dict()
        assert "confidence" not in data

    def test_from_dict_missing_confidence(self):
        data = {"diff_type": "conflict", "spec_name": "x", "description": "d"}
        diff = SpecDiff.from_dict(data)
        assert diff.confidence == ""


class TestConflictConfidence:
    def test_confidence_roundtrip(self):
        c = Conflict(spec_name="auth", description="d", confidence="low")
        data = c.to_dict()
        assert data["confidence"] == "low"
        restored = Conflict.from_dict(data)
        assert restored.confidence == "low"

    def test_confidence_omitted_when_empty(self):
        c = Conflict(spec_name="auth", description="d")
        data = c.to_dict()
        assert "confidence" not in data


# ---------------------------------------------------------------------------
# SyncAnalyzer confidence parsing tests
# ---------------------------------------------------------------------------

class TestSyncAnalyzerConfidence:
    def test_conflict_gets_confidence(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        response = json.dumps({
            "diffs": [
                {"type": "conflict", "description": "mismatch", "confidence": "high"},
            ]
        })
        result = analyzer._parse_analysis_response("auth", response)
        assert result.diffs[0].confidence == "high"

    def test_conflict_defaults_to_low(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        response = json.dumps({
            "diffs": [
                {"type": "conflict", "description": "mismatch"},
            ]
        })
        result = analyzer._parse_analysis_response("auth", response)
        assert result.diffs[0].confidence == "low"

    def test_gap_gets_no_confidence(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        response = json.dumps({
            "diffs": [
                {"type": "gap", "description": "missing", "confidence": "high"},
            ]
        })
        result = analyzer._parse_analysis_response("auth", response)
        assert result.diffs[0].confidence == ""

    def test_extension_gets_no_confidence(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        response = json.dumps({
            "diffs": [
                {"type": "extension", "description": "extra"},
            ]
        })
        result = analyzer._parse_analysis_response("auth", response)
        assert result.diffs[0].confidence == ""

    def test_analysis_prompt_mentions_confidence(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        prompt = analyzer._build_analysis_prompt("x", "s", "c")
        assert "confidence" in prompt.lower()


# ---------------------------------------------------------------------------
# Conflict resolution helper tests
# ---------------------------------------------------------------------------

class TestResolveConflictViaLLM:
    def test_returns_update_spec(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = json.dumps({"decision": "update_spec", "reasoning": "code is correct"})

        conflict = Conflict(spec_name="auth", description="Token mismatch")
        decision = engine._resolve_conflict_via_llm(conflict, llm)

        assert decision == "update_spec"

    def test_returns_create_issue(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = json.dumps({"decision": "create_issue", "reasoning": "spec is correct"})

        conflict = Conflict(spec_name="auth", description="Token mismatch")
        decision = engine._resolve_conflict_via_llm(conflict, llm)

        assert decision == "create_issue"

    def test_defaults_to_create_issue_on_unknown(self, tmp_path):
        engine = SyncEngine(tmp_path)
        llm = MagicMock()
        llm.call.return_value = json.dumps({"decision": "unknown"})

        conflict = Conflict(spec_name="x", description="d")
        decision = engine._resolve_conflict_via_llm(conflict, llm)

        assert decision == "create_issue"

    def test_defaults_to_create_issue_on_error(self, tmp_path):
        engine = SyncEngine(tmp_path)
        llm = MagicMock()
        llm.call.side_effect = RuntimeError("LLM fail")

        conflict = Conflict(spec_name="x", description="d")
        decision = engine._resolve_conflict_via_llm(conflict, llm)

        assert decision == "create_issue"

    def test_prompt_includes_spec_content(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec with JWT")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = json.dumps({"decision": "create_issue"})

        conflict = Conflict(spec_name="auth", description="Token mismatch")
        engine._resolve_conflict_via_llm(conflict, llm)

        prompt = llm.call.call_args.kwargs.get("prompt") or llm.call.call_args[0][0]
        assert "Auth spec with JWT" in prompt


class TestApplyConflictSpecUpdate:
    def test_updates_spec_file(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Old auth spec")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "# Updated auth spec"

        conflict = Conflict(spec_name="auth", description="Token mismatch")
        success = engine._apply_conflict_spec_update(conflict, llm)

        assert success
        actual = (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text()
        assert actual == "# Updated auth spec"

    def test_returns_false_when_spec_not_found(self, tmp_path):
        engine = SyncEngine(tmp_path)

        conflict = Conflict(spec_name="nonexistent", description="d")
        success = engine._apply_conflict_spec_update(conflict, MagicMock())

        assert not success

    def test_returns_false_on_empty_llm_response(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Original")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "   "

        conflict = Conflict(spec_name="auth", description="d")
        success = engine._apply_conflict_spec_update(conflict, llm)

        assert not success
        assert (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text() == "# Original"

    def test_returns_false_on_llm_error(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Original")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.side_effect = RuntimeError("fail")

        conflict = Conflict(spec_name="auth", description="d")
        success = engine._apply_conflict_spec_update(conflict, llm)

        assert not success


class TestApplyConflictCreateIssue:
    def test_creates_issue(self, tmp_path):
        engine = SyncEngine(tmp_path)
        conflict = Conflict(spec_name="auth", description="Token mismatch", code_location="src/auth.py:10")

        success = engine._apply_conflict_create_issue(conflict)

        assert success
        mgr = IssueManager(tmp_path)
        issues = mgr.list_issues()
        assert len(issues) == 1
        assert "[sync-conflict] auth: Token mismatch" in issues[0].title
        assert "conflict" in issues[0].tags
        assert "src/auth.py:10" in issues[0].description

    def test_idempotent_no_duplicates(self, tmp_path):
        engine = SyncEngine(tmp_path)
        conflict = Conflict(spec_name="auth", description="Token mismatch")

        engine._apply_conflict_create_issue(conflict)
        success = engine._apply_conflict_create_issue(conflict)

        assert not success
        mgr = IssueManager(tmp_path)
        assert len(mgr.list_issues()) == 1


# ---------------------------------------------------------------------------
# Fast mode handler tests
# ---------------------------------------------------------------------------

class TestHandleConflictsFast:
    def test_auto_resolves_all_conflicts(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")
        engine = SyncEngine(tmp_path, mode="fast")
        engine._load_specs()

        llm = MagicMock()
        llm.call.side_effect = [
            json.dumps({"decision": "update_spec"}),
            "# Updated auth spec",
            json.dumps({"decision": "create_issue"}),
        ]

        conflicts = [
            Conflict(spec_name="auth", description="Conflict A"),
            Conflict(spec_name="auth", description="Conflict B"),
        ]

        result = engine._handle_conflicts_fast(conflicts, llm)

        assert result["specs_updated"] == 1
        assert result["issues_created"] == 1

    def test_no_call_file_generated(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="fast")
        result = engine._handle_conflicts_fast([], MagicMock())

        assert result == {"specs_updated": 0, "issues_created": 0}
        assert not (tmp_path / "se3" / "calls").exists()

    def test_empty_conflicts(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="fast")
        result = engine._handle_conflicts_fast([], MagicMock())
        assert result["specs_updated"] == 0
        assert result["issues_created"] == 0


# ---------------------------------------------------------------------------
# Strict mode handler tests
# ---------------------------------------------------------------------------

class TestHandleConflictsStrict:
    def test_generates_call_file_for_all(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="strict")
        conflicts = [
            Conflict(spec_name="auth", description="Conflict A"),
            Conflict(spec_name="config", description="Conflict B"),
        ]

        result = engine._handle_conflicts_strict(conflicts)

        assert len(result["conflicts"]) == 2
        assert result["call_file"] is not None
        call_path = Path(result["call_file"])
        assert call_path.exists()

        data = json.loads(call_path.read_text())
        assert data["type"] == "sync_conflicts"
        assert len(data["conflicts"]) == 2
        assert data["conflicts"][0]["options"] == ["update_spec", "create_issue"]

    def test_no_conflicts_no_call_file(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="strict")
        result = engine._handle_conflicts_strict([])

        assert result["conflicts"] == []
        assert result["call_file"] is None


# ---------------------------------------------------------------------------
# Default mode handler tests
# ---------------------------------------------------------------------------

class TestHandleConflictsDefault:
    def test_auto_resolves_high_confidence(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")
        engine = SyncEngine(tmp_path, mode="default")
        engine._load_specs()

        llm = MagicMock()
        llm.call.side_effect = [
            json.dumps({"decision": "update_spec"}),
            "# Updated spec",
        ]

        conflicts = [
            Conflict(spec_name="auth", description="Mismatch A", confidence="high"),
        ]

        result = engine._handle_conflicts_default(conflicts, llm)

        assert result["specs_updated"] == 1
        assert result["issues_created"] == 0
        assert result["unresolved"] == []
        assert result["call_file"] is None

    def test_collects_low_confidence(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="default")

        conflicts = [
            Conflict(spec_name="auth", description="Mismatch A", confidence="low"),
            Conflict(spec_name="config", description="Mismatch B", confidence=""),
        ]

        result = engine._handle_conflicts_default(conflicts, MagicMock())

        assert result["specs_updated"] == 0
        assert result["issues_created"] == 0
        assert len(result["unresolved"]) == 2
        assert result["call_file"] is not None

    def test_mixed_confidence(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")
        engine = SyncEngine(tmp_path, mode="default")
        engine._load_specs()

        llm = MagicMock()
        llm.call.side_effect = [
            json.dumps({"decision": "create_issue"}),
        ]

        conflicts = [
            Conflict(spec_name="auth", description="High conf", confidence="high"),
            Conflict(spec_name="config", description="Low conf", confidence="low"),
        ]

        result = engine._handle_conflicts_default(conflicts, llm)

        assert result["issues_created"] == 1
        assert len(result["unresolved"]) == 1
        assert result["unresolved"][0].description == "Low conf"
        assert result["call_file"] is not None

    def test_no_low_confidence_no_call_file(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")
        engine = SyncEngine(tmp_path, mode="default")
        engine._load_specs()

        llm = MagicMock()
        llm.call.side_effect = [
            json.dumps({"decision": "update_spec"}),
            "# Updated spec",
        ]

        conflicts = [
            Conflict(spec_name="auth", description="High only", confidence="high"),
        ]

        result = engine._handle_conflicts_default(conflicts, llm)

        assert result["call_file"] is None
        assert result["unresolved"] == []


# ---------------------------------------------------------------------------
# Process call response tests
# ---------------------------------------------------------------------------

class TestProcessCallResponse:
    def _create_call_and_response(self, tmp_path, call_conflicts, response_decisions):
        calls_dir = tmp_path / "se3" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)

        call_file = calls_dir / "sync_conflicts_12345.json"
        call_data = {
            "type": "sync_conflicts",
            "mode": "strict",
            "timestamp": 12345,
            "conflicts": call_conflicts,
        }
        call_file.write_text(json.dumps(call_data), encoding="utf-8")

        response_file = calls_dir / "sync_conflicts_12345.json.response"
        response_data = {"conflicts": response_decisions}
        response_file.write_text(json.dumps(response_data), encoding="utf-8")

        return call_file

    def test_processes_update_spec_decision(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Old auth spec")

        call_file = self._create_call_and_response(
            tmp_path,
            [{"id": 1, "spec_name": "auth", "description": "Token mismatch", "code_location": "src/a.py"}],
            [{"id": 1, "decision": "update_spec"}],
        )

        llm = MagicMock()
        llm.call.return_value = "# Fixed auth spec"

        engine = SyncEngine(tmp_path)
        engine._load_specs()
        result = engine.process_call_response(call_file, llm)

        assert result["specs_updated"] == 1
        assert result["issues_created"] == 0
        actual = (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text()
        assert actual == "# Fixed auth spec"

    def test_processes_create_issue_decision(self, tmp_path):
        call_file = self._create_call_and_response(
            tmp_path,
            [{"id": 1, "spec_name": "auth", "description": "Token mismatch", "code_location": ""}],
            [{"id": 1, "decision": "create_issue"}],
        )

        engine = SyncEngine(tmp_path)
        result = engine.process_call_response(call_file, MagicMock())

        assert result["specs_updated"] == 0
        assert result["issues_created"] == 1
        mgr = IssueManager(tmp_path)
        issues = mgr.list_issues()
        assert len(issues) == 1
        assert "[sync-conflict]" in issues[0].title

    def test_mixed_decisions(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")

        call_file = self._create_call_and_response(
            tmp_path,
            [
                {"id": 1, "spec_name": "auth", "description": "Conflict A", "code_location": ""},
                {"id": 2, "spec_name": "config", "description": "Conflict B", "code_location": ""},
            ],
            [
                {"id": 1, "decision": "update_spec"},
                {"id": 2, "decision": "create_issue"},
            ],
        )

        llm = MagicMock()
        llm.call.return_value = "# Updated auth"

        engine = SyncEngine(tmp_path)
        engine._load_specs()
        result = engine.process_call_response(call_file, llm)

        assert result["specs_updated"] == 1
        assert result["issues_created"] == 1

    def test_skips_invalid_decision(self, tmp_path):
        call_file = self._create_call_and_response(
            tmp_path,
            [{"id": 1, "spec_name": "x", "description": "d", "code_location": ""}],
            [{"id": 1, "decision": "invalid"}],
        )

        engine = SyncEngine(tmp_path)
        result = engine.process_call_response(call_file, MagicMock())

        assert result["specs_updated"] == 0
        assert result["issues_created"] == 0

    def test_missing_response_file(self, tmp_path):
        calls_dir = tmp_path / "se3" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)
        call_file = calls_dir / "sync_conflicts_99.json"
        call_file.write_text("{}", encoding="utf-8")

        engine = SyncEngine(tmp_path)
        result = engine.process_call_response(call_file)

        assert result == {"specs_updated": 0, "issues_created": 0}

    def test_invalid_response_json(self, tmp_path):
        calls_dir = tmp_path / "se3" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)
        call_file = calls_dir / "sync_conflicts_99.json"
        call_file.write_text("{}", encoding="utf-8")
        response_file = calls_dir / "sync_conflicts_99.json.response"
        response_file.write_text("not json", encoding="utf-8")

        engine = SyncEngine(tmp_path)
        result = engine.process_call_response(call_file)

        assert result == {"specs_updated": 0, "issues_created": 0}

    def test_loads_specs_if_not_loaded(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")

        call_file = self._create_call_and_response(
            tmp_path,
            [{"id": 1, "spec_name": "auth", "description": "d", "code_location": ""}],
            [{"id": 1, "decision": "update_spec"}],
        )

        llm = MagicMock()
        llm.call.return_value = "# Updated"

        engine = SyncEngine(tmp_path)
        result = engine.process_call_response(call_file, llm)

        assert result["specs_updated"] == 1


# ---------------------------------------------------------------------------
# CLI process_call_response tests
# ---------------------------------------------------------------------------

class TestSyncCLIProcessCallResponse:
    def test_imports(self):
        from se3.commands.sync import process_call_response
        assert callable(process_call_response)

    def test_missing_call_file(self, tmp_path):
        from se3.commands.sync import process_call_response

        process_call_response(
            call_file=tmp_path / "nonexistent.json",
            project_root=tmp_path,
        )

    def test_missing_response_file(self, tmp_path):
        from se3.commands.sync import process_call_response

        call_file = tmp_path / "call.json"
        call_file.write_text("{}", encoding="utf-8")

        process_call_response(
            call_file=call_file,
            project_root=tmp_path,
        )


# ---------------------------------------------------------------------------
# Progress callback tests
# ---------------------------------------------------------------------------

class TestSyncEngineProgressCallback:
    @patch("se3.engine.sync_discovery.SpecDiscovery.discover_missing_specs", return_value=[])
    @patch("se3.engine.sync_engine.SyncEngine._load_specs")
    @patch("se3.engine.sync_engine.SyncEngine._load_existing_issues")
    @patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec")
    @patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None)
    @patch("se3.engine.project_context.ProjectContextCollector.collect")
    def test_progress_callback_called_for_each_spec(
        self, mock_collect, mock_llm_init, mock_analyze, mock_load_issues, mock_load_specs, mock_discover, tmp_path
    ):
        mock_collect.return_value = {"git": {}, "flow_engine": None, "backlog": [], "specs": []}
        mock_load_specs.return_value = {
            "base": {"name": "base", "path": Path("f"), "content": "# Base"},
            "auth": {"name": "auth", "path": Path("g"), "content": "# Auth"},
        }
        mock_analyze.side_effect = [
            SpecAnalysis(spec_name="base", diffs=[]),
            SpecAnalysis(spec_name="auth", diffs=[
                SpecDiff(DiffType.GAP, "auth", "Missing login"),
            ]),
        ]
        mock_load_issues.return_value = []

        calls = []

        def callback(phase, spec_name, index, total, analysis):
            calls.append((phase, spec_name, index, total))

        engine = SyncEngine(tmp_path)
        engine._sync_issues = []
        engine.run(progress_callback=callback)

        assert len(calls) == 5
        assert calls[0] == ("discovering", None, 0, 0)
        assert calls[1] == ("analyzing", "base", 0, 2)
        assert calls[2] == ("analyzed", "base", 0, 2)
        assert calls[3] == ("analyzing", "auth", 1, 2)
        assert calls[4] == ("analyzed", "auth", 1, 2)

    @patch("se3.engine.sync_discovery.SpecDiscovery.discover_missing_specs", return_value=[])
    @patch("se3.engine.sync_engine.SyncEngine._load_specs")
    @patch("se3.engine.sync_engine.SyncEngine._load_existing_issues")
    @patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec")
    @patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None)
    @patch("se3.engine.project_context.ProjectContextCollector.collect")
    def test_progress_callback_receives_analysis_on_analyzed(
        self, mock_collect, mock_llm_init, mock_analyze, mock_load_issues, mock_load_specs, mock_discover, tmp_path
    ):
        mock_collect.return_value = {"git": {}, "flow_engine": None, "backlog": [], "specs": []}
        mock_load_specs.return_value = {
            "base": {"name": "base", "path": Path("f"), "content": "# Base"},
        }
        analysis = SpecAnalysis(spec_name="base", diffs=[])
        mock_analyze.return_value = analysis
        mock_load_issues.return_value = []

        received = []

        def callback(phase, spec_name, index, total, analysis_result):
            received.append((phase, analysis_result))

        engine = SyncEngine(tmp_path)
        engine._sync_issues = []
        engine.run(progress_callback=callback)

        assert received[0] == ("discovering", None)
        assert received[1] == ("analyzing", None)
        assert received[2][0] == "analyzed"
        assert received[2][1] is not None
        assert received[2][1].spec_name == "base"

    @patch("se3.engine.sync_engine.SyncEngine._load_specs")
    @patch("se3.engine.sync_engine.SyncEngine._load_existing_issues")
    @patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None)
    @patch("se3.engine.project_context.ProjectContextCollector.collect")
    def test_run_works_without_callback(
        self, mock_collect, mock_llm_init, mock_load_issues, mock_load_specs, tmp_path
    ):
        mock_collect.return_value = {"git": {}, "flow_engine": None, "backlog": [], "specs": []}
        mock_load_specs.return_value = {}
        mock_load_issues.return_value = []

        engine = SyncEngine(tmp_path)
        engine._sync_issues = []

        with patch("se3.engine.sync_analyzer.SyncAnalyzer.generate_base_spec"):
            mock_load_specs.side_effect = [{}, {}]
            result = engine.run()
            assert isinstance(result, SyncResult)


# ---------------------------------------------------------------------------
# Render sync results tests
# ---------------------------------------------------------------------------

class TestRenderSyncResults:
    def test_render_all_in_sync(self):
        from se3.commands.sync import _render_sync_results

        result = SyncResult(
            analyses=[SpecAnalysis(spec_name="base", diffs=[])],
        )
        _render_sync_results(result)

    def test_render_with_diffs(self):
        from se3.commands.sync import _render_sync_results

        result = SyncResult(
            analyses=[
                SpecAnalysis(
                    spec_name="auth",
                    diffs=[
                        SpecDiff(DiffType.GAP, "auth", "Missing login"),
                        SpecDiff(DiffType.EXTENSION, "auth", "Extra feature"),
                        SpecDiff(DiffType.CONFLICT, "auth", "Token mismatch"),
                    ],
                ),
            ],
            issues_created=1,
            issues_closed=2,
            specs_updated=1,
        )
        _render_sync_results(result)

    def test_render_with_conflicts_and_call_file(self):
        from se3.commands.sync import _render_sync_results

        result = SyncResult(
            analyses=[
                SpecAnalysis(
                    spec_name="auth",
                    diffs=[SpecDiff(DiffType.CONFLICT, "auth", "Token mismatch")],
                ),
            ],
            conflicts=[Conflict(spec_name="auth", description="Token mismatch")],
            call_file="/tmp/se3/calls/sync_conflicts_123.json",
        )
        _render_sync_results(result)

    def test_render_empty_result(self):
        from se3.commands.sync import _render_sync_results
        _render_sync_results(SyncResult())
