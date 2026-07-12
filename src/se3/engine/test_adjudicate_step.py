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


def test_candidate_positions_skips_quoteless(tmp_path):
    """Two unrelated quoteless (evidence-only) findings in one file collapse to a
    per-file position_key ('file\\x1f'). Their differing expectations must NOT be
    presented as a bogus A/B oscillation candidate — mirror the trigger layer's
    quote-anchored guard (_detect_oscillation/_detect_contradiction)."""
    flow = _flow_with_ledger(tmp_path, oscillate=False)
    ctx = flow.state.context
    # Two regression-type issues: empty verbatim_quote, same file, differing
    # expectations across rounds. Same file → same 'file\x1f' position_key.
    adj.record_self_check_round(
        ctx, [_issue(quote="", expected="behavior A")], round_id="q0"
    )
    adj.record_self_check_round(
        ctx, [_issue(quote="", expected="behavior B")], round_id="q1"
    )
    ledger = adjmod._ledger(flow)
    cands = adjmod._candidate_positions(ledger)
    # Only the (nonexistent here) quote-anchored candidates would surface; the
    # quoteless per-file collapse is dropped.
    assert all(c["quote"] for c in cands)
    assert cands == []


def test_handler_reads_flat_trigger_inputs(tmp_path):
    """The handler consumes the FLAT trigger keys the state machine writes
    (adjudication_reasons / adjudication_triggering_positions): the prompt renders
    the real trigger reasons and a reproduction-only position with a single
    expectation still surfaces as a candidate because it was unioned in
    explicitly (issue: input-key mismatch)."""
    # No oscillation on the ledger — the ONLY reason this position is a candidate
    # is the explicit trigger union.
    flow = _flow_with_ledger(tmp_path, oscillate=False)
    ledger = adjmod._ledger(flow)
    pk = ledger["observations"][0]["position_key"]
    step = _adj_step(
        flow,
        adjudication_reasons=[adj.REASON_REPRODUCTION],
        adjudication_triggering_positions=[pk],
    )
    prompts: list[str] = []

    class _Caller:
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            prompts.append(k.get("prompt", a[0] if a else ""))
            return json.dumps(
                {
                    "contradiction_type": "review_divergence",
                    "adjudication_rationale": "no real contradiction",
                    "candidate_verdicts": [{"id": 0, "verdict": "benign", "reason": "x"}],
                }
            )

    with patch.object(adjmod, "LLMCaller", _Caller):
        adjmod.adjudicate_handler(step, flow)

    # The prompt names the real trigger reason (not the periodic-backstop default)
    # and surfaces the explicitly-triggered position as a candidate.
    assert adj.REASON_REPRODUCTION in prompts[0]
    assert "no structural candidate on record" not in prompts[0]


# --------------------------------------------------------------------------- #
# Issue 5: adjudicate over the pre-interjection base (no double-composition)
# --------------------------------------------------------------------------- #

def test_effective_task_description_ignores_interjection_composed_input(tmp_path):
    """The handler adjudicates over the PRE-interjection base, not the
    interjection-composed ``task_description`` input ``_build_step_inputs``
    injects. Otherwise the LLM rewrite (a full corrected description) would bake
    the ``## Additional Instructions`` section into the base layer, after which
    the composer re-appends the same interjections — duplicating them in every
    post-ruling prompt and in the self_check source pool (issue 5)."""
    flow = FlowInstance(task_description="Base spec text")
    flow.change_path = tmp_path / "c"
    step = _adj_step(
        flow,
        task_description=(
            "Base spec text\n\n## Additional Instructions\nDo the extra thing"
        ),
    )
    eff = adjmod._effective_task_description(step, flow)
    assert eff == "Base spec text"
    assert "Additional Instructions" not in eff


