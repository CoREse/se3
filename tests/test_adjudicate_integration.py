"""End-to-end integration tests for the fix-loop adjudication ("警察") mechanism.

These exercise the *whole* adjudication path through the real state machine and
the real SELF_CHECK / ADJUDICATE step handlers (only the LLM call is mocked),
proving the acceptance criteria as one connected flow rather than per-unit:

  * A self-contradictory task description makes SELF_CHECK flag the same code
    location in opposite directions on consecutive rounds; within ≤2 rounds the
    oscillation trigger routes to ADJUDICATE instead of the fix loop.
  * The ruling's override description is gated by the human confirmation门; once
    approved the flow reflows — skipping IMPLEMENT/TEST — and re-runs SELF_CHECK
    at pass #1 against the adjudicated text. The dead-clause issue that re-quotes
    the abolished clause is dropped by the source-pool switch, the flip stops,
    and the flow converges to COMMIT.
  * The cross-round fingerprint ledger accumulates across a ``--resume``
    boundary (State.to_dict / from_dict round-trip), so an oscillation split
    across the resume is still detected.
  * The periodic backstop forces an ADJUDICATE every N fix iterations even with
    no structural signal.

The se3 TEST step is never executed here (IMPLEMENT/TEST are simulated by status
setting), so no ``SE3_TEST_RUNNING`` recursion is possible; the guard-clearing
fixture below is a belt-and-suspenders honoring of the co-located-test
convention for failure-asserting flows.
"""

from __future__ import annotations

import json
import os
from unittest.mock import Mock, patch

import pytest

from se3.config import WorkflowConfig
from se3.engine import adjudication
from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.state_machine import StateMachine
from se3.engine.steps.adjudicate import adjudicate_handler
from se3.engine.steps.self_check import self_check_handler


# --------------------------------------------------------------------------- #
# Fixtures + task fixtures
# --------------------------------------------------------------------------- #

# The contradiction: the same sentence demands both return-None and raise.
CONTRADICTORY_TASK = (
    "Implement empty_check(x): return None when x is None, and raise "
    "ValueError when x is None."
)
# Both oscillating rounds cite this exact clause (same position); only their
# expected_behavior flips — the structural oscillation signal.
CLAUSE = "return None when x is None, and raise ValueError when x is None"
# The ruling removes the raise half, leaving a self-consistent spec.
ADJUDICATED_TASK = "Implement empty_check(x): return None when x is None."
# A quote that survives ONLY in the superseded original — dropped once the
# source pool switches to the adjudicated text.
DEAD_QUOTE = "raise ValueError when x is None"


@pytest.fixture(autouse=True)
def _clear_test_running_env():
    """Honor se3_test_running_recursion_guard for failure-asserting flows."""
    prior = os.environ.pop("SE3_TEST_RUNNING", None)
    try:
        yield
    finally:
        if prior is not None:
            os.environ["SE3_TEST_RUNNING"] = prior


def _cfg(**overrides):
    base = dict(
        max_fix_iterations=100,
        adjudicate_period=0,
        self_check_defer_fix_threshold=0,  # defer off → single issue → fix now
    )
    base.update(overrides)
    return WorkflowConfig(**base)


def _make_sm(tmp_path, cfg):
    with patch("se3.engine.state_machine.PersistenceManager"):
        sm = StateMachine(project_root=tmp_path)
    sm._get_workflow_config = lambda: cfg  # type: ignore[assignment]
    sm._get_self_check_passes_required = lambda: 1  # type: ignore[assignment]
    return sm


def _make_flow(tmp_path):
    """A flow parked just past a COMPLETED IMPLEMENT+TEST, ready for SELF_CHECK."""
    flow = FlowInstance(
        flow_id="adj-integ-flow",
        task_description=CONTRADICTORY_TASK,
        task_type="feature",
        status=FlowStatus.RUNNING,
        change_path=tmp_path / "changes" / "c",
    )
    flow.state.selected_steps = [
        StepType.IMPLEMENT,
        StepType.TEST,
        StepType.SELF_CHECK,
        StepType.COMMIT,
    ]
    implement = Step(
        step_type=StepType.IMPLEMENT,
        status=StepStatus.COMPLETED,
        outputs={
            "files_changed": [{"path": "src/foo.py", "action": "modify"}],
            "summary": "implemented empty_check",
        },
    )
    flow.state.add_step(implement)
    test = Step(
        step_type=StepType.TEST,
        status=StepStatus.COMPLETED,
        outputs={"test_results": {"passed": True, "overall_passed": True}},
    )
    flow.state.add_step(test)
    return flow, implement, test


