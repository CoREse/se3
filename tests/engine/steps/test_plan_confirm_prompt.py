"""Tests for the plan-specific requirement-coverage confirm prompt and dispatch.

Covers:
- ``build_plan_confirm_prompt`` content: requirement decomposition, per-requirement
  coverage check, JSON schema, embedding of task_description + task_groups, and the
  optional Previous Revision Feedback block.
- ``_llm_review`` dispatch: a plan confirm routes to ``build_plan_confirm_prompt``
  while a non-plan confirm keeps ``build_llm_review_prompt``.
- ``approved`` true/false map to COMPLETED / REVISION_NEEDED, and the cross-revision
  max_iterations cap still auto-approves.
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from tianluo.engine.context_builder import build_plan_confirm_prompt
from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType


TASK_DESCRIPTION = (
    "Add a --dry-run flag to the export command and emit a summary line. "
    "Also persist the export timestamp to the audit log."
)

TASK_GROUPS = [
    {
        "group_id": "G1",
        "name": "dry-run flag",
        "tasks": [
            {"id": 1, "description": "Add --dry-run flag to export command"},
        ],
    },
]


def _plan_output():
    return {
        "proposal": {"summary": "Implement dry-run export"},
        "design": {"overview": "Wire flag through CLI"},
        "task_groups": TASK_GROUPS,
    }


class TestBuildPlanConfirmPrompt:
    def test_prompt_requests_requirement_decomposition_and_coverage(self):
        prompt = build_plan_confirm_prompt(
            step_output=_plan_output(),
            task_description=TASK_DESCRIPTION,
        )
        low = prompt.lower()
        # Requirement decomposition from task_description.
        assert "discrete" in low and "requirement" in low
        assert "task_description" in prompt
        # Per-requirement coverage check ("every requirement has a covering task").
        assert "every requirement has" in low or "requirement-by-requirement" in low
        assert "covering task" in low or "corresponding task" in low

    def test_prompt_embeds_task_description_and_task_groups(self):
        prompt = build_plan_confirm_prompt(
            step_output=_plan_output(),
            task_description=TASK_DESCRIPTION,
        )
        assert "--dry-run flag to the export command" in prompt
        # task_groups content is rendered into the prompt.
        assert "task_groups" in prompt
        assert "dry-run flag" in prompt

    def test_prompt_includes_json_schema(self):
        prompt = build_plan_confirm_prompt(
            step_output=_plan_output(),
            task_description=TASK_DESCRIPTION,
        )
        assert '"approved"' in prompt
        assert '"feedback"' in prompt

    def test_prompt_includes_revision_feedback_block(self):
        prompt = build_plan_confirm_prompt(
            step_output=_plan_output(),
            task_description=TASK_DESCRIPTION,
            revision_feedback="Requirement 2 (audit log) had no covering task.",
        )
        assert "Previous Revision Feedback" in prompt
        assert "audit log" in prompt

    def test_prompt_no_revision_block_when_absent(self):
        prompt = build_plan_confirm_prompt(
            step_output=_plan_output(),
            task_description=TASK_DESCRIPTION,
        )
        assert "Previous Revision Feedback" not in prompt


class TestLlmReviewDispatch:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        (self.project_root / "se3" / "calls").mkdir(parents=True, exist_ok=True)

        self.flow = FlowInstance(
            task_description=TASK_DESCRIPTION,
            task_type="feature",
            change_name="test-change",
            change_path=self.project_root / "test-change",
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_confirm(self, reviewed_type):
        reviewed = Step(
            step_type=StepType.PLAN if reviewed_type == "plan" else StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            step_id="reviewed-001",
        )
        reviewed.outputs["plan"] = _plan_output()
        self.flow.state.add_step(reviewed)

        confirm = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            step_id="confirm-001",
            inputs={
                "step_to_review_id": "reviewed-001",
                "step_to_review_type": reviewed_type,
                "reviewer": "llm",
                "max_iterations": 3,
            },
        )
        self.flow.state.add_step(confirm)
        self.flow.state.current_step_id = "confirm-001"
        return confirm

    @patch("tianluo.engine.steps.confirm.build_llm_review_prompt")
    @patch("tianluo.engine.steps.confirm.build_plan_confirm_prompt")
    @patch("tianluo.engine.steps.confirm.LLMCaller")
    def test_plan_uses_plan_confirm_prompt(self, MockLLMCaller, mock_plan, mock_generic):
        from tianluo.engine.steps.confirm import _llm_review

        mock_plan.return_value = "PLAN_PROMPT"
        mock_generic.return_value = "GENERIC_PROMPT"
        caller = MagicMock()
        caller.call.return_value = '{"approved": true, "feedback": "all covered"}'
        MockLLMCaller.return_value = caller

        confirm = self._make_confirm("plan")
        status, result = _llm_review(confirm, self.flow)

        assert mock_plan.called
        assert not mock_generic.called
        assert caller.call.call_args.kwargs["prompt"] == "PLAN_PROMPT"
        assert status == StepStatus.COMPLETED

    @patch("tianluo.engine.steps.confirm.build_llm_review_prompt")
    @patch("tianluo.engine.steps.confirm.build_plan_confirm_prompt")
    @patch("tianluo.engine.steps.confirm.LLMCaller")
    def test_non_plan_uses_generic_prompt(self, MockLLMCaller, mock_plan, mock_generic):
        from tianluo.engine.steps.confirm import _llm_review

        mock_plan.return_value = "PLAN_PROMPT"
        mock_generic.return_value = "GENERIC_PROMPT"
        caller = MagicMock()
        caller.call.return_value = '{"approved": true, "feedback": "ok"}'
        MockLLMCaller.return_value = caller

        confirm = self._make_confirm("implement")
        status, result = _llm_review(confirm, self.flow)

        assert mock_generic.called
        assert not mock_plan.called
        assert caller.call.call_args.kwargs["prompt"] == "GENERIC_PROMPT"

    @patch("tianluo.engine.steps.confirm.LLMCaller")
    def test_plan_approved_false_returns_revision_needed(self, MockLLMCaller):
        from tianluo.engine.steps.confirm import _llm_review

        caller = MagicMock()
        caller.call.return_value = '{"approved": false, "feedback": "audit log requirement uncovered"}'
        MockLLMCaller.return_value = caller

        confirm = self._make_confirm("plan")
        status, result = _llm_review(confirm, self.flow)

        assert status == StepStatus.REVISION_NEEDED
        assert result["approved"] is False

    @patch("tianluo.engine.steps.confirm.LLMCaller")
    def test_plan_approved_true_returns_completed(self, MockLLMCaller):
        from tianluo.engine.steps.confirm import _llm_review

        caller = MagicMock()
        caller.call.return_value = '{"approved": true, "feedback": "every requirement covered"}'
        MockLLMCaller.return_value = caller

        confirm = self._make_confirm("plan")
        status, result = _llm_review(confirm, self.flow)

        assert status == StepStatus.COMPLETED
        assert result["approved"] is True

    @patch("tianluo.engine.steps.confirm.LLMCaller")
    def test_max_iterations_auto_approves_without_calling_llm(self, MockLLMCaller):
        """Cross-revision counter at the cap auto-approves before any LLM call."""
        from tianluo.engine.steps.confirm import _llm_review

        caller = MagicMock()
        MockLLMCaller.return_value = caller

        confirm = self._make_confirm("plan")
        # Drive the persisted cross-revision counter to the cap.
        for _ in range(3):
            self.flow.state.increment_review_iteration("reviewed-001")

        status, result = _llm_review(confirm, self.flow)

        assert status == StepStatus.COMPLETED
        assert result["approved"] is True
        assert not caller.call.called
