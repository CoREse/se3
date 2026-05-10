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
    task_description = step.inputs.get("task_description", flow.task_description)
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
            task_description=task_description,
            task_type=task_type,
            scope=scope,
            project_summary=project_summary,
        )

        # Append issue discovery injection if applicable
        from ..context_builder import get_issue_discovery_injection
        injection = get_issue_discovery_injection("read_spec", project_root)
        if injection:
            prompt += injection

        logger.info(f"LLM-based spec selection for: {task_description[:60]}...")

        # Call LLM for spec selection
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count, fix_iteration=step.inputs.get("fix_iteration", 0))
        response = caller.call(prompt=prompt, json_mode="two_phase")

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

        logger.info(f"Read spec step complete: {len(spec_contents)} specs loaded")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Read spec step failed")
        step.error_message = f"Failed to read specs: {str(e)}"
        return StepStatus.FAILED


