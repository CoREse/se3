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

from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
    STEP_POOL,
    get_default_step_sequence,
)
from tianluo.engine.state_machine import StateMachine
from tianluo.engine.steps import (
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
            "root_cause_clear": True,
            "project_summary": "Project: SE3 Framework, branch: main",
            "relevant_specs": ["flow-engine", "base"],
            "spec_content": {
                "base": "# Base spec content",
                "flow-engine": "# Flow engine spec content",
            },
        })
        return flow

    @patch("tianluo.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_analyze_forwards_project_summary(self, _cfg, sm, flow_with_analyze):
        inputs = sm._build_step_inputs(flow_with_analyze, StepType.PLAN)
        assert inputs["project_summary"] == "Project: SE3 Framework, branch: main"

    @patch("tianluo.engine.state_machine.resolve_confirm_inputs", return_value=None)
    def test_analyze_forwards_task_type_and_scope(self, _cfg, sm, flow_with_analyze):
        """Original ANALYZE outputs still forwarded."""
        inputs = sm._build_step_inputs(flow_with_analyze, StepType.PLAN)
        assert inputs["task_type"] == "feature"
        assert inputs["scope"] == "engine module"


class TestBuildStepInputsDeprecatedBackwardCompat:
    """Verify deprecated PROJECT_SUMMARY branch still works for old flows."""

    @pytest.fixture
    def sm(self, tmp_path):
        return StateMachine(tmp_path)

    @patch("tianluo.engine.state_machine.resolve_confirm_inputs", return_value=None)
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

    # Kept identical to tests/engine/test_step_sequence.py::ALL_TASK_TYPES so a
    # newly added task type cannot slip past one sweep while the other covers it.
    ALL_TASK_TYPES = ["feature", "bugfix", "review", "small", "survey", "discovery"]

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

    def test_review_sequence_analyze_then_invariant_check(self):
        # Charter refactor: VERIFY_SPEC retired, replaced by INVARIANT_CHECK.
        seq = get_default_step_sequence("review")
        assert seq == [StepType.ANALYZE, StepType.INVARIANT_CHECK, StepType.SUMMARIZE]

    def test_small_sequence(self):
        """Small sequence never had PROJECT_SUMMARY; charter refactor adds the
        non-blocking CHARTER_FRESHNESS before VERSION_ANALYZE."""
        seq = get_default_step_sequence("small")
        assert seq == [
            StepType.ANALYZE,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.CHARTER_FRESHNESS,
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
            StepType.SUMMARIZE,
        ]

    def test_survey_sequence(self):
        """survey is a pure investigation flow: ANALYZE → INVESTIGATE → SUMMARIZE,
        with no SELF_CHECK (there is no code change for it to check)."""
        seq = get_default_step_sequence("survey")
        assert seq == [
            StepType.ANALYZE,
            StepType.INVESTIGATE,
            StepType.SUMMARIZE,
        ]
        assert StepType.SELF_CHECK not in seq

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

    def test_analyze_outputs_declare_root_cause_clear(self):
        """The root-cause judgement that gates the conditional INVESTIGATE."""
        assert "root_cause_clear" in STEP_POOL[StepType.ANALYZE]["outputs"]

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

    @patch("tianluo.engine.steps.project_summary_handler")
    def test_project_summary_stub_forwards(self, mock_handler):
        """project_summary_stub_handler should forward to project_summary_handler."""
        mock_handler.return_value = StepStatus.COMPLETED
        step = Step(step_type=StepType.PROJECT_SUMMARY)
        flow = FlowInstance(flow_id="test-stub-ps", task_description="test")

        result = project_summary_stub_handler(step, flow)

        mock_handler.assert_called_once_with(step, flow)
        assert result == StepStatus.COMPLETED
