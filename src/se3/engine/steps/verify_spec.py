"""Verify Spec step handler.

Checks implementation against specifications for consistency.
Uses LLM to verify that requirements are met.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..llm_caller import LLMCaller, LLMCallError
from ..models import FlowInstance, Step, StepStatus

logger = logging.getLogger(__name__)


VERIFY_PROMPT = """You are an expert software quality assurance engineer. Verify that the implementation matches the specifications.

## Task Description
{task_description}

## Relevant Specifications
{spec_content}

## Changes Made
{changes_made}

## Test Results
{test_results}

## Instructions
Verify the implementation against the specifications. Check:

1. **Requirements Met**: Are all requirements from specs implemented?
2. **Design Compliance**: Does the code follow the specified design?
3. **No Unintended Changes**: Are there any changes that weren't specified?
4. **Correctness**: Is the implementation logically correct?

Respond in JSON format:
```json
{{
    "verified": true|false,
    "issues": [
        {{
            "severity": "error|warning|info",
            "message": "Description of the issue",
            "suggestion": "How to fix (if applicable)"
        }}
    ],
    "summary": "Brief summary of verification results",
    "recommendations": ["recommendation1", "recommendation2"]
}}
```

Set "verified" to true if the implementation is acceptable (may have minor issues).
Set "verified" to false only if there are critical errors that must be fixed.
"""


def verify_spec_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the verify_spec step.

    Verifies implementation against specifications using LLM.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    spec_content = step.inputs.get("spec_content", {})
    changes_made = step.inputs.get("changes_made", {})
    test_results = step.inputs.get("test_results", {})

    # Format inputs
    spec_text = _format_spec_content(spec_content)
    changes_text = _format_changes(changes_made)
    test_text = _format_test_results(test_results)

    # Build prompt
    prompt = VERIFY_PROMPT.format(
        task_description=task_description,
        spec_content=spec_text,
        changes_made=changes_text,
        test_results=test_text,
    )

    logger.info("Verifying implementation against specifications...")

    try:
        # Call LLM for verification
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        caller = LLMCaller(project_root)
        response = caller.call(prompt=prompt)

        # Parse JSON response
        verification = _parse_verify_response(response)

        if not verification:
            step.error_message = "Failed to parse verification from LLM response"
            return StepStatus.FAILED

        # Store outputs
        step.outputs["verification_result"] = verification
        step.outputs["verified"] = verification.get("verified", False)
        step.outputs["issues"] = verification.get("issues", [])

        issues = verification.get("issues", [])
        error_count = sum(1 for i in issues if i.get("severity") == "error")

        if verification.get("verified", False):
            logger.info(f"Verification passed ({len(issues)} issues found, {error_count} errors)")
        else:
            logger.warning(f"Verification failed: {error_count} errors found")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Verify step failed")
        step.error_message = f"Verification failed: {str(e)}"
        return StepStatus.FAILED


def _format_spec_content(spec_content: dict[str, str]) -> str:
    """Format spec content for inclusion in prompt."""
    if not spec_content:
        return "No specifications provided."

    parts = []
    for name, content in spec_content.items():
        parts.append(f"### {name}")
        # Truncate very long specs
        if len(content) > 3000:
            content = content[:3000] + "\n... [truncated]"
        parts.append(content)
        parts.append("")

    return "\n".join(parts)


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
        lines.append(f"- {action}: {path}")
        if explanation:
            lines.append(f"  ({explanation})")

    return "\n".join(lines) if lines else "Changes made but details unavailable."


def _format_test_results(test_results: dict[str, Any]) -> str:
    """Format test results for inclusion in prompt."""
    if not test_results:
        return "No test results available."

    passed = test_results.get("passed", False)
    returncode = test_results.get("returncode", "?")

    lines = [f"Tests passed: {passed} (exit code: {returncode})"]

    stdout = test_results.get("stdout", "")
    if stdout:
        # Include last 1000 chars of stdout
        stdout_preview = stdout[-1000:] if len(stdout) > 1000 else stdout
        lines.append(f"\nTest output:\n{stdout_preview}")

    return "\n".join(lines)


def _parse_verify_response(response: str) -> dict[str, Any] | None:
    """Parse the LLM response into structured verification result."""
    try:
        response = response.strip()

        # Remove markdown code block if present
        if response.startswith("```json"):
            response = response[7:]
        elif response.startswith("```"):
            response = response[3:]

        if response.endswith("```"):
            response = response[:-3]

        response = response.strip()

        result = json.loads(response)

        if "verified" not in result:
            logger.warning("Missing 'verified' in verify response")
            return None

        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error parsing response: {e}")
        return None
