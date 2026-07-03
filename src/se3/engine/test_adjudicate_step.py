"""Unit tests for the ADJUDICATE step handler (product writing + ledger update).

Co-located with the engine per the charter's engine-internal test exception:
these exercise the handler's private helpers and its interaction with the
adjudication ledger. The LLM call is mocked — the handler's own logic (candidate
assembly, verdict → abolish/reject mapping, output writing, fix_instructions
supersede, and the "no self_check issues" invariant) is what is under test.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from se3.engine import adjudication as adj
from se3.engine.models import FlowInstance, Step, StepStatus, StepType
from se3.engine.steps import adjudicate as adjmod


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _issue(file="src/foo.py", line=42, quote="do the thing", expected="return None"):
    return {
        "severity": "high",
        "actual_behavior": "returns 0",
        "expected_behavior": expected,
        "divergence": "when x is None",
        "expectation_source": {"type": "task_description", "verbatim_quote": quote},
        "evidence_lines": [f"{file}:{line}"],
        "missing_in": [],
    }


def _flow_with_ledger(tmp_path: Path, oscillate=True) -> FlowInstance:
    """A flow whose ledger has one oscillating position (two opposing rounds)."""
    flow = FlowInstance(task_description="Return None when x is None, and raise when x is None")
    flow.change_path = tmp_path / "change"
    ctx = flow.state.context
    # Round 0: expected "return None"
    adj.record_self_check_round(ctx, [_issue(expected="return None")], round_id="r0")
    if oscillate:
        # Round 1: same position, opposing expectation ⇒ oscillation candidate.
        adj.record_self_check_round(ctx, [_issue(expected="raise ValueError")], round_id="r1")
    return flow


def _adj_step(flow: FlowInstance, **inputs) -> Step:
    step = Step(step_type=StepType.ADJUDICATE)
    step.inputs.update(inputs)
    flow.state.add_step(step)
    return step


def _mock_call(payload: dict):
    """Return a patch target that makes LLMCaller().call() return ``payload`` JSON."""
    class _Caller:
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            return json.dumps(payload)

    return _Caller


# --------------------------------------------------------------------------- #
# Candidate assembly
# --------------------------------------------------------------------------- #

def test_candidate_positions_picks_oscillating(tmp_path):
    flow = _flow_with_ledger(tmp_path)
    ledger = adjmod._ledger(flow)
    cands = adjmod._candidate_positions(ledger)
    assert len(cands) == 1
    assert cands[0]["file"] == "src/foo.py"
    # Timeline shows both opposing expectations across two rounds.
    expecteds = {t["expected"] for t in cands[0]["timeline"]}
    assert expecteds == {"return None", "raise ValueError"}


def test_candidate_positions_ignores_single_expectation(tmp_path):
    flow = _flow_with_ledger(tmp_path, oscillate=False)
    ledger = adjmod._ledger(flow)
    assert adjmod._candidate_positions(ledger) == []


def test_candidate_positions_honors_explicit_trigger(tmp_path):
    """A single-expectation position still surfaces if the trigger flagged it."""
    flow = _flow_with_ledger(tmp_path, oscillate=False)
    ledger = adjmod._ledger(flow)
    pk = ledger["observations"][0]["position_key"]
    cands = adjmod._candidate_positions(ledger, explicit=[pk])
    assert len(cands) == 1


# --------------------------------------------------------------------------- #
# Handler: product writing + ledger update
# --------------------------------------------------------------------------- #

def test_handler_writes_override_to_own_outputs(tmp_path):
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow, fix_instructions="fix the None handling somehow")
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudicated_plan": None,
        "adjudication_rationale": "The spec demanded both return and raise; keep return.",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction", "reason": "opposing"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.COMPLETED
    assert step.outputs["adjudicated_description"] == "Return None when x is None."
    assert step.outputs["adjudicated_plan"] is None
    assert step.outputs["adjudication_rationale"]
    assert step.outputs["adjudicated_at"]  # ISO timestamp present
    assert step.outputs["contradiction_type"] == "internal_contradiction"
    # Handler must NOT emit self_check-style issues.
    assert "issues" not in step.outputs


def test_handler_supersedes_fix_instructions(tmp_path):
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow, fix_instructions="OLD pending instructions")
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudication_rationale": "keep return",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        adjmod.adjudicate_handler(step, flow)
    assert step.outputs["superseded_fix_instructions"] == "OLD pending instructions"
    assert step.outputs["fix_instructions_superseded"] is True


def test_contradiction_verdict_abolishes_position(tmp_path):
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudication_rationale": "keep return",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        adjmod.adjudicate_handler(step, flow)
    # Both round observations at the oscillating position are now abolished, so
    # the position no longer counts toward any trigger.
    ledger = adjmod._ledger(flow)
    assert all(o["abolished"] for o in ledger["observations"])
    assert step.outputs["abolished_count"] == 2
    # A fresh self_check round citing the same position no longer oscillates:
    # the prior observations are abolished, so it is a lone (unpaired) entry.
    adj.record_self_check_round(flow.state.context, [_issue(expected="raise ValueError")], round_id="r2")
    decision = adj.evaluate_triggers(
        flow.state.context, [_issue(expected="raise ValueError")], fix_iteration=1, period_n=0
    )
    assert not decision.suppress_convergence


def test_benign_verdict_records_rejected_candidate(tmp_path):
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    payload = {
        "contradiction_type": "review_divergence",
        "adjudicated_description": None,
        "adjudicated_plan": None,
        "adjudication_rationale": "not actually contradictory",
        "candidate_verdicts": [{"id": 0, "verdict": "benign", "reason": "different scopes"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        adjmod.adjudicate_handler(step, flow)
    ledger = adjmod._ledger(flow)
    # Position recorded as rejected → excluded from future oscillation triggers.
    assert ledger["observations"][0]["position_key"] in ledger["rejected_positions"]
    assert step.outputs["rejected_candidates"]
    assert step.outputs["rejected_candidates"][0]["reason"] == "different scopes"
    # No override, so nothing abolished.
    assert step.outputs["abolished_count"] == 0
    # And the previously-oscillating position no longer suppresses convergence.
    decision = adj.evaluate_triggers(
        flow.state.context, [_issue(expected="return None")], fix_iteration=1, period_n=0
    )
    assert not decision.suppress_convergence


def test_plan_override_accepted_only_as_nonempty_list(tmp_path):
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    groups = [{"group_id": "G1", "name": "fixed", "tasks": []}]
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": None,
        "adjudicated_plan": groups,
        "adjudication_rationale": "fix in the plan instead",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        adjmod.adjudicate_handler(step, flow)
    assert step.outputs["adjudicated_plan"] == groups
    assert step.outputs["adjudicated_description"] is None


def test_null_string_override_coerced_to_none(tmp_path):
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    payload = {
        "contradiction_type": "review_divergence",
        "adjudicated_description": "null",  # LLM emitted literal string
        "adjudicated_plan": "none",
        "adjudication_rationale": "no change",
        "candidate_verdicts": [{"id": 0, "verdict": "benign"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        adjmod.adjudicate_handler(step, flow)
    assert step.outputs["adjudicated_description"] is None
    assert step.outputs["adjudicated_plan"] is None


def test_override_without_verdicts_still_abolishes_candidates(tmp_path):
    """A patch with no explicit verdicts abolishes the flagged candidates anyway."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudication_rationale": "keep return",
        "candidate_verdicts": [],  # LLM forgot to enumerate verdicts
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        adjmod.adjudicate_handler(step, flow)
    ledger = adjmod._ledger(flow)
    assert all(o["abolished"] for o in ledger["observations"])


