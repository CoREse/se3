"""Tests for LLM-based reviewer in the confirmation step.

Tests cover:
- Approval path: LLM approves → COMPLETED
- Revision path: LLM requests changes → REVISION_NEEDED
- Max iterations: auto-approve after exceeding limit
- LLM call failure: graceful handling with auto-approve
- Malformed response: treated as not approved
- Review result structure matches expected format
- Config propagation: llm_reviewer dict in step inputs
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)


class TestLLMReviewApproval:
    """Test the LLM reviewer approval path."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        self.flow = FlowInstance(
            task_description="Implement user login feature",
            task_type="feature",
            change_name="test-change",
            change_path=self.project_root / "test-change",
        )
        self.flow.state.selected_steps = [StepType.PROPOSE, StepType.CONFIRM]

        self.propose_step = Step(
            step_type=StepType.PROPOSE,
            status=StepStatus.COMPLETED,
            step_id="propose-001",
        )
        self.propose_step.outputs["proposal"] = "Add login page with OAuth"
        self.flow.state.add_step(self.propose_step)

        self.confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            step_id="confirm-001",
            inputs={
                "step_to_review_id": "propose-001",
                "step_to_review_type": "propose",
                "reviewer": "llm",
                "llm_reviewer": {"model": None, "max_iterations": 3},
            },
        )
        self.flow.state.add_step(self.confirm_step)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("se3.engine.steps.confirm.LLMCaller")
    def test_approval_returns_completed(self, MockLLMCaller):
        """When LLM approves, handler returns COMPLETED."""
        from se3.engine.steps.confirm import confirm_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({
            "approved": True,
            "feedback": "The proposal is comprehensive and well-structured."
        })
        MockLLMCaller.return_value = mock_caller

        result = confirm_handler(self.confirm_step, self.flow)

        assert result == StepStatus.COMPLETED

    @patch("se3.engine.steps.confirm.LLMCaller")
    def test_approval_review_result_structure(self, MockLLMCaller):
        """Review result should have all required fields."""
        from se3.engine.steps.confirm import confirm_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({
            "approved": True,
            "feedback": "Looks good"
        })
        MockLLMCaller.return_value = mock_caller

        confirm_handler(self.confirm_step, self.flow)

        review_result = self.confirm_step.outputs["review_result"]
        assert review_result["approved"] is True
        assert review_result["feedback"] == "Looks good"
        assert review_result["step_to_review_id"] == "propose-001"
        assert review_result["step_to_review_type"] == "propose"
        assert review_result["reviewer"] == "llm"

    @patch("se3.engine.steps.confirm.LLMCaller")
    def test_approval_sets_revision_feedback(self, MockLLMCaller):
        """Revision feedback output should be set even on approval."""
        from se3.engine.steps.confirm import confirm_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({
            "approved": True,
            "feedback": "LGTM"
        })
        MockLLMCaller.return_value = mock_caller

        confirm_handler(self.confirm_step, self.flow)

        assert self.confirm_step.outputs["revision_feedback"] == "LGTM"


