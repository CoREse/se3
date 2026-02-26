"""Implement step handler.

Writes code implementation based on the design and task list.
Uses LLM to generate code changes.
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


IMPLEMENT_PROMPT = """You are an expert software engineer. Implement the following task.

## Task Description
{task_description}

## Design Document
{design_doc}

## Task List
{task_list}

## Current Task
Focus on implementing: {current_task}

## Files to Modify
{files_to_modify}

## Files to Create
{files_to_create}

## Project Context
{project_context}

## Instructions
Write clean, correct, well-tested code following these guidelines:

1. **Correctness**: Code must work as specified in the design
2. **Clarity**: Write readable code with clear intent
3. **Consistency**: Follow existing project conventions
4. **Error Handling**: Handle edge cases and errors appropriately
5. **Testing**: Include tests where applicable

For each file you create or modify, provide:
- The file path
- The complete file content (or changes for modifications)
- Brief explanation of what changed

Respond in JSON format:
```json
{{
    "files_changed": [
        {{
            "path": "path/to/file.py",
            "action": "create|modify",
            "content": "complete file content",
            "explanation": "what this file does"
        }}
    ],
    "tests_added": ["test_file.py"],
    "notes": "any important notes"
}}
```

IMPORTANT:
- Provide COMPLETE file content, not diffs
- Ensure code is syntactically correct
- Follow the project's existing style
"""


def implement_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the implement step.

    Generates code implementation using LLM based on design and tasks.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    design_doc = step.inputs.get("design_doc", {})
    task_list = step.inputs.get("task_list", [])
    proposal = step.inputs.get("proposal", {})

    # For small tasks or when design is skipped, create minimal design from proposal/task
    if not design_doc:
        design_doc = {
            "overview": task_description,
            "components": [],
            "architecture_decisions": [],
        }
        # Try to extract files from proposal if available
        if proposal:
            files_to_modify = proposal.get("files_to_modify", [])
            files_to_create = proposal.get("files_to_create", [])
            if files_to_modify or files_to_create:
                design_doc["components"] = [{
                    "name": "Implementation",
                    "responsibilities": f"Modify: {files_to_modify}, Create: {files_to_create}",
                    "interfaces": [],
                }]

    # Get current task to focus on
    current_task = _get_current_task(task_list)

    # Format inputs
    design_text = _format_design_doc(design_doc)
    task_text = _format_task_list(task_list)
    project_context = _gather_project_context(flow)

    files_to_modify = proposal.get("files_to_modify", [])
    files_to_create = proposal.get("files_to_create", [])

    # Build prompt
    prompt = IMPLEMENT_PROMPT.format(
        task_description=task_description,
        design_doc=design_text,
        task_list=task_text,
        current_task=current_task,
        files_to_modify=files_to_modify,
        files_to_create=files_to_create,
        project_context=project_context,
    )

    logger.info("Generating implementation...")

    try:
        # Call LLM for implementation
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        caller = LLMCaller(project_root)
        response = caller.call(prompt=prompt)

        # Parse JSON response
        implementation = parse_json_response(response, required_keys=["files_changed"])

        if not implementation:
            step.error_message = "Failed to parse implementation from LLM response"
            return StepStatus.FAILED

        # Store outputs
        step.outputs["implementation"] = implementation
        step.outputs["files_changed"] = implementation.get("files_changed", [])
        step.outputs["tests_added"] = implementation.get("tests_added", [])

        # Apply the changes to files
        _apply_changes(flow, implementation.get("files_changed", []))

        files_changed = implementation.get("files_changed", [])
        logger.info(f"Implementation complete: {len(files_changed)} files changed")
        for f in files_changed:
            logger.debug(f"  - {f.get('path', '?')} ({f.get('action', '?')})")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Implement step failed")
        step.error_message = f"Implementation failed: {str(e)}"
        return StepStatus.FAILED


def _get_current_task(task_list: list[dict[str, Any]]) -> str:
    """Get the current task to focus on.

    Args:
        task_list: List of tasks

    Returns:
        Description of current task or generic message
    """
    if not task_list:
        return "All tasks"

    # Find first incomplete task (for now, just return first)
    # In a more sophisticated version, we'd track completion
    return task_list[0].get("description", "Current task")


def _format_design_doc(design_doc: dict[str, Any]) -> str:
    """Format design document for inclusion in prompt."""
    lines = []
    if "overview" in design_doc:
        lines.append(f"Overview: {design_doc['overview']}")
    if "components" in design_doc:
        lines.append("\nComponents:")
        for comp in design_doc["components"]:
            lines.append(f"- {comp.get('name', '')}: {comp.get('responsibilities', '')}")
            if comp.get("interfaces"):
                lines.append(f"  Interfaces: {comp['interfaces']}")
    if "architecture_decisions" in design_doc:
        lines.append("\nKey Decisions:")
        for decision in design_doc["architecture_decisions"]:
            lines.append(f"- {decision.get('decision', '')}: {decision.get('rationale', '')}")
    return "\n".join(lines)


def _format_task_list(task_list: list[dict[str, Any]]) -> str:
    """Format task list for inclusion in prompt."""
    if not task_list:
        return "No specific task list provided."

    lines = []
    for task in task_list:
        task_id = task.get("id", "?")
        desc = task.get("description", "")
        complexity = task.get("complexity", "?")
        lines.append(f"{task_id}. [{complexity}] {desc}")
    return "\n".join(lines)


def _gather_project_context(flow: FlowInstance) -> str:
    """Gather relevant project context."""
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    context_parts = []

    # Check for common project indicators
    if (project_root / "pyproject.toml").exists():
        context_parts.append("Python project (pyproject.toml)")
    elif (project_root / "package.json").exists():
        context_parts.append("Node.js project (package.json)")

    # Check for test framework
    if (project_root / "pytest.ini").exists():
        context_parts.append("Uses pytest for testing")

    return "; ".join(context_parts) if context_parts else "Standard project"


def _apply_changes(flow: FlowInstance, files_changed: list[dict[str, Any]]) -> None:
    """Apply file changes to the project.

    Args:
        flow: The flow instance
        files_changed: List of file change dictionaries
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    for file_change in files_changed:
        try:
            path_str = file_change.get("path", "")
            action = file_change.get("action", "")
            content = file_change.get("content", "")

            if not path_str:
                continue

            file_path = project_root / path_str

            if action == "create":
                # Ensure parent directory exists
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(content, encoding="utf-8")
                logger.info(f"Created file: {path_str}")

            elif action == "modify":
                if file_path.exists():
                    file_path.write_text(content, encoding="utf-8")
                    logger.info(f"Modified file: {path_str}")
                else:
                    logger.warning(f"File to modify does not exist: {path_str}")

        except Exception as e:
            logger.error(f"Failed to apply change to {file_change.get('path', '?')}: {e}")
