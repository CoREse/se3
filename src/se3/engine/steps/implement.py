"""Implement step handler.

Writes code implementation based on the design and task groups.
Each task group is implemented in a separate LLM call with isolated context.
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


IMPLEMENT_GROUP_PROMPT = """You are an expert software engineer. Implement the following task group.

## Task Description
{task_description}

## Design Document
{design_doc}

## Proposal
{proposal}

## Current Task Group
Group: {group_name} ({group_id})
Description: {group_description}

### Tasks in This Group
{tasks}

## Files to Modify
{files_to_modify}

## Files to Create
{files_to_create}

## Project Context
{project_context}

## Important Notes
- You are implementing **only** this task group, not the entire project
- Other groups will be implemented separately with their own isolated contexts
- Focus only on the tasks listed above
- Do not assume knowledge of implementation details from other groups
- If a task depends on code from another group, assume it exists as specified in the design

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
- Do not reference or depend on implementation details from other task groups
"""


def implement_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the implement step.

    Implements each task group in a separate LLM call with isolated context.
    Each group has its own retry mechanism. Groups are executed sequentially.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    design_doc = step.inputs.get("design_doc", {})
    task_groups = step.inputs.get("task_groups", [])
    proposal = step.inputs.get("proposal", {})

    # Support legacy task_list format for backward compatibility
    if not task_groups:
        task_list = step.inputs.get("task_list", [])
        if task_list:
            task_groups = [{
                "group_id": "G1",
                "name": "Implementation",
                "description": "All implementation tasks",
                "group_order": 1,
                "depends_on": [],
                "tasks": task_list,
            }]

    # If still no task groups, create a default one from task description
    # This handles cases where PLAN_TASKS step was skipped or failed
    if not task_groups:
        # Extract files from proposal if available
        files_to_modify = proposal.get("files_to_modify", []) if proposal else []
        files_to_create = proposal.get("files_to_create", []) if proposal else []
        all_files = files_to_modify + files_to_create
        
        # Create a single default task
        default_task = {
            "id": 1,
            "description": task_description,
            "complexity": "medium",
            "acceptance_criteria": ["Implementation complete", "Code works as expected"],
            "files": all_files if all_files else [],
            "depends_on": [],
        }
        
        task_groups = [{
            "group_id": "G1",
            "name": "Implementation",
            "description": f"Implement: {task_description[:100]}..." if len(task_description) > 100 else f"Implement: {task_description}",
            "group_order": 1,
            "depends_on": [],
            "tasks": [default_task],
        }]
        
        logger.info(f"Created default task group for: {task_description[:50]}...")

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

    # Sort groups by order
    sorted_groups = sorted(task_groups, key=lambda g: g.get("group_order", 0))

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    project_context = _gather_project_context(flow)

    all_results = []
    total_files_changed = 0

    logger.info(f"Starting implementation of {len(sorted_groups)} task groups")

    for group in sorted_groups:
        group_id = group.get("group_id", "unknown")
        group_name = group.get("name", f"Group {group_id}")

        print(f"\n{'='*60}")
        print(f"📦 IMPLEMENTING TASK GROUP: {group_name} ({group_id})")
        print(f"{'='*60}")

        # Execute group with retry logic
        result = _implement_group_with_retry(
            group=group,
            task_description=task_description,
            design_doc=design_doc,
            proposal=proposal,
            project_context=project_context,
            project_root=project_root,
            flow=flow,
            step=step,
        )

        if result is None:
            # Failed after max retries
            step.error_message = f"Failed to implement task group {group_id} after max retries"
            step.outputs["implemented_groups"] = all_results
            step.outputs["failed_group"] = group_id
            return StepStatus.FAILED

        # Apply changes immediately for this group
        files_changed = result.get("files_changed", [])
        _apply_changes(flow, files_changed)

        all_results.append({
            "group_id": group_id,
            "group_name": group_name,
            "result": result,
            "files_changed_count": len(files_changed),
        })
        total_files_changed += len(files_changed)

        print(f"✓ Group {group_id} complete: {len(files_changed)} files changed")
        logger.info(f"Task group {group_id} complete: {len(files_changed)} files changed")

    # Store outputs
    step.outputs["implemented_groups"] = all_results
    step.outputs["files_changed"] = _aggregate_files_changed(all_results)
    step.outputs["tests_added"] = _aggregate_tests_added(all_results)
    step.outputs["total_groups"] = len(sorted_groups)
    step.outputs["total_files_changed"] = total_files_changed

    print(f"\n{'='*60}")
    print(f"✅ ALL TASK GROUPS COMPLETE")
    print(f"   Total groups: {len(sorted_groups)}")
    print(f"   Total files changed: {total_files_changed}")
    print(f"{'='*60}")

    logger.info(f"Implementation complete: {len(sorted_groups)} groups, {total_files_changed} files changed")

    return StepStatus.COMPLETED


def _implement_group_with_retry(
    group: dict[str, Any],
    task_description: str,
    design_doc: dict[str, Any],
    proposal: dict[str, Any],
    project_context: str,
    project_root: Path,
    flow: FlowInstance,
    step: Step,
) -> dict[str, Any] | None:
    """Implement a single task group with retry logic.

    Each group gets its own isolated LLM call with the same base context.
    Retries inherit the same context.

    Args:
        group: The task group to implement
        task_description: Overall task description
        design_doc: Design document
        proposal: Proposal
        project_context: Project context
        project_root: Project root path
        flow: Flow instance
        step: Current step

    Returns:
        Implementation result dict, or None if failed after max retries
    """
    group_id = group.get("group_id", "unknown")
    max_retries = step.max_retries

    for attempt in range(max_retries + 1):
        if attempt > 0:
            print(f"\n🔁 Retrying group {group_id} (attempt {attempt + 1}/{max_retries + 1})...")
            logger.info(f"Retrying group {group_id} (attempt {attempt + 1})")

        try:
            result = _implement_single_group(
                group=group,
                task_description=task_description,
                design_doc=design_doc,
                proposal=proposal,
                project_context=project_context,
                project_root=project_root,
                flow=flow,
                step=step,
                attempt=attempt,
            )

            if result:
                return result

        except Exception as e:
            logger.exception(f"Group {group_id} implementation failed on attempt {attempt + 1}")

        # If not the last attempt, continue to retry
        if attempt < max_retries:
            print(f"   ⚠️  Attempt {attempt + 1} failed, will retry...")
        else:
            print(f"   ❌ All {max_retries + 1} attempts failed for group {group_id}")

    return None


def _implement_single_group(
    group: dict[str, Any],
    task_description: str,
    design_doc: dict[str, Any],
    proposal: dict[str, Any],
    project_context: str,
    project_root: Path,
    flow: FlowInstance,
    step: Step,
    attempt: int,
) -> dict[str, Any] | None:
    """Execute a single implementation attempt for a task group.

    Args:
        group: The task group to implement
        task_description: Overall task description
        design_doc: Design document
        proposal: Proposal
        project_context: Project context
        project_root: Project root path
        flow: Flow instance
        step: Current step
        attempt: Current attempt number (0-based)

    Returns:
        Implementation result dict, or None if failed
    """
    group_id = group.get("group_id", "unknown")
    group_name = group.get("name", f"Group {group_id}")
    group_description = group.get("description", "")
    tasks = group.get("tasks", [])

    # Format inputs
    design_text = _format_design_doc(design_doc)
    tasks_text = _format_tasks(tasks)
    files_to_modify = proposal.get("files_to_modify", [])
    files_to_create = proposal.get("files_to_create", [])

    # Build prompt - same context for each group, but only this group's tasks
    prompt = IMPLEMENT_GROUP_PROMPT.format(
        task_description=task_description,
        design_doc=design_text,
        proposal=_format_proposal(proposal),
        group_id=group_id,
        group_name=group_name,
        group_description=group_description,
        tasks=tasks_text,
        files_to_modify=files_to_modify,
        files_to_create=files_to_create,
        project_context=project_context,
    )

    logger.info(f"Generating implementation for group {group_id} (attempt {attempt + 1})...")

    # Call LLM for implementation
    # Use consistent step_id for all attempts of the same group so that retries inherit context
    group_step_id = f"{step.step_id}-{group_id}"
    caller = LLMCaller(
        project_root,
        flow_id=flow.flow_id,
        step_id=group_step_id,
        step_type=f"implement-{group_id}",
        external_attempt=attempt,  # Pass attempt so retries inject history context
    )
    # Use two-phase JSON extraction for better reliability with large outputs
    response = caller.call(
        prompt=prompt,
        two_phase_json=True,
        json_schema_hint='{"files_changed": [{"path": "...", "action": "create|modify", "content": "...", "explanation": "..."}], "tests_added": [], "notes": "..."}',
    )

    # Parse JSON response
    implementation = parse_json_response(response, required_keys=["files_changed"])

    if not implementation:
        logger.warning(f"Failed to parse implementation for group {group_id}")
        return None

    files_changed = implementation.get("files_changed", [])
    logger.info(f"Group {group_id} implementation generated: {len(files_changed)} files")

    return implementation


def _format_tasks(tasks: list[dict[str, Any]]) -> str:
    """Format tasks for inclusion in prompt."""
    if not tasks:
        return "No tasks in this group."

    lines = []
    for task in tasks:
        task_id = task.get("id", "?")
        desc = task.get("description", "")
        complexity = task.get("complexity", "?")
        files = task.get("files", [])
        criteria = task.get("acceptance_criteria", [])

        lines.append(f"### Task {task_id}")
        lines.append(f"**Description:** {desc}")
        lines.append(f"**Complexity:** {complexity}")
        if files:
            lines.append(f"**Files:** {', '.join(files)}")
        if criteria:
            lines.append(f"**Acceptance Criteria:**")
            for c in criteria:
                lines.append(f"  - {c}")
        lines.append("")

    return "\n".join(lines)


def _aggregate_files_changed(all_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate files_changed from all group results."""
    all_files = []
    for result in all_results:
        group_result = result.get("result", {})
        files = group_result.get("files_changed", [])
        all_files.extend(files)
    return all_files


def _aggregate_tests_added(all_results: list[dict[str, Any]]) -> list[str]:
    """Aggregate tests_added from all group results."""
    all_tests = []
    for result in all_results:
        group_result = result.get("result", {})
        tests = group_result.get("tests_added", [])
        all_tests.extend(tests)
    return all_tests


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


def _format_proposal(proposal: dict[str, Any]) -> str:
    """Format proposal for inclusion in prompt."""
    lines = []
    if "summary" in proposal:
        lines.append(f"Summary: {proposal['summary']}")
    if "files_to_modify" in proposal:
        lines.append(f"Files to modify: {', '.join(proposal['files_to_modify'])}")
    if "files_to_create" in proposal:
        lines.append(f"Files to create: {', '.join(proposal['files_to_create'])}")
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