class TestLLMReviewRevision:
    """Test the LLM reviewer revision (not approved) path."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        self.flow = FlowInstance(
            task_description="Add error handling",
            task_type="feature",
            change_name="test-change",
            change_path=self.project_root / "test-change",
        )

        self.design_step = Step(
            step_type=StepType.DESIGN,
            status=StepStatus.COMPLETED,
            step_id="design-001",
        )
        self.design_step.outputs["design_doc"] = "Design document here"
        self.flow.state.add_step(self.design_step)

        self.confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            step_id="confirm-002",
            inputs={
                "step_to_review_id": "design-001",
                "step_to_review_type": "design",
                "reviewer": "llm",
                "llm_reviewer": {"model": None, "max_iterations": 3},
            },
        )
        self.flow.state.add_step(self.confirm_step)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("se3.engine.steps.confirm.LLMCaller")
    def test_revision_returns_revision_needed(self, MockLLMCaller):
        """When LLM does not approve, handler returns REVISION_NEEDED."""
        from se3.engine.steps.confirm import confirm_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({
            "approved": False,
            "feedback": "Missing error handling for network failures"
        })
        MockLLMCaller.return_value = mock_caller

        result = confirm_handler(self.confirm_step, self.flow)

        assert result == StepStatus.REVISION_NEEDED

    @patch("se3.engine.steps.confirm.LLMCaller")
    def test_revision_stores_feedback(self, MockLLMCaller):
        """Feedback from LLM should be stored in review_result and revision_feedback."""
        from se3.engine.steps.confirm import confirm_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({
            "approved": False,
            "feedback": "Need to handle edge cases"
        })
        MockLLMCaller.return_value = mock_caller

        confirm_handler(self.confirm_step, self.flow)

        review_result = self.confirm_step.outputs["review_result"]
        assert review_result["approved"] is False
        assert review_result["feedback"] == "Need to handle edge cases"
        assert self.confirm_step.outputs["revision_feedback"] == "Need to handle edge cases"


class TestLLMReviewMaxIterations:
    """Test max_iterations auto-approve safeguard."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        self.flow = FlowInstance(
            task_description="Test task",
            task_type="feature",
            change_name="test-change",
            change_path=self.project_root / "test-change",
        )

        self.propose_step = Step(
            step_type=StepType.PROPOSE,
            status=StepStatus.COMPLETED,
            step_id="propose-001",
        )
        self.propose_step.outputs["proposal"] = "Some proposal"
        self.flow.state.add_step(self.propose_step)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_auto_approve_when_max_iterations_exceeded(self):
        """When _llm_review_iteration >= max_iterations, auto-approve without calling LLM."""
        from se3.engine.steps.confirm import confirm_handler

        confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            step_id="confirm-max",
            inputs={
                "step_to_review_id": "propose-001",
                "step_to_review_type": "propose",
                "reviewer": "llm",
                "llm_reviewer": {"model": None, "max_iterations": 2},
                "_llm_review_iteration": 2,
            },
        )
        self.flow.state.add_step(confirm_step)

        # No LLMCaller mock needed — should not be called
        result = confirm_handler(confirm_step, self.flow)

        assert result == StepStatus.COMPLETED
        assert confirm_step.outputs["review_result"]["approved"] is True
        assert "max review iterations" in confirm_step.outputs["review_result"]["feedback"]

    def test_auto_approve_at_exact_limit(self):
        """Edge case: iteration count exactly at max_iterations triggers auto-approve."""
        from se3.engine.steps.confirm import confirm_handler

        confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            step_id="confirm-edge",
            inputs={
                "step_to_review_id": "propose-001",
                "step_to_review_type": "propose",
                "reviewer": "llm",
                "llm_reviewer": {"model": None, "max_iterations": 3},
                "_llm_review_iteration": 3,
            },
        )
        self.flow.state.add_step(confirm_step)

        result = confirm_handler(confirm_step, self.flow)

        assert result == StepStatus.COMPLETED
        assert confirm_step.outputs["review_result"]["approved"] is True


