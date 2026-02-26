"""JSON Schema definitions for SE3 state files.

This module defines the formal schemas for:
- engine.json: Flow engine state persistence
- context.json: AI context export for handoff/resumption

These schemas document the structure and provide validation capabilities.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, TypedDict


# ============================================================================
# engine.json Schema
# ============================================================================

class StepStatusValue(str, Enum):
    """Valid step status values."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    PAUSED = "paused"


class StepTypeValue(str, Enum):
    """Valid step type values."""

    ANALYZE = "analyze"
    READ_SPEC = "read_spec"
    PROPOSE = "propose"
    DESIGN = "design"
    PLAN_TASKS = "plan_tasks"
    IMPLEMENT = "implement"
    TEST = "test"
    VERIFY_SPEC = "verify_spec"
    UPDATE_SPEC = "update_spec"
    COMMIT = "commit"
    SUMMARIZE = "summarize"


class FlowStatusValue(str, Enum):
    """Valid flow status values."""

    INIT = "init"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    RECOVERING = "recovering"


class StepSchema(TypedDict, total=False):
    """Schema for a single step in engine.json.

    Example:
        {
            "step_id": "a1b2c3d4",
            "step_type": "analyze",
            "status": "completed",
            "started_at": "2026-02-24T10:30:00",
            "completed_at": "2026-02-24T10:31:15",
            "retry_count": 0,
            "max_retries": 3,
            "model": "claude-opus-4-6",
            "fallback_model": "claude-haiku-3-5",
            "inputs": {"task_description": "Implement feature X"},
            "outputs": {"task_type": "feature", "scope": "backend"},
            "artifacts": ["src/feature.py"],
            "error_message": null,
            "error_details": null
        }
    """

    step_id: str
    step_type: str  # StepTypeValue
    status: str  # StepStatusValue
    started_at: Optional[str]  # ISO format datetime
    completed_at: Optional[str]  # ISO format datetime
    retry_count: int
    max_retries: int
    model: Optional[str]
    fallback_model: Optional[str]
    inputs: Dict[str, Any]
    outputs: Dict[str, Any]
    artifacts: List[str]  # File paths as strings
    error_message: Optional[str]
    error_details: Optional[str]


class StateSchema(TypedDict, total=False):
    """Schema for the state object in engine.json.

    Tracks current step, step history, and execution context.

    Example:
        {
            "current_step_id": "a1b2c3d4",
            "step_history": ["a1b2c3d4", "e5f6g7h8"],
            "steps": {"a1b2c3d4": {...}, "e5f6g7h8": {...}},
            "context": {"task_description": "...", "task_type": "feature"},
            "selected_steps": ["analyze", "read_spec", "propose", ...],
            "current_step_index": 2
        }
    """

    current_step_id: Optional[str]
    step_history: List[str]
    steps: Dict[str, StepSchema]
    context: Dict[str, Any]
    selected_steps: List[str]  # StepTypeValue values
    current_step_index: int


class FlowInstanceSchema(TypedDict, total=False):
    """Root schema for engine.json.

    Top-level container for a single flow execution.

    Example:
        {
            "flow_id": "abc123def456",
            "status": "running",
            "task_description": "Implement feature X",
            "task_type": "feature",
            "state": {...},
            "created_at": "2026-02-24T10:00:00",
            "updated_at": "2026-02-24T10:30:00",
            "completed_at": null,
            "change_name": "feature-x-implementation",
            "change_path": "specs/_changelog/feature-x",
            "is_loop_mode": false,
            "loop_branch": null
        }
    """

    flow_id: str
    status: str  # FlowStatusValue
    task_description: str
    task_type: Optional[str]
    state: StateSchema
    created_at: str  # ISO format datetime
    updated_at: str  # ISO format datetime
    completed_at: Optional[str]  # ISO format datetime
    change_name: Optional[str]
    change_path: Optional[str]
    is_loop_mode: bool
    loop_branch: Optional[str]


