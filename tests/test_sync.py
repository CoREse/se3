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
