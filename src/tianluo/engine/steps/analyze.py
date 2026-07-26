"""Analyze step handler.

Analyzes the task description to determine:
- Task type (feature, bugfix, review, small, directive)
- Scope of changes
- Required steps for the workflow

It programmatically collects project context (replacing the former
PROJECT_SUMMARY step). Project-level conventions (the charter) and the
structural orientation map (the code-index top map) are injected into the
prompt; deeper function-level detail is pulled on demand by the agent via
``luo code-index show <path>``. The retired per-requirement spec index /
spec-selection mechanism has been removed — there is no longer a spec set to
select from.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from ..context import RUN_MODE_TYPES
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus, get_default_step_sequence
from ._project_root import resolve_flow_project_root
from ..project_context import ProjectContextCollector
from ..prompt_markers import inject_boundary
from ..utils.json_parser import parse_json_response
from ...config import (
    append_worktree_merge_steps,
    apply_step_config,
    insert_confirmation_steps,
)

logger = logging.getLogger(__name__)


ANALYZE_PROMPT = """You are an expert software engineering assistant. Analyze the following task description and determine:

1. **task_type**: The type of task. Choose from:
   - "feature": New functionality or significant enhancement (adds new capabilities)
   - "bugfix": Fixing a bug or issue (corrects incorrect behavior)
   - "review": Code review, audit, or analysis without code changes
   - "small": Minor fix, typo, or simple change (trivial scope, e.g., README update, comment fix)
   - "directive": Following specific instructions or requirements

   IMPORTANT: Do NOT use "discovery" - discovery mode is triggered separately via --discover flag.

2. **scope**: Brief description of what files/modules are likely affected

3. **complexity**: "simple", "medium", or "complex"

4. **reasoning**: Brief explanation of your classification

Before reading source, consult the code-index map (injected below) to locate the
relevant modules / symbols; pull deeper detail on demand via
``luo code-index show <path>`` rather than reading whole files blindly. To find
items by keyword or regex, use ``luo code-index search <pattern>`` instead of
``grep 'pattern' tianluo/code-index.md`` — each hit carries the item's full locating
path (a symbol renders as ``relpath::local_id``, which a raw grep line cannot
show); its syntax matches grep (regex ``pattern`` by default, ``-i``/``-F``/``-m``).

Respond in JSON format:
{{
    "task_type": "feature|bugfix|review|small|directive",
    "scope": "description of affected areas",
    "complexity": "simple|medium|complex",
    "reasoning": "explanation"
}}

Task description:
---
{task_description}
---

