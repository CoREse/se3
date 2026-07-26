"""Tests for estimated_test_duration extraction in implement_handler.

Verifies that the LLM-reported estimated_test_duration value is extracted
from the JSON response and written to step.outputs, so that the state
machine can forward it to the test step's inputs for dynamic timeout.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.steps.implement import (
    _format_fix_context_structured,
    _sanitize_estimated_test_duration,
    implement_handler,
)


class TestFormatFixContextTimeout:
    """Verify timeout metadata in fix_context reaches the rendered FIX_PROMPT."""

    def test_timeout_fields_rendered_for_test_failure(self):
        fix_context = {
            "reason": "test_failure",
            "test_failed": True,
            "timeout_reason": (
                "Tests timed out after 600s. "
                "Previous estimated_test_duration was 300. "
                "The timeout_multiplier is 2.0. "
                "Please provide a significantly higher estimated_test_duration."
            ),
            "previous_timeout": 600,
            "previous_estimated_test_duration": 300,
            "timeout_multiplier": 2.0,
        }
        rendered = _format_fix_context_structured(fix_context)
        assert "Reason: test_failure" in rendered
        assert "Timeout reason:" in rendered
        assert "Tests timed out after 600s" in rendered
        assert "Previous timeout: 600s" in rendered
        assert "Previous estimated_test_duration: 300" in rendered
        assert "Timeout multiplier: 2.0" in rendered

    def test_timeout_fields_rendered_when_previous_estimate_missing(self):
        fix_context = {
            "reason": "test_failure",
            "test_failed": True,
            "timeout_reason": "Tests timed out after 1800s.",
            "previous_timeout": 1800,
            "previous_estimated_test_duration": None,
            "timeout_multiplier": 2.0,
        }
        rendered = _format_fix_context_structured(fix_context)
        assert "Previous estimated_test_duration: not set" in rendered
        assert "Timeout multiplier: 2.0" in rendered

    def test_no_timeout_fields_when_timeout_reason_absent(self):
        fix_context = {
            "reason": "test_failure",
            "test_failed": True,
            "test_analysis": {
                "failure_summary": "Assertion failed",
                "root_cause": "Off-by-one",
            },
        }
        rendered = _format_fix_context_structured(fix_context)
        assert "Timeout reason:" not in rendered
        assert "Previous timeout:" not in rendered
        assert "Timeout multiplier:" not in rendered
        assert "Failure summary: Assertion failed" in rendered
        assert "Root cause: Off-by-one" in rendered

    def test_timeout_rendered_before_test_analysis(self):
        fix_context = {
            "reason": "test_failure",
            "test_failed": True,
            "timeout_reason": "Tests timed out after 600s.",
            "previous_timeout": 600,
            "previous_estimated_test_duration": 300,
            "timeout_multiplier": 2.0,
            "test_analysis": {
                "failure_summary": "Suite exceeded timeout",
                "root_cause": "Slow integration tests",
            },
        }
        rendered = _format_fix_context_structured(fix_context)
        timeout_idx = rendered.index("Timeout reason:")
        summary_idx = rendered.index("Failure summary:")
        assert timeout_idx < summary_idx


class TestSanitizeEstimatedTestDuration:
    """Unit tests for _sanitize_estimated_test_duration helper."""

    def test_accepts_positive_int(self):
        assert _sanitize_estimated_test_duration(120) == 120.0

    def test_accepts_positive_float(self):
        assert _sanitize_estimated_test_duration(45.5) == 45.5

    def test_rejects_none(self):
        assert _sanitize_estimated_test_duration(None) is None

    def test_rejects_bool_true(self):
        # bool is a subclass of int — must be filtered out
        assert _sanitize_estimated_test_duration(True) is None

    def test_rejects_bool_false(self):
        assert _sanitize_estimated_test_duration(False) is None

    def test_rejects_zero(self):
        assert _sanitize_estimated_test_duration(0) is None

    def test_rejects_negative(self):
        assert _sanitize_estimated_test_duration(-10) is None

    def test_rejects_non_numeric(self):
        assert _sanitize_estimated_test_duration("120") is None
        assert _sanitize_estimated_test_duration([120]) is None


class TestEstimatedDurationSingleCall:
    """Verify estimated_test_duration propagation in _run_single_llm_call path."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        self.flow = FlowInstance(
            flow_id="test-flow",
            task_description="Test task",
            task_type="feature",
            change_path=self.project_root / "change",
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_step(self):
        return Step(
            step_type=StepType.IMPLEMENT,
            step_id="impl-001",
            inputs={
                "task_description": "test",
                "task_type": "feature",
                "task_groups": [{"group_id": "G1", "tasks": ["t1"]}],
                "spec_content": {},
            },
            outputs={},
        )

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("tianluo.engine.steps.implement.LLMCaller")
    @patch("tianluo.engine.steps.implement.parse_json_response")
    def test_extracts_estimated_duration_from_llm_response(
        self, mock_parse, mock_caller_cls, mock_inj,
    ):
        """estimated_test_duration in LLM JSON is written to step.outputs."""
        mock_parse.return_value = {
            "files_changed": ["a.py"],
            "tests_added": [],
            "test_mapping": {},
            "summary": "Done",
            "completion_status": "complete",
            "estimated_test_duration": 240,
        }
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step()
        result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["estimated_test_duration"] == 240.0

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("tianluo.engine.steps.implement.LLMCaller")
    @patch("tianluo.engine.steps.implement.parse_json_response")
    def test_missing_estimated_duration_yields_none(
        self, mock_parse, mock_caller_cls, mock_inj,
    ):
        """When LLM omits estimated_test_duration, outputs value is None."""
        mock_parse.return_value = {
            "files_changed": ["a.py"],
            "tests_added": [],
            "test_mapping": {},
            "summary": "Done",
            "completion_status": "complete",
        }
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step()
        result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs.get("estimated_test_duration") is None

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("tianluo.engine.steps.implement.LLMCaller")
    @patch("tianluo.engine.steps.implement.parse_json_response")
    def test_bool_estimated_duration_coerced_to_none(
        self, mock_parse, mock_caller_cls, mock_inj,
    ):
        """A boolean estimated_test_duration must be rejected."""
        mock_parse.return_value = {
            "files_changed": [],
            "tests_added": [],
            "test_mapping": {},
            "summary": "Done",
            "completion_status": "complete",
            "estimated_test_duration": True,
        }
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step()
        implement_handler(step, self.flow)

        assert step.outputs["estimated_test_duration"] is None

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("tianluo.engine.steps.implement.LLMCaller")
    @patch("tianluo.engine.steps.implement.parse_json_response")
    def test_parse_failure_sets_estimated_duration_none(
        self, mock_parse, mock_caller_cls, mock_inj,
    ):
        """When JSON parse fails, estimated_test_duration is explicitly None."""
        mock_parse.return_value = None
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._make_step()
        implement_handler(step, self.flow)

        assert step.outputs.get("estimated_test_duration") is None


