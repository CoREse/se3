"""Tests for LLMCaller per-step agent override integration.

Verifies the integration wiring in ``LLMCaller.__init__`` for the
``llm_caller.steps.<step_type>`` override:

- When a step declares an override, the caller uses ONLY that list.
- When a step does not declare an override, the caller uses the default
  chain from ``load_agents``.
- Exhausting the override list does NOT fall back to the default chain.
- An explicit ``agents=...`` argument beats both the override and the
  default chain.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tianluo.agent_runner import InfraErrorType
from tianluo.engine.llm_caller import LLMCaller, LLMCallError


def _make_fail_result(returncode=1, output="usage limit", cmd_used="claude"):
    result = MagicMock()
    result.success = False
    result.output = output
    result.returncode = returncode
    result.cmd_used = cmd_used
    result.interrupted = False
    return result


def _make_success_result(output="ok"):
    result = MagicMock()
    result.success = True
    result.output = output
    result.returncode = 0
    result.cmd_used = "claude"
    result.interrupted = False
    return result


def _write_override(tmp_path, step, agents_yaml, registry=None):
    """Helper to write se3.yaml with a registry + step override.

    ``agents_yaml`` is the YAML body below the step key (a list of
    agent name references). ``registry`` defines the top-level agents
    dict; when None, a default registry with ``override-a``,
    ``override-b``, and ``solo-override`` is written.
    """
    if registry is None:
        registry = (
            "agents:\n"
            "  override-a: {cmd: claude-a, priority: 10}\n"
            "  override-b: {cmd: claude-b, priority: 5}\n"
            "  solo-override: {cmd: override-claude, priority: 10}\n"
        )
    (tmp_path / "se3.yaml").write_text(
        f"{registry}llm_caller:\n  steps:\n    {step}:\n{agents_yaml}"
    )


class TestStepOverrideChain:
    def test_override_used_when_declared(self, tmp_path):
        _write_override(
            tmp_path, "implement",
            "      - override-a\n"
            "      - override-b\n",
        )
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            caller = LLMCaller(
                project_root=tmp_path,
                step_type="implement",
            )
        # Only the two override agents — the default claude is NOT
        # appended as a fallback tail.
        assert [a["name"] for a in caller._agents] == [
            "override-a",
            "override-b",
        ]

    def test_other_step_falls_back_to_default_chain(self, tmp_path):
        # Only 'implement' has an override.
        _write_override(
            tmp_path, "implement",
            "      - override-a\n",
        )
        which_claude_only = patch(
            "tianluo.config.shutil.which",
            side_effect=lambda cmd, *a, **k: (
                "/fake/bin/claude" if cmd == "claude" else None
            ),
        )
        with patch("tianluo.config.Path.home", return_value=tmp_path), which_claude_only:
            # Running the 'plan' step — no override declared — gets the
            # built-in default chain. which() is pinned to claude only so the
            # chain does not vary with the host's installed agents.
            caller = LLMCaller(project_root=tmp_path, step_type="plan")

        assert len(caller._agents) == 1
        assert caller._agents[0]["name"] == "claude"
        assert caller._agents[0]["cmd"] == "claude"


class TestExhaustionDoesNotFallBack:
    @patch("tianluo.engine.llm_caller.ClaudeCodeRunner")
    def test_override_exhaustion_raises_without_falling_back(
        self, MockRunner, tmp_path,
    ):
        """When every override agent hits an infra error, the call must
        fail rather than rotating into the default chain.

        Regression guard for the central correctness property of the
        per-step override: override-is-a-hard-override.

        Isolation invariant: agent list is frozen at ``__init__`` — this
        test patches ``Path.home`` only during LLMCaller construction,
        not during ``caller.call(...)``. If a future refactor moves
        agent loading into ``call()`` (e.g. for lazy reload or per-retry
        config refresh), the real ``~/.se3/config.yaml`` on the dev
        machine could leak into the call path and make this test flaky
        — that refactor must also rework this test's patch scope.
        """
        # A single-agent override. Exhausting it should raise — we must
        # NOT silently fall through to the default chain's 'claude'.
        _write_override(
            tmp_path, "implement",
            "      - solo-override\n",
        )

        mock_runner = MagicMock()
        mock_runner.run_with_monitor.return_value = _make_fail_result(
            returncode=1, output="usage limit"
        )
        mock_runner.detect_infra_error.return_value = InfraErrorType.USAGE_LIMIT
        MockRunner.return_value = mock_runner

        with patch("tianluo.config.Path.home", return_value=tmp_path):
            caller = LLMCaller(
                project_root=tmp_path,
                step_type="implement",
                max_retries=2,
                retry_delay=0.0,
            )

        # Invariant: caller's chain is exactly the single override agent.
        assert [a["name"] for a in caller._agents] == ["solo-override"]

        with pytest.raises(LLMCallError):
            caller.call(prompt="test", on_output=lambda x: None)

        # After exhaustion, the index must still be within the override
        # list — not a new index into a merged default chain.
        assert caller._current_agent_index == 0
        assert caller._agents[caller._current_agent_index]["name"] == "solo-override"


class TestExplicitAgentsArgHighestPriority:
    def test_explicit_agents_bypasses_override_and_default(self, tmp_path):
        """Explicitly passing agents= must bypass both the per-step
        override in llm_caller.steps AND the default chain.

        This is the contract internal callers (e.g. JSONExtractor) rely
        on when they need a specific agent regardless of user config.
        """
        _write_override(
            tmp_path, "implement",
            "      - override-a\n",
        )
        explicit = [
            {
                "name": "explicit-agent",
                "type": "claude-code",
                "cmd": "explicit-claude",
                "priority": 0,
            }
        ]
        with patch("tianluo.config.Path.home", return_value=tmp_path):
            caller = LLMCaller(
                project_root=tmp_path,
                step_type="implement",
                agents=explicit,
            )
        assert caller._agents is explicit
        assert [a["name"] for a in caller._agents] == ["explicit-agent"]
