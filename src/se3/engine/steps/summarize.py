"""Summarize step handler.

Generates a summary and handoff context for the completed work.
Uses LLM to create a comprehensive summary in natural language.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus, StepType
from ..prompt_markers import inject_boundary

logger = logging.getLogger(__name__)


SUMMARIZE_PROMPT = """You are an expert software engineering assistant. Write a report of what THIS session actually did.

Your only job is to tell the user what happened in this session: the work that was performed, what changed, and the resulting test/verification status. This is a plain session report — do NOT hunt for new problems, do NOT propose unrelated issues to file, and do NOT pad the report with speculative work that did not happen.

## Task Description
{task_description}

## Task Type
{task_type}

## Changes Made
{changes_made}

## Test Results
{test_results}

## Verification Results
{verification_result}

## Commit
{commit_info}
{completion_section}
## Instructions
Write a concise Markdown report covering ONLY this session. Include:

1. **What was done** - What this session actually accomplished
2. **Key changes** - The notable changes made this session
3. **Files modified** - The files that were changed this session
4. **Test & verification status** - Whether tests passed and whether the work was verified, stated honestly based on the status above
5. **Handoff notes** - Anything the next session needs in order to continue (only when relevant)

Report only facts about this session. Do not invent work that did not happen, and do not claim the work is complete, verified, or "all green" unless the status above confirms it.
"""

# Two-segment marker only: USER_CONTENT region is empty.
# summarize consumes all upstream step outputs; no user-literal field is
# appended here. The web console renders the whole post-BEGIN tail inside
# the collapsed system-prompt chip.
SUMMARIZE_PROMPT = inject_boundary(SUMMARIZE_PROMPT, "## Task Description\n")


def summarize_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the summarize step.

    Generates a summary using LLM based on all previous steps.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", flow.task_description)
    task_type = flow.task_type or "feature"
    changes_made = step.inputs.get("changes_made", {})
    test_results = step.inputs.get("test_results", {})
    verification_result = step.inputs.get("verification_result", {})
    commit_hash = step.inputs.get("commit_hash", "")

    # Get completion status from implement step (defaults for backward compatibility)
    completion_status = step.inputs.get("completion_status", "complete")
    incomplete_tasks = step.inputs.get("incomplete_tasks", [])
    implement_summary = step.inputs.get("implement_summary", "")
    restricted_edits_applied = step.inputs.get("restricted_edits_applied", [])
    restricted_edits_failed = step.inputs.get("restricted_edits_failed", [])

    # Authoritative verification verdict (computed by verify_spec, bridged into
    # verification_result). True/False when verify_spec ran, None when the
    # workflow has no verify_spec step. The completion gate trips only on an
    # explicit False so verify_spec-less workflows report normally.
    verified = _resolve_verified(verification_result)

    # Format inputs
    changes_text = _format_changes(changes_made)
    test_text = _format_test_results(test_results)
    verification_text = _format_verification(verification_result)
    commit_info = f"Commit: {commit_hash[:8] if commit_hash else 'N/A'}"

    # Build completion section for the prompt
    completion_section = _build_completion_section(
        completion_status, incomplete_tasks, implement_summary,
        restricted_edits_applied, restricted_edits_failed,
        verified=verified,
    )

    # Build prompt
    prompt = SUMMARIZE_PROMPT.format(
        task_description=task_description,
        task_type=task_type,
        changes_made=changes_text,
        test_results=test_text,
        verification_result=verification_text,
        commit_info=commit_info,
        completion_section=completion_section,
    )

    # Surface the knowledge-artifact guard results (charter auto-update /
    # advisory + why-comment losses) so they are reachable in the flow-level
    # report rather than stranded in a step detail view nobody reopens.
    knowledge_section = _format_knowledge_guards(flow)
    if knowledge_section:
        prompt += knowledge_section

    # Append language instruction if configured.
    # Note: summarize deliberately does NOT receive B-class issue-discovery
    # injection. Its sole job is to report what this session did; it no longer
    # collects discovered_issues from the LLM response.
    from ..context_builder import (
        get_step_language_instruction,
        get_runtime_environment_injection,
    )
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    lang_instruction = get_step_language_instruction("summarize", project_root)
    if lang_instruction:
        prompt += lang_instruction

    # Append runtime environment injection if applicable
    runtime_env = get_runtime_environment_injection("summarize", project_root)
    if runtime_env:
        prompt += runtime_env

    logger.info("Generating summary...")

    try:
        # Call LLM for summary (natural language output, no JSON required)
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count, fix_iteration=step.inputs.get("fix_iteration", 0))
        response = caller.call(
            prompt=prompt,
            json_mode="off",
        )

        # Use LLMCaller response directly (already extracted plain text)
        summary_text = response

        if not summary_text:
            summary_text = _create_basic_summary_text(
                flow, changes_made, test_results, task_description,
                incomplete_tasks, completion_status, verified,
            )

        # Store output
        step.outputs["summary"] = summary_text

        # Save to file
        _save_summary(flow, summary_text)

        logger.info("Summary generated successfully")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Summarize step failed")
        # Don't fail the flow - create a basic summary
        summary_text = _create_basic_summary_text(
            flow, changes_made, test_results, task_description,
            incomplete_tasks, completion_status, verified,
        )
        step.outputs["summary"] = summary_text
        return StepStatus.COMPLETED


def _format_changes(changes_made: dict[str, Any]) -> str:
    """Format changes for inclusion in prompt."""
    if not changes_made:
        return "No changes recorded."

    lines = []
    files_changed = changes_made.get("files_changed", [])
    for file_change in files_changed:
        if isinstance(file_change, str):
            # implement step may output plain file paths
            lines.append(f"- [modified] {file_change}")
        elif isinstance(file_change, dict):
            path = file_change.get("path", "?")
            action = file_change.get("action", "?")
            explanation = file_change.get("explanation", "")
            lines.append(f"- [{action}] {path}")
            if explanation:
                lines.append(f"  ({explanation})")
        else:
            lines.append(f"- {file_change}")

    return "\n".join(lines) if lines else "Changes made but details unavailable."


def _gated_tests_passed(test_results: dict[str, Any]) -> bool:
    """Return the gated test verdict for the session report.

    Prefers ``overall_passed`` — the value the critical-acceptance gate forces
    to False when a critical test is skipped or missing — over the
    backward-compat ``passed`` key (the raw primary-pytest returncode==0, which
    stays True on a skip). Falls back to ``passed`` only for legacy
    ``test_results`` dicts written before ``overall_passed`` existed, so a
    skipped critical test is never reported as a green test status.
    """
    if "overall_passed" in test_results:
        return bool(test_results.get("overall_passed"))
    return bool(test_results.get("passed", False))


def _format_test_results(test_results: dict[str, Any]) -> str:
    """Format test results for inclusion in prompt."""
    if not test_results:
        return "No test results available."

    return f"Tests passed: {_gated_tests_passed(test_results)}"


def _format_verification(verification_result: dict[str, Any]) -> str:
    """Format verification results for inclusion in prompt."""
    if not verification_result:
        return "No verification results available."

    verified = verification_result.get("verified", False)
    summary = verification_result.get("summary", "")

    lines = [f"Verification passed: {verified}"]
    if summary:
        lines.append(f"Summary: {summary}")

    issues = verification_result.get("issues", [])
    if issues:
        error_count = sum(1 for i in issues if i.get("severity") == "error")
        lines.append(f"Issues found: {len(issues)} ({error_count} errors)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# knowledge-artifact guard aggregation (charter_freshness + invariant_check)
# ---------------------------------------------------------------------------
# charter_freshness closes its own propose->gate->apply loop and invariant_check
# records advisory why-comment losses, but both results only live in the single
# step's detail view — which nobody revisits after a flow finishes. summarize is
# the one flow-level consumer everyone reads, so it surfaces them here: without
# this, an auto-applied charter update, a degraded advisory, or a lost why-
# comment would be practically unreachable.

# Cap the charter diff excerpt so a large descriptive rewrite cannot flood the
# session report (or the LLM prompt) — the full diff stays in the step outputs.
MAX_CHARTER_DIFF_CHARS = 2000


def _collect_knowledge_guard_outputs(
    flow: FlowInstance,
) -> tuple[dict[str, Any], list[dict]]:
    """Pull the charter_freshness outputs and invariant_check why-comment losses.

    Returns ``(charter_outputs, why_losses)``. Scans ``flow.state.steps`` rather
    than ``step.inputs`` so summarize stays self-contained (the state machine
    does not forward these fields). When a step ran more than once (fix-loop
    re-run), the last occurrence carrying the field wins.
    """
    charter_outputs: dict[str, Any] = {}
    why_losses: list[dict] = []
    state = getattr(flow, "state", None)
    if state is None:
        return charter_outputs, why_losses
    for step in state.steps.values():
        outputs = step.outputs if isinstance(step.outputs, dict) else {}
        if step.step_type is StepType.CHARTER_FRESHNESS:
            charter_outputs = outputs
        elif step.step_type is StepType.INVARIANT_CHECK:
            losses = outputs.get("why_comment_losses")
            if isinstance(losses, list) and losses:
                why_losses = losses
    return charter_outputs, why_losses


def _format_why_loss(loss: Any) -> str:
    """Render one ``{file, comment, why_it_matters}`` advisory entry as one line."""
    if not isinstance(loss, dict):
        return str(loss)
    comment = str(loss.get("comment", "") or "").strip()
    file = str(loss.get("file", "") or "").strip()
    why = str(loss.get("why_it_matters", "") or "").strip()
    head = f"`{file}`: " if file else ""
    body = comment or "(comment)"
    tail = f" — {why}" if why else ""
    return f"{head}{body}{tail}"


def _format_knowledge_guards(flow: FlowInstance) -> str:
    """Build the flow-level "Knowledge Guards" markdown section (or "").

    Covers three shapes: charter auto-updated (note + capped diff excerpt),
    charter update advised but not applied (suggested_update + degraded reason),
    and non-empty why-comment losses. Returns "" when nothing happened so the
    common flow's report is unchanged.
    """
    charter_outputs, why_losses = _collect_knowledge_guard_outputs(flow)
    lines: list[str] = []

    if charter_outputs:
        auto_updated = bool(charter_outputs.get("charter_auto_updated"))
        update_needed = bool(charter_outputs.get("charter_update_needed"))
        if auto_updated:
            lines.append("### Charter Auto-Update")
            lines.append(
                "The project charter (`se3/charter.md`) was automatically updated "
                "to descriptively reflect this session's already-reviewed changes."
            )
            diff = str(charter_outputs.get("charter_diff", "") or "")
            if diff:
                excerpt = diff[:MAX_CHARTER_DIFF_CHARS]
                if len(diff) > MAX_CHARTER_DIFF_CHARS:
                    excerpt += "\n… (diff truncated)"
                lines.append("```diff")
                lines.append(excerpt)
                lines.append("```")
        elif update_needed:
            lines.append("### Charter Update Advisory")
            lines.append(
                "The diff appears to touch the charter but it was NOT auto-updated; "
                "a manual charter review/update is advised."
            )
            suggested = str(charter_outputs.get("suggested_update", "") or "").strip()
            if suggested:
                lines.append(f"- Suggested update: {suggested}")
            reason = str(charter_outputs.get("degraded_reason", "") or "").strip()
            if reason:
                lines.append(f"- Not auto-applied because: {reason}")

    if why_losses:
        lines.append("### Why-Comment Losses (advisory)")
        lines.append(
            "These meaningful why/intent comments were removed or rewritten this "
            "session — review whether the recorded intent was lost:"
        )
        for loss in why_losses[:10]:
            lines.append(f"- {_format_why_loss(loss)}")
        if len(why_losses) > 10:
            lines.append(f"- … and {len(why_losses) - 10} more")

    if not lines:
        return ""
    return "\n## Knowledge Guards\n" + "\n".join(lines) + "\n"


def _resolve_verified(verification_result: dict[str, Any]) -> bool | None:
    """Extract the authoritative ``verified`` verdict from verification_result.

    verify_spec bridges its rule-based ``verified`` into the
    ``verification_result`` dict. Returns the bool when present, or ``None``
    when the workflow had no verify_spec step (so the completion gate stays
    inactive instead of falsely reporting "not verified").
    """
    if not isinstance(verification_result, dict):
        return None
    value = verification_result.get("verified")
    if isinstance(value, bool):
        return value
    return None


def _verification_gate_lines(verified: bool | None) -> list[str]:
    """Return the verified=False completion-gate instruction lines.

    Empty unless ``verified`` is explicitly ``False``. This is the prompt-side
    half of the downstream completion gate: when the work did not pass
    verification, the summary must not claim it is done/all-green.
    """
    if verified is not False:
        return []
    return [
        "\n## Verification Status: NOT PASSED",
        "This session did NOT pass verification (`verified=False`). Report this "
        "honestly: you MUST NOT describe the work as \"complete\", \"done\", "
        "\"all green\", \"fully working\", or \"verified\". State clearly that "
        "verification did not pass and the work is unverified / unfinished, and "
        "summarize what still needs to happen.",
    ]


def _build_completion_section(
    completion_status: str,
    incomplete_tasks: list,
    implement_summary: str,
    restricted_edits_applied: list,
    restricted_edits_failed: list,
    verified: bool | None = None,
) -> str:
    """Build the completion status section for the summarize prompt.

    Returns an empty string when status is 'complete', there is nothing extra
    to report, and verification did not fail — keeping the prompt unchanged for
    the common case. When ``verified`` is ``False`` the completion gate is
    always included regardless of completion_status.
    """
    gate_lines = _verification_gate_lines(verified)

    if (
        completion_status == "complete"
        and not restricted_edits_applied
        and not restricted_edits_failed
    ):
        # Nothing extra to report — but the verified=False gate, if present,
        # must still reach the prompt.
        if gate_lines:
            return "\n".join(gate_lines + [""])
        return ""

    lines: list[str] = list(gate_lines)
    lines.append("\n## Completion Status")
    lines.append(f"Status: **{completion_status}**")

    if implement_summary:
        lines.append(f"\nImplementation summary: {implement_summary}")

    if incomplete_tasks:
        lines.append("\n### Incomplete Tasks")
        lines.append("The following tasks were NOT completed — report them clearly:")
        for task in incomplete_tasks:
            if isinstance(task, str):
                lines.append(f"- {task}")
            elif isinstance(task, dict):
                desc = task.get("description", task.get("task", str(task)))
                reason = task.get("reason", "")
                entry = f"- {desc}"
                if reason:
                    entry += f" — reason: {reason}"
                lines.append(entry)

    if restricted_edits_applied:
        lines.append("\n### Restricted Edits Applied")
        lines.append("These edits were applied by the engine (not the LLM subprocess):")
        for edit in restricted_edits_applied:
            fp = edit.get("file_path", "?") if isinstance(edit, dict) else str(edit)
            lines.append(f"- {fp}")

    if restricted_edits_failed:
        lines.append("\n### Restricted Edits Failed")
        lines.append("These restricted edits could NOT be applied:")
        for edit in restricted_edits_failed:
            if isinstance(edit, dict):
                fp = edit.get("file_path", "?")
                err = edit.get("error", "unknown error")
                lines.append(f"- {fp}: {err}")
            else:
                lines.append(f"- {edit}")

    lines.append("")  # trailing newline before next section
    return "\n".join(lines)


def _create_basic_summary_text(
    flow: FlowInstance,
    changes_made: dict[str, Any],
    test_results: dict[str, Any],
    task_description: str = "",
    incomplete_tasks: list | None = None,
    completion_status: str = "complete",
    verified: bool | None = None,
) -> str:
    """Create a basic summary if LLM generation fails.

    Args:
        flow: The flow instance
        changes_made: Changes made during implementation
        test_results: Test results
        task_description: Resolved task description (refined or original)
        incomplete_tasks: Tasks that were not completed
        completion_status: 'complete' or 'partial'
        verified: Authoritative verification verdict. When ``False`` the
            fallback summary must not claim the work is completed/verified
            (the completion gate, fallback half).

    Returns:
        Basic summary text in Markdown format
    """
    task_description = task_description or flow.task_description
    files_changed = changes_made.get("files_changed", [])
    file_list = []
    for f in files_changed:
        if isinstance(f, str):
            file_list.append(f)
        elif isinstance(f, dict):
            file_list.append(f.get("path", "?"))
        else:
            file_list.append(str(f))

    if verified is False:
        status_label = "Not verified (incomplete)"
    elif completion_status == "partial":
        status_label = "Partially completed"
    else:
        status_label = "Completed"
    lines = [
        f"## Work Summary\n",
        f"{status_label} {flow.task_type or 'task'}: {task_description[:100]}...\n",
    ]
    if verified is False:
        lines.extend([
            "### Verification Status: NOT PASSED",
            "This session did NOT pass verification — the work is unverified and "
            "not complete.\n",
        ])
    lines.extend([
        f"### Key Changes",
        f"- Modified {len(file_list)} files\n",
        f"### Files Modified",
    ])
    for f in file_list[:10]:
        lines.append(f"- {f}")
    if len(file_list) > 10:
        lines.append(f"- ... and {len(file_list) - 10} more")

    if incomplete_tasks:
        lines.extend(["", "### Incomplete Tasks"])
        for task in incomplete_tasks:
            if isinstance(task, str):
                lines.append(f"- {task}")
            elif isinstance(task, dict):
                desc = task.get("description", task.get("task", str(task)))
                reason = task.get("reason", "")
                entry = f"- {desc}"
                if reason:
                    entry += f" ({reason})"
                lines.append(entry)

    testing_status = "Tests passed" if _gated_tests_passed(test_results) else "Test status unknown"
    if verified is False:
        handoff = (
            f"Flow {flow.flow_id} ended NOT verified on {datetime.now().isoformat()}"
        )
    else:
        handoff = f"Flow {flow.flow_id} completed on {datetime.now().isoformat()}"

    lines.extend([
        "",
        f"### Testing Status",
        testing_status,
        "",
        f"### Handoff Context",
        handoff,
    ])

    # Same knowledge-guard aggregation the LLM prompt gets, so the deterministic
    # fallback report is not blind to charter auto-updates / why-comment losses.
    knowledge_section = _format_knowledge_guards(flow)
    if knowledge_section:
        lines.append(knowledge_section)

    return "\n".join(lines)


def _save_summary(flow: FlowInstance, summary_text: str) -> None:
    """Save the summary to a standard location.

    Args:
        flow: The flow instance
        summary_text: Summary text to save
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Save to se3/state/summary-{flow_id}.md (Markdown format)
    summary_dir = project_root / "se3" / "state"
    summary_dir.mkdir(parents=True, exist_ok=True)

    summary_file = summary_dir / f"summary-{flow.flow_id}.md"

    try:
        content = f"""# Work Summary

**Flow ID:** {flow.flow_id}
**Task:** {flow.task_description}
**Completed:** {datetime.now().isoformat()}

---

{summary_text}
"""
        summary_file.write_text(content, encoding="utf-8")
        logger.debug(f"Summary saved to {summary_file}")
    except Exception as e:
        logger.warning(f"Failed to save summary: {e}")