ENGINE_JSON_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SE3 Flow Engine State",
    "description": "State persistence for SE3 flow engine",
    "type": "object",
    "required": ["flow_id", "status", "task_description", "state", "created_at", "updated_at"],
    "properties": {
        "flow_id": {"type": "string", "description": "Unique flow identifier"},
        "status": {
            "type": "string",
            "enum": ["init", "running", "paused", "completed", "failed", "recovering"],
            "description": "Current flow status"
        },
        "task_description": {"type": "string", "description": "User's task description"},
        "task_type": {"type": ["string", "null"], "description": "Task type (feature, bugfix, etc.)"},
        "state": {
            "type": "object",
            "required": ["steps", "selected_steps"],
            "properties": {
                "current_step_id": {"type": ["string", "null"]},
                "step_history": {"type": "array", "items": {"type": "string"}},
                "steps": {"type": "object"},
                "context": {"type": "object"},
                "selected_steps": {"type": "array", "items": {"type": "string"}},
                "current_step_index": {"type": "integer"}
            }
        },
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
        "completed_at": {"type": ["string", "null"], "format": "date-time"},
        "change_name": {"type": ["string", "null"]},
        "change_path": {"type": ["string", "null"]},
        "is_loop_mode": {"type": "boolean"},
        "loop_branch": {"type": ["string", "null"]}
    }
}


# ============================================================================
# context.json Schema
# ============================================================================

class ContextStepInfo(TypedDict, total=False):
    """Summary info for a step in context.json."""

    step_type: str
    status: str
    started_at: Optional[str]
    completed_at: Optional[str]
    outputs_summary: Dict[str, Any]  # Key outputs only, not full details


class ContextSchema(TypedDict, total=False):
    """Root schema for context.json.

    AI-optimized context export for handoff and resumption.
    This is a distilled version of engine.json optimized for
    LLM consumption, not full state reconstruction.

    Example:
        {
            "type": "se3_context",
            "version": "3.0",
            "flow_id": "abc123def456",
            "status": "running",
            "task": {
                "description": "Implement feature X",
                "type": "feature"
            },
            "progress": {
                "completed": 3,
                "total": 8,
                "current_step": "implement"
            },
            "steps": [
                {"step_type": "analyze", "status": "completed", ...},
                {"step_type": "read_spec", "status": "completed", ...},
                ...
            ],
            "key_outputs": {
                "analyze": {"task_type": "feature", "scope": "backend"},
                "read_spec": {"relevant_specs": ["api-design"]}
            },
            "project_context": {
                "root": "/home/user/project",
                "change_path": "specs/_changelog/feature-x"
            },
            "timestamp": "2026-02-24T10:30:00"
        }
    """

    type: str  # "se3_context"
    version: str  # "3.0"
    flow_id: str
    status: str
    task: Dict[str, Any]
    progress: Dict[str, Any]
    steps: List[ContextStepInfo]
    key_outputs: Dict[str, Any]
    project_context: Dict[str, Any]
    timestamp: str


CONTEXT_JSON_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SE3 AI Context Export",
    "description": "AI-optimized context for handoff and resumption",
    "type": "object",
    "required": ["type", "version", "flow_id", "status", "task", "timestamp"],
    "properties": {
        "type": {"type": "string", "const": "se3_context"},
        "version": {"type": "string", "const": "3.0"},
        "flow_id": {"type": "string"},
        "status": {"type": "string"},
        "task": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "type": {"type": "string"}
            }
        },
        "progress": {
            "type": "object",
            "properties": {
                "completed": {"type": "integer"},
                "total": {"type": "integer"},
                "current_step": {"type": "string"}
            }
        },
        "steps": {"type": "array"},
        "key_outputs": {"type": "object"},
        "project_context": {"type": "object"},
        "timestamp": {"type": "string", "format": "date-time"}
    }
}


