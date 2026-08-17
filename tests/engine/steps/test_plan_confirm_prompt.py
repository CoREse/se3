"""Tests for the plan confirm gate: opt-in wiring, prompt content, dispatch.

plan-confirm used to be always-on and always a requirement -> task coverage
review. It is now an ordinary opt-in per-step confirmation whose review subject
follows the flow's persisted decomposition doctrine. These tests pin:

- Config layer: with no ``confirmation.steps.plan`` entry no CONFIRM is inserted
  after PLAN and ``resolve_confirm_inputs('plan')`` returns None; with an entry
  present the reviewer (human or LLM chain) resolves through the same generic
  path every other step uses.
- ``build_plan_confirm_prompt`` content: the capability doctrine yields a
  grouping review (group count vs. volume, forbidden artifact-type/layer splits,
  ``depends_on`` soundness) and explicitly does NOT ask for a per-requirement
  task decomposition; the legacy granular doctrine keeps the requirement
  coverage review verbatim.
- ``_llm_review`` dispatch: a plan confirm routes to ``build_plan_confirm_prompt``
  while a non-plan confirm keeps ``build_llm_review_prompt``.
- ``approved`` true/false map to COMPLETED / REVISION_NEEDED, and the
  cross-revision max_iterations cap still auto-approves.
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from tianluo.config import insert_confirmation_steps, resolve_confirm_inputs
from tianluo.engine.context_builder import build_plan_confirm_prompt
from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.plan_decomposition import (
    PLAN_DECOMPOSITION_KEY,
    PlanDecomposition,
)


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

CAPABILITY_GROUPS = [
    {
        "group_id": "G1",
        "name": "dry-run export flag",
        "description": "Add --dry-run to the export command and emit a summary line",
        "group_order": 1,
        "depends_on": [],
    },
]


def _plan_output(task_groups=None):
    return {
        "proposal": {"summary": "Implement dry-run export"},
        "design": {"overview": "Wire flag through CLI"},
        "task_groups": TASK_GROUPS if task_groups is None else task_groups,
    }


@pytest.fixture
def isolated_global_home(monkeypatch, tmp_path):
    """Neutralize the real ``~/.se3/config.yaml`` by pointing home at a clean
    temp dir, so only the project's tianluo.yaml (if any) is in play."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home


# ---------------------------------------------------------------------------
# Config layer: plan is an ordinary opt-in per-step confirmation
# ---------------------------------------------------------------------------


