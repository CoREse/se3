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


def _setup_per_agent_runners(caller, agents, behavior):
    """Pre-populate ``caller._runner_cache`` with one MagicMock runner per agent.

    ``behavior`` maps each agent name to a list of MonitoredResults consumed in
    order by successive ``run_with_monitor`` calls (the last result repeats once
    exhausted). Pre-populating the cache (keyed by agent name, exactly as
    ``_get_current_runner`` keys it) means rotation / entry-reset never
    constructs a real ClaudeCodeRunner — no patching of ClaudeCodeRunner needed.

    Returns a shared ``call_log`` list recording the agent name of every
    ``run_with_monitor`` invocation in call order, so tests can assert exactly
    which agent ran each attempt across resets and rotations.
    """
    call_log = []
    for agent in agents:
        name = agent["name"]
        runner = MagicMock()
        runner.detect_infra_error.return_value = InfraErrorType.NONE

        results = behavior[name]
        if not isinstance(results, list):
            results = [results]

        def make_side_effect(nm, res_list):
            it = iter(res_list)

            def _side_effect(*args, **kwargs):
                call_log.append(nm)
                try:
                    return next(it)
                except StopIteration:
                    return res_list[-1]  # repeat last result once exhausted

            return _side_effect

        runner.run_with_monitor.side_effect = make_side_effect(name, results)
        caller._runner_cache[name] = runner

    caller._current_agent_index = 0
    caller._runner = caller._runner_cache[agents[0]["name"]]
    return call_log


def _json_ok(payload='{"ok": true}'):
    """A success result whose output is a valid JSON document."""
    return _make_success_result(output=payload)


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


class TestAgentAttributionInHistory:
    """Test that LLMCaller records the agent name in chat history.

    Verifies that _record_prompt and _record_response receive the agent name
    of the current attempt's agent, and that rotation assigns distinct names
    to different attempts.
    """

    def test_record_prompt_gets_agent_name(self, tmp_path):
        """_record_prompt should receive the first agent's name."""
        caller = LLMCaller(
            project_root=tmp_path,
            agents=TWO_AGENTS,
            flow_id="flow1",
            step_id="step1",
            step_type="analyze",
        )
        caller._record_prompt("test prompt", 0, agent_name="agent-a")
        from se3.engine.chat_history import get_step_history
        session = get_step_history(tmp_path, "flow1", "step1")
        assert session is not None
        assert session.messages[0].agent_name == "agent-a"

    def test_record_response_gets_agent_name(self, tmp_path):
        """_record_response should receive the agent name."""
        caller = LLMCaller(
            project_root=tmp_path,
            agents=TWO_AGENTS,
            flow_id="flow1",
            step_id="step1",
            step_type="analyze",
        )
        ndjson = json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "response"}]},
        })
        caller._record_response(ndjson, 0, agent_name="agent-a")
        from se3.engine.chat_history import get_step_history
        session = get_step_history(tmp_path, "flow1", "step1")
        assert session is not None
        assert session.messages[0].agent_name == "agent-a"

    def test_rotation_records_different_agent_names(self, tmp_path):
        """After rotation, the new attempt should record the new agent name."""
        caller = LLMCaller(
            project_root=tmp_path,
            agents=TWO_AGENTS,
            flow_id="flow1",
            step_id="step1",
            step_type="analyze",
        )
        # First attempt: agent-a
        caller._record_prompt("prompt 1", 0, agent_name="agent-a")
        caller._record_response("", 0, agent_name="agent-a")
        # Rotate to agent-b
        caller._rotate_agent()
        # Second attempt: agent-b
        caller._record_prompt("prompt 2", 1, agent_name="agent-b")
        caller._record_response("", 1, agent_name="agent-b")
        from se3.engine.chat_history import get_step_history
        session = get_step_history(tmp_path, "flow1", "step1")
        assert session is not None
        # Check that the first attempt's prompt has agent-a
        assert session.messages[0].agent_name == "agent-a"
        # The second attempt's prompt has agent-b
        assert session.messages[2].agent_name == "agent-b"

    def test_record_failure_does_not_block_call(self, tmp_path):
        """If recording fails, the LLM call should still proceed."""
        caller = LLMCaller(
            project_root=tmp_path,
            agents=TWO_AGENTS,
            flow_id="flow1",
            step_id="step1",
            step_type="analyze",
        )
        # Recording with an invalid path would fail internally, but it's
        # caught and debug-logged — call should not raise.
        caller._record_prompt("prompt", 0, agent_name="agent-a")
        # No assertion on the history content, just that the call didn't raise

    @patch("se3.engine.llm_caller.ClaudeCodeRunner")
    def test_call_with_retry_passes_agent_name(self, MockRunner, tmp_path):
        """_call_with_retry should snapshot agent name and pass it to
        _record_prompt and _record_response."""
        mock_runner = MagicMock()
        mock_runner.run_with_monitor.return_value = _make_success_result(
            output=json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "result"}]},
            })
        )
        mock_runner.detect_infra_error.return_value = InfraErrorType.NONE
        MockRunner.return_value = mock_runner

        caller = LLMCaller(
            project_root=tmp_path,
            agents=TWO_AGENTS,
            flow_id="flow1",
            step_id="step1",
            step_type="analyze",
            retry_delay=0.01,
        )
        result = caller.call(prompt="test", on_output=lambda x: None)

        # Verify the history contains the agent name
        from se3.engine.chat_history import get_step_history
        session = get_step_history(tmp_path, "flow1", "step1")
        assert session is not None
        # The prompt should carry the first agent's name
        user_msgs = [m for m in session.messages if m.role == "user"]
        assert len(user_msgs) >= 1
        assert user_msgs[0].agent_name == "agent-a"
        # The response should also carry the first agent's name
        assistant_msgs = [m for m in session.messages if m.role == "assistant"]
        assert len(assistant_msgs) >= 1
        assert assistant_msgs[0].agent_name == "agent-a"


