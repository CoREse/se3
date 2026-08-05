"""Tests for the conditional INVESTIGATE round in front of a bugfix's PLAN.

A bugfix is the one task type where the *mechanism* may still be unknown when
planning starts. analyze therefore emits a ``root_cause_clear`` judgement, and
``_update_flow_steps`` turns a false judgement into an INVESTIGATE step placed
immediately before PLAN.

Three properties here fail quietly rather than loudly, so each gets its own
case:

1. **An explicit ``--type bugfix`` skips classification but NOT the judgement.**
   The override happens in ``_handle_type_conflict``, downstream of where the
   raw LLM result is read — if the extraction ever moved behind the override,
   every hand-typed bugfix would silently lose its investigation round.
2. **A ``--discover`` run gets it through the same path.** Such a flow keeps
   ``task_type == 'discovery'`` (to preserve its sequence), so the decision has
   to key off the *effective* type, not the sequence type.
3. **The insertion sits at the front of the rebuild chain.** Under
   ``--worktree`` the merge pair must still end the sequence and the CONFIRM
   gate must still sit directly after PLAN — inserting later would land the
   investigation on the wrong side of one of them.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.steps.analyze import _update_flow_steps


BUGFIX_BASELINE = [
    StepType.ANALYZE,
    StepType.PLAN,
    StepType.CONFIRM,
    StepType.IMPLEMENT,
    StepType.TEST,
    StepType.SELF_CHECK,
    StepType.INVARIANT_CHECK,
    StepType.CHARTER_FRESHNESS,
    StepType.VERSION_ANALYZE,
    StepType.COMMIT,
    StepType.SUMMARIZE,
]

BUGFIX_WITH_INVESTIGATION = [
    StepType.ANALYZE,
    StepType.INVESTIGATE,
    *BUGFIX_BASELINE[1:],
]


def _make_flow(project_root: Path, *, task_type: str, worktree: bool = False):
    flow = FlowInstance(
        flow_id="test-flow-conditional-investigate",
        task_description="Uploads intermittently 500 on large files",
        task_type=task_type,
        status=FlowStatus.RUNNING,
        is_worktree_mode=worktree,
    )
    flow.state.context["project_root"] = str(project_root)
    return flow


def _run_analyze(
    project_root: Path,
    *,
    explicit_type=None,
    llm_task_type: str = "bugfix",
    root_cause_clear=False,
    worktree: bool = False,
):
    """Drive the real ``analyze_handler`` with a stubbed LLM and collector."""
    from tianluo.engine.steps import analyze as analyze_mod

    flow = _make_flow(
        project_root,
        task_type=explicit_type or "pending",
        worktree=worktree,
    )
    if explicit_type:
        flow.state.context["explicit_type"] = explicit_type

    step = Step(step_type=StepType.ANALYZE, step_id="analyze-001")
    step.inputs["task_description"] = flow.task_description

    llm_result = {
        "task_type": llm_task_type,
        "scope": "upload handler",
        "complexity": "medium",
        "reasoning": "because",
    }
    if root_cause_clear is not None:
        llm_result["root_cause_clear"] = root_cause_clear

    with patch.object(analyze_mod, "_collect_project_summary", return_value="ctx"), \
            patch.object(analyze_mod, "LLMCaller") as MockCaller, \
            patch.object(analyze_mod, "parse_json_response", return_value=llm_result):
        # context_builder helpers are imported lazily inside the handler.
        import tianluo.engine.context_builder as cb
        with patch.object(cb, "get_issue_discovery_injection", return_value=""), \
                patch.object(cb, "get_charter_injection", return_value=""), \
                patch.object(cb, "get_code_index_injection", return_value=""), \
                patch.object(cb, "ensure_code_index_fresh", return_value=None), \
                patch.object(cb, "get_runtime_environment_injection", return_value=""):
            MockCaller.return_value.call.return_value = "{}"
            status = analyze_mod.analyze_handler(step, flow)

    assert status == StepStatus.COMPLETED
    return flow, step


@pytest.fixture
def project_root():
    """An empty project dir, so no repo tianluo.yaml colours the rebuild."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


# --- (a)/(b) automatic classification, both judgements ---------------------


