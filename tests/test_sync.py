"""Tests for SE3 Sync — data models, CLI registration, render layer, process_call_response.

This file focuses on the public surface that remained stable after the
G2/G3/G4 refactor:

* ``SpecDiff`` / ``DiffType`` / ``SpecAnalysis`` data models
* ``SyncAnalyzer`` (analyze_spec, generate_base_spec, prompt building)
* CLI ``sync_command`` parameter wiring + ``_render_loop_result`` rendering
* ``process_call_response`` (CLI wrapper around SyncEngine.process_call_response)

Engine-level run_once behaviour lives in test_sync_engine.py; loop-level
convergence behaviour lives in test_sync_loop.py.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from se3.engine.sync_engine import (
    DiffType,
    LoopResult,
    RoundResult,
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


# ---------------------------------------------------------------------------
# SyncResult / LoopResult tests
# ---------------------------------------------------------------------------

class TestLoopResult:
    def test_default_state(self):
        r = LoopResult()
        assert r.converged is False
        assert r.oscillation_detected is False
        assert r.total_specs_updated == 0
        assert r.rounds == []
        assert r.analyses == []
        assert r.all_in_sync is True

    def test_sync_result_alias_to_loop_result(self):
        assert SyncResult is LoopResult

    def test_to_dict_roundtrip(self):
        rr = RoundResult(round_index=1)
        rr.specs_updated = 2
        rr.changes_by_spec = {"auth": ["removed: stale req"]}
        rr.spec_hashes_after = {"auth": "abc"}
        loop = LoopResult(
            rounds=[rr],
            converged=True,
            total_specs_updated=2,
            final_round_index=1,
        )
        data = loop.to_dict()
        assert data["converged"] is True
        assert data["total_specs_updated"] == 2
        assert data["rounds"][0]["round_index"] == 1
        assert data["rounds"][0]["specs_updated"] == 2

    def test_analyses_flattened_from_final_round(self):
        a1 = SpecAnalysis(spec_name="auth", diffs=[])
        rr = RoundResult(round_index=1, analyses=[a1])
        loop = LoopResult(rounds=[rr])
        assert loop.analyses == [a1]


# ---------------------------------------------------------------------------
# CLI sync_cmd parameter handling
# ---------------------------------------------------------------------------

class TestSyncCLIArgs:
    def test_sync_command_imports(self):
        from se3.commands.sync import sync_command, process_call_response
        assert callable(sync_command)
        assert callable(process_call_response)

    def test_sync_cmd_default_once_false(self):
        """typer CLI exposes --once defaulting to False."""
        from se3.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, ["sync", "--help"])
        assert result.exit_code == 0
        assert "--once" in result.stdout
        assert "--max-rounds" in result.stdout
        assert "--stable-rounds" in result.stdout
        assert "--interactive" in result.stdout
        assert "--show-diff" in result.stdout
        # The removed --mode option must NOT appear in help.
        assert "--mode" not in result.stdout

    def test_sync_cmd_rejects_invalid_max_rounds(self):
        from se3.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(app, ["sync", "--max-rounds", "0"])
        assert result.exit_code != 0

    def test_sync_cmd_rejects_stable_gt_max(self):
        from se3.cli import app
        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            app, ["sync", "--max-rounds", "2", "--stable-rounds", "5"]
        )
        assert result.exit_code != 0

    def test_sync_command_calls_syncloop_run(self, tmp_path):
        from se3.commands.sync import sync_command

        with patch("se3.engine.sync_loop.SyncLoop") as MockLoop:
            MockLoop.return_value.run.return_value = LoopResult(
                converged=True, final_round_index=1
            )
            sync_command(project_root=tmp_path)
            MockLoop.return_value.run.assert_called_once()

    def test_sync_command_with_once_passes_max_rounds_1(self, tmp_path):
        """When --once is set, SyncLoop must be built with max_rounds=1.

        The CLI layer collapses --once to ``max_rounds=1, stable_rounds=1``
        before invoking sync_command.
        """
        from se3.commands.sync import sync_command

        with patch("se3.engine.sync_loop.SyncLoop") as MockLoop:
            MockLoop.return_value.run.return_value = LoopResult(
                converged=True, final_round_index=1
            )
            sync_command(
                project_root=tmp_path,
                max_rounds=1,
                stable_rounds=1,
                once=True,
            )
            kwargs = MockLoop.call_args.kwargs
            assert kwargs["max_rounds"] == 1
            assert kwargs["stable_rounds"] == 1

    def test_sync_command_forwards_interactive_flag(self, tmp_path):
        from se3.commands.sync import sync_command

        with patch("se3.engine.sync_loop.SyncLoop") as MockLoop:
            MockLoop.return_value.run.return_value = LoopResult(
                converged=True, final_round_index=1
            )
            sync_command(project_root=tmp_path, interactive=True)
            assert MockLoop.call_args.kwargs["interactive"] is True


# ---------------------------------------------------------------------------
# _render_loop_result tests
# ---------------------------------------------------------------------------

def _capture_render(result, *, show_diff=False):
    """Render the loop result into a captured StringIO and return the text.

    Rich highlighting is disabled so plain-text substring assertions work.
    """
    from se3.commands.sync import _render_loop_result

    console = Console(
        file=StringIO(),
        force_terminal=False,
        no_color=True,
        highlight=False,
        width=200,
    )
    with patch("se3.commands.sync.get_console", return_value=console):
        _render_loop_result(result, show_diff=show_diff)
    return console.file.getvalue()


class TestRenderLoopResult:
    def test_converged_summary(self):
        rr = RoundResult(round_index=2)
        rr.specs_updated = 0
        loop = LoopResult(
            rounds=[RoundResult(round_index=1), rr],
            converged=True,
            total_specs_updated=3,
            final_round_index=2,
        )
        out = _capture_render(loop)
        assert "Converged after 2 round(s)" in out
        assert "Total 3 spec(s) updated" in out
        assert "Final round: 0 change(s)" in out

    def test_max_rounds_exhausted_summary(self):
        rr = RoundResult(round_index=10)
        rr.specs_updated = 2
        loop = LoopResult(
            rounds=[rr],
            converged=False,
            total_specs_updated=20,
            final_round_index=10,
        )
        out = _capture_render(loop)
        assert "Did not converge within 10 round(s)" in out

    def test_oscillation_summary(self):
        rr = RoundResult(round_index=4)
        rr.specs_updated = 1
        loop = LoopResult(
            rounds=[rr],
            oscillation_detected=True,
            oscillation_report="Spec 'auth' oscillates with period=2",
            total_specs_updated=5,
            final_round_index=4,
        )
        out = _capture_render(loop)
        assert "Oscillation detected" in out
        assert "aborted" in out.lower()
        assert "period=2" in out

    def test_disclaimer_present_on_convergence(self):
        loop = LoopResult(
            rounds=[RoundResult(round_index=1)],
            converged=True,
            final_round_index=1,
        )
        out = _capture_render(loop)
        # The exact disclaimer string MUST be present verbatim so callers can
        # match it.
        assert (
            "Convergence means the LLM found no further drift in the last "
            "round; it does not guarantee absolute spec-code consistency."
            in out
        )

    def test_disclaimer_present_on_max_rounds(self):
        loop = LoopResult(
            rounds=[RoundResult(round_index=3)],
            converged=False,
            final_round_index=3,
        )
        out = _capture_render(loop)
        assert (
            "it does not guarantee absolute spec-code consistency."
            in out
        )

    def test_disclaimer_present_on_oscillation(self):
        loop = LoopResult(
            rounds=[RoundResult(round_index=4)],
            oscillation_detected=True,
            oscillation_report="x",
            final_round_index=4,
        )
        out = _capture_render(loop)
        assert (
            "it does not guarantee absolute spec-code consistency."
            in out
        )

    def test_show_diff_renders_per_round_changes(self):
        rr1 = RoundResult(round_index=1)
        rr1.changes_by_spec = {"auth": ["removed: stale requirement"]}
        rr1.specs_updated = 1
        rr2 = RoundResult(round_index=2)
        rr2.changes_by_spec = {"scaffold": ["added: new directory rule"]}
        rr2.specs_updated = 1
        loop = LoopResult(
            rounds=[rr1, rr2],
            converged=True,
            total_specs_updated=2,
            final_round_index=2,
        )

        out_no_diff = _capture_render(loop, show_diff=False)
        assert "Per-Round Changes" not in out_no_diff

        out_with_diff = _capture_render(loop, show_diff=True)
        assert "Per-Round Changes" in out_with_diff
        assert "Round 1" in out_with_diff
        assert "Round 2" in out_with_diff
        assert "auth" in out_with_diff
        assert "scaffold" in out_with_diff

    def test_show_diff_omitted_when_no_changes(self):
        loop = LoopResult(
            rounds=[RoundResult(round_index=1)],
            converged=True,
            final_round_index=1,
        )
        out = _capture_render(loop, show_diff=True)
        assert "Per-Round Changes" not in out

    def test_new_specs_displayed(self):
        loop = LoopResult(
            rounds=[RoundResult(round_index=1)],
            converged=True,
            total_specs_created=["new-mod"],
            final_round_index=1,
        )
        out = _capture_render(loop)
        assert "new-mod" in out


# ---------------------------------------------------------------------------
# process_call_response tests
# ---------------------------------------------------------------------------

class TestSyncCLIProcessCallResponse:
    def test_imports(self):
        from se3.commands.sync import process_call_response
        assert callable(process_call_response)

    def test_missing_call_file(self, tmp_path):
        from se3.commands.sync import process_call_response

        # Must not raise; just prints error.
        process_call_response(
            call_file=tmp_path / "nonexistent.json",
            project_root=tmp_path,
        )

    def test_missing_response_file(self, tmp_path):
        from se3.commands.sync import process_call_response

        call_file = tmp_path / "call.json"
        call_file.write_text(
            json.dumps({"type": "sync_high_impact_deletion", "items": []}),
            encoding="utf-8",
        )

        # Must not raise; just prints error.
        process_call_response(
            call_file=call_file,
            project_root=tmp_path,
        )

    def test_legacy_call_file_rejected(self, tmp_path):
        """A call file with a legacy type emits a clear unsupported error."""
        from se3.commands.sync import process_call_response

        call_file = tmp_path / "legacy_call.json"
        call_file.write_text(
            json.dumps({"type": "sync_conflicts", "conflicts": []}),
            encoding="utf-8",
        )
        response = tmp_path / "legacy_call.json.response"
        response.write_text(json.dumps({"items": []}), encoding="utf-8")

        # Capture rendered output by patching render_text.
        captured = []
        with patch(
            "se3.commands.sync.render_text",
            side_effect=lambda msg, **kw: captured.append(msg),
        ):
            process_call_response(call_file=call_file, project_root=tmp_path)

        assert any("Unsupported call file" in c for c in captured)

    def test_modern_call_file_routes_to_engine(self, tmp_path):
        """A sync_high_impact_deletion call file is forwarded to SyncEngine."""
        from se3.commands.sync import process_call_response

        call_file = tmp_path / "del_call.json"
        call_file.write_text(
            json.dumps({"type": "sync_high_impact_deletion", "items": []}),
            encoding="utf-8",
        )
        response = tmp_path / "del_call.json.response"
        response.write_text(json.dumps({"items": []}), encoding="utf-8")

        with patch(
            "se3.engine.sync_engine.SyncEngine.process_call_response",
            return_value={"specs_updated": 2, "skipped": 1},
        ) as mock_proc:
            captured = []
            with patch(
                "se3.commands.sync.render_text",
                side_effect=lambda msg, **kw: captured.append(msg),
            ):
                process_call_response(call_file=call_file, project_root=tmp_path)

            mock_proc.assert_called_once()

        # Output must mention both updated and skipped counts.
        assert any("Specs updated: 2" in c and "Skipped: 1" in c for c in captured)


# ---------------------------------------------------------------------------
# SyncAnalyzer tests (the analyzer's surface is unchanged)
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

    def test_parse_invalid_json(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        result = analyzer._parse_analysis_response("spec", "not json at all")
        assert len(result.diffs) == 1
        assert result.diffs[0].diff_type == DiffType.CONFLICT
        assert "JSON parse error" in result.diffs[0].description


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

    def test_uses_extract_json_mode(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = json.dumps({"diffs": []})
        analyzer = SyncAnalyzer(tmp_path, caller)
        analyzer.analyze_spec("x", "spec", "ctx")

        call_kwargs = caller.call.call_args
        assert call_kwargs.kwargs.get("json_mode") == "extract"

    def test_returns_error_analysis_after_max_retries(self, tmp_path):
        from se3.engine.llm_caller import LLMCallError

        caller = MagicMock()
        caller.call.side_effect = LLMCallError("persistent failure")
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec("broken", "spec", "ctx")

        assert caller.call.call_count == 3
        assert len(result.diffs) == 1
        assert result.diffs[0].diff_type == DiffType.CONFLICT


class TestGenerateBaseSpec:
    def test_generates_and_writes_base_spec(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = "# My Project — Base Specification"
        analyzer = SyncAnalyzer(tmp_path, caller)
        content = analyzer.generate_base_spec("project context here")

        assert "Base Specification" in content
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        assert spec_path.exists()
        assert spec_path.read_text(encoding="utf-8") == content

    def test_uses_off_json_mode(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = "# Spec"
        analyzer = SyncAnalyzer(tmp_path, caller)
        analyzer.generate_base_spec("ctx")

        call_kwargs = caller.call.call_args
        assert call_kwargs.kwargs.get("json_mode") == "off"

    def test_raises_on_llm_failure(self, tmp_path):
        from se3.engine.llm_caller import LLMCallError

        caller = MagicMock()
        caller.call.side_effect = LLMCallError("timeout")
        analyzer = SyncAnalyzer(tmp_path, caller)

        with pytest.raises(LLMCallError):
            analyzer.generate_base_spec("ctx")
