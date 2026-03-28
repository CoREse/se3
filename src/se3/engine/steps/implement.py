"""Implement step handler.

Executes implementation of task groups, writing code to files.
Uses LLM (claude -p) with TWO_PHASE JSON extraction.
Supports fix iterations for the test-verify-fix loop.
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


IMPLEMENT_PROMPT = """You are an expert software engineer. Implement the following tasks by writing code.

## Task Description
{task_description}

## Task Type
{task_type}

{design_section}

## Task Groups
{task_groups}

## Project Conventions
{spec_summary}

## Instructions
1. Read the relevant source files before making changes.
2. Implement each task in the task groups above.
3. Follow the project's coding conventions.
4. Write tests if the task requires them.
5. Do NOT commit — only write/edit files.

When you are done, output a JSON summary of what you did:
```json
{{
    "files_changed": ["path/to/file1.py", "path/to/file2.py"],
    "tests_added": ["tests/test_new.py"],
    "test_mapping": {{}},
    "summary": "Brief description of changes made",
    "completion_status": "complete",
    "incomplete_tasks": [],
    "restricted_edits": []
}}
```

### Response field notes:
- **completion_status**: Set to "complete" if all tasks were done, "partial" if some tasks could not be completed (e.g., permission restrictions on sensitive files), or "failed" if no meaningful progress was made.
- **incomplete_tasks**: An array of strings, each describing a task that could not be completed and why. Only populate when completion_status is "partial" or "failed".
- **restricted_edits**: An array of edits you attempted but could NOT perform due to file permission/protection restrictions (e.g., files under `.claude/` directory). Each entry must be: {{"file_path": "path/to/file", "old_string": "text to replace", "new_string": "replacement text"}}. Always attempt edits normally first — only use this field for edits that were rejected by the permission system.
"""

IMPLEMENT_GROUP_PROMPT = """You are an expert software engineer. Implement the tasks for this specific group by writing code.

## Task Description
{task_description}

## Task Type
{task_type}

{design_section}

## Current Group Tasks
{current_group}

## Previous Groups Context
{previous_results}

## Project Conventions
{spec_summary}

## Instructions
1. Read the relevant source files before making changes.
2. Implement the tasks listed in Current Group Tasks above.
3. Follow the project's coding conventions.
4. Write tests if the task requires them.
5. Do NOT commit — only write/edit files.

When you are done, output a JSON summary of what you did:
```json
{{
    "files_changed": ["path/to/file1.py", "path/to/file2.py"],
    "tests_added": ["tests/test_new.py"],
    "test_mapping": {{}},
    "summary": "Brief description of changes made",
    "completion_status": "complete",
    "incomplete_tasks": [],
    "restricted_edits": []
}}
```

### Response field notes:
- **completion_status**: Set to "complete" if all tasks were done, "partial" if some tasks could not be completed (e.g., permission restrictions on sensitive files), or "failed" if no meaningful progress was made.
- **incomplete_tasks**: An array of strings, each describing a task that could not be completed and why. Only populate when completion_status is "partial" or "failed".
- **restricted_edits**: An array of edits you attempted but could NOT perform due to file permission/protection restrictions (e.g., files under `.claude/` directory). Each entry must be: {{"file_path": "path/to/file", "old_string": "text to replace", "new_string": "replacement text"}}. Always attempt edits normally first — only use this field for edits that were rejected by the permission system.
"""

FIX_PROMPT = """You are an expert software engineer. Fix the issues found in the previous implementation.

## Task Description
{task_description}

## Fix Instructions
{fix_instructions}

## Fix Context
{fix_context}

## Fix Iteration
This is fix iteration {fix_iteration}.

## Instructions
1. Read the failing test output and error messages carefully.
2. Fix the root cause — do not just suppress errors.
3. Run the relevant tests mentally to verify your fix.
4. Do NOT commit — only write/edit files.

When you are done, output a JSON summary of what you did:
```json
{{
    "files_changed": ["path/to/file1.py"],
    "tests_added": [],
    "test_mapping": {{}},
    "summary": "Brief description of fix",
    "completion_status": "complete",
    "incomplete_tasks": [],
    "restricted_edits": []
}}
```

