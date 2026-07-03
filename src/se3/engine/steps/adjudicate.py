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
  * Parse the ruling into this step's own ``outputs``
    (adjudicated_description / adjudicated_plan / adjudication_rationale /
    adjudicated_at / superseded_fix_instructions / rejected_candidates),
    supersede the pending ``fix_instructions`` for audit, mark ledger entries
    grounded on now-defunct clauses ``abolished``, and record benign candidate
    positions so they never re-trigger.

The handler does NOT emit self_check-style ``issues`` (권责 stays split: review
reports deviations, adjudicate rules on the spec) and does NOT touch the
original discovery/plan step outputs. Routing of the ruling (skip
IMPLEMENT/TEST, re-run SELF_CHECK at pass #1) and the human/LLM confirmation
gate live in the state machine (later groups); a description-changing ruling is
gated by an inserted CONFIRM step (``confirmation.steps.adjudicate``).
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
    """Currently-effective task_description for the ruling prompt."""
    injected = step.inputs.get("task_description")
    if isinstance(injected, str) and injected:
        return injected
    prior = _latest_adjudicated(flow, "adjudicated_description")
    if isinstance(prior, str) and prior:
        return prior
    # Fall back to the pre-interjection base (handles discovery refinement).
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
    try:
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value,
            external_attempt=retry_count,
            fix_iteration=fix_iteration,
        )
        response = caller.call(
            prompt=prompt,
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
    """Write the ruling to ``step.outputs`` and update the ledger.

    Records the override patch (description/plan), the rationale, an ISO
    timestamp, and the superseded pending fix_instructions (audit). Maps each
    LLM candidate verdict back to a ledger position: "contradiction" abolishes
    every active fingerprint at that position (its grounding clause is gone),
    "benign" records the position as a rejected candidate so it never
    re-triggers. Never writes self_check-style ``issues``.
    """
    contradiction_type = result.get("contradiction_type", "")
    adjudicated_description = _coerce_override(result.get("adjudicated_description"))
    adjudicated_plan = _coerce_override(result.get("adjudicated_plan"))
    rationale = result.get("adjudication_rationale", "")
    verdicts = result.get("candidate_verdicts") or []

    # Only accept a plan override that is a non-empty list of task groups.
    if adjudicated_plan is not None and not (
        isinstance(adjudicated_plan, list) and adjudicated_plan
    ):
        adjudicated_plan = None
    # Only accept a description override that is a non-empty string.
    if adjudicated_description is not None and not isinstance(adjudicated_description, str):
        adjudicated_description = None

    # Map verdicts (by candidate id) to positions.
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
    # means the flagged candidates were resolved — abolish them too, so a ruling
    # is never silently ignored by the trigger layer on the next round.
    if (adjudicated_description or adjudicated_plan) and not contradiction_positions:
        contradiction_positions = [c["position_key"] for c in candidates]

    # Abolish ledger entries grounded on now-removed clauses; record benign ones.
    abolished_fps = _position_fingerprints(ledger, contradiction_positions)
    abolished_count = adjudication.mark_abolished(ctx, abolished_fps) if abolished_fps else 0
    adjudication.record_rejected_candidates(ctx, rejected_positions)

    # Supersede the pending fix_instructions (kept only for audit): the ruling
    # dissolved the deadlock, so implementing the old instructions would chase a
    # spec knot that no longer exists.
    superseded = step.inputs.get("fix_instructions", "") or ""

    step.outputs["contradiction_type"] = contradiction_type
    step.outputs["adjudicated_description"] = adjudicated_description
    step.outputs["adjudicated_plan"] = adjudicated_plan
    step.outputs["adjudication_rationale"] = rationale
    step.outputs["adjudicated_at"] = datetime.now(timezone.utc).isoformat()
    step.outputs["superseded_fix_instructions"] = superseded
    step.outputs["fix_instructions_superseded"] = bool(superseded)
    step.outputs["rejected_candidates"] = rejected_records
    step.outputs["abolished_fingerprints"] = abolished_fps
    step.outputs["abolished_count"] = abolished_count
    # Audit view of what was ruled against, for history renderers.
    step.outputs["candidates_considered"] = [
        {"file": c["file"], "quote": c["quote"], "position_key": c["position_key"]}
        for c in candidates
    ]

    logger.info(
        "Adjudication ruled '%s': description_patch=%s plan_patch=%s "
        "abolished=%d rejected=%d superseded_fix=%s",
        contradiction_type,
        bool(adjudicated_description),
        bool(adjudicated_plan),
        abolished_count,
        len(rejected_positions),
        bool(superseded),
    )
