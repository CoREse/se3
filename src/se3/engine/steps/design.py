"""Design step handler.

Creates a design document with architecture decisions based on the proposal.
Uses LLM to generate structured design documentation.
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


DESIGN_PROMPT = """You are an expert software architect. Create a design document for the following implementation.

## Project Context
{project_summary}

## Task Description
{task_description}

## Proposal
{proposal}

## Relevant Specifications
{spec_content}

{revision_section}

## Instructions
Create a design document that includes:

1. **Overview**: High-level description of the solution

2. **Architecture Decisions**: Key decisions with rationale
   - Why this approach was chosen
   - Trade-offs considered
   - Alternative approaches rejected

3. **Component Design**: Description of main components/modules
   - Responsibilities of each component
   - Interfaces between components

4. **Data Flow**: How data moves through the system

5. **Implementation Plan**: Step-by-step implementation approach

6. **Testing Strategy**: How to verify the implementation

Respond in JSON format:
```json
{{
    "overview": "...",
    "architecture_decisions": [
        {{"decision": "...", "rationale": "...", "alternatives_considered": "..."}}
    ],
    "components": [
        {{"name": "...", "responsibilities": "...", "interfaces": "..."}}
    ],
    "data_flow": "...",
    "implementation_plan": ["step1", "step2", ...],
    "testing_strategy": "..."
}}
```
"""

REVISION_SECTION = """
## Previous Design Feedback
The previous design was reviewed and requires changes:

{revision_feedback}

Please revise the design document addressing the feedback above.
"""


def design_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the design step.

    Creates a design document using LLM based on proposal and specs.
    Supports revision mode when revision_feedback is provided.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    proposal = step.inputs.get("proposal", {})
    spec_content = step.inputs.get("spec_content", {})
    project_summary = step.inputs.get("project_summary", "Not available")
    revision_feedback = step.inputs.get("revision_feedback", "")
    is_revision = step.inputs.get("is_revision", False)

    if not proposal:
        step.error_message = "No proposal available from previous step"
        return StepStatus.FAILED

    # Format inputs for prompt
    proposal_text = _format_proposal(proposal)
    spec_text = _format_spec_content(spec_content)

    # Build revision section if this is a revision
    if is_revision and revision_feedback:
        revision_section = REVISION_SECTION.format(revision_feedback=revision_feedback)
    else:
        revision_section = ""

    # Build prompt
    prompt = DESIGN_PROMPT.format(
        task_description=task_description,
        proposal=proposal_text,
        spec_content=spec_text,
        project_summary=project_summary,
        revision_section=revision_section,
    )

    # Append language instruction if configured (e.g., when human confirmation is enabled)
    from ..context_builder import get_step_language_instruction, get_issue_discovery_injection
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    lang_instruction = get_step_language_instruction("design", project_root)
    if lang_instruction:
        prompt += lang_instruction

    # Append issue discovery injection if applicable
    injection = get_issue_discovery_injection("design", project_root)
    if injection:
        prompt += injection

    logger.info("Generating design document...")

    try:
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count)
        response = caller.call(
            prompt=prompt,
            json_mode="extract",
            json_schema_hint='{"overview": "...", "architecture_decisions": [{"decision": "...", "rationale": "..."}], "components": [{"name": "...", "responsibilities": "..."}], "data_flow": "...", "implementation_plan": [], "testing_strategy": "..."}',
        )

        # Parse JSON response
        design = parse_json_response(response, required_keys=["overview"])

        if not design:
            step.error_message = "Failed to parse design from LLM response"
            return StepStatus.FAILED

        # Store outputs
        step.outputs["design_doc"] = design

        logger.info(f"Design document generated: {design.get('overview', '')[:80]}...")
        logger.debug(f"Architecture decisions: {len(design.get('architecture_decisions', []))}")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Design step failed")
        step.error_message = f"Design generation failed: {str(e)}"
        return StepStatus.FAILED


def _format_proposal(proposal: dict[str, Any]) -> str:
    """Format proposal for inclusion in prompt.

    Args:
        proposal: Proposal dictionary

    Returns:
        Formatted string
    """
    lines = []
    if "summary" in proposal:
        lines.append(f"Summary: {proposal['summary']}")
    if "motivation" in proposal:
        lines.append(f"Motivation: {proposal['motivation']}")
    if "changes" in proposal:
        lines.append(f"Changes: {proposal['changes']}")
    if "files_to_modify" in proposal:
        lines.append(f"Files to modify: {', '.join(proposal['files_to_modify'])}")
    if "files_to_create" in proposal:
        lines.append(f"Files to create: {', '.join(proposal['files_to_create'])}")
    return "\n".join(lines)


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
