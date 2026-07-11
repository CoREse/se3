"""Charter Freshness step handler.

A flow-end step that reuses the ``version_analyze`` shape ("LLM reads the change
and gives a recommendation") to answer one cheap question: **did this diff touch
any of the charter's three content classes?**

    1. project identity / positioning
    2. top-level / cross-subsystem architecture
    3. project-wide cross-cutting conventions & hard constraints

The overwhelming majority of flows touch none of those classes and pass for
free. When the diff *does* plausibly touch one, the step no longer stops at a
dead-letter advisory: sitting **after a COMPLETED ``invariant_check``**, it runs
a self-contained **propose -> gate -> apply** closed loop that may auto-write
``se3/charter.md`` itself.

Why the handler is allowed to write the constitution: ``invariant_check`` has
already proven the code diff legal against the *frozen* invariant anchors
(old charter / task text / why-comments). A purely **descriptive** charter
update — making the constitution reflect the already-approved new reality — is
therefore something that *should* be done, and doing it inside this flow is not
"the audited rewriting the standard of judgement" (this flow's anchors are
frozen; judgement is over). The generator of the update and its final
adjudicator are both this step, so routing through a separate ``implement``
sub-step would add cost without real separation of powers.

The loop is **fail-safe / prefer-stale-over-degraded**:

- **precondition** — the closed loop runs only when a ``COMPLETED``
  ``invariant_check`` step exists in this flow; the step sequence varies by task
  type, so this is checked explicitly. Without it, the step keeps today's
  advisory-only behavior (records ``suggested_update``, writes nothing).
- **propose** — the LLM judges freshness against the **on-disk** charter (NOT
  the frozen invariant anchor: this task may itself be a legitimate
  constitutional amendment that already edited ``se3/charter.md``, and anchoring
  the patch on the frozen text would clobber that edit). When an update is
  needed it emits an **anchored patch** (only ``insert_after`` pure insertions
  and verbatim-quoted ``replace`` rewrites — a full rewrite is a silent-deletion
  hazard and is forbidden).
- **gate** (over the in-memory candidate, never disk state) — two halves:
  (a) the mechanical anchored-replace check (:func:`_validate_anchored_patch`,
  a program, not an LLM: every ``old_text`` / ``anchor`` must match on-disk
  verbatim, uniquely, with a bounded total edit; any unquoted deletion is
  rejected), and (b) the in-process admission gate
  (:func:`~se3.engine.charter.check_admission` size red light + the
  :func:`~se3.engine.charter.build_admission_gate_prompt` LLM verdict, which
  adds the "did a removal weaken an unrelated convention" question). Either
  failure is fed back and retried **once** in-handler; a second failure writes
  nothing, degrades to advisory, and the step still COMPLETES.
- **apply** — a passing candidate is written with a same-directory temp file +
  ``os.replace`` atomic rename. Because the gate judges *text* not disk state
  and the write is last, any crash leaves disk holding either the old text or a
  fully-gated new text — no snapshot / rollback is needed. On resume the propose
  step re-judges the (already-updated) disk and naturally reports fresh: the
  loop is idempotent.

This sub-edit does **not** re-run ``invariant_check`` / ``self_check`` / tests:
the charter text's matching reviewer is this step's two-part gate, and the
frozen anchors would misread "modifying an old rule" as "violating an old rule"
(the amendment tension), which must be avoided. The step is **never blocking**
— it always returns COMPLETED.

It also hosts the charter **admission monitoring trigger**: when the diff itself
edited ``se3/charter.md`` (a human-initiated amendment), the monitoring-light
altitude check runs against the current charter and its warning is surfaced in
the step outputs. That, too, never blocks.
"""

from __future__ import annotations