def test_effective_task_description_honors_clean_base_input(tmp_path):
    """A clean ``task_description_base`` input (no interjection decoration) is used
    verbatim, keeping the handler testable in isolation (issue 5)."""
    flow = FlowInstance(task_description="Original")
    flow.change_path = tmp_path / "c"
    step = _adj_step(flow, task_description_base="Refined base spec")
    assert adjmod._effective_task_description(step, flow) == "Refined base spec"


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
    ledger = adjmod._ledger(flow)
    # Ledger side effects are DEFERRED until the ruling lands: the handler only
    # STAGES the abolition; the persisted ledger is untouched at this point.
    assert step.outputs["ledger_effects_applied"] is False
    assert not any(o["abolished"] for o in ledger["observations"])
    assert len(step.outputs["abolished_fingerprints"]) == 2

    # Landing the ruling (approved / 免确认) applies the staged effects.
    count = adjmod.apply_landed_ledger_effects(step, flow.state.context)
    assert count == 2
    assert step.outputs["abolished_count"] == 2
    assert step.outputs["ledger_effects_applied"] is True
    # Both round observations at the oscillating position are now abolished, so
    # the position no longer counts toward any trigger.
    assert all(o["abolished"] for o in ledger["observations"])
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
    # Staged only: the rejection is not recorded on the ledger until the ruling
    # lands (a benign ruling needs no confirmation, so it lands immediately).
    assert step.outputs["rejected_positions"]
    assert step.outputs["rejected_candidates"]
    assert step.outputs["rejected_candidates"][0]["reason"] == "different scopes"
    assert ledger["observations"][0]["position_key"] not in ledger["rejected_positions"]

    adjmod.apply_landed_ledger_effects(step, flow.state.context)
    # Position recorded as rejected → excluded from future oscillation triggers.
    assert ledger["observations"][0]["position_key"] in ledger["rejected_positions"]
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
    # review_divergence ⇒ transparent no-op: the coerced null overrides are not
    # written (the no-op path never emits adjudicated_description/plan at all).
    assert step.outputs["adjudication_noop"] is True
    assert step.outputs.get("adjudicated_description") is None
    assert step.outputs.get("adjudicated_plan") is None


def test_override_without_verdicts_still_abolishes_candidates(tmp_path):
    """A patch with no explicit verdicts abolishes the flagged candidates anyway
    (their quoted clause is gone from the adjudicated text)."""
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
    adjmod.apply_landed_ledger_effects(step, flow.state.context)
    assert all(o["abolished"] for o in ledger["observations"])


def test_handler_does_not_touch_prior_step_outputs(tmp_path):
    """Discovery AND plan step outputs stay byte-for-byte untouched by adjudication."""
    flow = _flow_with_ledger(tmp_path)
    # A completed DISCOVERY step whose refined_description must NOT be rewritten
    # in place — the ruling layers its override onto its own outputs instead.
    discovery_step = Step(step_type=StepType.DISCOVERY)
    discovery_step.status = StepStatus.COMPLETED
    discovery_step.outputs["refined_description"] = "Return None when x is None."
    discovery_step.outputs["discovery_summary"] = "clarified"
    flow.state.add_step(discovery_step)
    discovery_snapshot = json.dumps(discovery_step.outputs, sort_keys=True)

    plan_step = Step(step_type=StepType.PLAN)
    original_groups = [{"group_id": "G1", "name": "orig", "tasks": [{"id": 1, "description": "d"}]}]
    plan_step.status = StepStatus.COMPLETED
    plan_step.outputs["task_groups"] = original_groups
    flow.state.add_step(plan_step)
    plan_snapshot = json.dumps(plan_step.outputs, sort_keys=True)

    step = _adj_step(flow)
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None only.",
        "adjudicated_plan": [{"group_id": "G1", "name": "changed", "tasks": []}],
        "adjudication_rationale": "override plan",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        adjmod.adjudicate_handler(step, flow)
    # Original DISCOVERY + PLAN outputs unchanged; overrides live only on adj step.
    assert json.dumps(discovery_step.outputs, sort_keys=True) == discovery_snapshot
    assert json.dumps(plan_step.outputs, sort_keys=True) == plan_snapshot
    assert step.outputs["adjudicated_plan"][0]["name"] == "changed"
    assert step.outputs["adjudicated_description"] == "Return None when x is None only."


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


# --------------------------------------------------------------------------- #
# Issue 1: deferred ledger side effects — a rejected ruling never lands
# --------------------------------------------------------------------------- #

def test_handler_stages_but_does_not_apply_ledger_effects(tmp_path):
    """The handler only STAGES abolish/reject; the persisted ledger stays clean
    until the ruling lands via ``apply_landed_ledger_effects`` (issue 1)."""
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
    ledger = adjmod._ledger(flow)
    assert step.outputs["ledger_effects_applied"] is False
    assert not any(o["abolished"] for o in ledger["observations"])
    assert ledger["rejected_positions"] == []
    # "abolished_count" is written only at landing time.
    assert "abolished_count" not in step.outputs