def test_handler_does_not_touch_prior_step_outputs(tmp_path):
    """Discovery/plan step outputs stay byte-for-byte untouched by adjudication."""
    flow = _flow_with_ledger(tmp_path)
    plan_step = Step(step_type=StepType.PLAN)
    original_groups = [{"group_id": "G1", "name": "orig", "tasks": [{"id": 1, "description": "d"}]}]
    plan_step.status = StepStatus.COMPLETED
    plan_step.outputs["task_groups"] = original_groups
    flow.state.add_step(plan_step)
    snapshot = json.dumps(plan_step.outputs, sort_keys=True)

    step = _adj_step(flow)
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_plan": [{"group_id": "G1", "name": "changed", "tasks": []}],
        "adjudication_rationale": "override plan",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        adjmod.adjudicate_handler(step, flow)
    # Original PLAN outputs unchanged; the override lives only on the adj step.
    assert json.dumps(plan_step.outputs, sort_keys=True) == snapshot
    assert step.outputs["adjudicated_plan"][0]["name"] == "changed"


def test_effective_task_groups_prefers_prior_adjudication(tmp_path):
    """A later adjudication reads the latest prior adjudicated_plan, not the raw plan."""
    flow = FlowInstance(task_description="t")
    flow.change_path = tmp_path / "c"
    plan_step = Step(step_type=StepType.PLAN)
    plan_step.status = StepStatus.COMPLETED
    plan_step.outputs["task_groups"] = [{"group_id": "G1", "name": "orig", "tasks": []}]
    flow.state.add_step(plan_step)

    prior_adj = Step(step_type=StepType.ADJUDICATE)
    prior_adj.status = StepStatus.COMPLETED
    prior_adj.outputs["adjudicated_plan"] = [{"group_id": "G1", "name": "gen1", "tasks": []}]
    flow.state.add_step(prior_adj)

    current = Step(step_type=StepType.ADJUDICATE)
    flow.state.add_step(current)
    groups = adjmod._effective_task_groups(current, flow)
    assert groups[0]["name"] == "gen1"


