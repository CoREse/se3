"""Tests for fix-context formatting helpers.

Covers:
- verify_spec._format_fix_context (fix iteration context)
- verify_spec._format_previous_verification (previous round issues)
- implement._format_fix_context_structured (structured fix context dispatch)
"""

from __future__ import annotations

from se3.engine.steps._fix_context import (
    FIX_HISTORY_RENDER_TAIL,
    render_fix_context,
)
from se3.engine.steps.verify_spec import (
    _format_fix_context,
    _format_previous_verification,
)
from se3.engine.steps.implement import _format_fix_context_structured


class TestFormatFixContext:

    def test_initial_verification(self):
        result = _format_fix_context(0, 3)
        assert "initial verification" in result.lower()

    def test_basic_iteration(self):
        result = _format_fix_context(2, 5)
        assert "2 of 5" in result
        assert "Previous fix attempts: 2" in result

    def test_final_iteration_warning(self):
        result = _format_fix_context(3, 3)
        assert "final fix-loop iteration" in result or "WARNING" in result

    def test_with_fix_history(self):
        history = [
            {"iteration": 1, "reason": "test_failure", "trigger_step_type": "test"},
            {"iteration": 2, "reason": "spec_compliance", "trigger_step_type": "verify_spec"},
        ]
        result = _format_fix_context(2, 5, fix_history=history)
        assert "Fix History" in result
        assert "test_failure" in result
        assert "spec_compliance" in result

    def test_with_empty_fix_history(self):
        result = _format_fix_context(1, 3, fix_history=[])
        assert "Fix History" not in result

    def test_with_none_fix_history(self):
        result = _format_fix_context(1, 3, fix_history=None)
        assert "Fix History" not in result

    def test_unlimited_mid_loop_renders_unlimited_marker(self):
        # max_iterations=0 (unlimited sentinel) with fix_iteration > 0:
        # the iteration line MUST render as "Fix iteration: N (unlimited)"
        # and NEITHER the on-boundary "final fix-loop iteration" warning
        # NOR the past-final "Iteration cap exceeded" warning may appear.
        # This is the most user-visible payoff of the unlimited mode and
        # locks the rendering against a future copy-edit regression.
        result = render_fix_context(
            fix_iteration=7,
            max_iterations=0,
            step_label="verification",
        )
        assert "Fix iteration: 7 (unlimited)" in result
        assert "final fix-loop iteration" not in result
        assert "Iteration cap exceeded" not in result
        # Sanity: previous-fix-attempts counter still rendered.
        assert "Previous fix attempts: 7" in result

    def test_fix_history_tail_truncation(self):
        # Drive >FIX_HISTORY_RENDER_TAIL entries through render_fix_context
        # and assert (a) only the last N iteration numbers appear and
        # (b) the truncation marker is present.
        total = 38
        history = [
            {
                "iteration": i,
                "reason": "test_failure",
                "trigger_step_type": "test",
            }
            for i in range(1, total + 1)
        ]
        result = render_fix_context(
            fix_iteration=total,
            max_iterations=0,
            step_label="verification",
            fix_history=history,
        )
        # The first (total - FIX_HISTORY_RENDER_TAIL) iterations must be elided.
        kept_start = total - FIX_HISTORY_RENDER_TAIL + 1
        for elided in range(1, kept_start):
            assert f"Iteration {elided}:" not in result, (
                f"Iteration {elided} should be truncated"
            )
        for kept in range(kept_start, total + 1):
            assert f"Iteration {kept}:" in result, (
                f"Iteration {kept} should be present"
            )
        # Truncation marker
        truncated_count = total - FIX_HISTORY_RENDER_TAIL
        assert f"{truncated_count} earlier entries (truncated)" in result

    def test_fix_history_no_truncation_marker_at_or_below_tail(self):
        # When length is exactly FIX_HISTORY_RENDER_TAIL, no marker.
        history = [
            {"iteration": i, "reason": "r", "trigger_step_type": "test"}
            for i in range(1, FIX_HISTORY_RENDER_TAIL + 1)
        ]
        result = render_fix_context(
            fix_iteration=FIX_HISTORY_RENDER_TAIL,
            max_iterations=0,
            step_label="verification",
            fix_history=history,
        )
        assert "earlier entries (truncated)" not in result


class TestFormatPreviousVerification:

    def test_empty_issues(self):
        assert _format_previous_verification(None, None) == ""
        assert _format_previous_verification([], None) == ""

    def test_basic_issues(self):
        issues = [
            {"scope": "in_scope", "priority": "high", "message": "Missing validation"},
            {"scope": "in_scope", "priority": "medium", "message": "Wrong return type"},
        ]
        result = _format_previous_verification(issues, None)
        assert "Previous Verification" in result
        assert "Missing validation" in result
        assert "Wrong return type" in result
        assert "[high/in_scope]" in result

    def test_with_fix_instructions(self):
        issues = [{"scope": "in_scope", "priority": "high", "message": "Bug"}]
        result = _format_previous_verification(issues, "Fix the bug by adding a null check")
        assert "Fix Instructions" in result
        assert "null check" in result

    def test_truncation_at_20_issues(self):
        issues = [{"message": f"Issue {i}", "priority": "low", "scope": "in_scope"} for i in range(25)]
        result = _format_previous_verification(issues, None)
        assert "Issue 19" in result
        assert "Issue 20" not in result
        assert "5 more issues" in result

    def test_fix_instructions_truncated(self):
        issues = [{"message": "Bug", "priority": "high", "scope": "in_scope"}]
        long_instructions = "x" * 5000
        result = _format_previous_verification(issues, long_instructions)
        assert len(result) < 5000


class TestFormatFixContextStructured:

    def test_none_input(self):
        assert _format_fix_context_structured(None) == "No additional context."

    def test_empty_dict(self):
        assert _format_fix_context_structured({}) == "No additional context."

    def test_string_passthrough(self):
        assert _format_fix_context_structured("just a string") == "just a string"

    def test_test_failure_reason(self):
        ctx = {
            "reason": "test_failure",
            "test_analysis": {
                "failure_summary": "AssertionError in test_foo",
                "root_cause": "Off-by-one in loop",
            },
        }
        result = _format_fix_context_structured(ctx)
        assert "test_failure" in result
        assert "AssertionError" in result
        assert "Off-by-one" in result

    def test_spec_compliance_reason(self):
        ctx = {
            "reason": "spec_compliance",
            "spec_issues": [
                {"priority": "high", "message": "Missing endpoint"},
                {"priority": "medium", "message": "Wrong status code"},
            ],
        }
        result = _format_fix_context_structured(ctx)
        assert "spec_compliance" in result
        assert "Missing endpoint" in result
        assert "Wrong status code" in result

    def test_self_check_reason(self):
        ctx = {
            "reason": "self_check",
            "issues": [
                {"severity": "high", "description": "SQL injection risk", "location": "api.py:42"},
            ],
        }
        result = _format_fix_context_structured(ctx)
        assert "self_check" in result
        assert "SQL injection" in result
        assert "api.py:42" in result

    def test_unknown_reason_returns_reason_line(self):
        ctx = {"reason": "custom_reason"}
        result = _format_fix_context_structured(ctx)
        assert "custom_reason" in result
