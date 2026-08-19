"""Tests for step_renderers custom renderers.

Tests _render_analyze, _render_verify_spec, _render_update_spec, and _render_commit.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tianluo.engine.models import Step, StepStatus, StepType


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
    @patch("tianluo.engine.step_renderers.render_full")
    def test_normal_output(self, mock_render_full):
        step = _make_step(StepType.ANALYZE, {
            "task_type": "feature",
            "complexity": "medium",
            "scope": "src/tianluo/engine/step_renderers.py",
            "reasoning": "This task modifies rendering logic.",
            "selected_items": [
                {"spec": "flow-engine", "requirement_name": "FE-3"},
                {"spec": "base", "requirement_name": "Project Identity"},
            ],
            "spec_content": "long spec content...",
            "project_summary": "long project summary...",
        })

        from tianluo.engine.step_renderers import _render_analyze
        _render_analyze(step)

        mock_render_full.assert_called_once()
        content = mock_render_full.call_args[0][0]

        # Status bar shows task_type, complexity, scope
        assert "feature" in content
        assert "medium" in content
        assert "src/tianluo/engine/step_renderers.py" in content

        # Reasoning displayed
        assert "This task modifies rendering logic." in content

        # Relevant Spec Items listed as spec:requirement_name
        assert "Relevant Spec Items" in content
        assert "flow-engine:FE-3" in content
        assert "base:Project Identity" in content

        # Internal fields NOT displayed
        assert "long spec content..." not in content
        assert "long project summary..." not in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_partial_fields_missing(self, mock_render_full):
        step = _make_step(StepType.ANALYZE, {
            "task_type": "bugfix",
        })

        from tianluo.engine.step_renderers import _render_analyze
        _render_analyze(step)

        content = mock_render_full.call_args[0][0]
        assert "bugfix" in content
        assert "N/A" in content  # missing complexity/scope default to N/A

    @patch("tianluo.engine.step_renderers.render_full")
    def test_empty_outputs(self, mock_render_full):
        step = _make_step(StepType.ANALYZE, {})

        from tianluo.engine.step_renderers import _render_analyze
        _render_analyze(step)

        content = mock_render_full.call_args[0][0]
        assert "N/A" in content  # all defaults

    @patch("tianluo.engine.step_renderers.render_full")
    def test_selected_items_rendering(self, mock_render_full):
        step = _make_step(StepType.ANALYZE, {
            "task_type": "feature",
            "selected_items": [
                {"spec": "flow-engine", "requirement_name": "FE-3"},
                {"spec": "base", "requirement_name": "Project Identity"},
            ],
        })

        from tianluo.engine.step_renderers import _render_analyze
        _render_analyze(step)

        content = mock_render_full.call_args[0][0]
        assert "Relevant Spec Items" in content
        assert "flow-engine:FE-3" in content
        assert "base:Project Identity" in content

    @patch("tianluo.engine.step_renderers.render_full")
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

        from tianluo.engine.step_renderers import _render_analyze
        _render_analyze(step)

        content = mock_render_full.call_args[0][0]
        assert "Relevant Spec Items" in content
        assert "flow-engine:FE-3" in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_no_selected_items_no_section(self, mock_render_full):
        step = _make_step(StepType.ANALYZE, {
            "task_type": "feature",
            "complexity": "simple",
            "scope": "tiny",
            "selected_items": [],
        })

        from tianluo.engine.step_renderers import _render_analyze
        _render_analyze(step)

        content = mock_render_full.call_args[0][0]
        assert "Relevant Spec Items" not in content


# ---------------------------------------------------------------------------
# _render_verify_spec
# ---------------------------------------------------------------------------


class TestRenderVerifySpec:
    @patch("tianluo.engine.step_renderers.render_full")
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

        from tianluo.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "PASSED" in content
        assert "All checks passed." in content

        # Internal fields not displayed
        assert "fix_context" not in content
        assert "fix_iteration" not in content
        assert "verification_result" not in content

    @patch("tianluo.engine.step_renderers.render_full")
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

        from tianluo.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "FAILED" in content
        assert "Missing function" in content
        assert "Add the function" in content
        assert "Unused import" in content
        assert "Style note" in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_no_issues(self, mock_render_full):
        step = _make_step(StepType.VERIFY_SPEC, {
            "verified": True,
            "issues": [],
        })

        from tianluo.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "PASSED" in content
        # No scope/priority group headers when no issues
        assert "In-scope" not in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_with_recommendations(self, mock_render_full):
        step = _make_step(StepType.VERIFY_SPEC, {
            "verified": True,
            "recommendations": ["Consider adding more tests", "Update docs"],
        })

        from tianluo.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "Consider adding more tests" in content
        assert "Update docs" in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_infer_verified_from_fix_needed(self, mock_render_full):
        """When 'verified' key is absent, infer from fix_needed."""
        step = _make_step(StepType.VERIFY_SPEC, {
            "fix_needed": False,
        })

        from tianluo.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "PASSED" in content

    @patch("tianluo.engine.step_renderers.render_full")
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

        from tianluo.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "Nested summary text" in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_recommendations_from_verification_result(self, mock_render_full):
        """recommendations should fall back to verification_result nested dict."""
        step = _make_step(StepType.VERIFY_SPEC, {
            "verified": True,
            "verification_result": {
                "verified": True,
                "recommendations": ["Use type hints", "Add docstrings"],
            },
        })

        from tianluo.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "Use type hints" in content
        assert "Add docstrings" in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_toplevel_summary_takes_precedence(self, mock_render_full):
        """When top-level summary exists, it should be used over nested one."""
        step = _make_step(StepType.VERIFY_SPEC, {
            "verified": True,
            "summary": "Top-level summary",
            "verification_result": {
                "summary": "Nested summary",
            },
        })

        from tianluo.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "Top-level summary" in content

    @patch("tianluo.engine.step_renderers.render_full")
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

        from tianluo.engine.step_renderers import _render_verify_spec
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
    @patch("tianluo.engine.step_renderers.render_full")
    def test_with_updates(self, mock_render_full):
        step = _make_step(StepType.UPDATE_SPEC, {
            "updated_specs": [
                {"spec_name": "flow-engine", "change_description": "Added new requirement"},
                {"spec_name": "base", "change_description": "Updated conventions"},
            ],
        })

        from tianluo.engine.step_renderers import _render_update_spec
        _render_update_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "flow-engine" in content
        assert "Added new requirement" in content
        assert "base" in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_no_updates(self, mock_render_full):
        step = _make_step(StepType.UPDATE_SPEC, {})

        from tianluo.engine.step_renderers import _render_update_spec
        _render_update_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "No spec updates needed" in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_specs_updated_key_compat(self, mock_render_full):
        """Accept both 'updated_specs' and 'specs_updated' keys."""
        step = _make_step(StepType.UPDATE_SPEC, {
            "specs_updated": [
                {"spec_name": "base", "change_description": "Minor tweak"},
            ],
        })

        from tianluo.engine.step_renderers import _render_update_spec
        _render_update_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "base" in content
        assert "Minor tweak" in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_with_new_capabilities(self, mock_render_full):
        step = _make_step(StepType.UPDATE_SPEC, {
            "updated_specs": [{"spec_name": "x", "change_description": "y"}],
            "new_capabilities": ["Streaming support", "Batch mode"],
        })

        from tianluo.engine.step_renderers import _render_update_spec
        _render_update_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "Streaming support" in content
        assert "Batch mode" in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_string_specs(self, mock_render_full):
        step = _make_step(StepType.UPDATE_SPEC, {
            "updated_specs": ["flow-engine updated", "base updated"],
        })

        from tianluo.engine.step_renderers import _render_update_spec
        _render_update_spec(step)

        content = mock_render_full.call_args[0][0]
        assert "flow-engine updated" in content


# ---------------------------------------------------------------------------
# _render_commit
# ---------------------------------------------------------------------------


class TestRenderCommit:
    @patch("tianluo.engine.step_renderers.render_full")
    def test_committed_false(self, mock_render_full):
        step = _make_step(StepType.COMMIT, {"committed": False})

        from tianluo.engine.step_renderers import _render_commit
        _render_commit(step)

        content = mock_render_full.call_args[0][0]
        assert "No changes to commit" in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_committed_true_no_version(self, mock_render_full):
        step = _make_step(StepType.COMMIT, {
            "committed": True,
            "commit_hash": "abc1234def5678",
            "commit_message": "feat: add new renderers",
        })

        from tianluo.engine.step_renderers import _render_commit
        _render_commit(step)

        content = mock_render_full.call_args[0][0]
        assert "abc1234" in content
        assert "feat: add new renderers" in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_committed_true_with_version(self, mock_render_full):
        step = _make_step(StepType.COMMIT, {
            "committed": True,
            "commit_hash": "abc1234def5678",
            "commit_message": "feat: version bump",
            "version_bumped": True,
            "version": "1.3.0",
        })

        from tianluo.engine.step_renderers import _render_commit
        _render_commit(step)

        content = mock_render_full.call_args[0][0]
        assert "abc1234" in content
        assert "v1.3.0" in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_missing_commit_hash(self, mock_render_full):
        step = _make_step(StepType.COMMIT, {
            "committed": True,
        })

        from tianluo.engine.step_renderers import _render_commit
        _render_commit(step)

        content = mock_render_full.call_args[0][0]
        assert "N/A" in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_empty_outputs_defaults_to_no_commit(self, mock_render_full):
        step = _make_step(StepType.COMMIT, {})

        from tianluo.engine.step_renderers import _render_commit
        _render_commit(step)

        content = mock_render_full.call_args[0][0]
        assert "No changes to commit" in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_error_message_is_never_hidden_by_the_no_op_shortcut(self, mock_render_full):
        """A failed step must show its diagnostic, not "No changes to commit"."""
        step = _make_step(StepType.COMMIT, {"committed": False})
        step.error_message = (
            "failed to create version tag v2.0.0 on commit abc1234def5678: "
            "git command failed (exit 128): tag already exists"
        )

        from tianluo.engine.step_renderers import _render_commit
        _render_commit(step)

        content = mock_render_full.call_args[0][0]
        assert "No changes to commit" not in content
        assert "v2.0.0" in content
        assert "abc1234def5678" in content
        assert "tag already exists" in content


# ---------------------------------------------------------------------------
# _render_self_check
# ---------------------------------------------------------------------------


class TestRenderSelfCheck:
    @patch("tianluo.engine.step_renderers.render_full")
    def test_failed_status_with_zero_actionable_shows_failed(self, mock_render_full):
        """FAILED status with actionable_count=0 should show FAILED, not PASSED."""
        step = _make_step(
            StepType.SELF_CHECK,
            outputs={"actionable_count": 0},
            status=StepStatus.FAILED,
            error_message="Failed to parse self-check result from LLM response",
        )

        from tianluo.engine.step_renderers import _render_self_check
        _render_self_check(step)

        content = mock_render_full.call_args[0][0]
        assert "FAILED" in content
        assert "PASSED" not in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_completed_status_with_zero_actionable_shows_passed(self, mock_render_full):
        """COMPLETED status with no issues should show PASSED."""
        step = _make_step(
            StepType.SELF_CHECK,
            outputs={"actionable_count": 0, "issues": [], "self_check_result": {"summary": "All good"}},
            status=StepStatus.COMPLETED,
        )

        from tianluo.engine.step_renderers import _render_self_check
        _render_self_check(step)

        content = mock_render_full.call_args[0][0]
        assert "PASSED" in content
        assert "FAILED" not in content

    @patch("tianluo.engine.step_renderers.render_full")
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

        from tianluo.engine.step_renderers import _render_self_check
        _render_self_check(step)

        content = mock_render_full.call_args[0][0]
        assert "FAILED" in content
        assert "PASSED" not in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_failed_status_no_outputs_shows_failed(self, mock_render_full):
        """FAILED status with empty outputs (pre-failure) should show FAILED."""
        step = _make_step(
            StepType.SELF_CHECK,
            outputs={},
            status=StepStatus.FAILED,
            error_message="Self-check failed: LLM call error",
        )

        from tianluo.engine.step_renderers import _render_self_check
        _render_self_check(step)

        content = mock_render_full.call_args[0][0]
        assert "FAILED" in content
        assert "PASSED" not in content


# ---------------------------------------------------------------------------
# _render_spec_gate
# ---------------------------------------------------------------------------

# A raw pytest-style blob that MUST NOT appear in any spec_gate render output.
_RAW_TEST_OUTPUT = (
    "============================= test session starts =====================\n"
    "tests/test_foo.py::test_bar FAILED\n"
    "E   AssertionError: assert 44 == 45\n"
    "----------------------------- Captured stderr ------------------------\n"
    "Traceback (most recent call last): ...raw stderr dump...\n"
)

_TEST_RESULTS_FAIL = {
    "overall_passed": False,
    "passed": False,
    "command": "python -m pytest -v",
    "phases": [
        {"name": "default", "passed": False},
        {"name": "e2e", "passed": True},
    ],
    # Fields a naive full-dump renderer would surface (the bug being fixed):
    "stdout": _RAW_TEST_OUTPUT,
    "stderr": "raw stderr dump...",
}

_TEST_RESULTS_PASS = {
    "overall_passed": True,
    "passed": True,
    "command": "python -m pytest -v",
    "phases": [{"name": "default", "passed": True}],
    "stdout": _RAW_TEST_OUTPUT,
}


class TestRenderSpecGate:
    @patch("tianluo.engine.step_renderers.render_full")
    def test_registered_in_registry(self, _mock_render_full):
        """SPEC_GATE has a registered renderer — render_step_output won't default-render."""
        from tianluo.engine.step_renderers import STEP_RENDERERS, STEP_TITLE_KEYS

        assert StepType.SPEC_GATE in STEP_RENDERERS
        assert StepType.SPEC_GATE in STEP_TITLE_KEYS

    @patch("tianluo.engine.step_renderers.render_full")
    def test_gate_passed_clean(self, mock_render_full):
        step = _make_step(StepType.SPEC_GATE, {
            "gate_passed": True,
            "gate_route": "",
            "fix_needed": False,
            "test_results": _TEST_RESULTS_PASS,
        })

        from tianluo.engine.step_renderers import _render_spec_gate
        _render_spec_gate(step)

        content = mock_render_full.call_args[0][0]
        assert "PASSED" in content
        assert "FAILED" not in content
        # Summary present, raw output absent.
        assert "python -m pytest -v" in content
        assert _RAW_TEST_OUTPUT not in content
        assert "raw stderr dump" not in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_gate_skipped_noop(self, mock_render_full):
        step = _make_step(StepType.SPEC_GATE, {
            "gate_passed": True,
            "gate_route": "",
            "gate_skipped": True,
            "fix_needed": False,
        })

        from tianluo.engine.step_renderers import _render_spec_gate
        _render_spec_gate(step)

        content = mock_render_full.call_args[0][0]
        assert "PASSED" in content
        assert "skipped" in content.lower() or "no-op" in content.lower()

    @patch("tianluo.engine.step_renderers.render_full")
    def test_route_update_spec(self, mock_render_full):
        step = _make_step(StepType.SPEC_GATE, {
            "gate_passed": False,
            "gate_route": "update_spec",
            "fix_needed": True,
            "fix_instructions": "Re-apply the intended spec update.",
        })

        from tianluo.engine.step_renderers import _render_spec_gate
        _render_spec_gate(step)

        content = mock_render_full.call_args[0][0]
        assert "FAILED" in content
        assert "update_spec" in content
        assert "Re-apply the intended spec update." in content
        # No test_results → no re-test summary, definitely no raw output.
        assert _RAW_TEST_OUTPUT not in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_route_implement_with_test_results(self, mock_render_full):
        step = _make_step(StepType.SPEC_GATE, {
            "gate_passed": False,
            "gate_route": "implement",
            "fix_needed": True,
            "fix_instructions": "Update the stale assertion 44 → 45.",
            "test_results": _TEST_RESULTS_FAIL,
        })

        from tianluo.engine.step_renderers import _render_spec_gate
        _render_spec_gate(step)

        content = mock_render_full.call_args[0][0]
        assert "FAILED" in content
        assert "implement" in content
        # Phase summary rendered (status + phase list + command).
        assert "1 passed, 1 failed" in content
        assert "python -m pytest -v" in content
        # Raw stdout/stderr never leak.
        assert _RAW_TEST_OUTPUT not in content
        assert "raw stderr dump" not in content
        assert "Traceback" not in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_no_test_results_renders_conclusion_only(self, mock_render_full):
        step = _make_step(StepType.SPEC_GATE, {
            "gate_passed": True,
            "gate_route": "",
            "fix_needed": False,
        })

        from tianluo.engine.step_renderers import _render_spec_gate
        _render_spec_gate(step)

        content = mock_render_full.call_args[0][0]
        assert "PASSED" in content


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

    @patch("tianluo.engine.step_renderers.render_usage_block")
    def test_usage_block_rendered_when_present(self, mock_usage):
        from tianluo.engine.step_renderers import render_step_output

        # COMMIT renderer is simple and self-contained.
        step = _make_step(
            StepType.COMMIT,
            {"committed": False, "token_usage": self._USAGE},
        )
        render_step_output(step)

        mock_usage.assert_called_once()
        # First positional arg is the usage payload (the dict from outputs).
        assert mock_usage.call_args[0][0] == self._USAGE

    @patch("tianluo.engine.step_renderers.render_usage_block")
    def test_no_usage_block_when_absent(self, mock_usage):
        from tianluo.engine.step_renderers import render_step_output

        step = _make_step(StepType.COMMIT, {"committed": False})
        render_step_output(step)

        mock_usage.assert_not_called()

    @patch("tianluo.engine.step_renderers.render_usage_block")
    def test_no_usage_block_when_empty(self, mock_usage):
        from tianluo.engine.step_renderers import render_step_output

        step = _make_step(StepType.COMMIT, {"committed": False, "token_usage": {}})
        render_step_output(step)

        mock_usage.assert_not_called()

    @patch("tianluo.engine.step_renderers.render_usage_block")
    def test_usage_block_for_default_rendered_step(self, mock_usage):
        """A step type with no custom renderer still gets its usage block."""
        from tianluo.engine.step_renderers import render_step_output

        step = _make_step(
            StepType.PROJECT_SUMMARY,
            {"some_field": "x", "token_usage": self._USAGE},
        )
        render_step_output(step)

        mock_usage.assert_called_once()

    @patch("tianluo.engine.step_renderers.render_usage_block")
    def test_usage_block_rendered_for_non_terminal_step(self, mock_usage):
        """A REVISION_NEEDED step (e.g. self_check) that now has token_usage
        in outputs (written by G2's run_step fix) renders the usage block —
        both CLI and WebUI read the same `outputs.token_usage` field."""
        from tianluo.engine.step_renderers import render_step_output

        step = _make_step(
            StepType.SELF_CHECK,
            {"result": "revision_needed", "token_usage": self._USAGE},
            status=StepStatus.REVISION_NEEDED,
        )
        render_step_output(step)

        mock_usage.assert_called_once()
        assert mock_usage.call_args[0][0] == self._USAGE

    @patch("tianluo.engine.step_renderers.render_usage_summary_block")
    @patch("tianluo.engine.step_renderers.render_usage_block")
    def test_usage_summary_wins_over_the_legacy_projection(
        self, mock_legacy, mock_summary,
    ):
        """A step whose calls report tokens but no provider cost must not
        print a fabricated $0: the shared UsageSummary owns the cost column,
        and the legacy five-field projection collapses a missing cost to 0.0."""
        from tianluo.engine.step_renderers import render_step_usage

        summary = {
            "totals": {"logical_input_tokens": 100, "output_tokens": 10},
            "actual_cost_usd": None,
            "estimated_cost_usd": None,
            "completeness": "partial",
            "unknown_call_count": 0,
        }
        step = _make_step(
            StepType.SELF_CHECK,
            {"token_usage": self._USAGE, "usage_summary": summary},
            status=StepStatus.COMPLETED,
        )
        render_step_usage(step)

        mock_summary.assert_called_once()
        assert mock_summary.call_args[0][0] == summary
        mock_legacy.assert_not_called()

    @patch("tianluo.engine.step_renderers.render_usage_block")
    def test_usage_block_not_read_from_carried_token_usage(self, mock_usage):
        """render_step_usage reads ONLY outputs.token_usage, never the
        internal carried_token_usage — confirming the G2 convention that
        carried_token_usage is an engine-internal carry field, not a display
        source."""
        from tianluo.engine.step_renderers import render_step_usage

        # A step with carried_token_usage but no token_usage renders nothing.
        step = _make_step(
            StepType.SELF_CHECK,
            {"result": "revision_needed", "carried_token_usage": self._USAGE},
            status=StepStatus.REVISION_NEEDED,
        )
        render_step_usage(step)

        mock_usage.assert_not_called()