def _issue(*, expected, quote=CLAUSE, path="src/foo.py", line=5, severity="high"):
    return {
        "severity": severity,
        "actual_behavior": "the implementation does the opposite",
        "expected_behavior": expected,
        "divergence": "on empty input the behavior contradicts the spec",
        "expectation_source": {"type": "task_description", "verbatim_quote": quote},
        "evidence_lines": [f"{path}:{line}"],
        "missing_in": [],
        "out_of_scope": False,
    }


def _new_self_check(sm, flow):
    """Build a fresh PENDING SELF_CHECK via the real input assembler."""
    inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)
    # The evidence file must be in the changed set for validation to keep issues.
    inputs["changes_made"] = {
        "files_changed": [{"path": "src/foo.py", "action": "modify"}]
    }
    step = Step(step_type=StepType.SELF_CHECK, status=StepStatus.PENDING, inputs=inputs)
    flow.state.add_step(step)
    flow.state.current_step_id = step.step_id
    flow.state.current_step_index = flow.state.selected_steps.index(StepType.SELF_CHECK)
    return step


def _run_self_check(step, flow, issues, resolutions=None):
    payload = {"issues": issues, "summary": "review"}
    if resolutions is not None:
        payload["previous_issue_resolutions"] = resolutions
    with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
        caller = Mock()
        caller.call.return_value = json.dumps(payload)
        mock_cls.return_value = caller
        status = self_check_handler(step, flow)
    # The executor normally writes the returned status back onto the step;
    # replicate that so the next transition sees the terminal state.
    step.status = status
    return status


def _run_adjudicate(step, flow, ruling):
    with patch("se3.engine.steps.adjudicate.LLMCaller") as mock_cls:
        caller = Mock()
        caller.call.return_value = json.dumps(ruling)
        mock_cls.return_value = caller
        status = adjudicate_handler(step, flow)
    step.status = status
    return status


_RULING = {
    "contradiction_type": "internal_contradiction",
    "adjudicated_description": ADJUDICATED_TASK,
    "adjudicated_plan": None,
    "adjudication_rationale": (
        "The spec demanded both return None and raise on the same input; keeping "
        "the return-None branch resolves the contradiction with a minimal edit."
    ),
    "candidate_verdicts": [{"id": 0, "verdict": "contradiction", "reason": "opposing"}],
}

# A no-op ruling: the flagged position is NOT a real spec contradiction — the
# reviewers merely diverged. Carries no override patch (review_divergence).
_BENIGN_RULING = {
    "contradiction_type": "review_divergence",
    "adjudicated_description": None,
    "adjudicated_plan": None,
    "adjudication_rationale": (
        "The two rounds reflect reviewer taste, not a spec conflict; the "
        "description is internally consistent, so there is nothing to adjudicate."
    ),
    "candidate_verdicts": [{"id": 0, "verdict": "benign", "reason": "reviewer divergence"}],
}


# --------------------------------------------------------------------------- #
# 1. Full contradiction → adjudicate → converge
# --------------------------------------------------------------------------- #

