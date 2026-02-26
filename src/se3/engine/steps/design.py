"""Design step handler.

Creates a design document with architecture decisions based on the proposal.
Uses LLM to generate structured design documentation.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..llm_caller import LLMCaller, LLMCallError
from ..models import FlowInstance, Step, StepStatus

logger = logging.getLogger(__name__)


DESIGN_PROMPT = """You are an expert software architect. Create a design document for the following implementation.

## Task Description
{task_description}

## Proposal
{proposal}

## Relevant Specifications
{spec_content}

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


def design_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the design step.

    Creates a design document using LLM based on proposal and specs.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    proposal = step.inputs.get("proposal", {})
    spec_content = step.inputs.get("spec_content", {})

    if not proposal:
        step.error_message = "No proposal available from previous step"
        return StepStatus.FAILED

    # Format inputs for prompt
    proposal_text = _format_proposal(proposal)
    spec_text = _format_spec_content(spec_content)

    # Build prompt
    prompt = DESIGN_PROMPT.format(
        task_description=task_description,
        proposal=proposal_text,
        spec_content=spec_text,
    )

    logger.info("Generating design document...")

    try:
        # Call LLM for design
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        caller = LLMCaller(project_root)
        response = caller.call(prompt=prompt)

        # Parse JSON response
        design = _parse_design_response(response)

        if not design:
            step.error_message = "Failed to parse design from LLM response"
            return StepStatus.FAILED

        # Store outputs
        step.outputs["design_doc"] = design
        step.outputs["decisions"] = design.get("architecture_decisions", [])
        step.outputs["components"] = design.get("components", [])
        step.outputs["implementation_plan"] = design.get("implementation_plan", [])

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
        # Truncate very long specs
        if len(content) > 2000:
            content = content[:2000] + "\n... [truncated for brevity]"
        parts.append(content)
        parts.append("")

    return "\n".join(parts)


def _parse_design_response(response: str) -> dict[str, Any] | None:
    """Parse the LLM response into a structured design document.

    Args:
        response: Raw LLM response string

    Returns:
        Parsed dictionary or None if parsing fails
    """
    try:
        # Try to extract JSON from the response
        response = response.strip()

        # Remove markdown code block if present
        if response.startswith("```json"):
            response = response[7:]
        elif response.startswith("```"):
            response = response[3:]

        if response.endswith("```"):
            response = response[:-3]

        response = response.strip()

        # Try to find JSON object boundaries
        # Handle case where LLM adds extra text after JSON
        json_start = response.find('{')
        json_end = response.rfind('}')

        if json_start == -1 or json_end == -1 or json_end <= json_start:
            logger.warning("No JSON object found in response")
            return None

        # Extract just the JSON part
        json_str = response[json_start:json_end + 1]

        # Parse JSON
        result = json.loads(json_str)

        # Validate required fields
        if "overview" not in result:
            logger.warning("Missing 'overview' in design response")
            return None

        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        # Log the problematic response for debugging
        logger.debug(f"Response content: {response[:500]}...")
        return None
    except Exception as e:
        logger.error(f"Unexpected error parsing response: {e}")
        return None
