"""Regression tests for the failure-reason recorded when every agent fails.

A retry sequence that rotates through all agents used to raise
``LLMCallError`` with an empty reason, because the rotation branch
``continue``d past the point where the error string was assigned. These tests
pin the accumulated per-attempt reasons into the final message.
"""

from __future__ import annotations

import pytest

from tianluo.engine.llm_caller import InfraErrorType, LLMCallError, LLMCaller


class _FailingRunner:
    """Stub AgentRunner that always fails with a fixed exit code."""

    def __init__(self, returncode: int, cmd_used: str):
        self.returncode = returncode
        self.cmd_used = cmd_used

    def build_call_args(self, **kwargs):
        return [self.cmd_used]

    def run_with_monitor(self, **kwargs):
        return _Result(returncode=self.returncode, cmd_used=self.cmd_used)

    def detect_infra_error(self, returncode, output, stderr_tail):
        return InfraErrorType.NONE


class _Result:
    def __init__(self, returncode: int, cmd_used: str):
        self.success = False
        self.output = ""
        self.returncode = returncode
        self.cmd_used = cmd_used
        self.stderr_tail = ""
        self.interrupted = False


AGENTS = [
    {"name": "alpha", "cmd": "claude", "type": "claude"},
    {"name": "beta", "cmd": "claude", "type": "claude"},
    {"name": "gamma", "cmd": "claude", "type": "claude"},
]

RUNNERS = {
    "alpha": _FailingRunner(11, "alpha-cmd"),
    "beta": _FailingRunner(22, "beta-cmd"),
    "gamma": _FailingRunner(33, "gamma-cmd"),
}


@pytest.fixture
def make_caller(tmp_path, monkeypatch):
    # The se3 test step exports SE3_TEST_RUNNING, which makes engine helpers
    # skip real command execution; these tests assert on a failure path, so the
    # guard must not short-circuit them.
    monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)

    monkeypatch.setattr(
        LLMCaller,
        "_get_current_runner",
        lambda self: RUNNERS[self._agents[self._current_agent_index]["name"]],
    )

    def _make(max_retries: int) -> LLMCaller:
        return LLMCaller(
            project_root=tmp_path,
            max_retries=max_retries,
            retry_delay=0.0,
            agents=AGENTS,
        )

    return _make


def test_error_names_every_agent_and_exit_code(make_caller):
    """Attempts 1 and 2 end in a rotation; only attempt 3 hits tail-on-last.

    The rotating attempts used to be dropped from the reason entirely.
    """
    with pytest.raises(LLMCallError) as excinfo:
        make_caller(max_retries=3).call("do something")

    message = str(excinfo.value)
    for agent_name, runner in RUNNERS.items():
        assert f"'{agent_name}'" in message
        assert f"exit={runner.returncode}" in message
        assert f"cmd={runner.cmd_used}" in message


def test_all_attempts_rotating_still_yields_a_reason(make_caller):
    """Two retries over three agents: every attempt rotates, none falls through
    to tail-on-last. This is the exact shape that produced the bare
    ``"failed after N attempts: "`` message with no reason at all.
    """
    with pytest.raises(LLMCallError) as excinfo:
        make_caller(max_retries=2).call("do something")

    message = str(excinfo.value)
    assert not message.rstrip().endswith("attempts:")
    assert message.split("attempts:", 1)[1].strip()
    # gamma is never reached — only the two rotating agents are reported.
    assert "'alpha'" in message and "exit=11" in message
    assert "'beta'" in message and "exit=22" in message


def test_infra_error_label_is_reported(make_caller):
    with pytest.raises(LLMCallError) as excinfo:
        make_caller(max_retries=2).call("do something")

    assert "infra_error=other" in str(excinfo.value)
