"""Tests for the post-ADJUDICATE reflow and confirmation门 (group G6).

Once an ADJUDICATE step completes, the state machine must:
  * gate a task-description rewrite behind the confirmation门 (default human via
    se3/calls); a plan-only override follows ``confirmation.steps.adjudicate``
    (LLM reviewer or 免确认); a benign ruling needs no confirmation;
  * on a cleared gate (or no gate), reflow: skip IMPLEMENT/TEST and re-run
    SELF_CHECK directly at pass #1 (deferred stash reset), dropping the pending
    fix_instructions (superseded, kept for audit) rather than implementing them;
  * count the re-run as a fix iteration so ``max_fix_iterations`` still caps a
    ruling that fails to converge;
  * on a rejected confirmation, re-run ADJUDICATE via the shared revision loop
    (review_iterations-bounded, no new counter) with the reviewer's feedback.
"""

from __future__ import annotations

from unittest.mock import patch

from se3.config import WorkflowConfig
from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.state_machine import StateMachine


_SELECTED = [
    StepType.IMPLEMENT,
    StepType.TEST,
    StepType.ADJUDICATE,
    StepType.SELF_CHECK,
    StepType.COMMIT,
]


def _make_state_machine(tmp_path, cfg=None):
    with patch("se3.engine.state_machine.PersistenceManager"):
        sm = StateMachine(project_root=tmp_path)
    if cfg is None:
        cfg = WorkflowConfig(max_fix_iterations=100, adjudicate_period=0)
    sm._get_workflow_config = lambda: cfg  # type: ignore[assignment]
    return sm


def _issue(*, expected="returns None", path="a.py", line=1):
    return {
        "severity": "high",
        "actual_behavior": "broken",
        "expected_behavior": expected,
        "divergence": "concrete failure mode",
        "expectation_source": {
            "type": "task_description",
            "verbatim_quote": "handle the empty-input edge case",
        },
        "evidence_lines": [f"{path}:{line}"],
        "missing_in": [],
        "out_of_scope": False,
    }


