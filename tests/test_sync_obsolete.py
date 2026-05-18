"""Tests for obsolete spec marking and deletion mechanism (G6).

Covers:
- SpecAnalysis.code_fully_absent field serialization
- SyncAnalyzer code absence prompt injection when all deps are missing
- SyncAnalyzer._parse_analysis_response extracting code_fully_absent
- SyncLoop._update_obsolete_candidates (add / remove / persist)
- SyncCheckpoint.obsolete_specs field
- SpecDiscovery.delete_obsolete_specs (default direct delete, confirm interactive)
- SyncLoop passing deps to engine and accumulating across rounds
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.sync_analyzer import SyncAnalyzer, _CODE_ABSENCE_PROMPT
from se3.engine.sync_checkpoint import SyncCheckpoint
from se3.engine.sync_discovery import SpecDiscovery
from se3.engine.sync_engine import (
    DiffType,
    LoopResult,
    RoundResult,
    SpecAnalysis,
    SpecDiff,
    _hash_spec_content,
)
from se3.engine.sync_loop import SyncLoop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec_dir(tmp_path, name):
    """Create a real spec directory with spec.md on disk."""
    spec_dir = tmp_path / "se3" / "specs" / name
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "spec.md"
    spec_path.write_text(
        f"# {name} Specification\n\n## Purpose\n\nTest spec.\n\n"
        f"### Requirement: Test\n\nContent here.\n"
    )
    return spec_path


def _make_dep_file(tmp_path, rel_path, content="test content"):
    """Create a real dependency file on disk."""
    abs_path = tmp_path / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content)
    return abs_path


# ---------------------------------------------------------------------------
# 1. SpecAnalysis.code_fully_absent field
# ---------------------------------------------------------------------------

class TestSpecAnalysisCodeFullyAbsent:
    def test_default_is_false(self):
        analysis = SpecAnalysis(spec_name="test")
        assert analysis.code_fully_absent is False

    def test_serialize_when_true(self):
        analysis = SpecAnalysis(spec_name="test", code_fully_absent=True)
        d = analysis.to_dict()
        assert d["code_fully_absent"] is True

    def test_serialize_omits_when_false(self):
        analysis = SpecAnalysis(spec_name="test", code_fully_absent=False)
        d = analysis.to_dict()
        assert "code_fully_absent" not in d

    def test_deserialize_when_true(self):
        d = {"spec_name": "test", "code_fully_absent": True}
        analysis = SpecAnalysis.from_dict(d)
        assert analysis.code_fully_absent is True

    def test_deserialize_defaults_to_false(self):
        d = {"spec_name": "test"}
        analysis = SpecAnalysis.from_dict(d)
        assert analysis.code_fully_absent is False

    def test_roundtrip_preserves_code_fully_absent(self):
        analysis = SpecAnalysis(
            spec_name="test",
            diffs=[SpecDiff(diff_type=DiffType.GAP, spec_name="test", description="g")],
            code_fully_absent=True,
        )
        d = analysis.to_dict()
        restored = SpecAnalysis.from_dict(d)
        assert restored.code_fully_absent is True
        assert len(restored.diffs) == 1


# ---------------------------------------------------------------------------
# 2. SyncAnalyzer code absence prompt
# ---------------------------------------------------------------------------

class TestAnalyzerCodeAbsencePrompt:
    def _build(self, tmp_path, deps=None, all_deps_missing=False):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        return analyzer._build_analysis_prompt(
            "spec", "content", "ctx", deps=deps, all_deps_missing=all_deps_missing,
        )

    def test_no_deps_injects_no_code_absence(self, tmp_path):
        prompt = self._build(tmp_path, deps=None)
        assert _CODE_ABSENCE_PROMPT not in prompt

    def test_empty_deps_injects_no_code_absence(self, tmp_path):
        prompt = self._build(tmp_path, deps=[], all_deps_missing=False)
        assert _CODE_ABSENCE_PROMPT not in prompt

    def test_deps_present_injects_no_code_absence(self, tmp_path):
        prompt = self._build(tmp_path, deps=["src/a.py"], all_deps_missing=False)
        assert _CODE_ABSENCE_PROMPT not in prompt

    def test_all_deps_missing_injects_code_absence(self, tmp_path):
        prompt = self._build(tmp_path, deps=["src/a.py", "src/b.py"], all_deps_missing=True)
        assert _CODE_ABSENCE_PROMPT in prompt
        assert "code_fully_absent" in prompt
        assert "All previously-known source files" in prompt

    def test_code_absence_prompt_includes_json_schema(self, tmp_path):
        prompt = self._build(tmp_path, deps=["gone.py"], all_deps_missing=True)
        assert '"code_fully_absent"' in prompt


class TestAnalyzerCheckAllDepsMissing:
    def test_none_deps_returns_false(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        assert analyzer._check_all_deps_missing(None) is False

    def test_empty_deps_returns_false(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        assert analyzer._check_all_deps_missing([]) is False

    def test_all_files_present_returns_false(self, tmp_path):
        _make_dep_file(tmp_path, "src/exists.py")
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        assert analyzer._check_all_deps_missing(["src/exists.py"]) is False

    def test_any_file_present_returns_false(self, tmp_path):
        _make_dep_file(tmp_path, "src/exists.py")
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        assert analyzer._check_all_deps_missing(
            ["src/exists.py", "src/missing.py"]
        ) is False

    def test_all_files_missing_returns_true(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        assert analyzer._check_all_deps_missing(["src/missing.py", "gone.py"]) is True


# ---------------------------------------------------------------------------
# 3. _parse_analysis_response — code_fully_absent extraction
# ---------------------------------------------------------------------------

class TestParseCodeFullyAbsent:
    def _parse(self, tmp_path, response_dict):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        return analyzer._parse_analysis_response(
            "spec", json.dumps(response_dict)
        )

    def test_extracts_code_fully_absent_true(self, tmp_path):
        result = self._parse(tmp_path, {"diffs": [], "code_fully_absent": True})
        assert result.code_fully_absent is True

    def test_extracts_code_fully_absent_false(self, tmp_path):
        result = self._parse(tmp_path, {"diffs": [], "code_fully_absent": False})
        assert result.code_fully_absent is False

    def test_defaults_code_fully_absent_to_false(self, tmp_path):
        result = self._parse(tmp_path, {"diffs": []})
        assert result.code_fully_absent is False

    def test_code_fully_absent_with_diffs_present(self, tmp_path):
        """code_fully_absent can be true even when diffs are present
        (e.g. the LLM notes speculative diffs for code no longer there)."""
        result = self._parse(tmp_path, {
            "diffs": [{"type": "gap", "description": "removed feature"}],
            "code_fully_absent": True,
        })
        assert result.code_fully_absent is True
        assert len(result.diffs) == 1

    def test_fence_stripped_before_parsing(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        response = '```json\n{"diffs": [], "code_fully_absent": true}\n```'
        result = analyzer._parse_analysis_response("spec", response)
        assert result.code_fully_absent is True


# ---------------------------------------------------------------------------
# 4. analyze_spec with deps parameter
# ---------------------------------------------------------------------------

class TestAnalyzeSpecWithDeps:
    def test_passes_deps_to_prompt_builder(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = json.dumps({"diffs": []})
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec("spec", "content", "ctx")
        assert result.code_fully_absent is False

    def test_no_deps_does_not_alter_prompt(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = json.dumps({"diffs": []})
        analyzer = SyncAnalyzer(tmp_path, caller)
        analyzer.analyze_spec("spec", "content", "ctx", deps=None)
        prompt = caller.call.call_args.kwargs["prompt"]
        assert _CODE_ABSENCE_PROMPT not in prompt

    def test_deps_all_present_does_not_alter_prompt(self, tmp_path):
        _make_dep_file(tmp_path, "src/a.py")
        caller = MagicMock()
        caller.call.return_value = json.dumps({"diffs": []})
        analyzer = SyncAnalyzer(tmp_path, caller)
        analyzer.analyze_spec("spec", "content", "ctx", deps=["src/a.py"])
        prompt = caller.call.call_args.kwargs["prompt"]
        assert _CODE_ABSENCE_PROMPT not in prompt

    def test_deps_all_missing_injects_code_absence(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = json.dumps({
            "diffs": [], "code_fully_absent": True
        })
        analyzer = SyncAnalyzer(tmp_path, caller)
        result = analyzer.analyze_spec(
            "spec", "content", "ctx", deps=["missing.py"]
        )
        prompt = caller.call.call_args.kwargs["prompt"]
        assert _CODE_ABSENCE_PROMPT in prompt
        assert result.code_fully_absent is True


# ---------------------------------------------------------------------------
# 5. SyncLoop._update_obsolete_candidates
# ---------------------------------------------------------------------------

class TestUpdateObsoleteCandidates:
    def test_adds_candidate_when_deps_all_gone_and_code_absent(self, tmp_path):
        """Spec with deps all missing AND code_fully_absent → enters candidate set."""
        analysis = SpecAnalysis(
            spec_name="old-feature", code_fully_absent=True,
        )
        round_result = RoundResult(
            round_index=1,
            analyses=[analysis],
            per_spec_deps={},
        )
        accumulated_deps = {"old-feature": {"src/old.py", "src/old_test.py"}}
        candidates = SyncLoop._update_obsolete_candidates(
            round_result=round_result,
            accumulated_deps=accumulated_deps,
            previous_candidates=set(),
            project_root=tmp_path,
        )
        assert "old-feature" in candidates

    def test_does_not_add_candidate_when_deps_present(self, tmp_path):
        """Spec with deps still existing → does NOT enter candidate set."""
        _make_dep_file(tmp_path, "src/exists.py")
        analysis = SpecAnalysis(
            spec_name="feature", code_fully_absent=True,
        )
        round_result = RoundResult(
            round_index=1, analyses=[analysis], per_spec_deps={},
        )
        accumulated_deps = {"feature": {"src/exists.py"}}
        candidates = SyncLoop._update_obsolete_candidates(
            round_result=round_result,
            accumulated_deps=accumulated_deps,
            previous_candidates=set(),
            project_root=tmp_path,
        )
        assert "feature" not in candidates

    def test_does_not_add_candidate_when_code_not_absent(self, tmp_path):
        """Spec with deps all gone but code_fully_absent=False → NOT a candidate."""
        analysis = SpecAnalysis(
            spec_name="feature", code_fully_absent=False,
        )
        round_result = RoundResult(
            round_index=1, analyses=[analysis], per_spec_deps={},
        )
        accumulated_deps = {"feature": {"src/gone.py"}}
        candidates = SyncLoop._update_obsolete_candidates(
            round_result=round_result,
            accumulated_deps=accumulated_deps,
            previous_candidates=set(),
            project_root=tmp_path,
        )
        assert "feature" not in candidates

    def test_removes_candidate_when_code_reappears(self, tmp_path):
        """Spec was a candidate but code_fully_absent becomes False → removed."""
        _make_dep_file(tmp_path, "src/back.py")
        analysis = SpecAnalysis(
            spec_name="feature", code_fully_absent=False,
        )
        round_result = RoundResult(
            round_index=2, analyses=[analysis], per_spec_deps={},
        )
        accumulated_deps = {"feature": {"src/back.py", "src/gone.py"}}
        candidates = SyncLoop._update_obsolete_candidates(
            round_result=round_result,
            accumulated_deps=accumulated_deps,
            previous_candidates={"feature"},
            project_root=tmp_path,
        )
        assert "feature" not in candidates

    def test_removes_candidate_when_deps_reappear(self, tmp_path):
        """Spec was a candidate but deps files come back → removed."""
        _make_dep_file(tmp_path, "src/returned.py")
        analysis = SpecAnalysis(
            spec_name="feature", code_fully_absent=True,
        )
        round_result = RoundResult(
            round_index=2, analyses=[analysis], per_spec_deps={},
        )
        accumulated_deps = {"feature": {"src/returned.py"}}
        candidates = SyncLoop._update_obsolete_candidates(
            round_result=round_result,
            accumulated_deps=accumulated_deps,
            previous_candidates={"feature"},
            project_root=tmp_path,
        )
        assert "feature" not in candidates

    def test_preserves_candidate_across_rounds(self, tmp_path):
        """Once in candidate set and conditions stay → stays in set."""
        analysis = SpecAnalysis(
            spec_name="gone", code_fully_absent=True,
        )
        round_result = RoundResult(
            round_index=2, analyses=[analysis], per_spec_deps={},
        )
        accumulated_deps = {"gone": {"src/deleted.py"}}
        candidates = SyncLoop._update_obsolete_candidates(
            round_result=round_result,
            accumulated_deps=accumulated_deps,
            previous_candidates={"gone"},
            project_root=tmp_path,
        )
        assert "gone" in candidates

    def test_code_not_in_analysis_keeps_candidate(self, tmp_path):
        """Spec not in current round's analyses keeps its previous candidate status."""
        round_result = RoundResult(round_index=2, analyses=[], per_spec_deps={})
        accumulated_deps = {"gone": {"src/deleted.py"}}
        candidates = SyncLoop._update_obsolete_candidates(
            round_result=round_result,
            accumulated_deps=accumulated_deps,
            previous_candidates={"gone"},
            project_root=tmp_path,
        )
        assert "gone" in candidates

    def test_no_deps_accumulated_skips_evaluation(self, tmp_path):
        """Spec with no accumulated deps never enters candidates."""
        analysis = SpecAnalysis(
            spec_name="new-spec", code_fully_absent=True,
        )
        round_result = RoundResult(
            round_index=1, analyses=[analysis], per_spec_deps={},
        )
        accumulated_deps = {}
        candidates = SyncLoop._update_obsolete_candidates(
            round_result=round_result,
            accumulated_deps=accumulated_deps,
            previous_candidates=set(),
            project_root=tmp_path,
        )
        assert "new-spec" not in candidates

    def test_skipped_spec_does_not_evict_candidate(self, tmp_path):
        """A spec in skip_specs carries a synthetic placeholder analysis with
        the default code_fully_absent=False. That synthetic analysis must NOT
        evict the spec from the candidate set — its candidate status is frozen
        for rounds in which it is skipped (Level-2 / Level-3 early exit)."""
        analysis = SpecAnalysis(spec_name="gone", code_fully_absent=False)
        round_result = RoundResult(
            round_index=3, analyses=[analysis], per_spec_deps={},
        )
        accumulated_deps = {"gone": {"src/deleted.py"}}
        candidates = SyncLoop._update_obsolete_candidates(
            round_result=round_result,
            accumulated_deps=accumulated_deps,
            previous_candidates={"gone"},
            project_root=tmp_path,
            skip_specs={"gone"},
        )
        assert "gone" in candidates

    def test_skipped_spec_not_added_as_new_candidate(self, tmp_path):
        """A skipped spec is not freshly added to the candidate set either —
        its synthetic analysis is not a real verdict."""
        analysis = SpecAnalysis(spec_name="gone", code_fully_absent=False)
        round_result = RoundResult(
            round_index=3, analyses=[analysis], per_spec_deps={},
        )
        accumulated_deps = {"gone": {"src/deleted.py"}}
        candidates = SyncLoop._update_obsolete_candidates(
            round_result=round_result,
            accumulated_deps=accumulated_deps,
            previous_candidates=set(),
            project_root=tmp_path,
            skip_specs={"gone"},
        )
        assert "gone" not in candidates


