"""Unit tests for SyncEngine — issue idempotency, auto-close, mode handling, MCP call files."""

from __future__ import annotations

import json
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
        mgr.create("[sync] auth: Missing signup", "d", tags=list(SYNC_TAGS))

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
             patch("se3.engine.project_context.ProjectContextCollector.collect",
                   return_value={"git": {}, "flow_engine": None, "backlog": [], "specs": []}), \
             patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec") as mock_analyze:

            mock_analyze.return_value = SpecAnalysis(
                spec_name="auth",
                diffs=[SpecDiff(DiffType.GAP, "auth", "Missing login")],
            )

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
