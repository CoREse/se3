"""Tests for baseline commit recording and VERSION_ANALYZE forwarding.

Tests verify:
1. Baseline commit is recorded before flow execution starts
2. Baseline commit is NOT overwritten on resume
3. VERSION_ANALYZE outputs are forwarded to COMMIT step inputs
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.state_machine import StateMachine


class TestBaselineCommitRecording:
    """Test cases for baseline commit recording."""

    def test_record_baseline_commit_sets_hash(self, tmp_path):
        """Baseline commit is set to current HEAD hash when not already set."""
        sm = StateMachine(tmp_path)
        flow = FlowInstance(task_description="test")

        assert flow.baseline_commit is None

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=0,
                stdout="abc123def456\n",
            )
            sm._record_baseline_commit(flow)

        assert flow.baseline_commit == "abc123def456"
        mock_run.assert_called_once_with(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )

    def test_record_baseline_commit_does_not_overwrite(self, tmp_path):
        """Baseline commit is NOT overwritten if already set (resume scenario)."""
        sm = StateMachine(tmp_path)
        flow = FlowInstance(task_description="test", baseline_commit="existing_hash")

        with patch("subprocess.run") as mock_run:
            sm._record_baseline_commit(flow)

        assert flow.baseline_commit == "existing_hash"
        mock_run.assert_not_called()

    def test_record_baseline_commit_handles_git_failure(self, tmp_path):
        """Baseline commit stays None if git rev-parse fails."""
        sm = StateMachine(tmp_path)
        flow = FlowInstance(task_description="test")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1, stdout="")
            sm._record_baseline_commit(flow)

        assert flow.baseline_commit is None

    def test_record_baseline_commit_handles_exception(self, tmp_path):
        """Baseline commit stays None if subprocess raises."""
        sm = StateMachine(tmp_path)
        flow = FlowInstance(task_description="test")

        with patch("subprocess.run", side_effect=OSError("no git")):
            sm._record_baseline_commit(flow)

        assert flow.baseline_commit is None

    def test_record_baseline_commit_persists_flow(self, tmp_path):
        """Flow state is saved after recording baseline commit."""
        sm = StateMachine(tmp_path)
        sm.persistence = Mock()
        flow = FlowInstance(task_description="test")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="abc123\n")
            sm._record_baseline_commit(flow)

        sm.persistence.save_flow.assert_called_once_with(flow)

    def test_init_flow_calls_record_baseline_commit(self, tmp_path):
        """init_flow() calls _record_baseline_commit."""
        sm = StateMachine(tmp_path)

        flow = FlowInstance(task_description="test", status=FlowStatus.INIT)

        with patch.object(sm, "_record_baseline_commit") as mock_record, \
             patch.object(sm, "_write_flow_meta"):
            sm.init_flow(flow)

        mock_record.assert_called_once_with(flow)


class TestVersionAnalyzeForwarding:
    """Test cases for VERSION_ANALYZE output forwarding to COMMIT step."""

    def test_version_analyze_outputs_forwarded_to_commit(self, tmp_path):
        """VERSION_ANALYZE outputs (bump_type, reasoning, etc.) are included in COMMIT inputs."""
        sm = StateMachine(tmp_path)

        flow = FlowInstance(task_description="test task")
        flow.state.selected_steps = [
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
        ]

        # Add a completed VERSION_ANALYZE step with outputs
        va_step = Step(
            step_type=StepType.VERSION_ANALYZE,
            status=StepStatus.COMPLETED,
            outputs={
                "bump_type": "minor",
                "reasoning": "New feature added",
                "confidence": "high",
                "suggested_version": "1.2.0",
            },
        )
        flow.state.add_step(va_step)

        inputs = sm._build_step_inputs(flow, StepType.COMMIT)

        assert inputs["bump_type"] == "minor"
        assert inputs["reasoning"] == "New feature added"
        assert inputs["confidence"] == "high"
        assert inputs["suggested_version"] == "1.2.0"

    def test_version_analyze_not_forwarded_when_absent(self, tmp_path):
        """When no VERSION_ANALYZE step has run, its outputs are not in COMMIT inputs."""
        sm = StateMachine(tmp_path)

        flow = FlowInstance(task_description="test task")
        flow.state.selected_steps = [StepType.COMMIT]

        inputs = sm._build_step_inputs(flow, StepType.COMMIT)

        assert "bump_type" not in inputs
        assert "reasoning" not in inputs
        assert "confidence" not in inputs
        assert "suggested_version" not in inputs

    def test_version_analyze_partial_outputs_forwarded(self, tmp_path):
        """Partial VERSION_ANALYZE outputs are forwarded (missing keys become None)."""
        sm = StateMachine(tmp_path)

        flow = FlowInstance(task_description="test task")
        flow.state.selected_steps = [StepType.VERSION_ANALYZE, StepType.COMMIT]

        va_step = Step(
            step_type=StepType.VERSION_ANALYZE,
            status=StepStatus.COMPLETED,
            outputs={"bump_type": "patch"},
        )
        flow.state.add_step(va_step)

        inputs = sm._build_step_inputs(flow, StepType.COMMIT)

        assert inputs["bump_type"] == "patch"
        assert inputs["reasoning"] is None
        assert inputs["confidence"] is None
        assert inputs["suggested_version"] is None


class TestUserInterjectionsInTaskDescription:
    """Test cases for ``flow.state.context["user_interjections"]`` being
    composed onto every step's effective ``inputs["task_description"]``
    by ``_build_step_inputs``.

    The composer is unit-tested in ``test_task_description_composer.py``;
    these tests pin the integration with state_machine — that the section
    appears in every downstream step regardless of step type, that the
    refined_description overwrite still happens first, and that the
    section is absent when no interjections exist.
    """

    def test_no_interjections_leaves_task_description_untouched(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = FlowInstance(task_description="original task")
        flow.state.selected_steps = [StepType.IMPLEMENT]
        # No interjections in context

        inputs = sm._build_step_inputs(flow, StepType.IMPLEMENT)

        assert inputs["task_description"] == "original task"
        assert "## Additional Instructions" not in inputs["task_description"]

    def test_interjections_appended_to_task_description(self, tmp_path):
        sm = StateMachine(tmp_path)
        flow = FlowInstance(task_description="original task")
        flow.state.selected_steps = [StepType.IMPLEMENT]
        flow.state.context["user_interjections"] = [
            {"text": "redirect to using SQLAlchemy",
             "step_id": "01_analyze_xxx", "step_type": "analyze",
             "timestamp": "2026-05-10T10:00:00"},
        ]

        inputs = sm._build_step_inputs(flow, StepType.IMPLEMENT)

        assert inputs["task_description"].startswith("original task")
        assert "## Additional Instructions (added during run)" in inputs["task_description"]
        assert "redirect to using SQLAlchemy" in inputs["task_description"]
        assert "[analyze@2026-05-10T10:00:00]" in inputs["task_description"]

    def test_interjections_appended_after_refined_description_overwrite(self, tmp_path):
        """When discovery has produced a refined_description, the
        interjections must be appended onto the REFINED text, not the
        original — so users always see the interjection on top of the
        currently-effective task wording.
        """
        sm = StateMachine(tmp_path)
        flow = FlowInstance(task_description="original task")
        flow.state.selected_steps = [StepType.DISCOVERY, StepType.PLAN]
        # Add a completed DISCOVERY step whose outputs include a
        # refined_description; state_machine forwards it into
        # ``inputs["refined_description"]`` for downstream steps.
        discovery_step = Step(
            step_type=StepType.DISCOVERY,
            status=StepStatus.COMPLETED,
            outputs={"refined_description": "refined task scope"},
        )
        flow.state.add_step(discovery_step)
        flow.state.context["user_interjections"] = [
            {"text": "extra constraint", "step_type": "plan",
             "timestamp": "2026-05-10T10:00:00"},
        ]

        inputs = sm._build_step_inputs(flow, StepType.PLAN)

        # original_task_description preserved for traceability
        assert inputs["original_task_description"] == "original task"
        # task_description starts with refined (not original)
        assert inputs["task_description"].startswith("refined task scope")
        # And gets the interjection section appended
        assert "## Additional Instructions" in inputs["task_description"]
        assert "extra constraint" in inputs["task_description"]

    def test_interjections_propagate_across_step_types(self, tmp_path):
        """The same interjection list must be applied to every step type
        that goes through ``_build_step_inputs``."""
        sm = StateMachine(tmp_path)
        flow = FlowInstance(task_description="task")
        flow.state.selected_steps = [
            StepType.ANALYZE, StepType.PLAN, StepType.IMPLEMENT,
        ]
        flow.state.context["user_interjections"] = [
            {"text": "instruction A", "step_type": "analyze",
             "timestamp": "t1"},
        ]

        for step_type in (StepType.ANALYZE, StepType.PLAN, StepType.IMPLEMENT):
            inputs = sm._build_step_inputs(flow, step_type)
            assert "instruction A" in inputs["task_description"], (
                f"interjection must reach {step_type.value} step"
            )
