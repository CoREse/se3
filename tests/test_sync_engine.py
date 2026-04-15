"""Unit tests for SyncEngine — issue idempotency, auto-close, mode handling, MCP call files."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.issue_manager import Issue, IssueManager, IssueStatus
from se3.engine.sync_engine import (
    Conflict,
    ConflictDecision,
    DiffType,
    SYNC_TAGS,
    SpecAnalysis,
    SpecDiff,
    SyncEngine,
    SyncResult,
    strip_markdown_fences,
)


def _create_spec(tmp_path, name, content="# Spec\n## Purpose\nTest spec."):
    spec_dir = tmp_path / "se3" / "specs" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(content, encoding="utf-8")
    return spec_dir


# ---------------------------------------------------------------------------
# Issue idempotency (gap → issue creation)
# ---------------------------------------------------------------------------

class TestIssueIdempotency:
    def test_creates_issue_for_new_gap(self, tmp_path):
        engine = SyncEngine(tmp_path)
        gaps = [SpecDiff(DiffType.GAP, "auth", "Missing login endpoint")]
        created = engine._process_gaps(gaps)

        assert created == 1
        mgr = IssueManager(tmp_path)
        issues = mgr.list_issues()
        assert len(issues) == 1
        assert "[sync] auth: Missing login endpoint" == issues[0].title

    def test_skips_existing_issue_with_same_title(self, tmp_path):
        engine = SyncEngine(tmp_path)
        gaps = [SpecDiff(DiffType.GAP, "auth", "Missing login endpoint")]

        engine._process_gaps(gaps)
        created = engine._process_gaps(gaps)

        assert created == 0
        mgr = IssueManager(tmp_path)
        assert len(mgr.list_issues()) == 1

    def test_creates_separate_issues_for_different_gaps(self, tmp_path):
        engine = SyncEngine(tmp_path)
        gaps = [
            SpecDiff(DiffType.GAP, "auth", "Missing login"),
            SpecDiff(DiffType.GAP, "auth", "Missing signup"),
        ]
        created = engine._process_gaps(gaps)

        assert created == 2
        mgr = IssueManager(tmp_path)
        assert len(mgr.list_issues()) == 2

    def test_empty_gaps_creates_nothing(self, tmp_path):
        engine = SyncEngine(tmp_path)
        created = engine._process_gaps([])
        assert created == 0

    def test_issue_has_sync_tags(self, tmp_path):
        engine = SyncEngine(tmp_path)
        gaps = [SpecDiff(DiffType.GAP, "auth", "Missing feature")]
        engine._process_gaps(gaps)

        mgr = IssueManager(tmp_path)
        issues = mgr.list_issues()
        assert "source:sync" in issues[0].tags
        assert "auto-discovered" in issues[0].tags

    def test_issue_includes_code_location(self, tmp_path):
        engine = SyncEngine(tmp_path)
        gaps = [SpecDiff(DiffType.GAP, "cfg", "Missing validation", "src/config.py:42")]
        engine._process_gaps(gaps)

        mgr = IssueManager(tmp_path)
        issues = mgr.list_issues()
        assert "src/config.py:42" in issues[0].description

    def test_issue_type_is_task(self, tmp_path):
        engine = SyncEngine(tmp_path)
        gaps = [SpecDiff(DiffType.GAP, "auth", "Missing")]
        engine._process_gaps(gaps)

        mgr = IssueManager(tmp_path)
        assert mgr.list_issues()[0].type == "task"

    def test_conflict_issue_also_idempotent(self, tmp_path):
        engine = SyncEngine(tmp_path)
        conflict = Conflict(spec_name="auth", description="Token mismatch")

        engine._apply_conflict_create_issue(conflict)
        success = engine._apply_conflict_create_issue(conflict)

        assert not success
        mgr = IssueManager(tmp_path)
        assert len(mgr.list_issues()) == 1


# ---------------------------------------------------------------------------
# Issue auto-close (gap disappears → close issue)
# ---------------------------------------------------------------------------

class TestIssueAutoClose:
    def test_closes_issue_when_gap_disappears(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: Missing login", "d", tags=list(SYNC_TAGS))
        mgr.create("[sync] config: Missing validation", "d", tags=list(SYNC_TAGS))

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        current_gaps = {"[sync] auth: Missing login"}
        closed = engine._manage_issue_lifecycle(current_gaps)

        assert closed == 1
        remaining = mgr.list_issues()
        assert len(remaining) == 1
        assert remaining[0].title == "[sync] auth: Missing login"

    def test_closes_all_when_all_gaps_resolved(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: A", "d", tags=list(SYNC_TAGS))
        mgr.create("[sync] auth: B", "d", tags=list(SYNC_TAGS))

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        closed = engine._manage_issue_lifecycle(set())

        assert closed == 2
        assert mgr.list_issues() == []

    def test_does_not_close_when_gaps_still_present(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: Missing login", "d", tags=list(SYNC_TAGS))

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        current_gaps = {"[sync] auth: Missing login"}
        closed = engine._manage_issue_lifecycle(current_gaps)

        assert closed == 0
        assert len(mgr.list_issues()) == 1

    def test_does_not_close_non_sync_issues(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("Manual bug report", "d", tags=["manual"])

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        closed = engine._manage_issue_lifecycle(set())

        assert closed == 0
        assert len(mgr.list_issues()) == 1

    def test_no_sync_issues_returns_zero(self, tmp_path):
        engine = SyncEngine(tmp_path)
        engine._sync_issues = []

        closed = engine._manage_issue_lifecycle(set())
        assert closed == 0

    def test_close_failure_does_not_increment_count(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: Missing login", "d", tags=list(SYNC_TAGS))
        mgr.create("[sync] auth: Missing signup", "d", tags=list(SYNC_TAGS))

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        with patch("se3.engine.issue_manager.shutil.move", side_effect=OSError("disk full")):
            closed = engine._manage_issue_lifecycle(set())

        assert closed == 0


# ---------------------------------------------------------------------------
# Three modes — fast / strict / default
# ---------------------------------------------------------------------------

class TestHandleConflictsFast:
    def test_auto_resolves_update_spec(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")
        engine = SyncEngine(tmp_path, mode="fast")
        engine._load_specs()

        llm = MagicMock()
        llm.call.side_effect = [
            json.dumps({"decision": "update_spec", "reasoning": "code is right"}),
            "# Updated auth spec",
        ]

        conflicts = [Conflict(spec_name="auth", description="Token format")]
        result = engine._handle_conflicts_fast(conflicts, llm)

        assert result["specs_updated"] == 1
        assert result["issues_created"] == 0

    def test_auto_resolves_create_issue(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="fast")

        llm = MagicMock()
        llm.call.return_value = json.dumps({"decision": "create_issue", "reasoning": "spec is right"})

        conflicts = [Conflict(spec_name="auth", description="Token format")]
        result = engine._handle_conflicts_fast(conflicts, llm)

        assert result["specs_updated"] == 0
        assert result["issues_created"] == 1

    def test_handles_multiple_conflicts(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth")
        engine = SyncEngine(tmp_path, mode="fast")
        engine._load_specs()

        llm = MagicMock()
        llm.call.side_effect = [
            json.dumps({"decision": "update_spec"}),
            "# Updated",
            json.dumps({"decision": "create_issue"}),
        ]

        conflicts = [
            Conflict(spec_name="auth", description="A"),
            Conflict(spec_name="auth", description="B"),
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


class TestHandleConflictsStrict:
    def test_all_conflicts_go_to_call_file(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="strict")
        conflicts = [
            Conflict(spec_name="auth", description="A"),
            Conflict(spec_name="config", description="B"),
        ]

        result = engine._handle_conflicts_strict(conflicts)

        assert len(result["conflicts"]) == 2
        assert result["call_file"] is not None
        call_path = Path(result["call_file"])
        assert call_path.exists()

        data = json.loads(call_path.read_text())
        assert data["type"] == "sync_conflicts"
        assert len(data["conflicts"]) == 2

    def test_call_file_has_pending_decisions(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="strict")
        conflicts = [Conflict(spec_name="auth", description="A")]

        result = engine._handle_conflicts_strict(conflicts)
        data = json.loads(Path(result["call_file"]).read_text())

        assert data["conflicts"][0]["decision"] == "pending"
        assert data["conflicts"][0]["options"] == ["update_spec", "create_issue"]

    def test_no_conflicts_no_call_file(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="strict")
        result = engine._handle_conflicts_strict([])

        assert result["conflicts"] == []
        assert result["call_file"] is None

    def test_high_confidence_not_auto_resolved(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="strict")
        conflicts = [Conflict(spec_name="auth", description="A", confidence="high")]

        result = engine._handle_conflicts_strict(conflicts)

        assert len(result["conflicts"]) == 1
        assert result["call_file"] is not None


class TestHandleConflictsDefault:
    def test_auto_resolves_high_confidence(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth")
        engine = SyncEngine(tmp_path, mode="default")
        engine._load_specs()

        llm = MagicMock()
        llm.call.side_effect = [
            json.dumps({"decision": "update_spec"}),
            "# Updated",
        ]

        conflicts = [Conflict(spec_name="auth", description="A", confidence="high")]
        result = engine._handle_conflicts_default(conflicts, llm)

        assert result["specs_updated"] == 1
        assert result["unresolved"] == []
        assert result["call_file"] is None

    def test_collects_low_confidence(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="default")

        conflicts = [Conflict(spec_name="auth", description="A", confidence="low")]
        result = engine._handle_conflicts_default(conflicts, MagicMock())

        assert len(result["unresolved"]) == 1
        assert result["call_file"] is not None

    def test_empty_confidence_treated_as_low(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="default")

        conflicts = [Conflict(spec_name="auth", description="A", confidence="")]
        result = engine._handle_conflicts_default(conflicts, MagicMock())

        assert len(result["unresolved"]) == 1
        assert result["call_file"] is not None

    def test_mixed_confidence_levels(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth")
        engine = SyncEngine(tmp_path, mode="default")
        engine._load_specs()

        llm = MagicMock()
        llm.call.side_effect = [
            json.dumps({"decision": "create_issue"}),
        ]

        conflicts = [
            Conflict(spec_name="auth", description="High", confidence="high"),
            Conflict(spec_name="config", description="Low", confidence="low"),
        ]
        result = engine._handle_conflicts_default(conflicts, llm)

        assert result["issues_created"] == 1
        assert len(result["unresolved"]) == 1
        assert result["unresolved"][0].description == "Low"
        assert result["call_file"] is not None

    def test_all_high_confidence_no_call_file(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth")
        engine = SyncEngine(tmp_path, mode="default")
        engine._load_specs()

        llm = MagicMock()
        llm.call.side_effect = [
            json.dumps({"decision": "update_spec"}),
            "# Updated",
        ]

        conflicts = [Conflict(spec_name="auth", description="A", confidence="high")]
        result = engine._handle_conflicts_default(conflicts, llm)

        assert result["call_file"] is None


# ---------------------------------------------------------------------------
# MCP call file generation and response parsing
# ---------------------------------------------------------------------------

class TestGenerateCallFile:
    def test_creates_call_file(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="default")
        conflicts = [
            Conflict(spec_name="auth", description="Token issue", code_location="src/a.py:10"),
            Conflict(spec_name="config", description="Format issue"),
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
        engine._generate_call_file(conflicts)

        assert (tmp_path / "se3" / "calls").is_dir()

    def test_conflict_options_included(self, tmp_path):
        engine = SyncEngine(tmp_path)
        conflicts = [Conflict(spec_name="x", description="d")]
        call_path = engine._generate_call_file(conflicts)

        data = json.loads(call_path.read_text())
        assert data["conflicts"][0]["options"] == ["update_spec", "create_issue"]

    def test_file_name_contains_sync_conflicts(self, tmp_path):
        engine = SyncEngine(tmp_path)
        call_path = engine._generate_call_file([Conflict(spec_name="x", description="d")])
        assert "sync_conflicts" in call_path.name


class TestProcessCallResponse:
    def _setup_call_response(self, tmp_path, call_conflicts, response_decisions):
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

    def test_update_spec_decision(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Old")

        call_file = self._setup_call_response(
            tmp_path,
            [{"id": 1, "spec_name": "auth", "description": "Mismatch", "code_location": "src/a.py"}],
            [{"id": 1, "decision": "update_spec"}],
        )

        llm = MagicMock()
        llm.call.return_value = "# Updated auth spec"

        engine = SyncEngine(tmp_path)
        engine._load_specs()
        result = engine.process_call_response(call_file, llm)

        assert result["specs_updated"] == 1
        assert (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text() == "# Updated auth spec"

    def test_create_issue_decision(self, tmp_path):
        call_file = self._setup_call_response(
            tmp_path,
            [{"id": 1, "spec_name": "auth", "description": "Mismatch", "code_location": ""}],
            [{"id": 1, "decision": "create_issue"}],
        )

        engine = SyncEngine(tmp_path)
        result = engine.process_call_response(call_file, MagicMock())

        assert result["issues_created"] == 1
        mgr = IssueManager(tmp_path)
        assert len(mgr.list_issues()) == 1
        assert "[sync-conflict]" in mgr.list_issues()[0].title

    def test_skips_invalid_decision(self, tmp_path):
        call_file = self._setup_call_response(
            tmp_path,
            [{"id": 1, "spec_name": "x", "description": "d", "code_location": ""}],
            [{"id": 1, "decision": "skip_this"}],
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

    def test_mixed_decisions(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth")

        call_file = self._setup_call_response(
            tmp_path,
            [
                {"id": 1, "spec_name": "auth", "description": "A", "code_location": ""},
                {"id": 2, "spec_name": "config", "description": "B", "code_location": ""},
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

    def test_auto_loads_specs_if_not_loaded(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth")

        call_file = self._setup_call_response(
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
# SyncEngine.run() orchestration
# ---------------------------------------------------------------------------

class TestSyncEngineRun:
    @patch("se3.engine.sync_engine.SyncEngine._load_specs")
    @patch("se3.engine.sync_engine.SyncEngine._load_existing_issues")
    @patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec")
    @patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None)
    @patch("se3.engine.project_context.ProjectContextCollector.collect")
    def test_orchestrates_full_workflow(
        self, mock_collect, mock_llm_init, mock_analyze, mock_load_issues, mock_load_specs, tmp_path
    ):
        mock_collect.return_value = {"git": {}, "flow_engine": None, "backlog": [], "specs": []}
        mock_load_specs.return_value = {
            "base": {"name": "base", "path": Path("f"), "content": "# Base"},
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
    def test_generates_base_spec_when_missing(
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
                diffs=[SpecDiff(DiffType.GAP, "auth", "Missing login")],
            )
            mock_llm_call.return_value = json.dumps({
                "decision": "create_issue", "confidence": "high", "reasoning": "needed"
            })

            engine = SyncEngine(tmp_path)
            engine._sync_issues = []
            result = engine.run()

            assert result.issues_created == 1

    def test_run_fast_mode_uses_fast_handler(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")

        with patch("se3.engine.sync_engine.SyncEngine._load_existing_issues", return_value=[]), \
             patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None), \
             patch("se3.engine.project_context.ProjectContextCollector.collect",
                   return_value={"git": {}, "flow_engine": None, "backlog": [], "specs": []}), \
             patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec") as mock_analyze, \
             patch("se3.engine.sync_engine.SyncEngine._handle_conflicts_fast") as mock_fast:

            mock_analyze.return_value = SpecAnalysis(
                spec_name="auth",
                diffs=[SpecDiff(DiffType.CONFLICT, "auth", "Mismatch")],
            )
            mock_fast.return_value = {"specs_updated": 1, "issues_created": 0}

            engine = SyncEngine(tmp_path, mode="fast")
            engine._sync_issues = []
            result = engine.run()

            assert result.call_file is None
            mock_fast.assert_called_once()

    @patch("se3.engine.sync_engine.SyncEngine._load_specs")
    @patch("se3.engine.sync_engine.SyncEngine._load_existing_issues")
    @patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec")
    @patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None)
    @patch("se3.engine.project_context.ProjectContextCollector.collect")
    def test_run_calls_progress_callback(
        self, mock_collect, mock_llm_init, mock_analyze, mock_load_issues, mock_load_specs, tmp_path
    ):
        mock_collect.return_value = {"git": {}, "flow_engine": None, "backlog": [], "specs": []}
        mock_load_specs.return_value = {
            "base": {"name": "base", "path": Path("f"), "content": "# Base"},
            "auth": {"name": "auth", "path": Path("g"), "content": "# Auth"},
        }
        mock_analyze.side_effect = [
            SpecAnalysis(spec_name="base", diffs=[]),
            SpecAnalysis(spec_name="auth", diffs=[]),
        ]
        mock_load_issues.return_value = []

        calls = []
        engine = SyncEngine(tmp_path)
        engine._sync_issues = []
        engine.run(progress_callback=lambda *args: calls.append(args))

        assert len(calls) == 4
        assert calls[0][0] == "analyzing"
        assert calls[1][0] == "analyzed"
        assert calls[2][0] == "analyzing"
        assert calls[3][0] == "analyzed"


# ---------------------------------------------------------------------------
# Conflict resolution helpers
# ---------------------------------------------------------------------------

class TestResolveConflictViaLLM:
    def test_returns_update_spec(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = json.dumps({"decision": "update_spec", "reasoning": "ok"})

        conflict = Conflict(spec_name="auth", description="Mismatch")
        assert engine._resolve_conflict_via_llm(conflict, llm) == "update_spec"

    def test_returns_create_issue(self, tmp_path):
        engine = SyncEngine(tmp_path)

        llm = MagicMock()
        llm.call.return_value = json.dumps({"decision": "create_issue"})

        conflict = Conflict(spec_name="x", description="d")
        assert engine._resolve_conflict_via_llm(conflict, llm) == "create_issue"

    def test_defaults_on_unknown_decision(self, tmp_path):
        engine = SyncEngine(tmp_path)

        llm = MagicMock()
        llm.call.return_value = json.dumps({"decision": "ignore"})

        conflict = Conflict(spec_name="x", description="d")
        assert engine._resolve_conflict_via_llm(conflict, llm) == "create_issue"

    def test_defaults_on_llm_error(self, tmp_path):
        engine = SyncEngine(tmp_path)

        llm = MagicMock()
        llm.call.side_effect = RuntimeError("fail")

        conflict = Conflict(spec_name="x", description="d")
        assert engine._resolve_conflict_via_llm(conflict, llm) == "create_issue"

    def test_prompt_includes_spec_content(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth with JWT tokens")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = json.dumps({"decision": "create_issue"})

        conflict = Conflict(spec_name="auth", description="Token mismatch")
        engine._resolve_conflict_via_llm(conflict, llm)

        prompt = llm.call.call_args.kwargs.get("prompt") or llm.call.call_args[0][0]
        assert "Auth with JWT tokens" in prompt


class TestApplyConflictSpecUpdate:
    def test_updates_spec_file(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Old")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "# Updated"

        conflict = Conflict(spec_name="auth", description="d")
        assert engine._apply_conflict_spec_update(conflict, llm) is True
        assert (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text() == "# Updated"

    def test_returns_false_for_missing_spec(self, tmp_path):
        engine = SyncEngine(tmp_path)

        conflict = Conflict(spec_name="nonexistent", description="d")
        assert engine._apply_conflict_spec_update(conflict, MagicMock()) is False

    def test_returns_false_on_empty_response(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Original")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "   "

        conflict = Conflict(spec_name="auth", description="d")
        assert engine._apply_conflict_spec_update(conflict, llm) is False
        assert (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text() == "# Original"

    def test_returns_false_on_llm_error(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Original")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.side_effect = RuntimeError("fail")

        conflict = Conflict(spec_name="auth", description="d")
        assert engine._apply_conflict_spec_update(conflict, llm) is False


# ---------------------------------------------------------------------------
# _process_extensions — LLM spec update flow
# ---------------------------------------------------------------------------

class TestProcessExtensions:
    def test_updates_spec_file_on_disk(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth\n## Purpose\nOriginal auth spec content.")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "# Auth\n## Purpose\nOriginal auth spec content.\n\n### Requirement: New Feature\nNew feature description."

        extensions = [SpecDiff(DiffType.EXTENSION, "auth", "New feature found", "src/auth.py:50")]
        spec_info = engine._specs["auth"]
        updated = engine._process_extensions(extensions, spec_info, llm)

        assert updated == 1
        content = (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text()
        assert "New Feature" in content

    def test_prompt_includes_extension_descriptions(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "# Auth spec\n\n### New stuff added"

        extensions = [
            SpecDiff(DiffType.EXTENSION, "auth", "Token refresh logic", "src/auth.py:100"),
            SpecDiff(DiffType.EXTENSION, "auth", "Rate limiting", "src/middleware.py:20"),
        ]
        engine._process_extensions(extensions, engine._specs["auth"], llm)

        prompt = llm.call.call_args.kwargs.get("prompt", "")
        assert "Token refresh logic" in prompt
        assert "Rate limiting" in prompt
        assert "src/auth.py:100" in prompt

    def test_updates_specs_cache_after_write(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Old content")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        new_content = "# Updated content with extensions"
        llm = MagicMock()
        llm.call.return_value = new_content

        extensions = [SpecDiff(DiffType.EXTENSION, "auth", "New")]
        engine._process_extensions(extensions, engine._specs["auth"], llm)

        assert engine._specs["auth"]["content"] == new_content

    def test_rejects_suspiciously_short_update(self, tmp_path):
        original = "# Auth Spec\n\n## Purpose\nDetailed auth specification with many requirements.\n" * 5
        _create_spec(tmp_path, "auth", original)
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "# Short"

        extensions = [SpecDiff(DiffType.EXTENSION, "auth", "New")]
        updated = engine._process_extensions(extensions, engine._specs["auth"], llm)

        assert updated == 0
        assert (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text() == original

    def test_empty_extensions_returns_zero(self, tmp_path):
        engine = SyncEngine(tmp_path)
        result = engine._process_extensions([], {}, MagicMock())
        assert result == 0

    def test_handles_llm_error(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.side_effect = RuntimeError("LLM failed")

        extensions = [SpecDiff(DiffType.EXTENSION, "auth", "New")]
        updated = engine._process_extensions(extensions, engine._specs["auth"], llm)

        assert updated == 0
        assert (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text() == "# Auth"


# ---------------------------------------------------------------------------
# Integration: run() with extensions + issue lifecycle
# ---------------------------------------------------------------------------

class TestSyncEngineRunIntegration:
    def test_extensions_and_lifecycle_together(self, tmp_path):
        """End-to-end: spec gets extended, then conflict resolution uses fresh cache."""
        _create_spec(tmp_path, "auth", "# Auth spec\n## Purpose\nAuth module.")

        with patch("se3.engine.sync_engine.SyncEngine._load_existing_issues", return_value=[]), \
             patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None), \
             patch("se3.engine.llm_caller.LLMCaller.call") as mock_llm_call, \
             patch("se3.engine.project_context.ProjectContextCollector.collect",
                   return_value={"git": {}, "flow_engine": None, "backlog": [], "specs": []}), \
             patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec") as mock_analyze:

            mock_analyze.return_value = SpecAnalysis(
                spec_name="auth",
                diffs=[
                    SpecDiff(DiffType.EXTENSION, "auth", "New helper function"),
                ],
            )

            mock_llm_call.return_value = "# Auth spec\n## Purpose\nAuth module.\n\n### Requirement: Helper\nNew helper."

            engine = SyncEngine(tmp_path)
            engine._sync_issues = []
            result = engine.run()

            assert result.specs_updated == 1
            assert engine._specs["auth"]["content"] == mock_llm_call.return_value

    def test_confidence_case_insensitive_in_full_run(self, tmp_path):
        """Verify that 'High' confidence (mixed case) is auto-resolved in default mode."""
        _create_spec(tmp_path, "auth", "# Auth")

        with patch("se3.engine.sync_engine.SyncEngine._load_existing_issues", return_value=[]), \
             patch("se3.engine.llm_caller.LLMCaller.__init__", return_value=None), \
             patch("se3.engine.llm_caller.LLMCaller.call") as mock_llm_call, \
             patch("se3.engine.project_context.ProjectContextCollector.collect",
                   return_value={"git": {}, "flow_engine": None, "backlog": [], "specs": []}), \
             patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec") as mock_analyze:

            mock_analyze.return_value = SpecAnalysis(
                spec_name="auth",
                diffs=[
                    SpecDiff(DiffType.CONFLICT, "auth", "Mismatch", confidence="High"),
                ],
            )

            mock_llm_call.side_effect = [
                json.dumps({"decision": "update_spec"}),
                "# Updated auth",
            ]

            engine = SyncEngine(tmp_path, mode="default")
            engine._sync_issues = []
            result = engine.run()

            assert result.specs_updated == 1
            assert result.conflicts == []
            assert result.call_file is None


# ---------------------------------------------------------------------------
# G2: _normalize_for_matching
# ---------------------------------------------------------------------------

class TestNormalizeForMatching:
    def test_basic_sync_title(self, tmp_path):
        engine = SyncEngine(tmp_path)
        a = engine._normalize_for_matching("[sync] auth: Missing the Login Validation")
        b = engine._normalize_for_matching("[sync] auth: missing login validation")
        assert a == b

    def test_removes_articles(self, tmp_path):
        engine = SyncEngine(tmp_path)
        a = engine._normalize_for_matching("[sync] auth: A missing login, validation.")
        b = engine._normalize_for_matching("[sync] auth: missing login validation")
        assert a == b

    def test_removes_punctuation(self, tmp_path):
        engine = SyncEngine(tmp_path)
        result = engine._normalize_for_matching("[sync] cfg: Check (input) validation!")
        assert "(" not in result
        assert ")" not in result
        assert "!" not in result

    def test_non_sync_title_fallback(self, tmp_path):
        engine = SyncEngine(tmp_path)
        result = engine._normalize_for_matching("  Some Random Title  ")
        assert result == "some random title"

    def test_empty_string(self, tmp_path):
        engine = SyncEngine(tmp_path)
        assert engine._normalize_for_matching("") == ""

    def test_preserves_spec_name(self, tmp_path):
        engine = SyncEngine(tmp_path)
        result = engine._normalize_for_matching("[sync] my-spec: The description")
        assert result.startswith("[sync] my-spec:")

    def test_sync_conflict_prefix(self, tmp_path):
        engine = SyncEngine(tmp_path)
        result = engine._normalize_for_matching("[sync-conflict] auth: Token mismatch")
        assert "[sync] auth:" in result

    def test_collapses_whitespace(self, tmp_path):
        engine = SyncEngine(tmp_path)
        result = engine._normalize_for_matching("[sync] auth:   multiple   spaces   here")
        assert "  " not in result.split(": ", 1)[1]


# ---------------------------------------------------------------------------
# G2: _extract_spec_name_from_title
# ---------------------------------------------------------------------------

class TestExtractSpecNameFromTitle:
    def test_extracts_from_sync_title(self, tmp_path):
        engine = SyncEngine(tmp_path)
        assert engine._extract_spec_name_from_title("[sync] auth: Missing login") == "auth"

    def test_extracts_from_sync_conflict_title(self, tmp_path):
        engine = SyncEngine(tmp_path)
        assert engine._extract_spec_name_from_title("[sync-conflict] cfg: Mismatch") == "cfg"

    def test_returns_none_for_non_sync(self, tmp_path):
        engine = SyncEngine(tmp_path)
        assert engine._extract_spec_name_from_title("Regular issue") is None

    def test_returns_none_for_empty(self, tmp_path):
        engine = SyncEngine(tmp_path)
        assert engine._extract_spec_name_from_title("") is None


# ---------------------------------------------------------------------------
# G2: _process_gaps normalized idempotency
# ---------------------------------------------------------------------------

class TestProcessGapsNormalized:
    def test_skips_when_normalized_title_matches(self, tmp_path):
        """Existing issue with slightly different wording should block creation."""
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: Missing the Login Validation", "d", tags=list(SYNC_TAGS))

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        gaps = [SpecDiff(DiffType.GAP, "auth", "Missing Login Validation")]
        created = engine._process_gaps(gaps)
        assert created == 0

    def test_creates_when_different_gap(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: Missing login", "d", tags=list(SYNC_TAGS))

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        gaps = [SpecDiff(DiffType.GAP, "auth", "Missing signup endpoint")]
        created = engine._process_gaps(gaps)
        assert created == 1

    def test_empty_sync_issues_creates_all(self, tmp_path):
        engine = SyncEngine(tmp_path)
        engine._sync_issues = []

        gaps = [
            SpecDiff(DiffType.GAP, "auth", "A"),
            SpecDiff(DiffType.GAP, "auth", "B"),
        ]
        created = engine._process_gaps(gaps)
        assert created == 2


# ---------------------------------------------------------------------------
# G2: _manage_issue_lifecycle with three-layer matching
# ---------------------------------------------------------------------------

class TestManageIssueLifecycleNormalized:
    def test_keeps_issue_with_exact_title(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: Missing login", "d", tags=list(SYNC_TAGS))

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        closed = engine._manage_issue_lifecycle({"[sync] auth: Missing login"})
        assert closed == 0
        assert len(mgr.list_issues()) == 1

    def test_keeps_issue_with_normalized_match(self, tmp_path):
        """Title differs in articles/punctuation/case but normalizes the same."""
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: The Missing Login!", "d", tags=list(SYNC_TAGS))

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        closed = engine._manage_issue_lifecycle({"[sync] auth: missing login"})
        assert closed == 0
        assert len(mgr.list_issues()) == 1

    def test_keeps_issue_with_prefix_fallback(self, tmp_path):
        """Description totally different but same spec still has gaps → keep."""
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: Old wording for some gap", "d", tags=list(SYNC_TAGS))

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        closed = engine._manage_issue_lifecycle(
            {"[sync] auth: Completely different description"}
        )
        assert closed == 0
        assert len(mgr.list_issues()) == 1

    def test_closes_when_spec_has_no_gaps(self, tmp_path):
        """Spec has zero current gaps → close its issues."""
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: Missing login", "d", tags=list(SYNC_TAGS))

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        closed = engine._manage_issue_lifecycle(
            {"[sync] config: Some other gap"}
        )
        assert closed == 1
        assert len(mgr.list_issues()) == 0

    def test_non_sync_title_uses_fallback(self, tmp_path):
        """Non-[sync] issue: only normalized exact match applies."""
        mgr = IssueManager(tmp_path)
        mgr.create("Manual bug report", "d", tags=list(SYNC_TAGS))

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        closed = engine._manage_issue_lifecycle(set())
        assert closed == 1

    def test_catches_os_error_on_close(self, tmp_path):
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: Missing login", "d", tags=list(SYNC_TAGS))

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        with patch.object(IssueManager, "close_issue", side_effect=OSError("disk error")):
            closed = engine._manage_issue_lifecycle(set())
            assert closed == 0


# ---------------------------------------------------------------------------
# G3: _gather_all_conflicts populates spec_content
# ---------------------------------------------------------------------------

class TestGatherAllConflictsSpecContent:
    def test_populates_spec_content_from_cache(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec content here")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        analyses = [SpecAnalysis(
            spec_name="auth",
            diffs=[SpecDiff(DiffType.CONFLICT, "auth", "Token mismatch", "src/a.py:10", "high")],
        )]
        conflicts = engine._gather_all_conflicts(analyses)

        assert len(conflicts) == 1
        assert conflicts[0].spec_content == "# Auth spec content here"

    def test_empty_spec_content_when_spec_missing(self, tmp_path):
        engine = SyncEngine(tmp_path)
        engine._specs = {}

        analyses = [SpecAnalysis(
            spec_name="nonexistent",
            diffs=[SpecDiff(DiffType.CONFLICT, "nonexistent", "Some conflict")],
        )]
        conflicts = engine._gather_all_conflicts(analyses)

        assert len(conflicts) == 1
        assert conflicts[0].spec_content == ""

    def test_preserves_other_fields(self, tmp_path):
        _create_spec(tmp_path, "cfg", "# Config")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        analyses = [SpecAnalysis(
            spec_name="cfg",
            diffs=[SpecDiff(DiffType.CONFLICT, "cfg", "Format issue", "src/cfg.py:5", "low")],
        )]
        conflicts = engine._gather_all_conflicts(analyses)

        c = conflicts[0]
        assert c.spec_name == "cfg"
        assert c.description == "Format issue"
        assert c.code_location == "src/cfg.py:5"
        assert c.confidence == "low"


# ---------------------------------------------------------------------------
# G3: _generate_call_file includes spec_content
# ---------------------------------------------------------------------------

class TestGenerateCallFileSpecContent:
    def test_call_file_contains_spec_content(self, tmp_path):
        engine = SyncEngine(tmp_path)
        conflicts = [Conflict(
            spec_name="auth",
            description="Mismatch",
            spec_content="# Full spec content",
            code_location="src/a.py:10",
        )]
        call_path = engine._generate_call_file(conflicts)

        data = json.loads(call_path.read_text())
        assert data["conflicts"][0]["spec_content"] == "# Full spec content"

    def test_call_file_truncates_long_spec_content(self, tmp_path):
        engine = SyncEngine(tmp_path)
        long_content = "x" * 5000
        conflicts = [Conflict(
            spec_name="auth",
            description="d",
            spec_content=long_content,
        )]
        call_path = engine._generate_call_file(conflicts)

        data = json.loads(call_path.read_text())
        assert len(data["conflicts"][0]["spec_content"]) == 2000

    def test_call_file_empty_spec_content(self, tmp_path):
        engine = SyncEngine(tmp_path)
        conflicts = [Conflict(spec_name="auth", description="d", spec_content="")]
        call_path = engine._generate_call_file(conflicts)

        data = json.loads(call_path.read_text())
        assert data["conflicts"][0]["spec_content"] == ""


# ---------------------------------------------------------------------------
# G3: process_call_response skips unknown conflict_id
# ---------------------------------------------------------------------------

class TestProcessCallResponseUnknownConflictId:
    def _setup_call_response(self, tmp_path, call_conflicts, response_decisions):
        calls_dir = tmp_path / "se3" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)

        call_file = calls_dir / "sync_conflicts_99999.json"
        call_data = {
            "type": "sync_conflicts",
            "mode": "strict",
            "timestamp": 99999,
            "conflicts": call_conflicts,
        }
        call_file.write_text(json.dumps(call_data), encoding="utf-8")

        response_file = calls_dir / "sync_conflicts_99999.json.response"
        response_data = {"conflicts": response_decisions}
        response_file.write_text(json.dumps(response_data), encoding="utf-8")

        return call_file

    def test_skips_unknown_id_and_logs_warning(self, tmp_path, caplog):
        call_file = self._setup_call_response(
            tmp_path,
            [{"id": 1, "spec_name": "auth", "description": "Real", "code_location": ""}],
            [{"id": 999, "decision": "create_issue"}],
        )

        engine = SyncEngine(tmp_path)
        with caplog.at_level(logging.WARNING):
            result = engine.process_call_response(call_file, MagicMock())

        assert result["specs_updated"] == 0
        assert result["issues_created"] == 0
        assert "unknown conflict_id 999" in caplog.text.lower()

    def test_known_id_still_processed(self, tmp_path):
        call_file = self._setup_call_response(
            tmp_path,
            [{"id": 1, "spec_name": "auth", "description": "Real conflict", "code_location": ""}],
            [{"id": 1, "decision": "create_issue"}],
        )

        engine = SyncEngine(tmp_path)
        result = engine.process_call_response(call_file, MagicMock())

        assert result["issues_created"] == 1

    def test_mix_known_and_unknown_ids(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth")

        call_file = self._setup_call_response(
            tmp_path,
            [{"id": 1, "spec_name": "auth", "description": "Real", "code_location": ""}],
            [
                {"id": 1, "decision": "create_issue"},
                {"id": 42, "decision": "update_spec"},
            ],
        )

        engine = SyncEngine(tmp_path)
        result = engine.process_call_response(call_file, MagicMock())

        assert result["issues_created"] == 1
        assert result["specs_updated"] == 0

    def test_no_empty_issue_created_for_unknown_id(self, tmp_path):
        call_file = self._setup_call_response(
            tmp_path,
            [],
            [{"id": 1, "decision": "create_issue"}],
        )

        engine = SyncEngine(tmp_path)
        result = engine.process_call_response(call_file, MagicMock())

        mgr = IssueManager(tmp_path)
        assert len(mgr.list_issues()) == 0
        assert result["issues_created"] == 0


# ---------------------------------------------------------------------------
# strip_markdown_fences unit tests
# ---------------------------------------------------------------------------

class TestStripMarkdownFences:
    def test_strips_fences_with_language(self):
        text = "```markdown\n# Heading\n\nContent here.\n```"
        assert strip_markdown_fences(text) == "# Heading\n\nContent here."

    def test_strips_fences_without_language(self):
        text = "```\n# Heading\n\nContent here.\n```"
        assert strip_markdown_fences(text) == "# Heading\n\nContent here."

    def test_strips_fences_with_md_language(self):
        text = "```md\n# Title\n```"
        assert strip_markdown_fences(text) == "# Title"

    def test_no_fences_returns_original(self):
        text = "# Just a heading\n\nSome content."
        assert strip_markdown_fences(text) == text

    def test_empty_string_returns_empty(self):
        assert strip_markdown_fences("") == ""

    def test_only_opening_fence_returns_original(self):
        text = "```markdown\n# Heading\nNo closing fence"
        assert strip_markdown_fences(text) == text

    def test_only_closing_fence_returns_original(self):
        text = "# Heading\nContent\n```"
        assert strip_markdown_fences(text) == text

    def test_preserves_inner_fences(self):
        text = "```markdown\n# Doc\n\n```python\nprint('hi')\n```\n\nMore text.\n```"
        result = strip_markdown_fences(text)
        assert "```python" in result
        assert result.startswith("# Doc")
        assert result.endswith("More text.")

    def test_handles_surrounding_whitespace(self):
        text = "  \n```markdown\n# Content\n```\n  "
        assert strip_markdown_fences(text) == "# Content"

    def test_whitespace_only_returns_original(self):
        text = "   \n\n   "
        assert strip_markdown_fences(text) == text


# ---------------------------------------------------------------------------
# _apply_conflict_spec_update length guard
# ---------------------------------------------------------------------------

class TestApplyConflictSpecUpdateLengthGuard:
    def test_rejects_suspiciously_short_response(self, tmp_path):
        original = "# Auth Spec\n\n## Purpose\nDetailed auth specification.\n" * 5
        _create_spec(tmp_path, "auth", original)
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "# Short"

        conflict = Conflict(spec_name="auth", description="Token mismatch")
        result = engine._apply_conflict_spec_update(conflict, llm)

        assert result is False
        assert (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text() == original

    def test_accepts_normal_length_response(self, tmp_path):
        original = "# Auth Spec\n## Purpose\nAuth."
        _create_spec(tmp_path, "auth", original)
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        updated = "# Auth Spec\n## Purpose\nAuth updated with new info."
        llm = MagicMock()
        llm.call.return_value = updated

        conflict = Conflict(spec_name="auth", description="Token mismatch")
        result = engine._apply_conflict_spec_update(conflict, llm)

        assert result is True
        assert (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text() == updated

    def test_boundary_at_fifty_percent(self, tmp_path):
        original = "x" * 100
        _create_spec(tmp_path, "auth", original)
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "x" * 49
        conflict = Conflict(spec_name="auth", description="d")
        assert engine._apply_conflict_spec_update(conflict, llm) is False

        llm.call.return_value = "x" * 50
        assert engine._apply_conflict_spec_update(conflict, llm) is True

    def test_strips_fences_before_length_check(self, tmp_path):
        original = "# Auth Spec\n## Purpose\nAuth module details."
        _create_spec(tmp_path, "auth", original)
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        fenced = "```markdown\n# Auth Spec\n## Purpose\nAuth module details updated.\n```"
        llm = MagicMock()
        llm.call.return_value = fenced

        conflict = Conflict(spec_name="auth", description="d")
        result = engine._apply_conflict_spec_update(conflict, llm)

        assert result is True
        written = (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text()
        assert not written.startswith("```")


# ---------------------------------------------------------------------------
# _process_extensions fence stripping
# ---------------------------------------------------------------------------

class TestProcessExtensionsFenceStripping:
    def test_strips_fences_from_llm_response(self, tmp_path):
        original = "# Auth\n## Purpose\nOriginal spec."
        _create_spec(tmp_path, "auth", original)
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        fenced = "```markdown\n# Auth\n## Purpose\nOriginal spec.\n\n### New\nNew feature.\n```"
        llm = MagicMock()
        llm.call.return_value = fenced

        extensions = [SpecDiff(DiffType.EXTENSION, "auth", "New feature")]
        updated = engine._process_extensions(extensions, engine._specs["auth"], llm)

        assert updated == 1
        written = (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text()
        assert not written.startswith("```")
        assert "### New" in written


# ---------------------------------------------------------------------------
# Fix: _manage_issue_lifecycle excludes conflict issues
# ---------------------------------------------------------------------------

class TestManageIssueLifecycleConflictExclusion:
    def test_does_not_close_conflict_issues(self, tmp_path):
        """Conflict issues should not be managed by gap lifecycle."""
        mgr = IssueManager(tmp_path)
        mgr.create(
            "[sync-conflict] auth: Token mismatch", "d",
            tags=list(SYNC_TAGS) + ["conflict"],
        )

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        closed = engine._manage_issue_lifecycle(set())
        assert closed == 0
        assert len(mgr.list_issues()) == 1

    def test_closes_gap_but_keeps_conflict_for_same_spec(self, tmp_path):
        """Gap issue gets closed when resolved, but conflict issue stays."""
        mgr = IssueManager(tmp_path)
        mgr.create("[sync] auth: Missing login", "d", tags=list(SYNC_TAGS))
        mgr.create(
            "[sync-conflict] auth: Token format", "d",
            tags=list(SYNC_TAGS) + ["conflict"],
        )

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        closed = engine._manage_issue_lifecycle(set())
        assert closed == 1
        remaining = mgr.list_issues()
        assert len(remaining) == 1
        assert "conflict" in remaining[0].tags

    def test_only_conflict_issues_returns_zero(self, tmp_path):
        """When all sync issues are conflicts, nothing is closed."""
        mgr = IssueManager(tmp_path)
        mgr.create(
            "[sync-conflict] auth: A", "d",
            tags=list(SYNC_TAGS) + ["conflict"],
        )
        mgr.create(
            "[sync-conflict] cfg: B", "d",
            tags=list(SYNC_TAGS) + ["conflict"],
        )

        engine = SyncEngine(tmp_path)
        engine._load_existing_issues()

        closed = engine._manage_issue_lifecycle(set())
        assert closed == 0
        assert len(mgr.list_issues()) == 2


# ---------------------------------------------------------------------------
# Fix: strip_markdown_fences with unbalanced inner fences
# ---------------------------------------------------------------------------

class TestStripMarkdownFencesUnbalanced:
    def test_no_strip_when_inner_fence_unbalanced(self):
        """Content ending with a code block whose close looks like outer close."""
        text = "```markdown\nSome content\n```python\ncode\n```"
        result = strip_markdown_fences(text)
        assert result == text

    def test_strips_when_inner_fences_balanced(self):
        """Properly nested inner block should be stripped correctly."""
        text = "```markdown\n# Doc\n```python\ncode\n```\nMore.\n```"
        result = strip_markdown_fences(text)
        assert result == "# Doc\n```python\ncode\n```\nMore."

    def test_no_strip_single_fence_at_end(self):
        """Single inner opening fence with no matching close inside."""
        text = "```md\n# Title\n```python\n```"
        result = strip_markdown_fences(text)
        assert result == text


# ---------------------------------------------------------------------------
# G1: PendingDecision data model
# ---------------------------------------------------------------------------

from se3.engine.sync_engine import PendingDecision


class TestPendingDecision:
    def test_to_dict_roundtrip(self):
        pd = PendingDecision(
            type="gap",
            item_id="gap_auth_abc12345",
            spec_name="auth",
            description="Missing login",
            diff="src/auth.py:10",
            confidence="high",
            decision="pending",
        )
        data = pd.to_dict()
        restored = PendingDecision.from_dict(data)

        assert restored.type == "gap"
        assert restored.item_id == "gap_auth_abc12345"
        assert restored.spec_name == "auth"
        assert restored.description == "Missing login"
        assert restored.diff == "src/auth.py:10"
        assert restored.confidence == "high"
        assert restored.decision == "pending"

    def test_from_dict_defaults(self):
        data = {"type": "conflict"}
        pd = PendingDecision.from_dict(data)
        assert pd.item_id == ""
        assert pd.spec_name == ""
        assert pd.description == ""
        assert pd.diff == ""
        assert pd.confidence == ""
        assert pd.decision == "pending"

    def test_default_decision_is_pending(self):
        pd = PendingDecision(type="gap", spec_name="auth", description="d")
        assert pd.decision == "pending"

    def test_conflict_type(self):
        pd = PendingDecision(type="conflict", spec_name="cfg", description="d")
        data = pd.to_dict()
        assert data["type"] == "conflict"
        assert PendingDecision.from_dict(data).type == "conflict"

    def test_decision_values(self):
        for decision in ("pending", "update_spec", "create_issue"):
            pd = PendingDecision(type="gap", decision=decision)
            assert pd.to_dict()["decision"] == decision


# ---------------------------------------------------------------------------
# G1: SyncResult new fields serialization
# ---------------------------------------------------------------------------

class TestSyncResultNewFields:
    def test_new_fields_default_to_empty(self):
        result = SyncResult()
        assert result.specs_created == []
        assert result.gap_resolutions == []
        assert result.detailed_changes == []
        assert result.pending_decisions == []

    def test_new_fields_to_dict(self):
        result = SyncResult(
            specs_created=["new-spec"],
            gap_resolutions=[{"spec_name": "auth", "action": "update_spec", "description": "removed"}],
            detailed_changes=[{"spec_name": "auth", "changes": "added section"}],
            pending_decisions=[PendingDecision(type="gap", spec_name="auth", description="d")],
        )
        data = result.to_dict()
        assert data["specs_created"] == ["new-spec"]
        assert len(data["gap_resolutions"]) == 1
        assert data["gap_resolutions"][0]["action"] == "update_spec"
        assert len(data["detailed_changes"]) == 1
        assert len(data["pending_decisions"]) == 1
        assert data["pending_decisions"][0]["type"] == "gap"

    def test_new_fields_from_dict_backward_compatible(self):
        old_data = {
            "analyses": [],
            "issues_created": 2,
            "issues_closed": 1,
            "specs_updated": 3,
            "conflicts": [],
            "call_file": None,
            "completed_at": "2025-01-01T00:00:00",
        }
        result = SyncResult.from_dict(old_data)
        assert result.specs_created == []
        assert result.gap_resolutions == []
        assert result.detailed_changes == []
        assert result.pending_decisions == []
        assert result.issues_created == 2
        assert result.specs_updated == 3

    def test_new_fields_roundtrip(self):
        pd = PendingDecision(type="conflict", item_id="c1", spec_name="cfg", description="d")
        result = SyncResult(
            specs_created=["a", "b"],
            gap_resolutions=[{"spec_name": "a", "action": "create_issue", "description": "gap"}],
            detailed_changes=[{"spec_name": "a", "changes": "added"}],
            pending_decisions=[pd],
        )
        data = result.to_dict()
        restored = SyncResult.from_dict(data)
        assert restored.specs_created == ["a", "b"]
        assert len(restored.gap_resolutions) == 1
        assert len(restored.detailed_changes) == 1
        assert len(restored.pending_decisions) == 1
        assert restored.pending_decisions[0].type == "conflict"
        assert restored.pending_decisions[0].item_id == "c1"


# ---------------------------------------------------------------------------
# G1: Gap decision flow tests
# ---------------------------------------------------------------------------

class TestResolveGapViaLLM:
    def test_returns_update_spec(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = json.dumps({
            "decision": "update_spec", "confidence": "high", "reasoning": "outdated"
        })

        gap = SpecDiff(DiffType.GAP, "auth", "Old requirement")
        result = engine._resolve_gap_via_llm(gap, llm)
        assert result["decision"] == "update_spec"
        assert result["confidence"] == "high"

    def test_returns_create_issue(self, tmp_path):
        engine = SyncEngine(tmp_path)

        llm = MagicMock()
        llm.call.return_value = json.dumps({
            "decision": "create_issue", "confidence": "high", "reasoning": "needed"
        })

        gap = SpecDiff(DiffType.GAP, "auth", "Missing feature")
        result = engine._resolve_gap_via_llm(gap, llm)
        assert result["decision"] == "create_issue"

    def test_defaults_on_unknown_decision(self, tmp_path):
        engine = SyncEngine(tmp_path)

        llm = MagicMock()
        llm.call.return_value = json.dumps({"decision": "ignore"})

        gap = SpecDiff(DiffType.GAP, "auth", "d")
        result = engine._resolve_gap_via_llm(gap, llm)
        assert result["decision"] == "create_issue"

    def test_defaults_on_llm_error(self, tmp_path):
        engine = SyncEngine(tmp_path)

        llm = MagicMock()
        llm.call.side_effect = RuntimeError("fail")

        gap = SpecDiff(DiffType.GAP, "auth", "d")
        result = engine._resolve_gap_via_llm(gap, llm)
        assert result["decision"] == "create_issue"
        assert result["confidence"] == "low"

    def test_prompt_includes_spec_content(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth with JWT tokens")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = json.dumps({"decision": "create_issue", "confidence": "high"})

        gap = SpecDiff(DiffType.GAP, "auth", "Token mismatch")
        engine._resolve_gap_via_llm(gap, llm)

        prompt = llm.call.call_args.kwargs.get("prompt") or llm.call.call_args[0][0]
        assert "Auth with JWT tokens" in prompt

    def test_prompt_contains_guiding_principle(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = json.dumps({"decision": "create_issue", "confidence": "high"})

        gap = SpecDiff(DiffType.GAP, "auth", "d")
        engine._resolve_gap_via_llm(gap, llm)

        prompt = llm.call.call_args.kwargs.get("prompt") or llm.call.call_args[0][0]
        assert "implementation standard" in prompt.lower()


class TestApplyGapSpecUpdate:
    def test_updates_spec_file(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Old spec content")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "# Updated spec content"

        gap = SpecDiff(DiffType.GAP, "auth", "Remove outdated req")
        assert engine._apply_gap_spec_update(gap, llm) is True
        assert (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text() == "# Updated spec content"

    def test_returns_false_for_missing_spec(self, tmp_path):
        engine = SyncEngine(tmp_path)
        gap = SpecDiff(DiffType.GAP, "nonexistent", "d")
        assert engine._apply_gap_spec_update(gap, MagicMock()) is False

    def test_returns_false_on_empty_response(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Original")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "   "

        gap = SpecDiff(DiffType.GAP, "auth", "d")
        assert engine._apply_gap_spec_update(gap, llm) is False
        assert (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text() == "# Original"

    def test_rejects_suspiciously_short_response(self, tmp_path):
        original = "# Auth Spec\n\n## Purpose\nDetailed spec.\n" * 5
        _create_spec(tmp_path, "auth", original)
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "# Short"

        gap = SpecDiff(DiffType.GAP, "auth", "d")
        assert engine._apply_gap_spec_update(gap, llm) is False
        assert (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text() == original

    def test_strips_markdown_fences(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec content")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "```markdown\n# Auth spec content updated\n```"

        gap = SpecDiff(DiffType.GAP, "auth", "d")
        assert engine._apply_gap_spec_update(gap, llm) is True
        written = (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text()
        assert not written.startswith("```")

    def test_boundary_at_fifty_percent(self, tmp_path):
        original = "x" * 100
        _create_spec(tmp_path, "auth", original)
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "x" * 49
        gap = SpecDiff(DiffType.GAP, "auth", "d")
        assert engine._apply_gap_spec_update(gap, llm) is False

        llm.call.return_value = "x" * 50
        assert engine._apply_gap_spec_update(gap, llm) is True

    def test_returns_false_on_llm_error(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Original")
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.side_effect = RuntimeError("fail")

        gap = SpecDiff(DiffType.GAP, "auth", "d")
        assert engine._apply_gap_spec_update(gap, llm) is False


# ---------------------------------------------------------------------------
# G1: Gap processing with modes
# ---------------------------------------------------------------------------

class TestProcessGapsFastMode:
    def test_auto_decides_and_creates_issue(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="fast")
        sync_result = SyncResult()

        llm = MagicMock()
        llm.call.return_value = json.dumps({
            "decision": "create_issue", "confidence": "high", "reasoning": "needed"
        })

        gaps = [SpecDiff(DiffType.GAP, "auth", "Missing login")]
        created = engine._process_gaps(gaps, llm, sync_result)

        assert created == 1
        assert len(sync_result.gap_resolutions) == 1
        assert sync_result.gap_resolutions[0]["action"] == "create_issue"

    def test_auto_decides_and_updates_spec(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth spec content")
        engine = SyncEngine(tmp_path, mode="fast")
        engine._load_specs()
        sync_result = SyncResult()

        llm = MagicMock()
        llm.call.side_effect = [
            json.dumps({"decision": "update_spec", "confidence": "high", "reasoning": "outdated"}),
            "# Auth updated content",
        ]

        gaps = [SpecDiff(DiffType.GAP, "auth", "Old requirement")]
        created = engine._process_gaps(gaps, llm, sync_result)

        assert created == 0
        assert sync_result.specs_updated == 1
        assert len(sync_result.gap_resolutions) == 1
        assert sync_result.gap_resolutions[0]["action"] == "update_spec"

    def test_no_pending_decisions_in_fast_mode(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="fast")
        sync_result = SyncResult()

        llm = MagicMock()
        llm.call.return_value = json.dumps({
            "decision": "create_issue", "confidence": "low", "reasoning": "needed"
        })

        gaps = [SpecDiff(DiffType.GAP, "auth", "Missing")]
        engine._process_gaps(gaps, llm, sync_result)

        assert len(sync_result.pending_decisions) == 0


class TestProcessGapsDefaultMode:
    def test_high_confidence_auto_executes(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="default")
        sync_result = SyncResult()

        llm = MagicMock()
        llm.call.return_value = json.dumps({
            "decision": "create_issue", "confidence": "high", "reasoning": "needed"
        })

        gaps = [SpecDiff(DiffType.GAP, "auth", "Missing login")]
        created = engine._process_gaps(gaps, llm, sync_result)

        assert created == 1
        assert len(sync_result.pending_decisions) == 0

    def test_low_confidence_marks_pending(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="default")
        sync_result = SyncResult()

        llm = MagicMock()
        llm.call.return_value = json.dumps({
            "decision": "create_issue", "confidence": "low", "reasoning": "unsure"
        })

        gaps = [SpecDiff(DiffType.GAP, "auth", "Missing login")]
        created = engine._process_gaps(gaps, llm, sync_result)

        assert created == 0
        assert len(sync_result.pending_decisions) == 1
        pd = sync_result.pending_decisions[0]
        assert pd.type == "gap"
        assert pd.spec_name == "auth"
        assert pd.decision == "pending"

    def test_mixed_confidence_gaps(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="default")
        sync_result = SyncResult()

        llm = MagicMock()
        llm.call.side_effect = [
            json.dumps({"decision": "create_issue", "confidence": "high"}),
            json.dumps({"decision": "update_spec", "confidence": "low"}),
        ]

        gaps = [
            SpecDiff(DiffType.GAP, "auth", "Missing A"),
            SpecDiff(DiffType.GAP, "auth", "Missing B"),
        ]
        created = engine._process_gaps(gaps, llm, sync_result)

        assert created == 1
        assert len(sync_result.pending_decisions) == 1


class TestProcessGapsStrictMode:
    def test_all_gaps_marked_pending(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="strict")
        sync_result = SyncResult()

        gaps = [
            SpecDiff(DiffType.GAP, "auth", "Missing A"),
            SpecDiff(DiffType.GAP, "auth", "Missing B"),
        ]
        created = engine._process_gaps(gaps, MagicMock(), sync_result)

        assert created == 0
        assert len(sync_result.pending_decisions) == 2
        assert all(pd.type == "gap" for pd in sync_result.pending_decisions)
        assert all(pd.decision == "pending" for pd in sync_result.pending_decisions)

    def test_no_llm_calls_in_strict_mode(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="strict")
        sync_result = SyncResult()

        llm = MagicMock()
        gaps = [SpecDiff(DiffType.GAP, "auth", "Missing")]
        engine._process_gaps(gaps, llm, sync_result)

        llm.call.assert_not_called()

    def test_no_issues_created_in_strict_mode(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="strict")
        sync_result = SyncResult()

        gaps = [SpecDiff(DiffType.GAP, "auth", "Missing")]
        created = engine._process_gaps(gaps, MagicMock(), sync_result)

        assert created == 0
        from se3.engine.issue_manager import IssueManager
        mgr = IssueManager(tmp_path)
        assert len(mgr.list_issues()) == 0


# ---------------------------------------------------------------------------
# G1: Conflict handling with PendingDecision
# ---------------------------------------------------------------------------

class TestHandleConflictsStrictPendingDecision:
    def test_strict_marks_all_as_pending_decisions(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="strict")
        sync_result = SyncResult()

        conflicts = [
            Conflict(spec_name="auth", description="A", confidence="high"),
            Conflict(spec_name="config", description="B", confidence="low"),
        ]
        cr = engine._handle_conflicts_strict(conflicts, result=sync_result)

        assert len(sync_result.pending_decisions) == 2
        assert all(pd.type == "conflict" for pd in sync_result.pending_decisions)
        assert all(pd.decision == "pending" for pd in sync_result.pending_decisions)
        assert cr["call_file"] is None

    def test_strict_no_call_file_with_result(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="strict")
        sync_result = SyncResult()

        conflicts = [Conflict(spec_name="auth", description="A")]
        cr = engine._handle_conflicts_strict(conflicts, result=sync_result)

        assert cr["call_file"] is None

    def test_strict_backward_compat_without_result(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="strict")

        conflicts = [Conflict(spec_name="auth", description="A")]
        cr = engine._handle_conflicts_strict(conflicts)

        assert cr["call_file"] is not None


class TestHandleConflictsDefaultPendingDecision:
    def test_low_confidence_creates_pending_decision(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="default")
        sync_result = SyncResult()

        conflicts = [Conflict(spec_name="auth", description="A", confidence="low")]
        cr = engine._handle_conflicts_default(conflicts, MagicMock(), result=sync_result)

        assert len(sync_result.pending_decisions) == 1
        pd = sync_result.pending_decisions[0]
        assert pd.type == "conflict"
        assert pd.spec_name == "auth"
        assert pd.decision == "pending"

    def test_no_call_file_when_result_provided(self, tmp_path):
        engine = SyncEngine(tmp_path, mode="default")
        sync_result = SyncResult()

        conflicts = [Conflict(spec_name="auth", description="A", confidence="low")]
        cr = engine._handle_conflicts_default(conflicts, MagicMock(), result=sync_result)

        assert cr["call_file"] is None

    def test_high_confidence_still_auto_resolves(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth")
        engine = SyncEngine(tmp_path, mode="default")
        engine._load_specs()
        sync_result = SyncResult()

        llm = MagicMock()
        llm.call.side_effect = [
            json.dumps({"decision": "update_spec"}),
            "# Updated",
        ]

        conflicts = [Conflict(spec_name="auth", description="A", confidence="high")]
        cr = engine._handle_conflicts_default(conflicts, llm, result=sync_result)

        assert cr["specs_updated"] == 1
        assert len(sync_result.pending_decisions) == 0


# ---------------------------------------------------------------------------
# G1: _collect_pending_decisions
# ---------------------------------------------------------------------------

class TestCollectPendingDecisions:
    def test_collects_pending_from_gaps_and_conflicts(self, tmp_path):
        engine = SyncEngine(tmp_path)
        sync_result = SyncResult(
            pending_decisions=[
                PendingDecision(type="gap", spec_name="auth", description="g1", decision="pending"),
                PendingDecision(type="conflict", spec_name="cfg", description="c1", decision="pending"),
                PendingDecision(type="gap", spec_name="auth", description="g2", decision="update_spec"),
            ],
        )

        collected = engine._collect_pending_decisions(sync_result)
        assert len(collected) == 2
        assert all(pd.decision == "pending" for pd in collected)

    def test_empty_when_all_resolved(self, tmp_path):
        engine = SyncEngine(tmp_path)
        sync_result = SyncResult(
            pending_decisions=[
                PendingDecision(type="gap", decision="update_spec"),
                PendingDecision(type="conflict", decision="create_issue"),
            ],
        )

        collected = engine._collect_pending_decisions(sync_result)
        assert collected == []

    def test_empty_when_no_pending(self, tmp_path):
        engine = SyncEngine(tmp_path)
        sync_result = SyncResult()

        collected = engine._collect_pending_decisions(sync_result)
        assert collected == []