Project context:
{project_context}
"""

# Splice the two-segment sentinel markers (TEMPLATE_PREFIX_END /
# USER_CONTENT_BEGIN) right before the ``Task description:`` block.
#
# The ``analyze`` step has no user-literal field at the prompt-assembly point:
# ``task_description`` carries either the upstream ``refined_description``
# (when discovery preceded) or composed framework text (base + recorded
# interjections). Per the three-segment marker protocol the USER_CONTENT
# section is therefore empty; we intentionally stick with the legacy
# two-segment ``inject_boundary`` call so the web console falls back to
# rendering the whole post-BEGIN tail inside the collapsed system-prompt chip.
ANALYZE_PROMPT = inject_boundary(ANALYZE_PROMPT, "Task description:\n")


def analyze_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the analyze step.

    This is the "super-analyze" handler that combines:
    1. Programmatic pre-processing: project context collection + spec name listing
    2. Single LLM call: task classification + spec selection
    3. Programmatic post-processing: spec content loading (base + selected)

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    # Prefer refined_description from discovery step over raw task_description
    task_description = step.inputs.get("refined_description") or step.inputs.get("task_description", "")

    if not task_description:
        step.error_message = "No task description provided"
        return StepStatus.FAILED

    project_root = resolve_flow_project_root(flow)

    # --- Pre-processing (programmatic, no LLM) ---

    # Collect structured project context
    project_summary = _collect_project_summary(project_root)

    # Build prompt with project context
    prompt = ANALYZE_PROMPT.format(
        task_description=task_description,
        project_context=project_summary,
    )

    # Append issue discovery injection if applicable
    from ..context_builder import (
        get_issue_discovery_injection,
        get_charter_injection,
        get_code_index_injection,
        ensure_code_index_fresh,
        get_runtime_environment_injection,
    )
    injection = get_issue_discovery_injection("analyze", project_root)
    if injection:
        prompt += injection
    # Inject the project charter (full text) + code-index top map: project-level
    # conventions and the structural orientation map (function-level detail
    # pulled on demand via `luo code-index show <path>`).
    prompt += get_charter_injection(project_root)
    # Read-side freshness boundary: analyze is the FIRST step of the read/plan
    # run (analyze → plan → plan_tasks → implement → self_check), and code is not
    # edited until implement. So a single incremental refresh here gives every
    # read/plan step one consistent, current map — no per-step rebuild. The only
    # OTHER refresh point in a flow is just before `git commit` (commit.py), which
    # folds the code implement just wrote into the committed map. See that call
    # site for the write-side rationale.
    ensure_code_index_fresh(project_root)
    prompt += get_code_index_injection(project_root)
    runtime_env = get_runtime_environment_injection("analyze", project_root)
    if runtime_env:
        prompt += runtime_env

    logger.info(f"Analyzing task: {task_description[:60]}...")

    try:
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count, fix_iteration=step.inputs.get("fix_iteration", 0))

        # Schema hint for TWO_PHASE mode: if Phase 1 produces markdown prose
        # (not JSON), Phase 2 extraction needs the expected structure.
        ANALYZE_SCHEMA_HINT = (
            '{"task_type": "feature|bugfix|review|small|directive", '
            '"scope": "description of affected areas", '
            '"complexity": "simple|medium|complex", '
            '"reasoning": "explanation"}'
        )

        # --- LLM call: task classification ---
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint=ANALYZE_SCHEMA_HINT,
            required_keys=["task_type"],
        )

        # Parse JSON response
        result = parse_json_response(response, required_keys=["task_type"])
        if not result:
            step.error_message = "Failed to parse LLM response"
            return StepStatus.FAILED

        # Extract task_type from analyze result (discovery only valid with --discover flag)
        resolved_task_type = _extract_task_type(result, flow)

        # Explicit --type flag overrides LLM analysis
        resolved_task_type = _handle_type_conflict(flow, resolved_task_type)

        # Persist the real analyzed task type (sanitized to never be a run mode
        # like 'discovery') SEPARATELY from resolved_task_type / flow.task_type.
        # The latter deliberately stay as-is (possibly 'discovery') so the step
        # sequence & resume are untouched; commit-message / version consumers
        # read this field via effective_task_type so a --discover run is
        # prefixed with the type analyze actually inferred, not 'discovery'.
        analyzed_type = _sanitize_analyzed_type(result)
        flow.state.context["analyzed_type"] = analyzed_type

        # Update state with resolved task type
        flow.state.update_task_type(resolved_task_type)
        flow.task_type = resolved_task_type

        # Store outputs. Use the authoritative resolved value (after the
        # --discover preservation and --type override) so step.outputs agrees
        # with flow.task_type rather than diverging from a separately defaulted
        # raw value. The retired spec-selection mechanism no longer produces
        # ``spec_content`` / ``relevant_specs`` / ``selected_items`` — downstream
        # steps instead receive the charter + code-index injection. These keys are
        # kept (empty) so any defensive ``.get()`` consumers degrade cleanly.
        step.outputs["task_type"] = resolved_task_type
        step.outputs["analyzed_type"] = analyzed_type
        step.outputs["scope"] = result.get("scope", "")
        step.outputs["complexity"] = result.get("complexity", "medium")
        step.outputs["reasoning"] = result.get("reasoning", "")
        step.outputs["project_summary"] = project_summary
        step.outputs["relevant_specs"] = []
        step.outputs["spec_content"] = ""
        step.outputs["selected_items"] = []

        # Update flow's selected steps based on task_type (fixed sequences)
        # Note: discover mode is handled separately via --discover flag, not by analyze
        _update_flow_steps(flow, resolved_task_type)

        logger.info(
            f"Analysis complete: type={resolved_task_type}, "
            f"complexity={result.get('complexity')}"
        )

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Analyze step failed")
        step.error_message = f"Analysis failed: {str(e)}"
        return StepStatus.FAILED


def _extract_task_type(analyze_output: dict, flow: FlowInstance) -> str:
    """Extract task_type from LLM analyze output.

    Args:
        analyze_output: The parsed JSON output from analyze step
        flow: The flow instance (for checking explicit_type)

    Returns:
        The extracted task type string
    """
    valid_types = ["feature", "bugfix", "review", "small", "directive"]
    task_type = analyze_output.get("task_type", "feature")
    
    # Discovery mode can ONLY be triggered by the --discover flag, never by
    # analyze on its own. If analyze returns "discovery", preserve it ONLY when
    # --discover was actually set (explicit_type == "discovery"); otherwise
    # downgrade to "feature".
    if task_type == "discovery":
        explicit_type = flow.state.context.get("explicit_type")
        if explicit_type == "discovery":
            # --discover is set, so "discovery" is a legitimate, intended value.
            # Return early so the valid_types check below (which excludes
            # "discovery") does not overwrite it with "feature".
            return "discovery"
        logger.warning(f"Analyze returned 'discovery' but --discover flag not set, treating as 'feature'")
        task_type = "feature"

    # Validate and normalize task type
    if task_type not in valid_types:
        logger.warning(f"Invalid task_type '{task_type}' from analyze, defaulting to 'feature'")
        task_type = "feature"

    return task_type


def _sanitize_analyzed_type(analyze_output: dict) -> str:
    """Return the LLM's real task type, never a run mode (e.g. 'discovery').

    This is the value persisted as ``analyzed_type`` and consumed via
    ``effective_task_type`` by the commit-message / version steps, so it must be
    clean at the source: a ``--discover`` run whose LLM echoes 'discovery' (or
    returns anything invalid) degrades to 'feature' here, keeping the downstream
    helper's fallback logic trivial.
    """
    valid_types = ("feature", "bugfix", "review", "small", "directive")
    task_type = analyze_output.get("task_type", "feature")
    if task_type in RUN_MODE_TYPES or task_type not in valid_types:
        return "feature"
    return task_type


def _handle_type_conflict(flow: FlowInstance, resolved_type: str) -> str:
    """Check if explicit --type flag should override LLM analysis.

    When user explicitly specifies --type, that takes precedence over
    whatever the LLM classified the task as.

    Args:
        flow: The flow instance containing context
        resolved_type: The task type determined by analyze step

    Returns:
        The final task type to use (explicit overrides analyzed)
    """
    explicit_type = flow.state.context.get("explicit_type")

    if explicit_type and explicit_type != resolved_type:
        logger.info(
            f"Explicit --type='{explicit_type}' overrides "
            f"analyzed type='{resolved_type}'"
        )
        return explicit_type

    return resolved_type


def _collect_project_summary(project_root: Path) -> str:
    """Collect structured project context and format as text summary.

    Uses ProjectContextCollector to gather git status, flow engine state,
    backlog, and spec list, then formats them into a concise text block.
    This replaces the former PROJECT_SUMMARY LLM step with a programmatic
    approach — no LLM call needed.

    Args:
        project_root: Project root directory

    Returns:
        Formatted project context string
    """
    try:
        collector = ProjectContextCollector(project_root)
        raw = collector.collect()
    except Exception as e:
        logger.debug(f"Failed to collect project context: {e}")
        return "No additional context available"

    parts: List[str] = []

    # Git status
    git = raw.get("git", {})
    branch = git.get("branch", "unknown")
    uncommitted = git.get("uncommitted_count", 0)
    parts.append(f"Branch: {branch}")
    if uncommitted:
        parts.append(f"Uncommitted changes: {uncommitted}")
    commits = git.get("last_commits", [])
    if commits:
        parts.append("Recent commits:")
        for c in commits[:5]:
            parts.append(f"  - {c}")

    # Flow engine
    flow_engine = raw.get("flow_engine")
    if flow_engine:
        active = flow_engine.get("active_flows", [])
        if active:
            parts.append(f"Active flows: {len(active)}")
            for f in active[:3]:
                parts.append(f"  - {f.get('description', 'unknown')}")

    # Backlog highlights
    backlog = raw.get("backlog", [])
    if backlog:
        parts.append(f"Backlog items: {len(backlog)}")
        for item in backlog[:5]:
            status = item.get("status", "?")
            title = item.get("title", item.get("slug", "?"))
            parts.append(f"  - [{status}] {title}")

    # Specs
    specs = raw.get("specs", [])
    if specs:
        parts.append(f"Available specs: {', '.join(specs)}")

    return "\n".join(parts) if parts else "No additional context available"


def _update_flow_steps(
    flow: FlowInstance,
    task_type: str,
) -> None:
    """Update the flow's selected steps based on task type.
    
    Uses predefined step sequences for each task type.
    Discover mode is handled separately via --discover flag.
    Also inserts CONFIRM steps based on configuration.

    Args:
        flow: The flow instance to update
        task_type: The determined task type (feature, bugfix, small, review, directive)
    """
    # Get default sequence for task type (fixed sequences per spec)
    selected_steps = get_default_step_sequence(task_type)

    project_root = resolve_flow_project_root(flow)

    # Append optional steps from tianluo.yaml (e.g. summarize).
    # _update_flow_steps rebuilds from the default sequence every time, so
    # applying the config once here mirrors state_machine.create_flow's
    # (default -> apply_step_config -> [worktree merge steps] ->
    # insert_confirmation_steps) order and keeps configured steps from being
    # dropped by the rebuild. apply_step_config dedups by step value, so this
    # never appends duplicates.
    selected_steps = apply_step_config(selected_steps, project_root)

    # A worktree flow's release point is the merge: the two merge-side steps
    # appended by StateMachine.create_flow must survive this analyze-time
    # re-derivation, otherwise the rebuilt sequence ends at summarize and the
    # branch never lands on master (_finalize_worktree_cleanup then misdiagnoses
    # the flow as predating the in-flow merge steps and exits 1). ANALYZE is the
    # first step of every task-type sequence and always triggers this rebuild,
    # so re-appending here is what actually makes worktree merges run.
    if getattr(flow, "is_worktree_mode", False):
        selected_steps = append_worktree_merge_steps(selected_steps)

    # Insert confirmation steps based on config
    # This ensures CONFIRM steps are added after plan as configured
    flow.state.selected_steps = insert_confirmation_steps(selected_steps, project_root)
    
    logger.info(f"Using step sequence for {task_type}: {[s.value for s in flow.state.selected_steps]}")
