"""Investigate step handler — net-zero-diff root-cause investigation.

INVESTIGATE exists for the case a bugfix flow cannot plan its way out of: the
symptom is known but the *cause* is not. Reading alone is often not enough to
find it, so the step is explicitly allowed to experiment — temporary logging, a
probe patch, a scratch script — and is bounded by a different contract instead:

    net-zero diff, not read-only.

Everything the step touched must be back the way it was before the step ends.
The handler snapshots the workspace before and after the LLM call and compares
the two (see :mod:`..workspace_snapshot`). On a mismatch it hands the delta back
to the LLM once with an explicit "revert your experimental changes" instruction
and re-checks; still mismatched means the step FAILS.

INVARIANT: the engine never restores the workspace itself. A flow's working tree
routinely carries uncommitted work that predates this step (fix iterations
especially), so an automatic ``git reset``/``checkout``/``stash`` here would
destroy real work irreversibly. The engine only verifies and instructs.

The step never commits and never applies a fix: its whole product is a
structured root-cause report in ``step.outputs``, consumed by the later
PLAN / IMPLEMENT steps as a dedicated prompt section. It is deliberately NOT
written to any project file, and deliberately NOT merged into the task's
intent chain (that would pollute self_check's verbatim-quote source pool with
speculative text).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from pathlib import Path

from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..prompt_markers import inject_boundary
from ..utils.json_parser import parse_json_response
from ..workspace_snapshot import (
    WorkspaceSnapshot,
    compare_snapshots,
    snapshot_workspace,
)
from ._project_root import resolve_flow_project_root

logger = logging.getLogger(__name__)

# Where the pre-step workspace baseline is parked between attempts of the SAME
# step. It lives in ``step.inputs`` (persisted with the flow, not part of the
# step's report) rather than in a local variable, so any re-entry into this step
# still compares against the tree as it was before the investigation ever
# started. See :func:`ensure_workspace_baseline` for who captures it and when.
BASELINE_INPUT_KEY = "workspace_baseline"


INVESTIGATE_SCHEMA_HINT = (
    '{"root_cause": "the mechanism that produces the symptom", '
    '"evidence": ["concrete observation supporting the conclusion"], '
    '"files_involved": ["src/foo.py"], '
    '"suggested_fix_direction": "what a fix would have to change, and why", '
    '"confidence": "high|medium|low", '
    '"conclusive": true}'
)


INVESTIGATE_PROMPT = """You are a root-cause investigator. The problem below has a known symptom but an UNKNOWN cause. Your job is to find the cause — not to fix it.

## Task Description
{task_description}

## Scope (as classified upstream)
{scope}

## Investigation round
This is round {iteration} of at most {max_iterations}.
{previous_reports}

## How to investigate
Read the code, run commands, form hypotheses and test them until you can name the concrete mechanism that produces the symptom. Before reading source, consult the injected code-index map to locate the relevant modules / symbols; pull deeper detail on demand via ``luo code-index show <path>`` and search items with ``luo code-index search <pattern>`` rather than reading whole files blindly.

You MAY make experimental changes as investigation instruments: temporary logging / print statements, a throwaway probe patch to test a hypothesis, a scratch script, an added debug test. Running the test suite, git read commands (`git log`, `git diff`, `git show`, `git blame`), and any other read-only exploration are all fair game.

## HARD CONSTRAINTS — the step fails if you break them

1. **NET-ZERO DIFF.** Every experimental change you make MUST be reverted before you finish. When the step ends the working tree must be byte-for-byte what it was when the step started — no leftover logging, no probe patch, no scratch file, no new untracked file. Track what you touch so you can undo it precisely. The engine compares a snapshot of the workspace taken before this step against one taken after, and FAILS the step when they differ. The engine will NOT clean up for you: the working tree may hold unrelated uncommitted work, so it never resets or checks out anything on its own. Reverting is entirely your responsibility, and it must be surgical — do NOT run `git reset --hard`, `git checkout -- .`, `git stash`, or `git clean`, which would destroy that unrelated work.

2. **NO GIT COMMIT.** Do not run `git commit` (nor `luo commit`, nor any command that creates a commit, tag, or stash) under any circumstance. This step produces no commits.

3. **DO NOT FIX THE PROBLEM.** Even when the fix is obvious, do not implement it here. The fix is always carried out by the later PLAN -> IMPLEMENT steps, which will receive your report. Leaving a "small harmless fix" behind is a net-zero-diff violation and fails the step.

