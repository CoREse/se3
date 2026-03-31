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

    def test_run_calls_record_baseline_commit(self, tmp_path):
        """The run() method calls _record_baseline_commit before executing steps."""
        sm = StateMachine(tmp_path)

        flow = FlowInstance(task_description="test", status=FlowStatus.INIT)
        flow.state.selected_steps = [StepType.ANALYZE]
        step = Step(step_type=StepType.ANALYZE, status=StepStatus.PENDING)
        flow.state.add_step(step)
        flow.state.current_step_id = step.step_id

        # Register a handler that completes immediately
        sm.register_handler(StepType.ANALYZE, lambda s, f: StepStatus.COMPLETED)

        with patch.object(sm, "_record_baseline_commit") as mock_record, \
             patch.object(sm, "_write_flow_meta"):
            sm.run(flow)

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
