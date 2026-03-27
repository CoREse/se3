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


def _extract_text_from_stream_json(response: str) -> str:
    """Extract text content from stream-json (NDJSON) response.
    
    Args:
        response: Raw NDJSON response from LLM
        
    Returns:
        Extracted text content
    """
    if not response:
        return ""
    
    import json
    text_parts = []
    
    for line in response.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if isinstance(data, dict) and data.get('type') == 'assistant':
                message = data.get('message', {})
                content = message.get('content', [])
                for item in content:
                    if isinstance(item, dict) and item.get('type') == 'text':
                        text = item.get('text', '')
                        if text:
                            text_parts.append(text)
        except json.JSONDecodeError:
            continue
    
    return ''.join(text_parts).strip()


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

    # Format inputs
    changes_text = _format_changes(changes_made)
    test_text = _format_test_results(test_results)
    verification_text = _format_verification(verification_result)
    commit_info = f"Commit: {commit_hash[:8] if commit_hash else 'N/A'}"

    # Build prompt
    prompt = SUMMARIZE_PROMPT.format(
        task_description=task_description,
        task_type=task_type,
        changes_made=changes_text,
        test_results=test_text,
        verification_result=verification_text,
        commit_info=commit_info,
    )

    # Append language instruction if configured
    from ..context_builder import get_step_language_instruction
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    lang_instruction = get_step_language_instruction("summarize", project_root)
    if lang_instruction:
        prompt += lang_instruction

    logger.info("Generating summary...")

    try:
        # Call LLM for summary (natural language output, no JSON required)
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type if isinstance(step.step_type, str) else step.step_type.value, external_attempt=retry_count)
        response = caller.call(
            prompt=prompt,
            json_mode="off",
        )

        # Extract text from response
        summary_text = _extract_text_from_stream_json(response)

        if not summary_text:
            summary_text = _create_basic_summary_text(flow, changes_made, test_results, task_description)

        # Store output
        step.outputs["summary"] = summary_text

        # Extract discovered_issues from LLM response for B-class collection
        _extract_discovered_issues(response, step)

        # Print to terminal for user visibility
        print("\n" + "=" * 60)
        print("📋 WORK SUMMARY")
        print("=" * 60)
        print(summary_text)
        print("=" * 60)

        # Save to file
        _save_summary(flow, summary_text)

        logger.info("Summary generated successfully")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Summarize step failed")
        # Don't fail the flow - create a basic summary
        summary_text = _create_basic_summary_text(flow, changes_made, test_results, task_description)
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


def _create_basic_summary_text(
    flow: FlowInstance,
    changes_made: dict[str, Any],
    test_results: dict[str, Any],
    task_description: str = "",
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

    lines = [
        f"## Work Summary\n",
        f"Completed {flow.task_type or 'task'}: {task_description[:100]}...\n",
        f"### Key Changes",
        f"- Modified {len(file_list)} files\n",
        f"### Files Modified",
    ]
    for f in file_list[:10]:
        lines.append(f"- {f}")
    if len(file_list) > 10:
        lines.append(f"- ... and {len(file_list) - 10} more")
    
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

    # Get the text content (handle both NDJSON and plain text)
    text = _extract_text_from_stream_json(response)
    if not text:
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
        import json
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
