"""Tests for step_renderers custom renderers.

Tests _render_analyze, _render_verify_spec, _render_update_spec, and _render_commit.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from se3.engine.models import Step, StepStatus, StepType


def _make_step(
    step_type: StepType,
    outputs: dict,
    error_message: str | None = None,
    status: StepStatus = StepStatus.COMPLETED,
) -> Step:
    """Create a Step with given type and outputs."""
    step = Step(step_type=step_type, status=status)
    step.outputs = outputs
    step.error_message = error_message
    return step


# ---------------------------------------------------------------------------
# _render_analyze
# ---------------------------------------------------------------------------


class TestRenderAnalyze:
    @patch("se3.engine.step_renderers.render_full")
    def test_normal_output(self, mock_render_full):
        step = _make_step(StepType.ANALYZE, {
            "task_type": "feature",
            "complexity": "medium",
            "scope": "src/se3/engine/step_renderers.py",
            "reasoning": "This task modifies rendering logic.",
            "selected_items": [
                {"spec": "flow-engine", "requirement_name": "FE-3"},
                {"spec": "base", "requirement_name": "Project Identity"},
            ],
            "spec_content": "long spec content...",
            "project_summary": "long project summary...",
        })

        from se3.engine.step_renderers import _render_analyze
        _render_analyze(step)

        mock_render_full.assert_called_once()
        content = mock_render_full.call_args[0][0]

        # Status bar shows task_type, complexity, scope
        assert "feature" in content
        assert "medium" in content
        assert "src/se3/engine/step_renderers.py" in content

        # Reasoning displayed
        assert "This task modifies rendering logic." in content

        # Relevant Spec Items listed as spec:requirement_name
        assert "Relevant Spec Items" in content
        assert "flow-engine:FE-3" in content
        assert "base:Project Identity" in content

        # Internal fields NOT displayed
        assert "long spec content..." not in content
        assert "long project summary..." not in content

    @patch("se3.engine.step_renderers.render_full")
    def test_partial_fields_missing(self, mock_render_full):
        step = _make_step(StepType.ANALYZE, {
            "task_type": "bugfix",
        })

        from se3.engine.step_renderers import _render_analyze
        _render_analyze(step)

        content = mock_render_full.call_args[0][0]
        assert "bugfix" in content
        assert "N/A" in content  # missing complexity/scope default to N/A

    @patch("se3.engine.step_renderers.render_full")
    def test_empty_outputs(self, mock_render_full):
        step = _make_step(StepType.ANALYZE, {})

        from se3.engine.step_renderers import _render_analyze
        _render_analyze(step)

        content = mock_render_full.call_args[0][0]
        assert "N/A" in content  # all defaults

    @patch("se3.engine.step_renderers.render_full")
    def test_selected_items_rendering(self, mock_render_full):
        step = _make_step(StepType.ANALYZE, {
            "task_type": "feature",
            "selected_items": [
                {"spec": "flow-engine", "requirement_name": "FE-3"},
                {"spec": "base", "requirement_name": "Project Identity"},
            ],
        })

        from se3.engine.step_renderers import _render_analyze
        _render_analyze(step)

        content = mock_render_full.call_args[0][0]
        assert "Relevant Spec Items" in content
        assert "flow-engine:FE-3" in content
        assert "base:Project Identity" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_analyze_renderer_shows_spec_items(self, mock_render_full):
        """G4 acceptance test: analyze renderer displays 'Relevant Spec Items'
        and renders each item as ``spec:requirement_name`` (e.g., flow-engine:FE-3).
        Guards against regressions where the renderer reverts to showing
        relevant_specs / spec names only.
        """
        step = _make_step(StepType.ANALYZE, {
            "task_type": "feature",
            "selected_items": [
                {"spec": "flow-engine", "requirement_name": "FE-3"},
            ],
            "spec_content": "ignored payload",
            "project_summary": "ignored payload",
        })

        from se3.engine.step_renderers import _render_analyze
        _render_analyze(step)

        content = mock_render_full.call_args[0][0]
        assert "Relevant Spec Items" in content
        assert "flow-engine:FE-3" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_no_selected_items_no_section(self, mock_render_full):
        step = _make_step(StepType.ANALYZE, {
            "task_type": "feature",
            "complexity": "simple",
            "scope": "tiny",
            "selected_items": [],
        })

        from se3.engine.step_renderers import _render_analyze
        _render_analyze(step)

        content = mock_render_full.call_args[0][0]
        assert "Relevant Spec Items" not in content


# ---------------------------------------------------------------------------
# _render_verify_spec
# ---------------------------------------------------------------------------


class TestRenderVerifySpec:
    @patch("se3.engine.step_renderers.render_full")
    def test_passed(self, mock_render_full):
        step = _make_step(StepType.VERIFY_SPEC, {
            "verified": True,
            "summary": "All checks passed.",
            "issues": [],
            "fix_needed": False,
            "fix_context": "internal context",
            "fix_iteration": 0,
            "max_fix_iterations": 3,
            "verification_result": "passed",
        })

        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "PASSED" in content
        assert "All checks passed." in content

        # Internal fields not displayed
        assert "fix_context" not in content
        assert "fix_iteration" not in content
        assert "verification_result" not in content

    @patch("se3.engine.step_renderers.render_full")
    def test_failed_with_issues(self, mock_render_full):
        step = _make_step(StepType.VERIFY_SPEC, {
            "verified": False,
            "summary": "Issues found.",
            "issues": [
                {"priority": "high", "scope": "in_scope", "message": "Missing function", "suggestion": "Add the function"},
                {"priority": "medium", "scope": "in_scope", "message": "Unused import"},
                {"priority": "low", "scope": "out_of_scope", "message": "Style note"},
            ],
        })

        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "FAILED" in content
        assert "Missing function" in content
        assert "Add the function" in content
        assert "Unused import" in content
        assert "Style note" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_no_issues(self, mock_render_full):
        step = _make_step(StepType.VERIFY_SPEC, {
            "verified": True,
            "issues": [],
        })

        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "PASSED" in content
        # No scope/priority group headers when no issues
        assert "In-scope" not in content

    @patch("se3.engine.step_renderers.render_full")
    def test_with_recommendations(self, mock_render_full):
        step = _make_step(StepType.VERIFY_SPEC, {
            "verified": True,
            "recommendations": ["Consider adding more tests", "Update docs"],
        })

        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "Consider adding more tests" in content
        assert "Update docs" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_infer_verified_from_fix_needed(self, mock_render_full):
        """When 'verified' key is absent, infer from fix_needed."""
        step = _make_step(StepType.VERIFY_SPEC, {
            "fix_needed": False,
        })

        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "PASSED" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_summary_from_verification_result(self, mock_render_full):
        """summary should fall back to verification_result nested dict."""
        step = _make_step(StepType.VERIFY_SPEC, {
            "verified": True,
            "verification_result": {
                "verified": True,
                "summary": "Nested summary text",
                "recommendations": ["Rec from nested"],
            },
        })

        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "Nested summary text" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_recommendations_from_verification_result(self, mock_render_full):
        """recommendations should fall back to verification_result nested dict."""
        step = _make_step(StepType.VERIFY_SPEC, {
            "verified": True,
            "verification_result": {
                "verified": True,
                "recommendations": ["Use type hints", "Add docstrings"],
            },
        })

        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "Use type hints" in content
        assert "Add docstrings" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_toplevel_summary_takes_precedence(self, mock_render_full):
        """When top-level summary exists, it should be used over nested one."""
        step = _make_step(StepType.VERIFY_SPEC, {
            "verified": True,
            "summary": "Top-level summary",
            "verification_result": {
                "summary": "Nested summary",
            },
        })

        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "Top-level summary" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_rich_close_tag_no_extra_bracket(self, mock_render_full):
        """Close tags for issue priority colors must not have extra ] chars."""
        step = _make_step(StepType.VERIFY_SPEC, {
            "verified": False,
            "issues": [
                {"priority": "critical", "scope": "in_scope", "message": "crit msg"},
                {"priority": "high", "scope": "in_scope", "message": "err msg"},
                {"priority": "medium", "scope": "in_scope", "message": "warn msg"},
                {"priority": "low", "scope": "in_scope", "message": "info msg"},
            ],
        })

        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        # Correct close tags
        assert "[/red]" in content
        assert "[/yellow]" in content
        assert "[/dim]" in content
        # No malformed double-bracket close tags
        assert "[/red]]" not in content
        assert "[/yellow]]" not in content
        assert "[/dim]]" not in content


# ---------------------------------------------------------------------------
# _render_update_spec
# ---------------------------------------------------------------------------


class TestRenderUpdateSpec:
    @patch("se3.engine.step_renderers.render_full")
    def test_with_updates(self, mock_render_full):
        step = _make_step(StepType.UPDATE_SPEC, {
            "updated_specs": [
                {"spec_name": "flow-engine", "change_description": "Added new requirement"},
                {"spec_name": "base", "change_description": "Updated conventions"},
            ],
        })

        from se3.engine.step_renderers import _render_update_spec
        _render_update_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "flow-engine" in content
        assert "Added new requirement" in content
        assert "base" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_no_updates(self, mock_render_full):
        step = _make_step(StepType.UPDATE_SPEC, {})

        from se3.engine.step_renderers import _render_update_spec
        _render_update_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "No spec updates needed" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_specs_updated_key_compat(self, mock_render_full):
        """Accept both 'updated_specs' and 'specs_updated' keys."""
        step = _make_step(StepType.UPDATE_SPEC, {
            "specs_updated": [
                {"spec_name": "base", "change_description": "Minor tweak"},
            ],
        })

        from se3.engine.step_renderers import _render_update_spec
        _render_update_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "base" in content
        assert "Minor tweak" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_with_new_capabilities(self, mock_render_full):
        step = _make_step(StepType.UPDATE_SPEC, {
            "updated_specs": [{"spec_name": "x", "change_description": "y"}],
            "new_capabilities": ["Streaming support", "Batch mode"],
        })

        from se3.engine.step_renderers import _render_update_spec
        _render_update_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "Streaming support" in content
        assert "Batch mode" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_string_specs(self, mock_render_full):
        step = _make_step(StepType.UPDATE_SPEC, {
            "updated_specs": ["flow-engine updated", "base updated"],
        })

        from se3.engine.step_renderers import _render_update_spec
        _render_update_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "flow-engine updated" in content


# ---------------------------------------------------------------------------
# _render_commit
# ---------------------------------------------------------------------------


class TestRenderCommit:
    @patch("se3.engine.step_renderers.render_full")
    def test_committed_false(self, mock_render_full):
        step = _make_step(StepType.COMMIT, {"committed": False})

        from se3.engine.step_renderers import _render_commit
        _render_commit(step)

        content = mock_render_full.call_args[0][0]
        assert "No changes to commit" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_committed_true_no_version(self, mock_render_full):
        step = _make_step(StepType.COMMIT, {
            "committed": True,
            "commit_hash": "abc1234def5678",
            "commit_message": "feat: add new renderers",
        })

        from se3.engine.step_renderers import _render_commit
        _render_commit(step)

        content = mock_render_full.call_args[0][0]
        assert "abc1234" in content
        assert "feat: add new renderers" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_committed_true_with_version(self, mock_render_full):
        step = _make_step(StepType.COMMIT, {
            "committed": True,
            "commit_hash": "abc1234def5678",
            "commit_message": "feat: version bump",
            "version_bumped": True,
            "version": "1.3.0",
        })

        from se3.engine.step_renderers import _render_commit
        _render_commit(step)

        content = mock_render_full.call_args[0][0]
        assert "abc1234" in content
        assert "v1.3.0" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_missing_commit_hash(self, mock_render_full):
        step = _make_step(StepType.COMMIT, {
            "committed": True,
        })

        from se3.engine.step_renderers import _render_commit
        _render_commit(step)

        content = mock_render_full.call_args[0][0]
        assert "N/A" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_empty_outputs_defaults_to_no_commit(self, mock_render_full):
        step = _make_step(StepType.COMMIT, {})

        from se3.engine.step_renderers import _render_commit
        _render_commit(step)

        content = mock_render_full.call_args[0][0]
        assert "No changes to commit" in content


# ---------------------------------------------------------------------------
# _render_self_check
# ---------------------------------------------------------------------------


class TestRenderSelfCheck:
    @patch("se3.engine.step_renderers.render_full")
    def test_failed_status_with_zero_actionable_shows_failed(self, mock_render_full):
        """FAILED status with actionable_count=0 should show FAILED, not PASSED."""
        step = _make_step(
            StepType.SELF_CHECK,
            outputs={"actionable_count": 0},
            status=StepStatus.FAILED,
            error_message="Failed to parse self-check result from LLM response",
        )

        from se3.engine.step_renderers import _render_self_check
        _render_self_check(step)

        content = mock_render_full.call_args[0][0]
        assert "FAILED" in content
        assert "PASSED" not in content

    @patch("se3.engine.step_renderers.render_full")
    def test_completed_status_with_zero_actionable_shows_passed(self, mock_render_full):
        """COMPLETED status with no issues should show PASSED."""
        step = _make_step(
            StepType.SELF_CHECK,
            outputs={"actionable_count": 0, "issues": [], "self_check_result": {"summary": "All good"}},
            status=StepStatus.COMPLETED,
        )

        from se3.engine.step_renderers import _render_self_check
        _render_self_check(step)

        content = mock_render_full.call_args[0][0]
        assert "PASSED" in content
        assert "FAILED" not in content

    @patch("se3.engine.step_renderers.render_full")
    def test_failed_status_with_actionable_issues_shows_failed(self, mock_render_full):
        """FAILED status with actionable issues should show FAILED."""
        step = _make_step(
            StepType.SELF_CHECK,
            outputs={
                "actionable_count": 2,
                "issues": [
                    {"severity": "high", "description": "Bug A", "location": "file.py"},
                    {"severity": "medium", "description": "Bug B", "location": "file2.py"},
                ],
            },
            status=StepStatus.FAILED,
        )

        from se3.engine.step_renderers import _render_self_check
        _render_self_check(step)

        content = mock_render_full.call_args[0][0]
        assert "FAILED" in content
        assert "PASSED" not in content

    @patch("se3.engine.step_renderers.render_full")
    def test_failed_status_no_outputs_shows_failed(self, mock_render_full):
        """FAILED status with empty outputs (pre-failure) should show FAILED."""
        step = _make_step(
            StepType.SELF_CHECK,
            outputs={},
            status=StepStatus.FAILED,
            error_message="Self-check failed: LLM call error",
        )

        from se3.engine.step_renderers import _render_self_check
        _render_self_check(step)

        content = mock_render_full.call_args[0][0]
        assert "FAILED" in content
        assert "PASSED" not in content


# ---------------------------------------------------------------------------
# Per-step token-usage block appended by render_step_output (G3)
# ---------------------------------------------------------------------------


class TestStepUsageBlock:
    _USAGE = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 10,
        "total_cost_usd": 0.01,
    }

    @patch("se3.engine.step_renderers.render_usage_block")
    def test_usage_block_rendered_when_present(self, mock_usage):
        from se3.engine.step_renderers import render_step_output

        # COMMIT renderer is simple and self-contained.
        step = _make_step(
            StepType.COMMIT,
            {"committed": False, "token_usage": self._USAGE},
        )
        render_step_output(step)

        mock_usage.assert_called_once()
        # First positional arg is the usage payload (the dict from outputs).
        assert mock_usage.call_args[0][0] == self._USAGE

    @patch("se3.engine.step_renderers.render_usage_block")
    def test_no_usage_block_when_absent(self, mock_usage):
        from se3.engine.step_renderers import render_step_output

        step = _make_step(StepType.COMMIT, {"committed": False})
        render_step_output(step)

        mock_usage.assert_not_called()

    @patch("se3.engine.step_renderers.render_usage_block")
    def test_no_usage_block_when_empty(self, mock_usage):
        from se3.engine.step_renderers import render_step_output

        step = _make_step(StepType.COMMIT, {"committed": False, "token_usage": {}})
        render_step_output(step)

        mock_usage.assert_not_called()

    @patch("se3.engine.step_renderers.render_usage_block")
    def test_usage_block_for_default_rendered_step(self, mock_usage):
        """A step type with no custom renderer still gets its usage block."""
        from se3.engine.step_renderers import render_step_output

        step = _make_step(
            StepType.PROJECT_SUMMARY,
            {"some_field": "x", "token_usage": self._USAGE},
        )
        render_step_output(step)

        mock_usage.assert_called_once()