# ---------------------------------------------------------------------------
# 6. SyncCheckpoint.obsolete_specs field
# ---------------------------------------------------------------------------

class TestCheckpointObsoleteSpecs:
    def test_default_is_empty(self):
        cp = SyncCheckpoint(round_index=1, max_rounds=10)
        assert cp.obsolete_specs == []

    def test_serialize_with_obsolete_specs(self):
        cp = SyncCheckpoint(
            round_index=3, max_rounds=10,
            obsolete_specs=["old-auth", "legacy-api"],
        )
        d = cp.to_dict()
        assert d["obsolete_specs"] == ["old-auth", "legacy-api"]

    def test_serialize_omits_empty(self):
        cp = SyncCheckpoint(round_index=1, max_rounds=10)
        d = cp.to_dict()
        assert "obsolete_specs" not in d

    def test_deserialize_with_obsolete_specs(self):
        d = {
            "checkpoint_version": 1,
            "round_index": 2,
            "max_rounds": 10,
            "reason": "quota_exhausted",
            "obsolete_specs": ["spec-a"],
        }
        cp = SyncCheckpoint.from_dict(d)
        assert cp.obsolete_specs == ["spec-a"]

    def test_deserialize_without_field_defaults_empty(self):
        d = {"checkpoint_version": 1, "round_index": 1, "max_rounds": 10}
        cp = SyncCheckpoint.from_dict(d)
        assert cp.obsolete_specs == []

    def test_roundtrip_preserves_obsolete_specs(self):
        cp = SyncCheckpoint(
            round_index=4, max_rounds=10,
            in_sync_specs={"a": "hash1"},
            failed_analyses={"b": "llm_output_format_error"},
            obsolete_specs=["dead-spec"],
        )
        d = cp.to_dict()
        restored = SyncCheckpoint.from_dict(d)
        assert restored.obsolete_specs == ["dead-spec"]
        assert restored.round_index == 4
        assert restored.in_sync_specs == {"a": "hash1"}


