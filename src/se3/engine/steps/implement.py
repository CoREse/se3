"""Implement step handler.

Executes implementation of task groups, writing code to files.
Supports fix iterations for the test-verify-fix loop.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..models import FlowInstance, Step, StepStatus

logger = logging.getLogger(__name__)


def implement_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the implement step.

    Implements the task groups by writing code to files.
    In fix iterations, focuses on fixing issues identified by verify_spec.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_groups = step.inputs.get("task_groups", [])
    design_doc = step.inputs.get("design_doc", {})
    fix_context = step.inputs.get("fix_context")
    fix_instructions = step.inputs.get("fix_instructions")
    is_fix_iteration = step.inputs.get("is_fix_iteration", False)
    fix_iteration = step.inputs.get("fix_iteration", 0)

    if is_fix_iteration:
        logger.info(f"Running fix iteration {fix_iteration}")
        logger.info(f"Fix instructions: {fix_instructions[:200] if fix_instructions else 'None'}...")

    # TODO: Full implementation in another task group
    # For now, store minimal outputs to allow testing
    step.outputs["files_changed"] = step.outputs.get("files_changed", [])
    step.outputs["implemented_groups"] = step.inputs.get("task_groups", [])

    # Implement-Test contract: declare test artifacts
    # When fully implemented, the LLM will populate these via the prompt template
    step.outputs["tests_added"] = step.outputs.get("tests_added", [])
    step.outputs["test_mapping"] = step.outputs.get("test_mapping", {})

    return StepStatus.COMPLETED
