"""Tests for the spec-write-protection soft injection (G1).

Covers the derived exemption set (``SPEC_WRITE_ALLOWED_STEPS``), the
``get_spec_write_protection_injection`` helper, and its integration into
``LLMCaller.call()``. The constraint forbids *writing spec files* in every
non-read-only LLM step except ``update_spec`` and the sync steps, while
explicitly leaving behavior change free.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from se3.engine.context_builder import (
    SPEC_WRITE_ALLOWED_STEPS,
    _ALL_SYNC_STEPS,
    _READ_ONLY_SYNC_STEPS,
    _WRITABLE_SYNC_STEPS,
    _is_spec_write_protected_step,
    get_spec_write_protection_injection,
)


# --- Derived exemption set ---


class TestSpecWriteAllowedSteps:
    """The exemption set must be derived from the authoritative sync sets."""

    def test_writable_sync_steps_value(self):
        assert _WRITABLE_SYNC_STEPS == frozenset({"sync_resolve", "sync_respond"})

    def test_read_only_sync_steps_value(self):
        assert _READ_ONLY_SYNC_STEPS == frozenset({"sync_scan", "sync_analyze"})

    def test_all_sync_steps_is_union(self):
        assert _ALL_SYNC_STEPS == _READ_ONLY_SYNC_STEPS | _WRITABLE_SYNC_STEPS

    def test_spec_write_allowed_steps_is_derived(self):
        # Guards against re-introducing a hand-enumerated set that could drift
        # (e.g. dropping sync_respond).
        assert SPEC_WRITE_ALLOWED_STEPS == (
            frozenset({"update_spec"}) | _READ_ONLY_SYNC_STEPS | _WRITABLE_SYNC_STEPS
        )

    def test_spec_write_allowed_steps_membership(self):
        assert SPEC_WRITE_ALLOWED_STEPS == frozenset(
            {
                "update_spec",
                "sync_scan",
                "sync_analyze",
                "sync_resolve",
                "sync_respond",
            }
        )


# --- get_spec_write_protection_injection ---


class TestGetSpecWriteProtectionInjection:
    @pytest.mark.parametrize(
        "step_name", ["implement", "propose", "design", "plan_tasks"]
    )
    def test_protected_steps_return_constraint(self, step_name):
        injection = get_spec_write_protection_injection(step_name)
        assert injection != ""
        assert "SPEC FILE WRITE PROTECTION" in injection

    def test_update_spec_returns_empty(self):
        assert get_spec_write_protection_injection("update_spec") == ""

    @pytest.mark.parametrize(
        "step_name", ["sync_scan", "sync_analyze", "sync_resolve", "sync_respond"]
    )
    def test_all_sync_steps_return_empty(self, step_name):
        assert get_spec_write_protection_injection(step_name) == ""

    @pytest.mark.parametrize(
        "step_name",
        [
            "plan",
            "analyze",
            "verify_spec",
            "summarize",
            "commit",
            "test",
            "confirm",
            "discovery",
            "version_analyze",
            "self_check",
            "project_summary",
        ],
    )
    def test_read_only_or_non_llm_steps_return_empty(self, step_name):
        assert get_spec_write_protection_injection(step_name) == ""

    def test_unknown_step_returns_empty(self):
        assert get_spec_write_protection_injection("nonexistent_step") == ""

    def test_constraint_allows_behavior_change(self):
        """Wording must explicitly permit changing existing behavior."""
        injection = get_spec_write_protection_injection("implement")
        lowered = injection.lower()
        assert "free to change existing code behavior" in lowered
        # And it must point the behavior-change channel at plan/update_spec.
        assert "spec_changes" in injection
        assert "update_spec" in injection

    def test_constraint_forbids_spec_writes(self):
        injection = get_spec_write_protection_injection("implement")
        assert "se3/specs" in injection
        for tool in ("Write", "Edit", "NotebookEdit", "Bash"):
            assert tool in injection


class TestIsSpecWriteProtectedStep:
    def test_protected_steps(self):
        for step in ("implement", "propose", "design", "plan_tasks"):
            assert _is_spec_write_protected_step(step) is True

    def test_exempt_and_read_only_steps(self):
        for step in (
            "update_spec",
            "sync_scan",
            "sync_analyze",
            "sync_resolve",
            "sync_respond",
            "analyze",
            "plan",
            "verify_spec",
            "summarize",
            "test",
            "commit",
            "unknown_step",
        ):
            assert _is_spec_write_protected_step(step) is False


# --- LLMCaller integration ---


class TestLLMCallerSpecWriteIntegration:
    def _make_caller(self, step_type: str):
        from se3.engine.llm_caller import LLMCaller

        return LLMCaller(
            project_root="/tmp/test_project",
            step_type=step_type,
            agents=[{"name": "test", "type": "claude-code", "cmd": "echo test"}],
        )

    @patch("se3.engine.llm_caller.LLMCaller._call_with_retry")
    def test_protected_step_prompt_contains_constraint(self, mock_call):
        mock_call.return_value = "test output"
        caller = self._make_caller("implement")
        caller.call("Implement this feature", json_mode="off")
        called_prompt = mock_call.call_args[1]["prompt"]
        assert "SPEC FILE WRITE PROTECTION" in called_prompt

    @patch("se3.engine.llm_caller.LLMCaller._call_with_retry")
    def test_update_spec_prompt_has_no_constraint(self, mock_call):
        mock_call.return_value = "test output"
        caller = self._make_caller("update_spec")
        caller.call("Update the spec", json_mode="off")
        called_prompt = mock_call.call_args[1]["prompt"]
        assert "SPEC FILE WRITE PROTECTION" not in called_prompt

    @patch("se3.engine.llm_caller.LLMCaller._call_with_retry")
    def test_read_only_step_prompt_has_no_spec_write_constraint(self, mock_call):
        mock_call.return_value = "test output"
        caller = self._make_caller("analyze")
        caller.call("Analyze this", json_mode="off")
        called_prompt = mock_call.call_args[1]["prompt"]
        assert "SPEC FILE WRITE PROTECTION" not in called_prompt
        # The read-only constraint still applies to analyze.
        assert "READ-ONLY STEP CONSTRAINT" in called_prompt

    @patch("se3.engine.llm_caller.LLMCaller._call_two_phase")
    def test_protected_step_two_phase_mode(self, mock_call):
        mock_call.return_value = '{"result": "ok"}'
        caller = self._make_caller("implement")
        caller.call("Implement this feature", json_mode="two_phase")
        called_prompt = mock_call.call_args[1]["prompt"]
        assert "SPEC FILE WRITE PROTECTION" in called_prompt
