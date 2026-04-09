"""Tests for step_renderers custom renderers.

Tests _render_analyze, _render_verify_spec, _render_update_spec, and _render_commit.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from se3.engine.models import Step, StepStatus, StepType


def _make_step(step_type: StepType, outputs: dict, error_message: str | None = None) -> Step:
    """Create a Step with given type and outputs."""
    step = Step(step_type=step_type, status=StepStatus.COMPLETED)
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
            "relevant_specs": ["flow-engine", "base"],
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

        # Relevant specs listed
        assert "flow-engine" in content
        assert "base" in content

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
    def test_relevant_specs_as_dicts(self, mock_render_full):
        step = _make_step(StepType.ANALYZE, {
            "task_type": "feature",
            "relevant_specs": [
                {"name": "flow-engine", "relevance": "high"},
                {"spec_name": "base"},
            ],
        })

        from se3.engine.step_renderers import _render_analyze
        _render_analyze(step)

        content = mock_render_full.call_args[0][0]
        assert "flow-engine" in content
        assert "base" in content


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
                {"severity": "error", "message": "Missing function", "suggestion": "Add the function"},
                {"severity": "warning", "message": "Unused import"},
                {"severity": "info", "message": "Style note"},
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
        # No severity group headers
        assert "error" not in content.lower().replace("error_message", "")

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