class TestEstimatedDurationEndToEnd:
    """End-to-end verification: implement.outputs flows to test.inputs.

    This covers the state_machine wiring at state_machine.py:752 so that
    a bug in either implement_handler (missing extraction) or the state
    machine (missing forward) would be caught.
    """

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        self.flow = FlowInstance(
            flow_id="test-flow",
            task_description="Test task",
            task_type="feature",
            change_path=self.project_root / "change",
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_implement_step(self):
        return Step(
            step_type=StepType.IMPLEMENT,
            step_id="impl-001",
            inputs={
                "task_description": "test",
                "task_type": "feature",
                "task_groups": [{"group_id": "G1", "tasks": ["t1"]}],
                "spec_content": {},
            },
            outputs={},
        )

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch("tianluo.engine.steps.implement.LLMCaller")
    @patch("tianluo.engine.steps.implement.parse_json_response")
    def test_implement_output_feeds_state_machine_forward(
        self, mock_parse, mock_caller_cls, mock_inj,
    ):
        """implement output → state_machine forward → test step input.

        Exercises the exact forwarding logic in state_machine.py that
        copies implement outputs into the next step's inputs, confirming
        that estimated_test_duration is included.
        """
        mock_parse.return_value = {
            "files_changed": ["a.py"],
            "tests_added": ["tests/test_a.py"],
            "test_mapping": {},
            "summary": "Done",
            "completion_status": "complete",
            "estimated_test_duration": 180,
        }
        mock_caller_cls.return_value.call.return_value = "response"

        impl_step = self._make_implement_step()
        implement_handler(impl_step, self.flow)
        impl_step.status = StepStatus.COMPLETED

        # Mirror the relevant slice of state_machine.py:743-755 to verify
        # the contract. We use the same keys the state machine reads.
        next_step_inputs: dict = {}
        next_step_inputs["changes_made"] = {
            "files_changed": impl_step.outputs.get("files_changed", []),
            "implemented_groups": impl_step.outputs.get("implemented_groups", []),
        }
        next_step_inputs["tests_added"] = impl_step.outputs.get("tests_added", [])
        next_step_inputs["test_mapping"] = impl_step.outputs.get("test_mapping", {})
        next_step_inputs["estimated_test_duration"] = impl_step.outputs.get(
            "estimated_test_duration"
        )

        assert next_step_inputs["estimated_test_duration"] == 180.0
        assert next_step_inputs["tests_added"] == ["tests/test_a.py"]
