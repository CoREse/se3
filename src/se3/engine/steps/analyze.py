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
from typing import Any, Dict, List

from ..context_builder import ContextBuilder
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus, StepType, get_default_step_sequence
from ..project_context import ProjectContextCollector, list_spec_names
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

5. **selected_specs**: Based on the task scope and the available specs listed below, select which specifications are relevant to this task. Choose specs whose content would help understand the requirements, architecture, or conventions relevant to the task. Be selective — only include specs that are genuinely relevant. If no specs are relevant, return an empty list. Do NOT include "base" — it is always loaded automatically.

## Available Specs
{available_specs}

Respond in JSON format:
{{
    "task_type": "feature|bugfix|review|small|directive",
    "scope": "description of affected areas",
    "complexity": "simple|medium|complex",
    "reasoning": "explanation",
    "selected_specs": ["spec-name-1", "spec-name-2"]
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

    # (2) List available spec names
    builder = ContextBuilder(project_root)
    spec_names = list_spec_names(builder.specs_dir)
    available_specs = ", ".join(spec_names) if spec_names else "(none)"

    # Build prompt with project context and spec names
    prompt = ANALYZE_PROMPT.format(
        task_description=task_description,
        project_context=project_summary,
        available_specs=available_specs,
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
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count)
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
        selected_specs = result.get("selected_specs", [])
        spec_content, relevant_specs = _load_specs(
            builder, selected_specs, spec_names,
        )

        # Store outputs (original + new)
        step.outputs["task_type"] = task_type
        step.outputs["scope"] = result.get("scope", "")
        step.outputs["complexity"] = result.get("complexity", "medium")
        step.outputs["reasoning"] = result.get("reasoning", "")
        step.outputs["project_summary"] = project_summary
        step.outputs["relevant_specs"] = relevant_specs
        step.outputs["spec_content"] = spec_content

        # Update flow's selected steps based on task_type (fixed sequences)
        # Note: discover mode is handled separately via --discover flag, not by analyze
        _update_flow_steps(flow, resolved_task_type)

        logger.info(
            f"Analysis complete: type={resolved_task_type}, "
            f"complexity={result.get('complexity')}, "
            f"specs={relevant_specs}"
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


def _load_specs(
    builder: ContextBuilder,
    selected_specs: Any,
    known_spec_names: List[str],
) -> tuple[Dict[str, str], List[str]]:
    """Load spec content programmatically after LLM selection.

    Auto-loads base spec (if exists) regardless of LLM selection.
    Validates selected spec names against known specs and loads their content.

    Args:
        builder: ContextBuilder instance for spec loading
        selected_specs: List of spec names selected by LLM (may be invalid)
        known_spec_names: Valid spec names from the specs directory

    Returns:
        Tuple of (spec_content dict, relevant_specs list)
    """
    spec_content: Dict[str, str] = {}
    relevant_specs: List[str] = []

    # Auto-load base spec (always, regardless of LLM selection)
    base_content = builder._load_spec_content("base")
    if base_content:
        spec_content["base"] = base_content
        relevant_specs.append("base")
        logger.info(f"Auto-loaded base spec ({len(base_content)} chars)")

    # Validate and sanitize selected_specs from LLM
    if not isinstance(selected_specs, list):
        logger.warning(f"selected_specs is not a list ({type(selected_specs)}), using empty list")
        selected_specs = []

    for spec_name in selected_specs:
        if not isinstance(spec_name, str):
            continue
        # Skip base (already loaded) and unknown names
        if spec_name == "base":
            continue
        if spec_name not in known_spec_names:
            logger.warning(f"LLM selected unknown spec '{spec_name}', skipping")
            continue
        if spec_name in spec_content:
            continue

        content = builder._load_spec_content(spec_name)
        if content:
            spec_content[spec_name] = content
            relevant_specs.append(spec_name)
            logger.debug(f"Loaded spec: {spec_name} ({len(content)} chars)")
        else:
            logger.warning(f"Could not load spec content: {spec_name}")

    logger.info(f"Loaded {len(spec_content)} specs: {relevant_specs}")
    return spec_content, relevant_specs


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