import difflib
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from ..charter import (
    CHARTER_ADMISSION_STANDARD,
    admission_check_for_changes,
    build_admission_gate_prompt,
    charter_path,
    check_admission,
    load_charter,
)
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus, StepType
from ..prompt_markers import inject_boundary
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# anchored patch bounds
# ---------------------------------------------------------------------------
# The mechanical check keeps the auto-update small and reviewable. A descriptive
# freshness update rewords a handful of stale clauses at most; anything larger
# is a red flag (a full rewrite masquerading as a patch) and is rejected so a
# human can look. These are deliberately generous headroom, not tight limits.
MAX_PATCH_OPS = 20
MAX_PATCH_NEW_CHARS = 8000
# WHY: replace ops must not become a mass-deletion vector. A verbatim-unique
# `old_text` proves the removed text was quoted, but says nothing about its
# size — a single replace could quote (and delete) nearly the whole charter
# while inserting a short `new_text`, slipping mass deletion past the mechanical
# gate and leaving it to the LLM admission gate alone. Bounding the total
# removed length keeps deletion surgical here, in the program, not deferred.
MAX_PATCH_OLD_CHARS = 8000

#: Two attempts total: an initial propose -> gate, then one bounded retry with
#: the gate's structured failure fed back into the propose prompt. A second
#: failure degrades to advisory (prefer-stale-over-degraded); it never blocks.
MAX_GATE_ATTEMPTS = 2


CHARTER_FRESHNESS_PROMPT = """You are a charter-freshness auditor AND, when an update is warranted, the author of a minimal **anchored patch** to the charter. The **charter** is the small, high-altitude document injected in full into every step of every session. Decide whether the diff below changed anything that the charter should now reflect, and if so, propose a surgical, descriptive update.

## The charter's three content classes (and ONLY these)
1. **Project identity / positioning** — what the project is, its primary language / framework, its purpose.
2. **Top-level architecture** — the semantic/subjective layering mechanical structure cannot express: which modules form one subsystem, where cross-subsystem boundaries lie. (NOT per-module locators — those live in code-index, never the charter.)
3. **Project-wide cross-cutting conventions & hard constraints** — coding conventions, key constraints, workflow conventions, version policy that apply everywhere.

## Charter admission standard (what the charter MAY / MUST NOT carry)
{admission_standard}

## Task Description
{task_description}

## Changes Made (the diff)
{changes_made}

## Current Charter (the CURRENT on-disk text — your patch anchors MUST quote from THIS exact text)
{charter}

## Feedback from the previous rejected attempt (if any)
{feedback}

## Instructions
Answer conservatively. The DEFAULT and overwhelmingly common answer is that the diff touches NONE of the three classes — a normal feature / bugfix changes implementation detail, which belongs in the code and its why-comments, NOT the charter. Only flag a touch when the diff genuinely changes the project's identity, its top-level architecture, or a project-wide convention/constraint.

When you DO flag a touch, produce a **patch** — a list of anchored operations — that makes the charter *descriptively* reflect the already-reviewed new reality. HARD RULES for the patch:

- **Descriptive only.** Correct/refresh what the charter says so it matches the new reality. Do NOT introduce new universal rules, do NOT legislate a one-off as a project-wide convention, and do NOT touch clauses unrelated to this change.
- **No full rewrite.** A wholesale rewrite is forbidden (it is the classic vehicle for silent deletion). Keep the patch minimal and surgical.
- **Only two operation kinds:**
  - `insert_after`: `{{"op": "insert_after", "anchor": "<verbatim substring of the current charter>", "new_text": "<text to insert immediately after the anchor>"}}` — a PURE INSERTION.
  - `replace`: `{{"op": "replace", "old_text": "<verbatim substring of the current charter to remove>", "new_text": "<replacement text>"}}` — a quoted rewrite.
- `anchor` and `old_text` MUST be copied **verbatim** from the current on-disk charter above, and each MUST match **exactly once** (unique). Any deletion or modification that is not carried by a quoted `old_text` is forbidden — the mechanical gate will reject an unquoted change.
- Keep every high-altitude constraint: never propose copying per-module/per-file locators or implementation detail into the charter (that is exactly the low-level leakage the admission standard forbids).

If the charter is already fresh, set `charter_update_needed` to false and return an empty `patch`.

Respond in valid JSON format:
```json
{{
  "charter_update_needed": false,
  "touched_classes": [],
  "reason": "Why the diff does or does not touch a charter content class",
  "suggested_update": "",
  "patch": []
}}
```

- `charter_update_needed`: boolean. `true` only when the diff touches one of the three classes.
- `touched_classes`: array drawn from ["identity", "architecture", "conventions"]; empty when nothing is touched.
- `reason`: one or two sentences of justification.
- `suggested_update`: a concise high-altitude prose description of what the charter should now say (kept for human review even when the patch is applied automatically); empty when no update is needed.
- `patch`: the list of anchored operations (see rules above); empty when no update is needed.
"""

