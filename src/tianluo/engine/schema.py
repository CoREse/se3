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
    """Valid step status values.

    Mirrors :class:`tianluo.engine.models.StepStatus` — every value the model layer
    can emit into (and load from) engine.json must appear here, or a valid
    new-format file would be rejected by this schema's enum.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    RETRYING = "retrying"
    PAUSED = "paused"
    REVISION_NEEDED = "revision_needed"


class StepTypeValue(str, Enum):
    """Valid step type values.

    Mirrors :class:`tianluo.engine.models.StepType` — every step type the model
    layer can serialize must appear here so a valid engine.json containing e.g.
    a ``confirm`` or ``self_check`` step is not rejected by this schema.
    """

    DISCOVERY = "discovery"
    ANALYZE = "analyze"
    PROJECT_SUMMARY = "project_summary"
    PLAN = "plan"
    PROPOSE = "propose"  # deprecated: use PLAN
    DESIGN = "design"  # deprecated: use PLAN
    PLAN_TASKS = "plan_tasks"  # deprecated: use PLAN
    CONFIRM = "confirm"
    INVESTIGATE = "investigate"
    IMPLEMENT = "implement"
    TEST = "test"
    SELF_CHECK = "self_check"
    ADJUDICATE = "adjudicate"
    INVARIANT_CHECK = "invariant_check"
    CHARTER_FRESHNESS = "charter_freshness"
    VERIFY_SPEC = "verify_spec"  # deprecated by the charter refactor
    UPDATE_SPEC = "update_spec"  # deprecated by the charter refactor
    SPEC_GATE = "spec_gate"  # deprecated by the charter refactor
    VERSION_ANALYZE = "version_analyze"
    COMMIT = "commit"
    MERGE_INTEGRATE = "merge_integrate"
    VERSION_RECONCILE = "version_reconcile"
    SUMMARIZE = "summarize"


class FlowStatusValue(str, Enum):
    """Valid flow status values."""

    INIT = "init"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    RECOVERING = "recovering"


class ColdRefSchema(TypedDict, total=False):
    """Reference from a header step entry to its externalized cold file.

    New-format (hot/cold split, issue #244 一期) engine.json keeps only the
    per-step *status table* inline; each step's heavy inputs/outputs/artifacts
    live in ``tianluo/state/steps/<flow_id>/<step_id>.json`` and are referenced here.
    ``hash`` is the cold payload's content hash, letting the incremental write
    path rewrite only the cold files that actually changed.

    Example:
        {"file": "01_analyze_844c2cf8.json", "hash": "9f2b...c1"}
    """

    file: str
    hash: str


class StepSchema(TypedDict, total=False):
    """Schema for a single step in engine.json.

    Two on-disk shapes are accepted (loaders read both — no in-place migration):

    * New format (hot/cold split): inputs/outputs/artifacts are absent inline
      and a ``cold_ref`` points at the externalized cold file.
    * Legacy format: inputs/outputs/artifacts are inlined (``cold_ref`` absent).

    Example (new format):
        {
            "step_id": "01_analyze_844c2cf8",
            "step_type": "analyze",
            "status": "completed",
            "started_at": "2026-02-24T10:30:00",
            "completed_at": "2026-02-24T10:31:15",
            "retry_count": 0,
            "max_retries": 3,
            "model": "claude-opus-4-6",
            "fallback_model": "claude-haiku-3-5",
            "cold_ref": {"file": "01_analyze_844c2cf8.json", "hash": "9f2b...c1"},
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
    # Step-level working-directory override (absolute path). Present on the
    # merge-side steps (merge_integrate / version_reconcile) of a worktree flow,
    # which must run in the MAIN checkout rather than the isolated worktree; None
    # / absent for every ordinary step. Mirrors models.Step.cwd.
    cwd: Optional[str]
    cold_ref: ColdRefSchema  # New format: externalized inputs/outputs/artifacts
    inputs: Dict[str, Any]  # Legacy format only (inline)
    outputs: Dict[str, Any]  # Legacy format only (inline)
    artifacts: List[str]  # Legacy format only (inline); file paths as strings
    error_message: Optional[str]
    error_details: Optional[str]


class ContextRefSchema(TypedDict, total=False):
    """Reference from the header to the externalized shared-context cold file.

    New-format state keeps ``context`` (and ``fix_history``) out of the header,
    in ``tianluo/state/steps/<flow_id>/_context.json``, referenced here by hash.
    """

    file: str
    hash: str


class StateSchema(TypedDict, total=False):
    """Schema for the state object in engine.json.

    Tracks current step, step history, and execution context. In the new hot/cold
    format the shared ``context`` and ``fix_history`` are externalized and
    referenced via ``context_ref``; legacy state carries them inline.

    Example (new format):
        {
            "current_step_id": "01_analyze_844c2cf8",
            "step_history": ["01_analyze_844c2cf8", "02_plan_e5f6g7h8"],
            "steps": {"01_analyze_844c2cf8": {...}, "02_plan_e5f6g7h8": {...}},
            "context_ref": {"file": "_context.json", "hash": "3ac1...9f"},
            "selected_steps": ["analyze", "plan", "implement", ...],
            "current_step_index": 2,
            "session_token_usage": {...}
        }
    """

    current_step_id: Optional[str]
    step_history: List[str]
    steps: Dict[str, StepSchema]
    context_ref: ContextRefSchema  # New format: externalized shared context
    context: Dict[str, Any]  # Legacy format only (inline)
    selected_steps: List[str]  # StepTypeValue values
    current_step_index: int
    # Small scalar counters/usage the header keeps inline in BOTH formats — the
    # hot/cold split only externalizes the two unbounded growers (context and
    # fix_history), never these KB-scale fields (see State.to_header_dict).
    review_iterations: Dict[str, int]  # step_id -> review pass count
    fix_iterations: int  # test-verify-fix loop counter
    baseline_failures: Optional[Any]  # pre-implementation failing-test baseline
    session_token_usage: Dict[str, Any]  # UsageTotals.to_dict()
    fix_history: List[Dict[str, Any]]  # Legacy inline only; new format externalizes with context


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
            "change_path": "tianluo/specs/_changelog/feature-x",
            "is_worktree_mode": false,
            "worktree_branch": null
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
    source_issue_id: Optional[str]
    baseline_commit: Optional[str]
    is_worktree_mode: bool
    worktree_branch: Optional[str]
    worktree_path: Optional[str]
    worktree_original_branch: Optional[str]
    # Hot/cold split marker (issue #244 一期): "hotcold/1" when the header
    # externalizes step payloads + context to steps/<flow_id>/; absent on a
    # legacy fully-inline engine.json.
    engine_format: str
    # Present (and True) only while a synchronous run is queued acquiring the
    # main-worktree mutex before its first non-discovery step; absent otherwise.
    waiting_for_lock: bool


ENGINE_JSON_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "SE3 Flow Engine State",
    "description": (
        "State persistence for SE3 flow engine. Two on-disk layouts are valid "
        "and both load: the new hot/cold header (engine_format='hotcold/1', where "
        "step payloads and the shared context are externalized to "
        "steps/<flow_id>/ and referenced by cold_ref/context_ref) and the legacy "
        "fully-inline layout (no engine_format; inputs/outputs/context inline). "
        "New writes always use the hot/cold header; legacy files are read as-is "
        "with no in-place migration."
    ),
    "type": "object",
    "required": ["flow_id", "status", "task_description", "state", "created_at", "updated_at"],
    "properties": {
        "flow_id": {"type": "string", "description": "Unique flow identifier"},
        "engine_format": {
            "type": "string",
            "description": (
                "Hot/cold split marker. 'hotcold/1' => header + externalized cold "
                "files; absent => legacy fully-inline layout."
            ),
        },
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
                # Step entries hold the KB-scale status table; each carries either
                # an inline inputs/outputs body (legacy) or a cold_ref
                # {file, hash} pointing at steps/<flow_id>/<step_id>.json (new).
                "steps": {"type": "object"},
                # New format: shared context externalized to
                # steps/<flow_id>/_context.json, referenced by hash.
                "context_ref": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "hash": {"type": "string"},
                    },
                },
                # Legacy format: shared context inlined.
                "context": {"type": "object"},
                "selected_steps": {"type": "array", "items": {"type": "string"}},
                "current_step_index": {"type": "integer"},
                # Small scalar counters/usage kept inline in the header for BOTH
                # formats (the hot/cold split externalizes only context +
                # fix_history, not these); see State.to_header_dict.
                "review_iterations": {
                    "type": "object",
                    "additionalProperties": {"type": "integer"},
                },
                "fix_iterations": {"type": "integer"},
                "baseline_failures": {"type": ["object", "array", "null"]},
                "session_token_usage": {"type": "object"},
                # Legacy fully-inline layout only; the new format externalizes
                # fix_history alongside context into steps/<flow_id>/_context.json.
                "fix_history": {"type": "array", "items": {"type": "object"}}
            }
        },
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
        "completed_at": {"type": ["string", "null"], "format": "date-time"},
        "change_name": {"type": ["string", "null"]},
        "change_path": {"type": ["string", "null"]},
        "source_issue_id": {"type": ["string", "null"]},
        "baseline_commit": {"type": ["string", "null"]},
        "is_worktree_mode": {"type": "boolean"},
        "worktree_branch": {"type": ["string", "null"]},
        "worktree_path": {"type": ["string", "null"]},
        "worktree_original_branch": {"type": ["string", "null"]},
        # Present (and True) only while a synchronous run is queued acquiring the
        # main-worktree mutex before its first non-discovery step; absent otherwise
        # (FlowInstance.to_dict only emits it when True).
        "waiting_for_lock": {"type": "boolean"}
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
                {"step_type": "plan", "status": "completed", ...},
                ...
            ],
            "key_outputs": {
                "analyze": {"task_type": "feature", "scope": "backend"},
                "plan": {"task_groups": [...]}
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
        "plan": ["plan", "task_groups", "total_complexity"],
        "propose": ["proposal_summary"],
        "design": ["design_summary", "decisions"],
        "plan_tasks": ["task_count", "tasks"],
        "implement": ["files_changed", "changes_summary"],
        "test": ["test_results_summary"],
        "verify_spec": ["verification_passed"],
        "update_spec": ["specs_updated"],
        "commit": ["commit_hash"],
        "summarize": ["summary"]
    }

    fields = key_fields.get(step_type, list(outputs.keys())[:3])

    for field in fields:
        if field in outputs:
            value = outputs[field]
            # Truncate long values
            if isinstance(value, str) and len(value) > 1000:
                summary[field] = value[:1000] + "..."
            else:
                summary[field] = value

    return summary


def _extract_key_outputs(outputs: Dict[str, Any], step_type: str) -> Dict[str, Any]:
    """Extract the most important outputs for each step type."""
    key_map = {
        "analyze": ["task_type", "scope", "complexity"],
        "plan": ["plan", "task_groups", "total_complexity", "estimated_effort"],
        "propose": ["proposal", "acceptance_criteria"],
        "design": ["design_doc", "decisions", "architecture"],
        "plan_tasks": ["task_list"],
        "implement": ["changes_made", "files_modified"],
        "test": ["test_results", "coverage"],
        "verify_spec": ["verification_result", "issues_found"],
        "update_spec": ["updated_specs"],
        "commit": ["commit_hash", "commit_message"],
        "summarize": ["summary"]
    }

    keys = key_map.get(step_type, [])
    return {k: outputs.get(k) for k in keys if k in outputs}
