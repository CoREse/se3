"""Summarize step handler.

Generates a summary and handoff context for the completed work.
Uses LLM to create a comprehensive summary.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..llm_caller import LLMCaller, LLMCallError
from ..models import FlowInstance, Step, StepStatus
from ..utils.json_parser import parse_json_response

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
Generate a comprehensive summary including:

1. **What was accomplished**: Brief description of what was done
2. **Key changes**: List of major changes made
3. **Files modified**: List of files that were changed
4. **Testing status**: Whether tests pass and any notes
5. **Remaining work**: Any follow-up tasks or TODOs
6. **Handoff context**: Information useful for future sessions

Respond in JSON format:
```json
{{
    "summary": "Brief summary of work completed",
    "key_changes": ["change1", "change2", ...],
    "files_modified": ["file1.py", "file2.py"],
    "testing_status": "pass|fail|partial - with details",
    "remaining_work": ["task1", "task2"] or [],
    "handoff_context": "Context for future sessions",
    "next_steps": ["suggested next action1", "action2"]
}}
```
"""


def summarize_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the summarize step.

    Generates a summary using LLM based on all previous steps.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
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

    logger.info("Generating summary...")

    try:
        # Call LLM for summary
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        caller = LLMCaller(project_root)
        response = caller.call(prompt=prompt, require_json=True)

        # Parse JSON response
        summary = parse_json_response(response, required_keys=["summary"])

        if not summary:
            # Create a basic summary if parsing fails
            summary = _create_basic_summary(flow, changes_made, test_results)

        # Store outputs
        step.outputs["summary"] = summary
        step.outputs["handoff_context"] = summary.get("handoff_context", "")
        step.outputs["next_steps"] = summary.get("next_steps", [])

        # Also save to a standard location
        _save_summary(flow, summary)

        # Print summary to terminal for user visibility
        _print_summary(summary)

        logger.info("Summary generated successfully")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Summarize step failed")
        # Don't fail the flow - create a basic summary
        summary = _create_basic_summary(flow, changes_made, test_results)
        step.outputs["summary"] = summary
        step.outputs["handoff_context"] = summary.get("handoff_context", "")
        return StepStatus.COMPLETED


def _format_changes(changes_made: dict[str, Any]) -> str:
    """Format changes for inclusion in prompt."""
    if not changes_made:
        return "No changes recorded."

    lines = []
    files_changed = changes_made.get("files_changed", [])
    for file_change in files_changed:
        path = file_change.get("path", "?")
        action = file_change.get("action", "?")
        explanation = file_change.get("explanation", "")
        lines.append(f"- [{action}] {path}")
        if explanation:
            lines.append(f"  ({explanation})")

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


def _create_basic_summary(
    flow: FlowInstance,
    changes_made: dict[str, Any],
    test_results: dict[str, Any],
) -> dict[str, Any]:
    """Create a basic summary if LLM generation fails.

    Args:
        flow: The flow instance
        changes_made: Changes made during implementation
        test_results: Test results

    Returns:
        Basic summary dictionary
    """
    files_changed = changes_made.get("files_changed", [])
    file_list = [f.get("path", "?") for f in files_changed]

    return {
        "summary": f"Completed {flow.task_type or 'task'}: {flow.task_description[:50]}...",
        "key_changes": [f"Modified {len(file_list)} files"],
        "files_modified": file_list,
        "testing_status": "passed" if test_results.get("passed") else "unknown",
        "remaining_work": [],
        "handoff_context": f"Flow {flow.flow_id} completed on {datetime.now().isoformat()}",
        "next_steps": [],
    }


def _print_summary(summary: dict[str, Any]) -> None:
    """Print summary to terminal for user visibility.

    Args:
        summary: Summary dictionary
    """
    print("\n" + "=" * 60)
    print("📋 WORK SUMMARY")
    print("=" * 60)

    if "summary" in summary:
        print(f"\n{summary['summary']}")

    if "key_changes" in summary and summary["key_changes"]:
        print("\n📝 Key Changes:")
        for change in summary["key_changes"]:
            print(f"  • {change}")

    if "files_modified" in summary and summary["files_modified"]:
        print("\n📁 Files Modified:")
        for file in summary["files_modified"][:10]:  # Limit to 10 files
            print(f"  • {file}")
        if len(summary["files_modified"]) > 10:
            print(f"  ... and {len(summary['files_modified']) - 10} more")

    if "testing_status" in summary:
        print(f"\n✅ Testing Status: {summary['testing_status']}")

    if "remaining_work" in summary and summary["remaining_work"]:
        print("\n⏭️  Remaining Work:")
        for task in summary["remaining_work"]:
            print(f"  • {task}")

    if "next_steps" in summary and summary["next_steps"]:
        print("\n➡️  Suggested Next Steps:")
        for step in summary["next_steps"]:
            print(f"  • {step}")

    print("\n" + "=" * 60)


def _save_summary(flow: FlowInstance, summary: dict[str, Any]) -> None:
    """Save the summary to a standard location.

    Args:
        flow: The flow instance
        summary: Summary dictionary
    """
    from pathlib import Path

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Save to se3/state/summary.json
    summary_dir = project_root / "se3" / "state"
    summary_dir.mkdir(parents=True, exist_ok=True)

    summary_file = summary_dir / f"summary-{flow.flow_id}.json"

    try:
        import json
        summary_file.write_text(
            json.dumps(
                {
                    "flow_id": flow.flow_id,
                    "task_description": flow.task_description,
                    "completed_at": datetime.now().isoformat(),
                    **summary,
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        logger.debug(f"Summary saved to {summary_file}")
    except Exception as e:
        logger.warning(f"Failed to save summary: {e}")