class TestLLMReviewErrorHandling:
    """Test error handling in LLM reviewer."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

        self.flow = FlowInstance(
            task_description="Test task",
            task_type="feature",
            change_name="test-change",
            change_path=self.project_root / "test-change",
        )

        self.propose_step = Step(
            step_type=StepType.PROPOSE,
            status=StepStatus.COMPLETED,
            step_id="propose-001",
        )
        self.propose_step.outputs["proposal"] = "Test proposal"
        self.flow.state.add_step(self.propose_step)

        self.confirm_step = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.PENDING,
            step_id="confirm-err",
            inputs={
                "step_to_review_id": "propose-001",
                "step_to_review_type": "propose",
                "reviewer": "llm",
                "llm_reviewer": {"model": None, "max_iterations": 3},
            },
        )
        self.flow.state.add_step(self.confirm_step)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("se3.engine.steps.confirm.LLMCaller")
    def test_llm_call_failure_auto_approves(self, MockLLMCaller):
        """When LLM call raises an exception, auto-approve to avoid blocking."""
        from se3.engine.steps.confirm import confirm_handler

        mock_caller = MagicMock()
        mock_caller.call.side_effect = RuntimeError("LLM service unavailable")
        MockLLMCaller.return_value = mock_caller

        result = confirm_handler(self.confirm_step, self.flow)

        assert result == StepStatus.COMPLETED
        review_result = self.confirm_step.outputs["review_result"]
        assert review_result["approved"] is True
        assert "LLM call failure" in review_result["feedback"]

    @patch("se3.engine.steps.confirm.LLMCaller")
    def test_malformed_json_response(self, MockLLMCaller):
        """When LLM returns invalid JSON, treat as not approved."""
        from se3.engine.steps.confirm import confirm_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = "This is not JSON at all"
        MockLLMCaller.return_value = mock_caller

        result = confirm_handler(self.confirm_step, self.flow)

        assert result == StepStatus.REVISION_NEEDED
        review_result = self.confirm_step.outputs["review_result"]
        assert review_result["approved"] is False

    @patch("se3.engine.steps.confirm.LLMCaller")
    def test_json_missing_approved_field(self, MockLLMCaller):
        """When LLM returns JSON without 'approved' key, default to not approved."""
        from se3.engine.steps.confirm import confirm_handler

        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({"feedback": "some feedback"})
        MockLLMCaller.return_value = mock_caller

        result = confirm_handler(self.confirm_step, self.flow)

        # approved defaults to False when missing
        assert result == StepStatus.REVISION_NEEDED


class TestLLMReviewerConfigPropagation:
    """Test that llm_reviewer config is properly propagated to step inputs."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_config_propagation_with_llm_reviewer(self):
        """llm_reviewer dict should be in step.inputs when reviewer is 'llm'."""
        from se3.engine.state_machine import StateMachine
        from se3.engine.persistence import PersistenceManager

        # Create se3.yaml with LLM reviewer config
        (self.project_root / "se3" / "state").mkdir(parents=True, exist_ok=True)
        (self.project_root / "se3" / "specs").mkdir(parents=True, exist_ok=True)
        config_path = self.project_root / "se3.yaml"
        config_path.write_text(
            "confirmation:\n"
            "  enabled: true\n"
            "  steps: [propose]\n"
            "  reviewer: llm\n"
            "  llm_reviewer:\n"
            "    model: claude-sonnet\n"
            "    max_iterations: 5\n"
        )

        persistence = PersistenceManager(self.project_root)
        sm = StateMachine(self.project_root, persistence)

        flow = FlowInstance(
            task_description="Test config propagation",
            task_type="feature",
            change_name="test-cfg",
            change_path=self.project_root / "test-cfg",
        )
        flow.state.selected_steps = [StepType.PROPOSE, StepType.CONFIRM]

        # Create a completed PROPOSE step
        propose_step = Step(
            step_type=StepType.PROPOSE,
            status=StepStatus.COMPLETED,
            step_id="propose-001",
        )
        propose_step.outputs["proposal"] = "Test"
        flow.state.add_step(propose_step)
        flow.state.current_step_id = "propose-001"
        flow.state.current_step_index = 0

        # Transition to CONFIRM step
        next_step = sm.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.CONFIRM
        assert next_step.inputs.get("reviewer") == "llm"
        assert "llm_reviewer" in next_step.inputs
        assert next_step.inputs["llm_reviewer"]["model"] == "claude-sonnet"
        assert next_step.inputs["llm_reviewer"]["max_iterations"] == 5

    def test_config_default_max_iterations(self):
        """Default max_iterations should be 3 when not specified."""
        from se3.engine.state_machine import StateMachine
        from se3.engine.persistence import PersistenceManager

        (self.project_root / "se3" / "state").mkdir(parents=True, exist_ok=True)
        (self.project_root / "se3" / "specs").mkdir(parents=True, exist_ok=True)
        config_path = self.project_root / "se3.yaml"
        config_path.write_text(
            "confirmation:\n"
            "  enabled: true\n"
            "  steps: [propose]\n"
            "  reviewer: llm\n"
        )

        persistence = PersistenceManager(self.project_root)
        sm = StateMachine(self.project_root, persistence)

        flow = FlowInstance(
            task_description="Test defaults",
            task_type="feature",
            change_name="test-default",
            change_path=self.project_root / "test-default",
        )
        flow.state.selected_steps = [StepType.PROPOSE, StepType.CONFIRM]

        propose_step = Step(
            step_type=StepType.PROPOSE,
            status=StepStatus.COMPLETED,
            step_id="propose-001",
        )
        propose_step.outputs["proposal"] = "Test"
        flow.state.add_step(propose_step)
        flow.state.current_step_id = "propose-001"
        flow.state.current_step_index = 0

        next_step = sm.transition_to_next(flow)

        assert next_step.inputs["llm_reviewer"]["max_iterations"] == 3
        assert next_step.inputs["llm_reviewer"]["model"] is None


class TestLLMReviewPrompt:
    """Test the build_llm_review_prompt function."""

    def test_prompt_includes_step_output(self):
        """Prompt should include the reviewed step's output."""
        from se3.engine.context_builder import build_llm_review_prompt

        prompt = build_llm_review_prompt(
            step_to_review_type="propose",
            step_output={"proposal": "Add OAuth login"},
            task_description="Implement user login",
        )

        assert "propose" in prompt.lower()
        assert "Add OAuth login" in prompt
        assert "Implement user login" in prompt
        assert '"approved"' in prompt
        assert '"feedback"' in prompt

    def test_prompt_includes_revision_feedback(self):
        """When revision feedback is provided, it should be in the prompt."""
        from se3.engine.context_builder import build_llm_review_prompt

        prompt = build_llm_review_prompt(
            step_to_review_type="design",
            step_output={"design_doc": "Some design"},
            task_description="Test task",
            revision_feedback="Missing error handling section",
        )

        assert "Missing error handling section" in prompt
        assert "Previous Revision Feedback" in prompt

    def test_prompt_without_revision_feedback(self):
        """When no revision feedback, that section should not appear."""
        from se3.engine.context_builder import build_llm_review_prompt

        prompt = build_llm_review_prompt(
            step_to_review_type="propose",
            step_output={"proposal": "Test"},
            task_description="Test task",
        )

        assert "Previous Revision Feedback" not in prompt

    def test_prompt_includes_evaluation_criteria(self):
        """Prompt should include evaluation criteria."""
        from se3.engine.context_builder import build_llm_review_prompt

        prompt = build_llm_review_prompt(
            step_to_review_type="propose",
            step_output={"proposal": "Test"},
            task_description="Test task",
        )

        assert "Completeness" in prompt
        assert "Correctness" in prompt
        assert "Clarity" in prompt
        assert "Feasibility" in prompt