class TestContradictionConverges:
    def test_oscillation_triggers_adjudicate_within_two_rounds_and_converges(
        self, tmp_path
    ):
        cfg = _cfg()
        sm = _make_sm(tmp_path, cfg)
        flow, implement, test = _make_flow(tmp_path)

        # --- Round 1: reviewer demands "raise" at the contradictory clause. ---
        sc1 = _new_self_check(sm, flow)
        assert _run_self_check(sc1, flow, [_issue(expected="raise ValueError")]) == (
            StepStatus.REVISION_NEEDED
        )
        # No prior round yet → not an oscillation → normal fix loop (IMPLEMENT).
        nxt = sm.transition_to_next(flow)
        assert nxt.step_type == StepType.IMPLEMENT
        assert StepType.ADJUDICATE not in flow.state.selected_steps
        assert flow.state.get_fix_iteration() == 1
        # Simulate the fix landing (code now raises) without running TEST.
        nxt.status = StepStatus.COMPLETED
        nxt.outputs = {"files_changed": [{"path": "src/foo.py", "action": "modify"}]}

        # --- Round 2: reviewer flips to "return None" at the SAME clause. ---
        sc2 = _new_self_check(sm, flow)
        assert _run_self_check(sc2, flow, [_issue(expected="return None")]) == (
            StepStatus.REVISION_NEEDED
        )
        # Same position, opposite expectation across rounds → oscillation → route
        # to ADJUDICATE rather than a third fix iteration.
        adj = sm.transition_to_next(flow)
        assert adj.step_type == StepType.ADJUDICATE
        assert flow.state.current_step_id == adj.step_id
        # Only two SELF_CHECK rounds were needed to trip the adjudicator.
        ledger = flow.state.context[adjudication.LEDGER_KEY]
        assert ledger["round_count"] == 2

        # --- The ruling: rewrite the description to drop the raise clause. ---
        assert _run_adjudicate(adj, flow, _RULING) == StepStatus.COMPLETED
        assert adj.outputs["adjudicated_description"] == ADJUDICATED_TASK
        assert adj.outputs["adjudication_rationale"]
        assert adj.outputs["adjudicated_at"]  # timestamp for the audit trail
        # The pending fix_instructions from round 2 are recorded superseded.
        assert adj.outputs["fix_instructions_superseded"] is True
        assert adj.outputs["superseded_fix_instructions"]
        # The abolition is STAGED but not yet applied — a ruling's ledger side
        # effects land only after the confirmation门 clears (issue 1).
        assert adj.outputs["ledger_effects_applied"] is False
        assert not any(o["abolished"] for o in ledger["observations"])
        assert adj.outputs["abolished_fingerprints"]

        # --- Description change → human confirmation门. ---
        human = {"reviewer": "human", "max_iterations": 3, "agents": None}
        with patch(
            "se3.engine.state_machine.resolve_confirm_inputs", return_value=human
        ):
            confirm = sm.transition_to_next(flow)
        assert confirm.step_type == StepType.CONFIRM
        assert confirm.inputs["step_to_review_id"] == adj.step_id

        # --- Human approves; the flow reflows to SELF_CHECK (pass #1). ---
        confirm.status = StepStatus.COMPLETED
        confirm.outputs = {
            "review_result": {
                "approved": True,
                "step_to_review_id": adj.step_id,
                "step_to_review_type": StepType.ADJUDICATE.value,
            }
        }
        flow.state.current_step_id = confirm.step_id
        sc3 = sm.transition_to_next(flow)
        assert sc3.step_type == StepType.SELF_CHECK
        assert sc3.inputs["self_check_pass_index"] == 1
        # Reflow counted as a fix iteration; IMPLEMENT/TEST were NOT re-run — the
        # reflow jumped straight from the approved CONFIRM to a fresh SELF_CHECK.
        assert flow.state.get_fix_iteration() == 2
        assert implement.status == StepStatus.COMPLETED  # never reset for a re-run
        # The re-run audits against the adjudicated text.
        assert sc3.inputs["adjudicated_description"] == ADJUDICATED_TASK
        # Now that the ruling landed (human approved), the staged abolition was
        # applied: both oscillating observations stop counting toward triggers.
        assert adj.outputs["ledger_effects_applied"] is True
        assert all(o["abolished"] for o in ledger["observations"])

        # --- Post-ruling round: a new issue re-quoting the dead clause is
        #     dropped by the source-pool switch → clean → COMPLETED. ---
        status = _run_self_check(sc3, flow, [_issue(expected="raise ValueError", quote=DEAD_QUOTE)])
        assert status == StepStatus.COMPLETED
        assert sc3.outputs["issues"] == []
        assert sc3.outputs["validation_stats"]["quote_not_in_source_count"] == 1

        # --- The flip has stopped: the flow advances to COMMIT, not a new
        #     fix loop or another ADJUDICATE. The dynamically-inserted ADJUDICATE
        #     (and its CONFIRM) slot were stripped when the ruling landed, so a
        #     subsequent fix-loop cycle cannot re-enter a spurious un-triggered
        #     adjudication (see test_still_valid_issue_after_ruling_*). ---
        nxt = sm.transition_to_next(flow)
        assert nxt.step_type == StepType.COMMIT
        assert flow.state.selected_steps.count(StepType.ADJUDICATE) == 0
        assert flow.state.selected_steps.count(StepType.CONFIRM) == 0

        # --- And the flow converges to terminal COMPLETED. COMMIT is the last
        #     selected step; running it for real needs git, so we simulate its
        #     completion (status only) and assert the state machine finalizes the
        #     flow rather than re-entering any fix loop / ADJUDICATE between
        #     COMMIT and COMPLETED. ---
        nxt.status = StepStatus.COMPLETED
        flow.state.current_step_id = nxt.step_id
        terminal = sm.transition_to_next(flow)
        assert terminal is None
        assert flow.status == FlowStatus.COMPLETED

    def test_still_valid_issue_after_ruling_stays_in_fix_loop(self, tmp_path):
        """The reflow does not blanket-silence review: an issue grounded on the
        surviving clause is kept and drives a normal fix iteration — and the flow
        then completes a FULL post-ruling fix-loop cycle
        (IMPLEMENT → TEST → next slot) without re-entering a spurious,
        un-triggered ADJUDICATE. This is the spec's own primary follow-up
        scenario: when a landed ruling leaves its inserted ADJUDICATE/CONFIRM
        slots in ``selected_steps``, the transition after TEST builds a fresh
        ADJUDICATE and the flow crashes (TransitionError) or re-opens a spurious
        human gate. Driving the whole cycle here proves the slots were stripped
        on landing and the flip actually stops."""
        cfg = _cfg()
        sm = _make_sm(tmp_path, cfg)
        flow, implement, test = _make_flow(tmp_path)

        sc1 = _new_self_check(sm, flow)
        _run_self_check(sc1, flow, [_issue(expected="raise ValueError")])
        impl = sm.transition_to_next(flow)
        impl.status = StepStatus.COMPLETED
        impl.outputs = {"files_changed": [{"path": "src/foo.py", "action": "modify"}]}

        sc2 = _new_self_check(sm, flow)
        _run_self_check(sc2, flow, [_issue(expected="return None")])
        adj = sm.transition_to_next(flow)
        _run_adjudicate(adj, flow, _RULING)

        human = {"reviewer": "human", "max_iterations": 3, "agents": None}
        with patch(
            "se3.engine.state_machine.resolve_confirm_inputs", return_value=human
        ):
            confirm = sm.transition_to_next(flow)
        confirm.status = StepStatus.COMPLETED
        confirm.outputs = {
            "review_result": {
                "approved": True,
                "step_to_review_id": adj.step_id,
                "step_to_review_type": StepType.ADJUDICATE.value,
            }
        }
        flow.state.current_step_id = confirm.step_id
        sc3 = sm.transition_to_next(flow)

        # A live issue that quotes the SURVIVING clause stays grounded.
        live_quote = "return None when x is None"
        status = _run_self_check(sc3, flow, [_issue(expected="return None", quote=live_quote)])
        assert status == StepStatus.REVISION_NEEDED
        assert len(sc3.outputs["issues"]) == 1
        # It routes to the normal fix loop (not another adjudication: a single
        # surviving grounded issue is no longer an oscillation).
        nxt = sm.transition_to_next(flow)
        assert nxt.step_type == StepType.IMPLEMENT
        # The landed ruling stripped its inserted ADJUDICATE (+ CONFIRM) slots, so
        # the fix-loop sequence is back to the clean implement→test→self_check→commit.
        assert flow.state.selected_steps.count(StepType.ADJUDICATE) == 0
        assert flow.state.selected_steps.count(StepType.CONFIRM) == 0

        # --- Complete IMPLEMENT (no code change needed for the assertion) and run
        #     TEST, then drive the transition that used to crash. ---
        nxt.status = StepStatus.COMPLETED
        nxt.outputs = {"files_changed": [{"path": "src/foo.py", "action": "modify"}]}
        flow.state.current_step_id = nxt.step_id

        test2 = sm.transition_to_next(flow)
        assert test2.step_type == StepType.TEST
        test2.status = StepStatus.COMPLETED
        test2.outputs = {"test_results": {"passed": True, "overall_passed": True}}
        flow.state.current_step_id = test2.step_id

        # The transition after TEST: with the stale slots gone this is the next
        # SELF_CHECK, NOT a fresh un-triggered ADJUDICATE (which crashed the flow).
        sc4 = sm.transition_to_next(flow)
        assert sc4.step_type == StepType.SELF_CHECK
        assert flow.state.selected_steps.count(StepType.ADJUDICATE) == 0

        # --- A clean re-review converges the flow to COMMIT (the flip stopped). ---
        assert _run_self_check(sc4, flow, []) == StepStatus.COMPLETED
        commit = sm.transition_to_next(flow)
        assert commit.step_type == StepType.COMMIT

    def test_rejected_ruling_does_not_land_ledger_effects(self, tmp_path):
        """A human rejection re-runs ADJUDICATE; the rejected ruling's abolition
        must NOT land on the ledger, so the still-unresolved oscillation keeps
        counting. Only an APPROVED ruling applies its side effects (issue 1)."""
        cfg = _cfg()
        sm = _make_sm(tmp_path, cfg)
        flow, implement, test = _make_flow(tmp_path)

        sc1 = _new_self_check(sm, flow)
        _run_self_check(sc1, flow, [_issue(expected="raise ValueError")])
        impl = sm.transition_to_next(flow)
        impl.status = StepStatus.COMPLETED
        impl.outputs = {"files_changed": [{"path": "src/foo.py", "action": "modify"}]}

        sc2 = _new_self_check(sm, flow)
        _run_self_check(sc2, flow, [_issue(expected="return None")])
        adj = sm.transition_to_next(flow)
        assert adj.step_type == StepType.ADJUDICATE
        _run_adjudicate(adj, flow, _RULING)
        ledger = flow.state.context[adjudication.LEDGER_KEY]

        human = {"reviewer": "human", "max_iterations": 3, "agents": None}
        with patch(
            "se3.engine.state_machine.resolve_confirm_inputs", return_value=human
        ):
            confirm = sm.transition_to_next(flow)
        assert confirm.step_type == StepType.CONFIRM

        # --- Human REJECTS the ruling → revision re-runs the SAME ADJUDICATE. ---
        confirm.status = StepStatus.COMPLETED
        confirm.outputs = {
            "review_result": {
                "approved": False,
                "step_to_review_id": adj.step_id,
                "step_to_review_type": StepType.ADJUDICATE.value,
            },
            "revision_feedback": "the rewrite dropped a real requirement",
        }
        flow.state.current_step_id = confirm.step_id
        revised = sm.transition_to_next(flow)
        assert revised.step_type == StepType.ADJUDICATE
        assert revised.step_id == adj.step_id
        # The rejected ruling NEVER landed: no observation was abolished.
        assert not any(o["abolished"] for o in ledger["observations"])

        # --- Re-rule, then APPROVE → now the effects land. ---
        _run_adjudicate(revised, flow, _RULING)
        with patch(
            "se3.engine.state_machine.resolve_confirm_inputs", return_value=human
        ):
            confirm2 = sm.transition_to_next(flow)
        assert confirm2.step_type == StepType.CONFIRM
        confirm2.status = StepStatus.COMPLETED
        confirm2.outputs = {
            "review_result": {
                "approved": True,
                "step_to_review_id": adj.step_id,
                "step_to_review_type": StepType.ADJUDICATE.value,
            }
        }
        flow.state.current_step_id = confirm2.step_id
        sc3 = sm.transition_to_next(flow)
        assert sc3.step_type == StepType.SELF_CHECK
        # The approved ruling applied its staged abolition on landing.
        assert all(o["abolished"] for o in ledger["observations"])


