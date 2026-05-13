"""Tests for SyncEngine — single-round, one-directional sync.

After the G2 refactor the engine performs one stateless pass per call
(``run_once``); convergence and oscillation detection live in
``SyncLoop`` (tested separately in ``test_sync_loop.py``).

This module exercises:

* ``_load_specs`` and base-spec generation
* Drift application: gap (delete), extension (append), conflict (rewrite)
* Length and markdown-fence safety guards
* High-impact-deletion detection and interactive gating
* ``process_call_response`` for ``sync_high_impact_deletion`` payloads
* Spec-hash recording in ``RoundResult.spec_hashes_after``
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.sync_engine import (
    DiffType,
    LoopResult,
    RoundResult,
    SpecAnalysis,
    SpecDiff,
    SyncEngine,
    _hash_spec_content,
    strip_markdown_fences,
)
from se3.engine.sync_history import SyncFlowContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_spec(tmp_path, name, content=None):
    """Helper: create a spec directory with spec.md. Content defaults to a
    body long enough to clear the 50% length safety guard."""
    if content is None:
        content = (
            "# Spec\n\n## Purpose\n"
            "Test spec body that is long enough to clear the 50% safety guard. "
            * 4
        )
    spec_dir = tmp_path / "se3" / "specs" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text(content, encoding="utf-8")
    return spec_dir


def _make_flow_ctx(tmp_path):
    return SyncFlowContext(tmp_path)


# ---------------------------------------------------------------------------
# Spec loading
# ---------------------------------------------------------------------------

class TestSyncEngineInit:
    def test_init_stores_attributes(self, tmp_path):
        engine = SyncEngine(tmp_path, interactive=True)
        assert engine.project_root == tmp_path
        assert engine.interactive is True

    def test_default_interactive_false(self, tmp_path):
        engine = SyncEngine(tmp_path)
        assert engine.interactive is False


class TestSyncEngineLoadSpecs:
    def test_loads_base_spec_first(self, tmp_path):
        _create_spec(tmp_path, "base", "# Base spec content")
        _create_spec(tmp_path, "auth", "# Auth spec content")

        engine = SyncEngine(tmp_path)
        specs = engine._load_specs()

        assert "base" in specs
        assert "auth" in specs
        assert list(specs.keys())[0] == "base"

    def test_empty_specs_directory(self, tmp_path):
        (tmp_path / "se3" / "specs").mkdir(parents=True, exist_ok=True)
        engine = SyncEngine(tmp_path)
        assert engine._load_specs() == {}


# ---------------------------------------------------------------------------
# strip_markdown_fences
# ---------------------------------------------------------------------------

class TestStripMarkdownFences:
    def test_strips_outer_fences(self):
        text = "```markdown\n# Spec\n## Purpose\n```"
        assert strip_markdown_fences(text) == "# Spec\n## Purpose"

    def test_strips_unlabeled_fences(self):
        text = "```\n# Spec\n```"
        assert strip_markdown_fences(text) == "# Spec"

    def test_no_change_when_no_fences(self):
        text = "# Spec\nNo fences here."
        assert strip_markdown_fences(text) == text


# ---------------------------------------------------------------------------
# _hash_spec_content
# ---------------------------------------------------------------------------

class TestHashSpecContent:
    def test_hash_is_stable_across_trailing_whitespace(self):
        h1 = _hash_spec_content("line1\nline2\n")
        h2 = _hash_spec_content("line1   \nline2  ")
        assert h1 == h2

    def test_different_content_different_hash(self):
        assert _hash_spec_content("a") != _hash_spec_content("b")


# ---------------------------------------------------------------------------
# Drift application
# ---------------------------------------------------------------------------

class TestApplySpecDriftExtension:
    def test_extension_appends_via_llm(self, tmp_path):
        original = (
            "# Spec\n\n## Purpose\n"
            "Original spec body content that is long enough to clear the length guard. "
            * 4
        )
        _create_spec(tmp_path, "auth", original)

        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = original + "\n\n## New Section\nExtended content added."

        diff = SpecDiff(DiffType.EXTENSION, "auth", "Helper function added", "src/u.py:1")
        applied, label = engine._apply_spec_drift_update(diff, llm)

        assert applied is True
        assert "added" in label
        actual = (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text()
        assert "New Section" in actual


class TestApplySpecDriftGap:
    def test_gap_rewrites_to_remove_requirement(self, tmp_path):
        original = (
            "# Spec\n\n## Purpose\nIntro long enough to clear length guard. " * 4
            + "\n\n### Requirement: Keep me\n\n- Stay\n\n"
            "### Requirement: Delete me\n\n- Gone\n"
        )
        _create_spec(tmp_path, "auth", original)

        engine = SyncEngine(tmp_path)
        engine._load_specs()

        rewritten = (
            "# Spec\n\n## Purpose\nIntro long enough to clear length guard. " * 4
            + "\n\n### Requirement: Keep me\n\n- Stay\n"
        )
        llm = MagicMock()
        llm.call.return_value = rewritten

        diff = SpecDiff(
            DiffType.GAP, "auth",
            "Spec describes 'Delete me' requirement not present in code",
            "src/auth.py",
        )
        applied, label = engine._apply_spec_drift_update(diff, llm)

        assert applied is True
        assert "removed" in label
        actual = (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text()
        assert "Delete me" not in actual


class TestApplySpecDriftConflict:
    def test_conflict_rewrites_via_llm(self, tmp_path):
        original = "# Spec body that is long enough to clear the length safety guard. " * 4
        _create_spec(tmp_path, "auth", original)

        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = original.replace("body", "rewritten body")

        diff = SpecDiff(DiffType.CONFLICT, "auth", "Token format mismatch", "src/a.py:1")
        applied, label = engine._apply_spec_drift_update(diff, llm)

        assert applied is True
        assert "modified" in label


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------

class TestSpecUpdateLengthGuard:
    def test_short_response_rejected(self, tmp_path):
        original = "# Long original spec body. " * 20
        _create_spec(tmp_path, "auth", original)

        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "# tiny"

        diff = SpecDiff(DiffType.EXTENSION, "auth", "added feature")
        applied, _ = engine._apply_spec_drift_update(diff, llm)

        assert applied is False
        actual = (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text()
        assert actual == original

    def test_empty_response_rejected(self, tmp_path):
        original = "# Long original spec body. " * 20
        _create_spec(tmp_path, "auth", original)

        engine = SyncEngine(tmp_path)
        engine._load_specs()

        llm = MagicMock()
        llm.call.return_value = "   "

        diff = SpecDiff(DiffType.EXTENSION, "auth", "added")
        applied, _ = engine._apply_spec_drift_update(diff, llm)
        assert applied is False


class TestSpecUpdateFenceStripping:
    def test_fence_wrapped_response_is_stripped(self, tmp_path):
        original = "# Long original spec body. " * 20
        _create_spec(tmp_path, "auth", original)

        engine = SyncEngine(tmp_path)
        engine._load_specs()

        replacement = "# Long updated spec body content. " * 20
        wrapped = f"```markdown\n{replacement}\n```"

        llm = MagicMock()
        llm.call.return_value = wrapped

        diff = SpecDiff(DiffType.EXTENSION, "auth", "added")
        applied, _ = engine._apply_spec_drift_update(diff, llm)

        assert applied is True
        actual = (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text()
        assert "```" not in actual


# ---------------------------------------------------------------------------
# High-impact deletion detection
# ---------------------------------------------------------------------------

class TestIsHighImpactDeletion:
    def test_gap_mentioning_existing_requirement_is_high_impact(self, tmp_path):
        spec = (
            "# Spec\n## Purpose\nx\n\n"
            "### Requirement: Login Flow\n\n- step 1\n"
        )
        _create_spec(tmp_path, "auth", spec)
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        diff = SpecDiff(
            DiffType.GAP, "auth",
            "Spec describes 'Login Flow' requirement but code lacks it",
        )
        assert engine._is_high_impact_deletion(diff) is True

    def test_extension_never_high_impact(self, tmp_path):
        _create_spec(tmp_path, "auth", "### Requirement: Foo\n\n- x\n")
        engine = SyncEngine(tmp_path)
        engine._load_specs()
        diff = SpecDiff(DiffType.EXTENSION, "auth", "Foo extension")
        assert engine._is_high_impact_deletion(diff) is False

    def test_gap_not_mentioning_existing_requirement_low_impact(self, tmp_path):
        _create_spec(tmp_path, "auth", "### Requirement: Foo\n\n- x\n")
        engine = SyncEngine(tmp_path)
        engine._load_specs()
        diff = SpecDiff(DiffType.GAP, "auth", "Unrelated minor gap text")
        assert engine._is_high_impact_deletion(diff) is False

    def test_gap_mentioning_requirement_name_without_deletion_intent_is_low_impact(
        self, tmp_path
    ):
        """Regression: a GAP whose description merely mentions an existing
        Requirement heading by name — but proposes only a narrow in-place
        tweak — must NOT be classified as high-impact."""
        spec = (
            "# Spec\n## Purpose\nx\n\n"
            "### Requirement: Project Identity\n\n- primary language: Python 3.8+\n"
        )
        _create_spec(tmp_path, "base", spec)
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        diff = SpecDiff(
            DiffType.GAP,
            "base",
            "The project identity section's listed primary language is outdated.",
        )
        assert engine._is_high_impact_deletion(diff) is False

    def test_gap_with_explicit_removal_verb_and_requirement_word_is_high_impact(
        self, tmp_path
    ):
        spec = (
            "# Spec\n## Purpose\nx\n\n"
            "### Requirement: Legacy Auth\n\n- step 1\n"
        )
        _create_spec(tmp_path, "auth", spec)
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        diff = SpecDiff(
            DiffType.GAP,
            "auth",
            "The Legacy Auth requirement was removed from the code entirely.",
        )
        assert engine._is_high_impact_deletion(diff) is True

    def test_gap_with_heading_style_reference_is_high_impact(self, tmp_path):
        spec = (
            "# Spec\n## Purpose\nx\n\n"
            "### Requirement: Legacy Auth\n\n- step 1\n"
        )
        _create_spec(tmp_path, "auth", spec)
        engine = SyncEngine(tmp_path)
        engine._load_specs()

        diff = SpecDiff(
            DiffType.GAP,
            "auth",
            "Requirement: Legacy Auth has no corresponding implementation.",
        )
        assert engine._is_high_impact_deletion(diff) is True


# ---------------------------------------------------------------------------
# run_once integration
# ---------------------------------------------------------------------------

class TestRunOnce:
    def test_no_drift_returns_in_sync_round(self, tmp_path):
        _create_spec(tmp_path, "base", "# Base spec body. " * 10)
        engine = SyncEngine(tmp_path)
        flow_ctx = _make_flow_ctx(tmp_path)

        with patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec") as mock_an:
            mock_an.return_value = SpecAnalysis(spec_name="base", diffs=[])
            result = engine.run_once(
                round_index=1,
                flow_ctx=flow_ctx,
                llm_caller=MagicMock(),
                project_context="{}",
            )

        assert isinstance(result, RoundResult)
        assert result.round_index == 1
        assert result.specs_updated == 0
        assert "base" in result.spec_hashes_after

    def test_extension_drift_updates_spec(self, tmp_path):
        original = "# Spec body that is long enough to clear the 50% guard. " * 4
        _create_spec(tmp_path, "auth", original)

        engine = SyncEngine(tmp_path)
        flow_ctx = _make_flow_ctx(tmp_path)

        llm = MagicMock()
        llm.call.return_value = original + "\n\n## Extra\nNew section."

        with patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec") as mock_an:
            mock_an.return_value = SpecAnalysis(
                spec_name="auth",
                diffs=[SpecDiff(DiffType.EXTENSION, "auth", "Helper added")],
            )
            result = engine.run_once(
                round_index=1,
                flow_ctx=flow_ctx,
                llm_caller=llm,
                project_context="{}",
            )

        assert result.specs_updated == 1
        assert "auth" in result.changes_by_spec
        assert any("added" in c for c in result.changes_by_spec["auth"])

    def test_spec_hashes_recorded_for_all_specs(self, tmp_path):
        _create_spec(tmp_path, "base", "# Base body. " * 10)
        _create_spec(tmp_path, "auth", "# Auth body. " * 10)

        engine = SyncEngine(tmp_path)
        flow_ctx = _make_flow_ctx(tmp_path)

        with patch("se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec") as mock_an:
            mock_an.return_value = SpecAnalysis(spec_name="x", diffs=[])
            result = engine.run_once(
                round_index=1,
                flow_ctx=flow_ctx,
                llm_caller=MagicMock(),
                project_context="{}",
            )

        assert set(result.spec_hashes_after.keys()) == {"base", "auth"}

    def test_step_id_includes_round_index(self, tmp_path):
        _create_spec(tmp_path, "auth", "# Auth body. " * 10)

        engine = SyncEngine(tmp_path)
        flow_ctx = _make_flow_ctx(tmp_path)
        seen_step_ids = []

        llm = MagicMock()

        def record_step(spec_name, content, ctx):
            seen_step_ids.append(llm.step_id)
            return SpecAnalysis(spec_name=spec_name, diffs=[])

        with patch(
            "se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec",
            side_effect=record_step,
        ):
            engine.run_once(
                round_index=3,
                flow_ctx=flow_ctx,
                llm_caller=llm,
                project_context="{}",
            )

        assert any("r3" in sid for sid in seen_step_ids)


# ---------------------------------------------------------------------------
# Interactive high-impact gating
# ---------------------------------------------------------------------------

class TestInteractiveHighImpact:
    def test_interactive_invokes_collect_decisions(self, tmp_path):
        spec_content = (
            "# Spec\n## Purpose\np that is long enough to clear length guard. "
            * 4
            + "\n\n### Requirement: Foo\n\n- a body content long enough.\n"
        )
        _create_spec(tmp_path, "auth", spec_content)

        engine = SyncEngine(tmp_path, interactive=True)
        flow_ctx = _make_flow_ctx(tmp_path)
        llm = MagicMock()
        llm.call.return_value = spec_content.replace(
            "### Requirement: Foo\n\n- a body content long enough.\n",
            "",
        )

        with patch(
            "se3.engine.sync_interaction.SyncInteractionHandler.collect_decisions",
            autospec=True,
        ) as mock_collect, patch(
            "se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec"
        ) as mock_an:
            mock_an.return_value = SpecAnalysis(
                spec_name="auth",
                diffs=[
                    SpecDiff(
                        DiffType.GAP, "auth",
                        "Foo requirement not implemented in code",
                    ),
                ],
            )

            def approve_all(handler_self):
                return {item.item_id: "approve" for item in handler_self._pending_items}

            mock_collect.side_effect = approve_all

            result = engine.run_once(
                round_index=1,
                flow_ctx=flow_ctx,
                llm_caller=llm,
                project_context="{}",
            )

        assert mock_collect.called
        assert any(
            entry.get("decision") == "approve"
            for entry in result.high_impact_deletions
        )

    def test_non_interactive_auto_applies_high_impact(self, tmp_path):
        spec_content = (
            "# Spec\n## Purpose\np that is long enough to clear length guard. "
            * 4
            + "\n\n### Requirement: Foo\n\n- a body content long enough.\n"
        )
        _create_spec(tmp_path, "auth", spec_content)

        engine = SyncEngine(tmp_path, interactive=False)
        flow_ctx = _make_flow_ctx(tmp_path)
        llm = MagicMock()
        llm.call.return_value = (
            "# Spec\n## Purpose\np that is long enough to clear length guard. " * 4
            + "\n"
        )

        with patch(
            "se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec"
        ) as mock_an:
            mock_an.return_value = SpecAnalysis(
                spec_name="auth",
                diffs=[
                    SpecDiff(
                        DiffType.GAP, "auth",
                        "Foo requirement not implemented in code",
                    ),
                ],
            )

            result = engine.run_once(
                round_index=1,
                flow_ctx=flow_ctx,
                llm_caller=llm,
                project_context="{}",
            )

        assert any(
            e["decision"] == "auto" for e in result.high_impact_deletions
        )

    def test_keyboard_interrupt_during_approval_propagates(self, tmp_path):
        """Ctrl+C in interactive approval must abort the sync, not silently
        downgrade every pending deletion to ``skip`` and return normally."""
        spec_content = (
            "# Spec\n## Purpose\np that is long enough to clear length guard. "
            * 4
            + "\n\n### Requirement: Foo\n\n- a body content long enough.\n"
        )
        _create_spec(tmp_path, "auth", spec_content)

        engine = SyncEngine(tmp_path, interactive=True)
        flow_ctx = _make_flow_ctx(tmp_path)
        llm = MagicMock()

        with patch(
            "se3.engine.sync_interaction.SyncInteractionHandler.collect_decisions",
            side_effect=KeyboardInterrupt,
        ), patch(
            "se3.engine.sync_analyzer.SyncAnalyzer.analyze_spec"
        ) as mock_an:
            mock_an.return_value = SpecAnalysis(
                spec_name="auth",
                diffs=[
                    SpecDiff(
                        DiffType.GAP, "auth",
                        "Foo requirement not implemented in code",
                    ),
                ],
            )

            with pytest.raises(KeyboardInterrupt):
                engine.run_once(
                    round_index=1,
                    flow_ctx=flow_ctx,
                    llm_caller=llm,
                    project_context="{}",
                )


# ---------------------------------------------------------------------------
# Round-stability semantics (convergence honesty)
# ---------------------------------------------------------------------------


class TestRoundIsStable:
    def test_empty_round_is_stable(self):
        rr = RoundResult(round_index=1)
        assert rr.is_stable is True

    def test_round_with_updates_is_not_stable(self):
        rr = RoundResult(round_index=1)
        rr.specs_updated = 2
        assert rr.is_stable is False

    def test_round_with_unresolved_drift_is_not_stable(self):
        rr = RoundResult(round_index=1)
        rr.specs_updated = 0
        rr.analyses.append(
            SpecAnalysis(
                spec_name="auth",
                diffs=[SpecDiff(DiffType.GAP, "auth", "Unresolved drift")],
            )
        )
        assert rr.is_stable is False

    def test_round_with_clean_analyses_and_no_updates_is_stable(self):
        rr = RoundResult(round_index=1)
        rr.analyses.append(SpecAnalysis(spec_name="auth", diffs=[]))
        assert rr.is_stable is True


# ---------------------------------------------------------------------------
# process_call_response
# ---------------------------------------------------------------------------

class TestProcessCallResponse:
    def _write_call_and_response(self, tmp_path, items, responses):
        calls_dir = tmp_path / "se3" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)

        call_file = calls_dir / "sync_deletion_test.json"
        call_file.write_text(
            json.dumps({"type": "sync_high_impact_deletion", "items": items}),
            encoding="utf-8",
        )

        response_file = calls_dir / "sync_deletion_test.json.response"
        response_file.write_text(json.dumps({"items": responses}), encoding="utf-8")

        return call_file

    def test_approve_applies_update(self, tmp_path):
        original = "# Spec body content long enough to clear guard. " * 10
        _create_spec(tmp_path, "auth", original)

        call_file = self._write_call_and_response(
            tmp_path,
            items=[{
                "item_id": "del_auth_001",
                "spec_name": "auth",
                "requirement_name": "Foo",
                "excerpt": "Foo requirement excerpt that is long enough.",
            }],
            responses=[{"item_id": "del_auth_001", "decision": "approve"}],
        )

        engine = SyncEngine(tmp_path)
        llm = MagicMock()
        llm.call.return_value = "# Updated spec body content. " * 10
        result = engine.process_call_response(call_file, llm)

        assert result["specs_updated"] == 1
        assert result["skipped"] == 0

    def test_skip_does_not_apply_update(self, tmp_path):
        original = "# Original body content " * 10
        _create_spec(tmp_path, "auth", original)

        call_file = self._write_call_and_response(
            tmp_path,
            items=[{
                "item_id": "del_auth_001",
                "spec_name": "auth",
                "requirement_name": "Foo",
                "excerpt": "Excerpt",
            }],
            responses=[{"item_id": "del_auth_001", "decision": "skip"}],
        )

        engine = SyncEngine(tmp_path)
        result = engine.process_call_response(call_file, MagicMock())

        assert result["specs_updated"] == 0
        assert result["skipped"] == 1
        assert (tmp_path / "se3" / "specs" / "auth" / "spec.md").read_text() == original

    def test_unsupported_legacy_type_raises(self, tmp_path):
        calls_dir = tmp_path / "se3" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)
        call_file = calls_dir / "legacy.json"
        call_file.write_text(
            json.dumps({"type": "sync_pending_decisions", "items": []}),
            encoding="utf-8",
        )
        (calls_dir / "legacy.json.response").write_text(
            json.dumps({"items": []}), encoding="utf-8"
        )

        engine = SyncEngine(tmp_path)
        with pytest.raises(ValueError, match="Unsupported"):
            engine.process_call_response(call_file, MagicMock())

    def test_missing_response_file(self, tmp_path):
        calls_dir = tmp_path / "se3" / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)
        call_file = calls_dir / "x.json"
        call_file.write_text(
            json.dumps({"type": "sync_high_impact_deletion", "items": []}),
            encoding="utf-8",
        )

        engine = SyncEngine(tmp_path)
        result = engine.process_call_response(call_file)
        assert result == {"specs_updated": 0, "skipped": 0}
