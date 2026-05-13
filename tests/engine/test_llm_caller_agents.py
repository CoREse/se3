"""Tests for LLMCaller agent management and rotation.

Verifies:
- Agent list initialization from config or explicit parameter
- Infrastructure error triggers agent rotation
- Task failure does NOT trigger rotation
- All agents exhausted behavior
- Runner factory and caching
- Single agent scenario
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from se3.agent_runner import InfraErrorType
from se3.engine.llm_caller import LLMCaller, LLMCallError


def _make_success_result(output="ok"):
    """Create a mock MonitoredResult indicating success."""
    result = MagicMock()
    result.success = True
    result.output = output
    result.returncode = 0
    result.cmd_used = "claude"
    result.interrupted = False
    return result


def _make_fail_result(returncode=1, output="error", cmd_used="claude"):
    """Create a mock MonitoredResult indicating failure."""
    result = MagicMock()
    result.success = False
    result.output = output
    result.returncode = returncode
    result.cmd_used = cmd_used
    result.interrupted = False
    return result


TWO_AGENTS = [
    {"name": "agent-a", "type": "claude-code", "cmd": "claude-a", "priority": 10},
    {"name": "agent-b", "type": "claude-code", "cmd": "claude-b", "priority": 5},
]

THREE_AGENTS = [
    {"name": "agent-a", "type": "claude-code", "cmd": "claude-a", "priority": 10},
    {"name": "agent-b", "type": "claude-code", "cmd": "claude-b", "priority": 5},
    {"name": "agent-c", "type": "claude-code", "cmd": "claude-c", "priority": 1},
]


class TestAgentInitialization:
    """Test LLMCaller agent list initialization."""

    def test_explicit_agents_parameter(self):
        """Explicit agents parameter should be used directly."""
        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=TWO_AGENTS,
        )
        assert caller._agents == TWO_AGENTS
        assert caller._current_agent_index == 0

    def test_default_loads_from_config(self, tmp_path):
        """Without agents param, should load from config."""
        with patch("se3.config.Path.home", return_value=tmp_path):
            caller = LLMCaller(project_root=tmp_path)
        assert len(caller._agents) >= 1
        assert caller._agents[0]["type"] == "claude-code"


class TestCreateRunner:
    """Test runner factory method."""

    def test_creates_claude_code_runner(self):
        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=TWO_AGENTS,
        )
        runner = caller._create_runner(TWO_AGENTS[0])
        from se3.claude_runner import ClaudeCodeRunner
        assert isinstance(runner, ClaudeCodeRunner)
        assert runner.command["cmd"] == "claude-a"

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown agent type"):
            LLMCaller(
                project_root=Path("/tmp"),
                agents=[{"name": "x", "type": "unknown", "cmd": "x", "priority": 0}],
            )

    def test_runner_caching(self):
        """Same agent should return cached runner."""
        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=TWO_AGENTS,
        )
        runner1 = caller._get_current_runner()
        runner2 = caller._get_current_runner()
        assert runner1 is runner2


class TestRotateAgent:
    """Test agent rotation."""

    def test_rotate_increments_index(self):
        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=TWO_AGENTS,
        )
        assert caller._current_agent_index == 0
        result = caller._rotate_agent()
        assert result is True
        assert caller._current_agent_index == 1

    def test_rotate_exhausted_returns_false(self):
        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=TWO_AGENTS,
        )
        caller._current_agent_index = 1  # Already at last
        result = caller._rotate_agent()
        assert result is False
        assert caller._current_agent_index == 1

    def test_single_agent_cannot_rotate(self):
        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=[TWO_AGENTS[0]],
        )
        result = caller._rotate_agent()
        assert result is False


class TestInfraErrorRotation:
    """Test that infrastructure errors trigger agent rotation."""

    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
    def test_usage_limit_triggers_rotation(self, MockRunner):
        """Usage limit should rotate to next agent and retry."""
        # First agent fails with usage limit, second succeeds
        mock_runner_a = MagicMock()
        mock_runner_a.run_with_monitor.return_value = _make_fail_result(
            returncode=1, output="usage limit exceeded"
        )
        mock_runner_a.detect_infra_error.return_value = InfraErrorType.USAGE_LIMIT

        mock_runner_b = MagicMock()
        mock_runner_b.run_with_monitor.return_value = _make_success_result()
        mock_runner_b.detect_infra_error.return_value = InfraErrorType.NONE

        # MockRunner is called for each agent
        MockRunner.side_effect = [mock_runner_a, mock_runner_b]

        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=TWO_AGENTS,
            retry_delay=0.01,
        )

        result = caller.call(prompt="test", on_output=lambda x: None)
        assert result == "ok"
        assert caller._current_agent_index == 1

    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
    def test_timeout_triggers_rotation(self, MockRunner):
        """Timeout should rotate to next agent."""
        mock_runner_a = MagicMock()
        mock_runner_a.run_with_monitor.return_value = _make_fail_result(returncode=124)
        mock_runner_a.detect_infra_error.return_value = InfraErrorType.TIMEOUT

        mock_runner_b = MagicMock()
        mock_runner_b.run_with_monitor.return_value = _make_success_result()
        mock_runner_b.detect_infra_error.return_value = InfraErrorType.NONE

        MockRunner.side_effect = [mock_runner_a, mock_runner_b]

        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=TWO_AGENTS,
            retry_delay=0.01,
        )

        result = caller.call(prompt="test", on_output=lambda x: None)
        assert result == "ok"
        assert caller._current_agent_index == 1


class TestOtherErrorRotation:
    """Test that OTHER (unclassified) errors also trigger agent rotation."""

    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
    def test_other_error_rotates_to_next_agent(self, MockRunner):
        """Unclassified failure (detect_infra_error=NONE) should rotate."""
        mock_runner_a = MagicMock()
        mock_runner_a.run_with_monitor.return_value = _make_fail_result(
            returncode=1, output="file not found"
        )
        mock_runner_a.detect_infra_error.return_value = InfraErrorType.NONE

        mock_runner_b = MagicMock()
        mock_runner_b.run_with_monitor.return_value = _make_success_result()
        mock_runner_b.detect_infra_error.return_value = InfraErrorType.NONE

        MockRunner.side_effect = [mock_runner_a, mock_runner_b]

        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=TWO_AGENTS,
            max_retries=3,
            retry_delay=0.01,
        )

        result = caller.call(prompt="test", on_output=lambda x: None)
        assert result == "ok"
        assert caller._current_agent_index == 1

    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
    def test_unknown_certificate_error_triggers_rotation(self, MockRunner):
        """UNKNOWN_CERTIFICATE_VERIFICATION_ERROR (classified as NONE) should rotate."""
        cert_output = (
            "API Error: Unable to connect to API "
            "(UNKNOWN_CERTIFICATE_VERIFICATION_ERROR)"
        )
        mock_runner_a = MagicMock()
        mock_runner_a.run_with_monitor.return_value = _make_fail_result(
            returncode=1, output=cert_output
        )
        mock_runner_a.detect_infra_error.return_value = InfraErrorType.NONE

        mock_runner_b = MagicMock()
        mock_runner_b.run_with_monitor.return_value = _make_success_result()
        mock_runner_b.detect_infra_error.return_value = InfraErrorType.NONE

        MockRunner.side_effect = [mock_runner_a, mock_runner_b]

        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=TWO_AGENTS,
            max_retries=3,
            retry_delay=0.01,
        )

        result = caller.call(prompt="test", on_output=lambda x: None)
        assert result == "ok"
        # After the first failure, we should have rotated to the second agent.
        assert caller._current_agent_index == 1


class TestAllAgentsExhausted:
    """Test behavior when all agents are exhausted."""

    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
    def test_raises_after_exhaustion(self, MockRunner):
        """When all agents fail with infra errors, should raise LLMCallError."""
        mock_runner_a = MagicMock()
        mock_runner_a.run_with_monitor.return_value = _make_fail_result(
            returncode=1, output="usage limit"
        )
        mock_runner_a.detect_infra_error.return_value = InfraErrorType.USAGE_LIMIT

        mock_runner_b = MagicMock()
        mock_runner_b.run_with_monitor.return_value = _make_fail_result(
            returncode=1, output="usage limit"
        )
        mock_runner_b.detect_infra_error.return_value = InfraErrorType.USAGE_LIMIT

        # __init__ creates runner for first agent, rotation creates second
        MockRunner.side_effect = [mock_runner_a, mock_runner_b]

        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=list(TWO_AGENTS),  # fresh copy to avoid cross-test mutation
            max_retries=2,
            retry_delay=0.01,
        )
        # Clear the runner cache so rotation creates a fresh runner from mock
        caller._runner_cache.clear()
        MockRunner.side_effect = [mock_runner_a, mock_runner_b]
        caller._current_agent_index = 0
        caller._runner = mock_runner_a

        # Import LLMCallError fresh in case module was reloaded by other tests
        import se3.engine.llm_caller as _llm_mod
        with pytest.raises(_llm_mod.LLMCallError):
            caller.call(prompt="test", on_output=lambda x: None)


class TestSingleAgentScenario:
    """Test with only one agent (backward compat)."""

    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
    def test_single_agent_success(self, MockRunner):
        mock_runner = MagicMock()
        mock_runner.run_with_monitor.return_value = _make_success_result()
        MockRunner.return_value = mock_runner

        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=[TWO_AGENTS[0]],
        )

        result = caller.call(prompt="test", on_output=lambda x: None)
        assert result == "ok"

    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
    def test_single_agent_infra_error_no_rotation(self, MockRunner):
        """With single agent, infra error cannot rotate — falls through to retry."""
        mock_runner = MagicMock()
        fail_result = _make_fail_result(returncode=1, output="usage limit")
        success_result = _make_success_result()
        mock_runner.run_with_monitor.side_effect = [fail_result, success_result]
        mock_runner.detect_infra_error.side_effect = [
            InfraErrorType.USAGE_LIMIT,
            InfraErrorType.NONE,
        ]
        MockRunner.return_value = mock_runner

        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=[TWO_AGENTS[0]],
            max_retries=3,
            retry_delay=0.01,
        )

        result = caller.call(prompt="test", on_output=lambda x: None)
        assert result == "ok"
        assert caller._current_agent_index == 0

    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
    def test_single_agent_other_error_falls_through(self, MockRunner):
        """With single agent, OTHER error cannot rotate — falls through to same-agent retry."""
        mock_runner = MagicMock()
        fail_result = _make_fail_result(returncode=1, output="file not found")
        success_result = _make_success_result()
        mock_runner.run_with_monitor.side_effect = [fail_result, success_result]
        mock_runner.detect_infra_error.return_value = InfraErrorType.NONE
        MockRunner.return_value = mock_runner

        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=[TWO_AGENTS[0]],
            max_retries=3,
            retry_delay=0.01,
        )

        result = caller.call(prompt="test", on_output=lambda x: None)
        assert result == "ok"
        # Single agent can't rotate; fallthrough retries on same agent.
        assert caller._current_agent_index == 0
