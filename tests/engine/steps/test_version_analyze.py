"""Tests for version_analyze step handler — commit_message output."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.models import FlowInstance, Step, StepStatus, StepType
from se3.engine.steps.version_analyze import (
    _fallback_commit_message,
    _validate_result,
    version_analyze_handler,
)


def _make_flow(**kwargs) -> FlowInstance:
    defaults = {
        "flow_id": "test-flow-001",
        "task_description": "Implement user login",
        "task_type": "feature",
        "change_path": Path("/tmp/project/se3.yaml"),
    }
    defaults.update(kwargs)
    flow = MagicMock(spec=FlowInstance)
    for k, v in defaults.items():
        setattr(flow, k, v)
    return flow


def _make_step(inputs: dict | None = None) -> Step:
    step = MagicMock(spec=Step)
    step.inputs = inputs or {}
    step.outputs = {}
    step.step_type = StepType.VERSION_ANALYZE
    step.step_id = "va-001"
    return step


class TestCommitMessageInOutput:
    """version_analyze stores commit_message in step.outputs."""

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_commit_message_stored_from_llm_response(self, mock_caller_cls, mock_ver, mock_inject):
        """When LLM returns commit_message, it is stored in outputs."""
        llm_response = json.dumps({
            "bump_type": "minor",
            "reasoning": "New feature added",
            "confidence": "high",
            "suggested_version": "1.3.0",
            "commit_message": "Add user login with JWT tokens",
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow()
        step = _make_step({"task_description": "Implement user login"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["commit_message"] == "Add user login with JWT tokens"
        assert step.outputs["bump_type"] == "minor"

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_commit_message_fallback_when_llm_omits_field(self, mock_caller_cls, mock_ver, mock_inject):
        """When LLM response omits commit_message, fallback is used."""
        llm_response = json.dumps({
            "bump_type": "patch",
            "reasoning": "Bug fix",
            "confidence": "high",
            "suggested_version": "1.2.4",
            # No commit_message field
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(task_description="Fix login timeout bug")
        step = _make_step({"task_description": "Fix login timeout bug"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        # Fallback should use task description
        assert step.outputs["commit_message"] == "Fix login timeout bug"

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_commit_message_fallback_when_llm_returns_empty(self, mock_caller_cls, mock_ver, mock_inject):
        """When LLM returns empty commit_message, fallback is used."""
        llm_response = json.dumps({
            "bump_type": "patch",
            "reasoning": "Bug fix",
            "confidence": "high",
            "suggested_version": "1.2.4",
            "commit_message": "",
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(task_description="Fix login timeout bug")
        step = _make_step({"task_description": "Fix login timeout bug"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["commit_message"] == "Fix login timeout bug"


class TestCommitMessageOnLLMFailure:
    """When the entire LLM call fails, commit_message fallback is produced."""

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="1.0.0")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_llm_failure_produces_fallback_commit_message(self, mock_caller_cls, mock_ver, mock_inject):
        """On LLM exception, commit_message is generated from task description."""
        mock_caller = MagicMock()
        mock_caller.call.side_effect = RuntimeError("LLM unavailable")
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(task_description="Refactor auth module")
        step = _make_step({"task_description": "Refactor auth module"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED  # Falls back, doesn't fail
        assert step.outputs["commit_message"] == "Refactor auth module"
        assert step.outputs["confidence"] == "low"


class TestFallbackCommitMessage:
    """Unit tests for _fallback_commit_message helper."""

    def test_uses_first_sentence(self):
        msg = _fallback_commit_message("feature", "Add login flow. Also add logout.")
        assert msg == "Add login flow"

    def test_truncates_long_description(self):
        long_desc = "A" * 100
        msg = _fallback_commit_message("bugfix", long_desc)
        assert len(msg) <= 72
        assert msg.endswith("...")

    def test_empty_description_returns_default(self):
        msg = _fallback_commit_message("feature", "")
        assert msg == "Update project"

    def test_whitespace_only_description_returns_default(self):
        msg = _fallback_commit_message("feature", "   ")
        assert msg == "Update project"

    def test_short_description_preserved(self):
        msg = _fallback_commit_message("bugfix", "Fix typo in README")
        assert msg == "Fix typo in README"