# Two-segment marker only: USER_CONTENT region is empty (no user literal here).
CHARTER_FRESHNESS_PROMPT = inject_boundary(
    CHARTER_FRESHNESS_PROMPT, "## The charter's three content classes (and ONLY these)\n",
)


# ---------------------------------------------------------------------------
# changed-file extraction (unchanged helpers)
# ---------------------------------------------------------------------------
def _changed_files(changes_made: Any) -> list[str]:
    """Extract the flat list of changed paths from ``changes_made``.

    Tolerates both the plain-string and dict ``files_changed`` shapes the
    implement step may emit.
    """
    out: list[str] = []
    if not isinstance(changes_made, dict):
        return out
    files_changed = changes_made.get("files_changed") or []
    if not isinstance(files_changed, list):
        return out
    for entry in files_changed:
        if isinstance(entry, str) and entry:
            out.append(entry)
        elif isinstance(entry, dict):
            p = entry.get("path") or entry.get("file_path")
            if isinstance(p, str) and p:
                out.append(p)
    return out


def _format_changes(changes_made: dict[str, Any]) -> str:
    """Render the diff's file list (+ explanations) for the prompt."""
    if not changes_made:
        return "No changes recorded."
    lines: list[str] = []
    for fc in changes_made.get("files_changed", []):
        if isinstance(fc, str):
            lines.append(f"- modified: {fc}")
        elif isinstance(fc, dict):
            path = fc.get("path", "?")
            action = fc.get("action", "?")
            explanation = fc.get("explanation", "")
            lines.append(f"- {action}: {path}")
            if explanation:
                lines.append(f"  ({explanation})")
        else:
            lines.append(f"- {fc}")
    return "\n".join(lines) if lines else "Changes made but details unavailable."


# ---------------------------------------------------------------------------
# anchored patch: mechanical validation + in-memory application (task 1)
# ---------------------------------------------------------------------------
# The patch is the ONLY channel through which the auto-update may change the
# charter, and this validator is the resurrection of spec_gate's "the
# requirement set may not silently shrink" spirit — with explicit rewrites
# allowed. It carries the whole deletion-defence line: the admission standard
# audits content class / altitude / size but is silent on whether an existing
# clause was quietly removed, so any removal MUST be carried by a verbatim
# `old_text` quote that this validator matches against disk exactly and
# uniquely. A pure-increment (no-shrink) scheme was rejected as unworkable —
# freshness's typical job is correcting a stale statement, which increment-only
# can only refuse or append a self-contradicting sentence, and it is a monotone
# bloat machine at odds with the admission size red light.

def _resolve_edits(text: str, ops: list) -> list[tuple[int, int, str]]:
    """Resolve a validated op list into ``(start, end, replacement)`` spans.

    Only called after :func:`_validate_anchored_patch` has passed, so every
    ``anchor`` / ``old_text`` is guaranteed present-and-unique in *text*.
    ``insert_after`` yields a zero-width span (start == end) right after the
    anchor; ``replace`` yields the span covering the quoted old text.
    """
    edits: list[tuple[int, int, str]] = []
    for op in ops:
        kind = op.get("op")
        new_text = op.get("new_text", "") or ""
        if kind == "insert_after":
            anchor = op.get("anchor", "")
            pos = text.index(anchor) + len(anchor)
            edits.append((pos, pos, new_text))
        elif kind == "replace":
            old_text = op.get("old_text", "")
            start = text.index(old_text)
            edits.append((start, start + len(old_text), new_text))
    return edits