def test_rejected_ruling_leaves_ledger_untouched(tmp_path):
    """A ruling the confirmation门 rejects (its revision re-runs ADJUDICATE) must
    not leave abolish/reject side effects on the ledger (issue 1). The re-run
    simply overwrites the staged outputs; nothing was ever applied."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    rejected = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudication_rationale": "first ruling (will be rejected)",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(rejected)):
        adjmod.adjudicate_handler(step, flow)
    ledger = adjmod._ledger(flow)
    assert not any(o["abolished"] for o in ledger["observations"])

    # Simulate the revision re-run of the SAME step with a corrected ruling.
    revised = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None; keep requirement.",
        "adjudication_rationale": "second ruling after human feedback",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    step.inputs["is_revision"] = True
    step.inputs["revision_feedback"] = "restore the requirement"
    with patch.object(adjmod, "LLMCaller", _mock_call(revised)):
        adjmod.adjudicate_handler(step, flow)
    # Still nothing applied — the rejected ruling's effects never touched the
    # ledger, and the revised ruling is likewise only staged.
    assert not any(o["abolished"] for o in ledger["observations"])
    assert step.outputs["adjudication_rationale"].startswith("second ruling")
    assert step.outputs["ledger_effects_applied"] is False


# --------------------------------------------------------------------------- #
# Issue 2: abolish only positions whose clause actually left the source pool
# --------------------------------------------------------------------------- #

def test_plan_only_override_keeps_live_description_clause_counting(tmp_path):
    """A plan-only fix must NOT abolish a description-grounded position whose
    quoted clause still lives in the (unchanged) task description (issue 2)."""
    flow = FlowInstance(task_description="Keep the hard constraint X and also do Y")
    flow.change_path = tmp_path / "c"
    ctx = flow.state.context
    q = "Keep the hard constraint X"
    adj.record_self_check_round(ctx, [_issue(quote=q, expected="enforce X")], round_id="r0")
    adj.record_self_check_round(ctx, [_issue(quote=q, expected="drop X")], round_id="r1")

    step = _adj_step(flow)
    groups = [{"group_id": "G1", "name": "fixed", "tasks": []}]
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": None,   # description untouched
        "adjudicated_plan": groups,        # only the plan changed
        "adjudication_rationale": "resolve in the plan; keep constraint X",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        adjmod.adjudicate_handler(step, flow)
    # The quoted clause survives in the effective (unchanged) description, so the
    # position is not staged for abolition — it keeps counting toward triggers.
    assert step.outputs["abolished_fingerprints"] == []
    adjmod.apply_landed_ledger_effects(step, flow.state.context)
    ledger = adjmod._ledger(flow)
    assert not any(o["abolished"] for o in ledger["observations"])
    # A next round at the same live position still oscillates (a THIRD distinct
    # expectation) → suppress convergence, because the history was not abolished.
    decision = adj.evaluate_triggers(
        ctx, [_issue(quote=q, expected="mangle X")], fix_iteration=1, period_n=0
    )
    assert decision.suppress_convergence


def test_description_rewrite_abolishes_only_the_removed_clause(tmp_path):
    """A description rewrite that keeps one quoted hard constraint abolishes ONLY
    the position whose clause was removed; the preserved clause keeps its history
    (issue 2)."""
    flow = FlowInstance(task_description="Do A. Never delete user data. Do B differently.")
    flow.change_path = tmp_path / "c"
    ctx = flow.state.context
    survive = "Never delete user data"
    dead = "Do B differently"
    adj.record_self_check_round(ctx, [
        _issue(file="src/a.py", quote=survive, expected="guard deletes"),
        _issue(file="src/b.py", quote=dead, expected="do B one way"),
    ], round_id="r0")
    adj.record_self_check_round(ctx, [
        _issue(file="src/a.py", quote=survive, expected="allow deletes"),
        _issue(file="src/b.py", quote=dead, expected="do B other way"),
    ], round_id="r1")

    step = _adj_step(flow)
    # Rewrite keeps the data-safety constraint, drops the contradictory B clause.
    payload = {
        "contradiction_type": "hard_constraint_conflict",
        "adjudicated_description": "Do A. Never delete user data.",
        "adjudicated_plan": None,
        "adjudication_rationale": "keep data-safety; drop the contradictory B demand",
        "candidate_verdicts": [
            {"id": 0, "verdict": "contradiction"},
            {"id": 1, "verdict": "contradiction"},
        ],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        adjmod.adjudicate_handler(step, flow)
    adjmod.apply_landed_ledger_effects(step, flow.state.context)

    ledger = adjmod._ledger(flow)
    surviving = [o for o in ledger["observations"] if o["file"] == "src/a.py"]
    removed = [o for o in ledger["observations"] if o["file"] == "src/b.py"]
    # The preserved-clause position keeps its history; only the removed one dies.
    assert not any(o["abolished"] for o in surviving)
    assert all(o["abolished"] for o in removed)


def test_description_only_ruling_keeps_clause_the_live_plan_still_restates(tmp_path):
    """A description-only ruling must NOT abolish a clause the (un-overridden) plan
    still restates. When no ``adjudicated_plan`` is supplied the latest plan stays
    authoritative — self_check keeps validating ``plan_task`` quotes against it — so
    a clause still present there remains quotable. Abolishing it on the strength of
    a description rewrite alone would let the next SELF_CHECK drop a valid
    plan-grounded issue as quote_not_in_source and silently erase an unchanged plan
    requirement; only a ruling that also overrides the plan may retire that clause.
    """
    from se3.engine.steps.self_check import _validate_and_filter_issues

    flow = FlowInstance(
        task_description="Return None when x is None, and raise when x is None"
    )
    flow.change_path = tmp_path / "c"
    ctx = flow.state.context
    dead_clause = "raise when x is None"
    # The oscillating position cites the clause the description rewrite drops.
    adj.record_self_check_round(
        ctx, [_issue(quote=dead_clause, expected="raise ValueError")], round_id="r0"
    )
    adj.record_self_check_round(
        ctx, [_issue(quote=dead_clause, expected="return None")], round_id="r1"
    )

    # The effective plan still restates the clause verbatim — and, with no
    # ``adjudicated_plan``, it remains authoritative.
    live_plan = [
        {
            "group_id": "G1",
            "name": "impl",
            "tasks": [
                {
                    "description": "Handle empty input: raise when x is None",
                    "acceptance_criteria": [],
                }
            ],
        }
    ]
    step = _adj_step(flow, task_groups=live_plan)
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudicated_plan": None,  # plan left untouched (still restates the clause)
        "adjudication_rationale": "keep the return branch; drop the raise demand",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        adjmod.adjudicate_handler(step, flow)

    # The clause survives in the effective (unchanged) plan, so it is NOT staged for
    # abolition despite leaving the adjudicated description.
    assert step.outputs["abolished_fingerprints"] == []
    adjmod.apply_landed_ledger_effects(step, flow.state.context)
    ledger = adjmod._ledger(flow)
    assert not any(o["abolished"] for o in ledger["observations"])

    # Downstream: nothing was abolished, so the self_check source pool still carries
    # the live plan task, and a valid plan-grounded issue re-quoting the clause is
    # KEPT (not silently dropped by validation).
    abolished_quotes = [
        o["quote_norm"] for o in ledger["observations"] if o["abolished"]
    ]
    assert abolished_quotes == []
    sc_inputs = {
        "task_description_base": "Return None when x is None.",
        "adjudicated_description": "Return None when x is None.",
        "abolished_clause_quotes": abolished_quotes,
        "task_groups": live_plan,
        "changes_made": {"files_changed": ["src/foo.py"]},
    }
    live_issue = _issue(quote="raise when x is None", expected="raise ValueError")
    live_issue["expectation_source"]["type"] = "plan_task"
    kept, stats = _validate_and_filter_issues([live_issue], sc_inputs)
    assert stats["quote_not_in_source_count"] == 0
    assert len(kept) == 1


# --------------------------------------------------------------------------- #
# Issue 3: a no-op contradiction ruling is rejected (never reflows unchanged)
# --------------------------------------------------------------------------- #

def test_noop_contradiction_ruling_fails_after_retries(tmp_path):
    """A real-contradiction verdict with NO covering patch is rejected instead of
    reflowing to SELF_CHECK against an unchanged spec (issue 3)."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": None,
        "adjudicated_plan": None,
        "adjudication_rationale": "these two clauses conflict",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.FAILED
    assert "override patch" in step.error_message
    # Nothing was written or staged.
    ledger = adjmod._ledger(flow)
    assert not any(o["abolished"] for o in ledger["observations"])
    assert "adjudicated_at" not in step.outputs


