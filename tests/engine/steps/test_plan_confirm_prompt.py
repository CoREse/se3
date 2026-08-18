"""Tests for the plan confirm gate: opt-in wiring, prompt content, dispatch.

plan-confirm used to be always-on and always a requirement -> task coverage
review. It is now an ordinary opt-in per-step confirmation whose review subject
follows the flow's persisted decomposition doctrine. These tests pin:

- Config layer: with no ``confirmation.steps.plan`` entry no CONFIRM is inserted
  after PLAN and ``resolve_confirm_inputs('plan')`` returns None; with an entry
  present the reviewer (human or LLM chain) resolves through the same generic
  path every other step uses. A CONFIRM the retired always-on rule already
  wrote into a persisted sequence keeps resolving to the unattended LLM review
  it had before the degrade, instead of falling through to a human gate.
- ``build_plan_confirm_prompt`` content: the capability doctrine yields a
  grouping review (group count vs. the number of mutually independent tasks,
  forbidden artifact-type/layer splits, ``depends_on`` soundness) and explicitly
  does NOT ask for a per-requirement task decomposition; the legacy granular
  doctrine keeps the requirement coverage review verbatim.
- The count dimension follows the flow's ``plan_granularity`` pin: ``single``
  puts the count out of scope (it is a configured guarantee PLAN may not
  deviate from) and ``conservative`` allows the deliberate over-splitting it
  orders, so the gate never demands a regrouping PLAN cannot produce.
- ``_llm_review`` dispatch: a plan confirm routes to ``build_plan_confirm_prompt``
  with both the persisted doctrine and granularity, while a non-plan confirm
  keeps ``build_llm_review_prompt``.
- ``approved`` true/false map to COMPLETED / REVISION_NEEDED, and the
  cross-revision max_iterations cap still auto-approves.
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from tianluo.config import (
    insert_confirmation_steps,
    resolve_confirm_inputs,
    resolve_retired_always_on_confirm_inputs,
)
from tianluo.engine.context_builder import build_plan_confirm_prompt
from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.plan_decomposition import (
    PLAN_DECOMPOSITION_KEY,
    PLAN_GRANULARITY_KEY,
    PlanDecomposition,
    PlanGranularity,
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


class TestRetiredAlwaysOnPlanConfirm:
    """A CONFIRM the retired always-on rule already persisted after PLAN.

    ``selected_steps`` is only ever rebuilt by ANALYZE, so a flow created while
    plan-confirm was always-on resumes with a CONFIRM that has no
    ``confirmation.steps.plan`` entry behind it. Degrading the gate must not
    *strengthen* it into a blocking human approval on those in-flight flows.
    """

    def test_plan_resolves_to_the_default_llm_chain(
        self, tmp_path, isolated_global_home
    ):
        with patch(
            "tianluo.config.shutil.which",
            side_effect=lambda cmd: "/usr/bin/claude" if cmd == "claude" else None,
        ):
            resolved = resolve_retired_always_on_confirm_inputs(
                tmp_path, "plan", flow_predates_degrade=True,
            )
        assert resolved is not None
        assert resolved["reviewer"] is None
        assert [a["name"] for a in resolved["agents"]] == ["claude"]
        # None so the caller applies the same default every LLM reviewer gets.
        assert resolved["max_iterations"] is None

    @pytest.mark.parametrize("step_type", ["implement", "adjudicate", "test"])
    def test_steps_that_were_never_always_on_are_untouched(
        self, tmp_path, isolated_global_home, step_type
    ):
        assert resolve_retired_always_on_confirm_inputs(
            tmp_path, step_type, flow_predates_degrade=True,
        ) is None

    def test_post_degrade_flow_gets_no_retired_resolution(
        self, tmp_path, isolated_global_home
    ):
        # A flow created after the degrade cannot be holding an unconfigured
        # CONFIRM for the old reason, so it must fall through to the caller's
        # drift path instead of buying an unattended LLM review.
        assert resolve_retired_always_on_confirm_inputs(
            tmp_path, "plan", flow_predates_degrade=False,
        ) is None

    @staticmethod
    def _flow_at_plan(tmp_path, *, plan_decomposition=None):
        flow = FlowInstance(
            task_description=TASK_DESCRIPTION,
            task_type="feature",
            change_name="t",
            change_path=tmp_path / "t",
        )
        flow.state.selected_steps = [StepType.PLAN, StepType.CONFIRM]
        plan_step = Step(
            step_type=StepType.PLAN,
            status=StepStatus.COMPLETED,
            step_id="plan-001",
        )
        plan_step.outputs["proposal"] = "P"
        flow.state.add_step(plan_step)
        flow.state.current_step_id = "plan-001"
        flow.state.current_step_index = 0
        if plan_decomposition is not None:
            flow.state.context["plan_decomposition"] = plan_decomposition
        return flow

    def test_config_drift_on_a_post_degrade_flow_falls_back_to_human(
        self, tmp_path, isolated_global_home, caplog
    ):
        """Entry deleted mid-flow on a new flow: warn + human, no LLM review."""
        import logging

        from tianluo.engine.persistence import PersistenceManager
        from tianluo.engine.state_machine import StateMachine

        (tmp_path / "tianluo" / "state").mkdir(parents=True, exist_ok=True)
        sm = StateMachine(tmp_path, PersistenceManager(tmp_path))
        flow = self._flow_at_plan(tmp_path, plan_decomposition="capability")

        with caplog.at_level(logging.WARNING, logger="tianluo.engine.state_machine"):
            with patch(
                "tianluo.config.shutil.which",
                side_effect=lambda cmd: "/usr/bin/claude" if cmd == "claude" else None,
            ):
                next_step = sm.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.CONFIRM
        assert next_step.inputs["reviewer"] == "human"
        assert "agents" not in next_step.inputs
        assert "no entry under confirmation.steps" in caplog.text

    def test_persisted_plan_confirm_runs_llm_review_not_a_human_gate(
        self, tmp_path, isolated_global_home
    ):
        """The end-to-end resume path: no config entry, CONFIRM already there."""
        from tianluo.engine.persistence import PersistenceManager
        from tianluo.engine.state_machine import StateMachine

        (tmp_path / "tianluo" / "state").mkdir(parents=True, exist_ok=True)
        sm = StateMachine(tmp_path, PersistenceManager(tmp_path))

        # No persisted plan_decomposition — the marker of a pre-degrade flow.
        flow = self._flow_at_plan(tmp_path)

        with patch(
            "tianluo.config.shutil.which",
            side_effect=lambda cmd: "/usr/bin/claude" if cmd == "claude" else None,
        ):
            next_step = sm.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.CONFIRM
        assert next_step.inputs["reviewer"] is None
        assert next_step.inputs["agents"]
        assert next_step.inputs["max_iterations"] == 3


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

    def test_reviews_group_count_against_independent_tasks(self):
        low = self._prompt().lower()
        assert "group count" in low
        assert "independent tasks" in low
        assert "safely carry" in low

    def test_reviews_forbidden_artifact_type_splits(self):
        low = " ".join(self._prompt().lower().split())
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
        # The task-unit doctrine: one task one group by default, split only
        # at the capability edge.
        assert "one task that one call can finish" in low
        assert "too large for one call" in low
        assert "one group each" in low
        assert "default to aggregation" in low
        assert "capability edge" in low

    def test_old_split_bias_wording_is_gone(self):
        low = self._prompt().lower()
        for stale in (
            "borderline",
            "volume of work",
            "one capability that one call can finish",
            "be conservative",
        ):
            assert stale not in low

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


class TestGroupingReviewFollowsGranularityPin:
    """The count dimension must not demand what the granularity forbids.

    ``plan_granularity`` pins the group *count*: ``single`` orders PLAN to emit
    exactly one group for the whole requirement (a configured guarantee, not a
    PLAN judgement), and ``conservative`` deliberately splits below the
    capability edge. A reviewer phrased only for ``auto`` would reject those
    plans for a count PLAN is not allowed to change, and the revision loop would
    spin until ``max_iterations`` auto-approves.
    """

    def _prompt(self, granularity, **kwargs):
        return build_plan_confirm_prompt(
            step_output=_plan_output(CAPABILITY_GROUPS),
            task_description=TASK_DESCRIPTION,
            decomposition=PlanDecomposition.CAPABILITY,
            granularity=granularity,
            **kwargs,
        )

    def test_auto_is_the_default_and_keeps_the_count_rule(self):
        assert self._prompt(PlanGranularity.AUTO) == self._prompt(None)
        low = " ".join(self._prompt(None).lower().split())
        assert "does the number of groups equal the number of mutually unrelated" in low

    def test_single_declares_the_count_out_of_scope(self):
        low = " ".join(self._prompt(PlanGranularity.SINGLE).lower().split())
        assert "plan_granularity: single" in low
        assert "do not fail the review on the group count" in low
        # The exact defect the auto wording would have manufactured here.
        assert (
            "a single group covering several mutually unrelated tasks is the "
            "correct output here" in low
        )

    def test_single_drops_the_auto_count_demand(self):
        low = " ".join(self._prompt(PlanGranularity.SINGLE).lower().split())
        assert "does the number of groups equal the number of mutually unrelated" not in low
        assert "unrelated tasks fused into one group (which needlessly" not in low

    def test_conservative_keeps_fusion_defect_but_allows_over_splitting(self):
        low = " ".join(self._prompt(PlanGranularity.CONSERVATIVE).lower().split())
        assert "plan_granularity: conservative" in low
        assert "fused into one group is a defect" in low
        assert "must not be flagged as over-splitting" in low
        # A lowered threshold is not a licence for phase/artifact pre-cuts.
        assert "implementation phases" in low

    def test_string_granularity_accepted(self):
        assert self._prompt("single") == self._prompt(PlanGranularity.SINGLE)

    def test_unknown_granularity_falls_back_to_auto(self):
        assert self._prompt("not-a-granularity") == self._prompt(PlanGranularity.AUTO)

    def test_all_three_variants_keep_the_other_two_dimensions(self):
        for granularity in (
            PlanGranularity.AUTO,
            PlanGranularity.SINGLE,
            PlanGranularity.CONSERVATIVE,
        ):
            prompt = self._prompt(granularity)
            low = prompt.lower()
            assert "forbidden splits by artifact type or layer" in low
            assert "dependency declarations" in low
            assert "depends_on" in prompt

    def test_granularity_ignored_by_the_legacy_granular_branch(self):
        # granular is behaviour-preserving: its coverage review has no count
        # dimension to pin, so the new argument must not alter it.
        base = build_plan_confirm_prompt(
            step_output=_plan_output(),
            task_description=TASK_DESCRIPTION,
            decomposition=PlanDecomposition.GRANULAR,
        )
        for granularity in ("single", "conservative", PlanGranularity.AUTO):
            assert (
                build_plan_confirm_prompt(
                    step_output=_plan_output(),
                    task_description=TASK_DESCRIPTION,
                    decomposition=PlanDecomposition.GRANULAR,
                    granularity=granularity,
                )
                == base
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
    def test_reviewed_step_doctrine_used_when_context_has_none(
        self, MockLLMCaller, mock_plan
    ):
        """PLAN's own record decides the review subject, not the default.

        A flow whose context predates the doctrine keys still records what PLAN
        ran in the PLAN step's outputs; reading only the context would review a
        legacy fine-grained plan under the grouping-granularity prompt.
        """
        from tianluo.engine.steps.confirm import _llm_review

        mock_plan.return_value = "PLAN_PROMPT"
        caller = MagicMock()
        caller.call.return_value = '{"approved": true, "feedback": "ok"}'
        MockLLMCaller.return_value = caller

        confirm = self._make_confirm("plan")
        assert PLAN_DECOMPOSITION_KEY not in self.flow.state.context
        self.flow.state.steps["reviewed-001"].outputs[
            PLAN_DECOMPOSITION_KEY
        ] = "granular"
        _llm_review(confirm, self.flow)

        assert (
            mock_plan.call_args.kwargs["decomposition"] is PlanDecomposition.GRANULAR
        )

    @patch("tianluo.engine.steps.confirm.build_plan_confirm_prompt")
    @patch("tianluo.engine.steps.confirm.LLMCaller")
    def test_reviewed_step_doctrine_outranks_flow_context(
        self, MockLLMCaller, mock_plan
    ):
        """The doctrine PLAN actually ran wins over a stale flow context."""
        from tianluo.engine.steps.confirm import _llm_review

        mock_plan.return_value = "PLAN_PROMPT"
        caller = MagicMock()
        caller.call.return_value = '{"approved": true, "feedback": "ok"}'
        MockLLMCaller.return_value = caller

        self.flow.state.context[PLAN_DECOMPOSITION_KEY] = "granular"
        confirm = self._make_confirm("plan")
        self.flow.state.steps["reviewed-001"].outputs[
            PLAN_DECOMPOSITION_KEY
        ] = "capability"
        _llm_review(confirm, self.flow)

        assert (
            mock_plan.call_args.kwargs["decomposition"] is PlanDecomposition.CAPABILITY
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

    @patch("tianluo.engine.steps.confirm.build_plan_confirm_prompt")
    @patch("tianluo.engine.steps.confirm.LLMCaller")
    def test_persisted_granularity_is_forwarded_to_prompt_builder(
        self, MockLLMCaller, mock_plan
    ):
        """The count pin travels with the doctrine.

        Without it a flow pinned to ``single`` would be reviewed under the
        ``auto`` count rule and rejected for a group count PLAN was ordered to
        emit — a revision it cannot act on.
        """
        from tianluo.engine.steps.confirm import _llm_review

        mock_plan.return_value = "PLAN_PROMPT"
        caller = MagicMock()
        caller.call.return_value = '{"approved": true, "feedback": "ok"}'
        MockLLMCaller.return_value = caller

        self.flow.state.context[PLAN_DECOMPOSITION_KEY] = "capability"
        self.flow.state.context[PLAN_GRANULARITY_KEY] = "single"
        confirm = self._make_confirm("plan")
        _llm_review(confirm, self.flow)

        assert mock_plan.call_args.kwargs["granularity"] is PlanGranularity.SINGLE

    @patch("tianluo.engine.steps.confirm.build_plan_confirm_prompt")
    @patch("tianluo.engine.steps.confirm.LLMCaller")
    def test_default_granularity_forwarded_when_context_empty(
        self, MockLLMCaller, mock_plan
    ):
        from tianluo.engine.steps.confirm import _llm_review

        mock_plan.return_value = "PLAN_PROMPT"
        caller = MagicMock()
        caller.call.return_value = '{"approved": true, "feedback": "ok"}'
        MockLLMCaller.return_value = caller

        confirm = self._make_confirm("plan")
        _llm_review(confirm, self.flow)

        assert mock_plan.call_args.kwargs["granularity"] is PlanGranularity.AUTO

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