# --------------------------------------------------------------------------- #
# 2. Ledger continuity across --resume
# --------------------------------------------------------------------------- #

class TestResumeContinuity:
    def test_ledger_accumulates_across_resume_boundary(self, tmp_path):
        cfg = _cfg()
        sm = _make_sm(tmp_path, cfg)
        flow, implement, test = _make_flow(tmp_path)

        # Round 1 recorded, then the process "stops".
        sc1 = _new_self_check(sm, flow)
        _run_self_check(sc1, flow, [_issue(expected="raise ValueError")])
        fp_round0 = adjudication.fingerprint(_issue(expected="raise ValueError"))
        pre = flow.state.context[adjudication.LEDGER_KEY]
        assert pre["round_count"] == 1
        assert any(o["fingerprint"] == fp_round0 for o in pre["observations"])

        # --resume: serialize + deserialize the whole state (engine.json shape).
        restored_state = State.from_dict(flow.state.to_dict())
        ledger = restored_state.context[adjudication.LEDGER_KEY]
        assert ledger["round_count"] == 1
        assert any(o["fingerprint"] == fp_round0 for o in ledger["observations"])

        # Continue the flow on the restored state; the fix loop advanced the
        # iteration counter before the stop, so start round 2 as a fix round.
        flow2 = FlowInstance(
            flow_id="adj-integ-flow",
            task_description=CONTRADICTORY_TASK,
            task_type="feature",
            status=FlowStatus.RUNNING,
            change_path=tmp_path / "changes" / "c",
        )
        flow2.state = restored_state
        flow2.state.fix_iterations = 1

        sc2 = _new_self_check(sm, flow2)
        _run_self_check(sc2, flow2, [_issue(expected="return None")])
        # The oscillation spans the resume: round 0 (pre-resume) vs round 1
        # (post-resume) at the same position → adjudicate fires.
        adj = sm.transition_to_next(flow2)
        assert adj.step_type == StepType.ADJUDICATE
        assert flow2.state.context[adjudication.LEDGER_KEY]["round_count"] == 2


