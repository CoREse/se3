"""Update Spec step handler.

Updates specifications to reflect the changes made.
Uses LLM to generate spec updates.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..llm_caller import LLMCaller, LLMCallError
from ..models import FlowInstance, Step, StepStatus
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)


UPDATE_SPEC_PROMPT = """You are an expert technical writer. Update the specifications to reflect the changes made.

## Task Description
{task_description}

## Changes Made
{changes_made}

## Verification Results
{verification_result}

## Current Specifications
{spec_content}

## Instructions
Review the current specifications and determine what updates are needed to reflect the changes made.

For each spec that needs updating, provide:
1. The spec name
2. The section to update
3. The new content (or a description of the change)

Respond in JSON format:
```json
{{
    "specs_to_update": [
        {{
            "spec_name": "name-of-spec",
            "section": "Section Title",
            "change_description": "What changed",
            "new_content": "The updated content (optional - can be description)"
        }}
    ],
    "new_capabilities": ["capability1", "capability2"],
    "notes": "Any additional notes"
}}
```

If no spec updates are needed, return an empty `specs_to_update` array.
"""


def update_spec_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the update_spec step.

    Determines spec updates needed based on changes made.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    changes_made = step.inputs.get("changes_made", {})
    spec_content = step.inputs.get("spec_content", {})
    verification_result = step.inputs.get("verification_result", {})

    # Format inputs
    spec_text = _format_spec_content(spec_content)
    changes_text = _format_changes(changes_made)
    verification_text = _format_verification(verification_result)

    # Build prompt
    prompt = UPDATE_SPEC_PROMPT.format(
        task_description=task_description,
        changes_made=changes_text,
        verification_result=verification_text,
        spec_content=spec_text,
    )

    # Append language instruction if configured
    from ..context_builder import get_step_language_instruction
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    lang_instruction = get_step_language_instruction("update_spec", project_root)
    if lang_instruction:
        prompt += lang_instruction

    logger.info("Determining spec updates needed...")

    try:
        # Call LLM for spec updates (use EXTRACT mode for nested array structures)
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count)
        response = caller.call(
            prompt=prompt,
            json_mode="extract",
            json_schema_hint='{"specs_to_update": [{"spec_name": "...", "section": "...", "change_description": "..."}], "new_capabilities": [], "notes": "..."}',
        )

        # Parse JSON response
        update_plan = parse_json_response(response, required_keys=["specs_to_update"])

        if not update_plan:
            step.error_message = "Failed to parse spec update plan from LLM response"
            return StepStatus.FAILED

        # Store outputs
        step.outputs["updated_specs"] = update_plan.get("specs_to_update", [])
        step.outputs["new_capabilities"] = update_plan.get("new_capabilities", [])

        specs_to_update = update_plan.get("specs_to_update", [])
        if specs_to_update:
            logger.info(f"Spec updates needed: {len(specs_to_update)} specs")
            for spec in specs_to_update:
                logger.debug(f"  - {spec.get('spec_name', '?')}: {spec.get('change_description', '')}")
        else:
            logger.info("No spec updates needed")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Update spec step failed")
        step.error_message = f"Spec update planning failed: {str(e)}"
        return StepStatus.FAILED


def _format_spec_content(spec_content: dict[str, str]) -> str:
    """Format spec content for inclusion in prompt."""
    if not spec_content:
        return "No specifications provided."

    parts = []
    for name, content in spec_content.items():
        parts.append(f"### {name}")
        # Truncate very long specs
        if len(content) > 2000:
            content = content[:2000] + "\n... [truncated]"
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
        if isinstance(file_change, str):
            # implement step may output plain file paths
            lines.append(f"- modified: {file_change}")
        elif isinstance(file_change, dict):
            path = file_change.get("path", "?")
            action = file_change.get("action", "?")
            explanation = file_change.get("explanation", "")
            lines.append(f"- {action}: {path}")
            if explanation:
                lines.append(f"  ({explanation})")
        else:
            lines.append(f"- {file_change}")

    return "\n".join(lines) if lines else "Changes made but details unavailable."


def _format_verification(verification_result: dict[str, Any]) -> str:
    """Format verification results for inclusion in prompt."""
    if not verification_result:
        return "No verification results available."

    verified = verification_result.get("verified", False)
    summary = verification_result.get("summary", "")

    lines = [f"Verification passed: {verified}"]
    if summary:
        lines.append(f"Summary: {summary}")

    return "\n".join(lines)