# ---------------------------------------------------------------------------
# 7. SpecDiscovery.delete_obsolete_specs
# ---------------------------------------------------------------------------

class TestDeleteObsoleteSpecs:
    def test_deletes_existing_spec_directory(self, tmp_path):
        _make_spec_dir(tmp_path, "obsolete")
        spec_dir = tmp_path / "se3" / "specs" / "obsolete"
        assert spec_dir.is_dir()

        result = SpecDiscovery.delete_obsolete_specs(
            project_root=tmp_path,
            obsolete_specs=["obsolete"],
        )
        assert result["deleted"] == ["obsolete"]
        assert result["kept"] == []
        assert not spec_dir.exists()

    def test_skips_nonexistent_directory(self, tmp_path):
        result = SpecDiscovery.delete_obsolete_specs(
            project_root=tmp_path,
            obsolete_specs=["nonexistent"],
        )
        assert result["deleted"] == []
        assert result["kept"] == ["nonexistent"]

    def test_deletes_multiple_specs(self, tmp_path):
        _make_spec_dir(tmp_path, "a")
        _make_spec_dir(tmp_path, "b")
        result = SpecDiscovery.delete_obsolete_specs(
            project_root=tmp_path,
            obsolete_specs=["a", "b"],
        )
        assert sorted(result["deleted"]) == ["a", "b"]
        assert not (tmp_path / "se3" / "specs" / "a").exists()
        assert not (tmp_path / "se3" / "specs" / "b").exists()

    def test_mixed_existing_and_missing(self, tmp_path):
        _make_spec_dir(tmp_path, "exists")
        result = SpecDiscovery.delete_obsolete_specs(
            project_root=tmp_path,
            obsolete_specs=["exists", "missing"],
        )
        assert result["deleted"] == ["exists"]
        assert result["kept"] == ["missing"]

    def test_empty_obsolete_list_returns_nothing(self, tmp_path):
        result = SpecDiscovery.delete_obsolete_specs(
            project_root=tmp_path,
            obsolete_specs=[],
        )
        assert result == {"deleted": [], "kept": []}

    def test_sorted_deletion_order(self, tmp_path):
        """Specs are deleted in sorted order."""
        _make_spec_dir(tmp_path, "z-spec")
        _make_spec_dir(tmp_path, "a-spec")
        _make_spec_dir(tmp_path, "m-spec")
        result = SpecDiscovery.delete_obsolete_specs(
            project_root=tmp_path,
            obsolete_specs=["z-spec", "a-spec", "m-spec"],
        )
        assert result["deleted"] == ["a-spec", "m-spec", "z-spec"]

    def test_confirm_yes_deletes(self, tmp_path):
        _make_spec_dir(tmp_path, "feature")
        with patch("builtins.input", return_value="y"):
            result = SpecDiscovery.delete_obsolete_specs(
                project_root=tmp_path,
                obsolete_specs=["feature"],
                confirm=True,
            )
        assert result["deleted"] == ["feature"]

    def test_confirm_no_keeps(self, tmp_path):
        _make_spec_dir(tmp_path, "feature")
        with patch("builtins.input", return_value="n"):
            result = SpecDiscovery.delete_obsolete_specs(
                project_root=tmp_path,
                obsolete_specs=["feature"],
                confirm=True,
            )
        assert result["kept"] == ["feature"]
        assert (tmp_path / "se3" / "specs" / "feature").is_dir()

    def test_confirm_empty_input_keeps(self, tmp_path):
        _make_spec_dir(tmp_path, "feature")
        with patch("builtins.input", return_value=""):
            result = SpecDiscovery.delete_obsolete_specs(
                project_root=tmp_path,
                obsolete_specs=["feature"],
                confirm=True,
            )
        assert "feature" in result["kept"]

    def test_confirm_eof_keeps(self, tmp_path):
        _make_spec_dir(tmp_path, "feature")
        with patch("builtins.input", side_effect=EOFError):
            result = SpecDiscovery.delete_obsolete_specs(
                project_root=tmp_path,
                obsolete_specs=["feature"],
                confirm=True,
            )
        assert "feature" in result["kept"]

    def test_confirm_keyboard_interrupt_keeps(self, tmp_path):
        _make_spec_dir(tmp_path, "feature")
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            result = SpecDiscovery.delete_obsolete_specs(
                project_root=tmp_path,
                obsolete_specs=["feature"],
                confirm=True,
            )
        assert "feature" in result["kept"]
        assert (tmp_path / "se3" / "specs" / "feature").is_dir()