# --------------------------------------------------------------------------- #
# 3. Periodic backstop
# --------------------------------------------------------------------------- #

class TestPeriodicBackstop:
    def test_backstop_forces_adjudicate_every_n_iterations(self, tmp_path):
        """With no oscillation signal, the every-N-iteration backstop still
        routes a SELF_CHECK REVISION_NEEDED to ADJUDICATE."""
        cfg = _cfg(adjudicate_period=3)
        sm = _make_sm(tmp_path, cfg)
        flow, implement, test = _make_flow(tmp_path)
        # Drive the fix-iteration counter to the period edge without any
        # oscillation on the ledger (each round cites a distinct clause).
        flow.state.fix_iterations = 3

        sc = _new_self_check(sm, flow)
        # A lone, non-oscillating issue: no structural trigger would fire.
        _run_self_check(sc, flow, [_issue(expected="raise ValueError")])
        nxt = sm.transition_to_next(flow)

        assert nxt.step_type == StepType.ADJUDICATE
        # The backstop rebased to the current iteration for the next sweep.
        assert flow.state.context[adjudication.LEDGER_KEY]["period_baseline"] == 3

    def test_below_period_stays_on_fix_loop(self, tmp_path):
        cfg = _cfg(adjudicate_period=5)
        sm = _make_sm(tmp_path, cfg)
        flow, implement, test = _make_flow(tmp_path)
        flow.state.fix_iterations = 3  # short of the period, no signal

        sc = _new_self_check(sm, flow)
        _run_self_check(sc, flow, [_issue(expected="raise ValueError")])
        nxt = sm.transition_to_next(flow)

        assert nxt.step_type == StepType.IMPLEMENT
        assert StepType.ADJUDICATE not in flow.state.selected_steps


