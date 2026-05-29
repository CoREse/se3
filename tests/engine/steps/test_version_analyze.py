"""Tests for version_analyze step handler — commit_message output."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.models import FlowInstance, Step, StepStatus, StepType
from se3.engine.steps.version_analyze import (
    VERSION_RULES_MAX_BYTES,
    _fallback_commit_message,
    _read_version_rules_file,
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


class TestVersionChangesOutput:
    """version_analyze stores changelog bullets (versions_changes) in outputs."""

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_versions_changes_stored_from_llm_response(self, mock_caller_cls, mock_ver, mock_inject):
        """(m) When LLM returns versions_changes, they are stored verbatim."""
        bullets = [
            "Add user login with JWT tokens",
            "Add password reset endpoint",
            "Document the new auth flow in README",
        ]
        llm_response = json.dumps({
            "bump_type": "minor",
            "reasoning": "New feature added",
            "confidence": "high",
            "suggested_version": "1.3.0",
            "commit_message": "Add user authentication",
            "versions_changes": bullets,
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow()
        step = _make_step({"task_description": "Implement user login"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["versions_changes"] == bullets

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_versions_changes_fallback_when_llm_omits_field(self, mock_caller_cls, mock_ver, mock_inject):
        """(n) When LLM omits versions_changes, fall back to [commit_message]."""
        llm_response = json.dumps({
            "bump_type": "patch",
            "reasoning": "Bug fix",
            "confidence": "high",
            "suggested_version": "1.2.4",
            "commit_message": "Fix login timeout regression",
            # No versions_changes field
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(task_description="Fix login timeout bug")
        step = _make_step({"task_description": "Fix login timeout bug"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["versions_changes"] == ["Fix login timeout regression"]

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_versions_changes_not_a_list_falls_back(self, mock_caller_cls, mock_ver, mock_inject):
        """(n) A non-list versions_changes falls back to [commit_message]."""
        llm_response = json.dumps({
            "bump_type": "patch",
            "reasoning": "Bug fix",
            "confidence": "high",
            "suggested_version": "1.2.4",
            "commit_message": "Fix crash on empty input",
            "versions_changes": "Fix crash on empty input",  # string, not a list
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(task_description="Fix crash")
        step = _make_step({"task_description": "Fix crash"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["versions_changes"] == ["Fix crash on empty input"]

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_versions_changes_filters_non_string_elements(self, mock_caller_cls, mock_ver, mock_inject):
        """(o) Non-string / empty elements are filtered, strings are kept."""
        llm_response = json.dumps({
            "bump_type": "minor",
            "reasoning": "feature",
            "confidence": "high",
            "suggested_version": "1.3.0",
            "commit_message": "Add things",
            "versions_changes": [
                "Add valid bullet one",
                42,
                None,
                {"nested": "obj"},
                "   ",  # whitespace only -> dropped
                "Add valid bullet two",
            ],
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow()
        step = _make_step({"task_description": "Add things"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["versions_changes"] == [
            "Add valid bullet one",
            "Add valid bullet two",
        ]

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_versions_changes_all_non_string_falls_back(self, mock_caller_cls, mock_ver, mock_inject):
        """(o) A list with no usable strings falls back to [commit_message]."""
        llm_response = json.dumps({
            "bump_type": "patch",
            "reasoning": "fix",
            "confidence": "high",
            "suggested_version": "1.2.4",
            "commit_message": "Fix it",
            "versions_changes": [1, 2, None, {"a": 1}],
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(task_description="Fix it")
        step = _make_step({"task_description": "Fix it"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["versions_changes"] == ["Fix it"]


class TestVersionChangesForwarding:
    """versions_changes is forwarded from version_analyze.outputs to commit.inputs."""

    def _build_commit_inputs(self, va_outputs: dict) -> dict:
        from se3.engine.models import State
        from se3.engine.state_machine import StateMachine

        va_step = Step(
            step_type=StepType.VERSION_ANALYZE,
            status=StepStatus.COMPLETED,
            step_id="va-1",
        )
        va_step.outputs = va_outputs

        state = State()
        state.steps[va_step.step_id] = va_step
        state.step_history = [va_step.step_id]

        flow = FlowInstance(
            flow_id="flow-1",
            task_description="Implement something",
            task_type="feature",
            state=state,
        )

        sm = StateMachine(project_root=Path("/tmp/project"))
        return sm._build_step_inputs(flow, StepType.COMMIT)

    def test_versions_changes_forwarded_to_commit_inputs(self):
        bullets = ["Add A", "Fix B", "Document C"]
        inputs = self._build_commit_inputs({
            "suggested_version": "1.3.0",
            "commit_message": "Add features",
            "versions_changes": bullets,
        })
        assert inputs["versions_changes"] == bullets

    def test_versions_changes_defaults_to_empty_when_absent(self):
        # Older persisted flows have no versions_changes key on outputs.
        inputs = self._build_commit_inputs({
            "suggested_version": "1.3.0",
            "commit_message": "Add features",
        })
        assert inputs["versions_changes"] == []


class TestLLMFailureFailsStep:
    """When the LLM call fails or omits suggested_version, the step FAILS."""

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="1.0.0")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_llm_exception_marks_step_failed(self, mock_caller_cls, mock_ver, mock_inject):
        """LLM exception → step FAILED with informative error message."""
        mock_caller = MagicMock()
        mock_caller.call.side_effect = RuntimeError("LLM unavailable")
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(task_description="Refactor auth module")
        step = _make_step({"task_description": "Refactor auth module"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.FAILED
        assert step.outputs.get("current_version") == "1.0.0"
        assert "1.0.0" in step.error_message
        assert "suggested_version" in step.error_message

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="1.0.0")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_missing_suggested_version_marks_step_failed(self, mock_caller_cls, mock_ver, mock_inject):
        """LLM returns JSON without suggested_version → step FAILED."""
        llm_response = json.dumps({
            "bump_type": "patch",
            "reasoning": "Bug fix",
            "confidence": "high",
            # No suggested_version
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow()
        step = _make_step({"task_description": "Fix bug"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.FAILED
        assert "suggested_version" in step.error_message

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="1.0.0")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_empty_suggested_version_marks_step_failed(self, mock_caller_cls, mock_ver, mock_inject):
        """LLM returns empty suggested_version → step FAILED."""
        llm_response = json.dumps({
            "bump_type": "patch",
            "reasoning": "Bug fix",
            "confidence": "high",
            "suggested_version": "   ",
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow()
        step = _make_step({"task_description": "Fix bug"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.FAILED


class TestVersionRulesFileInjection:
    """version_analyze reads se3/version-rules.md and injects it into the prompt."""

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_rules_file_absent_uses_default_placeholder(
        self, mock_caller_cls, mock_ver, mock_inject, tmp_path
    ):
        """No rules file present → prompt contains default-SemVer placeholder."""
        llm_response = json.dumps({
            "bump_type": "minor",
            "suggested_version": "1.3.0",
            "reasoning": "feature added",
            "confidence": "high",
            "commit_message": "Add feature",
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(change_path=tmp_path / "se3.yaml")
        step = _make_step({"task_description": "Add feature"})

        result = version_analyze_handler(step, flow)
        assert result == StepStatus.COMPLETED

        called_prompt = mock_caller.call.call_args.kwargs["prompt"]
        assert "No project-specific rules file found" in called_prompt
        assert "Project-Specific Version Rules" in called_prompt

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_rules_file_present_is_injected_into_prompt(
        self, mock_caller_cls, mock_ver, mock_inject, tmp_path
    ):
        """When rules file exists, its content is injected into the prompt."""
        rules_dir = tmp_path / "se3"
        rules_dir.mkdir()
        rules_marker = "PROJECT RULE: docs-only commits never bump version."
        (rules_dir / "version-rules.md").write_text(rules_marker, encoding="utf-8")

        llm_response = json.dumps({
            "bump_type": "none",
            "suggested_version": "1.2.3",
            "reasoning": "docs only",
            "confidence": "high",
            "commit_message": "Update docs",
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(change_path=tmp_path / "se3.yaml")
        step = _make_step({"task_description": "Update docs"})

        result = version_analyze_handler(step, flow)
        assert result == StepStatus.COMPLETED

        called_prompt = mock_caller.call.call_args.kwargs["prompt"]
        assert rules_marker in called_prompt
        assert "No project-specific rules file found" not in called_prompt


class TestReadVersionRulesFile:
    """Unit tests for _read_version_rules_file."""

    def test_missing_file_returns_none(self, tmp_path):
        assert _read_version_rules_file(tmp_path) is None

    def test_normal_file_returns_full_content(self, tmp_path):
        rules_dir = tmp_path / "se3"
        rules_dir.mkdir()
        content = "# Custom Rules\n\n- docs → none\n"
        (rules_dir / "version-rules.md").write_text(content, encoding="utf-8")

        result = _read_version_rules_file(tmp_path)
        assert result is not None
        assert content in result

    def test_oversized_file_is_truncated_with_warning(self, tmp_path, caplog):
        rules_dir = tmp_path / "se3"
        rules_dir.mkdir()
        # Build content well over the limit
        big = "x" * (VERSION_RULES_MAX_BYTES + 1024)
        (rules_dir / "version-rules.md").write_text(big, encoding="utf-8")

        import logging as _logging
        with caplog.at_level(_logging.WARNING):
            result = _read_version_rules_file(tmp_path)

        assert result is not None
        # The injected text contains the truncation notice
        assert "Truncated by SE3" in result
        # And the raw content is bounded by the limit (plus the notice suffix)
        assert any("exceeds" in rec.message for rec in caplog.records)

    def test_invalid_utf8_returns_none_with_warning(self, tmp_path, caplog):
        rules_dir = tmp_path / "se3"
        rules_dir.mkdir()
        (rules_dir / "version-rules.md").write_bytes(b"\xff\xfe\xfa not utf-8")

        import logging as _logging
        with caplog.at_level(_logging.WARNING):
            result = _read_version_rules_file(tmp_path)

        assert result is None
        assert any("not valid UTF-8" in rec.message for rec in caplog.records)


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
