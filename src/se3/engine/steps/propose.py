"""Propose step handler.

Generates a change proposal based on the task description and relevant specs.
Uses LLM to create a structured proposal.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)


PROPOSE_PROMPT = """You are an expert software engineering assistant. Create a change proposal for the following task.

## Project Context
{project_summary}

## Task Description
{task_description}

## Task Type
{task_type}

## Scope
{scope}

## Relevant Specifications
{spec_content}

{revision_section}

## Instructions
Create a detailed change proposal that includes:

1. **Summary**: Brief description of the change (2-3 sentences)

2. **Motivation**: Why this change is needed

3. **Files to Modify**: List of existing files that will be changed

4. **Files to Create**: List of new files to be created (if any)

5. **Changes**: High-level description of what will change

6. **Risks**: Potential risks or considerations

7. **Testing**: How the changes will be tested

Respond in JSON format:
```json
{{
    "summary": "...",
    "motivation": "...",
    "files_to_modify": ["file1.py", "file2.py"],
    "files_to_create": ["new_file.py"],
    "changes": "High-level description...",
    "risks": ["risk1", "risk2"],
    "testing": "How to test..."
}}
```
"""

REVISION_SECTION = """
## Previous Proposal Feedback
The previous proposal was reviewed and requires changes:

{revision_feedback}

Please revise the proposal addressing the feedback above.
"""


def propose_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the propose step.

    Generates a change proposal using LLM based on task and specs.
    Supports revision mode when revision_feedback is provided.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    task_type = step.inputs.get("task_type", "feature")
    scope = step.inputs.get("scope", "")
    spec_content = step.inputs.get("spec_content", {})
    project_summary = step.inputs.get("project_summary", "Not available")
    revision_feedback = step.inputs.get("revision_feedback", "")
    is_revision = step.inputs.get("is_revision", False)

    if not task_description:
        step.error_message = "No task description provided"
        return StepStatus.FAILED

    # Format spec content for prompt
    spec_text = _format_spec_content(spec_content)

    # Build revision section if this is a revision
    if is_revision and revision_feedback:
        revision_section = REVISION_SECTION.format(revision_feedback=revision_feedback)
    else:
        revision_section = ""

    # Build prompt
    prompt = PROPOSE_PROMPT.format(
        task_description=task_description,
        task_type=task_type,
        scope=scope,
        spec_content=spec_text,
        project_summary=project_summary,
        revision_section=revision_section,
    )

    # Append language instruction if configured (e.g., when human confirmation is enabled)
    from ..context_builder import get_step_language_instruction, get_issue_discovery_injection
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    lang_instruction = get_step_language_instruction("propose", project_root)
    if lang_instruction:
        prompt += lang_instruction

    # Append issue discovery injection if applicable
    injection = get_issue_discovery_injection("propose", project_root)
    if injection:
        prompt += injection

    logger.info(f"Generating proposal for: {task_description[:60]}...")

    try:
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count)
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint='{"summary": "...", "motivation": "...", "files_to_modify": [], "files_to_create": [], "changes": "...", "risks": [], "testing": "..."}',
        )

        # Parse JSON response
        proposal = parse_json_response(response, required_keys=["summary"])

        if not proposal:
            step.error_message = "Failed to parse proposal from LLM response"
            return StepStatus.FAILED

        # Store outputs
        step.outputs["proposal"] = proposal

        logger.info(f"Proposal generated: {proposal.get('summary', '')[:80]}...")
        logger.debug(f"Files to modify: {proposal.get('files_to_modify', [])}")
        logger.debug(f"Files to create: {proposal.get('files_to_create', [])}")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Propose step failed")
        step.error_message = f"Proposal generation failed: {str(e)}"
        return StepStatus.FAILED


def _format_spec_content(spec_content: dict[str, str]) -> str:
    """Format spec content for inclusion in prompt.

    Args:
        spec_content: Dictionary of spec name -> content

    Returns:
        Formatted string
    """
    if not spec_content:
        return "No relevant specifications found."

    parts = []
    for name, content in spec_content.items():
        parts.append(f"### {name}")
        parts.append(content)
        parts.append("")

    return "\n".join(parts)
