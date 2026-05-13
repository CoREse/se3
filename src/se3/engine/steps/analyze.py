"""Analyze step handler.

Analyzes the task description to determine:
- Task type (feature, bugfix, review, small, directive)
- Scope of changes
- Required steps for the workflow
- Relevant specs to load for downstream steps

This is the "super-analyze" step: it combines task classification with
spec selection in a single LLM call, and programmatically collects
project context (replacing the former PROJECT_SUMMARY step) and loads
spec content (replacing the former READ_SPEC step).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, List

from ..context_builder import ContextBuilder
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus, StepType, get_default_step_sequence
from ..project_context import ProjectContextCollector, list_spec_names
from ..spec_index import load_or_build
from ..spec_loader import load_for_step
from ..utils.json_parser import parse_json_response
from ...config import insert_confirmation_steps

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

5. **selected_items**: Based on the task scope and the available spec items listed below, select which individual Requirements are relevant to this task. Be selective — only include items that are genuinely relevant. The base spec is always loaded automatically, so you do NOT need to select items from it.

## Available Items
{available_items}

Respond in JSON format:
{{
    "task_type": "feature|bugfix|review|small|directive",
    "scope": "description of affected areas",
    "complexity": "simple|medium|complex",
    "reasoning": "explanation",
    "selected_items": [
        {{"spec": "spec-name", "requirement_name": "Requirement Name"}}
    ]
}}

Task description:
---
{task_description}
---

Project context:
{project_context}
"""


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

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # --- Pre-processing (programmatic, no LLM) ---

    # (1) Collect structured project context
    project_summary = _collect_project_summary(project_root)

    # (2) Build item-level index and list available items for selector
    builder = ContextBuilder(project_root)
    index = load_or_build(project_root)
    selector_items = index.list_for_selector()
    available_items = _format_selector_items(selector_items)

    # (3) List spec names for validating LLM-returned selected_items
    spec_names = list_spec_names(builder.specs_dir)

    # Build prompt with project context and available items
    prompt = ANALYZE_PROMPT.format(
        task_description=task_description,
        project_context=project_summary,
        available_items=available_items,
    )

    # Append issue discovery injection if applicable
    from ..context_builder import get_issue_discovery_injection
    injection = get_issue_discovery_injection("analyze", project_root)
    if injection:
        prompt += injection

    logger.info(f"Analyzing task: {task_description[:60]}...")

    try:
        # --- LLM call: task classification + spec selection ---
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count, fix_iteration=step.inputs.get("fix_iteration", 0))
        response = caller.call(prompt=prompt, json_mode="two_phase")

        # Parse JSON response
        result = parse_json_response(response, required_keys=["task_type"])

        if not result:
            step.error_message = "Failed to parse LLM response"
            return StepStatus.FAILED

        # Validate task_type
        valid_types = ["feature", "bugfix", "review", "small", "directive"]
        task_type = result.get("task_type", "feature")
        if task_type not in valid_types:
            logger.warning(f"Invalid task_type '{task_type}', defaulting to 'feature'")
            task_type = "feature"

        # Extract task_type from analyze result (discovery only valid with --discover flag)
        resolved_task_type = _extract_task_type(result, flow)

        # Explicit --type flag overrides LLM analysis
        resolved_task_type = _handle_type_conflict(flow, resolved_task_type)

        # Update state with resolved task type
        flow.state.update_task_type(resolved_task_type)
        flow.task_type = resolved_task_type

        # --- Post-processing: load spec content programmatically ---
        # Parse selected_items (new primary format) with fallback to selected_specs
        selected_items = result.get("selected_items", [])

        # Fallback: if LLM returned old-format selected_specs but no selected_items,
        # map spec names to all their requirements
        if not selected_items and result.get("selected_specs"):
            logger.warning(
                "LLM returned legacy selected_specs instead of selected_items; "
                "falling back to full-spec loading for each selected spec. "
                "This defeats item-level loading — consider re-prompting for selected_items."
            )
            raw_specs = result.get("selected_specs", [])
            # Validate spec names before fallback so unknown specs are logged
            # rather than silently producing an empty item list.
            if spec_names:
                unknown = [s for s in raw_specs if s not in spec_names]
                if unknown:
                    logger.warning(
                        "Filtering out unknown specs from selected_specs: %r",
                        unknown,
                    )
                raw_specs = [s for s in raw_specs if s in spec_names]
            selected_items = _fallback_items_from_specs(index, raw_specs)

        # Validate selected_items
        if not isinstance(selected_items, list):
            logger.warning(
                "selected_items is not a list (%s), using empty list",
                type(selected_items).__name__,
            )
            selected_items = []

        # Filter out items with hallucinated spec names (spec doesn't exist)
        if spec_names:
            valid_items = []
            for item in selected_items:
                if isinstance(item, dict) and item.get("spec") in spec_names:
                    valid_items.append(item)
                elif isinstance(item, dict):
                    logger.warning(
                        "Filtering out selected_items entry with unknown spec: %r",
                        item.get("spec"),
                    )
            selected_items = valid_items

        # Use spec_loader to assemble spec content
        load_result = load_for_step(
            step_type="analyze",
            selected_items=selected_items,
            project_root=project_root,
            mode="items",
        )

        # Defensive: detect hallucinated requirement names that passed spec-name
        # validation but don't exist in the actual spec files.
        selected_ids = {
            f"{item.get('spec')}::{item.get('requirement_name')}"
            for item in selected_items
            if isinstance(item, dict) and item.get("spec") and item.get("requirement_name")
        }
        # Exclude base-spec selections (base is always loaded as full text)
        non_base_selected_ids = {sid for sid in selected_ids if not sid.startswith("base::")}
        loaded_ids = set(load_result.loaded_items)
        if non_base_selected_ids and loaded_ids < non_base_selected_ids:
            missing = sorted(non_base_selected_ids - loaded_ids)
            logger.warning(
                "LLM selected %d non-base item(s) but only %d loaded — "
                "hallucinated requirement names: %r",
                len(non_base_selected_ids), len(loaded_ids), missing,
            )

        # Store outputs
        step.outputs["task_type"] = task_type
        step.outputs["scope"] = result.get("scope", "")
        step.outputs["complexity"] = result.get("complexity", "medium")
        step.outputs["reasoning"] = result.get("reasoning", "")
        step.outputs["project_summary"] = project_summary
        step.outputs["relevant_specs"] = load_result.relevant_specs
        step.outputs["spec_content"] = load_result.text
        step.outputs["selected_items"] = selected_items

        # Persist selected_items in flow context for cross-session /
        # cross-CONFIRM resilience — downstream steps that scan history
        # for selected_items will find it even if the ANALYZE step object
        # is not directly reachable.
        flow.state.context["selected_items"] = selected_items

        # Update flow's selected steps based on task_type (fixed sequences)
        # Note: discover mode is handled separately via --discover flag, not by analyze
        _update_flow_steps(flow, resolved_task_type)

        logger.info(
            f"Analysis complete: type={resolved_task_type}, "
            f"complexity={result.get('complexity')}, "
            f"specs={load_result.relevant_specs}, "
            f"items={len(selected_items)}"
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
    
    # Discovery mode can ONLY be triggered by --discover flag, never by analyze
    # If analyze returns "discovery", treat it as "feature"
    if task_type == "discovery":
        explicit_type = flow.state.context.get("explicit_type")
        if explicit_type != "discovery":
            logger.warning(f"Analyze returned 'discovery' but --discover flag not set, treating as 'feature'")
            task_type = "feature"
    
    # Validate and normalize task type
    if task_type not in valid_types:
        logger.warning(f"Invalid task_type '{task_type}' from analyze, defaulting to 'feature'")
        task_type = "feature"

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


def _format_selector_items(items: list[dict[str, Any]]) -> str:
    """Format the item list for injection into the analyze prompt.

    Each line shows: ``- spec::Requirement Name [tags: foo, bar] — summary``

    Args:
        items: Output of ``SpecIndex.list_for_selector()``.

    Returns:
        Formatted multi-line string.
    """
    if not items:
        return "(no items available)"

    lines: list[str] = []
    current_spec: str = ""
    for item in items:
        spec = item.get("spec", "")
        if not spec:
            continue  # Defensive: skip items with missing spec name
        if spec != current_spec:
            heading = f"### {spec}"
            if lines:
                heading = f"\n{heading}"
            lines.append(heading)
            current_spec = spec
        name = item.get("requirement_name", "")
        tags = item.get("tags", [])
        summary = item.get("summary", "")
        tag_str = f" [tags: {', '.join(tags)}]" if tags else ""
        summary_str = f" — {summary}" if summary else ""
        lines.append(f"- {spec}::{name}{tag_str}{summary_str}")

    return "\n".join(lines)


def _fallback_items_from_specs(
    index: Any,
    selected_specs: list[str],
) -> list[dict[str, str]]:
    """Map old-format ``selected_specs`` to ``selected_items``.

    For each selected spec name, include ALL Requirements from that spec.
    This is a best-effort fallback when an older LLM returns the legacy
    ``selected_specs`` array.

    Args:
        index: A ``SpecIndex`` (or anything with ``.items`` dict).
        selected_specs: List of spec names from legacy output.

    Returns:
        List of ``{"spec": str, "requirement_name": str}`` dicts.
    """
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for spec_name in selected_specs:
        if not isinstance(spec_name, str):
            continue
        for item_id, meta in getattr(index, "items", {}).items():
            if item_id.startswith(f"{spec_name}::"):
                req_name = getattr(meta, "requirement_name", "")
                if req_name and req_name != "__no_requirements__":
                    key = f"{spec_name}::{req_name}"
                    if key not in seen:
                        seen.add(key)
                        items.append({"spec": spec_name, "requirement_name": req_name})
    return items


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
    
    # Insert confirmation steps based on config
    # This ensures CONFIRM steps are added after plan as configured
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    flow.state.selected_steps = insert_confirmation_steps(selected_steps, project_root)
    
    logger.info(f"Using step sequence for {task_type}: {[s.value for s in flow.state.selected_steps]}")