4. **THE REPORT IS THE ONLY DELIVERABLE.** Write your findings into the JSON response below — do NOT write them into any project file (no notes file, no markdown report, no code comment). Anything written to disk is a leftover change.

## What to report
- `root_cause`: the concrete mechanism producing the symptom — which code, under which condition, does what wrong. "X is broken" is not a root cause; "X passes an unnormalized path to Y, which only handles absolute paths, so Z silently no-ops" is.
- `evidence`: the concrete observations that support it (file:line references, command output you actually saw, an experiment result). Do not list guesses here.
- `files_involved`: the files the cause actually lives in or flows through.
- `suggested_fix_direction`: what a fix would have to change and why — a direction for the planner, not a patch.
- `confidence`: "high" / "medium" / "low".
- `conclusive`: `true` ONLY when you have identified the actual mechanism with supporting evidence. Set it to `false` when you are still hypothesising, when the evidence is compatible with several causes, or when you ran out of things to try this round — another investigation round will then be scheduled. Do NOT claim conclusive to end the loop early; an honest `false` with your best current hypothesis is far more useful.

Respond in JSON format:
```json
{{
    "root_cause": "...",
    "evidence": ["..."],
    "files_involved": ["src/foo.py"],
    "suggested_fix_direction": "...",
    "confidence": "high|medium|low",
    "conclusive": true
}}
```
"""

# Two-segment marker only: the USER_CONTENT region is empty here — like
# ``analyze``, this step's ``task_description`` is composed framework text (the
# effective description), not a user literal, so the web console renders the
# whole post-BEGIN tail inside the collapsed system-prompt chip.
INVESTIGATE_PROMPT = inject_boundary(INVESTIGATE_PROMPT, "## Task Description\n")


REVERT_PROMPT = """Your investigation left the working tree DIFFERENT from how it was when the step started. The step's net-zero-diff contract requires you to revert every experimental change you made.

Detected difference (paths are relative to the project root, and are exactly the files that changed while this step ran — nothing else needs touching):
{delta}

Revert exactly those changes now — remove the temporary logging / probe patches you added, delete the scratch files you created, and restore anything you edited to its previous content. Work through the listed paths one by one; `git diff HEAD -- <path>` shows a tracked file's current state, but remember that not every hunk in it is necessarily yours.

Be SURGICAL. The working tree may contain unrelated uncommitted work that predates this step and MUST survive: do NOT run `git reset --hard`, `git checkout -- .`, `git stash`, or `git clean`. Undo only what you yourself changed.

If the difference above says HEAD moved, you created a commit — which this step must never do. Move the branch back to the commit named as the "from" id WITHOUT discarding file contents (a soft reset), then revert the experimental changes it contained as described above. Never discard work that was not yours.

Do not re-investigate and do not fix the underlying problem — only restore the workspace. When you are done, reply with a one-line confirmation of what you reverted.
"""


def _format_previous_reports(reports: Any) -> str:
    """Render earlier rounds' reports for the prompt of a repeat investigation.

    Returns an empty string on the first round so the prompt carries no dangling
    "previous findings" section.
    """
    if not isinstance(reports, list) or not reports:
        return ""
    lines: List[str] = ["", "### Findings from previous rounds (not conclusive)"]
    for i, report in enumerate(reports, 1):
        if isinstance(report, dict):
            lines.append(f"- Round {i}:")
            hypothesis = report.get("root_cause", "")
            if hypothesis:
                lines.append(f"  - hypothesis: {hypothesis}")
            evidence = report.get("evidence")
            if isinstance(evidence, list) and evidence:
                lines.append(f"  - evidence: {'; '.join(str(e) for e in evidence)}")
            confidence = report.get("confidence", "")
            if confidence:
                lines.append(f"  - confidence: {confidence}")
        elif report:
            lines.append(f"- Round {i}: {report}")
    lines.append("")
    lines.append(
        "Do NOT repeat the experiments above verbatim — take the investigation "
        "further, or rule those hypotheses out with new evidence."
    )
    return "\n".join(lines)


def _coerce_str_list(value: Any) -> List[str]:
    """Normalize a JSON field that should be a list of strings.

    A string is wrapped (LLMs sometimes emit a single item unwrapped); anything
    else degrades to an empty list rather than raising.
    """
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return []


def _coerce_conclusive(value: Any) -> bool:
    """Interpret the ``conclusive`` field, defaulting to False.

    WHY default False: an unparsable / missing verdict must schedule another
    round rather than silently promote a half-formed hypothesis to "the answer".
    The loop is separately bounded by ``investigation.max_iterations``, so
    erring toward "not conclusive" cannot run away.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "conclusive")
    return False