class TestAutoClassifiedBugfix:
    def test_unclear_root_cause_inserts_investigate_before_plan(self, project_root):
        flow, step = _run_analyze(project_root, root_cause_clear=False)

        assert step.outputs["root_cause_clear"] is False
        assert flow.state.selected_steps == BUGFIX_WITH_INVESTIGATION
        # Placement, stated independently of the expected table: the
        # investigation must precede the plan it exists to inform.
        steps = flow.state.selected_steps
        assert steps.index(StepType.INVESTIGATE) < steps.index(StepType.PLAN)

    def test_clear_root_cause_keeps_the_existing_sequence(self, project_root):
        flow, step = _run_analyze(project_root, root_cause_clear=True)

        assert step.outputs["root_cause_clear"] is True
        assert StepType.INVESTIGATE not in flow.state.selected_steps
        assert flow.state.selected_steps == BUGFIX_BASELINE


# --- (c) explicit --type bugfix -------------------------------------------


class TestExplicitBugfixTypeStillJudged:
    """An explicit type overrides classification, never the root-cause call."""

    def test_explicit_bugfix_with_unclear_root_cause_inserts_investigate(
        self, project_root
    ):
        # The LLM classified this as a feature; the user said bugfix. The user
        # wins on type — and the LLM's root-cause judgement still applies.
        flow, step = _run_analyze(
            project_root,
            explicit_type="bugfix",
            llm_task_type="feature",
            root_cause_clear=False,
        )

        assert flow.task_type == "bugfix"
        assert step.outputs["root_cause_clear"] is False
        assert flow.state.selected_steps == BUGFIX_WITH_INVESTIGATION

    def test_explicit_bugfix_with_clear_root_cause_skips_investigate(
        self, project_root
    ):
        flow, _step = _run_analyze(
            project_root,
            explicit_type="bugfix",
            llm_task_type="feature",
            root_cause_clear=True,
        )

        assert flow.state.selected_steps == BUGFIX_BASELINE

    def test_explicit_feature_type_suppresses_the_insertion(self, project_root):
        """The user's type decides the sequence: a feature has no bugfix branch."""
        flow, _step = _run_analyze(
            project_root,
            explicit_type="feature",
            llm_task_type="bugfix",
            root_cause_clear=False,
        )

        assert flow.task_type == "feature"
        assert StepType.INVESTIGATE not in flow.state.selected_steps


# --- (d) --discover entry --------------------------------------------------


class TestDiscoverEntryGetsInsertion:
    def test_discovery_flow_with_bugfix_root_type_inserts_investigate(
        self, project_root
    ):
        """flow.task_type stays 'discovery'; the decision keys off the real type."""
        flow, step = _run_analyze(
            project_root,
            explicit_type="discovery",
            llm_task_type="bugfix",
            root_cause_clear=False,
        )

        # The run mode is preserved so the discovery sequence / resume hold...
        assert flow.task_type == "discovery"
        assert flow.state.selected_steps[0] == StepType.DISCOVERY
        # ...and the investigation still lands before the plan.
        steps = flow.state.selected_steps
        assert StepType.INVESTIGATE in steps
        assert steps.index(StepType.INVESTIGATE) < steps.index(StepType.PLAN)
        assert steps.index(StepType.ANALYZE) < steps.index(StepType.INVESTIGATE)
        assert step.outputs["root_cause_clear"] is False

    def test_discovery_flow_with_clear_root_cause_is_untouched(self, project_root):
        flow, _step = _run_analyze(
            project_root,
            explicit_type="discovery",
            llm_task_type="bugfix",
            root_cause_clear=True,
        )

        assert StepType.INVESTIGATE not in flow.state.selected_steps

    def test_discovery_flow_with_feature_root_type_is_untouched(self, project_root):
        flow, _step = _run_analyze(
            project_root,
            explicit_type="discovery",
            llm_task_type="feature",
            root_cause_clear=False,
        )

        assert StepType.INVESTIGATE not in flow.state.selected_steps


# --- (e) worktree mode ------------------------------------------------------