def _make_post_adjudicate_flow(
    tmp_path,
    *,
    adj_outputs,
    fix_iterations=0,
    deferred_stash=None,
):
    """Flow parked on a just-COMPLETED ADJUDICATE step (as after the handler ran).

    Sequence mirrors what ``_transition_to_adjudicate`` leaves behind: ADJUDICATE
    inserted immediately before the SELF_CHECK slot, current index on ADJUDICATE.
    """
    flow = FlowInstance(
        flow_id="adj-reflow-flow",
        task_description="Implement the parser and handle the empty-input edge case",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = list(_SELECTED)
    flow.state.fix_iterations = fix_iterations
    if deferred_stash is not None:
        flow.state.context["self_check_deferred_issues"] = deferred_stash

    implement = Step(
        step_type=StepType.IMPLEMENT,
        status=StepStatus.COMPLETED,
        outputs={"files_changed": ["a.py"], "summary": "done"},
    )
    flow.state.add_step(implement)

    test = Step(
        step_type=StepType.TEST,
        status=StepStatus.COMPLETED,
        outputs={"test_results": {"passed": True, "overall_passed": True}},
    )
    flow.state.add_step(test)

    self_check = Step(
        step_type=StepType.SELF_CHECK,
        status=StepStatus.REVISION_NEEDED,
        outputs={
            "fix_needed": True,
            "fix_instructions": "fix the empty-input path",
            "fix_context": {"reason": "self_check", "issues": [_issue()]},
            "issues": [_issue()],
        },
    )
    flow.state.add_step(self_check)

    adjudicate = Step(
        step_type=StepType.ADJUDICATE,
        status=StepStatus.COMPLETED,
        inputs={"fix_instructions": "fix the empty-input path"},
        outputs=adj_outputs,
    )
    flow.state.add_step(adjudicate)

    flow.state.current_step_id = adjudicate.step_id
    flow.state.current_step_index = flow.state.selected_steps.index(StepType.ADJUDICATE)
    return flow, implement, test, self_check, adjudicate


_BENIGN = {
    "adjudicated_description": None,
    "adjudicated_plan": None,
    "fix_instructions_superseded": False,
}

_DESC_PATCH = {
    "adjudicated_description": "Implement the parser; on empty input return None.",
    "adjudicated_plan": None,
    "fix_instructions_superseded": True,
    "superseded_fix_instructions": "fix the empty-input path",
}

_PLAN_PATCH = {
    "adjudicated_description": None,
    "adjudicated_plan": [{"group_id": "G1", "name": "parser", "tasks": []}],
    "fix_instructions_superseded": True,
    "superseded_fix_instructions": "fix the empty-input path",
}


# ---------------------------------------------------------------------------
# Task 1 — reflow: skip IMPLEMENT/TEST, re-run SELF_CHECK at pass #1
# ---------------------------------------------------------------------------

class TestReflow:
    def test_benign_ruling_reflows_to_self_check(self, tmp_path):
        """A ruling with no override needs no confirmation; reflow re-runs
        SELF_CHECK directly (IMPLEMENT/TEST skipped)."""
        sm = _make_state_machine(tmp_path)
        flow, implement, test, self_check, adj = _make_post_adjudicate_flow(
            tmp_path, adj_outputs=dict(_BENIGN), fix_iterations=2,
        )

        next_step = sm.transition_to_next(flow)

        assert next_step is not None
        assert next_step.step_type == StepType.SELF_CHECK
        assert next_step.step_id != self_check.step_id  # a fresh SELF_CHECK
        assert flow.state.current_step_id == next_step.step_id
        # IMPLEMENT/TEST skipped — untouched from their prior COMPLETED run.
        assert implement.status == StepStatus.COMPLETED
        assert "fix_instructions" not in implement.inputs
        assert test.status == StepStatus.COMPLETED

    def test_reflow_starts_at_pass_1_and_resets_stash(self, tmp_path):
        sm = _make_state_machine(tmp_path)
        flow, *_ , adj = _make_post_adjudicate_flow(
            tmp_path,
            adj_outputs=dict(_BENIGN),
            fix_iterations=3,
            deferred_stash=[{"stale": "issue"}],
        )

        next_step = sm.transition_to_next(flow)

        assert next_step.inputs["self_check_pass_index"] == 1
        # pass #1 mechanically resets the cross-pass deferred stash.
        assert flow.state.context["self_check_deferred_issues"] == []
        assert next_step.inputs["self_check_deferred_issues"] == []

    def test_reflow_counts_as_fix_iteration(self, tmp_path):
        sm = _make_state_machine(tmp_path)
        flow, *_ , adj = _make_post_adjudicate_flow(
            tmp_path, adj_outputs=dict(_BENIGN), fix_iterations=3,
        )

        next_step = sm.transition_to_next(flow)

        assert flow.state.get_fix_iteration() == 4
        assert next_step.inputs["fix_iteration"] == 4

    def test_superseded_fix_instructions_never_reach_implement(self, tmp_path):
        sm = _make_state_machine(tmp_path)
        flow, implement, test, self_check, adj = _make_post_adjudicate_flow(
            tmp_path, adj_outputs=dict(_PLAN_PATCH), fix_iterations=1,
        )
        # A plan-only ruling with no confirmation config → 免确认 → straight reflow.
        with patch(
            "se3.engine.state_machine.resolve_confirm_inputs", return_value=None
        ):
            next_step = sm.transition_to_next(flow)

        assert next_step.step_type == StepType.SELF_CHECK
        # IMPLEMENT was not re-run with the superseded instructions.
        assert implement.status == StepStatus.COMPLETED
        assert implement.inputs.get("fix_instructions") is None
        # The ruling recorded the supersede for audit.
        assert adj.outputs["fix_instructions_superseded"] is True

    def test_no_synthetic_description_changed_issue(self, tmp_path):
        """The reflow must not fabricate a self_check-style issue announcing the
        description change — implement picks up the new text via the effective-
        text layer instead."""
        sm = _make_state_machine(tmp_path)
        flow, implement, test, self_check, adj = _make_post_adjudicate_flow(
            tmp_path, adj_outputs=dict(_BENIGN), fix_iterations=1,
        )

        next_step = sm.transition_to_next(flow)

        # The fresh SELF_CHECK carries no injected pending issue list of its own;
        # its only issue provenance is prev_self_check_issues (the audit echo).
        assert "issues" not in next_step.outputs
        assert next_step.outputs == {}

    def test_max_fix_iterations_caps_after_reflow(self, tmp_path):
        """The reflow increment keeps the global bound enforceable: once the
        re-run SELF_CHECK trips REVISION_NEEDED at the cap, the flow FAILS."""
        cfg = WorkflowConfig(max_fix_iterations=2, adjudicate_period=0)
        sm = _make_state_machine(tmp_path, cfg)
        flow, implement, test, self_check, adj = _make_post_adjudicate_flow(
            tmp_path, adj_outputs=dict(_BENIGN), fix_iterations=1,
        )

        rerun = sm.transition_to_next(flow)
        assert rerun.step_type == StepType.SELF_CHECK
        assert flow.state.get_fix_iteration() == 2  # now at the bound

        # The re-run finds issues again → REVISION_NEEDED at the cap → FAIL.
        rerun.status = StepStatus.REVISION_NEEDED
        rerun.outputs = {
            "fix_needed": True,
            "fix_instructions": "still broken",
            "issues": [_issue()],
        }
        with patch.object(sm, "_get_issue_discovery", return_value=None):
            result = sm.transition_to_next(flow)

        assert result is None
        assert flow.status == FlowStatus.FAILED


# ---------------------------------------------------------------------------
# Task 2 — confirmation门
# ---------------------------------------------------------------------------

class TestConfirmationGate:
    def test_description_change_inserts_human_confirm(self, tmp_path):
        sm = _make_state_machine(tmp_path)
        flow, implement, test, self_check, adj = _make_post_adjudicate_flow(
            tmp_path, adj_outputs=dict(_DESC_PATCH), fix_iterations=1,
        )
        human_cfg = {"reviewer": "human", "max_iterations": 3, "agents": None}
        with patch(
            "se3.engine.state_machine.resolve_confirm_inputs", return_value=human_cfg
        ):
            next_step = sm.transition_to_next(flow)

        assert next_step.step_type == StepType.CONFIRM
        assert next_step.inputs["reviewer"] == "human"
        assert next_step.inputs["step_to_review_id"] == adj.step_id
        assert next_step.inputs["step_to_review_type"] == "adjudicate"
        # CONFIRM sits between ADJUDICATE and SELF_CHECK.
        adj_idx = flow.state.selected_steps.index(StepType.ADJUDICATE)
        assert flow.state.selected_steps[adj_idx + 1] == StepType.CONFIRM
        # Not yet counted as a fix iteration — the gate has not cleared.
        assert flow.state.get_fix_iteration() == 1

    def test_description_change_forces_human_when_unconfigured(self, tmp_path):
        """A description rewrite is high-impact: with no confirmation config it
        still confirms (human fallback), never silently reflowing."""
        sm = _make_state_machine(tmp_path)
        flow, *_ , adj = _make_post_adjudicate_flow(
            tmp_path, adj_outputs=dict(_DESC_PATCH), fix_iterations=1,
        )
        with patch(
            "se3.engine.state_machine.resolve_confirm_inputs", return_value=None
        ):
            next_step = sm.transition_to_next(flow)

        assert next_step.step_type == StepType.CONFIRM
        assert next_step.inputs["reviewer"] == "human"

    def test_plan_only_no_config_skips_confirmation(self, tmp_path):
        sm = _make_state_machine(tmp_path)
        flow, *_ , adj = _make_post_adjudicate_flow(
            tmp_path, adj_outputs=dict(_PLAN_PATCH), fix_iterations=1,
        )
        with patch(
            "se3.engine.state_machine.resolve_confirm_inputs", return_value=None
        ):
            next_step = sm.transition_to_next(flow)

        assert next_step.step_type == StepType.SELF_CHECK
        assert StepType.CONFIRM not in flow.state.selected_steps

    def test_plan_only_llm_reviewer_inserts_confirm(self, tmp_path):
        sm = _make_state_machine(tmp_path)
        flow, *_ , adj = _make_post_adjudicate_flow(
            tmp_path, adj_outputs=dict(_PLAN_PATCH), fix_iterations=1,
        )
        llm_cfg = {
            "reviewer": None,
            "max_iterations": 3,
            "agents": [{"name": "claude", "type": "claude-code", "cmd": "", "priority": 0}],
        }
        with patch(
            "se3.engine.state_machine.resolve_confirm_inputs", return_value=llm_cfg
        ):
            next_step = sm.transition_to_next(flow)

        assert next_step.step_type == StepType.CONFIRM
        assert next_step.inputs["reviewer"] is None  # LLM (default chain)

    def test_confirm_approved_reflows_and_counts(self, tmp_path):
        """An approved adjudicate-CONFIRM reflows exactly like a direct ruling."""
        sm = _make_state_machine(tmp_path)
        flow, implement, test, self_check, adj = _make_post_adjudicate_flow(
            tmp_path, adj_outputs=dict(_DESC_PATCH), fix_iterations=1,
        )
        # Simulate the inserted, approved CONFIRM.
        adj_idx = flow.state.selected_steps.index(StepType.ADJUDICATE)
        flow.state.selected_steps.insert(adj_idx + 1, StepType.CONFIRM)
        confirm = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.COMPLETED,
            outputs={
                "review_result": {
                    "approved": True,
                    "step_to_review_id": adj.step_id,
                    "step_to_review_type": "adjudicate",
                },
            },
        )
        flow.state.add_step(confirm)
        flow.state.current_step_id = confirm.step_id
        flow.state.current_step_index = adj_idx + 1

        next_step = sm.transition_to_next(flow)

        assert next_step.step_type == StepType.SELF_CHECK
        assert flow.state.get_fix_iteration() == 2
        assert implement.status == StepStatus.COMPLETED

    def test_confirm_rejected_reruns_adjudicate_via_revision(self, tmp_path):
        """A rejected confirmation re-runs ADJUDICATE through the shared revision
        loop (review_iterations-bounded, no fix-iteration double count)."""
        sm = _make_state_machine(tmp_path)
        flow, implement, test, self_check, adj = _make_post_adjudicate_flow(
            tmp_path, adj_outputs=dict(_DESC_PATCH), fix_iterations=1,
        )
        adj_idx = flow.state.selected_steps.index(StepType.ADJUDICATE)
        flow.state.selected_steps.insert(adj_idx + 1, StepType.CONFIRM)
        confirm = Step(
            step_type=StepType.CONFIRM,
            status=StepStatus.REVISION_NEEDED,
            outputs={
                "review_result": {
                    "approved": False,
                    "step_to_review_id": adj.step_id,
                    "step_to_review_type": "adjudicate",
                },
                "revision_feedback": "the rewrite dropped a real requirement",
            },
        )
        flow.state.add_step(confirm)
        flow.state.current_step_id = confirm.step_id
        flow.state.current_step_index = adj_idx + 1

        next_step = sm.transition_to_next(flow)

        # Re-runs the SAME adjudicate step with feedback threaded in.
        assert next_step.step_id == adj.step_id
        assert next_step.step_type == StepType.ADJUDICATE
        assert next_step.status == StepStatus.PENDING
        assert next_step.inputs["is_revision"] is True
        assert next_step.inputs["revision_feedback"] == (
            "the rewrite dropped a real requirement"
        )
        # Reuses the cross-revision review counter (no new counter, no double count).
        assert flow.state.get_review_iteration(adj.step_id) == 1
        # A rejected gate does not consume a fix iteration.
        assert flow.state.get_fix_iteration() == 1
