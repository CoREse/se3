"""Tests for _normalize_issue_fields (state_machine) and _format_fix_history (implement)."""

from __future__ import annotations

from se3.engine.state_machine import _normalize_issue_fields
from se3.engine.steps.implement import _format_fix_history


# ---------------------------------------------------------------------------
# _normalize_issue_fields
# ---------------------------------------------------------------------------

class TestNormalizeIssueFields:
    def test_adds_severity_from_priority(self):
        issues = [{"priority": "high", "message": "spec mismatch"}]
        result = _normalize_issue_fields(issues)
        assert result[0]["severity"] == "high"
        assert result[0]["priority"] == "high"

    def test_adds_priority_from_severity(self):
        issues = [{"severity": "medium", "description": "logic gap"}]
        result = _normalize_issue_fields(issues)
        assert result[0]["priority"] == "medium"
        assert result[0]["severity"] == "medium"

    def test_both_already_present_unchanged(self):
        issues = [{"severity": "low", "priority": "low"}]
        result = _normalize_issue_fields(issues)
        assert result[0]["severity"] == "low"
        assert result[0]["priority"] == "low"

    def test_neither_present_unchanged(self):
        issues = [{"description": "no severity or priority"}]
        result = _normalize_issue_fields(issues)
        assert "severity" not in result[0]
        assert "priority" not in result[0]

    def test_non_dict_items_skipped(self):
        issues = [{"severity": "high"}, "not-a-dict", 42, None]
        result = _normalize_issue_fields(issues)
        assert result[0]["priority"] == "high"
        assert result[1] == "not-a-dict"

    def test_empty_list(self):
        assert _normalize_issue_fields([]) == []

    def test_mutates_in_place(self):
        issues = [{"priority": "critical"}]
        result = _normalize_issue_fields(issues)
        assert result is issues
        assert issues[0]["severity"] == "critical"


# ---------------------------------------------------------------------------
# _format_fix_history
# ---------------------------------------------------------------------------

class TestFormatFixHistory:
    def test_empty_returns_no_attempts(self):
        assert _format_fix_history([]) == "No previous fix attempts."

    def test_none_returns_no_attempts(self):
        assert _format_fix_history(None) == "No previous fix attempts."

    def test_single_iteration_with_issues(self):
        history = [
            {
                "iteration": 1,
                "reason": "test_failure",
                "trigger_step_type": "test",
                "issues": [
                    {"severity": "high", "description": "assertion failed", "location": "test_auth.py:42"},
                ],
            }
        ]
        result = _format_fix_history(history)
        assert "Iteration 1" in result
        assert "triggered by test" in result
        assert "[high]" in result
        assert "assertion failed" in result
        assert "@ test_auth.py:42" in result

    def test_issues_capped_at_five(self):
        issues = [
            {"severity": "medium", "description": f"issue {i}", "location": ""}
            for i in range(8)
        ]
        history = [
            {
                "iteration": 1,
                "reason": "self_check",
                "trigger_step_type": "self_check",
                "issues": issues,
            }
        ]
        result = _format_fix_history(history)
        assert "issue 0" in result
        assert "issue 4" in result
        assert "issue 5" not in result
        assert "3 more issue(s)" in result

    def test_backward_compat_fix_instructions_summary(self):
        history = [
            {
                "iteration": 1,
                "reason": "test_failure",
                "trigger_step_type": "test",
                "fix_instructions_summary": "Old-style summary text",
            }
        ]
        result = _format_fix_history(history)
        assert "Old-style summary text" in result
        assert "Summary:" in result

    def test_issues_preferred_over_summary(self):
        """When both issues and fix_instructions_summary exist, issues win."""
        history = [
            {
                "iteration": 1,
                "reason": "test_failure",
                "trigger_step_type": "test",
                "issues": [{"severity": "high", "description": "real issue"}],
                "fix_instructions_summary": "should not appear",
            }
        ]
        result = _format_fix_history(history)
        assert "real issue" in result
        assert "should not appear" not in result

    def test_issue_uses_message_field_fallback(self):
        history = [
            {
                "iteration": 1,
                "reason": "spec_compliance",
                "trigger_step_type": "verify_spec",
                "issues": [{"severity": "high", "message": "spec violation"}],
            }
        ]
        result = _format_fix_history(history)
        assert "spec violation" in result

    def test_missing_fields_use_defaults(self):
        history = [{}]
        result = _format_fix_history(history)
        assert "Iteration ?" in result
        assert "unknown" in result

    def test_no_location_omits_at_suffix(self):
        history = [
            {
                "iteration": 1,
                "reason": "test_failure",
                "trigger_step_type": "test",
                "issues": [{"severity": "low", "description": "minor"}],
            }
        ]
        result = _format_fix_history(history)
        assert "@ " not in result

    def test_multiple_iterations(self):
        history = [
            {
                "iteration": 1,
                "reason": "test_failure",
                "trigger_step_type": "test",
                "issues": [{"severity": "high", "description": "first"}],
            },
            {
                "iteration": 2,
                "reason": "self_check",
                "trigger_step_type": "self_check",
                "issues": [{"severity": "medium", "description": "second"}],
            },
        ]
        result = _format_fix_history(history)
        assert "Iteration 1" in result
        assert "Iteration 2" in result
        assert "first" in result
        assert "second" in result

    def test_renders_new_schema_self_check_issues(self):
        """Regression: post-Commit-3 self_check stores new-schema issues
        (actual_behavior / divergence / evidence_lines) in fix_history.
        ``_format_fix_history`` must extract those — otherwise every
        historical issue line is empty (``  - [high]`` with no
        description or location), giving the next implement iteration
        no useful signal about prior bugs."""
        history = [
            {
                "iteration": 1,
                "reason": "self_check",
                "trigger_step_type": "self_check",
                "issues": [
                    {
                        "severity": "high",
                        "actual_behavior": "returns None on missing key",
                        "expected_behavior": "raises KeyError",
                        "divergence": "callers crash on iteration",
                        "evidence_lines": ["src/lookup.py:42"],
                        "missing_in": [],
                    },
                ],
            },
        ]
        result = _format_fix_history(history)
        assert "returns None on missing key" in result
        assert "callers crash on iteration" in result
        assert "src/lookup.py:42" in result
        assert "[high]" in result

    def test_renders_legacy_message_field(self):
        """verify_spec issues use legacy schema with ``message`` instead of
        ``description``. The extractor falls back to ``message``."""
        history = [
            {
                "iteration": 1,
                "reason": "spec_compliance",
                "trigger_step_type": "verify_spec",
                "issues": [
                    {
                        "severity": "high",
                        "message": "missing endpoint",
                        "location": "api.py:10",
                    },
                ],
            },
        ]
        result = _format_fix_history(history)
        assert "missing endpoint" in result
        assert "api.py:10" in result