def _build_report(result: Dict[str, Any]) -> Dict[str, Any]:
    """Project the parsed LLM JSON into the canonical report shape."""
    return {
        "root_cause": str(result.get("root_cause", "") or ""),
        "evidence": _coerce_str_list(result.get("evidence")),
        "files_involved": _coerce_str_list(result.get("files_involved")),
        "suggested_fix_direction": str(
            result.get("suggested_fix_direction", "") or ""
        ),
        "confidence": str(result.get("confidence", "low") or "low"),
        "conclusive": _coerce_conclusive(result.get("conclusive")),
    }


def ensure_workspace_baseline(step: Step, project_root: Path) -> WorkspaceSnapshot:
    """Capture the step's net-zero-diff baseline into ``step.inputs`` if absent.

    Idempotent: an already-stored baseline is returned untouched, so every
    re-entry into the same step keeps comparing against the tree as it was before
    the investigation ever started.

    WHY the baseline is persisted rather than re-taken per attempt: the step can
    be re-entered with its own unreverted experimental changes still on disk —
    via run.py's Retry/Skip/Abort gate after a failed net-zero-diff check, or via
    ``luo run --resume`` after the process died mid-step. Re-baselining there
    would swallow the leftovers: the next attempt would end byte-identical to its
    own dirty start, report COMPLETED, and let the probe patch ride on into
    PLAN/IMPLEMENT and get committed.

    WHY the state machine calls this *before* marking the step RUNNING (see
    ``StateMachine.run_step``) instead of leaving it to the handler: the flow is
    only persisted at that transition and again after the handler returns, so a
    baseline first written inside the handler exists solely in memory for the
    whole investigation call — by far the longest part of the step. A hard kill
    (SIGKILL / OOM / power loss) during it would lose the baseline entirely, and
    the resumed round would re-baseline onto the interrupted round's leftovers.
    Capturing it on the persisted side of that save closes the window; the
    handler still calls this as a fallback for the paths that invoke it directly.
    """
    stored = WorkspaceSnapshot.from_dict(step.inputs.get(BASELINE_INPUT_KEY))
    if stored is not None:
        return stored

    # Taken BEFORE the LLM runs so a working tree that was already dirty is part
    # of the baseline and cannot be blamed on this step.
    before = snapshot_workspace(project_root)
    # An unavailable capture is deliberately NOT persisted: it would pin every
    # later attempt to an undecidable comparison even once git is reachable
    # again. The current attempt still degrades to undecidable, which the
    # net-zero-diff check reports rather than failing on.
    if before.available:
        step.inputs[BASELINE_INPUT_KEY] = before.to_dict()
    return before


def _resolve_baseline(step: Step, project_root: Path) -> WorkspaceSnapshot:
    """Return the pre-step workspace baseline for this attempt."""
    if WorkspaceSnapshot.from_dict(step.inputs.get(BASELINE_INPUT_KEY)) is not None:
        logger.info(
            "investigate: reusing the workspace baseline captured before the "
            "first attempt of this step (a re-entry must not re-baseline on a "
            "workspace still holding unreverted experimental changes)."
        )
    return ensure_workspace_baseline(step, project_root)


