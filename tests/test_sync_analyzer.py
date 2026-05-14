"""Unit tests for SyncAnalyzer — prompt construction, JSON parsing, diff classification, retry handling."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from se3.engine.llm_caller import LLMCallError
from se3.engine.sync_analyzer import SyncAnalyzer, _ANALYSIS_PROMPT_TEMPLATE, _BASE_SPEC_PROMPT_TEMPLATE
from se3.engine.sync_engine import DiffType, SpecAnalysis, SpecDiff


# ---------------------------------------------------------------------------
# Prompt construction tests
# ---------------------------------------------------------------------------

class TestBuildAnalysisPrompt:
    def _build(self, tmp_path, spec_name="auth", spec_content="spec", context="ctx"):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        return analyzer._build_analysis_prompt(spec_name, spec_content, context)

    def test_contains_spec_name(self, tmp_path):
        prompt = self._build(tmp_path, spec_name="my-feature")
        assert "my-feature" in prompt

    def test_contains_spec_content(self, tmp_path):
        prompt = self._build(tmp_path, spec_content="SHALL validate all inputs")
        assert "SHALL validate all inputs" in prompt

    def test_contains_project_context(self, tmp_path):
        prompt = self._build(tmp_path, context="project tree: src/ tests/ config.yaml")
        assert "project tree: src/ tests/ config.yaml" in prompt

    def test_defines_gap_type(self, tmp_path):
        prompt = self._build(tmp_path)
        assert "gap" in prompt.lower()
        assert "NOT implemented" in prompt

    def test_defines_extension_type(self, tmp_path):
        prompt = self._build(tmp_path)
        assert "extension" in prompt.lower()
        assert "NOT describe" in prompt

    def test_defines_conflict_type(self, tmp_path):
        prompt = self._build(tmp_path)
        assert "conflict" in prompt.lower()
        assert "DIFFERENTLY" in prompt

    def test_includes_json_schema_structure(self, tmp_path):
        prompt = self._build(tmp_path)
        assert '"diffs"' in prompt
        assert '"type"' in prompt
        assert '"description"' in prompt
        assert '"code_location"' in prompt
        assert '"confidence"' in prompt

    def test_mentions_confidence_levels(self, tmp_path):
        prompt = self._build(tmp_path)
        assert '"high"' in prompt
        assert '"low"' in prompt

    def test_declares_one_directional_sync_semantics(self, tmp_path):
        """The prompt MUST tell the LLM that this analysis drives a
        one-directional spec update (code → spec only) so that
        not-yet-built features in the spec are not flagged as gaps —
        they get removed instead. See sync philosophy: spec is a
        snapshot of code, not a forward-looking commitment."""
        prompt = self._build(tmp_path)
        lowered = prompt.lower()
        assert "one-directional" in lowered
        assert "one-directional spec update" in lowered
        # And the prompt must explicitly redirect future intent to issues.
        assert "issue" in lowered

    def test_special_characters_in_spec_content(self, tmp_path):
        prompt = self._build(tmp_path, spec_content='Code uses {braces} and "quotes"')
        assert '{braces}' in prompt
        assert '"quotes"' in prompt

    def test_multiline_spec_content(self, tmp_path):
        content = "# Heading\n\n## Section\n\n- Item 1\n- Item 2"
        prompt = self._build(tmp_path, spec_content=content)
        assert "# Heading" in prompt
        assert "- Item 2" in prompt


# ---------------------------------------------------------------------------
# Response parsing tests — three diff types
# ---------------------------------------------------------------------------

class TestParseAnalysisResponse:
    def _parse(self, tmp_path, spec_name, response_dict):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        return analyzer._parse_analysis_response(spec_name, json.dumps(response_dict))

    def test_gap_classification(self, tmp_path):
        result = self._parse(tmp_path, "auth", {
            "diffs": [{"type": "gap", "description": "Missing login", "code_location": "src/auth.py"}]
        })
        assert len(result.diffs) == 1
        assert result.diffs[0].diff_type == DiffType.GAP
        assert result.diffs[0].description == "Missing login"
        assert result.diffs[0].code_location == "src/auth.py"
        assert result.diffs[0].confidence == ""

    def test_extension_classification(self, tmp_path):
        result = self._parse(tmp_path, "base", {
            "diffs": [{"type": "extension", "description": "Helper function added"}]
        })
        assert len(result.diffs) == 1
        assert result.diffs[0].diff_type == DiffType.EXTENSION
        assert result.diffs[0].confidence == ""

    def test_conflict_classification_with_confidence(self, tmp_path):
        result = self._parse(tmp_path, "auth", {
            "diffs": [{"type": "conflict", "description": "Token format", "confidence": "high"}]
        })
        assert len(result.diffs) == 1
        assert result.diffs[0].diff_type == DiffType.CONFLICT
        assert result.diffs[0].confidence == "high"

    def test_conflict_defaults_confidence_to_low(self, tmp_path):
        result = self._parse(tmp_path, "auth", {
            "diffs": [{"type": "conflict", "description": "Token format"}]
        })
        assert result.diffs[0].confidence == "low"

    def test_mixed_diff_types(self, tmp_path):
        result = self._parse(tmp_path, "spec", {
            "diffs": [
                {"type": "gap", "description": "g1"},
                {"type": "extension", "description": "e1"},
                {"type": "conflict", "description": "c1", "confidence": "high"},
                {"type": "gap", "description": "g2"},
            ]
        })
        assert len(result.gaps) == 2
        assert len(result.extensions) == 1
        assert len(result.conflicts) == 1

    def test_empty_diffs_array(self, tmp_path):
        result = self._parse(tmp_path, "clean", {"diffs": []})
        assert result.is_in_sync
        assert result.diffs == []

    def test_skips_unknown_diff_type(self, tmp_path):
        result = self._parse(tmp_path, "spec", {
            "diffs": [
                {"type": "gap", "description": "valid"},
                {"type": "improvement", "description": "unknown type"},
                {"type": "extension", "description": "also valid"},
            ]
        })
        assert len(result.diffs) == 2
        types = {d.diff_type for d in result.diffs}
        assert types == {DiffType.GAP, DiffType.EXTENSION}

    def test_missing_diffs_key_returns_empty(self, tmp_path):
        result = self._parse(tmp_path, "spec", {"analysis": "no diffs key"})
        assert result.is_in_sync

    def test_spec_name_preserved(self, tmp_path):
        result = self._parse(tmp_path, "my-custom-spec", {"diffs": []})
        assert result.spec_name == "my-custom-spec"

    def test_defaults_missing_fields(self, tmp_path):
        result = self._parse(tmp_path, "spec", {
            "diffs": [{"type": "gap"}]
        })
        assert result.diffs[0].description == ""
        assert result.diffs[0].code_location == ""


class TestParseInvalidJson:
    def test_invalid_json_returns_no_diffs_with_format_reason(self, tmp_path):
        # G4: malformed JSON no longer fabricates a CONFLICT diff. The
        # analysis is marked as failed with `llm_output_format_error` and
        # the diffs list stays empty so it does not poison convergence.
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        result = analyzer._parse_analysis_response("spec", "not valid json {{{")
        assert result.diffs == []
        assert result.failed_analysis_reason == "llm_output_format_error"
        assert result.analysis_failed is True
        assert result.is_in_sync is True

    def test_invalid_json_preserves_spec_name(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        result = analyzer._parse_analysis_response("my-spec", "bad json")
        assert result.spec_name == "my-spec"

    def test_truncated_json(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        result = analyzer._parse_analysis_response("spec", '{"diffs": [{"type": "gap"')
        assert result.diffs == []
        assert result.failed_analysis_reason == "llm_output_format_error"


# ---------------------------------------------------------------------------
# analyze_spec — LLM call + retry logic
# ---------------------------------------------------------------------------

class TestAnalyzeSpec:
    def test_successful_first_attempt(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = json.dumps({
            "diffs": [{"type": "gap", "description": "Missing X"}]
        })
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec("auth", "spec", "ctx")

        assert result.spec_name == "auth"
        assert len(result.diffs) == 1
        assert result.diffs[0].diff_type == DiffType.GAP
        caller.call.assert_called_once()

    def test_passes_extract_json_mode(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = json.dumps({"diffs": []})
        analyzer = SyncAnalyzer(tmp_path, caller)
        analyzer.analyze_spec("x", "s", "c")

        assert caller.call.call_args.kwargs["json_mode"] == "extract"

    def test_passes_json_schema_hint(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = json.dumps({"diffs": []})
        analyzer = SyncAnalyzer(tmp_path, caller)
        analyzer.analyze_spec("x", "s", "c")

        hint = caller.call.call_args.kwargs.get("json_schema_hint", "")
        assert '"diffs"' in hint

    def test_retry_on_llm_call_error(self, tmp_path):
        caller = MagicMock()
        caller.call.side_effect = [
            LLMCallError("rate limited"),
            json.dumps({"diffs": [{"type": "extension", "description": "extra"}]}),
        ]
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec("auth", "s", "c")

        assert caller.call.call_count == 2
        assert len(result.diffs) == 1
        assert result.diffs[0].diff_type == DiffType.EXTENSION

    def test_retry_on_generic_exception(self, tmp_path):
        caller = MagicMock()
        caller.call.side_effect = [
            RuntimeError("network error"),
            json.dumps({"diffs": []}),
        ]
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec("x", "s", "c")

        assert caller.call.call_count == 2
        assert result.is_in_sync

    def test_retry_twice_then_succeed(self, tmp_path):
        caller = MagicMock()
        caller.call.side_effect = [
            LLMCallError("fail 1"),
            RuntimeError("fail 2"),
            json.dumps({"diffs": [{"type": "gap", "description": "found"}]}),
        ]
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec("x", "s", "c")

        assert caller.call.call_count == 3
        assert len(result.diffs) == 1
        assert result.diffs[0].diff_type == DiffType.GAP

    def test_all_retries_exhausted_returns_error(self, tmp_path):
        # G4: exhausted retries no longer fabricate a CONFLICT. The
        # spec is marked failed with the `infrastructure_failure` reason
        # so the sync loop does not interpret it as real drift.
        caller = MagicMock()
        caller.call.side_effect = LLMCallError("persistent failure")
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec("broken", "s", "c")

        assert caller.call.call_count == 3
        assert result.diffs == []
        assert result.failed_analysis_reason == "infrastructure_failure"
        assert result.analysis_failed is True

    def test_error_analysis_has_correct_spec_name(self, tmp_path):
        caller = MagicMock()
        caller.call.side_effect = LLMCallError("fail")
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec("my-spec", "s", "c")

        assert result.spec_name == "my-spec"
        assert result.failed_analysis_reason == "infrastructure_failure"

    def test_prompt_includes_spec_content_and_context(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = json.dumps({"diffs": []})
        analyzer = SyncAnalyzer(tmp_path, caller)
        analyzer.analyze_spec("auth", "SHALL authenticate users", "project: flask app")

        prompt = caller.call.call_args.kwargs["prompt"]
        assert "SHALL authenticate users" in prompt
        assert "project: flask app" in prompt
        assert "auth" in prompt


# ---------------------------------------------------------------------------
# Base spec generation
# ---------------------------------------------------------------------------

class TestGenerateBaseSpec:
    def test_generates_and_writes_file(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = "# Project — Base Specification\n\n## Purpose\nA test."
        analyzer = SyncAnalyzer(tmp_path, caller)
        content = analyzer.generate_base_spec("context data")

        assert "Base Specification" in content
        spec_path = tmp_path / "se3" / "specs" / "base" / "spec.md"
        assert spec_path.exists()
        assert spec_path.read_text(encoding="utf-8") == content

    def test_creates_directory_structure(self, tmp_path):
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

        assert caller.call.call_args.kwargs["json_mode"] == "off"

    def test_prompt_includes_project_context(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = "# Spec"
        analyzer = SyncAnalyzer(tmp_path, caller)
        analyzer.generate_base_spec("my detailed context")

        prompt = caller.call.call_args.kwargs.get("prompt") or caller.call.call_args[0][0]
        assert "my detailed context" in prompt

    def test_strips_response_whitespace(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = "\n  # Content  \n\n"
        analyzer = SyncAnalyzer(tmp_path, caller)
        content = analyzer.generate_base_spec("ctx")

        assert content == "# Content"
        assert (tmp_path / "se3" / "specs" / "base" / "spec.md").read_text(encoding="utf-8") == "# Content"

    def test_overwrites_existing_base_spec(self, tmp_path):
        base_dir = tmp_path / "se3" / "specs" / "base"
        base_dir.mkdir(parents=True)
        (base_dir / "spec.md").write_text("old content")

        caller = MagicMock()
        caller.call.return_value = "# New Content"
        analyzer = SyncAnalyzer(tmp_path, caller)
        content = analyzer.generate_base_spec("ctx")

        assert content == "# New Content"
        assert (base_dir / "spec.md").read_text() == "# New Content"

    def test_raises_on_llm_failure(self, tmp_path):
        caller = MagicMock()
        caller.call.side_effect = LLMCallError("timeout")
        analyzer = SyncAnalyzer(tmp_path, caller)

        with pytest.raises(LLMCallError):
            analyzer.generate_base_spec("ctx")

    def test_no_base_spec_file_created_on_failure(self, tmp_path):
        caller = MagicMock()
        caller.call.side_effect = LLMCallError("fail")
        analyzer = SyncAnalyzer(tmp_path, caller)

        with pytest.raises(LLMCallError):
            analyzer.generate_base_spec("ctx")

        assert not (tmp_path / "se3" / "specs" / "base" / "spec.md").exists()

    def test_strips_markdown_fences_from_response(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = "```markdown\n# Project — Base Specification\n\n## Purpose\nA test.\n```"
        analyzer = SyncAnalyzer(tmp_path, caller)
        content = analyzer.generate_base_spec("ctx")

        assert not content.startswith("```")
        assert not content.endswith("```")
        assert content == "# Project — Base Specification\n\n## Purpose\nA test."
        written = (tmp_path / "se3" / "specs" / "base" / "spec.md").read_text(encoding="utf-8")
        assert written == content

    def test_no_fences_passthrough(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = "# Plain Spec\n\n## Purpose\nNo fences."
        analyzer = SyncAnalyzer(tmp_path, caller)
        content = analyzer.generate_base_spec("ctx")

        assert content == "# Plain Spec\n\n## Purpose\nNo fences."


# ---------------------------------------------------------------------------
# G1: Prompt code browsing instructions
# ---------------------------------------------------------------------------

class TestPromptCodeBrowsingInstructions:
    def _build(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        return analyzer._build_analysis_prompt("auth", "spec", "ctx")

    def test_prompt_contains_read_tool_instruction(self, tmp_path):
        prompt = self._build(tmp_path)
        assert "Read" in prompt

    def test_prompt_contains_grep_tool_instruction(self, tmp_path):
        prompt = self._build(tmp_path)
        assert "Grep" in prompt

    def test_prompt_contains_glob_tool_instruction(self, tmp_path):
        prompt = self._build(tmp_path)
        assert "Glob" in prompt

    def test_prompt_instructs_active_code_reading(self, tmp_path):
        prompt = self._build(tmp_path)
        assert "source code" in prompt.lower()

    def test_prompt_instructs_verification(self, tmp_path):
        prompt = self._build(tmp_path)
        assert "verify" in prompt.lower()

    def test_prompt_warns_against_assumptions(self, tmp_path):
        prompt = self._build(tmp_path)
        assert "assumption" in prompt.lower() or "assume" in prompt.lower()

    def test_json_schema_unchanged(self, tmp_path):
        prompt = self._build(tmp_path)
        assert '"diffs"' in prompt
        assert '"type"' in prompt
        assert '"confidence"' in prompt

    def test_analyze_spec_interface_unchanged(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = json.dumps({"diffs": []})
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec("auth", "spec content", "project context")
        assert result.spec_name == "auth"
        assert result.is_in_sync