### Response field notes:
- **completion_status**: Set to "complete" if all issues were fixed, "partial" if some fixes could not be applied (e.g., permission restrictions on sensitive files), or "failed" if no meaningful progress was made.
- **incomplete_tasks**: An array of strings, each describing a fix that could not be applied and why.
- **restricted_edits**: An array of edits you attempted but could NOT perform due to file permission/protection restrictions (e.g., files under `.claude/` directory). Each entry must be: {{"file_path": "path/to/file", "old_string": "text to replace", "new_string": "replacement text"}}. Always attempt edits normally first — only use this field for edits that were rejected by the permission system.
"""


def implement_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the implement step.

    Calls LLM via claude -p to actually write/edit source files.
    Uses TWO_PHASE JSON mode: LLM writes code naturally, then we
    extract the JSON summary of what was changed.

    In fix iterations, focuses on fixing issues identified by verify_spec.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    task_type = step.inputs.get("task_type", "feature")
    task_groups = step.inputs.get("task_groups") or step.inputs.get("task_list", [])
    design_doc = step.inputs.get("design_doc", {})
    spec_content = step.inputs.get("spec_content", {})
    fix_context = step.inputs.get("fix_context")
    fix_instructions = step.inputs.get("fix_instructions")
    is_fix_iteration = step.inputs.get("is_fix_iteration", False)
    fix_iteration = step.inputs.get("fix_iteration", 0)

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Format design section (shared across paths)
    design_section = ""
    if design_doc:
        if isinstance(design_doc, dict):
            design_section = "## Design Document\n" + json.dumps(
                design_doc, indent=2, ensure_ascii=False
            )
        else:
            design_section = f"## Design Document\n{design_doc}"

    spec_summary = _format_spec_brief(spec_content)

    # Append issue discovery injection if applicable
    from ..context_builder import get_issue_discovery_injection
    injection = get_issue_discovery_injection("implement", project_root)

    retry_count = step.inputs.get("retry_count", 0)

    # Build the prompt
    if is_fix_iteration and fix_instructions:
        logger.info(f"Running fix iteration {fix_iteration}")
        prompt = FIX_PROMPT.format(
            task_description=task_description,
            fix_instructions=fix_instructions,
            fix_context=fix_context or "No additional context.",
            fix_iteration=fix_iteration,
        )
        if injection:
            prompt += injection

        return _run_single_llm_call(
            prompt, step, flow, project_root, task_groups, retry_count,
        )

    # Determine if we should use group-by-group execution
    groups = _extract_sorted_groups(task_groups)

    if len(groups) <= 1:
        # Fallback: single LLM call for empty or single group
        if isinstance(task_groups, list):
            task_groups_text = json.dumps(task_groups, indent=2, ensure_ascii=False)
        else:
            task_groups_text = str(task_groups)

        prompt = IMPLEMENT_PROMPT.format(
            task_description=task_description,
            task_type=task_type,
            design_section=design_section,
            task_groups=task_groups_text,
            spec_summary=spec_summary,
        )
        if injection:
            prompt += injection

        return _run_single_llm_call(
            prompt, step, flow, project_root, task_groups, retry_count,
        )

    # --- Group-by-group execution ---
    logger.info("Executing %d task groups sequentially", len(groups))

    all_files_changed = []
    all_tests_added = []
    merged_test_mapping = {}
    previous_results: list[dict] = []
    implemented_group_ids: list[str] = []
    all_restricted_applied: list[dict] = []
    all_restricted_failed: list[dict] = []
    all_completion_statuses: list[str] = []
    all_incomplete_tasks: list[str] = []

    # Check for resume state
    completed_groups = set()
    if step.inputs.get("resumed") and step.outputs.get("implemented_groups"):
        completed_groups = set(
            g if isinstance(g, str) else g.get("group_id", "")
            for g in step.outputs["implemented_groups"]
        )
        all_files_changed = list(step.outputs.get("files_changed", []))
        all_tests_added = list(step.outputs.get("tests_added", []))
        merged_test_mapping = dict(step.outputs.get("test_mapping", {}))

    for group in groups:
        group_id = group.get("group_id", group.get("name", "unknown"))
        if group_id in completed_groups:
            logger.info("Skipping already-completed group: %s", group_id)
            # Reconstruct minimal previous result for context
            previous_results.append({
                "group_id": group_id,
                "files_changed": [],
                "summary": "(previously completed)",
            })
            continue

        logger.info("Implementing group: %s", group_id)

        # Build previous results context
        prev_ctx = "No previous groups." if not previous_results else json.dumps(
            previous_results, indent=2, ensure_ascii=False,
        )

        prompt = IMPLEMENT_GROUP_PROMPT.format(
            task_description=task_description,
            task_type=task_type,
            design_section=design_section,
            current_group=json.dumps(group, indent=2, ensure_ascii=False),
            previous_results=prev_ctx,
            spec_summary=spec_summary,
        )
        if injection:
            prompt += injection

        try:
            caller = LLMCaller(
                project_root,
                flow_id=flow.flow_id,
                step_id=step.step_id,
                step_type=step.step_type.value,
                external_attempt=retry_count,
            )
            response = caller.call(
                prompt=prompt,
                json_mode="two_phase",
                json_schema_hint='{"files_changed": [], "tests_added": [], "test_mapping": {}, "summary": "...", "completion_status": "complete|partial|failed", "incomplete_tasks": [], "restricted_edits": [{"file_path": "...", "old_string": "...", "new_string": "..."}]}',
            )
            result = parse_json_response(response, required_keys=[])
        except LLMCallError as e:
            logger.exception("Group %s LLM call failed", group_id)
            step.error_message = f"Implementation failed at group {group_id}: {str(e)}"
            return StepStatus.FAILED
        except Exception as e:
            logger.exception("Group %s failed", group_id)
            step.error_message = f"Implementation failed at group {group_id}: {str(e)}"
            return StepStatus.FAILED

        group_files = result.get("files_changed", []) if result else []
        group_tests = result.get("tests_added", []) if result else []
        group_mapping = result.get("test_mapping", {}) if result else {}
        group_summary = result.get("summary", "") if result else ""

        # Apply restricted edits for this group (Bug A)
        restricted_edits = result.get("restricted_edits", []) if result else []
        if restricted_edits:
            applied, failed_edits = _apply_restricted_edits(restricted_edits, project_root)
            all_restricted_applied.extend(applied)
            all_restricted_failed.extend(failed_edits)
            for edit in applied:
                fp = edit.get("file_path", "")
                if fp and fp not in group_files:
                    group_files.append(fp)
            if applied:
                logger.info("Group %s: applied %d restricted edits", group_id, len(applied))
            if failed_edits:
                logger.warning("Group %s: failed %d restricted edits", group_id, len(failed_edits))

        # Track per-group completion status (Bug B)
        group_completion = result.get("completion_status", "complete") if result else "complete"
        group_incomplete = result.get("incomplete_tasks", []) if result else []
        all_completion_statuses.append(group_completion)
        all_incomplete_tasks.extend(group_incomplete)

        all_files_changed.extend(group_files)
        all_tests_added.extend(group_tests)
        merged_test_mapping.update(group_mapping)
        implemented_group_ids.append(group_id)

        previous_results.append({
            "group_id": group_id,
            "files_changed": group_files,
            "summary": group_summary,
        })

        # Persist incremental progress
        step.outputs["files_changed"] = all_files_changed
        step.outputs["tests_added"] = all_tests_added
        step.outputs["test_mapping"] = merged_test_mapping
        step.outputs["implemented_groups"] = implemented_group_ids

    # Final outputs
    step.outputs["files_changed"] = all_files_changed
    step.outputs["tests_added"] = all_tests_added
    step.outputs["test_mapping"] = merged_test_mapping
    step.outputs["implemented_groups"] = implemented_group_ids

    # Restricted edits aggregation
    if all_restricted_applied:
        step.outputs["restricted_edits_applied"] = all_restricted_applied
    if all_restricted_failed:
        step.outputs["restricted_edits_failed"] = all_restricted_failed

    # Compute overall completion status
    if "failed" in all_completion_statuses:
        overall_status = "failed"
    elif "partial" in all_completion_statuses:
        overall_status = "partial"
    else:
        overall_status = "complete"

    step.outputs["completion_status"] = overall_status
    step.outputs["incomplete_tasks"] = all_incomplete_tasks
    step.outputs["summary"] = "; ".join(
        r.get("summary", "") for r in previous_results if r.get("summary")
    )

    if overall_status == "failed":
        step.error_message = "LLM reported implementation failed"
        return StepStatus.FAILED
    elif overall_status == "partial":
        logger.warning(
            "Implementation partially completed. Incomplete tasks: %s",
            all_incomplete_tasks,
        )
        return StepStatus.PARTIAL

    return StepStatus.COMPLETED


def _extract_sorted_groups(task_groups) -> list[dict]:
    """Extract and sort task groups by group_order."""
    if not isinstance(task_groups, list):
        return []
    groups = [g for g in task_groups if isinstance(g, dict)]
    groups.sort(key=lambda g: g.get("group_order", 0))
    return groups


def _run_single_llm_call(
    prompt: str,
    step: Step,
    flow: FlowInstance,
    project_root: Path,
    task_groups,
    retry_count: int,
) -> StepStatus:
    """Execute a single LLM call for implement (fallback path)."""
    try:
        caller = LLMCaller(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value,
            external_attempt=retry_count,
        )
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint='{"files_changed": [], "tests_added": [], "test_mapping": {}, "summary": "...", "completion_status": "complete|partial|failed", "incomplete_tasks": [], "restricted_edits": [{"file_path": "...", "old_string": "...", "new_string": "..."}]}',
        )

        result = parse_json_response(response, required_keys=[])

        if result:
            files_changed = result.get("files_changed", [])

            # Apply restricted edits (Bug A)
            restricted_edits = result.get("restricted_edits", [])
            if restricted_edits:
                applied, failed_edits = _apply_restricted_edits(restricted_edits, project_root)
                step.outputs["restricted_edits_applied"] = applied
                step.outputs["restricted_edits_failed"] = failed_edits
                # Add successfully edited files to files_changed
                for edit in applied:
                    fp = edit.get("file_path", "")
                    if fp and fp not in files_changed:
                        files_changed.append(fp)
                if applied:
                    logger.info("Applied %d restricted edits", len(applied))
                if failed_edits:
                    logger.warning("Failed %d restricted edits", len(failed_edits))

            step.outputs["files_changed"] = files_changed
            step.outputs["tests_added"] = result.get("tests_added", [])
            step.outputs["test_mapping"] = result.get("test_mapping", {})
            step.outputs["implemented_groups"] = task_groups
            step.outputs["summary"] = result.get("summary", "")

            # Completion status detection (Bug B)
            completion_status = result.get("completion_status", "complete")
            incomplete_tasks = result.get("incomplete_tasks", [])
            step.outputs["completion_status"] = completion_status
            step.outputs["incomplete_tasks"] = incomplete_tasks

            if completion_status == "failed":
                step.error_message = "LLM reported implementation failed"
                return StepStatus.FAILED
            elif completion_status == "partial":
                logger.warning(
                    "Implementation partially completed. Incomplete tasks: %s",
                    incomplete_tasks,
                )
                return StepStatus.PARTIAL

            return StepStatus.COMPLETED
        else:
            logger.warning("Could not parse implement summary JSON, using defaults")
            step.outputs["files_changed"] = []
            step.outputs["tests_added"] = []
            step.outputs["test_mapping"] = {}
            step.outputs["implemented_groups"] = task_groups

        return StepStatus.COMPLETED

    except LLMCallError as e:
        logger.exception("Implement step LLM call failed")
        step.error_message = f"Implementation failed: {str(e)}"
        return StepStatus.FAILED
    except Exception as e:
        logger.exception("Implement step failed")
        step.error_message = f"Implementation failed: {str(e)}"
        return StepStatus.FAILED


def _apply_restricted_edits(
    restricted_edits: list[dict], project_root: Path,
) -> tuple[list[dict], list[dict]]:
    """Apply edits that the LLM subprocess could not perform due to permission restrictions.

    Args:
        restricted_edits: List of {file_path, old_string, new_string} dicts.
        project_root: Root directory of the project.

    Returns:
        Tuple of (successful_edits, failed_edits). Each failed edit includes an 'error' key.
    """
    successful = []
    failed = []

    for edit in restricted_edits:
        file_path_str = edit.get("file_path", "")
        old_string = edit.get("old_string", "")
        new_string = edit.get("new_string", "")

        if not file_path_str or not old_string:
            failed.append({**edit, "error": "Missing file_path or old_string"})
            continue

        target = project_root / file_path_str
        try:
            if not target.is_file():
                failed.append({**edit, "error": f"File not found: {file_path_str}"})
                continue

            content = target.read_text(encoding="utf-8")

            if old_string not in content:
                failed.append({**edit, "error": f"old_string not found in {file_path_str}"})
                continue

            new_content = content.replace(old_string, new_string, 1)
            target.write_text(new_content, encoding="utf-8")

            # Verify the edit
            verify_content = target.read_text(encoding="utf-8")
            if new_string not in verify_content:
                failed.append({**edit, "error": f"Verification failed: new_string not found after write in {file_path_str}"})
                continue

            logger.info("Applied restricted edit to %s", file_path_str)
            successful.append(edit)

        except Exception as e:
            failed.append({**edit, "error": f"Exception: {e}"})

    return successful, failed


def _format_spec_brief(spec_content: dict[str, str]) -> str:
    """Format spec content as a brief summary for the implement prompt.

    Only includes coding conventions and key constraints, not full spec text.
    """
    if not spec_content:
        return "No project conventions specified."

    parts = []
    for name, content in spec_content.items():
        # Only include the base spec's conventions section, keep others brief
        if name == "base":
            parts.append(f"### {name}\n{content}")
        else:
            # Just the first few lines as context
            lines = content.split("\n")[:10]
            parts.append(f"### {name}\n" + "\n".join(lines) + "\n...")

    return "\n".join(parts) if parts else "No project conventions specified."