class TestPlanConfirmIsOptIn:
    def test_no_config_file_does_not_confirm_plan(self, tmp_path, isolated_global_home):
        # No tianluo.yaml at all → confirmation.steps is empty, so PLAN gets no
        # CONFIRM. This is the degraded default the gate was moved to.
        result = insert_confirmation_steps(
            [StepType.ANALYZE, StepType.PLAN, StepType.IMPLEMENT], tmp_path,
        )
        assert StepType.CONFIRM not in result
        assert result == [StepType.ANALYZE, StepType.PLAN, StepType.IMPLEMENT]

    def test_empty_steps_dict_does_not_confirm_plan(self, tmp_path, isolated_global_home):
        (tmp_path / "tianluo.yaml").write_text("confirmation: {steps: {}}\n")
        result = insert_confirmation_steps([StepType.PLAN, StepType.IMPLEMENT], tmp_path)
        assert StepType.CONFIRM not in result

    def test_other_step_configured_does_not_drag_in_plan_confirm(
        self, tmp_path, isolated_global_home
    ):
        (tmp_path / "tianluo.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    implement: {reviewer: human}\n"
        )
        result = insert_confirmation_steps([StepType.PLAN, StepType.IMPLEMENT], tmp_path)
        impl_idx = result.index(StepType.IMPLEMENT)
        plan_idx = result.index(StepType.PLAN)
        assert result[impl_idx + 1] == StepType.CONFIRM
        assert result[plan_idx + 1] == StepType.IMPLEMENT
        assert result.count(StepType.CONFIRM) == 1

    def test_explicit_plan_entry_inserts_exactly_one_confirm(
        self, tmp_path, isolated_global_home
    ):
        (tmp_path / "tianluo.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        result = insert_confirmation_steps([StepType.PLAN, StepType.IMPLEMENT], tmp_path)
        plan_idx = result.index(StepType.PLAN)
        assert result[plan_idx + 1] == StepType.CONFIRM
        assert result.count(StepType.CONFIRM) == 1

    def test_plan_confirm_not_inserted_when_plan_absent_from_sequence(
        self, tmp_path, isolated_global_home
    ):
        (tmp_path / "tianluo.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        result = insert_confirmation_steps([StepType.IMPLEMENT, StepType.TEST], tmp_path)
        assert StepType.CONFIRM not in result


class TestResolvePlanConfirmInputs:
    def test_plan_unconfigured_returns_none(self, tmp_path, isolated_global_home):
        assert resolve_confirm_inputs(tmp_path, "plan") is None

    def test_plan_unconfigured_matches_other_unconfigured_steps(
        self, tmp_path, isolated_global_home
    ):
        # plan carries no special case any more: it resolves exactly like every
        # other unconfigured step type.
        assert resolve_confirm_inputs(tmp_path, "plan") == resolve_confirm_inputs(
            tmp_path, "implement"
        )

    def test_plan_human_reviewer_gate(self, tmp_path, isolated_global_home):
        # reviewer: human under confirmation.steps.plan is the manual grouping
        # gate the degraded model deliberately keeps available.
        (tmp_path / "tianluo.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human, max_iterations: 5}\n"
        )
        resolved = resolve_confirm_inputs(tmp_path, "plan")
        assert resolved is not None
        assert resolved["reviewer"] == "human"
        assert resolved["max_iterations"] == 5
        assert resolved["agents"] is None

    def test_plan_llm_reviewer_resolves_default_chain(
        self, tmp_path, isolated_global_home
    ):
        (tmp_path / "tianluo.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {}\n"
        )
        # The builtin chain probes PATH, so pin which commands resolve.
        with patch(
            "tianluo.config.shutil.which",
            side_effect=lambda cmd: "/usr/bin/claude" if cmd == "claude" else None,
        ):
            resolved = resolve_confirm_inputs(tmp_path, "plan")
        assert resolved is not None
        assert resolved["reviewer"] is None
        assert resolved["agents"] == [
            {
                "name": "claude",
                "type": "claude-code",
                "cmd": "claude",
                "priority": 0,
                "provider": "anthropic",
            }
        ]
        # No plan-specific max_iterations baking any more; the state machine
        # applies the generic None -> default fallback like every other step.
        assert resolved["max_iterations"] is None


# ---------------------------------------------------------------------------
# Prompt content: capability grouping review vs legacy coverage review
# ---------------------------------------------------------------------------


class TestCapabilityGroupingReviewPrompt:
    def _prompt(self, **kwargs):
        return build_plan_confirm_prompt(
            step_output=_plan_output(CAPABILITY_GROUPS),
            task_description=TASK_DESCRIPTION,
            decomposition=PlanDecomposition.CAPABILITY,
            **kwargs,
        )

    def test_default_decomposition_is_capability(self):
        # An omitted doctrine falls back to the current default, not to legacy.
        assert self._prompt() == build_plan_confirm_prompt(
            step_output=_plan_output(CAPABILITY_GROUPS),
            task_description=TASK_DESCRIPTION,
        )

    def test_string_decomposition_accepted(self):
        assert self._prompt() == build_plan_confirm_prompt(
            step_output=_plan_output(CAPABILITY_GROUPS),
            task_description=TASK_DESCRIPTION,
            decomposition="capability",
        )

    def test_reviews_group_count_against_volume(self):
        low = self._prompt().lower()
        assert "group count" in low
        assert "volume of work" in low
        assert "safely carry" in low

    def test_reviews_forbidden_artifact_type_splits(self):
        low = self._prompt().lower()
        assert "artifact type" in low
        assert "test group" in low and "docs group" in low and "config group" in low
        assert "layer" in low

    def test_reviews_dependency_declarations(self):
        prompt = self._prompt()
        assert "depends_on" in prompt
        low = prompt.lower()
        assert "cycle" in low or "cycles" in low
        assert "dangling" in low

    def test_does_not_ask_for_per_requirement_decomposition(self):
        prompt = self._prompt()
        low = prompt.lower()
        assert "do not ask for the requirements to be decomposed" in low
        assert "numbered list of discrete, atomic" not in low
        assert "requirement-by-requirement" not in low
        assert "every requirement has a corresponding task" not in low

    def test_states_the_grouping_doctrine_criteria(self):
        low = self._prompt().lower()
        # The three doctrine cases plus the conservative borderline rule.
        assert "one capability that one call can finish" in low
        assert "too large for one call" in low
        assert "still one group" in low
        assert "borderline" in low

    def test_embeds_task_description_and_groups(self):
        prompt = self._prompt()
        assert "--dry-run flag to the export command" in prompt
        assert "task_groups" in prompt
        assert "dry-run export flag" in prompt

    def test_includes_json_schema(self):
        prompt = self._prompt()
        assert '"approved"' in prompt
        assert '"feedback"' in prompt

    def test_includes_revision_feedback_block(self):
        prompt = self._prompt(
            revision_feedback="G3 was a standalone test group; fold it back in.",
        )
        assert "Previous Revision Feedback" in prompt
        assert "standalone test group" in prompt

    def test_no_revision_block_when_absent(self):
        assert "Previous Revision Feedback" not in self._prompt()

    def test_unknown_decomposition_falls_back_to_capability(self):
        # An unrecognized persisted value must not silently select the legacy
        # coverage review.
        assert (
            build_plan_confirm_prompt(
                step_output=_plan_output(CAPABILITY_GROUPS),
                task_description=TASK_DESCRIPTION,
                decomposition="not-a-doctrine",
            )
            == self._prompt()
        )


class TestGranularCoverageReviewPrompt:
    def _prompt(self, **kwargs):
        return build_plan_confirm_prompt(
            step_output=_plan_output(),
            task_description=TASK_DESCRIPTION,
            decomposition=PlanDecomposition.GRANULAR,
            **kwargs,
        )

    def test_prompt_requests_requirement_decomposition_and_coverage(self):
        prompt = self._prompt()
        low = prompt.lower()
        # Requirement decomposition from task_description.
        assert "discrete" in low and "requirement" in low
        assert "task_description" in prompt
        # Per-requirement coverage check ("every requirement has a covering task").
        assert "every requirement has" in low or "requirement-by-requirement" in low
        assert "covering task" in low or "corresponding task" in low

    def test_prompt_embeds_task_description_and_task_groups(self):
        prompt = self._prompt()
        assert "--dry-run flag to the export command" in prompt
        assert "task_groups" in prompt
        assert "dry-run flag" in prompt

    def test_prompt_includes_json_schema(self):
        prompt = self._prompt()
        assert '"approved"' in prompt
        assert '"feedback"' in prompt

    def test_prompt_includes_revision_feedback_block(self):
        prompt = self._prompt(
            revision_feedback="Requirement 2 (audit log) had no covering task.",
        )
        assert "Previous Revision Feedback" in prompt
        assert "audit log" in prompt

    def test_prompt_no_revision_block_when_absent(self):
        assert "Previous Revision Feedback" not in self._prompt()

    def test_legacy_text_preserved_verbatim(self):
        # granular is a behaviour-preserving legacy value: pin the exact
        # wording of the review procedure so a future edit to the capability
        # branch cannot drift it.
        prompt = self._prompt()
        assert (
            "Your one and only job here is to verify **requirement coverage**"
            in prompt
        )
        assert "1. **Decompose discrete requirements from the task_description**" in prompt
        assert "2. **Check requirement-by-requirement coverage**" in prompt
        assert "3. **List uncovered requirements**" in prompt
        assert (
            "Approve only if every discrete requirement maps to at least one "
            "covering task." in prompt
        )

    def test_capability_and_granular_prompts_differ(self):
        capability = build_plan_confirm_prompt(
            step_output=_plan_output(),
            task_description=TASK_DESCRIPTION,
            decomposition=PlanDecomposition.CAPABILITY,
        )
        assert capability != self._prompt()


# ---------------------------------------------------------------------------
# _llm_review dispatch and outcome mapping
# ---------------------------------------------------------------------------


class TestLlmReviewDispatch:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        (self.project_root / "tianluo" / "calls").mkdir(parents=True, exist_ok=True)

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

    @patch("tianluo.engine.steps.confirm.build_plan_confirm_prompt")
    @patch("tianluo.engine.steps.confirm.LLMCaller")
    def test_persisted_doctrine_is_forwarded_to_prompt_builder(
        self, MockLLMCaller, mock_plan
    ):
        """The review subject follows the doctrine the flow was planned with."""
        from tianluo.engine.steps.confirm import _llm_review

        mock_plan.return_value = "PLAN_PROMPT"
        caller = MagicMock()
        caller.call.return_value = '{"approved": true, "feedback": "ok"}'
        MockLLMCaller.return_value = caller

        self.flow.state.context[PLAN_DECOMPOSITION_KEY] = "granular"
        confirm = self._make_confirm("plan")
        _llm_review(confirm, self.flow)

        assert (
            mock_plan.call_args.kwargs["decomposition"] is PlanDecomposition.GRANULAR
        )

    @patch("tianluo.engine.steps.confirm.build_plan_confirm_prompt")
    @patch("tianluo.engine.steps.confirm.LLMCaller")
    def test_default_doctrine_forwarded_when_context_empty(
        self, MockLLMCaller, mock_plan
    ):
        from tianluo.engine.steps.confirm import _llm_review

        mock_plan.return_value = "PLAN_PROMPT"
        caller = MagicMock()
        caller.call.return_value = '{"approved": true, "feedback": "ok"}'
        MockLLMCaller.return_value = caller

        confirm = self._make_confirm("plan")
        _llm_review(confirm, self.flow)

        assert (
            mock_plan.call_args.kwargs["decomposition"] is PlanDecomposition.CAPABILITY
        )

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
        caller.call.return_value = '{"approved": false, "feedback": "G3 is a test-only group"}'
        MockLLMCaller.return_value = caller

        confirm = self._make_confirm("plan")
        status, result = _llm_review(confirm, self.flow)

        assert status == StepStatus.REVISION_NEEDED
        assert result["approved"] is False

    @patch("tianluo.engine.steps.confirm.LLMCaller")
    def test_plan_approved_true_returns_completed(self, MockLLMCaller):
        from tianluo.engine.steps.confirm import _llm_review

        caller = MagicMock()
        caller.call.return_value = '{"approved": true, "feedback": "grouping is sound"}'
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
