"""Tests for read-only step injection: STEP_POOL attribute, injection function, and LLMCaller integration."""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

from se3.engine.models import STEP_POOL, StepType, get_step_info
from se3.engine.context_builder import get_read_only_injection, is_step_read_only


# --- STEP_POOL completeness tests ---


class TestStepPoolReadOnly:
    """Verify all STEP_POOL entries have the read_only attribute."""

    def test_all_steps_have_read_only_key(self):
        """Every entry in STEP_POOL must contain a 'read_only' boolean key."""
        for step_type, info in STEP_POOL.items():
            assert "read_only" in info, f"{step_type.value} missing 'read_only' key"
            assert isinstance(info["read_only"], bool), (
                f"{step_type.value} 'read_only' is not bool: {type(info['read_only'])}"
            )

    def test_read_only_steps(self):
        """Steps that should be marked read_only=True."""
        expected_read_only = {
            StepType.DISCOVERY,
            StepType.ANALYZE,
            StepType.PROJECT_SUMMARY,
            StepType.PLAN,
            StepType.VERIFY_SPEC,
            StepType.VERSION_ANALYZE,
            StepType.SUMMARIZE,
            StepType.SELF_CHECK,
        }
        for step_type in expected_read_only:
            assert STEP_POOL[step_type]["read_only"] is True, (
                f"{step_type.value} should be read_only=True"
            )

    def test_non_read_only_steps(self):
        """Steps that should be marked read_only=False."""
        expected_non_read_only = {
            StepType.PROPOSE,
            StepType.DESIGN,
            StepType.PLAN_TASKS,
            StepType.CONFIRM,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.UPDATE_SPEC,
            StepType.COMMIT,
        }
        for step_type in expected_non_read_only:
            assert STEP_POOL[step_type]["read_only"] is False, (
                f"{step_type.value} should be read_only=False"
            )

    def test_discovery_is_read_only(self):
        """DISCOVERY must be read-only."""
        assert STEP_POOL[StepType.DISCOVERY]["read_only"] is True


# --- get_read_only_injection() tests ---


class TestGetReadOnlyInjection:
    """Verify the injection function returns correct results."""

    @pytest.mark.parametrize("step_name", [
        "discovery", "analyze", "project_summary", "plan",
        "verify_spec", "version_analyze", "summarize", "self_check",
    ])
    def test_read_only_steps_return_constraint(self, step_name):
        """Read-only steps should return a non-empty constraint prompt."""
        result = get_read_only_injection(step_name)
        assert result != ""
        assert "READ-ONLY" in result
        assert "MUST NOT modify" in result

    @pytest.mark.parametrize("step_name", [
        "implement", "test", "update_spec", "commit", "confirm",
    ])
    def test_non_read_only_steps_return_empty(self, step_name):
        """Non-read-only steps should return empty string."""
        result = get_read_only_injection(step_name)
        assert result == ""

    def test_unknown_step_returns_empty(self):
        """Unknown step_type should safely return empty string."""
        assert get_read_only_injection("merge_conflict") == ""
        assert get_read_only_injection("") == ""
        assert get_read_only_injection("nonexistent_step") == ""

    def test_constraint_forbids_write_edit(self):
        """Constraint text must explicitly forbid Write and Edit tools."""
        result = get_read_only_injection("analyze")
        assert "Write" in result
        assert "Edit" in result

    def test_constraint_allows_read_grep_glob(self):
        """Constraint text must allow Read, Grep, and Glob tools."""
        result = get_read_only_injection("analyze")
        assert "Read" in result
        assert "Grep" in result
        assert "Glob" in result


# --- Sync pseudo-step read-only classification (bugfix) ---


