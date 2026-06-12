"""Tests for merging project_summary and read_spec into analyze step.

Verifies:
1. _build_step_inputs ANALYZE mapping includes spec_content, relevant_specs, project_summary
2. Step sequences no longer contain PROJECT_SUMMARY (READ_SPEC fully removed)
3. STEP_POOL ANALYZE outputs updated; PROJECT_SUMMARY marked deprecated
4. Stub handlers exist and forward correctly
5. Downstream steps (plan, implement, verify_spec) get spec_content from analyze outputs
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
    STEP_POOL,
    get_default_step_sequence,
)
from se3.engine.state_machine import StateMachine
from se3.engine.steps import (
    STEP_HANDLERS,
    project_summary_stub_handler,
)


# --- Helpers ---

def _make_flow(tmp_path: Path, task_type: str = "feature") -> FlowInstance:
    return FlowInstance(
        flow_id="test-flow-analyze-merge",
        task_description="Test task",
        task_type=task_type,
        status=FlowStatus.RUNNING,
    )


def _add_completed_step(flow: FlowInstance, step_type: StepType, outputs: dict) -> Step:
    step = Step(step_type=step_type, status=StepStatus.COMPLETED, outputs=outputs)
    flow.state.add_step(step)
    return step


# --- Task 4: _build_step_inputs ANALYZE mapping ---

class TestBuildStepInputsAnalyzeMapping:
    """Verify ANALYZE outputs are forwarded to downstream steps."""

    @pytest.fixture
    def sm(self, tmp_path):
        return StateMachine(tmp_path)

    @pytest.fixture
    def flow_with_analyze(self, tmp_path):
        """Flow with a completed ANALYZE step containing new merged outputs."""
        flow = _make_flow(tmp_path)
        _add_completed_step(flow, StepType.ANALYZE, {
            "task_type": "feature",
            "scope": "engine module",
            "complexity": "medium",
            "reasoning": "This is a feature task",
            "project_summary": "Project: SE3 Framework, branch: main",
            "relevant_specs": ["flow-engine", "base"],
            "spec_content": {
                "base": "# Base spec content",
                "flow-engine": "# Flow engine spec content",
            },
        })
        return flow

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_analyze_forwards_project_summary(self, _cfg, sm, flow_with_analyze):
        inputs = sm._build_step_inputs(flow_with_analyze, StepType.PLAN)
        assert inputs["project_summary"] == "Project: SE3 Framework, branch: main"

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_analyze_forwards_relevant_specs(self, _cfg, sm, flow_with_analyze):
        inputs = sm._build_step_inputs(flow_with_analyze, StepType.PLAN)
        assert inputs["relevant_specs"] == ["flow-engine", "base"]

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_analyze_forwards_spec_content(self, _cfg, sm, flow_with_analyze):
        inputs = sm._build_step_inputs(flow_with_analyze, StepType.PLAN)
        assert inputs["spec_content"]["base"] == "# Base spec content"
        assert inputs["spec_content"]["flow-engine"] == "# Flow engine spec content"

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_analyze_forwards_task_type_and_scope(self, _cfg, sm, flow_with_analyze):
        """Original ANALYZE outputs still forwarded."""
        inputs = sm._build_step_inputs(flow_with_analyze, StepType.PLAN)
        assert inputs["task_type"] == "feature"
        assert inputs["scope"] == "engine module"

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_verify_spec_gets_spec_content_from_analyze(self, _cfg, sm, flow_with_analyze):
        """verify_spec (direct consumer in review flow) gets spec_content from analyze."""
        inputs = sm._build_step_inputs(flow_with_analyze, StepType.VERIFY_SPEC)
        assert "spec_content" in inputs
        assert inputs["spec_content"]["flow-engine"] == "# Flow engine spec content"

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_implement_gets_spec_content_from_analyze(self, _cfg, sm, flow_with_analyze):
        """implement gets spec_content from analyze (via inputs passthrough)."""
        inputs = sm._build_step_inputs(flow_with_analyze, StepType.IMPLEMENT)
        assert "spec_content" in inputs


class TestBuildStepInputsDeprecatedBackwardCompat:
    """Verify deprecated PROJECT_SUMMARY branch still works for old flows."""

    @pytest.fixture
    def sm(self, tmp_path):
        return StateMachine(tmp_path)

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_old_project_summary_step_still_forwards(self, _cfg, sm, tmp_path):
        """Old persisted flow with PROJECT_SUMMARY step should still forward project_summary."""
        flow = _make_flow(tmp_path)
        _add_completed_step(flow, StepType.PROJECT_SUMMARY, {
            "project_summary": "Old-style project summary",
        })
        inputs = sm._build_step_inputs(flow, StepType.PLAN)
        assert inputs["project_summary"] == "Old-style project summary"

    def test_old_read_spec_step_type_no_longer_parseable(self):
        """READ_SPEC has been fully removed from StepType enum."""
        with pytest.raises(ValueError):
            StepType("read_spec")


# --- Task 5: Step sequence updates ---

class TestStepSequenceNoProjectSummaryOrReadSpec:
    """Verify PROJECT_SUMMARY removed from all task type sequences (READ_SPEC fully removed)."""

    ALL_TASK_TYPES = ["feature", "bugfix", "review", "small", "directive", "discovery"]

    @pytest.mark.parametrize("task_type", ALL_TASK_TYPES)
    def test_no_project_summary_in_sequence(self, task_type):
        sequence = get_default_step_sequence(task_type)
        assert StepType.PROJECT_SUMMARY not in sequence, (
            f"{task_type} sequence still contains PROJECT_SUMMARY"
        )

    def test_feature_sequence_starts_analyze_then_plan(self):
        seq = get_default_step_sequence("feature")
        assert seq[0] == StepType.ANALYZE
        assert seq[1] == StepType.PLAN

    def test_review_sequence_analyze_then_verify_spec(self):
        seq = get_default_step_sequence("review")
        assert seq == [StepType.ANALYZE, StepType.VERIFY_SPEC, StepType.SUMMARIZE]

    def test_small_sequence_unchanged(self):
        """Small sequence never had PROJECT_SUMMARY (now ends with SUMMARIZE)."""
        seq = get_default_step_sequence("small")
        assert seq == [
            StepType.ANALYZE,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
            StepType.SUMMARIZE,
        ]

    def test_discovery_sequence_starts_with_discovery_then_analyze(self):
        seq = get_default_step_sequence("discovery")
        assert seq[0] == StepType.DISCOVERY
        assert seq[1] == StepType.ANALYZE
        assert seq[2] == StepType.PLAN


# --- Task 6: STEP_POOL updates ---

class TestStepPoolUpdates:
    """Verify STEP_POOL reflects analyze merge changes."""

    def test_analyze_outputs_include_new_fields(self):
        outputs = STEP_POOL[StepType.ANALYZE]["outputs"]
        assert "project_summary" in outputs
        assert "relevant_specs" in outputs
        assert "spec_content" in outputs

    def test_analyze_outputs_include_original_fields(self):
        outputs = STEP_POOL[StepType.ANALYZE]["outputs"]
        assert "task_type" in outputs
        assert "scope" in outputs

    def test_project_summary_marked_deprecated(self):
        info = STEP_POOL[StepType.PROJECT_SUMMARY]
        assert info.get("deprecated") is True
        assert "deprecated" in info["description"].lower()

    def test_project_summary_still_has_read_only(self):
        """Deprecated steps retain read_only for backward compat."""
        assert STEP_POOL[StepType.PROJECT_SUMMARY]["read_only"] is True

    def test_read_spec_step_type_removed(self):
        """READ_SPEC fully removed from StepType enum."""
        with pytest.raises(ValueError):
            StepType("read_spec")


# --- Task 7: Stub handlers ---

class TestStubHandlers:
    """Verify deprecated stub handlers exist and forward correctly."""

    def test_step_handlers_uses_project_summary_stub(self):
        assert STEP_HANDLERS[StepType.PROJECT_SUMMARY] is project_summary_stub_handler

    @patch("se3.engine.steps.project_summary_handler")
    def test_project_summary_stub_forwards(self, mock_handler):
        """project_summary_stub_handler should forward to project_summary_handler."""
        mock_handler.return_value = StepStatus.COMPLETED
        step = Step(step_type=StepType.PROJECT_SUMMARY)
        flow = FlowInstance(flow_id="test-stub-ps", task_description="test")

        result = project_summary_stub_handler(step, flow)

        mock_handler.assert_called_once_with(step, flow)
        assert result == StepStatus.COMPLETED

# --- Integration: downstream steps get spec_content from analyze ---

class TestDownstreamSpecContentAccess:
    """Verify plan, implement, verify_spec get spec_content from analyze outputs.

    This is the critical integration path: analyze now provides spec_content
    that was previously provided by read_spec.
    """

    @pytest.fixture
    def sm(self, tmp_path):
        return StateMachine(tmp_path)

    def _flow_with_analyze_and_plan(self, tmp_path):
        """Flow with completed ANALYZE and PLAN steps."""
        flow = _make_flow(tmp_path)
        _add_completed_step(flow, StepType.ANALYZE, {
            "task_type": "feature",
            "scope": "auth module",
            "project_summary": "SE3 project",
            "relevant_specs": ["flow-engine", "base"],
            "spec_content": {"flow-engine": "# FE spec", "base": "# Base spec"},
        })
        _add_completed_step(flow, StepType.PLAN, {
            "plan": {"proposal": {"summary": "Add auth"}, "design": {"overview": "Auth design"}},
            "task_groups": [{"group_id": "G1", "name": "auth", "tasks": []}],
            "spec_changes": [],
        })
        return flow

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_plan_receives_spec_content(self, _cfg, sm, tmp_path):
        flow = _make_flow(tmp_path)
        _add_completed_step(flow, StepType.ANALYZE, {
            "task_type": "feature",
            "scope": "auth",
            "project_summary": "SE3",
            "relevant_specs": ["base"],
            "spec_content": {"base": "# Base"},
        })
        inputs = sm._build_step_inputs(flow, StepType.PLAN)
        assert inputs["spec_content"] == {"base": "# Base"}

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_implement_receives_spec_content(self, _cfg, sm, tmp_path):
        flow = self._flow_with_analyze_and_plan(tmp_path)
        inputs = sm._build_step_inputs(flow, StepType.IMPLEMENT)
        assert inputs["spec_content"] == {"flow-engine": "# FE spec", "base": "# Base spec"}

    @patch("se3.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_verify_spec_receives_spec_content_in_review_flow(self, _cfg, sm, tmp_path):
        """In review flow, verify_spec is the direct consumer of analyze's spec_content."""
        flow = _make_flow(tmp_path, task_type="review")
        _add_completed_step(flow, StepType.ANALYZE, {
            "task_type": "review",
            "scope": "full review",
            "project_summary": "SE3 review",
            "relevant_specs": ["flow-engine"],
            "spec_content": {"flow-engine": "# Flow engine spec"},
        })
        inputs = sm._build_step_inputs(flow, StepType.VERIFY_SPEC)
        assert inputs["spec_content"] == {"flow-engine": "# Flow engine spec"}
        assert inputs["relevant_specs"] == ["flow-engine"]
        assert inputs["project_summary"] == "SE3 review"