class TestWorktreeRebuildOrdering:
    def test_merge_tail_and_confirm_gate_survive_the_insertion(self, project_root):
        flow, _step = _run_analyze(
            project_root, root_cause_clear=False, worktree=True
        )

        steps = flow.state.selected_steps
        assert StepType.INVESTIGATE in steps
        # The merge pair still lands immediately after commit (the release point).
        commit_at = steps.index(StepType.COMMIT)
        assert steps[commit_at + 1] == StepType.MERGE_INTEGRATE
        assert steps[commit_at + 2] == StepType.VERSION_RECONCILE
        # The plan gate is still directly after plan, not after the investigation.
        plan_at = steps.index(StepType.PLAN)
        assert steps[plan_at + 1] == StepType.CONFIRM
        assert steps[plan_at - 1] == StepType.INVESTIGATE


# --- (f) every other task type ---------------------------------------------


class TestOtherTaskTypesUnaffected:
    @pytest.mark.parametrize("task_type", ["feature", "small", "review", "survey"])
    def test_needs_investigation_does_not_change_other_types(
        self, project_root, task_type
    ):
        """Even if the flag were passed, only sequences with a PLAN can take it.

        feature/small/review/survey are compared against their own no-flag
        rebuild, so this stays true as those sequences evolve.
        """
        baseline_flow = _make_flow(project_root, task_type=task_type)
        _update_flow_steps(baseline_flow, task_type)
        baseline = list(baseline_flow.state.selected_steps)

        flagged_flow = _make_flow(project_root, task_type=task_type)
        _update_flow_steps(flagged_flow, task_type, needs_investigation=True)

        if task_type == "feature":
            # feature has a PLAN, so the flag would take effect — but analyze
            # never sets it for a non-bugfix. Guarded by the classification
            # tests above; here we only pin that nothing else shifted.
            assert flagged_flow.state.selected_steps == [
                baseline[0],
                StepType.INVESTIGATE,
                *baseline[1:],
            ]
        else:
            assert flagged_flow.state.selected_steps == baseline

    @pytest.mark.parametrize("task_type", ["small", "review", "survey"])
    def test_planless_sequences_take_no_insertion(self, project_root, task_type):
        """No PLAN, no insertion — and no exception either."""
        flow = _make_flow(project_root, task_type=task_type)
        _update_flow_steps(flow, task_type, needs_investigation=True)

        assert StepType.PLAN not in flow.state.selected_steps
        assert flow.state.selected_steps.count(StepType.INVESTIGATE) == (
            1 if task_type == "survey" else 0
        ), "survey owns exactly one INVESTIGATE of its own; nothing was added"

    @pytest.mark.parametrize("llm_type", ["feature", "small", "review", "survey"])
    def test_non_bugfix_classification_never_investigates_for_the_root_cause(
        self, project_root, llm_type
    ):
        """A false judgement on a non-bugfix task must not add a round."""
        flow, _step = _run_analyze(
            project_root, llm_task_type=llm_type, root_cause_clear=False
        )

        expected = 1 if llm_type == "survey" else 0
        assert flow.state.selected_steps.count(StepType.INVESTIGATE) == expected


# --- (g) the prompt that produces the judgement ----------------------------


class TestRootCauseJudgementPrompt:
    """The judgement is only worth reading if the prompt actually asks for one.

    ``root_cause_clear`` is read off the RAW result, before the explicit-``--type``
    override — precisely so a hand-typed bugfix keeps its investigation round.
    That carve-out is worthless if the prompt lets the model rubber-stamp ``true``
    whenever its OWN classification is not "bugfix": a task worded as a desired
    change ("it sometimes returns empty, make it not") reads as a feature to the
    classifier, so the user's ``--type bugfix`` would arrive with a judgement
    nobody ever made.
    """

    def test_prompt_does_not_license_a_blanket_true_for_non_bugfix(self) -> None:
        from tianluo.engine.steps.analyze import ANALYZE_PROMPT

        assert "this field is irrelevant" not in ANALYZE_PROMPT

    def test_prompt_decouples_the_judgement_from_the_classification(self) -> None:
        from tianluo.engine.steps.analyze import ANALYZE_PROMPT

        lowered = ANALYZE_PROMPT.lower()
        assert "independently of the task_type" in lowered
        # The symptom vocabulary is what makes the judgement reachable for a task
        # the classifier did not call a bugfix.
        assert "intermittent" in lowered
        assert "regression" in lowered
