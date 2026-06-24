"""Tests for plan step spec_changes output (G1)."""

from __future__ import annotations

import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from se3.engine.models import FlowInstance, Step, StepStatus, StepType, FlowStatus
from se3.engine.steps.plan import (
    SPEC_CHANGES_SECTION,
    SPEC_WRITE_PROTECTION_SECTION,
    FULL_JSON_SCHEMA,
    MEDIUM_JSON_SCHEMA,
    SHALLOW_JSON_SCHEMA,
    VERSION_FILE_GUARDRAIL,
    _build_prompt,
    _get_prompt_depth,
    plan_handler,
)


class TestSpecChangesSection:
    """Test SPEC_CHANGES_SECTION constant."""

    def test_section_exists(self):
        assert isinstance(SPEC_CHANGES_SECTION, str)
        assert len(SPEC_CHANGES_SECTION) > 0

    def test_section_describes_structure(self):
        assert "spec_name" in SPEC_CHANGES_SECTION
        assert "change_type" in SPEC_CHANGES_SECTION
        assert "target" in SPEC_CHANGES_SECTION
        assert "description" in SPEC_CHANGES_SECTION
        assert "rationale" in SPEC_CHANGES_SECTION

    def test_section_describes_change_types(self):
        assert "add_requirement" in SPEC_CHANGES_SECTION
        assert "modify_requirement" in SPEC_CHANGES_SECTION
        assert "add_scenario" in SPEC_CHANGES_SECTION
        assert "deprecate_requirement" in SPEC_CHANGES_SECTION


class TestJsonSchemas:
    """Test spec_changes in JSON schemas."""

    def test_full_schema_includes_spec_changes(self):
        assert "spec_changes" in FULL_JSON_SCHEMA

    def test_medium_schema_excludes_spec_changes(self):
        assert "spec_changes" not in MEDIUM_JSON_SCHEMA

    def test_shallow_schema_excludes_spec_changes(self):
        assert "spec_changes" not in SHALLOW_JSON_SCHEMA


class TestBuildPrompt:
    """Test _build_prompt includes SPEC_CHANGES_SECTION only for full depth."""

    def test_full_depth_includes_spec_changes_section(self):
        prompt = _build_prompt(
            task_description="Add feature X",
            task_type="feature",
            scope="module_a",
            spec_content="some spec",
            project_summary="summary",
            revision_section="",
            depth="full",
        )
        assert "Spec Changes Declaration" in prompt
        assert "spec_changes" in prompt

    def test_medium_depth_excludes_spec_changes_section(self):
        prompt = _build_prompt(
            task_description="Fix bug Y",
            task_type="bugfix",
            scope="module_b",
            spec_content="some spec",
            project_summary="summary",
            revision_section="",
            depth="medium",
        )
        assert "Spec Changes Declaration" not in prompt

    def test_shallow_depth_excludes_spec_changes_section(self):
        prompt = _build_prompt(
            task_description="Directive Z",
            task_type="directive",
            scope="module_c",
            spec_content="some spec",
            project_summary="summary",
            revision_section="",
            depth="shallow",
        )
        assert "Spec Changes Declaration" not in prompt


