"""Implement step handler.

Executes implementation of task groups, writing code to files.
Uses LLM (claude -p) with TWO_PHASE JSON extraction.
Supports fix iterations for the test-verify-fix loop.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from ..dag_scheduler import DAGScheduler, GroupResult, RelayContext, RelayPlan, classify_chains
from ..transitive_reduction import transitive_reduce
from ..llm_caller import LLMCaller, LLMCallError
from ..models import FlowInstance, Step, StepStatus
from ..utils.json_parser import parse_json_response
from ..worktree import (
    _run_git,
    create_worktree,
    delete_branch,
    force_cleanup_worktree,
    fork_worktree,
    get_conflicting_files,
    get_current_branch,
    has_commits,
    has_new_commits,
    resolve_merge_conflicts_with_context,
)

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
6. If you need to install dependencies or generate build artifacts, FIRST add the output directory to .gitignore (e.g., `node_modules/`, `.pixi/`, `venv/`, `dist/`) before running the install command.

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
6. If you need to install dependencies or generate build artifacts, FIRST add the output directory to .gitignore (e.g., `node_modules/`, `.pixi/`, `venv/`, `dist/`) before running the install command.

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

{spec_summary}

{design_section}

## Fix Instructions
{fix_instructions}

## Fix Context
{fix_context}

## Fix History
{fix_history}

## Fix Iteration
This is fix iteration {fix_iteration}.

## Instructions
1. Read the failing test output and error messages carefully.
2. Fix the root cause — do not just suppress errors.
3. Run the relevant tests mentally to verify your fix.
4. Do NOT commit — only write/edit files.
5. If you need to install dependencies, FIRST add the output directory to .gitignore before running the install command.

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
    task_groups = step.inputs.get("task_groups", [])
    design_doc = step.inputs.get("design_doc", {})
    spec_content = step.inputs.get("spec_content", {})
    fix_context = step.inputs.get("fix_context")
    fix_instructions = step.inputs.get("fix_instructions")
    is_fix_iteration = step.inputs.get("is_fix_iteration", False)
    fix_iteration = step.inputs.get("fix_iteration", 0)

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Record baseline commit hash before any implementation.
    # Used after all paths to get ground-truth files_changed via git diff.
    baseline_hash = _get_head_hash(project_root)

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
        fix_history = step.inputs.get("fix_history", [])
        fix_history_text = _format_fix_history(fix_history)
        fix_context_text = _format_fix_context_structured(fix_context)
        prompt = FIX_PROMPT.format(
            task_description=task_description,
            fix_instructions=fix_instructions,
            fix_context=fix_context_text,
            fix_iteration=fix_iteration,
            fix_history=fix_history_text,
            spec_summary=spec_summary,
            design_section=design_section,
        )
        if injection:
            prompt += injection

        result = _run_single_llm_call(
            prompt, step, flow, project_root, task_groups, retry_count,
        )
        _resolve_files_changed(step, project_root, baseline_hash)
        return result

    # Determine if we should use group-by-group execution
    groups = _extract_sorted_groups(task_groups)

    if len(groups) <= 1:
        # Fallback: single LLM call for empty or single group
        _display_task_plan(groups, "single", _compute_total_loc(groups), 0)
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

        result = _run_single_llm_call(
            prompt, step, flow, project_root, task_groups, retry_count,
        )
        _resolve_files_changed(step, project_root, baseline_hash)
        return result

    # LOC threshold: merge small multi-group tasks into single LLM call
    from ...config import ImplementConfig
    impl_config = ImplementConfig.load(project_root)
    total_loc = _compute_total_loc(groups)

    if total_loc > 0 and total_loc <= impl_config.group_loc_threshold:
        logger.info(
            "Total LOC %d <= threshold %d, merging %d groups into single LLM call",
            total_loc, impl_config.group_loc_threshold, len(groups),
        )
        _display_task_plan(groups, "single", total_loc, impl_config.group_loc_threshold)
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

        result = _run_single_llm_call(
            prompt, step, flow, project_root, task_groups, retry_count,
        )
        _resolve_files_changed(step, project_root, baseline_hash)
        return result

    # --- DAG parallel path (when dependency info is present) ---
    if _should_use_dag(groups, total_loc, impl_config.group_loc_threshold):
        _display_task_plan(groups, "dag_parallel", total_loc, impl_config.group_loc_threshold)
        if not has_commits(project_root):
            logger.warning(
                "Repository has no commits — falling back to sequential "
                "group-by-group execution instead of DAG parallel"
            )
        else:
            # Resume filtering for DAG parallel path
            prior_outputs: dict[str, Any] | None = None
            dag_groups = groups

            if step.inputs.get("resumed"):
                # Try step.outputs first (normal resume where state was saved)
                completed_groups_dag = set(
                    g if isinstance(g, str) else g.get("group_id", "")
                    for g in step.outputs.get("implemented_groups", [])
                )

                # Disaster recovery: check for surviving impl branches
                # that completed after the last state save.
                # Skip this scan if step.outputs already accounts for all groups.
                all_group_ids = {g.get("group_id", g.get("name", "unknown")) for g in groups}
                unaccounted = all_group_ids - completed_groups_dag

                if unaccounted:
                    original_branch = get_current_branch(project_root)

                    # If we're on an impl branch from a crashed run, restore
                    impl_prefix = f"impl/{flow.flow_id}/"
                    if original_branch.startswith(impl_prefix):
                        logger.warning(
                            "DAG disaster recovery: repo left on impl branch %s",
                            original_branch,
                        )
                        # Try common base branches directly (checkout - is unreliable
                        # after multi-thread checkout sequences)
                        restored = False
                        for candidate in ("master", "main", "develop"):
                            result = _run_git(project_root, "rev-parse", "--verify", candidate, check=False)
                            if result.returncode == 0:
                                _run_git(project_root, "checkout", candidate, check=False)
                                original_branch = candidate
                                restored = True
                                logger.info("DAG disaster recovery: restored to %s", candidate)
                                break
                        if not restored:
                            logger.error("DAG disaster recovery: cannot determine base branch, aborting")
                            step.error_message = (
                                f"Repo is on impl branch {original_branch} from a crashed run "
                                f"and no base branch (master/main) found. "
                                f"Please checkout the correct branch manually and retry."
                            )
                            return StepStatus.FAILED


                    for gid in unaccounted:
                        branch = f"impl/{flow.flow_id}/{gid}"
                        try:
                            if has_new_commits(project_root, branch, original_branch):
                                completed_groups_dag.add(gid)
                                logger.info(
                                    "DAG disaster recovery: found surviving branch %s",
                                    branch,
                                )
                        except Exception:
                            pass

                if completed_groups_dag:
                    dag_groups = [
                        g for g in groups
                        if g.get("group_id", g.get("name", "unknown")) not in completed_groups_dag
                    ]
                    if not dag_groups:
                        logger.info("DAG parallel: all groups already completed on resume")
                        # Merge surviving branches before returning
                        result = _run_dag_parallel(
                            groups=[],
                            step=step,
                            flow=flow,
                            project_root=project_root,
                            task_description=task_description,
                            task_type=task_type,
                            design_section=design_section,
                            spec_summary=spec_summary,
                            injection=injection,
                            retry_count=retry_count,
                            prior_outputs={
                                "files_changed": list(step.outputs.get("files_changed", [])),
                                "tests_added": list(step.outputs.get("tests_added", [])),
                                "test_mapping": dict(step.outputs.get("test_mapping", {})),
                                "implemented_groups": list(completed_groups_dag),
                            },
                        )
                        _resolve_files_changed(step, project_root, baseline_hash)
                        return result
                    prior_outputs = {
                        "files_changed": list(step.outputs.get("files_changed", [])),
                        "tests_added": list(step.outputs.get("tests_added", [])),
                        "test_mapping": dict(step.outputs.get("test_mapping", {})),
                        "implemented_groups": list(completed_groups_dag),
                    }
                    logger.info(
                        "DAG parallel resume: skipping %d completed groups, running %d remaining",
                        len(completed_groups_dag), len(dag_groups),
                    )

            result = _run_dag_parallel(
                groups=dag_groups,
                step=step,
                flow=flow,
                project_root=project_root,
                task_description=task_description,
                task_type=task_type,
                design_section=design_section,
                spec_summary=spec_summary,
                injection=injection,
                retry_count=retry_count,
                prior_outputs=prior_outputs,
            )
            _resolve_files_changed(step, project_root, baseline_hash)
            return result

    # --- Group-by-group execution ---
    _display_task_plan(groups, "sequential", total_loc, impl_config.group_loc_threshold)
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
        # Carry forward previously-completed group IDs so they survive
        # across multiple retries (Bug fix: was starting empty, losing
        # completed groups on subsequent retries)
        implemented_group_ids = list(completed_groups)

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
            # Use group-specific step_id so each group has its own history
            # file, preventing mixed conversations on retry
            group_step_id = f"{step.step_id}_{group_id}"
            caller = LLMCaller(
                project_root,
                flow_id=flow.flow_id,
                step_id=group_step_id,
                step_type=step.step_type.value,
                external_attempt=retry_count,
                stream_prefix=f'[{group_id}] ',
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

    # Resolve files_changed from git diff (ground truth)
    _resolve_files_changed(step, project_root, baseline_hash)

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


def _display_task_plan(
    groups: list[dict],
    strategy: str,
    total_loc: int,
    threshold: int,
) -> None:
    """Display implementation task plan with execution strategy.

    Wraps the call in try/except so display failures never block execution.
    For dag_parallel strategy, computes RelayPlan to show execution topology.
    """
    try:
        from ..formatters import TaskFormatter
        from ..display import get_console

        console = get_console()
        formatter = TaskFormatter(console=console)

        # Compute relay plan for DAG parallel topology display
        relay_plan = None
        if strategy == "dag_parallel" and len(groups) > 1:
            try:
                reduced = transitive_reduce(groups)
                relay_plan = classify_chains(reduced)
            except Exception:
                logger.debug("Could not compute relay plan for display", exc_info=True)

        console.print(formatter.format_implement_plan(
            task_groups=groups,
            execution_strategy=strategy,
            total_loc=total_loc,
            loc_threshold=threshold,
            relay_plan=relay_plan,
        ))
    except Exception:
        logger.debug("Could not render implementation plan", exc_info=True)


def _extract_sorted_groups(task_groups) -> list[dict]:
    """Extract and sort task groups by group_order."""
    if not isinstance(task_groups, list):
        return []
    groups = [g for g in task_groups if isinstance(g, dict)]
    groups.sort(key=lambda g: g.get("group_order", 0))
    return groups


def _compute_total_loc(groups: list[dict]) -> int:
    """Sum estimated_loc across all tasks in all groups.

    Tasks missing the ``estimated_loc`` field default to 50 LOC each,
    providing a sensible fallback for plans generated before the field
    was introduced.
    """
    total = 0
    for g in groups:
        for task in g.get("tasks", []):
            if isinstance(task, dict):
                total += task.get("estimated_loc", 50)
    return total


def _should_use_dag(groups: list[dict], total_loc: int = 0, loc_threshold: int = 300) -> bool:
    """Check whether to enable DAG parallel execution path.

    Returns True when there are multiple groups AND the total estimated
    LOC exceeds the configured threshold.  Small multi-group tasks are
    better served by a single LLM call (the LOC-merge path in
    ``implement_handler``).

    Args:
        groups: Sorted group dicts.
        total_loc: Pre-computed total estimated LOC across all groups.
        loc_threshold: Configured LOC threshold from ImplementConfig.
    """
    if len(groups) <= 1:
        return False
    # When total_loc is available and below threshold, prefer single call
    if total_loc > 0 and total_loc <= loc_threshold:
        return False
    return True


def _make_execute_fn(
    project_root: Path,
    original_branch: str,
    flow: FlowInstance,
    step: Step,
    task_description: str,
    task_type: str,
    design_section: str,
    spec_summary: str,
    injection: str | None,
    retry_count: int,
) -> Callable[[dict, dict[str, GroupResult], RelayContext], GroupResult]:
    """Build the execute_fn closure for relay-based DAG parallel execution.

    The returned callable acquires a worktree via the relay strategy
    (reuse predecessor / fork / create new), handles convergence merges,
    runs the LLM agent, commits changes, and returns a GroupResult.
    """
    git_lock = threading.Lock()

    def execute_fn(
        group: dict,
        deps_results: dict[str, GroupResult],
        relay_context: RelayContext,
    ) -> GroupResult:
        group_id = group.get("group_id", group.get("name", "unknown"))
        branch_name = f"impl/{flow.flow_id}/{group_id}"
        worktree_path: Path | None = None

        try:
            # Step 1: Acquire worktree based on relay_context
            if relay_context.worktree_path is not None:
                # Relay: reuse predecessor's worktree and branch directly
                worktree_path = relay_context.worktree_path
                branch_name = relay_context.branch_name or branch_name
                logger.info(
                    "DAG relay: group %s reusing worktree %s (branch %s)",
                    group_id, worktree_path, branch_name,
                )
            elif relay_context.is_fork and relay_context.fork_source_branch:
                # Fork: create new branch from predecessor's branch + new worktree
                with git_lock:
                    force_cleanup_worktree(project_root, branch_name)
                    _run_git(project_root, "branch", "-D", branch_name, check=False)
                    worktree_path = fork_worktree(
                        project_root, relay_context.fork_source_branch, branch_name,
                    )
                logger.info(
                    "DAG fork: group %s forked from %s to %s (worktree %s)",
                    group_id, relay_context.fork_source_branch,
                    branch_name, worktree_path,
                )
            else:
                # Root node (or no relay_plan): create new branch + worktree
                with git_lock:
                    force_cleanup_worktree(project_root, branch_name)
                    _run_git(project_root, "branch", "-D", branch_name, check=False)
                    _run_git(project_root, "branch", branch_name, original_branch)
                    logger.info(
                        "DAG root: created branch %s from %s",
                        branch_name, original_branch,
                    )
                    worktree_path = create_worktree(project_root, branch_name)
                logger.info(
                    "DAG root: group %s created worktree %s",
                    group_id, worktree_path,
                )

            # Step 2: Convergence merge — merge secondary predecessor branches
            if relay_context.convergence_merges:
                with git_lock:
                    for sec_branch in relay_context.convergence_merges:
                        logger.info(
                            "DAG convergence: merging %s into worktree at %s",
                            sec_branch, worktree_path,
                        )
                        merge_result = _run_git(
                            worktree_path, "merge", sec_branch, "--no-edit",
                            check=False,
                        )
                        if merge_result.returncode != 0:
                            stderr = merge_result.stderr.strip()
                            is_conflict = "CONFLICT" in (
                                merge_result.stdout + merge_result.stderr
                            )
                            if is_conflict:
                                logger.warning(
                                    "DAG convergence: conflict merging %s, "
                                    "attempting LLM resolution",
                                    sec_branch,
                                )
                                conflict_files = get_conflicting_files(worktree_path)
                                if conflict_files:
                                    resolved = _resolve_convergence_conflicts(
                                        worktree_path, conflict_files,
                                        task_description, deps_results,
                                    )
                                    if not resolved:
                                        _run_git(
                                            worktree_path, "merge", "--abort",
                                            check=False,
                                        )
                                        return GroupResult.failed(
                                            group_id,
                                            f"Convergence merge conflict with "
                                            f"{sec_branch} could not be resolved",
                                        )
                                else:
                                    _run_git(
                                        worktree_path, "merge", "--abort",
                                        check=False,
                                    )
                                    return GroupResult.failed(
                                        group_id,
                                        f"Convergence merge failed: {sec_branch}: "
                                        f"{stderr}",
                                    )
                            else:
                                _run_git(
                                    worktree_path, "merge", "--abort",
                                    check=False,
                                )
                                return GroupResult.failed(
                                    group_id,
                                    f"Convergence merge failed: {sec_branch}: "
                                    f"{stderr}",
                                )

            # Step 3: Build previous_results context from deps_results
            if deps_results:
                prev_results = [
                    {
                        "group_id": did,
                        "files_changed": dr.files_changed,
                        "summary": dr.summary,
                    }
                    for did, dr in deps_results.items()
                ]
                prev_ctx = json.dumps(prev_results, indent=2, ensure_ascii=False)
            else:
                prev_ctx = "No previous groups."

            # Step 4: Format prompt
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

            # Inject runtime context for worktree isolation
            from ..context_builder import get_runtime_context_injection
            runtime_ctx = get_runtime_context_injection(worktree_path, project_root)
            if runtime_ctx:
                prompt += runtime_ctx

            # Step 5: Run LLM in the worktree
            # Use group-specific step_id so each group has its own history file
            group_step_id = f"{step.step_id}_{group_id}"

            # Restore history from main repo so retry context injection works
            _restore_history_to_worktree(project_root, worktree_path, flow.flow_id)

            caller = LLMCaller(
                worktree_path,
                flow_id=flow.flow_id,
                step_id=group_step_id,
                step_type=step.step_type.value,
                external_attempt=retry_count,
                stream_prefix=f'[{group_id}] ',
            )
            response = caller.call(
                prompt=prompt,
                json_mode="two_phase",
                json_schema_hint='{"files_changed": [], "tests_added": [], "test_mapping": {}, "summary": "...", "completion_status": "complete|partial|failed", "incomplete_tasks": [], "restricted_edits": [{"file_path": "...", "old_string": "...", "new_string": "..."}]}',
            )
            result = parse_json_response(response, required_keys=[])

            # Step 6: Commit changes in the worktree (only if there are changes)
            _run_git(worktree_path, "add", "-A", check=False)
            status_result = _run_git(worktree_path, "status", "--porcelain", check=False)
            has_changes = bool(status_result.stdout.strip())

            if has_changes:
                commit_result = _run_git(
                    worktree_path, "commit",
                    "-m", f"impl: group {group_id}",
                    check=False,
                )
                if commit_result.returncode != 0:
                    logger.warning(
                        "DAG: commit failed for %s: %s",
                        group_id, commit_result.stderr.strip(),
                    )
            else:
                logger.info("DAG: no changes in worktree for group %s, skipping commit", group_id)

            # Step 7: Build GroupResult
            files_changed = result.get("files_changed", []) if result else []
            tests_added = result.get("tests_added", []) if result else []
            test_mapping = result.get("test_mapping", {}) if result else {}
            summary = result.get("summary", "") if result else ""
            completion_status = result.get("completion_status", "complete") if result else "complete"
            incomplete_tasks = result.get("incomplete_tasks", []) if result else []
            restricted_edits = result.get("restricted_edits", []) if result else []

            # For relay/fork nodes, always preserve the branch name so downstream
            # groups and leaf merge can locate the accumulated commits.
            if has_changes:
                effective_branch = branch_name
            elif relay_context.worktree_path is not None or relay_context.is_fork:
                effective_branch = branch_name
            else:
                effective_branch = ""

            return GroupResult(
                group_id=group_id,
                status="completed",
                files_changed=files_changed,
                tests_added=tests_added,
                test_mapping=test_mapping,
                summary=summary,
                branch_name=effective_branch,
                worktree_path=worktree_path,
                completion_status=completion_status,
                incomplete_tasks=incomplete_tasks,
                restricted_edits=restricted_edits,
            )

        except subprocess.TimeoutExpired as e:
            wt_display = str(worktree_path) if worktree_path else "<not yet created>"
            msg = (
                f"Git worktree creation timed out for group {group_id} "
                f"(branch: {branch_name}, worktree: {wt_display}, repo: {project_root}). "
                f"Possible causes: git lock files in the repo, very large repository, "
                f"or a git process waiting for interactive input. "
                f"Check for stale .git/worktrees/*/locked files or index.lock."
            )
            logger.error(msg)
            result = GroupResult.failed(group_id, f"{msg} Original error: {e}")
            result.worktree_path = worktree_path  # preserve for history salvaging
            return result
        except (LLMCallError, Exception) as e:
            logger.exception("DAG: group %s failed", group_id)
            result = GroupResult.failed(group_id, str(e))
            result.worktree_path = worktree_path  # preserve for history salvaging
            return result

    return execute_fn


def _resolve_convergence_conflicts(
    worktree_path: Path,
    conflict_files: list[str],
    task_description: str,
    deps_results: dict[str, GroupResult],
) -> bool:
    """Resolve merge conflicts at a convergence point using LLM.

    Reads each conflicting file, sends it to the LLM with context about
    the converging groups, and writes back the resolved content.

    Args:
        worktree_path: Path to the worktree where the merge is happening
        conflict_files: List of conflicting file paths
        task_description: Overall task description for context
        deps_results: Results from predecessor groups for context

    Returns:
        True if all conflicts resolved and merge committed, False otherwise
    """
    try:
        from ..llm_caller import LLMCaller
    except ImportError:
        logger.warning("LLMCaller not available for convergence conflict resolution")
        return False

    # Build context about what each predecessor did
    group_context_parts: list[str] = []
    for gid, result in deps_results.items():
        group_context_parts.append(
            f"- Group {gid}: {result.summary} (files: {', '.join(result.files_changed)})"
        )
    group_context = "\n".join(group_context_parts) if group_context_parts else "No group context."

    for filepath in conflict_files:
        full_path = worktree_path / filepath
        if not full_path.exists():
            logger.warning("Convergence conflict file not found: %s", filepath)
            return False

        try:
            content = full_path.read_text(encoding="utf-8")
        except Exception:
            logger.warning("Could not read convergence conflict file: %s", filepath)
            return False

        prompt = (
            "You are resolving a git merge conflict at a convergence point where "
            "multiple parallel implementation groups are being merged together.\n\n"
            f"## Task Description\n{task_description}\n\n"
            f"## Group Summaries\n{group_context}\n\n"
            f"## Conflicting File: {filepath}\n\n"
            f"```\n{content}\n```\n\n"
            "Output ONLY the fully resolved file content with no conflict markers "
            "(no <<<<<<< / ======= / >>>>>>>). Do not add any explanation."
        )

        try:
            caller = LLMCaller(worktree_path, step_type="convergence_conflict")
            resolved_content = caller.call(prompt=prompt)
        except Exception as e:
            logger.warning(
                "LLM convergence conflict resolution failed for %s: %s", filepath, e,
            )
            return False

        # Verify no conflict markers remain
        if "<<<<<<<" in resolved_content or ">>>>>>>" in resolved_content:
            logger.warning(
                "LLM output still contains conflict markers for %s", filepath,
            )
            return False

        # Write resolved content
        try:
            full_path.write_text(resolved_content, encoding="utf-8")
            _run_git(worktree_path, "add", filepath, check=False)
        except Exception as e:
            logger.warning("Failed to write resolved content for %s: %s", filepath, e)
            return False

    # Complete the merge commit
    commit_result = _run_git(worktree_path, "commit", "--no-edit", check=False)
    if commit_result.returncode != 0:
        logger.warning(
            "Convergence merge commit failed: %s", commit_result.stderr.strip(),
        )
        return False

    logger.info("Convergence merge conflicts resolved successfully")
    return True


def _merge_leaf_branch(
    project_root: Path,
    branch: str,
    original_branch: str,
    task_description: str,
    group_summaries: list[dict],
    spec_content: str,
    flow_id: str | None = None,
    merge_step_id: str | None = None,
) -> bool:
    """Merge a leaf branch back to original_branch with enhanced conflict resolution.

    Unlike the old approach that fell back to ``--theirs`` on conflict, this
    function uses :func:`resolve_merge_conflicts_with_context` which provides the LLM
    with full task context.  If the LLM cannot resolve after 3 retries the merge
    is aborted and ``False`` is returned — no silent data loss.

    Args:
        project_root: Project root directory
        branch: Branch to merge
        original_branch: Target branch
        task_description: Overall task description for LLM context
        group_summaries: List of ``{group_id, summary, files_changed}`` dicts
        spec_content: Spec summary for LLM context

    Returns:
        True on successful merge, False on failure
    """
    current = get_current_branch(project_root)
    if current != original_branch:
        _run_git(project_root, "checkout", original_branch)

    result = _run_git(
        project_root, "merge", branch, "--no-edit",
        "-m", f"Merge leaf branch {branch}",
        check=False,
    )

    if result.returncode == 0:
        logger.info("Leaf merge succeeded: %s -> %s", branch, original_branch)
        return True

    is_conflict = "CONFLICT" in (result.stdout + result.stderr)
    if not is_conflict:
        logger.error(
            "Leaf merge failed (non-conflict): %s: %s",
            branch, result.stderr.strip(),
        )
        _run_git(project_root, "merge", "--abort", check=False)
        return False

    logger.warning("Leaf merge conflict: %s, attempting LLM resolution", branch)
    conflict_files = get_conflicting_files(project_root)
    if not conflict_files:
        _run_git(project_root, "merge", "--abort", check=False)
        return False

    resolved = resolve_merge_conflicts_with_context(
        project_root, conflict_files, task_description, group_summaries, spec_content,
        flow_id=flow_id, step_id=merge_step_id,
    )
    if resolved:
        logger.info("Leaf merge conflicts resolved: %s -> %s", branch, original_branch)
        return True

    logger.error(
        "Leaf merge conflict resolution failed after retries: %s", branch,
    )
    _run_git(project_root, "merge", "--abort", check=False)
    return False


def _run_dag_parallel(
    groups: list[dict],
    step: Step,
    flow: FlowInstance,
    project_root: Path,
    task_description: str,
    task_type: str,
    design_section: str,
    spec_summary: str,
    injection: str | None,
    retry_count: int,
    prior_outputs: dict[str, Any] | None = None,
) -> StepStatus:
    """DAG parallel execution path for implement step.

    Creates a DAGScheduler from group dependencies, runs all groups in
    parallel via worktree-isolated LLM agents, then merges results back
    in topological order.

    Args:
        prior_outputs: Optional dict with keys files_changed, tests_added,
            test_mapping, implemented_groups from a previous (resumed) run.
            These are merged into the final aggregated outputs.
    """
    original_branch = get_current_branch(project_root)

    # Disaster recovery: merge surviving branches from prior_outputs
    # before running new groups (so new groups see recovered code)
    recovered_groups: list[str] = list(prior_outputs.get("implemented_groups", [])) if prior_outputs else []
    already_deleted_gids: set[str] = set()
    if recovered_groups:
        from ..worktree import has_new_commits
        for gid in recovered_groups:
            branch = f"impl/{flow.flow_id}/{gid}"
            # Salvage history from stale worktree before cleanup
            safe_name = branch.replace("/", "-")
            stale_wt = project_root / "se3" / "worktrees" / safe_name
            if stale_wt.exists():
                try:
                    _salvage_history_from_worktree(stale_wt, project_root)
                except Exception:
                    logger.debug("DAG resume: failed to salvage history from %s", stale_wt)
            # Clean up stale worktree from crashed run (if any)
            try:
                force_cleanup_worktree(project_root, branch)
            except Exception:
                pass
            # Check if branch still exists and has unmerged commits
            check = _run_git(project_root, "rev-parse", "--verify", branch, check=False)
            if check.returncode != 0:
                continue  # Branch already merged/deleted from a previous run
            try:
                if has_new_commits(project_root, branch, original_branch):
                    logger.info("DAG resume: pre-merging recovered branch %s", branch)
                    merge_step_id = f"{step.step_id}_recover_{gid}"
                    success = _merge_leaf_branch(
                        project_root, branch, original_branch,
                        task_description, [], spec_summary,
                        flow_id=flow.flow_id, merge_step_id=merge_step_id,
                    )
                    if success:
                        delete_branch(project_root, branch)
                        already_deleted_gids.add(gid)
                    else:
                        logger.error("DAG resume: merge failed for recovered %s", gid)
                else:
                    # Branch exists but no new commits — clean up
                    delete_branch(project_root, branch)
                    already_deleted_gids.add(gid)
            except Exception as e:
                logger.warning("DAG resume: failed to process branch %s: %s", branch, e)

    logger.info(
        "DAG parallel: executing %d groups (branch=%s)",
        len(groups), original_branch,
    )

    if not groups:
        # All groups recovered, nothing new to run — just aggregate outputs
        step.outputs["files_changed"] = list(prior_outputs.get("files_changed", [])) if prior_outputs else []
        step.outputs["tests_added"] = list(prior_outputs.get("tests_added", [])) if prior_outputs else []
        step.outputs["test_mapping"] = dict(prior_outputs.get("test_mapping", {})) if prior_outputs else {}
        step.outputs["implemented_groups"] = recovered_groups
        step.outputs["summary"] = "Recovered from previous run"
        step.outputs["completion_status"] = "complete"
        step.outputs["incomplete_tasks"] = []
        return StepStatus.COMPLETED

    # Transitive reduction: remove redundant dependency edges
    reduced_groups = transitive_reduce(groups)

    # Log which redundant edges were removed
    orig_deps = {g['group_id']: set(g.get('depends_on', [])) for g in groups}
    any_removed = False
    for rg in reduced_groups:
        gid = rg['group_id']
        removed = orig_deps.get(gid, set()) - set(rg.get('depends_on', []))
        if removed:
            any_removed = True
            logger.info("Transitive reduction: %s removed redundant deps %s", gid, sorted(removed))
    if not any_removed:
        logger.info("Transitive reduction: no redundant edges found")

    # Generate relay execution plan
    relay_plan = classify_chains(reduced_groups)
    logger.info(
        "DAG relay plan: %d root(s), %d leaf(ves), %d convergence point(s)",
        len(relay_plan.root_nodes), len(relay_plan.leaf_nodes),
        len(relay_plan.convergence_points),
    )

    scheduler = DAGScheduler(reduced_groups, max_workers=4, relay_plan=relay_plan)
    execute_fn = _make_execute_fn(
        project_root=project_root,
        original_branch=original_branch,
        flow=flow,
        step=step,
        task_description=task_description,
        task_type=task_type,
        design_section=design_section,
        spec_summary=spec_summary,
        injection=injection,
        retry_count=retry_count,
    )

    results: list[GroupResult] = []
    try:
        results = scheduler.run(execute_fn)
    finally:
        # Salvage history files from worktrees before cleanup
        for r in results:
            if r.worktree_path:
                try:
                    _salvage_history_from_worktree(r.worktree_path, project_root)
                except Exception:
                    logger.warning("DAG: failed to salvage history from worktree %s", r.worktree_path)

        # Clean up worktrees (deduplicated for relay chains sharing worktrees).
        # NOTE: Only remove worktree directories here, NOT branches.
        # Branches must survive until after merge-back completes.
        cleaned_branches: set[str] = set()
        for r in results:
            if r.branch_name and r.branch_name not in cleaned_branches:
                cleaned_branches.add(r.branch_name)
                try:
                    force_cleanup_worktree(project_root, r.branch_name)
                except Exception:
                    logger.warning("DAG: failed to force-cleanup worktree for branch %s", r.branch_name)

        # Ensure we're back on original_branch
        try:
            current = get_current_branch(project_root)
            if current != original_branch:
                _run_git(project_root, "checkout", original_branch)
        except Exception:
            logger.warning("DAG: failed to restore original branch %s", original_branch)

    # Build results map for easy lookup
    results_map: dict[str, GroupResult] = {r.group_id: r for r in results}

    # Merge only leaf nodes + fallback leaves (not all completed groups).
    # With relay strategy, intermediate groups' commits are already on the
    # leaf branch — only the leaf needs to merge back to original_branch.
    fallback_leaf_ids = set(scheduler.get_fallback_leaves())
    merge_group_ids = relay_plan.leaf_nodes | fallback_leaf_ids

    # Collect unique branches to merge (relay chains share the same branch)
    branches_to_merge: list[tuple[str, str]] = []
    seen_branches: set[str] = set()
    for gid in merge_group_ids:
        r = results_map.get(gid)
        if r and r.status == "completed" and r.branch_name and r.branch_name not in seen_branches:
            seen_branches.add(r.branch_name)
            branches_to_merge.append((gid, r.branch_name))

    # Build group summaries for LLM conflict resolution context
    group_summaries = [
        {"group_id": r.group_id, "summary": r.summary, "files_changed": r.files_changed}
        for r in results if r.status == "completed"
    ]

    merge_failures: list[str] = []
    for merge_idx, (gid, branch) in enumerate(branches_to_merge):
        logger.info("DAG: merging leaf branch %s back to %s", branch, original_branch)
        merge_step_id = f"{step.step_id}_merge_{merge_idx}"
        success = _merge_leaf_branch(
            project_root, branch, original_branch,
            task_description, group_summaries, spec_summary,
            flow_id=flow.flow_id, merge_step_id=merge_step_id,
        )
        if not success:
            logger.error("DAG: leaf merge failed for %s (branch %s)", gid, branch)
            merge_failures.append(gid)

    # Delete impl branches — collect actual branch names from results + recovered
    branches_to_delete: set[str] = set()
    for r in results:
        if r.branch_name:
            branches_to_delete.add(r.branch_name)
    for gid in recovered_groups:
        if gid not in already_deleted_gids:
            branches_to_delete.add(f"impl/{flow.flow_id}/{gid}")
    for branch in branches_to_delete:
        try:
            delete_branch(project_root, branch)
        except Exception:
            logger.debug("DAG: failed to delete branch %s", branch)

    # Apply restricted edits on original_branch
    all_restricted_applied: list[dict] = []
    all_restricted_failed: list[dict] = []
    for r in results:
        if r.status == "completed" and r.restricted_edits:
            applied, failed_edits = _apply_restricted_edits(r.restricted_edits, project_root)
            all_restricted_applied.extend(applied)
            all_restricted_failed.extend(failed_edits)

    # Aggregate outputs — seed from prior_outputs (resume) if available
    all_files_changed: list[str] = list(prior_outputs.get("files_changed", [])) if prior_outputs else []
    all_tests_added: list[str] = list(prior_outputs.get("tests_added", [])) if prior_outputs else []
    merged_test_mapping: dict = dict(prior_outputs.get("test_mapping", {})) if prior_outputs else {}
    all_completion_statuses: list[str] = []
    all_incomplete_tasks: list[str] = []
    implemented_group_ids: list[str] = list(prior_outputs.get("implemented_groups", [])) if prior_outputs else []
    summaries: list[str] = []

    for r in results:
        if r.status == "completed":
            all_files_changed.extend(r.files_changed)
            all_tests_added.extend(r.tests_added)
            merged_test_mapping.update(r.test_mapping)
            implemented_group_ids.append(r.group_id)
            all_completion_statuses.append(r.completion_status)
            all_incomplete_tasks.extend(r.incomplete_tasks)
            if r.summary:
                summaries.append(r.summary)
        elif r.status == "failed":
            all_completion_statuses.append("failed")
            if r.error:
                all_incomplete_tasks.append(f"Group {r.group_id}: {r.error}")
        elif r.status == "skipped":
            all_completion_statuses.append("failed")
            all_incomplete_tasks.append(f"Group {r.group_id}: skipped (upstream dependency failed)")

    # Add files from successful restricted edits
    for edit in all_restricted_applied:
        fp = edit.get("file_path", "")
        if fp and fp not in all_files_changed:
            all_files_changed.append(fp)

    step.outputs["files_changed"] = all_files_changed
    step.outputs["tests_added"] = all_tests_added
    step.outputs["test_mapping"] = merged_test_mapping
    step.outputs["implemented_groups"] = implemented_group_ids
    step.outputs["summary"] = "; ".join(summaries)

    if all_restricted_applied:
        step.outputs["restricted_edits_applied"] = all_restricted_applied
    if all_restricted_failed:
        step.outputs["restricted_edits_failed"] = all_restricted_failed

    # Compute overall completion status.
    # When fallback leaves were successfully merged, some work is preserved
    # even though downstream groups failed — report "partial" not "failed".
    if "failed" in all_completion_statuses:
        if fallback_leaf_ids and not merge_failures:
            overall_status = "partial"
        else:
            overall_status = "failed"
    elif "partial" in all_completion_statuses:
        overall_status = "partial"
    else:
        overall_status = "complete"

    step.outputs["completion_status"] = overall_status
    step.outputs["incomplete_tasks"] = all_incomplete_tasks

    if merge_failures:
        step.outputs["merge_failures"] = merge_failures

    if overall_status == "failed":
        step.error_message = "DAG parallel: one or more groups failed"
        return StepStatus.FAILED
    elif overall_status == "partial":
        logger.warning(
            "DAG parallel: partially completed. Incomplete: %s",
            all_incomplete_tasks,
        )
        return StepStatus.PARTIAL

    return StepStatus.COMPLETED


def _run_single_llm_call(
    prompt: str,
    step: Step,
    flow: FlowInstance,
    project_root: Path,
    task_groups,
    retry_count: int,
    stream_prefix: str = '',
) -> StepStatus:
    """Execute a single LLM call for implement (fallback path)."""
    try:
        caller = LLMCaller(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value,
            external_attempt=retry_count,
            stream_prefix=stream_prefix,
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


def _salvage_history_from_worktree(worktree_path: Path, main_repo_root: Path) -> None:
    """Copy history files from a worktree back to the main repository.

    When LLM runs in a worktree, chat history is recorded under the worktree's
    se3/history/ directory. This function copies those files to the main repo
    before the worktree is cleaned up, preventing history loss.

    Args:
        worktree_path: Path to the worktree directory
        main_repo_root: Path to the main repository root
    """
    import shutil

    wt_history = worktree_path / "se3" / "history"
    if not wt_history.exists():
        return

    main_history = main_repo_root / "se3" / "history"
    main_history.mkdir(parents=True, exist_ok=True)

    copied = 0
    for flow_dir in wt_history.iterdir():
        if not flow_dir.is_dir():
            continue
        target_flow_dir = main_history / flow_dir.name
        target_flow_dir.mkdir(parents=True, exist_ok=True)

        for history_file in flow_dir.iterdir():
            if not history_file.is_file():
                continue
            target_file = target_flow_dir / history_file.name
            if target_file.exists():
                # Append content (NDJSON is line-based, safe to concatenate)
                with open(target_file, "a", encoding="utf-8") as dst:
                    dst.write(history_file.read_text(encoding="utf-8"))
            else:
                shutil.copy2(history_file, target_file)
            copied += 1

    if copied:
        logger.info("Salvaged %d history file(s) from worktree %s", copied, worktree_path)


def _restore_history_to_worktree(main_repo_root: Path, worktree_path: Path, flow_id: str) -> None:
    """Copy history files from main repo into a worktree.

    This enables LLMCaller retry context injection in worktrees,
    which look for history at their own project_root/se3/history/.
    Only copies history for the given flow_id.

    Args:
        main_repo_root: Main repository root
        worktree_path: Worktree directory
        flow_id: Flow ID to copy history for
    """
    import shutil

    main_flow_dir = main_repo_root / "se3" / "history" / flow_id
    if not main_flow_dir.exists():
        return

    wt_flow_dir = worktree_path / "se3" / "history" / flow_id
    wt_flow_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    for history_file in main_flow_dir.iterdir():
        if not history_file.is_file():
            continue
        target = wt_flow_dir / history_file.name
        shutil.copy2(history_file, target)
        copied += 1

    if copied:
        logger.debug("Restored %d history file(s) to worktree %s", copied, worktree_path)


def _get_head_hash(project_root: Path) -> str | None:
    """Get current HEAD commit hash, or None for empty repos."""
    result = _run_git(project_root, "rev-parse", "HEAD", check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def _resolve_files_changed(step: Step, project_root: Path, baseline_hash: str | None) -> None:
    """Replace LLM-reported files_changed with git diff ground truth.

    Compares the current HEAD (after implementation) against the baseline
    hash (before implementation) to get the definitive list of changed files.
    Also includes any uncommitted changes in the working tree.

    Args:
        step: Step whose outputs["files_changed"] will be overwritten
        project_root: Project root directory
        baseline_hash: Commit hash from before implementation, or None
    """
    try:
        changed: set[str] = set()

        # Committed changes since baseline
        if baseline_hash:
            result = _run_git(
                project_root, "diff", "--name-only", baseline_hash, "HEAD",
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                changed.update(result.stdout.strip().splitlines())

        # Uncommitted changes (staged + unstaged)
        result = _run_git(
            project_root, "diff", "--name-only", "HEAD", check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            changed.update(result.stdout.strip().splitlines())

        # Staged but not committed
        result = _run_git(
            project_root, "diff", "--name-only", "--cached", check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            changed.update(result.stdout.strip().splitlines())

        # Untracked files (new files created by LLM)
        result = _run_git(
            project_root, "ls-files", "--others", "--exclude-standard", check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            changed.update(result.stdout.strip().splitlines())

        if changed:
            step.outputs["files_changed"] = sorted(changed)
            logger.info("Resolved files_changed from git: %d files", len(changed))
        # If git diff found nothing but LLM reported files, keep LLM report as fallback

    except Exception as e:
        logger.debug("Failed to resolve files_changed from git: %s", e)
        # Keep LLM-reported files_changed as fallback


def _format_fix_history(fix_history: list) -> str:
    """Format fix history for inclusion in FIX_PROMPT."""
    if not fix_history:
        return "No previous fix attempts."

    lines = []
    for entry in fix_history:
        it = entry.get("iteration", "?")
        reason = entry.get("reason", "unknown")
        trigger = entry.get("trigger_step_type", "unknown")
        summary = entry.get("fix_instructions_summary", "")
        lines.append(f"- Iteration {it}: triggered by {trigger} ({reason})")
        if summary:
            lines.append(f"  Summary: {summary[:200]}")

    return "\n".join(lines)


def _format_fix_context_structured(fix_context: dict | str | None) -> str:
    if not fix_context:
        return "No additional context."
    if isinstance(fix_context, str):
        return fix_context

    lines = []
    reason = fix_context.get("reason", "unknown")
    lines.append(f"Reason: {reason}")

    if reason == "test_failure" or fix_context.get("test_failed"):
        test_analysis = fix_context.get("test_analysis", {})
        if test_analysis:
            summary = test_analysis.get("failure_summary", "")
            root_cause = test_analysis.get("root_cause", "")
            if summary:
                lines.append(f"Failure summary: {summary}")
            if root_cause:
                lines.append(f"Root cause: {root_cause}")

    if reason == "spec_compliance":
        spec_issues = fix_context.get("spec_issues", [])
        if spec_issues:
            lines.append("Spec issues:")
            for issue in spec_issues:
                priority = issue.get("priority", "high")
                msg = issue.get("message", "")
                lines.append(f"  - [{priority}] {msg}")

    if reason == "self_check":
        issues = fix_context.get("issues", [])
        if issues:
            lines.append("Self-check findings:")
            for issue in issues:
                severity = issue.get("severity", "high")
                desc = issue.get("description", "")
                location = issue.get("location", "")
                loc_suffix = f" @ {location}" if location else ""
                lines.append(f"  - [{severity}] {desc}{loc_suffix}")

    return "\n".join(lines) if lines else "No additional context."


def _format_spec_brief(spec_content: dict[str, str]) -> str:
    """Format spec content for the implement prompt."""
    if not spec_content:
        return "No project conventions specified."

    parts = []
    for name, content in spec_content.items():
        parts.append(f"### {name}\n{content}")

    return "\n".join(parts) if parts else "No project conventions specified."