def test_handler_threads_revision_feedback_into_prompt(tmp_path):
    """A confirmation-门 rejection re-runs ADJUDICATE with the reviewer's
    feedback; the handler must surface it in the ruling prompt so the re-ruling
    can address the objection (group G6, task 2 revision回流)."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(
        flow,
        fix_instructions="fix it",
        is_revision=True,
        revision_feedback="the rewrite dropped a real requirement",
    )
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None; keep the requirement.",
        "adjudication_rationale": "restore the dropped requirement",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    captured = {}

    class _Caller:
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            captured["prompt"] = k.get("prompt", a[0] if a else "")
            return json.dumps(payload)

    with patch.object(adjmod, "LLMCaller", _Caller):
        status = adjmod.adjudicate_handler(step, flow)

    assert status == StepStatus.COMPLETED
    assert "the rewrite dropped a real requirement" in captured["prompt"]
    assert "rejected your previous ruling" in captured["prompt"].lower()


def test_handler_ignores_revision_feedback_when_not_revision(tmp_path):
    """Without ``is_revision`` the feedback section is absent (the normal path
    that all other handler tests exercise stays unchanged)."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow, fix_instructions="fix it", revision_feedback="stray")
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudication_rationale": "keep return",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    captured = {}

    class _Caller:
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            captured["prompt"] = k.get("prompt", a[0] if a else "")
            return json.dumps(payload)

    with patch.object(adjmod, "LLMCaller", _Caller):
        adjmod.adjudicate_handler(step, flow)

    assert "rejected your previous ruling" not in captured["prompt"].lower()


def test_handler_fails_gracefully_on_unparseable_llm(tmp_path):
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)

    class _BadCaller:
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            return "not json at all"

    with patch.object(adjmod, "LLMCaller", _BadCaller):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.FAILED
    assert step.error_message


# --------------------------------------------------------------------------- #
# Acceptance: every generation of a ruling stays auditable
# --------------------------------------------------------------------------- #

def test_multi_generation_rulings_are_each_auditable(tmp_path):
    """Each ADJUDICATE generation keeps its own rationale + timestamp in its own
    outputs, and the effective-text layer resolves to the newest — so the full
    ruling history remains inspectable (acceptance: 历史可见每代裁决及理由与时间戳)."""
    flow = _flow_with_ledger(tmp_path)

    step1 = _adj_step(flow)
    with patch.object(adjmod, "LLMCaller", _mock_call({
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Gen-1: return None when x is None.",
        "adjudication_rationale": "generation one rationale",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    })):
        # The executor writes the returned status back; the effective-text layer
        # only considers COMPLETED rulings, so mirror that here.
        step1.status = adjmod.adjudicate_handler(step1, flow)

    # A second oscillation later → a second generation ruling.
    adj.record_self_check_round(flow.state.context, [_issue(expected="raise again")], round_id="r3")
    adj.record_self_check_round(flow.state.context, [_issue(expected="return again")], round_id="r4")
    step2 = _adj_step(flow)
    with patch.object(adjmod, "LLMCaller", _mock_call({
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Gen-2: raise ValueError when x is None.",
        "adjudication_rationale": "generation two rationale",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    })):
        step2.status = adjmod.adjudicate_handler(step2, flow)

    # Both generations' audit records survive independently.
    assert step1.outputs["adjudication_rationale"] == "generation one rationale"
    assert step2.outputs["adjudication_rationale"] == "generation two rationale"
    assert step1.outputs["adjudicated_at"] and step2.outputs["adjudicated_at"]
    assert step1.outputs["adjudicated_description"].startswith("Gen-1")
    # The effective description resolves to the LATEST generation.
    assert adjmod._latest_adjudicated(flow, "adjudicated_description").startswith("Gen-2")


# --------------------------------------------------------------------------- #
# Acceptance: source-pool switch drops a dead-clause issue after a ruling
# --------------------------------------------------------------------------- #

def test_ruling_description_drops_dead_clause_issue_via_source_pool(tmp_path):
    """A handler ruling's adjudicated_description becomes the verbatim-quote
    source pool: a subsequent issue re-quoting the abolished clause is dropped
    by validation (acceptance: 源池切换后引用已废条款 issue 被 validation 丢弃)."""
    from se3.engine.steps.self_check import _validate_and_filter_issues

    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    with patch.object(adjmod, "LLMCaller", _mock_call({
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudication_rationale": "keep the return branch",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    })):
        adjmod.adjudicate_handler(step, flow)

    adjudicated = step.outputs["adjudicated_description"]
    # Post-ruling self_check inputs: source pool switched to the adjudicated text
    # (the superseded original still carried the now-dead "raise ..." clause).
    sc_inputs = {
        "task_description_base": adjudicated,
        "original_task_description": "Return None when x is None, and raise when x is None",
        "adjudicated_description": adjudicated,
        "changes_made": {"files_changed": ["src/foo.py"]},
    }
    dead = _issue(quote="raise when x is None", expected="raise ValueError")
    live = _issue(quote="Return None when x is None", expected="return None")

    kept, stats = _validate_and_filter_issues([dead, live], sc_inputs)
    # The dead-clause issue is dropped; the live one survives.
    assert stats["quote_not_in_source_count"] == 1
    assert len(kept) == 1
    assert kept[0]["expectation_source"]["verbatim_quote"] == "Return None when x is None"