# --------------------------------------------------------------------------- #
# 4. No-op ruling (review_divergence) → transparent pass-through to IMPLEMENT
# --------------------------------------------------------------------------- #

class TestBenignNoop:
    def test_oscillation_then_benign_ruling_passes_through_to_implement(self, tmp_path):
        """End-to-end: an oscillation trips ADJUDICATE, but the LLM rules the
        flagged position a review_divergence (no real contradiction). ADJUDICATE
        must then be a transparent no-op — the triggering round's fix_instructions
        flow UNTOUCHED into IMPLEMENT (not a fresh SELF_CHECK), with no extra fix
        iteration and the issue left intact — exactly as if ADJUDICATE had never
        been inserted. Only two mechanical effects survive: the ADJUDICATE slot is
        stripped and the benign position lands in ``rejected_positions``."""
        cfg = _cfg()
        sm = _make_sm(tmp_path, cfg)
        flow, implement, test = _make_flow(tmp_path)

        # --- Round 1: reviewer demands "raise" at the contradictory clause. ---
        sc1 = _new_self_check(sm, flow)
        _run_self_check(sc1, flow, [_issue(expected="raise ValueError")])
        nxt = sm.transition_to_next(flow)
        assert nxt.step_type == StepType.IMPLEMENT
        nxt.status = StepStatus.COMPLETED
        nxt.outputs = {"files_changed": [{"path": "src/foo.py", "action": "modify"}]}

        # --- Round 2: reviewer flips to "return None" → oscillation → ADJUDICATE. ---
        sc2 = _new_self_check(sm, flow)
        _run_self_check(sc2, flow, [_issue(expected="return None")])
        adj = sm.transition_to_next(flow)
        assert adj.step_type == StepType.ADJUDICATE
        fix_iter_at_ruling = flow.state.get_fix_iteration()
        pending_fix = sc2.outputs["fix_instructions"]
        assert pending_fix  # the round's real pending instructions
        # ``_transition_to_adjudicate`` reset the period baseline at insertion
        # (this sweep actually ran); the no-op pass-through must NOT touch it
        # again — the reset is one of the two mechanical effects it preserves.
        ledger_at_ruling = flow.state.context[adjudication.LEDGER_KEY]
        baseline_at_insertion = ledger_at_ruling["period_baseline"]

        # --- The ruling: benign (review_divergence), no override patch. ---
        assert _run_adjudicate(adj, flow, _BENIGN_RULING) == StepStatus.COMPLETED
        assert adj.outputs["adjudication_noop"] is True
        # No-op leaves supersede/override fields ABSENT (transparent audit shape),
        # and pops any stale value a prior rejected patch ruling re-run in place
        # left behind so it cannot leak a never-approved rewrite.
        assert "superseded_fix_instructions" not in adj.outputs
        assert "fix_instructions_superseded" not in adj.outputs
        assert "adjudicated_description" not in adj.outputs
        assert "adjudicated_plan" not in adj.outputs
        # Audit verdicts are recorded.
        assert adj.outputs["candidate_verdicts"]
        assert adj.outputs["adjudication_rationale"]

        # --- Transparent pass-through: straight to IMPLEMENT, untouched fix. ---
        impl = sm.transition_to_next(flow)
        assert impl.step_type == StepType.IMPLEMENT
        assert impl.inputs["fix_instructions"] == pending_fix
        # Exactly one increment (owned by _transition_to_fix) — no extra count.
        assert flow.state.get_fix_iteration() == fix_iter_at_ruling + 1
        # The oscillation issue was NOT cleared.
        assert sc2.outputs["issues"]
        # The inserted ADJUDICATE slot was stripped; the fix loop can't re-run it.
        assert StepType.ADJUDICATE not in flow.state.selected_steps
        # The benign position landed in the ledger (trigger-layer filter) so the
        # same flip won't re-invoke the adjudicator every round.
        ledger = flow.state.context[adjudication.LEDGER_KEY]
        assert ledger["rejected_positions"]
        # The period baseline stays at its insertion-time reset value — the no-op
        # path performs no second reset (only rejected_positions + the already-done
        # baseline reset are its mechanical bookkeeping).
        assert ledger["period_baseline"] == baseline_at_insertion
