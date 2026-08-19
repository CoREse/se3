"""Tests for the plan step prompt and outputs after the spec machinery retired.

The plan step no longer routes work through the retired spec governance steps
(``verify_spec`` / ``update_spec``): it plans against the task, the charter, and
the code-index. These tests pin that the spec-change declaration section and the
spec-file-write-protection section are gone from the prompt / JSON schemas, that
the version-file guardrail is still injected, and that PLAN emits no
``spec_changes`` output at all.
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType, FlowStatus
from tianluo.engine.steps.plan import (
    FULL_JSON_SCHEMA,
    MEDIUM_JSON_SCHEMA,
    SHALLOW_JSON_SCHEMA,
    VERSION_FILE_GUARDRAIL,
    _build_prompt,
    _get_prompt_depth,
    plan_handler,
)


class TestSpecMachineryRetired:
    """The retired spec-governance constants are gone from the plan module."""

    def test_spec_changes_section_constant_removed(self):
        import tianluo.engine.steps.plan as plan_mod

        assert not hasattr(plan_mod, "SPEC_CHANGES_SECTION")

    def test_spec_write_protection_section_constant_removed(self):
        import tianluo.engine.steps.plan as plan_mod

        assert not hasattr(plan_mod, "SPEC_WRITE_PROTECTION_SECTION")


class TestJsonSchemas:
    """No depth's JSON schema solicits spec_changes anymore."""

    def test_full_schema_excludes_spec_changes(self):
        assert "spec_changes" not in FULL_JSON_SCHEMA

    def test_medium_schema_excludes_spec_changes(self):
        assert "spec_changes" not in MEDIUM_JSON_SCHEMA

    def test_shallow_schema_excludes_spec_changes(self):
        assert "spec_changes" not in SHALLOW_JSON_SCHEMA


class TestBuildPrompt:
    """_build_prompt no longer carries any retired spec machinery."""

    def test_full_depth_has_no_spec_machinery(self):
        prompt = _build_prompt(
            task_description="Add feature X",
            task_type="feature",
            scope="module_a",
            project_summary="summary",
            revision_section="",
            depth="full",
        )
        assert "Spec Changes Declaration" not in prompt
        assert "spec_changes" not in prompt
        assert "update_spec" not in prompt
        assert "verify_spec" not in prompt
        # The header no longer frames a Relevant Specifications spec dump.
        assert "Relevant Specifications" not in prompt

    def test_medium_depth_has_no_spec_machinery(self):
        prompt = _build_prompt(
            task_description="Fix bug Y",
            task_type="bugfix",
            scope="module_b",
            project_summary="summary",
            revision_section="",
            depth="medium",
        )
        assert "Spec Changes Declaration" not in prompt
        assert "spec_changes" not in prompt

    def test_shallow_depth_has_no_spec_machinery(self):
        prompt = _build_prompt(
            task_description="Small tweak Z",
            task_type="small",
            scope="module_c",
            project_summary="summary",
            revision_section="",
            depth="shallow",
        )
        assert "Spec Changes Declaration" not in prompt
        assert "spec_changes" not in prompt


