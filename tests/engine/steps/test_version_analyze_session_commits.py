"""Tests for version_analyze prompt rendering of session-introduced commits
and pre-session version baseline (G4 of bugfix for double-bump defect)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.models import FlowInstance, Step, StepStatus, StepType
from se3.engine.steps.version_analyze import (
    _format_session_commits,
    version_analyze_handler,
)


def _make_flow(**kwargs) -> FlowInstance:
    defaults = {
        "flow_id": "test-flow-va-sc",
        "task_description": "Fix something small",
        "task_type": "bugfix",
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
    step.step_id = "va-sc-001"
    return step


def _llm_response_json(**overrides) -> str:
    payload = {
        "bump_type": "patch",
        "reasoning": "Fix-only changes",
        "confidence": "high",
        "suggested_version": "5.1.1",
        "commit_message": "Fix double version bump in worktree flow",
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestFormatSessionCommitsHelper:
    """Direct unit tests for _format_session_commits."""

    def test_empty_list_renders_explanatory_text(self):
        text = _format_session_commits([])
        assert "implement 阶段未在主分支留下任何 commit" in text

    def test_none_renders_explanatory_text(self):
        text = _format_session_commits(None)
        assert "implement 阶段未在主分支留下任何 commit" in text

    def test_non_empty_renders_sha_and_subject(self):
        commits = [
            {
                "sha": "abcdef1234567890",
                "subject": "bump version to 5.2.0",
                "files": ["pyproject.toml", "VERSIONS.md"],
            },
            {
                "sha": "1122334455667788",
                "subject": "fix typo in helper",
                "files": ["src/se3/foo.py"],
            },
        ]
        text = _format_session_commits(commits)
        assert "abcdef12" in text
        assert "11223344" in text
        assert "bump version to 5.2.0" in text
        assert "fix typo in helper" in text
        assert "pyproject.toml" in text

    def test_renders_at_most_50_commits(self):
        commits = [
            {"sha": f"{i:040x}", "subject": f"commit {i}", "files": []}
            for i in range(60)
        ]
        text = _format_session_commits(commits)
        assert "还有 10 个未展示" in text
        # First commit appears, 50th commit appears, 51st does not.
        assert "commit 0" in text
        assert "commit 49" in text
        assert "commit 50" not in text

    def test_folds_long_file_lists(self):
        files = [f"file_{i}.py" for i in range(15)]
        commits = [{"sha": "deadbeefcafebabe", "subject": "many files", "files": files}]
        text = _format_session_commits(commits)
        assert "file_0.py" in text
        assert "file_9.py" in text
        assert "还有 5 个文件未展示" in text


class TestPromptIncludesSessionCommitsAndPreSession:
    """version_analyze_handler renders new fields into the prompt."""

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="5.2.0")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_prompt_includes_pre_session_version_and_commit_list(
        self, mock_caller_cls, mock_ver, mock_inject
    ):
        mock_caller = MagicMock()
        mock_caller.call.return_value = _llm_response_json()
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow()
        step = _make_step(
            {
                "task_description": "Fix small bug",
                "pre_session_version": "5.1.0",
                "session_commits": [
                    {
                        "sha": "aaaaaaaa11112222",
                        "subject": "bump version to 5.2.0",
                        "files": ["pyproject.toml", "VERSIONS.md"],
                    },
                    {
                        "sha": "bbbbbbbb33334444",
                        "subject": "implement group G2 feature",
                        "files": ["src/se3/foo.py"],
                    },
                ],
            }
        )

        result = version_analyze_handler(step, flow)
        assert result == StepStatus.COMPLETED

        # Inspect prompt that was passed to LLMCaller.call
        call_kwargs = mock_caller.call.call_args.kwargs
        prompt = call_kwargs.get("prompt") or mock_caller.call.call_args.args[0]
        assert "Pre-Session Version" in prompt
        assert "5.1.0" in prompt
        assert "Session-Introduced Commits" in prompt
        assert "aaaaaaaa" in prompt
        assert "bbbbbbbb" in prompt
        assert "bump version to 5.2.0" in prompt
        assert "implement group G2 feature" in prompt
        # Fixed instruction about treating commits as not having happened.
        assert "视为未发生" in prompt
        # Disk-version is still surfaced for cross-reference.
        assert "5.2.0" in prompt

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="5.1.0")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_prompt_handles_empty_session_commits(
        self, mock_caller_cls, mock_ver, mock_inject
    ):
        mock_caller = MagicMock()
        mock_caller.call.return_value = _llm_response_json(
            suggested_version="5.1.1"
        )
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow()
        step = _make_step(
            {
                "task_description": "Tiny doc fix",
                "pre_session_version": "5.1.0",
                "session_commits": [],
            }
        )

        result = version_analyze_handler(step, flow)
        assert result == StepStatus.COMPLETED

        call_kwargs = mock_caller.call.call_args.kwargs
        prompt = call_kwargs.get("prompt") or mock_caller.call.call_args.args[0]
        assert "Session-Introduced Commits" in prompt
        assert "implement 阶段未在主分支留下任何 commit" in prompt
        assert "Pre-Session Version" in prompt
        assert "5.1.0" in prompt

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="5.1.0")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_pre_session_version_fallback_to_current_version(
        self, mock_caller_cls, mock_ver, mock_inject, caplog
    ):
        """When pre_session_version is absent, fall back to disk-read current_version."""
        mock_caller = MagicMock()
        mock_caller.call.return_value = _llm_response_json(
            suggested_version="5.1.1"
        )
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow()
        # No pre_session_version, no session_commits in inputs.
        step = _make_step({"task_description": "Tiny doc fix"})

        import logging

        with caplog.at_level(logging.WARNING):
            result = version_analyze_handler(step, flow)
        assert result == StepStatus.COMPLETED

        call_kwargs = mock_caller.call.call_args.kwargs
        prompt = call_kwargs.get("prompt") or mock_caller.call.call_args.args[0]
        # pre_session_version slot is filled with the disk version as fallback.
        assert "Pre-Session Version" in prompt
        assert "5.1.0" in prompt
        # Empty list renders the explanatory line, not a crash.
        assert "implement 阶段未在主分支留下任何 commit" in prompt
        # Warning was logged about fallback.
        assert any(
            "pre_session_version missing" in rec.getMessage()
            for rec in caplog.records
        )