def build_context_from_flow(flow_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Build context.json content from flow instance dict.

    This transforms the full engine.json state into an AI-optimized
    context format that contains only what's needed for resumption.

    Args:
        flow_dict: FlowInstance as dictionary (from FlowInstance.to_dict())

    Returns:
        Context dictionary matching ContextSchema
    """
    state = flow_dict.get("state", {})
    steps = state.get("steps", {})
    step_history = state.get("step_history", [])

    # Build step summaries
    context_steps = []
    key_outputs = {}

    for step_id in step_history:
        step = steps.get(step_id)
        if not step:
            continue

        step_type = step.get("step_type", "unknown")
        status = step.get("status", "unknown")

        # Add to steps list
        context_steps.append({
            "step_type": step_type,
            "status": status,
            "started_at": step.get("started_at"),
            "completed_at": step.get("completed_at"),
            "outputs_summary": _summarize_outputs(step.get("outputs", {}), step_type)
        })

        # Collect key outputs for quick reference
        if status == "completed" and step.get("outputs"):
            key_outputs[step_type] = _extract_key_outputs(step["outputs"], step_type)

    # Get current step
    current_step_id = state.get("current_step_id")
    current_step = steps.get(current_step_id) if current_step_id else None
    current_step_type = current_step.get("step_type") if current_step else None

    # Calculate progress
    completed = sum(1 for s in context_steps if s["status"] == "completed")
    total = len(state.get("selected_steps", []))

    return {
        "type": "se3_context",
        "version": "3.0",
        "flow_id": flow_dict.get("flow_id", ""),
        "status": flow_dict.get("status", ""),
        "task": {
            "description": flow_dict.get("task_description", ""),
            "type": flow_dict.get("task_type")
        },
        "progress": {
            "completed": completed,
            "total": total,
            "current_step": current_step_type,
            "percent": (completed / total * 100) if total > 0 else 0
        },
        "steps": context_steps,
        "key_outputs": key_outputs,
        "project_context": {
            "root": str(flow_dict.get("change_path", "")).split("/specs")[0] if flow_dict.get("change_path") else "",
            "change_path": flow_dict.get("change_path"),
            "change_name": flow_dict.get("change_name")
        },
        "timestamp": flow_dict.get("updated_at", "")
    }


def _summarize_outputs(outputs: Dict[str, Any], step_type: str) -> Dict[str, Any]:
    """Summarize step outputs for context.

    Only include key fields, truncate long values.
    """
    summary = {}

    # Define key fields per step type
    key_fields = {
        "analyze": ["task_type", "scope", "required_steps"],
        "read_spec": ["relevant_specs"],
        "propose": ["proposal_summary"],
        "design": ["design_summary", "decisions"],
        "plan_tasks": ["task_count", "tasks"],
        "implement": ["files_changed", "changes_summary"],
        "test": ["test_results_summary"],
        "verify_spec": ["verification_passed"],
        "update_spec": ["specs_updated"],
        "commit": ["commit_hash"],
        "summarize": ["summary", "handoff_context"]
    }

    fields = key_fields.get(step_type, list(outputs.keys())[:3])

    for field in fields:
        if field in outputs:
            value = outputs[field]
            # Truncate long values
            if isinstance(value, str) and len(value) > 200:
                summary[field] = value[:200] + "..."
            else:
                summary[field] = value

    return summary


def _extract_key_outputs(outputs: Dict[str, Any], step_type: str) -> Dict[str, Any]:
    """Extract the most important outputs for each step type."""
    key_map = {
        "analyze": ["task_type", "scope", "complexity"],
        "read_spec": ["relevant_specs", "spec_summary"],
        "propose": ["proposal", "acceptance_criteria"],
        "design": ["design_doc", "decisions", "architecture"],
        "plan_tasks": ["task_list"],
        "implement": ["changes_made", "files_modified"],
        "test": ["test_results", "coverage"],
        "verify_spec": ["verification_result", "issues_found"],
        "update_spec": ["updated_specs"],
        "commit": ["commit_hash", "commit_message"],
        "summarize": ["summary", "handoff_context"]
    }

    keys = key_map.get(step_type, [])
    return {k: outputs.get(k) for k in keys if k in outputs}
