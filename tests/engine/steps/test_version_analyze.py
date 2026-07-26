"""Tests for version_analyze step handler — commit_message output."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.steps.version_analyze import (
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
        "change_path": Path("/tmp/project/tianluo.yaml"),
        # Mirror the real FlowInstance default. Without it a MagicMock(spec=…)
        # returns a truthy MagicMock for is_worktree_mode, sending the handler
        # down the worktree intent-only branch (which suppresses the
        # authoritative suggested_version these tests expect and writes a stray
        # intent file to change_path's parent). These tests cover the
        # synchronous path; the worktree branch is covered in test_steps.py.
        "is_worktree_mode": False,
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

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
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

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
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

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
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


class TestDiscoveryRunUsesRealType:
    """A --discover run must inject the real analyzed type (not the run mode) as
    the prompt's Task Type and drive _fallback_commit_message with it."""

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
    def test_discovery_flow_injects_real_task_type(self, mock_caller_cls, mock_ver, mock_inject):
        llm_response = json.dumps({
            "bump_type": "minor",
            "reasoning": "New feature",
            "confidence": "high",
            "suggested_version": "1.3.0",
            "commit_message": "Add thing",
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(task_type="discovery")
        # analyze persisted the real inferred type on the flow's context.
        flow.state = MagicMock()
        flow.state.context = {"analyzed_type": "feature"}
        step = _make_step({"task_description": "Build a thing"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        # Inspect the prompt the LLM saw: the Task Type line must be the real
        # analyzed type, never the 'discovery' run mode.
        prompt = mock_caller.call.call_args.kwargs["prompt"]
        assert "**Task Type:** feature" in prompt
        assert "**Task Type:** discovery" not in prompt

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
    def test_discovery_flow_fallback_commit_message_uses_real_type(
        self, mock_caller_cls, mock_ver, mock_inject
    ):
        """When the LLM omits commit_message, the fallback is derived with the
        real analyzed type — it must not fail nor leak 'discovery'."""
        llm_response = json.dumps({
            "bump_type": "minor",
            "reasoning": "New feature",
            "confidence": "high",
            "suggested_version": "1.3.0",
            # no commit_message
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(task_type="discovery", task_description="Add discovery-driven feature")
        flow.state = MagicMock()
        flow.state.context = {"analyzed_type": "feature"}
        step = _make_step({"task_description": "Add discovery-driven feature"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        # Fallback uses the task description (type-agnostic body) and stores it.
        assert step.outputs["commit_message"] == "Add discovery-driven feature"


class TestVersionChangesOutput:
    """version_analyze stores changelog bullets (versions_changes) in outputs."""

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
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

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
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

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
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

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
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

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
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


class TestTagDecisionOutput:
    """version_analyze exposes the tag decision metadata."""

    @pytest.mark.parametrize(
        ("bump_type", "suggested_version", "expected"),
        [
            ("major", "2.0.0", True),
            ("minor", "1.1.0", True),
            ("patch", "1.0.1", False),
        ],
    )
    def test_default_semver_derives_is_tag_from_version_advance(
        self, bump_type, suggested_version, expected
    ):
        result = _validate_result(
            {
                "suggested_version": suggested_version,
                "bump_type": bump_type,
                "reasoning": "Default SemVer decision",
                "confidence": "high",
            },
            current_version="1.0.0",
        )
        assert result["is_tag"] is expected

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
    def test_default_semver_outputs_is_tag_for_minor(
        self, mock_caller_cls, mock_ver, mock_inject
    ):
        llm_response = json.dumps({
            "bump_type": "minor",
            "reasoning": "New feature",
            "confidence": "high",
            "suggested_version": "1.3.0",
            "commit_message": "Add feature",
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow()
        step = _make_step({"task_description": "Add feature"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["is_tag"] is True

    def test_malformed_bump_type_still_tags_a_minor_suggested_version(self):
        """suggested_version is authoritative; garbled bump_type must not untag."""
        result = _validate_result(
            {
                "suggested_version": "11.16.0",
                "bump_type": "Minor Feature",
                "reasoning": "New feature",
                "confidence": "high",
            },
            current_version="11.15.3",
        )
        assert result["bump_type"] == "patch"
        assert result["is_tag"] is True

    def test_malformed_bump_type_on_patch_advance_does_not_tag(self):
        result = _validate_result(
            {
                "suggested_version": "11.15.4",
                "bump_type": "Bugfix",
                "reasoning": "Fix",
                "confidence": "high",
            },
            current_version="11.15.3",
        )
        assert result["is_tag"] is False

    def test_minor_bump_type_on_patch_advance_does_not_tag(self):
        """A patch-only version advance never tags, whatever bump_type claims."""
        result = _validate_result(
            {
                "suggested_version": "11.15.4",
                "bump_type": "minor",
                "reasoning": "Fix",
                "confidence": "high",
            },
            current_version="11.15.3",
        )
        assert result["bump_type"] == "minor"
        assert result["is_tag"] is False

    @pytest.mark.parametrize(
        ("current_version", "suggested_version"),
        [
            ("not-a-version", "2026.07.09"),
            ("1.0.0", "release-2"),
            (None, "2.0.0"),
        ],
    )
    def test_non_comparable_versions_never_tag_under_default_semver(
        self, current_version, suggested_version
    ):
        """No SemVer delta ⇒ no default-policy verdict ⇒ no tag from bump_type."""
        result = _validate_result(
            {
                "suggested_version": suggested_version,
                "bump_type": "minor",
                "reasoning": "Calendar release",
                "confidence": "high",
            },
            current_version=current_version,
        )
        assert result["is_tag"] is False

    @pytest.mark.parametrize("is_tag", [True, False])
    def test_custom_rules_preserve_boolean_is_tag(self, is_tag):
        result = _validate_result(
            {
                "suggested_version": "2026.07.09",
                "bump_type": "patch",
                "reasoning": "Custom rule decision",
                "confidence": "high",
                "is_tag": is_tag,
            },
            has_custom_rules=True,
        )
        assert result["is_tag"] is is_tag

    @pytest.mark.parametrize("raw_is_tag", ["true", 1, None, {"value": True}])
    def test_custom_rules_non_boolean_is_tag_normalizes_false(self, raw_is_tag):
        result = _validate_result(
            {
                "suggested_version": "2026.07.09",
                "bump_type": "minor",
                "reasoning": "Custom rule decision",
                "confidence": "high",
                "is_tag": raw_is_tag,
            },
            has_custom_rules=True,
        )
        assert result["is_tag"] is False

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
    def test_custom_rules_prompt_and_output_include_is_tag(
        self, mock_caller_cls, mock_ver, mock_inject, tmp_path
    ):
        rules_dir = tmp_path / "tianluo"
        rules_dir.mkdir()
        (rules_dir / "version-rules.md").write_text(
            "Create tags for calendar releases only.", encoding="utf-8"
        )

        llm_response = json.dumps({
            "bump_type": "patch",
            "suggested_version": "2026.07.09",
            "reasoning": "Calendar release",
            "confidence": "high",
            "commit_message": "Ship calendar release",
            "is_tag": True,
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(change_path=tmp_path / "tianluo.yaml")
        step = _make_step({"task_description": "Ship calendar release"})

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["is_tag"] is True
        called_prompt = mock_caller.call.call_args.kwargs["prompt"]
        assert "**is_tag**" in called_prompt
        assert '"is_tag": true' in called_prompt

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
    def test_worktree_intent_includes_is_tag(
        self, mock_caller_cls, mock_ver, mock_inject, tmp_path
    ):
        from tianluo.engine.version_intent import read_intent

        llm_response = json.dumps({
            "bump_type": "minor",
            "reasoning": "New feature",
            "confidence": "high",
            "suggested_version": "1.3.0",
            "commit_message": "Add feature",
        })
        mock_caller = MagicMock()
        mock_caller.call.return_value = llm_response
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(
            change_path=tmp_path / "tianluo.yaml",
            flow_id="flow-tag-intent",
            is_worktree_mode=True,
        )
        step = _make_step(
            {"task_description": "Add feature", "pre_session_version": "1.2.3"}
        )

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["version_intent"]["is_tag"] is True
        intent = read_intent(tmp_path, "flow-tag-intent")
        assert intent is not None
        assert intent.is_tag is True


class TestVersionChangesForwarding:
    """versions_changes is forwarded from version_analyze.outputs to commit.inputs."""

    def _build_commit_inputs(self, va_outputs: dict) -> dict:
        from tianluo.engine.models import State
        from tianluo.engine.state_machine import StateMachine

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

    def test_is_tag_forwarded_to_commit_inputs(self):
        inputs = self._build_commit_inputs({
            "suggested_version": "1.3.0",
            "commit_message": "Add features",
            "is_tag": True,
        })
        assert inputs["is_tag"] is True


class TestLLMFailureFailsStep:
    """When the LLM call fails or omits suggested_version, the step FAILS."""

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.0.0")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
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

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.0.0")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
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

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.0.0")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
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
    """version_analyze reads tianluo/version-rules.md and injects it into the prompt."""

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
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

        flow = _make_flow(change_path=tmp_path / "tianluo.yaml")
        step = _make_step({"task_description": "Add feature"})

        result = version_analyze_handler(step, flow)
        assert result == StepStatus.COMPLETED

        called_prompt = mock_caller.call.call_args.kwargs["prompt"]
        assert "No project-specific rules file found" in called_prompt
        assert "Project-Specific Version Rules" in called_prompt
        assert '"is_tag": true' not in called_prompt
        assert "**is_tag**" not in called_prompt

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.steps.version_analyze._get_current_version", return_value="1.2.3")
    @patch("tianluo.engine.steps.version_analyze.LLMCaller")
    def test_rules_file_present_is_injected_into_prompt(
        self, mock_caller_cls, mock_ver, mock_inject, tmp_path
    ):
        """When rules file exists, its content is injected into the prompt."""
        rules_dir = tmp_path / "tianluo"
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

        flow = _make_flow(change_path=tmp_path / "tianluo.yaml")
        step = _make_step({"task_description": "Update docs"})

        result = version_analyze_handler(step, flow)
        assert result == StepStatus.COMPLETED

        called_prompt = mock_caller.call.call_args.kwargs["prompt"]
        assert rules_marker in called_prompt
        assert "No project-specific rules file found" not in called_prompt
        assert "**is_tag**" in called_prompt
        assert '"is_tag": true' in called_prompt


class TestReadVersionRulesFile:
    """Unit tests for _read_version_rules_file."""

    def test_missing_file_returns_none(self, tmp_path):
        assert _read_version_rules_file(tmp_path) is None

    def test_normal_file_returns_full_content(self, tmp_path):
        rules_dir = tmp_path / "tianluo"
        rules_dir.mkdir()
        content = "# Custom Rules\n\n- docs → none\n"
        (rules_dir / "version-rules.md").write_text(content, encoding="utf-8")

        result = _read_version_rules_file(tmp_path)
        assert result is not None
        assert content in result

    def test_oversized_file_is_truncated_with_warning(self, tmp_path, caplog):
        rules_dir = tmp_path / "tianluo"
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
        rules_dir = tmp_path / "tianluo"
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
