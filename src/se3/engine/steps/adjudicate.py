"""Adjudicate step handler — the fix-loop's spec-contradiction 'police'.

The review layer (``self_check`` / ``invariant_check``) reports *deviations*
with high recall; it deliberately holds no authority to rule on a contradiction.
When a task description contradicts itself (or conflicts with a hard
constraint), the fix loop can oscillate: the same location is flagged in
opposite directions across rounds, each fix undoing the last. ADJUDICATE is the
single layer that *rules* on such contradictions.

Its input is the cross-round issue-fingerprint ledger (persisted in
``flow.state.context``) plus the currently-effective ``task_description`` and
``plan`` — never the full transcript. Its product is an **override patch**:
``adjudicated_description`` (overrides ``task_description``) and/or
``adjudicated_plan`` (overrides the latest plan's ``task_groups``), kept minimal,
with rationale and timestamp, stored in this step's own ``outputs`` so the
original discovery/plan outputs stay byte-for-byte untouched.

Handler responsibilities (group G4):
  * Assemble the ruling prompt from the structured cross-round ledger + the
    currently-effective task_description and latest plan task_groups (no full
    transcript), plus the charter so spec-vs-hard-constraint conflicts are
    visible.
  * Call the LLM to classify the contradiction (internal spec contradiction /
    spec-vs-hard-constraint / review divergence), decide whether to patch the
    description or the plan, emit a minimal override patch, and rule each
    candidate oscillation true (real contradiction) or benign.
  * Parse the ruling into this step's own ``outputs`` and route by a **gated
    two-phase** rule keyed on whether a real spec contradiction exists:
      - Real contradiction (internal spec contradiction / spec-vs-hard-constraint)
        → the patch path (unchanged): write adjudicated_description /
        adjudicated_plan / adjudication_rationale / adjudicated_at /
        superseded_fix_instructions / rejected_candidates, supersede the pending
        ``fix_instructions`` for audit, mark ledger entries grounded on
        now-defunct clauses ``abolished``, and record benign candidate positions
        so they never re-trigger. The state machine then reflows to a fresh
        SELF_CHECK pass #1 (counting one fix iteration).
      - No real contradiction (all candidates benign / ``review_divergence``,
        including a period-baseline sweep that found nothing) → ADJUDICATE is a
        **transparent no-op**: mark ``adjudication_noop`` and record the
        audit-only verdict (candidate_verdicts / rationale), but do NOT
        supersede fix_instructions, clear issues, abolish, or reflow. The state
        machine routes straight to IMPLEMENT with the triggering round's
        untouched fix_instructions — as if ADJUDICATE had never been inserted.
        Only two mechanical bookkeeping effects remain: benign positions written
        to ``rejected_positions`` (trigger-layer filter) and the already-done
        ``period_baseline`` reset.

The handler does NOT emit self_check-style ``issues`` (권责 stays split: review
reports deviations, adjudicate rules on the spec) and does NOT touch the
original discovery/plan step outputs. Routing of the ruling — the patch path's
reflow to SELF_CHECK pass #1 vs. the no-op path's transparent pass-through to
IMPLEMENT — and the human/LLM confirmation gate live in the state machine; a
description-changing (patch-path) ruling is gated by an inserted CONFIRM step
(``confirmation.steps.adjudicate``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import adjudication
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus, StepType
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Ledger reading + candidate assembly
# --------------------------------------------------------------------------- #
#
# The handler reads the ledger (populated cross-round by ``adjudication``) to
# build the prompt and, after the ruling, to compute which entries to abolish.
# It reuses the observation records verbatim (each already carries its
# ``fingerprint`` / ``position_key`` / ``expected_norm``) rather than
# re-fingerprinting, so the handler and the pure-logic module cannot drift.


def _ledger(flow: FlowInstance) -> Dict[str, Any]:
    """Return the ledger sub-tree of ``flow.state.context`` (read-only view).

    Tolerates a flow that never accumulated a ledger (empty dict) so the
    periodic backstop can still run a benign ruling on an empty history.
    """
    ctx = flow.state.context if flow.state else {}
    ledger = ctx.get(adjudication.LEDGER_KEY)
    return ledger if isinstance(ledger, dict) else {}


def _active_nonrejected_observations(ledger: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Observations that still count: not abolished and not at a rejected position.

    Rejected positions (candidates the adjudicator previously ruled benign) are
    excluded so a benign location is never re-presented as a candidate.
    """
    rejected = set(ledger.get("rejected_positions", []))
    return [
        o
        for o in ledger.get("observations", [])
        if not o.get("abolished") and o.get("position_key") not in rejected
    ]


