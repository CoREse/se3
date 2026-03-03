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


PLAN_TASKS_PROMPT = """You are an expert software engineering assistant. Break down the following implementation into logical task groups.

## Task Description
{task_description}

## Design Document
{design_doc}

## Proposal
{proposal}

{revision_section}

## Instructions
Create a structured task plan organized into **logical task groups**. Each group contains highly related tasks that should be implemented together in a single context.

### Grouping Principles
- **High cohesion within groups**: Tasks in the same group should be logically related (e.g., same component, same data flow, same abstraction layer)
- **Low coupling between groups**: Groups should be as independent as possible
- **Clear dependencies**: Use `depends_on` to express inter-group dependencies
- **Sequential execution**: Groups will be executed in order, so later groups can assume earlier groups are complete

### Task Structure (within each group)
Each task should:
- Have a clear objective that can be verified as complete
- Be small enough to implement in one focused session
- Include specific acceptance criteria
- Have estimated complexity (small/medium/large)

Guidelines for complexity:
- small: < 30 minutes, < 50 lines of code
- medium: 30-90 minutes, 50-200 lines of code
- large: > 90 minutes, > 200 lines of code (should be broken down further if possible)

Respond in JSON format:
```json
{{
    "task_groups": [
        {{
            "group_id": "G1",
            "name": "Group name describing the focus",
            "description": "What this group accomplishes and why these tasks are grouped together",
            "group_order": 1,
            "depends_on": [],
            "tasks": [
                {{
                    "id": 1,
                    "description": "Clear task description",
                    "complexity": "small|medium|large",
                    "acceptance_criteria": ["criterion 1", "criterion 2"],
                    "files": ["file1.py", "file2.py"],
                    "depends_on": []
                }}
            ]
        }},
        {{
            "group_id": "G2",
            "name": "Another group",
            "description": "...",
            "group_order": 2,
            "depends_on": ["G1"],
            "tasks": [...]
        }}
    ],
    "total_complexity": "small|medium|large",
    "estimated_effort": "brief estimate"
}}
```

Important:
- `group_id` should be unique (G1, G2, G3...)
- `group_order` determines execution sequence (1, 2, 3...)
- `depends_on` lists group_ids that must complete before this group
- Each group will be implemented in a **separate LLM call with isolated context**
- Groups should not assume knowledge of other groups' implementation details
"""

REVISION_SECTION = """
## Previous Task Plan Feedback
The previous task plan was reviewed and requires changes:

{revision_feedback}

Please revise the task plan addressing the feedback above.
"""


def plan_tasks_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the plan_tasks step.

    Breaks down implementation into concrete tasks using LLM.
    Supports revision mode when revision_feedback is provided.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    design_doc = step.inputs.get("design_doc", {})
    proposal = step.inputs.get("proposal", {})
    revision_feedback = step.inputs.get("revision_feedback", "")
    is_revision = step.inputs.get("is_revision", False)

    # If no design_doc available, create a minimal one from proposal or task description
    # This handles cases where DESIGN step was skipped (e.g., directive task type)
    if not design_doc:
        logger.info("No design document available, creating minimal design from context")
        
        # Try to extract information from proposal
        if proposal:
            files_to_modify = proposal.get("files_to_modify", [])
            files_to_create = proposal.get("files_to_create", [])
            summary = proposal.get("summary", task_description)
            
            design_doc = {
                "overview": summary,
                "components": [{
                    "name": "Implementation",
                    "responsibilities": f"Modify: {files_to_modify}, Create: {files_to_create}" if files_to_modify or files_to_create else "Implement required changes",
                    "interfaces": [],
                }],
                "architecture_decisions": [],
            }
        else:
            # Minimal design from task description only
            design_doc = {
                "overview": task_description,
                "components": [{
                    "name": "Implementation",
                    "responsibilities": "Implement required changes as specified in task description",
                    "interfaces": [],
                }],
                "architecture_decisions": [],
            }

    # Format inputs for prompt
    design_text = _format_design_doc(design_doc)
    proposal_text = _format_proposal(proposal)

    # Build revision section if this is a revision
    if is_revision and revision_feedback:
        revision_section = REVISION_SECTION.format(revision_feedback=revision_feedback)
    else:
        revision_section = ""

    # Build prompt
    prompt = PLAN_TASKS_PROMPT.format(
        task_description=task_description,
        design_doc=design_text,
        proposal=proposal_text,
        revision_section=revision_section,
    )

    logger.info("Generating task list...")

    try:
        # Call LLM for task planning (use EXTRACT mode for deeply nested structures)
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value)
        response = caller.call(
            prompt=prompt,
            json_mode="extract",
            json_schema_hint='{"task_groups": [{"group_id": "G1", "name": "...", "tasks": [{"id": 1, "description": "...", "complexity": "..."}]}], "total_complexity": "..."}',
        )

        # Parse JSON response
        task_plan = parse_json_response(response, required_keys=["task_groups"])

        if not task_plan:
            step.error_message = "Failed to parse task groups from LLM response"
            return StepStatus.FAILED

        # Store outputs
        task_groups = task_plan.get("task_groups", [])
        step.outputs["task_groups"] = task_groups
        step.outputs["total_complexity"] = task_plan.get("total_complexity", "medium")
        step.outputs["estimated_effort"] = task_plan.get("estimated_effort", "")

        # Also flatten tasks for backward compatibility
        all_tasks = []
        for group in task_groups:
            all_tasks.extend(group.get("tasks", []))
        step.outputs["task_list"] = all_tasks

        logger.info(f"Task groups generated: {len(task_groups)} groups, {len(all_tasks)} total tasks")
        for task in all_tasks:
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