class TestPlanHandlerSpecChanges:
    """Test plan_handler extracts and stores spec_changes."""

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
                "spec_content": {"flow-engine": "spec content"},
                "project_summary": "A project",
            },
        )

    def _mock_llm_response(self, spec_changes=None):
        """Build a mock LLM JSON response."""
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
        if spec_changes is not None:
            data["spec_changes"] = spec_changes
        return json.dumps(data)

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.context_builder.get_step_language_instruction", return_value="")
    def test_spec_changes_extracted_and_stored(self, _lang, _inj, flow, step):
        changes = [
            {"spec_name": "flow-engine", "change_type": "add_requirement", "target": "Req X", "description": "Add X", "rationale": "Need X"}
        ]
        with patch("se3.engine.steps.plan.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = self._mock_llm_response(spec_changes=changes)
            mock_cls.return_value = mock_caller

            result = plan_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["spec_changes"] == changes

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.context_builder.get_step_language_instruction", return_value="")
    def test_spec_changes_defaults_to_empty_list(self, _lang, _inj, flow, step):
        """When LLM omits spec_changes, default to []."""
        with patch("se3.engine.steps.plan.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = self._mock_llm_response(spec_changes=None)
            mock_cls.return_value = mock_caller

            result = plan_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["spec_changes"] == []

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.context_builder.get_step_language_instruction", return_value="")
    def test_display_not_affected_by_spec_changes(self, _lang, _inj, flow, step):
        """Display logic should not crash with spec_changes present."""
        changes = [{"spec_name": "x", "change_type": "add_requirement", "target": "T", "description": "D", "rationale": "R"}]
        with patch("se3.engine.steps.plan.LLMCaller") as mock_cls, \
             patch("se3.engine.steps.plan._display_plan") as mock_display:
            mock_caller = Mock()
            mock_caller.call.return_value = self._mock_llm_response(spec_changes=changes)
            mock_cls.return_value = mock_caller

            result = plan_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_display.assert_called_once()


class TestVersionFileGuardrail:
    """G1: prompt-layer guardrail forbidding version-file bumps as plan tasks."""

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
            spec_content="s",
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
            spec_content="s",
            project_summary="p",
            revision_section="",
            depth="medium",
        )
        assert "Do Not Bump Version Files" in prompt
        assert "pyproject.toml" in prompt

    def test_shallow_depth_includes_guardrail(self):
        prompt = _build_prompt(
            task_description="Directive Z",
            task_type="directive",
            scope="m",
            spec_content="s",
            project_summary="p",
            revision_section="",
            depth="shallow",
        )
        assert "Do Not Bump Version Files" in prompt
        assert "pyproject.toml" in prompt


class TestSpecWriteProtectionSection:
    """G2: plan-specific guardrail forbidding downstream spec-file writes,
    while preserving (and encouraging) the spec_changes declaration channel."""

    def test_section_exists(self):
        assert isinstance(SPEC_WRITE_PROTECTION_SECTION, str)
        assert len(SPEC_WRITE_PROTECTION_SECTION) > 0

    def test_section_forbids_instructing_downstream_spec_writes(self):
        text = SPEC_WRITE_PROTECTION_SECTION
        # Two-layer semantics: forbid spec-file writes ...
        assert "se3/specs/" in text
        assert "MUST NOT" in text
        assert "implement" in text
        # ... but preserve the spec_changes declaration channel.
        assert "spec_changes" in text
        assert "update_spec" in text
        assert "verify_spec" in text

    def test_section_allows_behavior_change(self):
        """Wording must allow changing existing behavior; it only restricts
        who writes spec files."""
        lowered = SPEC_WRITE_PROTECTION_SECTION.lower()
        assert "behavior" in lowered

    def test_full_depth_includes_section_and_keeps_spec_changes(self):
        prompt = _build_prompt(
            task_description="Add feature X",
            task_type="feature",
            scope="m",
            spec_content="s",
            project_summary="p",
            revision_section="",
            depth="full",
        )
        assert SPEC_WRITE_PROTECTION_SECTION in prompt
        # full depth still keeps the spec_changes declaration channel intact
        assert "Spec Changes Declaration" in prompt
        assert "spec_changes" in prompt

    def test_medium_depth_includes_section(self):
        prompt = _build_prompt(
            task_description="Fix bug Y",
            task_type="bugfix",
            scope="m",
            spec_content="s",
            project_summary="p",
            revision_section="",
            depth="medium",
        )
        assert SPEC_WRITE_PROTECTION_SECTION in prompt

    def test_shallow_depth_includes_section(self):
        prompt = _build_prompt(
            task_description="Directive Z",
            task_type="directive",
            scope="m",
            spec_content="s",
            project_summary="p",
            revision_section="",
            depth="shallow",
        )
        assert SPEC_WRITE_PROTECTION_SECTION in prompt


class TestStepPoolSpecChanges:
    """Test that STEP_POOL declares spec_changes in PLAN outputs."""

    def test_plan_outputs_include_spec_changes(self):
        from se3.engine.models import STEP_POOL
        plan_info = STEP_POOL[StepType.PLAN]
        assert "spec_changes" in plan_info["outputs"]
