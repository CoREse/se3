"""Analyze step handler.

Analyzes the task description to determine:
- Task type (feature, bugfix, review, small, directive)
- Scope of changes
- Required steps for the workflow
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus, StepType, get_default_step_sequence
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


def analyze_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the analyze step.

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

    # Gather project context
    project_context = _gather_project_context(flow)

    # Build prompt
    prompt = ANALYZE_PROMPT.format(
        task_description=task_description,
        project_context=project_context,
    )

    # Append issue discovery injection if applicable
    from ..context_builder import get_issue_discovery_injection
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    injection = get_issue_discovery_injection("analyze", project_root)
    if injection:
        prompt += injection

    logger.info(f"Analyzing task: {task_description[:60]}...")

    try:
        # Call LLM for analysis
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count)
        response = caller.call(prompt=prompt, require_json=True)

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

        # Store outputs
        step.outputs["task_type"] = task_type
        step.outputs["scope"] = result.get("scope", "")
        step.outputs["complexity"] = result.get("complexity", "medium")
        step.outputs["reasoning"] = result.get("reasoning", "")

        # Update flow's selected steps based on task_type (fixed sequences)
        # Note: discover mode is handled separately via --discover flag, not by analyze
        _update_flow_steps(flow, resolved_task_type)

        logger.info(f"Analysis complete: type={resolved_task_type}, complexity={result.get('complexity')}")

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


def _gather_project_context(flow: FlowInstance) -> str:
    """Gather relevant project context for analysis.

    Args:
        flow: The flow instance

    Returns:
        String containing project context information
    """
    context_parts = []

    # Check for existing change
    if flow.change_name:
        context_parts.append(f"Active change: {flow.change_name}")

    # Check for project structure
    project_root = Path(flow.change_path).parent if flow.change_path else Path.cwd()

    # Look for common project indicators
    if (project_root / "pyproject.toml").exists():
        context_parts.append("Project type: Python (pyproject.toml found)")
    elif (project_root / "package.json").exists():
        context_parts.append("Project type: Node.js (package.json found)")
    elif (project_root / "Cargo.toml").exists():
        context_parts.append("Project type: Rust (Cargo.toml found)")
    elif (project_root / "go.mod").exists():
        context_parts.append("Project type: Go (go.mod found)")

    # Check for test framework
    if (project_root / "pytest.ini").exists() or (project_root / "setup.py").exists():
        context_parts.append("Testing: pytest")

    # Check for specs
    spec_dir = project_root / "specs"
    if not spec_dir.exists():
        spec_dir = project_root / "openspec" / "specs"
    if spec_dir.exists():
        spec_count = len(list(spec_dir.glob("**/*.md")))
        if spec_count > 0:
            context_parts.append(f"Specs: {spec_count} found")

    return "\n".join(context_parts) if context_parts else "No additional context available"


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
    # This ensures CONFIRM steps are added after propose/design as configured
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    flow.state.selected_steps = insert_confirmation_steps(selected_steps, project_root)
    
    logger.info(f"Using step sequence for {task_type}: {[s.value for s in flow.state.selected_steps]}")