def investigate_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute one round of root-cause investigation.

    Returns COMPLETED with the round's report in ``step.outputs`` (the state
    machine decides from ``conclusive`` + the iteration count whether to schedule
    another round — this handler never expresses looping itself), or FAILED when
    the LLM's output is unparsable or the net-zero-diff contract was broken.
    """
    task_description = (
        step.inputs.get("task_description")
        or flow.task_description
        or ""
    )
    if not task_description:
        step.error_message = "No task description provided"
        return StepStatus.FAILED

    project_root = resolve_flow_project_root(flow)

    iteration = step.inputs.get("investigation_iteration", 1)
    if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 1:
        iteration = 1
    max_iterations = step.inputs.get("investigation_max_iterations", 0)
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool):
        max_iterations = 0
    max_display = str(max_iterations) if max_iterations > 0 else "unlimited"

    prompt = INVESTIGATE_PROMPT.format(
        task_description=task_description,
        scope=step.inputs.get("scope") or "_(not specified)_",
        iteration=iteration,
        max_iterations=max_display,
        previous_reports=_format_previous_reports(
            step.inputs.get("previous_investigation_reports")
        ),
    )

    from ..context_builder import (
        ensure_code_index_fresh,
        get_charter_injection,
        get_code_index_injection,
        get_issue_discovery_injection,
        get_runtime_environment_injection,
    )

    injection = get_issue_discovery_injection("investigate", project_root)
    if injection:
        prompt += injection
    prompt += get_charter_injection(project_root)
    # Read-side step: the map is refreshed by analyze earlier in the flow, so no
    # rebuild is forced here — investigate changes nothing, so it cannot stale it.
    prompt += get_code_index_injection(project_root)
    runtime_env = get_runtime_environment_injection("investigate", project_root)
    if runtime_env:
        prompt += runtime_env

    logger.info(
        "Investigating root cause (round %d/%s): %s...",
        iteration, max_display, task_description[:60],
    )

    # A retry after a net-zero-diff failure re-enters this handler with the
    # previous attempt's outputs still in place (the failure gate only resets the
    # status and bumps retry_count). Those markers describe an attempt that is
    # over, so drop them here: whatever this attempt's own comparison finds is
    # re-recorded below, and a round that comes back clean must not inherit the
    # earlier round's "workspace not restored" banner.
    for stale_key in ("workspace_delta", "workspace_check"):
        step.outputs.pop(stale_key, None)

    before = _resolve_baseline(step, project_root)

    try:
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value,
            external_attempt=retry_count,
            fix_iteration=step.inputs.get("fix_iteration", 0),
        )
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint=INVESTIGATE_SCHEMA_HINT,
            required_keys=["root_cause"],
        )

        result = parse_json_response(response, required_keys=["root_cause"])
        if not result:
            step.error_message = (
                "Failed to parse the root-cause report from the LLM response "
                "(expected JSON with a 'root_cause' key)"
            )
            return StepStatus.FAILED

        report = _build_report(result)

        # Net-zero-diff verification. One revert round-trip is granted before the
        # step is failed: the LLM made the changes, so it is the only party that
        # can undo them precisely (the engine must not touch the tree).
        delta = compare_snapshots(before, snapshot_workspace(project_root))
        if not delta.is_clean:
            logger.warning(
                "investigate: workspace not restored — asking the LLM to revert.\n%s",
                delta.describe(),
            )
            _request_revert(caller, delta.describe())
            delta = compare_snapshots(before, snapshot_workspace(project_root))
            if not delta.is_clean:
                step.outputs["workspace_delta"] = delta.describe()
                step.error_message = (
                    "Investigation violated the net-zero-diff contract: the "
                    "workspace was not restored even after an explicit revert "
                    "instruction. The engine does not revert anything itself "
                    "(the working tree may carry unrelated uncommitted work) — "
                    "restore it manually before resuming.\n"
                    + delta.describe()
                )
                logger.error(
                    "investigate FAILED: workspace still modified after revert "
                    "instruction.\n%s", delta.describe(),
                )
                return StepStatus.FAILED
            logger.info("investigate: workspace restored after revert instruction.")

        if delta.undecidable:
            # git unavailable: record why the guard could not run rather than
            # silently implying a verified-clean workspace.
            step.outputs["workspace_check"] = delta.describe()

        step.outputs["root_cause"] = report["root_cause"]
        step.outputs["evidence"] = report["evidence"]
        step.outputs["files_involved"] = report["files_involved"]
        step.outputs["suggested_fix_direction"] = report["suggested_fix_direction"]
        step.outputs["confidence"] = report["confidence"]
        step.outputs["conclusive"] = report["conclusive"]
        step.outputs["investigation_iteration"] = iteration
        # Whole report under one key so downstream context injection reads a
        # single structured object instead of re-assembling the loose fields.
        step.outputs["root_cause_report"] = report

        # The contract held, so the baseline has done its job; drop it rather
        # than carrying a per-file hash table around in the persisted flow.
        step.inputs.pop(BASELINE_INPUT_KEY, None)

        logger.info(
            "Investigation round %d complete: conclusive=%s, confidence=%s",
            iteration, report["conclusive"], report["confidence"],
        )
        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Investigate step failed")
        step.error_message = f"Investigation failed: {e}"
        return StepStatus.FAILED


def _request_revert(caller: LLMCaller, delta_description: str) -> None:
    """Ask the LLM to undo its experimental changes (best effort).

    Failure here is not itself fatal: the caller re-snapshots afterwards and
    that comparison — not this call's outcome — decides the step's status.
    """
    try:
        caller.call(prompt=REVERT_PROMPT.format(delta=delta_description), json_mode="off")
    except Exception:
        logger.info("investigate: revert instruction call failed", exc_info=True)
