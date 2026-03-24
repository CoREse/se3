"""Read Spec step handler.

Uses LLM to intelligently select relevant specs from the specs directory.
The LLM has access to file system tools and can browse spec content to
determine which specs are relevant to the current task.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..context_builder import ContextBuilder
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)


READ_SPEC_PROMPT = """You are the spec selector for the SE3 flow engine.

Your job: browse the specs directory and select which specifications are relevant to the current task.

## Specs Directory
{specs_dir}

## Task Description
{task_description}

## Task Type
{task_type}

## Scope
{scope}

## Project Context
{project_summary}

## Instructions

1. Use the Glob tool to list directories in the specs directory (each subdirectory is a spec).
2. For specs that look potentially relevant based on their name, use the Read tool to read their spec.md file.
3. Based on the content, decide which specs are truly relevant to this task.
4. Return your selection.

Respond with ONLY this JSON (no other text):
{{
    "selected_specs": ["spec-name-1", "spec-name-2"],
    "reasoning": "Brief explanation of why these specs were selected"
}}

Important:
- Skip directories starting with "_" (internal directories like _changelog, _backlog).
- Be selective — only include specs that are genuinely relevant to the task.
- If no specs are relevant, return an empty list.
"""


def read_spec_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the read_spec step.

    Uses LLM to browse specs directory and select relevant specs.
    Then loads their full content programmatically.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_type = step.inputs.get("task_type", "feature")
    scope = step.inputs.get("scope", "")
    project_summary = step.inputs.get("project_summary", "Not available")

    # Determine project root
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    try:
        # Initialize context builder for spec loading
        builder = ContextBuilder(project_root)
        specs_dir = str(builder.specs_dir.resolve())

        # Auto-load base spec if it exists (before LLM selection)
        spec_contents: dict[str, str] = {}
        relevant_specs: list[str] = []

        base_spec_content = builder._load_spec_content("base")
        if base_spec_content:
            spec_contents["base"] = base_spec_content
            relevant_specs.append("base")
            logger.info(f"Auto-loaded base spec ({len(base_spec_content)} chars)")

        # Build prompt for LLM
        prompt = READ_SPEC_PROMPT.format(
            specs_dir=specs_dir,
            task_description=flow.task_description,
            task_type=task_type,
            scope=scope,
            project_summary=project_summary,
        )

        logger.info(f"LLM-based spec selection for: {flow.task_description[:60]}...")

        # Call LLM for spec selection
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count)
        response = caller.call(prompt=prompt, require_json=True)

        # Parse LLM response
        result = parse_json_response(response, required_keys=["selected_specs"])

        if result:
            llm_specs = result.get("selected_specs", [])
            reasoning = result.get("reasoning", "")
            logger.info(f"LLM selected specs: {llm_specs} — {reasoning}")
            for spec in llm_specs:
                if spec not in relevant_specs:
                    relevant_specs.append(spec)
        else:
            logger.warning("Failed to parse LLM spec selection response, using empty list")

        # Merge with analyze step's required_specs
        if flow.state.context.get("required_specs"):
            additional_specs = flow.state.context["required_specs"]
            for spec in additional_specs:
                if spec not in relevant_specs:
                    relevant_specs.append(spec)

        logger.info(f"Final spec selection: {len(relevant_specs)} specs: {relevant_specs}")

        # Load spec content for non-base specs (base already loaded)
        for spec_name in relevant_specs:
            if spec_name in spec_contents:
                continue
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

        # Build summary
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
