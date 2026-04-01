"""Update Spec step handler.

Updates specifications to reflect the changes made.
Uses LLM to generate spec updates.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)


UPDATE_SPEC_PROMPT = """You are an expert technical writer. Update the project specifications to reflect the changes made.

## Task Description
{task_description}

## Changes Made
{changes_made}

## Verification Results
{verification_result}

## Specs Directory
{specs_dir}

## Instructions
1. Read the relevant spec files in the specs directory using the Read tool.
2. Determine which specs need updating to reflect the changes made.
3. Use the Edit tool to directly modify the spec files. Follow existing formatting conventions.
4. Only update specs that genuinely need changes — do not rewrite specs unnecessarily.
5. Follow spec guardrails: do NOT delete existing requirements, only add or modify.

When you are done, output a JSON summary:
```json
{{
    "specs_updated": [
        {{
            "spec_name": "name-of-spec",
            "change_description": "What was changed and why"
        }}
    ],
    "new_capabilities": ["capability1", "capability2"],
    "notes": "Any additional notes"
}}
```

If no spec updates are needed, return an empty `specs_updated` array.
"""


def update_spec_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the update_spec step.

    Updates spec files to reflect changes made during implementation.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    changes_made = step.inputs.get("changes_made", {})
    verification_result = step.inputs.get("verification_result", {})

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Resolve specs directory for LLM tool access
    from ..context_builder import ContextBuilder
    builder = ContextBuilder(project_root)
    specs_dir = str(builder.specs_dir.resolve())

    # Format inputs
    changes_text = _format_changes(changes_made)
    verification_text = _format_verification(verification_result)

    # Build prompt
    prompt = UPDATE_SPEC_PROMPT.format(
        task_description=task_description,
        changes_made=changes_text,
        verification_result=verification_text,
        specs_dir=specs_dir,
    )

    # Append language instruction if configured
    from ..context_builder import get_step_language_instruction, get_issue_discovery_injection
    lang_instruction = get_step_language_instruction("update_spec", project_root)
    if lang_instruction:
        prompt += lang_instruction

    # Append issue discovery injection if applicable
    injection = get_issue_discovery_injection("update_spec", project_root)
    if injection:
        prompt += injection

    logger.info("Updating specs to reflect implementation...")

    try:
        # Call LLM with tool access (TWO_PHASE) so it can read and edit spec files
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count)
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint='{"specs_updated": [{"spec_name": "...", "change_description": "..."}], "new_capabilities": [], "notes": "..."}',
        )

        # Parse JSON response
        update_result = parse_json_response(response, required_keys=[])

        if not update_result:
            logger.warning("Could not parse update_spec summary, using defaults")
            update_result = {"specs_updated": [], "new_capabilities": []}

        # Store outputs
        specs_updated = update_result.get("specs_updated", [])
        step.outputs["updated_specs"] = specs_updated
        step.outputs["new_capabilities"] = update_result.get("new_capabilities", [])

        if specs_updated:
            logger.info(f"Specs updated: {len(specs_updated)}")
            for spec in specs_updated:
                logger.info(f"  - {spec.get('spec_name', '?')}: {spec.get('change_description', '')}")
        else:
            logger.info("No spec updates needed")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Update spec step failed")
        step.error_message = f"Spec update failed: {str(e)}"
        return StepStatus.FAILED


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