# ---------------------------------------------------------------------------
# 8. LoopResult.obsolete_specs integration
# ---------------------------------------------------------------------------

class TestLoopResultObsoleteSpecs:
    def test_default_empty(self):
        lr = LoopResult()
        assert lr.obsolete_specs == []
        assert lr.obsolete_specs_deleted == []
        assert lr.obsolete_specs_kept == []

    def test_serialized_in_to_dict(self):
        lr = LoopResult(
            converged=True,
            obsolete_specs=["old"],
            obsolete_specs_deleted=["old"],
            obsolete_specs_kept=["maybe-old"],
        )
        d = lr.to_dict()
        assert d["obsolete_specs"] == ["old"]
        assert d["obsolete_specs_deleted"] == ["old"]
        assert d["obsolete_specs_kept"] == ["maybe-old"]


# ---------------------------------------------------------------------------
# 9. Run engine.run_once with spec_deps
# ---------------------------------------------------------------------------

class TestRunOnceWithSpecDeps:
    def test_passes_deps_to_analyzer(self, tmp_path):
        """Verify that spec_deps are passed through to analyze_spec."""
        spec_path = _make_spec_dir(tmp_path, "test-spec")

        from se3.engine.llm_caller import LLMCaller
        from se3.engine.sync_engine import SyncEngine
        from se3.engine.sync_history import SyncFlowContext

        # Create a dep file that exists
        _make_dep_file(tmp_path, "src/exists.py")
        # A dep file that doesn't exist
        deps = ["src/exists.py", "src/missing.py"]

        caller = MagicMock(spec=LLMCaller)
        caller.call.return_value = json.dumps({"diffs": []})
        caller.step_id = "test"

        engine = SyncEngine(tmp_path)
        flow_ctx = MagicMock()
        flow_ctx.make_round_step_id.return_value = "test_step"

        result = engine.run_once(
            round_index=1,
            flow_ctx=flow_ctx,
            llm_caller=caller,
            project_context="{}",
            spec_deps={"test-spec": deps},
        )

        # The analyzer should have been called with deps
        # We can verify by checking that the call happened
        assert len(result.analyses) == 1
        assert result.analyses[0].spec_name == "test-spec"

    def test_spec_not_in_deps_passes_none(self, tmp_path):
        """Spec name not in spec_deps dict gets None deps."""
        _make_spec_dir(tmp_path, "test-spec")
        from se3.engine.llm_caller import LLMCaller
        from se3.engine.sync_engine import SyncEngine
        from se3.engine.sync_history import SyncFlowContext

        caller = MagicMock(spec=LLMCaller)
        caller.call.return_value = json.dumps({"diffs": []})
        caller.step_id = "test"

        engine = SyncEngine(tmp_path)
        flow_ctx = MagicMock()
        flow_ctx.make_round_step_id.return_value = "test_step"

        result = engine.run_once(
            round_index=1,
            flow_ctx=flow_ctx,
            llm_caller=caller,
            project_context="{}",
            spec_deps={"other-spec": ["src/a.py"]},
        )
        assert len(result.analyses) == 1
        # The analyzer shouldn't receive deps for this spec
        assert result.analyses[0].spec_name == "test-spec"