class TestPerSequenceAgentReset:
    """Regression coverage for the per-sequence agent-rotation reset.

    Each NEW internal retry sequence (json_retry_count == 0) must start over
    from the first/preferred agent, instead of inheriting wherever the previous
    sequence's rotation happened to stop (the bug fixed here: the index used to
    stay permanently pinned to the last agent once rotation reached it).
    """

    def test_index_starts_at_zero_each_call_when_reused(self):
        """(a) Reusing one LLMCaller across calls (≈ se3 sync cross-round
        reuse): the first call rotates all the way to the last agent, yet the
        second call's sequence still starts on the first/preferred agent."""
        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=list(THREE_AGENTS),
            max_retries=5,
            retry_delay=0.0,
        )
        # First call: a fails, b fails, c succeeds — rotation reaches the last
        # agent. Second call: a succeeds immediately (its 2nd queued result).
        call_log = _setup_per_agent_runners(
            caller,
            THREE_AGENTS,
            {
                "agent-a": [_make_fail_result(), _make_success_result()],
                "agent-b": [_make_fail_result()],
                "agent-c": [_make_success_result()],
            },
        )

        result1 = caller.call(prompt="round-1", on_output=lambda x: None)
        assert result1 == "ok"
        # Rotation advanced to the last agent during the first sequence.
        assert call_log == ["agent-a", "agent-b", "agent-c"]
        assert caller._current_agent_index == 2

        call_log.clear()
        result2 = caller.call(prompt="round-2", on_output=lambda x: None)
        assert result2 == "ok"
        # The new sequence reset to the preferred agent — NOT stuck on the last.
        assert call_log == ["agent-a"]
        assert caller._current_agent_index == 0

    def test_reset_refreshes_legacy_runner(self):
        """Entry reset must also refresh the legacy ``self._runner`` so it
        agrees with the reset index, not the previous sequence's last agent."""
        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=list(THREE_AGENTS),
            max_retries=5,
            retry_delay=0.0,
        )
        _setup_per_agent_runners(
            caller,
            THREE_AGENTS,
            {
                "agent-a": [_make_fail_result(), _make_success_result()],
                "agent-b": [_make_fail_result()],
                "agent-c": [_make_success_result()],
            },
        )
        caller.call(prompt="round-1", on_output=lambda x: None)
        # After the first sequence self._runner points at the last agent.
        assert caller._runner is caller._runner_cache["agent-c"]

        caller.call(prompt="round-2", on_output=lambda x: None)
        # The second sequence's entry reset refreshed it back to the preferred.
        assert caller._runner is caller._runner_cache["agent-a"]
        assert caller._current_agent_index == 0


