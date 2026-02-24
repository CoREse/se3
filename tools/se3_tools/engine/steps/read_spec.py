"""Read Spec step handler.

Programmatically reads relevant OpenSpec specifications based on
the task type and scope determined in the analyze step.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..context_builder import ContextBuilder
from ..models import FlowInstance, Step, StepStatus

logger = logging.getLogger(__name__)


def read_spec_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the read_spec step.

    Reads relevant OpenSpec specifications and stores their content.
    This is a non-LLM step - it performs file operations only.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_type = step.inputs.get("task_type", "feature")
    scope = step.inputs.get("scope", "")

    # Determine project root
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    try:
        # Initialize context builder
        builder = ContextBuilder(project_root)

        # Get relevant specs based on task description and analysis
        task_description = flow.task_description
        relevant_specs = builder.find_relevant_specs(task_description)

        # Also check if analyze step suggested specific specs
        if flow.state.context.get("required_specs"):
            additional_specs = flow.state.context["required_specs"]
            for spec in additional_specs:
                if spec not in relevant_specs:
                    relevant_specs.append(spec)

        logger.info(f"Found {len(relevant_specs)} relevant specs: {relevant_specs}")

        # Load spec content
        spec_contents: dict[str, str] = {}
        for spec_name in relevant_specs:
            content = builder._load_spec_content(spec_name)
            if content:
                spec_contents[spec_name] = content
                logger.debug(f"Loaded spec: {spec_name} ({len(content)} chars)")
            else:
                logger.warning(f"Could not load spec: {spec_name}")

        # Store outputs
        step.outputs["relevant_specs"] = relevant_specs
        step.outputs["spec_content"] = spec_contents
        step.outputs["spec_count"] = len(spec_contents)

        # Build a summary for context
        summary = _build_spec_summary(spec_contents, task_type)
        step.outputs["spec_summary"] = summary

        logger.info(f"Read spec step complete: {len(spec_contents)} specs loaded")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Read spec step failed")
        step.error_message = f"Failed to read specs: {str(e)}"
        return StepStatus.FAILED


def _build_spec_summary(spec_contents: dict[str, str], task_type: str) -> str:
    """Build a summary of specifications for context.

    Args:
        spec_contents: Dictionary of spec name -> content
        task_type: Type of task

    Returns:
        Formatted summary string
    """
    if not spec_contents:
        return "No relevant specifications found."

    lines = [f"## Relevant Specifications for {task_type} task", ""]

    for spec_name, content in spec_contents.items():
        lines.append(f"### {spec_name}")
        lines.append("")

        # Extract key sections (first 500 chars or first section)
        lines_preview = content.split("\n")[:30]  # First 30 lines
        preview = "\n".join(lines_preview)

        if len(preview) > 2000:
            preview = preview[:2000] + "\n... [truncated]"

        lines.append(preview)
        lines.append("")

    return "\n".join(lines)
