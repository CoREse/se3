"""Tests for AgentRunner abstract base class and data types.

Verifies interface contract: ABC cannot be instantiated directly,
subclasses must implement all abstract methods, and data types behave correctly.
"""

import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from se3.agent_runner import AgentRunner, InfraErrorType, RunResult


class TestInfraErrorType:
    """Test InfraErrorType enum."""

    def test_enum_values(self):
        assert InfraErrorType.NONE.value == "none"
        assert InfraErrorType.USAGE_LIMIT.value == "usage_limit"
        assert InfraErrorType.TIMEOUT.value == "timeout"
        assert InfraErrorType.HANG.value == "hang"

    def test_enum_has_four_members(self):
        assert len(InfraErrorType) == 4


class TestRunResult:
    """Test RunResult dataclass."""

    def test_default_values(self):
        result = RunResult(returncode=0)
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""
        assert result.infra_error_type == InfraErrorType.NONE

    def test_custom_values(self):
        result = RunResult(
            returncode=1,
            stdout="output",
            stderr="error",
            infra_error_type=InfraErrorType.USAGE_LIMIT,
        )
        assert result.returncode == 1
        assert result.stdout == "output"
        assert result.stderr == "error"
        assert result.infra_error_type == InfraErrorType.USAGE_LIMIT


class TestAgentRunnerABC:
    """Test AgentRunner abstract base class contract."""

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            AgentRunner()

    def test_subclass_missing_run(self):
        class IncompleteRunner(AgentRunner):
            def run_with_monitor(self, args, **kwargs):
                pass
            def detect_infra_error(self, returncode, stdout, stderr):
                return InfraErrorType.NONE

        with pytest.raises(TypeError):
            IncompleteRunner()

    def test_subclass_missing_run_with_monitor(self):
        class IncompleteRunner(AgentRunner):
            def run(self, args, **kwargs):
                pass
            def detect_infra_error(self, returncode, stdout, stderr):
                return InfraErrorType.NONE

        with pytest.raises(TypeError):
            IncompleteRunner()

    def test_subclass_missing_detect_infra_error(self):
        class IncompleteRunner(AgentRunner):
            def run(self, args, **kwargs):
                pass
            def run_with_monitor(self, args, **kwargs):
                pass

        with pytest.raises(TypeError):
            IncompleteRunner()

    def test_complete_subclass_can_instantiate(self):
        class CompleteRunner(AgentRunner):
            def run(self, args, timeout=None, cwd=None, env=None, on_retry=None):
                return subprocess.CompletedProcess(args=[], returncode=0)
            def run_with_monitor(self, args, log_file=None, wall_timeout=None,
                                 inactivity_timeout=1800, cwd=None, env=None,
                                 on_output=None, on_activity=None):
                return None
            def detect_infra_error(self, returncode, stdout, stderr):
                return InfraErrorType.NONE
            def build_call_args(self, prompt, read_only, context_files=None):
                return ["-p", prompt]

        runner = CompleteRunner()
        assert isinstance(runner, AgentRunner)