def _validate_anchored_patch(text: str, ops: Any) -> tuple[bool, str]:
    """Mechanically validate an anchored patch against the on-disk *text*.

    Returns ``(ok, reason)``. The empty patch (a fresh charter) is valid and
    produces a candidate equal to the input. A patch is rejected — with a
    human/LLM-feedable *reason* — when it:

    - is not a list, or exceeds the op / total-new-char / total-removed-char
      bounds;
    - contains an op that is not ``insert_after`` or ``replace``;
    - has an ``anchor`` / ``old_text`` that is missing, non-string, empty, or
      matches the on-disk text zero or multiple times (must be verbatim-unique);
    - has ops whose target regions overlap.

    Verbatim-unique matching is what makes an unquoted deletion impossible: the
    only text a ``replace`` may remove is text it quoted, and the quote must
    exist exactly as written on disk. Bounding the total quoted-and-removed
    length is the companion defence — it stops a replace op from quoting a huge
    unique region (or nearly the whole charter) and deleting it behind a short
    ``new_text``, which the new-char bound alone would not catch.
    """
    if not isinstance(ops, list):
        return False, "patch must be a list of operations"
    if not ops:
        return True, ""  # fresh: empty patch is valid, candidate == text
    if len(ops) > MAX_PATCH_OPS:
        return False, f"patch has too many operations ({len(ops)} > {MAX_PATCH_OPS})"

    total_new = 0
    total_old = 0
    for i, op in enumerate(ops, start=1):
        if not isinstance(op, dict):
            return False, f"operation #{i} is not an object"
        kind = op.get("op")
        new_text = op.get("new_text", "")
        if not isinstance(new_text, str):
            return False, f"operation #{i}: 'new_text' must be a string"
        total_new += len(new_text)

        if kind == "insert_after":
            anchor = op.get("anchor")
            if not isinstance(anchor, str) or not anchor:
                return False, f"operation #{i}: insert_after requires a non-empty 'anchor'"
            count = text.count(anchor)
            if count == 0:
                return False, (
                    f"operation #{i}: 'anchor' does not appear verbatim in the "
                    "on-disk charter (it must be copied exactly)"
                )
            if count > 1:
                return False, (
                    f"operation #{i}: 'anchor' matches {count} places in the "
                    "charter (it must be unique — quote more surrounding text)"
                )
        elif kind == "replace":
            old_text = op.get("old_text")
            if not isinstance(old_text, str) or not old_text:
                return False, f"operation #{i}: replace requires a non-empty 'old_text'"
            count = text.count(old_text)
            if count == 0:
                return False, (
                    f"operation #{i}: 'old_text' does not appear verbatim in the "
                    "on-disk charter (any removal must quote the exact text)"
                )
            if count > 1:
                return False, (
                    f"operation #{i}: 'old_text' matches {count} places in the "
                    "charter (it must be unique — quote more surrounding text)"
                )
            total_old += len(old_text)
        else:
            return False, (
                f"operation #{i}: unknown op {kind!r} — only 'insert_after' and "
                "'replace' are allowed"
            )

    if total_new > MAX_PATCH_NEW_CHARS:
        return False, (
            f"patch inserts too much text ({total_new} > {MAX_PATCH_NEW_CHARS} "
            "chars) — a freshness update must be surgical, not a rewrite"
        )

    if total_old > MAX_PATCH_OLD_CHARS:
        return False, (
            f"patch removes too much text ({total_old} > {MAX_PATCH_OLD_CHARS} "
            "chars quoted for replacement) — a freshness update must be surgical, "
            "not a mass deletion behind a short replacement"
        )

    # Reject overlapping edits: two ops that touch the same region make the
    # result order-dependent and are a sign of a non-surgical patch.
    edits = sorted(_resolve_edits(text, ops), key=lambda e: (e[0], e[1]))
    prev_end: Optional[int] = None
    for start, end, _ in edits:
        if prev_end is not None and start < prev_end:
            return False, "patch operations overlap the same charter region"
        prev_end = end if prev_end is None else max(prev_end, end)

    return True, ""


