"""Plan Tasks step handler.

Breaks down the implementation into concrete, verifiable tasks.
Uses LLM to generate a structured task list.
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


PLAN_TASKS_PROMPT = """You are an expert software engineering assistant. Break down the following implementation into concrete tasks.

## Task Description
{task_description}

## Design Document
{design_doc}

## Proposal
{proposal}

## Instructions
Create a list of concrete, verifiable tasks for implementing this design.

Each task should:
- Have a clear objective that can be verified as complete
- Be small enough to implement in one focused session
- Include specific acceptance criteria
- Have estimated complexity (small/medium/large)

Consider:
- Implementation order (dependencies between tasks)
- Testing requirements for each task
- Files that need to be modified

Respond in JSON format:
```json
{{
    "tasks": [
        {{
            "id": 1,
            "description": "Clear task description",
            "complexity": "small|medium|large",
            "acceptance_criteria": ["criterion 1", "criterion 2"],
            "files": ["file1.py", "file2.py"],
            "depends_on": []
        }}
    ],
    "total_complexity": "small|medium|large",
    "estimated_effort": "brief estimate"
}}
```

Guidelines for complexity:
- small: < 30 minutes, < 50 lines of code
- medium: 30-90 minutes, 50-200 lines of code
- large: > 90 minutes, > 200 lines of code (should be broken down further if possible)
"""


def plan_tasks_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the plan_tasks step.

    Breaks down implementation into concrete tasks using LLM.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    design_doc = step.inputs.get("design_doc", {})
    proposal = step.inputs.get("proposal", {})

    if not design_doc:
        step.error_message = "No design document available from previous step"
        return StepStatus.FAILED

    # Format inputs for prompt
    design_text = _format_design_doc(design_doc)
    proposal_text = _format_proposal(proposal)

    # Build prompt
    prompt = PLAN_TASKS_PROMPT.format(
        task_description=task_description,
        design_doc=design_text,
        proposal=proposal_text,
    )

    logger.info("Generating task list...")

    try:
        # Call LLM for task planning
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        caller = LLMCaller(project_root)
        response = caller.call(prompt=prompt, require_json=True)

        # Parse JSON response
        task_plan = parse_json_response(response, required_keys=["tasks"])

        if not task_plan:
            step.error_message = "Failed to parse task list from LLM response"
            return StepStatus.FAILED

        # Store outputs
        step.outputs["task_list"] = task_plan.get("tasks", [])
        step.outputs["total_complexity"] = task_plan.get("total_complexity", "medium")
        step.outputs["estimated_effort"] = task_plan.get("estimated_effort", "")

        tasks = task_plan.get("tasks", [])
        logger.info(f"Task list generated: {len(tasks)} tasks")
        for task in tasks:
            logger.debug(f"  - [{task.get('complexity', '?')}] {task.get('description', '')[:50]}...")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Plan tasks step failed")
        step.error_message = f"Task planning failed: {str(e)}"
        return StepStatus.FAILED


def _format_design_doc(design_doc: dict[str, Any]) -> str:
    """Format design document for inclusion in prompt.

    Args:
        design_doc: Design document dictionary

    Returns:
        Formatted string
    """
    lines = []
    if "overview" in design_doc:
        lines.append(f"Overview: {design_doc['overview']}")
    if "architecture_decisions" in design_doc:
        lines.append("\nKey Decisions:")
        for decision in design_doc["architecture_decisions"]:
            lines.append(f"- {decision.get('decision', '')}")
    if "components" in design_doc:
        lines.append("\nComponents:")
        for comp in design_doc["components"]:
            lines.append(f"- {comp.get('name', '')}: {comp.get('responsibilities', '')}")
    if "implementation_plan" in design_doc:
        lines.append(f"\nImplementation Plan: {design_doc['implementation_plan']}")
    return "\n".join(lines)


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
    if "files_to_modify" in proposal:
        lines.append(f"Files to modify: {', '.join(proposal['files_to_modify'])}")
    if "files_to_create" in proposal:
        lines.append(f"Files to create: {', '.join(proposal['files_to_create'])}")
    return "\n".join(lines)