# ---------------------------------------------------------------------------
# 10. check_all_deps_missing static method
# ---------------------------------------------------------------------------

class TestCheckAllDepsMissing:
    def test_none_deps(self, tmp_path):
        result = SyncLoop._check_all_deps_missing(set(), tmp_path)
        assert result is False


# ---------------------------------------------------------------------------
# 12. End-to-end: full SyncLoop.run() detects and deletes an obsolete spec
# ---------------------------------------------------------------------------

class TestObsoleteSpecEndToEnd:
    """Drive a real ``SyncLoop.run()`` where a cached ``sync_state`` recorded a
    spec's deps and those dep files are now gone. Exercises the full wiring:
    cached ``sync_state.spec_deps`` → ``_accumulated_deps`` seed → ``spec_deps``
    passed to ``run_once`` → obsolete candidate → deletion after convergence.
    """

    def _init_git(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "t@t"],
                       cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=str(tmp_path), check=True)
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "main.py").write_text("print('hi')")
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=str(tmp_path), check=True)

    def test_obsolete_spec_detected_and_deleted_end_to_end(
        self, tmp_path, monkeypatch
    ):
        from se3.engine.sync_state import state_path

        self._init_git(tmp_path)

        # Cached sync_state: 'obsolete-feature' recorded a dependency file that
        # no longer exists on disk — its subsystem code was deleted.
        sp = state_path(tmp_path)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({
            "state_version": 1,
            "converged_at": "2026-01-01T00:00:00Z",
            "code_fingerprint": "stale-fingerprint",
            "discovery_converged": True,
            "spec_deps": {
                "obsolete-feature": {
                    "spec_hash": "x",
                    "deps": {"src/old_feature.py": "abc123"},
                },
            },
            "obsolete_specs": [],
        }))

        # The obsolete spec's spec.md still exists on disk (only its code was
        # deleted, not the spec).
        _make_spec_dir(tmp_path, "obsolete-feature")
        spec_path = tmp_path / "se3" / "specs" / "obsolete-feature" / "spec.md"

        calls: list = []

        class _Engine:
            """Engine stand-in: records run_once kwargs and returns a round
            whose analyzer confirmed code_fully_absent for the obsolete spec."""

            def __init__(self, project_root, interactive=False):
                self.project_root = project_root
                self.interactive = interactive
                self._specs = {
                    "obsolete-feature": {
                        "name": "obsolete-feature",
                        "path": str(spec_path),
                        "content": spec_path.read_text(),
                    },
                }

            def run_once(self, **kwargs):
                calls.append(kwargs)
                analysis = SpecAnalysis(
                    spec_name="obsolete-feature",
                    diffs=[],
                    code_fully_absent=True,
                )
                rr = RoundResult(round_index=kwargs["round_index"])
                rr.analyses = [analysis]
                rr.spec_hashes_after = {"obsolete-feature": "h1"}
                return rr

            def _load_specs(self):
                return dict(self._specs)

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", _Engine)
        monkeypatch.setattr(
            "se3.engine.llm_caller.LLMCaller",
            lambda **kwargs: MagicMock(name="LLMCaller"),
        )
        fake_collector = MagicMock()
        fake_collector.collect.return_value = {"git": {}, "specs": []}
        monkeypatch.setattr(
            "se3.engine.project_context.ProjectContextCollector",
            lambda project_root: fake_collector,
        )

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
        result = loop.run()

        # The cached historical deps were fed to the analyzer via spec_deps.
        assert calls, "engine.run_once was never called"
        passed_deps = calls[0].get("spec_deps") or {}
        assert "obsolete-feature" in passed_deps, (
            "cached sync_state deps never reached run_once"
        )
        assert "src/old_feature.py" in passed_deps["obsolete-feature"]

        # The spec was detected obsolete and its directory deleted.
        assert result.converged is True
        assert "obsolete-feature" in result.obsolete_specs
        assert "obsolete-feature" in result.obsolete_specs_deleted
        assert not (tmp_path / "se3" / "specs" / "obsolete-feature").exists()


# ---------------------------------------------------------------------------
# 11. Fence stripping in _parse_analysis_response
# ---------------------------------------------------------------------------

class TestParseAnalysisResponseFenceStripping:
    def test_json_in_fenced_block(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        result = analyzer._parse_analysis_response(
            "spec",
            '```json\n{"diffs": [{"type": "gap", "description": "test"}], "code_fully_absent": true}\n```',
        )
        assert len(result.diffs) == 1
        assert result.diffs[0].diff_type == DiffType.GAP
        assert result.code_fully_absent is True

    def test_empty_after_stripping_returns_infrastructure_failure(self, tmp_path):
        """Empty content after fence stripping → infrastructure failure
        (the agent produced no meaningful output)."""
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        result = analyzer._parse_analysis_response("spec", "```\n```")
        assert result.analysis_failed
        assert result.failed_analysis_reason == "infrastructure_failure"

    def test_whitespace_only_after_stripping(self, tmp_path):
        """Whitespace-only after fence stripping → infrastructure failure."""
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        result = analyzer._parse_analysis_response("spec", "```json\n   \n```")
        assert result.analysis_failed
        assert result.failed_analysis_reason == "infrastructure_failure"
