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
from ..models import FlowInstance, Step, StepStatus
from ..prompt_markers import inject_boundary

logger = logging.getLogger(__name__)


SUMMARIZE_PROMPT = """You are an expert software engineering assistant. Generate a summary of the completed work.

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
Generate a comprehensive summary in Markdown format. Include:

1. **What was accomplished** - Brief description of what was done
2. **Key changes** - List of major changes made
3. **Files modified** - List of files that were changed
4. **Testing status** - Whether tests pass and any notes
5. **Remaining work** - Any follow-up tasks or TODOs
6. **Handoff context** - Information useful for future sessions
7. **Suggested next steps** - Recommended next actions

Format your response as clean Markdown with clear headings and bullet points.
"""

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

    # Format inputs
    changes_text = _format_changes(changes_made)
    test_text = _format_test_results(test_results)
    verification_text = _format_verification(verification_result)
    commit_info = f"Commit: {commit_hash[:8] if commit_hash else 'N/A'}"

    # Build completion section for the prompt
    completion_section = _build_completion_section(
        completion_status, incomplete_tasks, implement_summary,
        restricted_edits_applied, restricted_edits_failed,
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

    # Append language instruction if configured
    from ..context_builder import get_step_language_instruction, get_issue_discovery_injection
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    lang_instruction = get_step_language_instruction("summarize", project_root)
    if lang_instruction:
        prompt += lang_instruction

    # Append issue discovery injection if applicable
    injection = get_issue_discovery_injection("summarize", project_root)
    if injection:
        prompt += injection

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
            summary_text = _create_basic_summary_text(flow, changes_made, test_results, task_description, incomplete_tasks, completion_status)

        # Store output
        step.outputs["summary"] = summary_text

        # Extract discovered_issues from LLM response for B-class collection
        _extract_discovered_issues(response, step)

        # Save to file
        _save_summary(flow, summary_text)

        logger.info("Summary generated successfully")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Summarize step failed")
        # Don't fail the flow - create a basic summary
        summary_text = _create_basic_summary_text(flow, changes_made, test_results, task_description, incomplete_tasks, completion_status)
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


def _format_test_results(test_results: dict[str, Any]) -> str:
    """Format test results for inclusion in prompt."""
    if not test_results:
        return "No test results available."

    passed = test_results.get("passed", False)
    return f"Tests passed: {passed}"


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


def _build_completion_section(
    completion_status: str,
    incomplete_tasks: list,
    implement_summary: str,
    restricted_edits_applied: list,
    restricted_edits_failed: list,
) -> str:
    """Build the completion status section for the summarize prompt.

    Returns an empty string when status is 'complete' and there is nothing
    extra to report, keeping the prompt unchanged for the common case.
    """
    if completion_status == "complete" and not restricted_edits_applied and not restricted_edits_failed:
        return ""

    lines: list[str] = ["\n## Completion Status"]
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
) -> str:
    """Create a basic summary if LLM generation fails.

    Args:
        flow: The flow instance
        changes_made: Changes made during implementation
        test_results: Test results
        task_description: Resolved task description (refined or original)

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

    status_label = "Partially completed" if completion_status == "partial" else "Completed"
    lines = [
        f"## Work Summary\n",
        f"{status_label} {flow.task_type or 'task'}: {task_description[:100]}...\n",
        f"### Key Changes",
        f"- Modified {len(file_list)} files\n",
        f"### Files Modified",
    ]
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

    lines.extend([
        "",
        f"### Testing Status",
        f"{'Tests passed' if test_results.get('passed') else 'Test status unknown'}",
        "",
        f"### Handoff Context",
        f"Flow {flow.flow_id} completed on {datetime.now().isoformat()}",
    ])

    return "\n".join(lines)


def _extract_discovered_issues(response: str, step: Step) -> None:
    """Extract discovered_issues JSON from LLM natural language response.

    Looks for a JSON block containing discovered_issues in the response text.
    Stores found issues in step.outputs['discovered_issues'].

    Args:
        response: Raw LLM response (may be NDJSON stream or plain text)
        step: Step to store discovered issues in
    """
    import json
    import re

    text = response or ""

    # Look for JSON block containing discovered_issues
    # Try to find ```json ... ``` blocks first
    json_blocks = re.findall(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
    for block in json_blocks:
        try:
            data = json.loads(block.strip())
            if isinstance(data, dict) and "discovered_issues" in data:
                issues = data["discovered_issues"]
                if isinstance(issues, list) and issues:
                    step.outputs["discovered_issues"] = issues
                    return
        except (json.JSONDecodeError, ValueError):
            continue

    # Try to find inline JSON with discovered_issues
    pattern = r'\{[^{}]*"discovered_issues"\s*:\s*\[.*?\]\s*\}'
    matches = re.findall(pattern, text, re.DOTALL)
    for match in matches:
        try:
            data = json.loads(match)
            issues = data.get("discovered_issues", [])
            if isinstance(issues, list) and issues:
                step.outputs["discovered_issues"] = issues
                return
        except (json.JSONDecodeError, ValueError):
            continue


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