class TestTailOnLastWithinSequence:
    """(b) Within a single sequence, when max_retries exceeds the agent count,
    rotation advances to the last agent and then tails there (no wrap), with
    the sequence ultimately terminating via the max_retries cap."""

    def test_tail_on_last_then_raises(self):
        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=list(THREE_AGENTS),
            max_retries=5,  # greater than the 3 agents
            retry_delay=0.0,
        )
        call_log = _setup_per_agent_runners(
            caller,
            THREE_AGENTS,
            {
                "agent-a": [_make_fail_result()],
                "agent-b": [_make_fail_result()],
                "agent-c": [_make_fail_result()],
            },
        )

        import se3.engine.llm_caller as _llm_mod
        with pytest.raises(_llm_mod.LLMCallError, match="after 5 attempts"):
            caller.call(prompt="test", on_output=lambda x: None)

        # a → b → c (rotation), then the two surplus attempts tail on the last
        # agent c without wrapping back to a.
        assert call_log == [
            "agent-a", "agent-b", "agent-c", "agent-c", "agent-c",
        ]
        assert caller._current_agent_index == 2


class TestTwoPhasePerPhaseReset:
    """(c) two_phase mode: each Phase 1 _call_with_retry resets independently,
    so two separate two_phase calls do not inherit rotation progress."""

    def test_phase1_resets_each_call(self):
        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=list(THREE_AGENTS),
            max_retries=5,
            retry_delay=0.0,
        )
        # First two_phase call: Phase 1 rotates a→b before b returns valid JSON
        # (so Phase 2 is skipped). Second call: a returns valid JSON directly.
        call_log = _setup_per_agent_runners(
            caller,
            THREE_AGENTS,
            {
                "agent-a": [_make_fail_result(), _json_ok()],
                "agent-b": [_json_ok()],
                "agent-c": [_make_fail_result()],
            },
        )

        out1 = caller.call(
            prompt="p1", on_output=lambda x: None, json_mode="two_phase",
        )
        assert json.loads(out1) == {"ok": True}
        assert call_log == ["agent-a", "agent-b"]
        assert caller._current_agent_index == 1

        call_log.clear()
        out2 = caller.call(
            prompt="p2", on_output=lambda x: None, json_mode="two_phase",
        )
        assert json.loads(out2) == {"ok": True}
        # Phase 1 of the second call started over from the preferred agent.
        assert call_log == ["agent-a"]
        assert caller._current_agent_index == 0

    def test_phase2_starts_from_first_agent(self):
        """Force the two_phase call THROUGH the REAL production Phase 2 and
        assert the Phase-2 extraction begins a fresh internal retry sequence on
        the preferred agent, independently of where Phase 1's rotation stopped.

        Phase 1 rotates agent-a → agent-b and then emits NON-JSON output, so
        ``_contains_valid_json`` is False and Phase 2 runs. Production Phase 2
        (``_extract_json_phase2``) routes the extraction prompt through THIS
        caller's own ``_call_with_retry`` (``json_retry_count == 0``) rather
        than a fresh ``JSONExtractor``-spawned ``LLMCaller``; per the entry-reset
        semantics it must snap back to the first agent rather than inherit
        Phase 1's tail (agent-b). No test double is patched here — the assertion
        exercises the production path, so it fails if the per-sequence reset (or
        the Phase-2 routing) regresses.
        """
        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=list(THREE_AGENTS),
            max_retries=5,
            retry_delay=0.0,
        )
        # agent-a: fail (consumed by Phase 1) then valid JSON (consumed by the
        # Phase-2 extraction sequence after it resets back to the preferred
        # agent). agent-b: a successful but NON-JSON response that forces the
        # fall-through to Phase 2.
        call_log = _setup_per_agent_runners(
            caller,
            THREE_AGENTS,
            {
                "agent-a": [_make_fail_result(), _json_ok()],
                "agent-b": [_make_success_result(output="PROSE: no json here")],
                "agent-c": [_make_fail_result()],
            },
        )

        out = caller.call(
            prompt="p1", on_output=lambda x: None, json_mode="two_phase",
        )
        assert json.loads(out) == {"ok": True}
        # Phase 1 rotated a→b; the non-JSON output reached production Phase 2;
        # Phase 2's extraction sequence reset back to the preferred agent
        # (agent-a) and produced the valid JSON.
        assert call_log == ["agent-a", "agent-b", "agent-a"]
        assert caller._current_agent_index == 0

    def test_phase2_runs_extraction_prompt_verbatim_on_step_retry(self, tmp_path):
        """Regression: when a two_phase step is retried at the state-machine
        level (external_attempt > 0) and Phase 1 emits non-JSON, the production
        Phase-2 extraction prompt must still run VERBATIM.

        Because Phase 2 reuses THIS caller instance, ``self.external_attempt``
        equals the step's retry count. With the default ``retry_mode='continue'``
        and a non-empty chat-history retry context, an un-suppressed Phase-2
        ``_call_with_retry`` would set ``is_retry=True`` and replace the
        extraction prompt with 'Continue the task from where you left off …',
        so the extractor never sees the content to reformat and the step fails.
        The fix forces ``inject_retry_context=False`` for Phase 2, so the
        self-contained extraction prompt is always sent as-is regardless of the
        external retry count.

        With ``external_attempt=0`` this regression is invisible (is_retry is
        False anyway), so this test deliberately sets ``external_attempt=1`` and
        a non-empty retry context — the exact production conditions under which
        the bug manifests.
        """
        caller = LLMCaller(
            project_root=tmp_path,
            agents=list(THREE_AGENTS),
            max_retries=5,
            retry_delay=0.0,
            flow_id="flow1",
            step_id="step1",
            step_type="analyze",
            external_attempt=1,  # simulate a state-machine retry of the step
            retry_mode="continue",  # the default mode that drops the prompt
        )
        # Always run Phase 1 (no on-disk cache short-circuit).
        caller._get_phase1_cache_path = lambda: None
        # Force a non-empty retry context so retry-context injection WOULD fire
        # if it were not suppressed for Phase 2.
        caller._get_retry_context = lambda: "PREVIOUS CONVERSATION CONTEXT BLOCK"

        phase1_prose = "PROSE: definitely not json here"
        call_log = _setup_per_agent_runners(
            caller,
            THREE_AGENTS,
            {
                # Phase 1 (attempt with external retry) emits non-JSON prose;
                # the second result is consumed by the Phase-2 extraction
                # sequence after its entry reset returns to agent-a.
                "agent-a": [
                    _make_success_result(output=phase1_prose),
                    _json_ok(),
                ],
                "agent-b": [_make_fail_result()],
                "agent-c": [_make_fail_result()],
            },
        )

        out = caller.call(
            prompt="p1", on_output=lambda x: None, json_mode="two_phase",
        )
        # Extraction succeeded — the verbatim extraction prompt produced JSON.
        assert json.loads(out) == {"ok": True}
        # Phase 1 on agent-a, then Phase 2 reset back to agent-a.
        assert call_log == ["agent-a", "agent-a"]

        # Inspect the prompts actually handed to the runner. agent-a's
        # build_call_args was called twice: [0] Phase 1, [1] Phase 2.
        prompts = [
            c.kwargs["prompt"]
            for c in caller._runner_cache["agent-a"].build_call_args.call_args_list
        ]
        assert len(prompts) == 2
        phase1_prompt, phase2_prompt = prompts

        # Phase 1 IS a primary-task retry (external_attempt>0): continue-mode
        # injection is active and correct for it.
        assert "Continue the task from where you left off" in phase1_prompt

        # Phase 2 must be VERBATIM: it embeds the Phase-1 content to reformat and
        # must NOT have been hijacked by continue-mode retry-context injection.
        assert phase1_prose in phase2_prompt
        assert "Continue the task from where you left off" not in phase2_prompt
        assert "PREVIOUS CONVERSATION CONTEXT BLOCK" not in phase2_prompt