class TestPlanHandlerSpecChanges:
    """plan_handler completes and emits no spec_changes channel."""

    @pytest.fixture
    def flow(self, tmp_path):
        flow = FlowInstance(
            flow_id="test-flow-sc",
            task_description="Test spec changes",
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "test",
        )
        flow.state.selected_steps = [StepType.PLAN]
        return flow

    @pytest.fixture
    def step(self):
        return Step(
            step_type=StepType.PLAN,
            status=StepStatus.PENDING,
            inputs={
                "task_description": "Add new feature",
                "task_type": "feature",
                "scope": "engine",
                "project_summary": "A project",
            },
        )

    def _mock_llm_response(self):
        """Build a mock LLM JSON response (no spec_changes — the prompt no
        longer solicits it)."""
        data = {
            "plan": {
                "proposal": {"summary": "s", "motivation": "m", "files_to_modify": [], "files_to_create": [], "risks": []},
                "design": {"overview": "o", "architecture_decisions": [], "components": [], "data_flow": "", "testing_strategy": ""},
            },
            "task_groups": [
                {
                    "group_id": "G1",
                    "name": "group",
                    "description": "d",
                    "group_order": 1,
                    "depends_on": [],
                    "tasks": [{"id": 1, "description": "t", "complexity": "small", "estimated_loc": 10, "acceptance_criteria": ["c"], "files": ["f.py"], "depends_on": []}],
                }
            ],
            "total_complexity": "small",
            "estimated_effort": "1h",
        }
        return json.dumps(data)

    @patch("tianluo.engine.context_builder.get_runtime_environment_injection", return_value="")
    @patch("tianluo.engine.context_builder.get_code_index_injection", return_value="")
    @patch("tianluo.engine.context_builder.ensure_code_index_fresh", return_value=None)
    @patch("tianluo.engine.context_builder.get_charter_injection", return_value="")
    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.context_builder.get_step_language_instruction", return_value="")
    def test_spec_changes_output_removed(self, _lang, _inj, _ch, _fresh, _ci, _env, flow, step):
        """PLAN emits no spec_changes output — the step-to-step channel is gone."""
        with patch("tianluo.engine.steps.plan.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = self._mock_llm_response()
            mock_cls.return_value = mock_caller

            result = plan_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert "spec_changes" not in step.outputs
        assert step.outputs["task_groups"]

    @patch("tianluo.engine.context_builder.get_runtime_environment_injection", return_value="")
    @patch("tianluo.engine.context_builder.get_code_index_injection", return_value="")
    @patch("tianluo.engine.context_builder.ensure_code_index_fresh", return_value=None)
    @patch("tianluo.engine.context_builder.get_charter_injection", return_value="")
    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("tianluo.engine.context_builder.get_step_language_instruction", return_value="")
    def test_display_not_affected(self, _lang, _inj, _ch, _fresh, _ci, _env, flow, step):
        """Display logic should not crash."""
        with patch("tianluo.engine.steps.plan.LLMCaller") as mock_cls, \
             patch("tianluo.engine.steps.plan._display_plan") as mock_display:
            mock_caller = Mock()
            mock_caller.call.return_value = self._mock_llm_response()
            mock_cls.return_value = mock_caller

            result = plan_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_display.assert_called_once()


class TestVersionFileGuardrail:
    """The prompt-layer guardrail forbidding version-file bumps as plan tasks
    is still injected at every depth."""

    def test_guardrail_constant_exists_and_lists_examples(self):
        assert isinstance(VERSION_FILE_GUARDRAIL, str)
        assert "pyproject.toml" in VERSION_FILE_GUARDRAIL
        assert "package.json" in VERSION_FILE_GUARDRAIL
        assert "VERSIONS.md" in VERSION_FILE_GUARDRAIL
        # Must reference the engine steps that own version bumping
        assert "version_analyze" in VERSION_FILE_GUARDRAIL
        assert "commit" in VERSION_FILE_GUARDRAIL

    def test_full_depth_includes_guardrail(self):
        prompt = _build_prompt(
            task_description="Add feature X",
            task_type="feature",
            scope="m",
            project_summary="p",
            revision_section="",
            depth="full",
        )
        assert "Do Not Bump Version Files" in prompt
        assert "pyproject.toml" in prompt
        assert "VERSIONS.md" in prompt

    def test_medium_depth_includes_guardrail(self):
        prompt = _build_prompt(
            task_description="Fix bug Y",
            task_type="bugfix",
            scope="m",
            project_summary="p",
            revision_section="",
            depth="medium",
        )
        assert "Do Not Bump Version Files" in prompt
        assert "pyproject.toml" in prompt

    def test_shallow_depth_includes_guardrail(self):
        prompt = _build_prompt(
            task_description="Small tweak Z",
            task_type="small",
            scope="m",
            project_summary="p",
            revision_section="",
            depth="shallow",
        )
        assert "Do Not Bump Version Files" in prompt
        assert "pyproject.toml" in prompt