def _candidate_positions(
    ledger: Dict[str, Any], explicit: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """Build the candidate-contradiction list for the prompt.

    A candidate is a location (position_key = file + normalized quote) that the
    active ledger has flagged with two or more *materially different* expected
    behaviors — the structural oscillation signal. Explicitly-supplied trigger
    positions (from the state machine's ``evaluate_triggers`` decision) are
    unioned in so a signal trigger the periodic scan would miss is still ruled
    on. Each candidate carries its per-round expected-behavior timeline so the
    LLM can see the flip without the full transcript.
    """
    obs = _active_nonrejected_observations(ledger)
    by_pos: Dict[str, List[Dict[str, Any]]] = {}
    for o in obs:
        by_pos.setdefault(o["position_key"], []).append(o)

    explicit_set = set(explicit or [])
    candidates: List[Dict[str, Any]] = []
    for pos_key, entries in by_pos.items():
        # Quoteless positions collapse to per-file identity (all quoteless
        # regressions in one file share ``file\x1f``), so two unrelated findings
        # would spuriously look like an A/B flip. Mirror the trigger layer
        # (_detect_oscillation/_detect_contradiction) and never present them as a
        # candidate — only quote-anchored positions have a stable enough identity.
        # (Explicit triggers are already quote-filtered upstream by
        # evaluate_triggers, so this drops nothing legitimate.)
        if not adjudication._pk_quote(pos_key):
            continue
        expected_values = {e.get("expected_norm", "") for e in entries}
        # Oscillation signal (≥2 distinct expected) OR an explicit trigger hit.
        if len(expected_values) < 2 and pos_key not in explicit_set:
            continue
        first = entries[0]
        timeline = [
            {"round": e.get("round"), "expected": e.get("expected_norm", "")}
            for e in sorted(entries, key=lambda e: e.get("round", 0))
        ]
        candidates.append(
            {
                "position_key": pos_key,
                "file": first.get("file", ""),
                "quote": first.get("quote_norm", ""),
                "timeline": timeline,
            }
        )
    return candidates


def _fixed_then_reopened(ledger: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Positions declared ``fixed`` in a fix round then flagged again (打脸).

    Surfaced in the prompt as extra evidence of a real contradiction: a fix
    that was reported resolved yet the same location is demanded otherwise.
    """
    rows: List[Dict[str, Any]] = []
    for res in ledger.get("resolutions", []):
        if res.get("abolished") or res.get("status") != "fixed":
            continue
        rows.append(
            {
                "position_key": res.get("position_key", ""),
                "expected_when_fixed": res.get("expected_norm", ""),
            }
        )
    return rows


def _position_fingerprints(
    ledger: Dict[str, Any], position_keys: List[str]
) -> List[str]:
    """All still-active fingerprints at the given positions.

    Used to abolish every ledger entry grounded on a clause the ruling removed:
    both directions of an oscillating position cited the (now-defunct) clause,
    so all of that position's fingerprints must stop counting toward triggers.
    """
    targets = set(position_keys)
    fps: List[str] = []
    for o in ledger.get("observations", []):
        if o.get("position_key") in targets and not o.get("abolished"):
            fp = o.get("fingerprint")
            if fp:
                fps.append(fp)
    return fps


# --------------------------------------------------------------------------- #
# Effective task_description / plan resolution (self-sufficient reads)
# --------------------------------------------------------------------------- #
#
# The state machine also injects these via ``_build_step_inputs`` (adjudicated >
# refined > original), but the handler resolves them itself so it is testable in
# isolation and robust when an input key is absent. Priority mirrors the state
# machine: a prior ADJUDICATE ruling's override wins over discovery-refined,
# which wins over the raw original.


def _latest_adjudicated(flow: FlowInstance, key: str) -> Any:
    """Most recent completed ADJUDICATE step's ``key`` output, if any.

    Walks step_history in reverse so multi-generational rulings resolve to the
    latest. The *current* step is excluded (its outputs are being written now).
    """
    if not flow.state:
        return None
    for sid in reversed(flow.state.step_history):
        s = flow.state.steps.get(sid)
        if (
            s
            and s.step_type == StepType.ADJUDICATE
            and s.status in (StepStatus.COMPLETED, StepStatus.PARTIAL)
        ):
            val = s.outputs.get(key)
            if val:
                return val
    return None


def _effective_task_description(step: Step, flow: FlowInstance) -> str:
    """Currently-effective *pre-interjection* task_description for the prompt.

    Deliberately resolves the un-decorated base (``adjudicated > refined >
    original``), NOT the interjection-composed ``step.inputs['task_description']``
    that ``_build_step_inputs`` injects (that value carries the
    ``## Additional Instructions`` section). The LLM is asked for a *full
    corrected task description* which is then installed at the BASE layer by
    ``_effective_task_description_base``; feeding it the composed text would bake
    the interjections into the base, after which the composer re-appends the same
    interjections — duplicating every interjection in every post-ruling prompt
    and in the self_check ``task_description_base`` source-pool entry. A clean
    ``task_description_base`` input is honored when present (so the handler stays
    testable in isolation); otherwise the flow's effective base is recomputed
    here (which already folds in any prior adjudication / discovery refinement).
    """
    injected_base = step.inputs.get("task_description_base")
    if isinstance(injected_base, str) and injected_base:
        return injected_base
    from ..state_machine import _effective_task_description_base
    return _effective_task_description_base(flow)


def _effective_task_groups(step: Step, flow: FlowInstance) -> List[Dict[str, Any]]:
    """Currently-effective plan task_groups for the ruling prompt."""
    injected = step.inputs.get("task_groups")
    if isinstance(injected, list) and injected:
        return injected
    plan = step.inputs.get("plan")
    if isinstance(plan, dict) and isinstance(plan.get("task_groups"), list):
        return plan["task_groups"]
    prior = _latest_adjudicated(flow, "adjudicated_plan")
    if isinstance(prior, list) and prior:
        return prior
    # Latest completed PLAN step's task_groups.
    if flow.state:
        for sid in reversed(flow.state.step_history):
            s = flow.state.steps.get(sid)
            if (
                s
                and s.step_type in (StepType.PLAN, StepType.PLAN_TASKS)
                and s.status in (StepStatus.COMPLETED, StepStatus.PARTIAL)
            ):
                tg = s.outputs.get("task_groups")
                if isinstance(tg, list) and tg:
                    return tg
    return []


# --------------------------------------------------------------------------- #
# Prompt assembly
# --------------------------------------------------------------------------- #

ADJUDICATE_PROMPT = """\
You are the ADJUDICATE step — the fix-loop's spec-contradiction adjudicator.

The review layer (self_check) has been flagging the SAME code location in
CONFLICTING directions across multiple fix rounds, causing the fix loop to
oscillate (each fix undoes the last). You are the single authority that rules on
whether the task specification itself is internally contradictory, conflicts
with a hard project constraint (see the Charter below), or whether the review
simply diverged. You then issue a MINIMAL override patch to break the deadlock.

You are NOT a reviewer: do not report new code deviations or invent issues. Rule
on the specification, not the code.

## Trigger reasons
{trigger_reasons}

## Currently-effective task description
{task_description}

## Currently-effective plan task groups
{task_groups}

## Cross-round issue ledger — candidate contradictions
Each candidate is a code location repeatedly flagged with DIFFERENT expected
behaviors across rounds. `expected` values are normalized. Rule each candidate:
is it a REAL contradiction (the spec genuinely demands two incompatible things),
or BENIGN (the flip is a review misfire / already reconciled)?

{candidates}

## Positions reported "fixed" then re-flagged (打脸 evidence)
{fixed_reopened}

## Your task
1. Classify the overall situation: "internal_contradiction" (the spec
   contradicts itself), "hard_constraint_conflict" (the spec conflicts with a
   charter/hard constraint), or "review_divergence" (no real contradiction; the
   review just diverged).
2. Decide the SMALLEST fix that resolves the deadlock: rewrite the task
   DESCRIPTION, and/or override the plan's task GROUPS. Change only what is
   necessary — the override REPLACES the corresponding text wholesale, so return
   the complete corrected text/groups, but keep edits minimal.
   - If you rewrite the description, remove or reconcile the dead/contradictory
     clause so it can no longer be cited.
   - If the real fix is in the plan (e.g. a task's acceptance criterion is the
     contradictory demand), override `adjudicated_plan` with the full corrected
     task_groups instead.
   - If the situation is pure "review_divergence", leave BOTH null and mark the
     candidates benign.
3. For EACH candidate, return a verdict: "contradiction" (real; resolved by your
   patch) or "benign" (not a real contradiction; give a short reason). Benign
   candidates will never re-trigger you.

Respond in JSON:
```json
{{
    "contradiction_type": "internal_contradiction|hard_constraint_conflict|review_divergence",
    "adjudicated_description": "full corrected task description, or null if unchanged",
    "adjudicated_plan": [ {{ "group_id": "G1", "name": "...", "tasks": [] }} ],
    "adjudication_rationale": "why this ruling resolves the oscillation (required)",
    "candidate_verdicts": [
        {{"id": 0, "verdict": "contradiction|benign", "reason": "..."}}
    ]
}}
```
Set `adjudicated_plan` to null if you are not changing the plan. Set
`adjudicated_description` to null if you are not changing the description. At
least one of them MUST be non-null unless `contradiction_type` is
"review_divergence".
"""


def _format_task_groups(task_groups: List[Dict[str, Any]]) -> str:
    if not task_groups:
        return "(no plan task groups on record)"
    import json
    try:
        return json.dumps(task_groups, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(task_groups)


def _format_candidates(candidates: List[Dict[str, Any]]) -> str:
    if not candidates:
        return "(no structural candidate on record; ruling on periodic backstop)"
    lines: List[str] = []
    for idx, cand in enumerate(candidates):
        lines.append(f"### Candidate id={idx}")
        lines.append(f"- file: {cand['file'] or '(unknown)'}")
        lines.append(f"- clause quoted: {cand['quote'] or '(none)'}")
        lines.append("- expected-behavior across rounds:")
        for t in cand["timeline"]:
            lines.append(f"    round {t['round']}: {t['expected'] or '(empty)'}")
    return "\n".join(lines)


def _format_fixed_reopened(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "(none)"
    lines = []
    for r in rows:
        # Show only the human-facing quote portion of the position key.
        pos = r["position_key"].split(adjudication._KEY_SEP)
        loc = pos[1] if len(pos) > 1 else r["position_key"]
        lines.append(f"- {loc or '(location)'}: was fixed as \"{r['expected_when_fixed']}\"")
    return "\n".join(lines)


def _build_prompt(
    task_description: str,
    task_groups: List[Dict[str, Any]],
    candidates: List[Dict[str, Any]],
    fixed_reopened: List[Dict[str, Any]],
    trigger_reasons: List[str],
    project_root: Path,
) -> str:
    prompt = ADJUDICATE_PROMPT.format(
        trigger_reasons=", ".join(trigger_reasons) or "(periodic backstop)",
        task_description=task_description or "(empty)",
        task_groups=_format_task_groups(task_groups),
        candidates=_format_candidates(candidates),
        fixed_reopened=_format_fixed_reopened(fixed_reopened),
    )
    # Inject the charter (hard-constraint source) so spec-vs-hard-constraint
    # conflicts are detectable. Deliberately NOT the full transcript.
    try:
        from ..context_builder import get_charter_injection
        prompt += get_charter_injection(project_root)
    except Exception:  # pragma: no cover - charter injection is best-effort
        pass
    return prompt


# --------------------------------------------------------------------------- #
# Ruling validation (covering-patch requirement)
# --------------------------------------------------------------------------- #
#
# A real-contradiction ruling MUST carry a covering override patch. These
# helpers gate the LLM output before it is allowed to land so a malformed no-op
# ruling cannot silently reflow to SELF_CHECK against an unchanged spec.

# Contradiction classes that REQUIRE a covering override patch. Only
# ``review_divergence`` (no real contradiction) may legitimately return none.
_PATCH_REQUIRED_TYPES = ("internal_contradiction", "hard_constraint_conflict")

# Bounded semantic-retry count for a no-op contradiction ruling.
_MAX_RULING_ATTEMPTS = 2

_NOOP_RULING_REPROMPT = (
    "\n\n## Your previous ruling was rejected\n"
    "Your ruling (\"{contradiction_type}\") was not actionable. EVERY ruling MUST "
    "carry a non-empty `adjudication_rationale` explaining the verdict (it is the "
    "recorded justification and the dismissal reason for benign candidates). A "
    "ruling that classifies a REAL contradiction (internal_contradiction / "
    "hard_constraint_conflict) MUST additionally carry a covering override patch "
    "(`adjudicated_description` and/or `adjudicated_plan`) so the effective spec "
    "actually changes. Provide the missing field(s); if there is in fact no real "
    "contradiction, reclassify as \"review_divergence\" — but still give a "
    "rationale saying why.\n"
)


def _normalized_overrides(result: Dict[str, Any]) -> tuple:
    """Coerce + type-validate the two override fields to (description, plan).

    Returns each as its accepted value or ``None``. A description is accepted
    only as a non-empty string; a plan only as a non-empty list of task groups.
    Shared by the actionability gate and ``_apply_ruling`` so both judge a patch
    "present" by identical rules.
    """
    desc = _coerce_override(result.get("adjudicated_description"))
    if desc is not None and not (isinstance(desc, str) and desc.strip()):
        desc = None
    plan = _coerce_override(result.get("adjudicated_plan"))
    if plan is not None and not (isinstance(plan, list) and plan):
        plan = None
    return desc, plan


def _ruling_is_actionable(result: Dict[str, Any]) -> bool:
    """True when the ruling is safe to land.

    ``review_divergence`` legitimately carries no patch (nothing to change). Any
    other classification (a real contradiction) MUST carry a covering override
    in ``adjudicated_description`` and/or ``adjudicated_plan`` — otherwise
    landing it would re-run SELF_CHECK against the same effective spec and let
    the loop keep oscillating. EVERY ruling — including ``review_divergence`` —
    MUST additionally carry a non-empty ``adjudication_rationale``: it is the sole
    recorded justification for the human confirmer and the audit trail, and it is
    reused as the dismissal reason stamped onto each benign candidate. A blank or
    whitespace-only rationale would leave the audit empty and stamp whitespace
    reasons onto rejected candidates, so treat it as non-actionable and let the
    semantic retry re-ask for one.
    """
    rationale = str(result.get("adjudication_rationale", "") or "").strip()
    if not rationale:
        return False
    ctype = str(result.get("contradiction_type", "")).strip().lower()
    if ctype == "review_divergence":
        return True
    desc, plan = _normalized_overrides(result)
    return bool(desc or plan)


# --------------------------------------------------------------------------- #
# Effective source pool (dead-clause detection for abolish filtering)
# --------------------------------------------------------------------------- #


def _effective_source_pool(description: Any, task_groups: Any) -> List[str]:
    """Source-pool entries the *adjudicated* spec exposes to quote validation.

    Mirrors ``self_check._build_source_pool`` composition for the parts an
    override can touch: the task description plus each plan task's description
    and acceptance_criteria. Used to decide whether a candidate's quoted clause
    actually left the effective spec after the ruling.
    """
    pool: List[str] = []
    if isinstance(description, str) and description:
        pool.append(description)
    if isinstance(task_groups, list):
        for group in task_groups:
            if not isinstance(group, dict):
                continue
            tasks = group.get("tasks") or []
            if not isinstance(tasks, list):
                continue
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                desc = task.get("description")
                if isinstance(desc, str) and desc.strip():
                    pool.append(desc)
                criteria = task.get("acceptance_criteria") or []
                if isinstance(criteria, list):
                    for c in criteria:
                        if isinstance(c, str) and c.strip():
                            pool.append(c)
    return pool


def _positions_with_dead_clause(
    positions: List[str],
    quote_by_pos: Dict[str, str],
    effective_desc: Any,
    effective_groups: Any,
) -> List[str]:
    """Filter ``positions`` to those whose quoted clause is GONE from the pool.

    Abolishing by position alone (the old behavior) wrongly clears history for a
    clause the ruling PRESERVED — a plan-only fix, or a description rewrite that
    kept one quoted hard constraint. Only a position whose normalized quote no
    longer substring-matches any entry of the now-effective source pool has
    truly been abolished; a still-live quote keeps counting toward triggers
    (a)/(b)/(c). A position with no quote at all is left counting: an override
    cannot be shown to have removed a clause that was never quoted (e.g. a
    regression grounded only in evidence_lines).

    The match mirrors self_check's quote validation exactly (via the shared
    ``adjudication.relaxed_quote_candidates``): a clause is dead only when NONE of
    its relaxed variants (bullet/id-prefix- or trailing-ellipsis-stripped) match
    the pool — otherwise a decorated ledger quote self_check would still accept
    would be judged dead and its live oscillation history wrongly cleared.
    """
    pool_norm = [
        adjudication._normalize_for_quote_match(s)
        for s in _effective_source_pool(effective_desc, effective_groups)
    ]
    pool_norm = [p for p in pool_norm if p]
    dead: List[str] = []
    for pos in positions:
        quote = quote_by_pos.get(pos)
        if quote is None:
            # Defensive: recover the normalized-quote component from the key.
            quote = adjudication._pk_quote(pos)
        if not quote:
            continue
        candidates = adjudication.relaxed_quote_candidates(quote)
        if not any(c in p for c in candidates for p in pool_norm):
            dead.append(pos)
    return dead


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #

def adjudicate_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Rule on spec contradictions surfaced by the fix loop.

    Builds the ruling prompt from the cross-round ledger + effective
    task_description/plan (no transcript), calls the LLM to classify and emit a
    minimal override patch, then writes the ruling into this step's own
    ``outputs`` and updates the ledger (abolish clauses the ruling removed,
    record benign candidates). Returns COMPLETED; the confirmation gate and
    post-ruling routing (skip IMPLEMENT/TEST, re-run SELF_CHECK at pass #1) are
    the state machine's responsibility.
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    ctx = flow.state.context if flow.state else {}

    ledger = _ledger(flow)
    task_description = _effective_task_description(step, flow)
    task_groups = _effective_task_groups(step, flow)

    explicit_positions = step.inputs.get("adjudication_triggering_positions") or []
    trigger_reasons = step.inputs.get("adjudication_reasons") or []
    candidates = _candidate_positions(ledger, explicit_positions)
    fixed_reopened = _fixed_then_reopened(ledger)

    prompt = _build_prompt(
        task_description,
        task_groups,
        candidates,
        fixed_reopened,
        trigger_reasons,
        project_root,
    )

    # When the confirmation门 rejected a prior ruling, the reviewer's feedback is
    # threaded back here (via the shared _transition_to_revision path) so the
    # re-ruling can address the objection rather than re-emit the same override.
    revision_feedback = step.inputs.get("revision_feedback")
    if step.inputs.get("is_revision") and revision_feedback:
        prompt += (
            "\n\n## Reviewer rejected your previous ruling — revise accordingly\n"
            "A reviewer declined the ruling above. Address this feedback and "
            "emit a corrected minimal override patch:\n"
            f"{revision_feedback}\n"
        )

    logger.info(
        "Running adjudication (flow %s): %d candidate(s), reasons=%s",
        flow.flow_id, len(candidates), trigger_reasons or "periodic",
    )

    fix_iteration = step.inputs.get("fix_iteration", 0)
    retry_count = step.inputs.get("retry_count", 0)

    # A ruling that classifies a REAL contradiction must carry a covering
    # override patch (adjudicated_description and/or adjudicated_plan). A no-op
    # contradiction ruling (patches both null) would reflow to SELF_CHECK with
    # the effective spec unchanged and let the loop keep oscillating — the exact
    # failure the adjudicator exists to break. Re-ask the LLM with a strict
    # patch demand before giving up; only ``review_divergence`` may return no
    # patch. LLMCaller has its own transport retries; this bounded loop is a
    # *semantic* retry layered on top.
    result: Optional[Dict[str, Any]] = None
    strict_suffix = ""
    for attempt in range(_MAX_RULING_ATTEMPTS):
        try:
            caller = LLMCaller(
                project_root,
                flow_id=flow.flow_id,
                step_id=step.step_id,
                step_type=step.step_type.value,
                external_attempt=retry_count + attempt,
                fix_iteration=fix_iteration,
            )
            response = caller.call(
                prompt=prompt + strict_suffix,
                json_mode="two_phase",
                json_schema_hint=(
                    '{"contradiction_type": "internal_contradiction|hard_constraint_conflict|review_divergence", '
                    '"adjudicated_description": "... or null", '
                    '"adjudicated_plan": [{"group_id": "G1", "name": "...", "tasks": []}], '
                    '"adjudication_rationale": "...", '
                    '"candidate_verdicts": [{"id": 0, "verdict": "contradiction|benign", "reason": "..."}]}'
                ),
                required_keys=["adjudication_rationale"],
            )
            result = parse_json_response(response, required_keys=["adjudication_rationale"])
        except Exception as e:  # pragma: no cover - defensive; LLMCaller has its own retries
            step.error_message = f"Adjudication LLM call failed: {e}"
            return StepStatus.FAILED

        if not result:
            step.error_message = "Failed to parse adjudication result from LLM response"
            return StepStatus.FAILED

        if _ruling_is_actionable(result):
            break

        strict_suffix = _NOOP_RULING_REPROMPT.format(
            contradiction_type=result.get("contradiction_type", "")
        )
        logger.warning(
            "Adjudication returned a no-op contradiction ruling "
            "(attempt %d/%d); re-asking with a strict patch demand",
            attempt + 1, _MAX_RULING_ATTEMPTS,
        )

    if not _ruling_is_actionable(result):
        # A ruling that never became actionable survived every retry (missing
        # rationale, or a real-contradiction verdict with no covering patch):
        # fail rather than reflow with an unchanged/unaudited spec (which would
        # let the oscillation continue indefinitely).
        step.error_message = (
            "Adjudication ruling was not actionable "
            f"(type='{result.get('contradiction_type', '')}': missing rationale "
            "and/or override patch) after retries; refusing to reflow with an "
            "unchanged spec"
        )
        return StepStatus.FAILED

    _apply_ruling(step, ctx, ledger, result, candidates, task_description, task_groups)
    return StepStatus.COMPLETED


def _coerce_override(value: Any) -> Any:
    """Normalize a nullable override field.

    The LLM may emit the literal string "null"/"none"/"" for "no change"; treat
    all of those as absent so the effective-text layer falls through to the
    prior generation rather than overriding with an empty patch.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped.lower() in ("null", "none"):
            return None
        return value
    return value


def _apply_ruling(
    step: Step,
    ctx: Dict[str, Any],
    ledger: Dict[str, Any],
    result: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    task_description: str,
    task_groups: List[Dict[str, Any]],
) -> None:
    """Write the ruling to ``step.outputs`` and stage its ledger side effects.

    Gated two-phase: if the ruling found NO real contradiction (a
    ``review_divergence`` — its override, if any, is discarded first so
    ``has_patch`` is False), take the transparent **no-op** path: mark
    ``adjudication_noop`` and record the audit-only verdict + benign
    ``rejected_positions``, then return WITHOUT writing
    adjudicated_description / adjudicated_plan / superseded_fix_instructions /
    fix_instructions_superseded and WITHOUT abolishing anything. The state
    machine then routes to IMPLEMENT with the triggering round's fix_instructions
    intact.

    Otherwise (a real contradiction) take the patch path: record the override
    patch (description/plan), the rationale, an ISO timestamp, and the superseded
    pending fix_instructions (audit). Map each LLM candidate verdict back to a
    ledger position: "contradiction" *stages* abolition of the position's active
    fingerprints — but only for positions whose quoted clause actually left the
    now-effective source pool (a preserved clause keeps counting). "benign"
    stages the position as a rejected candidate so it never re-triggers. Never
    writes self_check-style ``issues``.

    The ledger is NOT mutated here: the abolish/reject effects are staged into
    ``step.outputs`` and applied by ``apply_landed_ledger_effects`` only once the
    ruling LANDS (approved by the confirmation门, or when免确认). A rejected
    ruling's revision re-runs ADJUDICATE and overwrites these pending outputs, so
    a rejected ruling never lands its side effects on the persisted ledger.
    """
    contradiction_type = result.get("contradiction_type", "")
    is_review_divergence = str(contradiction_type).strip().lower() == "review_divergence"
    adjudicated_description, adjudicated_plan = _normalized_overrides(result)
    # A review_divergence ruling asserts there is NO real contradiction, so it has
    # no authority to rewrite the spec. Discard any override the LLM emitted next
    # to that classification *before* has_patch is computed: otherwise a benign
    # oscillation returning {contradiction_type:"review_divergence",
    # adjudicated_description:"..."} would land a task-description/plan override and
    # stage abolition against the patched source pool — exactly the behavior a
    # non-contradiction verdict must never produce. Only a real-contradiction
    # classification may carry a covering patch.
    if is_review_divergence:
        adjudicated_description = None
        adjudicated_plan = None
    rationale = result.get("adjudication_rationale", "")
    verdicts = result.get("candidate_verdicts") or []
    has_patch = bool(adjudicated_description or adjudicated_plan)

    # Map verdicts (by candidate id) to positions.
    quote_by_pos = {c["position_key"]: c["quote"] for c in candidates}
    contradiction_positions: List[str] = []
    rejected_positions: List[str] = []
    rejected_records: List[Dict[str, Any]] = []
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        try:
            cid = int(v.get("id"))
        except (TypeError, ValueError):
            continue
        if cid < 0 or cid >= len(candidates):
            continue
        pos_key = candidates[cid]["position_key"]
        verdict = str(v.get("verdict", "")).lower()
        if verdict == "contradiction":
            contradiction_positions.append(pos_key)
        elif verdict == "benign":
            rejected_positions.append(pos_key)
            rejected_records.append(
                {
                    "position_key": pos_key,
                    "file": candidates[cid]["file"],
                    "quote": candidates[cid]["quote"],
                    "reason": v.get("reason", ""),
                }
            )

    # A description/plan override with no explicit contradiction verdicts still
    # means the flagged candidates were resolved — consider them all, so a ruling
    # is never silently ignored by the trigger layer on the next round.
    if has_patch and not contradiction_positions:
        contradiction_positions = [c["position_key"] for c in candidates]

    # A review_divergence ruling asserts there is NO real contradiction, so its
    # override was dropped above (has_patch is therefore always False here) and it
    # abolishes nothing. Any triggering candidate it leaves un-rejected (e.g. an
    # empty/omitted candidate_verdicts list, or a stray per-candidate
    # "contradiction" verdict that has no patch behind it to abolish) would stay
    # live and re-trigger ADJUDICATE next round — the exact loop the
    # rejected-candidate ledger exists to break. Close out every candidate:
    # default the ones the LLM did not explicitly rule benign to rejected, so a
    # no-contradiction ruling can never re-fire on the same position.
    if is_review_divergence and not has_patch:
        contradiction_positions = []
        # ``_ruling_is_actionable`` already guarantees a non-blank rationale, but
        # strip defensively so a whitespace-only string can never become a benign
        # candidate's dismissal reason (a bare-space reason reads as "no reason").
        clean_rationale = rationale.strip() if isinstance(rationale, str) else ""
        default_reason = clean_rationale or "adjudicated as review divergence — no real contradiction"
        for c in candidates:
            pos_key = c["position_key"]
            if pos_key in rejected_positions:
                continue
            rejected_positions.append(pos_key)
            rejected_records.append(
                {
                    "position_key": pos_key,
                    "file": c["file"],
                    "quote": c["quote"],
                    "reason": default_reason,
                }
            )

        # No real contradiction survived (every candidate benign / a
        # review_divergence verdict — including a period-baseline sweep that
        # found nothing): ADJUDICATE becomes a transparent no-op. It writes an
        # audit-only verdict but must NOT supersede the pending fix_instructions,
        # clear issues, abolish ledger entries, or reflow — the state machine
        # reads ``adjudication_noop`` and routes straight to IMPLEMENT with the
        # triggering round's untouched fix_instructions, as if ADJUDICATE had
        # never been inserted. Exactly two mechanical bookkeeping effects are
        # kept: the benign positions staged into ``rejected_positions`` (a
        # trigger-layer filter that stops the same benign flip re-invoking the
        # LLM every round), landed by ``apply_landed_ledger_effects``; and the
        # ``period_baseline`` reset, already done at insertion time. Deliberately
        # absent: adjudicated_description / adjudicated_plan /
        # superseded_fix_instructions / fix_instructions_superseded.
        step.outputs["adjudication_noop"] = True
        step.outputs["contradiction_type"] = contradiction_type
        step.outputs["adjudication_rationale"] = rationale
        step.outputs["adjudicated_at"] = datetime.now(timezone.utc).isoformat()
        step.outputs["candidate_verdicts"] = verdicts
        step.outputs["rejected_candidates"] = rejected_records
        step.outputs["rejected_positions"] = rejected_positions
        # REMOVE the patch/supersede-path keys rather than writing falsy sentinels.
        # The no-op output contract is that adjudicated_description / adjudicated_plan
        # / superseded_fix_instructions / fix_instructions_superseded are ABSENT, so an
        # audit consumer (history renderer, --resume replay) sees the transparent
        # no-op shape and cannot mistake a sentinel None/""/False for an override or
        # supersede that actually happened. Popping also clears any stale value a prior
        # in-place run left behind: this step may re-run (a rejected patch ruling →
        # _transition_to_revision keeps step.outputs → the revision re-run now rules
        # review_divergence). Were a rejected, never-approved override left lingering,
        # _latest_adjudicated_output / _effective_task_description_base would pick up the
        # stale adjudicated_description and every downstream step would silently operate
        # on the spec rewrite the human explicitly rejected, bypassing the confirmation
        # gate. Consumers all read these via .get()/truthiness, so absence is equivalent
        # to the old sentinels for routing while keeping the audit trail honest.
        for _patch_key in (
            "adjudicated_description",
            "adjudicated_plan",
            "superseded_fix_instructions",
            "fix_instructions_superseded",
        ):
            step.outputs.pop(_patch_key, None)
        # No patch → nothing to abolish; abolished_fingerprints stays empty so
        # apply_landed_ledger_effects skips mark_abolished and only lands the
        # benign rejected_positions.
        step.outputs["abolished_fingerprints"] = []
        step.outputs["ledger_effects_applied"] = False
        step.outputs["candidates_considered"] = [
            {"file": c["file"], "quote": c["quote"], "position_key": c["position_key"]}
            for c in candidates
        ]
        logger.info(
            "Adjudication ruled '%s' (no-op): no real contradiction; "
            "staged_rejected=%d, no supersede/abolish/reflow (transparent pass-through)",
            contradiction_type, len(rejected_positions),
        )
        return

    # Only abolish a position whose quoted clause is GONE from the now-effective
    # source pool: a plan-only fix (description unchanged) or a description
    # rewrite that kept a quoted hard constraint must NOT clear the history of a
    # still-live clause, or renewed oscillation there would evade the triggers.
    effective_desc = adjudicated_description if adjudicated_description else task_description
    # When no ``adjudicated_plan`` is supplied the latest plan (``task_groups``)
    # stays authoritative: self_check keeps validating ``plan_task`` quotes against
    # it, so a clause still present there remains quotable and must NOT be
    # abolished. Only a plan override actually rewrites the plan; a description-only
    # ruling has no authority to erase a plan requirement it never overrode.
    # Judging dead-clause against an empty plan here would abolish a still-live
    # plan-grounded quote, and self_check would then drop the valid issue re-raising
    # it as quote_not_in_source — silently converging on an unchanged plan
    # requirement. So a clause is dead only when gone from BOTH the effective
    # description and the effective (latest, or overridden) plan.
    effective_groups = adjudicated_plan if adjudicated_plan else task_groups
    abolishable_positions = _positions_with_dead_clause(
        contradiction_positions, quote_by_pos, effective_desc, effective_groups,
    )
    abolished_fps = _position_fingerprints(ledger, abolishable_positions)

    # Supersede the pending fix_instructions (kept only for audit). Only the
    # patch path reaches here — a no-op (review_divergence) ruling returned above
    # WITHOUT superseding, so its triggering-round instructions flow untouched
    # into IMPLEMENT. For a real contradiction the post-ruling reflow re-runs
    # SELF_CHECK from pass #1 rather than feeding the pre-adjudication
    # instructions into IMPLEMENT: those instructions come from the oscillating
    # round and applying them would chase the very knot the ruling just examined
    # while ignoring the switched source pool. Re-running instead reproduces any
    # still-valid issue and routes it through the normal fix loop, while the
    # recorded rejected candidates keep a benign flip from re-triggering ADJUDICATE.
    superseded = step.inputs.get("fix_instructions", "") or ""

    # This step may re-run in place: a prior run could have produced a no-op
    # ruling (adjudication_noop=True), and this re-run is now a real
    # contradiction taking the patch path. Force the flag false so
    # transition_to_next never treats a *stale* True as authoritative and routes
    # a real-contradiction ruling through the no-op pass-through — bypassing the
    # confirmation gate, supersede, abolition and SELF_CHECK reflow it requires.
    step.outputs["adjudication_noop"] = False
    step.outputs["contradiction_type"] = contradiction_type
    step.outputs["adjudicated_description"] = adjudicated_description
    step.outputs["adjudicated_plan"] = adjudicated_plan
    step.outputs["adjudication_rationale"] = rationale
    step.outputs["adjudicated_at"] = datetime.now(timezone.utc).isoformat()
    step.outputs["superseded_fix_instructions"] = superseded
    step.outputs["fix_instructions_superseded"] = bool(superseded)
    step.outputs["rejected_candidates"] = rejected_records
    # Staged (not yet applied) ledger side effects — see docstring / issue 1.
    step.outputs["abolished_fingerprints"] = abolished_fps
    step.outputs["rejected_positions"] = rejected_positions
    step.outputs["ledger_effects_applied"] = False
    # Audit view of what was ruled against, for history renderers.
    step.outputs["candidates_considered"] = [
        {"file": c["file"], "quote": c["quote"], "position_key": c["position_key"]}
        for c in candidates
    ]

    logger.info(
        "Adjudication ruled '%s': description_patch=%s plan_patch=%s "
        "staged_abolish=%d staged_rejected=%d superseded_fix=%s (pending landing)",
        contradiction_type,
        bool(adjudicated_description),
        bool(adjudicated_plan),
        len(abolished_fps),
        len(rejected_positions),
        bool(superseded),
    )


def apply_landed_ledger_effects(step: Step, ctx: Dict[str, Any]) -> int:
    """Apply a ruling's staged ledger side effects once it has LANDED.

    Called by the state machine only after the ruling is APPROVED by the
    confirmation门 (or when免确认) — never on a rejected ruling. This is where
    ``abolished`` marking and rejected-candidate recording actually mutate the
    persisted ledger, so a ruling the human rejects leaves the ledger untouched
    and the oscillation it failed to resolve keeps counting toward the triggers.

    Idempotent: ``mark_abolished`` and ``record_rejected_candidates`` both skip
    already-applied entries, so a ``--resume`` replay of the landing is safe.
    Returns the number of ledger entries newly flagged abolished.
    """
    fps = step.outputs.get("abolished_fingerprints") or []
    count = adjudication.mark_abolished(ctx, fps) if fps else 0
    positions = step.outputs.get("rejected_positions") or []
    adjudication.record_rejected_candidates(ctx, positions)
    step.outputs["abolished_count"] = count
    step.outputs["ledger_effects_applied"] = True

    logger.info(
        "Adjudication ruling landed: abolished=%d entries, rejected=%d position(s)",
        count, len(positions),
    )
    return count