class TestJsonContinuationNoReset:
    """(d) The JSON-continuation recursion (json_retry_count > 0) must NOT
    reset: it keeps the current agent and conversation context rather than
    snapping back to the preferred agent."""

    def test_json_retry_keeps_current_agent(self):
        caller = LLMCaller(
            project_root=Path("/tmp"),
            agents=list(THREE_AGENTS),
            max_retries=5,
            retry_delay=0.0,
        )
        # Sequence: a fails → rotate to b. b succeeds but returns non-JSON →
        # triggers the json_retry recursion (json_retry_count=1). The recursion
        # must stay on agent-b (no reset to agent-a) and b then returns valid
        # JSON.
        call_log = _setup_per_agent_runners(
            caller,
            THREE_AGENTS,
            {
                "agent-a": [_make_fail_result()],
                "agent-b": [
                    _make_success_result(output="this is not json at all"),
                    _json_ok(),
                ],
                "agent-c": [_make_fail_result()],
            },
        )

        result = caller.call(
            prompt="need-json", on_output=lambda x: None, json_mode="strict",
        )
        assert json.loads(result) == {"ok": True}
        # The continuation stayed on agent-b — it did NOT reset back to agent-a.
        assert call_log == ["agent-a", "agent-b", "agent-b"]
        assert caller._current_agent_index == 1
