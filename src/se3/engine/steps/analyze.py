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

from ..llm_caller import LLMCaller, LLMCallError
from ..models import FlowInstance, Step, StepStatus, StepType, get_default_step_sequence

logger = logging.getLogger(__name__)


ANALYZE_PROMPT = """You are an expert software engineering assistant. Analyze the following task description and determine:

1. **task_type**: The type of task. Choose from:
   - "feature": New functionality or significant enhancement
   - "bugfix": Fixing a bug or issue
   - "review": Code review, audit, or analysis without code changes
   - "small": Minor fix, typo, or simple change (trivial scope)
   - "directive": Following specific instructions or requirements

2. **scope**: Brief description of what files/modules are likely affected

3. **complexity**: "simple", "medium", or "complex"

4. **required_steps**: List of step names needed (choose from: analyze, read_spec, propose, design, plan_tasks, implement, test, verify_spec, update_spec, commit, summarize)
   - Always include "analyze" (already done)
   - "small" tasks can skip propose/design
   - "review" tasks only need analyze, read_spec, verify_spec, summarize
   - "bugfix" may skip propose but usually needs design if complex

5. **reasoning**: Brief explanation of your classification

Respond in JSON format:
{{
    "task_type": "feature|bugfix|review|small|directive",
    "scope": "description of affected areas",
    "complexity": "simple|medium|complex",
    "required_steps": ["step1", "step2", ...],
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
    task_description = step.inputs.get("task_description", "")
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

    logger.info(f"Analyzing task: {task_description[:60]}...")

    try:
        # Call LLM for analysis
        project_root = flow.change_path.parent if flow.change_path else Path.cwd()
        caller = LLMCaller(project_root)
        response = caller.call(prompt=prompt)

        # Parse JSON response
        result = _parse_analyze_response(response)

        if not result:
            step.error_message = "Failed to parse LLM response"
            return StepStatus.FAILED

        # Validate task_type
        valid_types = ["feature", "bugfix", "review", "small", "directive"]
        task_type = result.get("task_type", "feature")
        if task_type not in valid_types:
            logger.warning(f"Invalid task_type '{task_type}', defaulting to 'feature'")
            task_type = "feature"

        # Store outputs
        step.outputs["task_type"] = task_type
        step.outputs["scope"] = result.get("scope", "")
        step.outputs["complexity"] = result.get("complexity", "medium")
        step.outputs["required_steps"] = result.get("required_steps", [])
        step.outputs["reasoning"] = result.get("reasoning", "")

        # Update flow's selected steps based on analysis
        _update_flow_steps(flow, result.get("required_steps", []), task_type)

        logger.info(f"Analysis complete: type={task_type}, complexity={result.get('complexity')}")
        logger.debug(f"Required steps: {result.get('required_steps')}")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Analyze step failed")
        step.error_message = f"Analysis failed: {str(e)}"
        return StepStatus.FAILED


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


def _parse_analyze_response(response: str) -> dict[str, Any] | None:
    """Parse the LLM response into a structured result.

    Args:
        response: Raw LLM response string

    Returns:
        Parsed dictionary or None if parsing fails
    """
    try:
        # Try to extract JSON from the response
        response = response.strip()

        # Remove markdown code block if present
        if response.startswith("```json"):
            response = response[7:]
        elif response.startswith("```"):
            response = response[3:]

        if response.endswith("```"):
            response = response[:-3]

        response = response.strip()

        # Try to find JSON object boundaries
        # Handle case where LLM adds extra text after JSON
        json_start = response.find('{')
        json_end = response.rfind('}')

        if json_start == -1 or json_end == -1 or json_end <= json_start:
            logger.warning("No JSON object found in response")
            return None

        # Extract just the JSON part
        json_str = response[json_start:json_end + 1]

        # Parse JSON
        result = json.loads(json_str)

        # Validate required fields
        if "task_type" not in result:
            logger.warning("Missing 'task_type' in analyze response")
            return None

        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        # Log the problematic response for debugging
        logger.debug(f"Response content: {response[:500]}...")
        return None
    except Exception as e:
        logger.error(f"Unexpected error parsing response: {e}")
        return None


def _update_flow_steps(
    flow: FlowInstance,
    required_steps: list[str],
    task_type: str,
) -> None:
    """Update the flow's selected steps based on analysis result.

    Args:
        flow: The flow instance to update
        required_steps: List of required step names from analysis
        task_type: The determined task type
    """
    # If no specific steps provided, use default sequence for task type
    if not required_steps:
        flow.state.selected_steps = get_default_step_sequence(task_type)
        logger.info(f"Using default step sequence for {task_type}")
        return

    # Map step names to StepType enum
    step_type_map = {
        "analyze": StepType.ANALYZE,
        "read_spec": StepType.READ_SPEC,
        "propose": StepType.PROPOSE,
        "design": StepType.DESIGN,
        "plan_tasks": StepType.PLAN_TASKS,
        "implement": StepType.IMPLEMENT,
        "test": StepType.TEST,
        "verify_spec": StepType.VERIFY_SPEC,
        "update_spec": StepType.UPDATE_SPEC,
        "commit": StepType.COMMIT,
        "summarize": StepType.SUMMARIZE,
    }

    selected = []
    for step_name in required_steps:
        step_name_normalized = step_name.lower().replace("-", "_")
        if step_name_normalized in step_type_map:
            selected.append(step_type_map[step_name_normalized])
        else:
            logger.warning(f"Unknown step name: {step_name}")

    # Ensure analyze is first if present (it's already done)
    if selected and selected[0] != StepType.ANALYZE:
        # Insert analyze at the beginning for tracking
        selected.insert(0, StepType.ANALYZE)

    # Update flow state
    if selected:
        flow.state.selected_steps = selected
        logger.info(f"Updated step sequence: {[s.value for s in selected]}")
    else:
        # Fallback to default
        flow.state.selected_steps = get_default_step_sequence(task_type)
        logger.info(f"Falling back to default sequence for {task_type}")