class TestSyncStepReadOnly:
    """sync_scan/sync_analyze are read-only sync pseudo-steps; sync_resolve
    is writable (its Way-A path edits se3/specs in place)."""

    def test_is_step_read_only_sync_scan_and_analyze(self):
        assert is_step_read_only("sync_scan") is True
        assert is_step_read_only("sync_analyze") is True

    def test_is_step_read_only_sync_resolve_is_false(self):
        assert is_step_read_only("sync_resolve") is False

    def test_is_step_read_only_writable_steps_false(self):
        assert is_step_read_only("implement") is False
        assert is_step_read_only("update_spec") is False
        assert is_step_read_only("commit") is False

    def test_is_step_read_only_pool_read_only_steps_true(self):
        assert is_step_read_only("analyze") is True
        assert is_step_read_only("plan") is True

    def test_is_step_read_only_unknown_step_false(self):
        assert is_step_read_only("totally_unknown_step") is False
        assert is_step_read_only("") is False

    def test_is_step_read_only_internal_pure_data_steps_true(self):
        # The code-index summariser and the migrate salvager are pure-data
        # sub-agents: they only read and return JSON/text while SE3 writes every
        # file itself, so they MUST run read-only (no Write/Edit tool) — otherwise
        # the agent can drop stray response files into the tree and pollute the
        # gitignore-respecting index. Regression guard for that.
        assert is_step_read_only("code_index") is True
        assert is_step_read_only("migrate") is True

    def test_injection_for_sync_scan_and_analyze_non_empty(self):
        assert get_read_only_injection("sync_scan") != ""
        assert "READ-ONLY" in get_read_only_injection("sync_scan")
        assert get_read_only_injection("sync_analyze") != ""
        assert "READ-ONLY" in get_read_only_injection("sync_analyze")

    def test_injection_for_sync_resolve_empty(self):
        assert get_read_only_injection("sync_resolve") == ""


# --- LLMCaller integration tests ---


class TestLLMCallerReadOnlyIntegration:
    """Verify LLMCaller.call() injects read-only constraint for read-only steps."""

    def _make_caller(self, step_type: str):
        """Create an LLMCaller with mocked runner for testing."""
        from se3.engine.llm_caller import LLMCaller

        caller = LLMCaller(
            project_root="/tmp/test_project",
            step_type=step_type,
            agents=[{"name": "test", "type": "claude-code", "cmd": "echo test"}],
        )
        return caller

    @patch("se3.engine.llm_caller.LLMCaller._call_with_retry")
    def test_read_only_step_prompt_contains_constraint(self, mock_call):
        """For read-only steps, the prompt passed to _call_with_retry should contain the constraint."""
        mock_call.return_value = "test output"
        caller = self._make_caller("analyze")

        caller.call("Analyze this task", json_mode="off")

        called_prompt = mock_call.call_args[1]["prompt"]
        assert "READ-ONLY STEP CONSTRAINT" in called_prompt
        assert "MUST NOT modify" in called_prompt

    @patch("se3.engine.llm_caller.LLMCaller._call_with_retry")
    def test_non_read_only_step_prompt_has_no_constraint(self, mock_call):
        """For non-read-only steps, no read-only constraint should be in the prompt."""
        mock_call.return_value = "test output"
        caller = self._make_caller("implement")

        caller.call("Implement this feature", json_mode="off")

        called_prompt = mock_call.call_args[1]["prompt"]
        assert "READ-ONLY STEP CONSTRAINT" not in called_prompt

    @patch("se3.engine.llm_caller.LLMCaller._call_strict")
    def test_read_only_injection_with_strict_mode(self, mock_call):
        """Read-only constraint is injected before mode dispatch, including strict mode."""
        mock_call.return_value = '{"result": "ok"}'
        caller = self._make_caller("plan")

        caller.call("Plan this task", json_mode="strict")

        called_prompt = mock_call.call_args[1]["prompt"]
        assert "READ-ONLY STEP CONSTRAINT" in called_prompt

    @patch("se3.engine.llm_caller.LLMCaller._call_two_phase")
    def test_read_only_injection_with_two_phase_mode(self, mock_call):
        """Read-only constraint is injected before mode dispatch, including two_phase mode."""
        mock_call.return_value = '{"result": "ok"}'
        caller = self._make_caller("version_analyze")

        caller.call("Analyze version", json_mode="two_phase")

        called_prompt = mock_call.call_args[1]["prompt"]
        assert "READ-ONLY STEP CONSTRAINT" in called_prompt

    @patch("se3.engine.llm_caller.LLMCaller._call_with_retry")
    def test_read_only_injection_after_extra_prompt(self, mock_call):
        """Read-only constraint should appear after extra_prompt injection."""
        from se3.engine.llm_caller import set_extra_prompt
        mock_call.return_value = "test output"

        try:
            set_extra_prompt("User extra instruction")
            caller = self._make_caller("analyze")
            caller.call("Analyze this", json_mode="off")

            called_prompt = mock_call.call_args[1]["prompt"]
            extra_pos = called_prompt.find("User extra instruction")
            constraint_pos = called_prompt.find("READ-ONLY STEP CONSTRAINT")
            assert extra_pos < constraint_pos, (
                "Read-only constraint should appear after extra_prompt"
            )
        finally:
            from se3.engine.llm_caller import clear_extra_prompt
            clear_extra_prompt()