def test_noop_contradiction_ruling_retries_then_succeeds(tmp_path):
    """The first no-op ruling is re-asked with a strict patch demand; a corrected
    second ruling lands normally (issue 3, the retry branch)."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    noop = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": None,
        "adjudicated_plan": None,
        "adjudication_rationale": "conflict",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    good = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudication_rationale": "keep return",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    responses = [json.dumps(noop), json.dumps(good)]
    prompts = []

    class _Caller:
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            prompts.append(k.get("prompt", a[0] if a else ""))
            return responses.pop(0)

    with patch.object(adjmod, "LLMCaller", _Caller):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.COMPLETED
    assert step.outputs["adjudicated_description"] == "Return None when x is None."
    # The second attempt's prompt carried the strict re-prompt demanding a
    # covering patch (and rationale).
    assert "was rejected" in prompts[1].lower()
    assert "covering override patch" in prompts[1].lower()


def test_review_divergence_needs_no_patch(tmp_path):
    """A ``review_divergence`` verdict legitimately lands with no override patch
    (issue 3: only real contradictions require a covering patch)."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    payload = {
        "contradiction_type": "review_divergence",
        "adjudicated_description": None,
        "adjudicated_plan": None,
        "adjudication_rationale": "no real contradiction; the flip was a misfire",
        "candidate_verdicts": [{"id": 0, "verdict": "benign", "reason": "scopes differ"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.COMPLETED
    # No real contradiction ⇒ transparent no-op: no override patch is written.
    assert step.outputs["adjudication_noop"] is True
    assert step.outputs.get("adjudicated_description") is None
    assert step.outputs.get("adjudicated_plan") is None


def test_review_divergence_blank_rationale_is_rejected(tmp_path):
    """Every ruling — including ``review_divergence`` — must carry a non-blank
    ``adjudication_rationale`` before it can land: it is the audit justification
    AND the dismissal reason stamped onto benign candidates. A whitespace-only
    rationale is non-actionable, so the handler retries and (still blank) FAILS
    rather than writing a blank audit and whitespace-reason rejected candidates
    (issue 2)."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    payload = {
        "contradiction_type": "review_divergence",
        "adjudicated_description": None,
        "adjudicated_plan": None,
        "adjudication_rationale": "   ",  # whitespace only → not actionable
        "candidate_verdicts": [],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.FAILED
    # No ruling landed: no blank audit, no whitespace-reason rejected candidates.
    assert "rejected_candidates" not in step.outputs


def test_review_divergence_without_verdicts_rejects_all_candidates(tmp_path):
    """A no-patch ``review_divergence`` ruling that omits candidate_verdicts must
    still close out its candidates: every flagged position is staged rejected so
    the same oscillation cannot re-trigger ADJUDICATE next round.

    Regression: previously such a ruling landed with an empty rejected list, so
    the candidate stayed live and ADJUDICATE re-fired on it every round —
    recreating the loop the rejected-candidate ledger exists to prevent.
    """
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    pos_key = adjmod._ledger(flow)["observations"][0]["position_key"]
    payload = {
        "contradiction_type": "review_divergence",
        "adjudicated_description": None,
        "adjudicated_plan": None,
        "adjudication_rationale": "review misfire",
        "candidate_verdicts": [],  # LLM forgot to enumerate benign verdicts
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.COMPLETED
    # The candidate is staged rejected with the rationale as its reason.
    assert pos_key in step.outputs["rejected_positions"]
    assert step.outputs["rejected_candidates"][0]["position_key"] == pos_key
    assert step.outputs["rejected_candidates"][0]["reason"] == "review misfire"
    # Nothing to abolish (no override patch).
    assert step.outputs["abolished_fingerprints"] == []

    # After the ruling lands, the position is on the ledger's rejected list and no
    # longer suppresses convergence / re-triggers ADJUDICATE.
    ledger = adjmod._ledger(flow)
    adjmod.apply_landed_ledger_effects(step, flow.state.context)
    assert pos_key in ledger["rejected_positions"]
    decision = adj.evaluate_triggers(
        flow.state.context, [_issue(expected="raise ValueError")], fix_iteration=1, period_n=0
    )
    assert not decision.suppress_convergence


def test_review_divergence_stray_contradiction_verdict_is_rejected(tmp_path):
    """A no-patch ``review_divergence`` ruling with a stray per-candidate
    "contradiction" verdict still rejects the position — with no patch there is
    nothing to abolish, so leaving it as a contradiction would re-trigger."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    pos_key = adjmod._ledger(flow)["observations"][0]["position_key"]
    payload = {
        "contradiction_type": "review_divergence",
        "adjudicated_description": None,
        "adjudicated_plan": None,
        "adjudication_rationale": "no real contradiction",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.COMPLETED
    assert pos_key in step.outputs["rejected_positions"]
    assert step.outputs["abolished_fingerprints"] == []


def test_review_divergence_with_patch_discards_override(tmp_path):
    """A ``review_divergence`` ruling that ALSO carries an override patch must not
    rewrite the spec or abolish anything: the non-contradiction classification
    strips its own authority to patch, so the override is discarded and every
    candidate is staged rejected.

    Regression: previously ``has_patch`` was computed before the override was
    dropped, so a benign oscillation returning
    {contradiction_type:"review_divergence", adjudicated_description:"rewritten"}
    landed the task-description override and staged abolition against the patched
    source pool — even though the adjudicator ruled it not a real contradiction.
    """
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    pos_key = adjmod._ledger(flow)["observations"][0]["position_key"]
    payload = {
        "contradiction_type": "review_divergence",
        "adjudicated_description": "a rewritten task description the LLM should not land",
        "adjudicated_plan": [{"group_id": "G1", "name": "x", "tasks": []}],
        "adjudication_rationale": "the flip was a scope misfire, not a contradiction",
        "candidate_verdicts": [],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.COMPLETED
    # The override is discarded — no spec rewrite lands (no-op path omits both
    # override keys entirely).
    assert step.outputs["adjudication_noop"] is True
    assert step.outputs.get("adjudicated_description") is None
    assert step.outputs.get("adjudicated_plan") is None
    # Nothing abolished; the candidate is instead staged rejected so it stops
    # re-triggering without clearing its audit history.
    assert step.outputs["abolished_fingerprints"] == []
    assert pos_key in step.outputs["rejected_positions"]


def test_review_divergence_is_transparent_noop(tmp_path):
    """A no-real-contradiction ruling is a transparent no-op: it flags
    ``adjudication_noop``, records an audit-only verdict (candidate_verdicts +
    rationale) and the benign ``rejected_positions`` mechanical bookkeeping, but
    deliberately writes NONE of the supersede/override fields — so the state
    machine can route straight to IMPLEMENT with the triggering round's
    fix_instructions intact, as if ADJUDICATE had never been inserted (G1)."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow, fix_instructions="ORIGINAL fix instructions")
    verdicts = [{"id": 0, "verdict": "benign", "reason": "different scopes"}]
    payload = {
        "contradiction_type": "review_divergence",
        "adjudicated_description": None,
        "adjudicated_plan": None,
        "adjudication_rationale": "the flip was a review misfire, not a contradiction",
        "candidate_verdicts": verdicts,
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.COMPLETED

    # No-op contract flag + audit trail recorded.
    assert step.outputs["adjudication_noop"] is True
    assert step.outputs["candidate_verdicts"] == verdicts
    assert step.outputs["adjudication_rationale"].strip()
    assert step.outputs["contradiction_type"] == "review_divergence"
    assert step.outputs["adjudicated_at"]

    # Benign rejected_positions mechanical bookkeeping is preserved (the only
    # ledger-facing effect of the no-op path).
    pos_key = adjmod._ledger(flow)["observations"][0]["position_key"]
    assert pos_key in step.outputs["rejected_positions"]
    assert step.outputs["rejected_candidates"][0]["position_key"] == pos_key

    # Supersede/override fields are ABSENT from the no-op output (the transparent
    # audit shape), not present as falsy sentinels: a benign audit record must be
    # indistinguishable from "no supersede/override ever happened". A rejected patch
    # ruling can re-run this SAME step in place (outputs kept), so the no-op branch
    # must POP any lingering override rather than leave a never-approved rewrite
    # behind — else it would leak past the confirmation gate as the effective task
    # description. Absent, IMPLEMENT sees the original fix_instructions untouched and
    # no reflow/abolish is staged.
    assert "superseded_fix_instructions" not in step.outputs
    assert "fix_instructions_superseded" not in step.outputs
    assert "adjudicated_description" not in step.outputs
    assert "adjudicated_plan" not in step.outputs
    assert step.outputs["abolished_fingerprints"] == []
    assert step.outputs["ledger_effects_applied"] is False


def test_real_contradiction_is_not_a_noop(tmp_path):
    """A real contradiction keeps the current patch-path behavior: it supersedes
    the pending fix_instructions and writes the override, and ``adjudication_noop``
    is never set (routing treats its absence as False → reflow, not pass-through)."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow, fix_instructions="OLD pending instructions")
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudicated_plan": None,
        "adjudication_rationale": "the spec demanded both; keep return",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.COMPLETED
    assert step.outputs.get("adjudication_noop") is not True
    assert step.outputs["adjudicated_description"] == "Return None when x is None."
    assert step.outputs["superseded_fix_instructions"] == "OLD pending instructions"
    assert step.outputs["fix_instructions_superseded"] is True


def test_noop_reruling_clears_rejected_patch_ruling(tmp_path):
    """Regression: a rejected patch ruling re-runs the SAME step in place
    (_transition_to_revision keeps step.outputs), then rules review_divergence.
    The no-op branch MUST remove the earlier ruling's adjudicated_description /
    adjudicated_plan / superseded_fix_instructions / fix_instructions_superseded —
    otherwise the never-approved rewrite lingers and _latest_adjudicated_output
    would silently make it the effective task description, bypassing the
    reviewer's rejection at the (opted-in) confirmation gate."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow, fix_instructions="ORIGINAL fix instructions")

    # --- First ruling: real contradiction with a description patch. ---
    patch_payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "REJECTED rewrite the human refused.",
        "adjudicated_plan": None,
        "adjudication_rationale": "keep return",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(patch_payload)):
        adjmod.adjudicate_handler(step, flow)
    assert step.outputs["adjudicated_description"] == "REJECTED rewrite the human refused."
    assert step.outputs["fix_instructions_superseded"] is True

    # --- Confirmation门 rejects → step re-runs in place; outputs are kept. ---
    # --- Revision re-run: LLM now rules review_divergence (no-op). ---
    noop_payload = {
        "contradiction_type": "review_divergence",
        "adjudicated_description": None,
        "adjudicated_plan": None,
        "adjudication_rationale": "on reflection this was a review misfire",
        "candidate_verdicts": [{"id": 0, "verdict": "benign", "reason": "different scopes"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(noop_payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.COMPLETED
    assert step.outputs["adjudication_noop"] is True

    # The rejected patch ruling must be fully wiped — no stale override survives,
    # and the no-op leaves the patch/supersede keys absent (not falsy sentinels).
    assert "adjudicated_description" not in step.outputs
    assert "adjudicated_plan" not in step.outputs
    assert "superseded_fix_instructions" not in step.outputs
    assert "fix_instructions_superseded" not in step.outputs


def test_contradiction_reruling_clears_stale_noop_flag(tmp_path):
    """Regression: a no-op (review_divergence) ruling re-runs the SAME step in
    place, then rules a real contradiction. The patch branch MUST force
    ``adjudication_noop`` back to False — otherwise transition_to_next reads the
    stale True and routes the real-contradiction ruling through the no-op
    pass-through, skipping the confirmation gate / supersede / abolition /
    SELF_CHECK reflow the ruling requires."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow, fix_instructions="ORIGINAL fix instructions")

    # --- First ruling: review_divergence (no-op) sets adjudication_noop=True. ---
    noop_payload = {
        "contradiction_type": "review_divergence",
        "adjudicated_description": None,
        "adjudicated_plan": None,
        "adjudication_rationale": "review misfire",
        "candidate_verdicts": [{"id": 0, "verdict": "benign", "reason": "different scopes"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(noop_payload)):
        adjmod.adjudicate_handler(step, flow)
    assert step.outputs["adjudication_noop"] is True

    # --- Re-run in place: LLM now rules a real internal contradiction. ---
    patch_payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudicated_plan": None,
        "adjudication_rationale": "the spec demanded both; keep return",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(patch_payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.COMPLETED

    # The stale no-op flag must be cleared so the router takes the patch path.
    assert step.outputs.get("adjudication_noop") is not True
    assert step.outputs["adjudicated_description"] == "Return None when x is None."
    assert step.outputs["superseded_fix_instructions"] == "ORIGINAL fix instructions"
    assert step.outputs["fix_instructions_superseded"] is True


# --------------------------------------------------------------------------- #
# covered_surfaces — homomorphic-surface sweep (one boundary clause, many
# surfaces). The field is OPTIONAL (an empty sweep is a legitimate result), but
# each listed entry must be complete or the ruling is not landable.
# --------------------------------------------------------------------------- #

def _sequenced_caller(payloads, prompts):
    """LLMCaller stub returning ``payloads`` in order, recording each prompt."""
    class _Caller:
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            prompts.append(k.get("prompt", a[0] if a else ""))
            return json.dumps(payloads[min(len(prompts) - 1, len(payloads) - 1)])

    return _Caller


def test_prompt_demands_homomorphic_sweep_and_conservatism(tmp_path):
    """The ruling prompt carries the sweep instruction, the when-in-doubt-leave-it-out
    rule, the rule-in-full demand, and the covered_surfaces schema."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    prompts: list[str] = []
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudication_rationale": "keep return",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _sequenced_caller([payload], prompts)):
        adjmod.adjudicate_handler(step, flow)
    p = prompts[0]
    assert "HOMOMORPHIC SURFACES" in p
    assert "by construction" in p
    assert "hints" in p.lower()  # ledger observations are hints, not the criterion
    assert "WHEN IN DOUBT, LEAVE IT OUT" in p
    assert "AUTO-PASS" in p
    assert "covered_surfaces" in p
    assert "justification" in p
    assert "RULE IN FULL, IN ONE GO" in p


def test_covered_surfaces_persisted_on_patch_path(tmp_path):
    """A real contradiction with a well-formed sweep lands the sanitized list in
    the step's own outputs (unconditionally — the confirm gate is usually off)."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None (all surfaces).",
        "adjudication_rationale": "one boundary clause covers both surfaces",
        "covered_surfaces": [
            {"surface": "  step cold file  ", "justification": "  triggering surface: R27+R29  "},
            {"surface": "_context.json", "justification": "B2 and B3 both govern it by construction"},
        ],
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.COMPLETED
    assert step.outputs["covered_surfaces"] == [
        {"surface": "step cold file", "justification": "triggering surface: R27+R29"},
        {"surface": "_context.json", "justification": "B2 and B3 both govern it by construction"},
    ]


def test_covered_surfaces_absent_or_empty_still_actionable(tmp_path):
    """An empty sweep is a legitimate outcome: a real-contradiction ruling with no
    covered_surfaces (or an empty list) lands unchanged — forcing the field would
    push the LLM to invent surfaces, the exact over-claim we guard against."""
    for value in ("__omit__", None, []):
        flow = _flow_with_ledger(tmp_path)
        step = _adj_step(flow)
        payload = {
            "contradiction_type": "internal_contradiction",
            "adjudicated_description": "Return None when x is None.",
            "adjudication_rationale": "keep return",
            "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
        }
        if value != "__omit__":
            payload["covered_surfaces"] = value
        with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
            status = adjmod.adjudicate_handler(step, flow)
        assert status == StepStatus.COMPLETED
        assert step.outputs["covered_surfaces"] == []


def test_normalized_covered_surfaces_shapes():
    ok = {"covered_surfaces": [{"surface": " s ", "justification": " j "}]}
    assert adjmod._normalized_covered_surfaces(ok) == ([{"surface": "s", "justification": "j"}], True)
    assert adjmod._normalized_covered_surfaces({}) == ([], True)
    assert adjmod._normalized_covered_surfaces({"covered_surfaces": None}) == ([], True)
    assert adjmod._normalized_covered_surfaces({"covered_surfaces": []}) == ([], True)
    # Malformed shapes.
    for bad in (
        "a string",
        ["not a dict"],
        [{"justification": "j"}],
        [{"surface": "s"}],
        [{"surface": "  ", "justification": "j"}],
        [{"surface": "s", "justification": ""}],
        [{"surface": 3, "justification": "j"}],
    ):
        assert adjmod._normalized_covered_surfaces({"covered_surfaces": bad}) == ([], False)


def test_incomplete_covered_surfaces_entry_triggers_semantic_retry(tmp_path):
    """An entry missing its justification makes the ruling non-actionable: the
    handler re-asks with the reason spelled out, and a corrected second ruling
    lands."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    bad = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudication_rationale": "keep return",
        "covered_surfaces": [{"surface": "_context.json"}],  # no justification
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    good = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudication_rationale": "keep return",
        "covered_surfaces": [{"surface": "_context.json", "justification": "both clauses cover it"}],
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    responses = [json.dumps(bad), json.dumps(good)]
    prompts: list[str] = []

    class _Caller:
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            prompts.append(k.get("prompt", a[0] if a else ""))
            return responses.pop(0)

    with patch.object(adjmod, "LLMCaller", _Caller):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.COMPLETED
    assert len(prompts) == 2
    assert "malformed `covered_surfaces`" in prompts[1]
    assert step.outputs["covered_surfaces"] == [
        {"surface": "_context.json", "justification": "both clauses cover it"}
    ]


def test_covered_surfaces_never_valid_fails_with_reason(tmp_path):
    """Two malformed rulings in a row → FAILED, and the error message names the
    gate the ruling never cleared."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "Return None when x is None.",
        "adjudication_rationale": "keep return",
        "covered_surfaces": "not a list",
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.FAILED
    assert "covered_surfaces" in step.error_message
    # Nothing landed.
    assert "adjudicated_at" not in step.outputs


def test_review_divergence_ignores_malformed_covered_surfaces(tmp_path):
    """A no-op ruling writes no boundary clause, so covered_surfaces is meaningless
    there: a malformed one must not block an otherwise-valid review_divergence,
    and the key must be ABSENT from the no-op outputs."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow)
    payload = {
        "contradiction_type": "review_divergence",
        "adjudicated_description": None,
        "adjudicated_plan": None,
        "adjudication_rationale": "review misfire",
        "covered_surfaces": [{"surface": ""}],  # garbage — ignored for a no-op
        "candidate_verdicts": [{"id": 0, "verdict": "benign", "reason": "different scopes"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.COMPLETED
    assert step.outputs["adjudication_noop"] is True
    assert "covered_surfaces" not in step.outputs


def test_noop_rerun_pops_stale_covered_surfaces(tmp_path):
    """covered_surfaces is a patch-class key: after a patch ruling is rejected and
    the step re-runs in place as a review_divergence, no stale governance claim may
    survive in the outputs."""
    flow = _flow_with_ledger(tmp_path)
    step = _adj_step(flow, fix_instructions="ORIGINAL")
    patch_payload = {
        "contradiction_type": "internal_contradiction",
        "adjudicated_description": "REJECTED rewrite.",
        "adjudication_rationale": "conflict",
        "covered_surfaces": [{"surface": "_context.json", "justification": "both clauses cover it"}],
        "candidate_verdicts": [{"id": 0, "verdict": "contradiction"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(patch_payload)):
        adjmod.adjudicate_handler(step, flow)
    assert step.outputs["covered_surfaces"]

    noop_payload = {
        "contradiction_type": "review_divergence",
        "adjudicated_description": None,
        "adjudicated_plan": None,
        "adjudication_rationale": "on reflection, a review misfire",
        "candidate_verdicts": [{"id": 0, "verdict": "benign", "reason": "different scopes"}],
    }
    with patch.object(adjmod, "LLMCaller", _mock_call(noop_payload)):
        status = adjmod.adjudicate_handler(step, flow)
    assert status == StepStatus.COMPLETED
    assert "covered_surfaces" not in step.outputs