def _apply_patch(text: str, ops: list) -> str:
    """Apply a **validated** anchored patch to *text*, returning the candidate.

    All spans are resolved against the original *text* (so ordering of ops does
    not matter) and stitched in a single left-to-right pass. An empty patch
    returns *text* unchanged. Assumes :func:`_validate_anchored_patch` already
    passed for ``(text, ops)``.
    """
    if not ops:
        return text
    # Sort by (start, end) — the SAME key the validator's overlap check uses.
    # WHY: the validator admits a zero-width insert_after whose insertion point
    # equals the start of a same-position replace span; a start-only sort would
    # keep such ties in LLM-supplied op order, so a replace listed before its
    # preceding insert would apply first and the insert's backward cursor reset
    # would re-emit the replaced old_text. Ordering the zero-width insert before
    # the same-start replace makes the result independent of op order.
    edits = sorted(_resolve_edits(text, ops), key=lambda e: (e[0], e[1]))
    out: list[str] = []
    cursor = 0
    for start, end, replacement in edits:
        out.append(text[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


# ---------------------------------------------------------------------------
# atomic write + diff (task 4)
# ---------------------------------------------------------------------------
def _atomic_write_charter(project_root: Path, text: str) -> None:
    """Write *text* to ``se3/charter.md`` atomically (temp + ``os.replace``).

    The temp file is created in the charter's own directory so ``os.replace``
    is an atomic same-filesystem rename: any crash leaves disk holding either
    the old charter or the complete new one — never a partial write — so no
    snapshot / rollback machinery is needed.
    """
    path = charter_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".charter-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _unified_charter_diff(old: str, new: str) -> str:
    """Return a unified diff of the charter (old -> new) for the step outputs."""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile="se3/charter.md (old)",
        tofile="se3/charter.md (new)",
    )
    return "".join(diff)


# ---------------------------------------------------------------------------
# precondition (task 2)
# ---------------------------------------------------------------------------
def _invariant_check_completed(flow: FlowInstance) -> bool:
    """Return True iff this flow has a COMPLETED ``invariant_check`` step.

    The three-part syllogism behind the auto-update rests on "the change has
    already passed review", which is exactly what a COMPLETED ``invariant_check``
    certifies. The step sequence varies by task type (lightweight commit-only
    flows have no ``invariant_check`` at all), so the precondition must be
    checked explicitly rather than assumed from position.
    """
    state = getattr(flow, "state", None)
    if state is None:
        return False
    for candidate in state.steps.values():
        if (
            candidate.step_type is StepType.INVARIANT_CHECK
            and candidate.status is StepStatus.COMPLETED
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# admission monitoring trigger (unchanged — the human-amendment path)
# ---------------------------------------------------------------------------
def _run_admission_trigger(
    step: Step, project_root: Path, changed: list[str],
) -> None:
    """Run the monitoring-light altitude gate iff the diff edited the charter.

    This is the *human-amendment* path: when ``se3/charter.md`` is among the
    changed files (the implement step edited it directly), run
    :func:`check_admission` on the current charter and surface its
    monitoring-light warning. Distinct from the auto-update gate below, which is
    *gating*; this one only annotates ``step.outputs`` and never blocks.
    """
    try:
        result = admission_check_for_changes(project_root, changed)
    except Exception:
        logger.debug("charter admission check raised; ignoring", exc_info=True)
        return
    if result is None:
        return
    step.outputs["admission_checked"] = True
    step.outputs["admission_over_threshold"] = result.over_threshold
    if result.warning:
        step.outputs["admission_warning"] = result.warning
        logger.warning("charter admission monitoring light: %s", result.warning)
    else:
        logger.info(
            "charter admission check ran (charter touched): %d bytes, within "
            "the %d-byte monitoring threshold.",
            result.size_bytes, result.threshold_bytes,
        )


# ---------------------------------------------------------------------------
# gate orchestration (task 3)
# ---------------------------------------------------------------------------
def _run_gate(
    caller: LLMCaller,
    charter_text: str,
    patch: list,
    diff_summary: str,
) -> tuple[bool, Optional[str], dict, str]:
    """Run the two-part gate over the in-memory candidate.

    Order: (a) mechanical anchored-replace check, (b) admission size red light
    (gating on this path), (c) admission LLM gate (with the removal-weakening
    question). Returns ``(ok, candidate, verdicts, reason)`` where *candidate*
    is the applied-in-memory text (``None`` when the mechanical check itself
    failed, so no candidate exists), *verdicts* records each sub-check for the
    step outputs, and *reason* is the feedable failure explanation ("" on pass).
    """
    verdicts: dict[str, Any] = {}

    # (a) mechanical anchored-replace check — a program, not an LLM.
    ok, reason = _validate_anchored_patch(charter_text, patch)
    verdicts["mechanical_ok"] = ok
    verdicts["mechanical_reason"] = reason
    if not ok:
        return False, None, verdicts, f"mechanical anchored-patch check failed: {reason}"

    candidate = _apply_patch(charter_text, patch)

    # (b) size red light — a *monitoring* light elsewhere, but GATING here.
    adm = check_admission(candidate)
    verdicts["size_over_threshold"] = adm.over_threshold
    if adm.over_threshold:
        return (
            False, candidate, verdicts,
            f"admission size red light (gating on this path): {adm.warning}",
        )

    # (c) admission LLM gate — content class / altitude + removal-weakening.
    replaced = [
        op.get("old_text", "")
        for op in patch
        if isinstance(op, dict) and op.get("op") == "replace"
    ]
    gate_prompt = build_admission_gate_prompt(
        candidate_text=candidate,
        replaced_texts=replaced,
        diff_summary=diff_summary,
    )
    try:
        response = caller.call(
            prompt=gate_prompt,
            json_mode="two_phase",
            json_schema_hint='{"admitted": true, "violations": [], "weakened_removals": []}',
            required_keys=["admitted"],
        )
        gate_result = parse_json_response(response, required_keys=["admitted"])
    except Exception as e:
        verdicts["llm_error"] = str(e)
        return False, candidate, verdicts, f"admission gate LLM call failed: {e}"

    if not gate_result:
        verdicts["llm_admitted"] = None
        return False, candidate, verdicts, "admission gate LLM response was unparsable"

    # A gate is a proof of passage: only a real boolean ``admitted: true`` and
    # real list findings count. Coercing string-typed ("false", "drops a rule")
    # fields would let a malformed response masquerade as a clean pass, so any
    # type deviation fails the gate (fail-closed) rather than being smoothed over.
    admitted_raw = gate_result.get("admitted")
    violations = gate_result.get("violations", [])
    weakened = gate_result.get("weakened_removals", [])
    malformed: list[str] = []
    if not isinstance(admitted_raw, bool):
        malformed.append(f"admitted={admitted_raw!r} (expected bool)")
    if not isinstance(violations, list):
        malformed.append(f"violations={violations!r} (expected list)")
    if not isinstance(weakened, list):
        malformed.append(f"weakened_removals={weakened!r} (expected list)")
    if malformed:
        verdicts["llm_malformed"] = malformed
        return (
            False, candidate, verdicts,
            "admission gate response was malformed (fail-closed): "
            + "; ".join(malformed),
        )

    admitted = admitted_raw
    verdicts["llm_admitted"] = admitted
    verdicts["llm_violations"] = violations
    verdicts["llm_weakened_removals"] = weakened

    if not admitted:
        return (
            False, candidate, verdicts,
            f"admission gate rejected the candidate: {violations or '(no detail)'}",
        )
    if weakened:
        return (
            False, candidate, verdicts,
            f"admission gate found removals weakening unrelated conventions: {weakened}",
        )
    return True, candidate, verdicts, ""


# ---------------------------------------------------------------------------
# output recording
# ---------------------------------------------------------------------------
def _record_advisory(step: Step, propose: Optional[dict]) -> None:
    """Record the advisory freshness fields (present on every non-cheap path)."""
    if propose:
        touched = propose.get("touched_classes") or []
        step.outputs["charter_update_needed"] = bool(propose.get("charter_update_needed"))
        step.outputs["touched_classes"] = touched if isinstance(touched, list) else []
        step.outputs["reason"] = str(propose.get("reason", "") or "")
        step.outputs["suggested_update"] = str(propose.get("suggested_update", "") or "")
    else:
        step.outputs["charter_update_needed"] = False
        step.outputs["touched_classes"] = []
        step.outputs["reason"] = ""
        step.outputs["suggested_update"] = ""


def charter_freshness_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the charter_freshness step.

    Always returns COMPLETED (non-blocking). Empty diff -> cheap pass (no LLM
    call). Otherwise the propose -> gate -> apply closed loop runs; every failure
    mode (no precondition, gate rejection, LLM/write error) degrades to today's
    advisory behavior with the charter left byte-for-byte unchanged.
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    changes_made = step.inputs.get("changes_made") or {}
    if not isinstance(changes_made, dict):
        changes_made = {}
    changed = _changed_files(changes_made)

    # The human-amendment monitoring trigger is independent of the freshness
    # loop and must fire even on the (rare) charter-only diff.
    _run_admission_trigger(step, project_root, changed)

    # Cheap pass: no diff -> the charter cannot have been touched.
    if not changed:
        logger.info("charter_freshness: no diff — passing (cheap, no LLM call).")
        step.outputs["charter_update_needed"] = False
        step.outputs["touched_classes"] = []
        step.outputs["reason"] = "No changes in this flow; charter unaffected."
        step.outputs["suggested_update"] = ""
        step.outputs["charter_auto_updated"] = False
        step.outputs["skipped_reason"] = "no_diff"
        return StepStatus.COMPLETED

    task_description = (
        step.inputs.get("task_description") or flow.task_description or ""
    )
    # Base = the ON-DISK charter, NOT the frozen invariant anchor
    # (step.inputs['charter']): this task may itself be a legitimate amendment
    # that already edited se3/charter.md, and anchoring the patch on the frozen
    # text would clobber that edit. The frozen version is only for the invariant
    # judgement, which is already over.
    charter_text = load_charter(project_root)
    charter_for_prompt = charter_text if charter_text.strip() else "_(no charter on disk)_"

    precondition_ok = _invariant_check_completed(flow)
    diff_summary = _format_changes(changes_made)

    logger.info("Checking whether the diff touches the charter's content classes...")

    retry_count = step.inputs.get("retry_count", 0)
    # force_read_only: the handler's Python does the writing; the LLM sub-calls
    # only PROPOSE / judge text and must stay read-only (registry read_only is
    # False for this step, so the lock has to be re-applied per call).
    caller = LLMCaller(
        project_root,
        flow_id=flow.flow_id,
        step_id=step.step_id,
        step_type=step.step_type.value,
        external_attempt=retry_count,
        fix_iteration=step.inputs.get("fix_iteration", 0),
        force_read_only=True,
    )

    propose: Optional[dict] = None
    gate_verdicts: dict[str, Any] = {}
    degraded_reason: Optional[str] = None
    applied = False
    charter_diff = ""
    feedback = "_(none)_"

    for attempt in range(MAX_GATE_ATTEMPTS):
        prompt = CHARTER_FRESHNESS_PROMPT.format(
            admission_standard=CHARTER_ADMISSION_STANDARD,
            task_description=task_description or "_(none)_",
            changes_made=diff_summary,
            charter=charter_for_prompt,
            feedback=feedback,
        )
        try:
            response = caller.call(
                prompt=prompt,
                json_mode="two_phase",
                json_schema_hint=(
                    '{"charter_update_needed": false, "touched_classes": [], '
                    '"reason": "...", "suggested_update": "", "patch": []}'
                ),
                required_keys=["charter_update_needed"],
            )
            propose = parse_json_response(response, required_keys=["charter_update_needed"])
        except Exception as e:
            # Advisory step: an LLM failure must NOT block the flow.
            logger.warning("charter_freshness propose LLM call failed (non-blocking): %s", e)
            _record_advisory(step, None)
            step.outputs["reason"] = f"charter_freshness skipped: LLM call failed ({e})."
            step.outputs["charter_auto_updated"] = False
            step.outputs["skipped_reason"] = "llm_error"
            return StepStatus.COMPLETED

        if not propose:
            logger.warning("charter_freshness: unparsable propose response (non-blocking).")
            _record_advisory(step, None)
            step.outputs["reason"] = "charter_freshness skipped: unparsable LLM response."
            step.outputs["charter_auto_updated"] = False
            step.outputs["skipped_reason"] = "parse_error"
            return StepStatus.COMPLETED

        update_needed = bool(propose.get("charter_update_needed"))
        patch = propose.get("patch") or []
        if not isinstance(patch, list):
            patch = []

        # Fresh (or no concrete patch): advisory only, nothing to write.
        if not update_needed or not patch:
            break

        # Precondition: the closed loop runs only after a COMPLETED
        # invariant_check certified the code diff. Otherwise keep today's
        # advisory behavior (surface suggested_update, write nothing).
        if not precondition_ok:
            degraded_reason = "invariant_check_not_completed"
            logger.info(
                "charter_freshness: update suggested but no COMPLETED "
                "invariant_check in this flow — staying advisory, not writing."
            )
            break

        ok, candidate, verdicts, reason = _run_gate(
            caller, charter_text, patch, diff_summary,
        )
        gate_verdicts = verdicts
        if ok and candidate is not None:
            try:
                _atomic_write_charter(project_root, candidate)
            except Exception as e:
                degraded_reason = f"charter write failed: {e}"
                logger.warning("charter_freshness: atomic write failed (non-blocking): %s", e)
                break
            applied = True
            charter_diff = _unified_charter_diff(charter_text, candidate)
            logger.warning(
                "charter_freshness: auto-updated se3/charter.md (descriptive, gated). "
                "touched=%s", propose.get("touched_classes"),
            )
            break

        # Gate failed — feed the structured reason back and retry once (bounded).
        degraded_reason = reason
        feedback = (
            "The previous patch was REJECTED by the gate for this reason:\n"
            f"{reason}\n"
            "Produce a corrected minimal anchored patch, or set "
            "charter_update_needed=false with an empty patch if no safe "
            "descriptive update is possible."
        )
        logger.info("charter_freshness: gate rejected attempt %d: %s", attempt + 1, reason)

    # Final output recording (advisory fields present on every path here).
    _record_advisory(step, propose)
    step.outputs["charter_auto_updated"] = applied
    if applied:
        step.outputs["charter_diff"] = charter_diff
    if gate_verdicts:
        step.outputs["gate_verdicts"] = gate_verdicts
    if degraded_reason and not applied:
        step.outputs["degraded_reason"] = degraded_reason

    if not applied and step.outputs.get("charter_update_needed"):
        logger.warning(
            "charter_freshness: diff appears to touch the charter but no "
            "auto-update was applied (%s). Suggested update kept as advisory.",
            degraded_reason or "fresh / no patch",
        )
    elif not applied:
        logger.info("charter_freshness: diff does not touch the charter — passing.")

    return StepStatus.COMPLETED