# ---------------------------------------------------------------------------
# _render_implement
# ---------------------------------------------------------------------------


class TestRenderImplement:
    """Regression coverage for the IMPLEMENT report card.

    The i18n migration shadowed the imported ``t()`` helper with a ``for t in
    tests_added`` loop variable, making every call in the function raise
    UnboundLocalError. EventEmitter.emit swallows sink exceptions, so the report
    card and the trailing token-usage block vanished from the console silently.
    These tests pin the renderer's real end-to-end path (render_step_output, not
    just the private function) so a raising renderer fails loudly here.
    """

    _OUTPUTS = {
        "completion_status": "complete",
        "summary": "Add i18n loader and wire the CLI",
        "group_summaries": [
            {"group_id": "G1", "summary": "Add i18n loader and wire the CLI"},
        ],
        "files_changed": ["src/tianluo/cli.py", "src/tianluo/i18n/loader.py"],
        "tests_added": ["tests/test_i18n.py"],
        "implemented_groups": ["G1"],
    }

    @patch("tianluo.engine.step_renderers.render_full")
    def test_renders_full_report_card(self, mock_render_full):
        from tianluo.engine.step_renderers import _render_implement

        step = _make_step(StepType.IMPLEMENT, dict(self._OUTPUTS))
        _render_implement(step)

        mock_render_full.assert_called_once()
        content = mock_render_full.call_args[0][0]

        assert "Add i18n loader and wire the CLI" in content
        assert "src/tianluo/cli.py" in content
        # The tests-added loop must render its entries, not shadow t().
        assert "tests/test_i18n.py" in content
        # t() resolved, so no raw dotted key leaked into the output.
        assert "cli.steprender." not in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_tests_added_loop_does_not_shadow_translator(self, mock_render_full):
        """The tests_added section is the exact site of the shadowing bug: with
        a non-empty list, every later t() call must still be the i18n helper."""
        from tianluo.engine.step_renderers import _render_implement

        step = _make_step(StepType.IMPLEMENT, {
            "completion_status": "partial",
            "files_changed": ["a.py"],
            "tests_added": ["tests/test_a.py", "tests/test_b.py"],
            # Sections rendered AFTER the loop — these were unreachable before.
            "incomplete_tasks": [{"task_id": "T2", "reason": "blocked"}],
        })
        _render_implement(step)

        content = mock_render_full.call_args[0][0]
        assert "tests/test_a.py" in content
        assert "tests/test_b.py" in content
        assert "T2" in content and "blocked" in content

    @patch("tianluo.engine.step_renderers.render_usage_block")
    @patch("tianluo.engine.step_renderers.render_full")
    def test_usage_block_follows_report(self, mock_render_full, mock_usage):
        """render_step_output must reach the token-usage block: a renderer that
        raises would drop it (the observed regression)."""
        from tianluo.engine.step_renderers import render_step_output

        usage = {"input_tokens": 10, "output_tokens": 20}
        outputs = dict(self._OUTPUTS)
        outputs["token_usage"] = usage
        step = _make_step(StepType.IMPLEMENT, outputs)

        render_step_output(step)

        mock_render_full.assert_called_once()
        mock_usage.assert_called_once()
        assert mock_usage.call_args[0][0] == usage

    @patch("tianluo.engine.step_renderers.render_full")
    def test_group_summaries_are_labelled_by_real_group_id(self, mock_render_full):
        """The summary section must name each group by its real group_id.

        Regression: the renderer split the aggregate ``summary`` string on ";"
        and labelled the fragments G1…Gn by position, so a group summary that
        itself contained a semicolon inflated the group count and produced
        labels for groups PLAN never emitted.
        """
        from tianluo.engine.step_renderers import _render_implement

        step = _make_step(StepType.IMPLEMENT, {
            "completion_status": "complete",
            "summary": "added a; wired b; fixed c; covered d",
            "group_summaries": [
                {"group_id": "G2", "summary": "added a; wired b"},
                {"group_id": "G5", "summary": "fixed c; covered d"},
            ],
            "implemented_groups": ["G2", "G5"],
            "files_changed": [],
            "tests_added": [],
        })
        _render_implement(step)

        content = mock_render_full.call_args[0][0]
        assert "[dim]G2.[/dim] added a; wired b" in content
        assert "[dim]G5.[/dim] fixed c; covered d" in content
        # Exactly two labelled groups — no positional numbering survives.
        assert content.count("[/dim] ") == 2
        for bogus in ("G1.", "G3.", "G4.", "G6.", "G7."):
            assert f"[dim]{bogus}[/dim]" not in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_single_group_summary_renders_unlabelled(self, mock_render_full):
        from tianluo.engine.step_renderers import _render_implement

        step = _make_step(StepType.IMPLEMENT, {
            "completion_status": "complete",
            "summary": "only; part",
            "group_summaries": [{"group_id": "G1", "summary": "only; part"}],
            "implemented_groups": ["G1"],
            "files_changed": [],
            "tests_added": [],
        })
        _render_implement(step)

        content = mock_render_full.call_args[0][0]
        assert "only; part" in content
        assert "[dim]G1.[/dim]" not in content

    @patch("tianluo.engine.step_renderers.render_full")
    def test_legacy_summary_only_flow_renders_whole_string(self, mock_render_full):
        """Flows recorded before ``group_summaries`` existed keep the string
        whole — no splitting, no numbering."""
        from tianluo.engine.step_renderers import _render_implement

        step = _make_step(StepType.IMPLEMENT, {
            "completion_status": "complete",
            "summary": "alpha; beta; gamma",
            "implemented_groups": ["G1", "G2", "G3"],
            "files_changed": [],
            "tests_added": [],
        })
        _render_implement(step)

        content = mock_render_full.call_args[0][0]
        assert "alpha; beta; gamma" in content
        for bogus in ("G1.", "G2.", "G3.", "1.", "2.", "3."):
            assert f"[dim]{bogus}[/dim]" not in content
