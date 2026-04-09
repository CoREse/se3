"""Tests for _render_verify_spec renderer fixes.

Verifies:
1. summary fallback from verification_result nested dict
2. recommendations fallback from verification_result nested dict
3. Top-level fields take precedence over nested ones
4. Rich close tag format correct for [red], [yellow], [dim]
"""

from __future__ import annotations

from unittest.mock import patch

from se3.engine.models import Step, StepStatus, StepType


def _make_step(outputs: dict) -> Step:
    step = Step(step_type=StepType.VERIFY_SPEC, status=StepStatus.COMPLETED)
    step.outputs = outputs
    return step


class TestSummaryFallback:
    """summary should fall back to outputs['verification_result']['summary']."""

    @patch("se3.engine.step_renderers.render_full")
    def test_reads_from_nested_verification_result(self, mock_render):
        step = _make_step({
            "verified": True,
            "verification_result": {
                "verified": True,
                "summary": "All scenarios verified successfully",
            },
        })
        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render.call_args[0][0]
        assert "All scenarios verified successfully" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_toplevel_takes_precedence(self, mock_render):
        step = _make_step({
            "verified": True,
            "summary": "Top-level summary wins",
            "verification_result": {
                "summary": "Nested summary loses",
            },
        })
        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render.call_args[0][0]
        assert "Top-level summary wins" in content
        # Nested should NOT appear when top-level exists
        assert "Nested summary loses" not in content

    @patch("se3.engine.step_renderers.render_full")
    def test_no_summary_anywhere(self, mock_render):
        step = _make_step({"verified": True})
        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render.call_args[0][0]
        # Should still render without error
        assert "PASSED" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_verification_result_not_dict(self, mock_render):
        """When verification_result is a string, don't crash."""
        step = _make_step({
            "verified": True,
            "verification_result": "some string",
        })
        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render.call_args[0][0]
        assert "PASSED" in content


class TestRecommendationsFallback:
    """recommendations should fall back to verification_result dict."""

    @patch("se3.engine.step_renderers.render_full")
    def test_reads_from_nested_verification_result(self, mock_render):
        step = _make_step({
            "verified": True,
            "verification_result": {
                "recommendations": ["Add integration tests", "Update changelog"],
            },
        })
        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render.call_args[0][0]
        assert "Add integration tests" in content
        assert "Update changelog" in content

    @patch("se3.engine.step_renderers.render_full")
    def test_toplevel_takes_precedence(self, mock_render):
        step = _make_step({
            "verified": True,
            "recommendations": ["Top rec A"],
            "verification_result": {
                "recommendations": ["Nested rec B"],
            },
        })
        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render.call_args[0][0]
        assert "Top rec A" in content
        assert "Nested rec B" not in content


class TestRichCloseTag:
    """Rich close tags must not have extra ] characters."""

    @patch("se3.engine.step_renderers.render_full")
    def test_red_close_tag(self, mock_render):
        step = _make_step({
            "verified": False,
            "issues": [{"severity": "error", "message": "broken"}],
        })
        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render.call_args[0][0]
        assert "[/red]" in content
        assert "[/red]]" not in content

    @patch("se3.engine.step_renderers.render_full")
    def test_yellow_close_tag(self, mock_render):
        step = _make_step({
            "verified": False,
            "issues": [{"severity": "warning", "message": "risky"}],
        })
        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render.call_args[0][0]
        assert "[/yellow]" in content
        assert "[/yellow]]" not in content

    @patch("se3.engine.step_renderers.render_full")
    def test_dim_close_tag(self, mock_render):
        step = _make_step({
            "verified": False,
            "issues": [{"severity": "info", "message": "note"}],
        })
        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render.call_args[0][0]
        assert "[/dim]" in content
        assert "[/dim]]" not in content

    @patch("se3.engine.step_renderers.render_full")
    def test_all_severities_correct_tags(self, mock_render):
        """All three severity levels produce correct close tags."""
        step = _make_step({
            "verified": False,
            "issues": [
                {"severity": "error", "message": "err"},
                {"severity": "warning", "message": "warn"},
                {"severity": "info", "message": "info"},
            ],
        })
        from se3.engine.step_renderers import _render_verify_spec
        _render_verify_spec(step)

        content = mock_render.call_args[0][0]
        # All close tags correct
        for tag in ("[/red]", "[/yellow]", "[/dim]"):
            assert tag in content
        # No malformed double-bracket close tags
        for bad in ("[/red]]", "[/yellow]]", "[/dim]]"):
            assert bad not in content
