"""Invariant Check step handler.

Replaces the retired ``spec_gate`` / ``spec_check`` per-requirement drift
machinery. INVARIANT_CHECK is an **anchored** check: it asks the LLM whether
the diff violates any invariant that has been *explicitly recorded*, where the
closed anchor set is::

    {task_description, charter full text, why-comments of the touched code}

frozen at flow start. It reuses the verbatim_quote anchoring that ``self_check``
adopted after its free-text schema let ungrounded "nits" spin the fix loop: an
issue survives validation only when its ``expectation_source.verbatim_quote`` is
a literal substring of the anchor pool. This makes the coverage face honest —
**only invariants that were written down (as a charter rule or a why-comment, or
that the task itself demands) are machine-guarded**; unwritten expectations are
deliberately not. We explicitly reject an anchor-less self-check: when there are
no anchors (no charter, no why-comments, no task text), the step cheap-passes
rather than inventing invariants to enforce.

Routing: a surviving violation returns ``REVISION_NEEDED`` (the state machine
routes it into the existing fix loop, same as TEST / SELF_CHECK / VERIFY_SPEC).
No diff, or no anchors, returns ``COMPLETED`` (cheap pass) — this is what makes
the ``review`` flow (``analyze -> invariant_check -> summarize``) pass for free
when it changed nothing.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from ..charter import load_charter
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..prompt_markers import inject_boundary
from ..utils.json_parser import parse_json_response
from ...config import DEFAULT_MAX_FIX_ITERATIONS
from ._fix_context import format_fix_iteration_display
# Reuse self_check's anchoring machinery verbatim — single source of truth so
# the two anchored checks cannot drift in their normalization / validation.
from .self_check import (
    _changed_paths,
    _describe_issue,
    _normalize_for_quote_match,
    _validate_and_filter_issues,
)

logger = logging.getLogger(__name__)


# Comment prefixes we treat as carrying potential "why" intent when harvesting
# the touched files' colocated comments into the anchor pool. Best-effort and
# language-agnostic: we keep the comment text (after the marker) so a
# verbatim_quote can substring-match the human-written rationale.
_COMMENT_MARKERS = ("#", "//")


def _extract_comments(text: str) -> list[str]:
    """Return the text of single-line comments (``#`` / ``//``) in ``text``.

    Best-effort textual harvest (not an AST pass): keeps the body after the
    comment marker, dropping empty markers.
    """
    comments: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        for marker in _COMMENT_MARKERS:
            if stripped.startswith(marker):
                body = stripped[len(marker):].strip()
                if body:
                    comments.append(body)
                break
    return comments


def _read_baseline_file(
    project_root: Path, baseline_commit: str | None, rel: str
) -> str | None:
    """Read ``rel`` as it existed at the flow's frozen baseline commit.

    Uses ``git show <baseline>:<rel>`` so the ORIGINAL pre-implementation text
    is recovered even after the working tree edited or deleted the file.
    Returns ``None`` (silently) when there is no baseline, the file did not
    exist at baseline, or git is unavailable.
    """
    if not baseline_commit:
        return None
    try:
        result = subprocess.run(
            ["git", "show", f"{baseline_commit}:{rel}"],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _harvest_why_comments(
    project_root: Path,
    changed_files: set[str],
    baseline_commit: str | None = None,
) -> list[str]:
    """Collect colocated comment lines from the touched code files.

    The new knowledge system puts *why / intent* into colocated comments, so the
    why-comments of the changed code are part of the anchor set. Crucially, the
    harvest reads BOTH the file's **baseline** content (as frozen at flow start,
    before ``implement`` could touch it) AND its current working-tree content,
    merging the two comment sets per file. This closes the gap where an
    implementation that deletes or rewrites a why-comment documenting a binding
    invariant — while violating that invariant — would otherwise erase the
    original quote from the anchor pool and let the violation slip through: the
    baseline copy preserves the original quote regardless of what the diff did,
    and the working-tree copy still surfaces any newly added intent comments.

    Unreadable or binary files are skipped silently. Returns one string per file
    (its merged comment block joined by newlines) so ``_build_source_pool`` can
    treat each as a pool entry.
    """
    out: list[str] = []
    for rel in sorted(changed_files):
        if not isinstance(rel, str) or not rel:
            continue
        comments: list[str] = []
        seen: set[str] = set()

        def _add(text: str) -> None:
            for body in _extract_comments(text):
                if body not in seen:
                    seen.add(body)
                    comments.append(body)

        # Baseline (pre-implementation) content first — preserves the ORIGINAL
        # why-comment anchor text even if the diff edited or deleted it.
        baseline_text = _read_baseline_file(project_root, baseline_commit, rel)
        if baseline_text is not None:
            _add(baseline_text)

        # Working-tree content — surfaces newly added intent comments.
        path = project_root / rel
        try:
            if path.is_file():
                _add(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            pass

        if comments:
            out.append("\n".join(comments))
    return out


def _build_anchor_inputs(step: Step, flow: FlowInstance, project_root: Path) -> dict:
    """Assemble the synthetic step_inputs that drives self_check's validators.

    We reuse ``self_check._validate_and_filter_issues`` (and the
    ``_build_source_pool`` it calls) unchanged. Those read:

    - ``task_description_base`` — the clean task text (anchor #1)
    - ``spec_content`` (non-``base`` entries) — here we inject the charter full
      text (anchor #2) and the harvested why-comments (anchor #3) under
      descriptive keys, so they enter the verbatim_quote source pool.
    - ``changes_made.files_changed`` — the touched paths, used to validate each
      issue's ``evidence_lines``.

    The anchor sources are read from ``step.inputs`` first (the state machine
    freezes them at flow start); the charter falls back to an on-disk read so
    the handler is self-sufficient when those inputs are absent.
    """
    task_description = (
        step.inputs.get("task_description")
        or flow.task_description
        or ""
    )

    charter_text = step.inputs.get("charter")
    if not (isinstance(charter_text, str) and charter_text):
        charter_text = load_charter(project_root)

    changes_made = step.inputs.get("changes_made") or {}
    if not isinstance(changes_made, dict):
        changes_made = {}
    changed = _changed_paths({"changes_made": changes_made})

    # why-comments: prefer an explicit frozen list from inputs, else harvest.
    # The harvest reads the baseline (pre-implementation) copy of each touched
    # file as well as the working tree, so the original why-comment anchor text
    # survives even when the diff deleted or rewrote it.
    why_comments = step.inputs.get("why_comments")
    if isinstance(why_comments, list):
        why_comments = [w for w in why_comments if isinstance(w, str) and w.strip()]
    else:
        baseline_commit = getattr(flow, "baseline_commit", None)
        why_comments = _harvest_why_comments(project_root, changed, baseline_commit)

    spec_content: dict[str, str] = {}
    if isinstance(charter_text, str) and charter_text.strip():
        spec_content["charter"] = charter_text
    if why_comments:
        spec_content["why_comments"] = "\n\n".join(why_comments)

    return {
        "task_description_base": task_description,
        "spec_content": spec_content,
        "changes_made": changes_made,
    }


def _anchor_pool_is_empty(anchor_inputs: dict) -> bool:
    """True when the frozen anchor set yields no usable text to anchor against.

    Mirrors the substring-pool construction of ``_build_source_pool`` +
    ``_normalize_for_quote_match``: if every anchor normalizes to empty, there
    is nothing recorded to guard, so we must NOT run an anchor-less check.
    """
    from .self_check import _build_source_pool

    pool = [_normalize_for_quote_match(s) for s in _build_source_pool(anchor_inputs)]
    return not any(p for p in pool)


INVARIANT_CHECK_PROMPT = """You are an invariant auditor. Your ONLY job is to decide whether the diff below VIOLATES an invariant that has been **explicitly written down** in the anchored material.

## Task Description
{task_description}

## Charter (project-wide binding conventions — anchor)
{charter}

## Why-comments of the touched code (intent recorded next to the code — anchor)
{why_comments}

## Changes Made (the diff under audit)
{changes_made}

## What an invariant violation is
A *binding invariant* is a rule that the Task Description, the Charter, or a touched-code why-comment states EXPLICITLY. A violation is the diff doing something that contradicts such a recorded rule.

## HARD scope limit (this is the whole point of this check)
You may ONLY report a violation when you can quote the recorded rule **verbatim** from the anchored material above. If a concern is real but is not written down anywhere above, it is OUT OF SCOPE here — do NOT report it. Unwritten expectations are intentionally not machine-guarded. This is NOT a free code review: do NOT report style, missing tests, performance, or "nice to have" concerns, and do NOT report anything a downstream specialized step owns (e.g. version bumping is decided by version_analyze).

## Issue Schema (HARD requirements — handler validates and drops violators)
Each issue MUST be a JSON object with:
- `severity`: one of "critical" / "high" / "medium" / "low"
- `actual_behavior`: what the diff does that violates the invariant (concrete, observable; non-empty)
- `expected_behavior`: what the recorded invariant requires instead (non-empty)
- `divergence`: the concrete input / sequence / state under which the diff breaks the invariant (non-empty)
- `expectation_source`: where the invariant is recorded. Must be:
    {{ "type": "task_description" | "charter" | "why_comment",
       "verbatim_quote": "<a literal substring copied from the Task Description, Charter, or a why-comment above>" }}
  The handler normalizes both the quote and the anchor pool and DROPS any issue whose quote is not a literal substring of the anchored material. Quote the substantive rule, not a generic word.
- `evidence_lines`: array of `"path:N"` strings whose `path` appears in the Changes Made files. At least one entry required UNLESS `missing_in` is non-empty.
- `missing_in`: array of file paths that the invariant required to be edited but were not.
- `out_of_scope`: boolean. Set `true` for any concern that is not a recorded-invariant violation — the handler discards these.

Respond in JSON format:
```json
{{
    "issues": [
        {{
            "severity": "critical|high|medium|low",
            "actual_behavior": "...",
            "expected_behavior": "...",
            "divergence": "...",
            "expectation_source": {{
                "type": "charter",
                "verbatim_quote": "..."
            }},
            "evidence_lines": ["src/foo.py:42"],
            "missing_in": [],
            "out_of_scope": false
        }}
    ],
    "summary": "Brief statement of whether any recorded invariant is violated"
}}
```

If the diff violates no recorded invariant, return an empty issues array.
"""

# Two-segment marker only: USER_CONTENT region is empty (no user literal here).
INVARIANT_CHECK_PROMPT = inject_boundary(INVARIANT_CHECK_PROMPT, "## Task Description\n")


def _has_changed_files(changes_made: Any) -> bool:
    """True iff ``changes_made`` reports at least one changed file/path."""
    if not isinstance(changes_made, dict):
        return False
    return bool(_changed_paths({"changes_made": changes_made}))


def _format_changes(changes_made: dict[str, Any]) -> str:
    """Render the diff's file list for the prompt (mirror self_check's view)."""
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


def invariant_check_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the invariant_check step.

    Returns COMPLETED when the diff violates no recorded invariant (or when
    there is no diff / no anchors — the cheap-pass cases). Returns
    REVISION_NEEDED when at least one anchored invariant violation survives
    validation, letting the state machine route the fix loop and handle
    exhaustion centrally.
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    changes_made = step.inputs.get("changes_made") or {}

    # Cheap pass #1 — no diff (the review flow's invariant_check lands here).
    if not _has_changed_files(changes_made):
        logger.info("invariant_check: no diff to audit — passing (cheap).")
        step.outputs["issues"] = []
        step.outputs["actionable_count"] = 0
        step.outputs["skipped_reason"] = "no_diff"
        return StepStatus.COMPLETED

    anchor_inputs = _build_anchor_inputs(step, flow, project_root)

    # Cheap pass #2 — no anchors recorded. We explicitly REFUSE an anchor-less
    # check: with nothing written down there is no binding invariant to guard,
    # and inventing one would be exactly the ungrounded self-check this design
    # rejects.
    if _anchor_pool_is_empty(anchor_inputs):
        logger.info(
            "invariant_check: no recorded anchors (charter / why-comments / "
            "task) — passing (cheap); anchor-less check refused."
        )
        step.outputs["issues"] = []
        step.outputs["actionable_count"] = 0
        step.outputs["skipped_reason"] = "no_anchors"
        return StepStatus.COMPLETED

    fix_iteration = step.inputs.get("fix_iteration", 0)
    raw_max = step.inputs.get("max_fix_iterations")
    max_iterations = (
        raw_max if isinstance(raw_max, int) and not isinstance(raw_max, bool)
        else DEFAULT_MAX_FIX_ITERATIONS
    )

    task_description = anchor_inputs["task_description_base"]
    spec_content = anchor_inputs["spec_content"]
    charter_text = spec_content.get("charter", "_(no charter recorded)_")
    why_comments_text = spec_content.get(
        "why_comments", "_(no why-comments harvested from the touched code)_"
    )

    prompt = INVARIANT_CHECK_PROMPT.format(
        task_description=task_description or "_(none)_",
        charter=charter_text,
        why_comments=why_comments_text,
        changes_made=_format_changes(changes_made),
    )

    logger.info("Running invariant check (anchored to charter + why-comments)...")

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
                '{"issues": [{"severity": "critical|high|medium|low", '
                '"actual_behavior": "...", "expected_behavior": "...", '
                '"divergence": "...", '
                '"expectation_source": {"type": "charter|task_description|why_comment", "verbatim_quote": "..."}, '
                '"evidence_lines": ["path:N"], "missing_in": [], "out_of_scope": false}], '
                '"summary": "..."}'
            ),
            required_keys=["issues"],
        )
        result = parse_json_response(response, required_keys=["issues"])
        if not result:
            step.error_message = "Failed to parse invariant-check result from LLM response"
            return StepStatus.FAILED

        raw_issues = result.get("issues", [])

        # Anchor each issue against {task_description, charter, why-comments}.
        kept_issues, validation_stats = _validate_and_filter_issues(
            raw_issues, anchor_inputs,
        )

        dropped = validation_stats["input_count"] - validation_stats["kept_count"]
        if dropped > 0:
            reasons = ", ".join(
                f"{k}={v}" for k, v in validation_stats.items()
                if k.endswith("_count") and v > 0
                and k not in ("kept_count", "input_count")
            )
            logger.info(
                "invariant_check validation: %d raw -> %d kept (dropped %d: %s)",
                validation_stats["input_count"],
                validation_stats["kept_count"],
                dropped, reasons,
            )

        step.outputs["invariant_check_result"] = result
        step.outputs["raw_issues"] = raw_issues
        step.outputs["issues"] = kept_issues
        step.outputs["actionable_count"] = len(kept_issues)
        step.outputs["validation_stats"] = validation_stats

        if not kept_issues:
            logger.info("invariant_check passed (no recorded invariant violated).")
            return StepStatus.COMPLETED

        return _build_fix_outputs(step, kept_issues, fix_iteration, max_iterations)

    except Exception as e:
        logger.exception("Invariant-check step failed")
        step.error_message = f"Invariant check failed: {e}"
        return StepStatus.FAILED


def _build_fix_outputs(
    step: Step,
    issues: list,
    fix_iteration: int,
    max_iterations: int,
) -> StepStatus:
    """Populate fix-loop outputs from validated invariant violations.

    Mirrors ``self_check._build_fix_outputs`` so the state machine and the
    renderers consume invariant_check's REVISION_NEEDED the same way they
    consume self_check's.
    """
    iter_display = format_fix_iteration_display(fix_iteration, max_iterations)
    logger.warning(
        "invariant_check entering fix with %d invariant violation(s) "
        "(fix iteration %s)",
        len(issues), iter_display,
    )

    issue_details = "\n".join(
        f"- [{i.get('severity', 'high')}] {_describe_issue(i)}"
        for i in issues
    )
    fix_instructions = (
        f"Invariant check found {len(issues)} recorded-invariant violation(s):\n"
        f"{issue_details}\n\n"
        "Fix the diff so it no longer contradicts the quoted invariant(s)."
    )

    step.outputs["issues"] = issues
    step.outputs["actionable_count"] = len(issues)
    step.outputs["fix_needed"] = True
    step.outputs["fix_iteration"] = fix_iteration
    step.outputs["max_fix_iterations"] = max_iterations
    step.outputs["fix_instructions"] = fix_instructions
    step.outputs["fix_context"] = {
        "reason": "invariant_check",
        "issues": issues,
        "iteration": fix_iteration + 1,
    }
    return StepStatus.REVISION_NEEDED
