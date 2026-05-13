"""Data models for the flow engine state machine.

Defines the core data structures: Step, State, Transition, and FlowInstance.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Sliding-window cap on State.fix_history to keep memory / engine.json size /
# per-transition deepcopy cost bounded under unlimited mode
# (max_fix_iterations=0). Held at the *default* value of
# ``DEFAULT_MAX_FIX_ITERATIONS`` so a flow that runs the default cap never
# silently drops fix_history entries.
#
# DOCUMENTED TRADE-OFF for users who raise ``workflow.max_fix_iterations``
# above this floor (e.g. 200): when iteration count crosses
# ``FIX_HISTORY_MAX_ENTRIES``, the oldest entries are trimmed. The impact is
# bounded:
#   - verify_spec / self_check fix-context renderers tail-truncate to 20
#     entries, so the LLM's prompt context is unaffected once iteration
#     count > 20 regardless of cap.
#   - implement.py's ``_format_fix_history`` iterates the *full* persisted
#     list, so iterations beyond ~100 lose early-iteration entries from
#     the implement-step prompt. In practice this is acceptable: a fix
#     loop running >100 iterations on the same task is already a stuck
#     loop where ancient history adds noise rather than signal.
# If the trade-off ever becomes actually painful, the right fix is to
# plumb the resolved ``WorkflowConfig`` into ``State`` and use
# ``max(default, max_fix_iterations)`` here. For now, the current cap is
# the simpler choice — see the inline note in
# ``State.increment_fix_iteration``.
FIX_HISTORY_MAX_ENTRIES = 100


class StepType(Enum):
    """Types of workflow steps in the step pool."""

    DISCOVERY = "discovery"  # Discovery mode: explore requirements with user
    ANALYZE = "analyze"  # Analyze input, determine task type and scope
    PROJECT_SUMMARY = "project_summary"  # Generate project context summary
    PLAN = "plan"  # Unified planning: proposal + design + task breakdown
    PROPOSE = "propose"  # Generate change proposal (deprecated: use PLAN)
    DESIGN = "design"  # Design solution and architecture decisions (deprecated: use PLAN)
    PLAN_TASKS = "plan_tasks"  # Break down into concrete tasks (deprecated: use PLAN)
    CONFIRM = "confirm"  # Review and confirm previous step output
    IMPLEMENT = "implement"  # Write code (most critical step)
    TEST = "test"  # Run tests (program execution, not LLM)
    SELF_CHECK = "self_check"  # Code self-review: logic completeness and robustness
    VERIFY_SPEC = "verify_spec"  # Check implementation vs spec consistency
    UPDATE_SPEC = "update_spec"  # Update spec to record changes
    VERSION_ANALYZE = "version_analyze"  # Analyze changes to determine version bump type
    COMMIT = "commit"  # Commit changes (program execution)
    SUMMARIZE = "summarize"  # Generate summary and handoff


class StepStatus(Enum):
    """Status of a step execution."""

    PENDING = "pending"  # Not started yet
    RUNNING = "running"  # Currently executing
    COMPLETED = "completed"  # Successfully finished
    PARTIAL = "partial"  # Partial completion due to unrecoverable constraints (e.g., permission restrictions); distinct from FAILED which triggers retries
    FAILED = "failed"  # Failed after retries
    RETRYING = "retrying"  # Currently retrying
    PAUSED = "paused"  # Paused waiting for user input
    REVISION_NEEDED = "revision_needed"  # Changes requested, go back to previous step


@dataclass
class Step:
    """A single workflow step.

    Each step has a type, status, and stores its inputs/outputs.
    Steps LLM calls through subprocess, with retry and fallback logic.
    """

    step_type: StepType
    status: StepStatus = StepStatus.PENDING
    step_id: str = ""  # Auto-generated in State.add_step if empty

    # Execution tracking
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    retry_count: int = 0
    max_retries: int = 3

    # LLM call configuration
    model: Optional[str] = None  # Model to use for this step
    fallback_model: Optional[str] = None  # Fallback if primary fails

    # Inputs and outputs
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[Path] = field(default_factory=list)  # Files produced

    # Error tracking
    error_message: Optional[str] = None
    error_details: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize step to dictionary."""
        return {
            "step_id": self.step_id,
            "step_type": self.step_type.value,
            "status": self.status.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "model": self.model,
            "fallback_model": self.fallback_model,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "artifacts": [str(p) for p in self.artifacts],
            "error_message": self.error_message,
            "error_details": self.error_details,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Step:
        """Deserialize step from dictionary."""
        return cls(
            step_type=StepType(data["step_type"]),
            status=StepStatus(data["status"]),
            step_id=data["step_id"],
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            model=data.get("model"),
            fallback_model=data.get("fallback_model"),
            inputs=data.get("inputs", {}),
            outputs=data.get("outputs", {}),
            artifacts=[Path(p) for p in data.get("artifacts", [])],
            error_message=data.get("error_message"),
            error_details=data.get("error_details"),
        )


@dataclass
class Transition:
    """A state transition rule.

    Defines when and how to move from one step to another.
    """

    from_step: StepType
    to_step: StepType
    condition: Optional[str] = None  # Optional condition name for conditional transitions
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_step": self.from_step.value,
            "to_step": self.to_step.value,
            "condition": self.condition,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Transition:
        return cls(
            from_step=StepType(data["from_step"]),
            to_step=StepType(data["to_step"]),
            condition=data.get("condition"),
            description=data.get("description", ""),
        )


@dataclass
class State:
    """Current state of the flow engine.

    Tracks the active step, completed steps, and execution context.
    """

    current_step_id: Optional[str] = None
    step_history: List[str] = field(default_factory=list)  # Ordered list of step IDs
    steps: Dict[str, Step] = field(default_factory=dict)  # step_id -> Step

    # Global context shared across steps
    context: Dict[str, Any] = field(default_factory=dict)

    # Selected steps for this flow (dynamic selection from step pool)
    selected_steps: List[StepType] = field(default_factory=list)
    current_step_index: int = 0

    # Review cycle tracking: step_id -> review iteration count
    review_iterations: Dict[str, int] = field(default_factory=dict)

    # Fix loop tracking: for test-verify-fix iterations
    fix_iterations: int = 0
    fix_history: List[Dict[str, Any]] = field(default_factory=list)

    def get_current_step(self) -> Optional[Step]:
        """Get the currently active step."""
        if self.current_step_id:
            return self.steps.get(self.current_step_id)
        return None

    def add_step(self, step: Step) -> None:
        """Add a step to the state.

        Auto-generates step_id if not set, using format: NN_steptype_uuid8
        (e.g., 01_analyze_844c2cf8) for human-readable, sortable history files.
        """
        if not step.step_id:
            seq = len(self.step_history) + 1
            uid = str(uuid.uuid4())[:8]
            step.step_id = f"{seq:02d}_{step.step_type.value}_{uid}"
        self.steps[step.step_id] = step
        if step.step_id not in self.step_history:
            self.step_history.append(step.step_id)

    def get_step_to_review(self, confirm_step_id: str) -> Optional[Step]:
        """Get the step that needs review (the one before confirm).

        In a review cycle, the confirm step follows the step being reviewed.
        This finds that preceding step from history.

        Args:
            confirm_step_id: The ID of the confirm step

        Returns:
            The step being reviewed, or None if not found
        """
        try:
            idx = self.step_history.index(confirm_step_id)
            if idx > 0:
                prev_step_id = self.step_history[idx - 1]
                return self.steps.get(prev_step_id)
        except ValueError:
            pass
        return None

    def increment_review_iteration(self, step_id: str) -> int:
        """Increment and return the review iteration count for a step.

        Args:
            step_id: The step being reviewed

        Returns:
            New iteration count (1-based)
        """
        current = self.review_iterations.get(step_id, 0)
        self.review_iterations[step_id] = current + 1
        return current + 1

    def get_review_iteration(self, step_id: str) -> int:
        """Get the current review iteration count for a step.

        Args:
            step_id: The step being reviewed

        Returns:
            Current iteration count (0 if never reviewed)
        """
        return self.review_iterations.get(step_id, 0)

    def increment_fix_iteration(self, fix_context: Optional[Dict[str, Any]] = None) -> int:
        """Increment and return the fix iteration count for the test-verify-fix loop.

        Args:
            fix_context: Optional context about the fix (step_id, reason, etc.)

        Returns:
            New iteration count (1-based)
        """
        self.fix_iterations += 1

        # Track in history for debugging
        history_entry = {
            "iteration": self.fix_iterations,
            "timestamp": datetime.now().isoformat(),
        }
        if fix_context:
            history_entry.update(fix_context)
        self.fix_history.append(history_entry)
        # Sliding-window cap so the list cannot grow unboundedly under unlimited
        # mode (max_fix_iterations=0). The full list is persisted to engine.json
        # on every save and deep-copied into step.inputs each transition; without
        # a cap, a degenerate stuck loop would inflate memory, file size, and
        # deepcopy cost linearly with iteration count. Keep the most recent
        # entries because every consumer (verify_spec/self_check tail-truncate to
        # 20, issue_discovery uses last 5) cares about recency.
        if len(self.fix_history) > FIX_HISTORY_MAX_ENTRIES:
            self.fix_history = self.fix_history[-FIX_HISTORY_MAX_ENTRIES:]

        # Also store in context for easy access by steps
        self.context["fix_iterations"] = self.fix_iterations
        self.context["fix_history"] = self.fix_history

        return self.fix_iterations

    def get_fix_iteration(self) -> int:
        """Get the current fix iteration count.

        Returns:
            Current iteration count (0 if no fix iterations yet)
        """
        return self.fix_iterations

    def update_task_type(self, task_type: str) -> None:
        """Update the resolved task type after analyze step.

        This stores the LLM-determined task type in the context,
        separate from any explicitly provided type.

        Args:
            task_type: The task type determined by analyze step
        """
        self.context["resolved_type"] = task_type

    def is_type_pending(self) -> bool:
        """Check if task type is still pending (not yet resolved).

        Returns:
            True if no resolved_type is set, False otherwise
        """
        return "resolved_type" not in self.context

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state to dictionary."""
        return {
            "current_step_id": self.current_step_id,
            "step_history": self.step_history,
            "steps": {sid: step.to_dict() for sid, step in self.steps.items()},
            "context": self.context,
            "selected_steps": [s.value for s in self.selected_steps],
            "current_step_index": self.current_step_index,
            "review_iterations": self.review_iterations,
            "fix_iterations": self.fix_iterations,
            "fix_history": self.fix_history,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> State:
        """Deserialize state from dictionary."""
        # Retroactively apply the sliding-window cap: an engine.json written
        # by an older build (or a build with a higher cap) may carry more than
        # ``FIX_HISTORY_MAX_ENTRIES`` entries, and without clamping the
        # oversized list would be deepcopied per transition and re-persisted
        # on every save until the next append finally trims it. Mirror the
        # tail-keep policy used in ``increment_fix_iteration``.
        loaded_history = data.get("fix_history", [])
        if len(loaded_history) > FIX_HISTORY_MAX_ENTRIES:
            loaded_history = loaded_history[-FIX_HISTORY_MAX_ENTRIES:]
        state = cls(
            current_step_id=data.get("current_step_id"),
            step_history=data.get("step_history", []),
            context=data.get("context", {}),
            selected_steps=[StepType(s) for s in data.get("selected_steps", [])],
            current_step_index=data.get("current_step_index", 0),
            review_iterations=data.get("review_iterations", {}),
            fix_iterations=data.get("fix_iterations", 0),
            fix_history=loaded_history,
        )
        # Keep ``state.context['fix_history']`` consistent with the clamped
        # list — ``increment_fix_iteration`` mirrors fix_history into context,
        # so a resumed flow whose context still holds the oversized copy
        # would see two diverging sources of truth.
        if "fix_history" in state.context:
            state.context["fix_history"] = loaded_history
        state.steps = {
            sid: Step.from_dict(step_data) for sid, step_data in data.get("steps", {}).items()
        }
        return state


class FlowStatus(Enum):
    """Overall status of a flow instance."""

    INIT = "init"  # Just created
    RUNNING = "running"  # Actively executing
    PAUSED = "paused"  # Paused for user input or decision
    COMPLETED = "completed"  # All steps finished successfully
    FAILED = "failed"  # Failed and cannot continue
    RECOVERING = "recovering"  # Attempting to recover from interruption


@dataclass
class FlowInstance:
    """A complete workflow instance.

    This is the top-level container for a single run of the flow engine.
    """

    flow_id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d-%H%M%S") + "_" + str(uuid.uuid4())[:8])
    status: FlowStatus = FlowStatus.INIT

    # User input
    task_description: str = ""
    task_type: Optional[str] = None  # auto-detected or user-specified

    # State machine state
    state: State = field(default_factory=State)

    # Metadata
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    # Change tracking (for SE3 integration)
    change_name: Optional[str] = None
    change_path: Optional[Path] = None

    # Issue tracking
    source_issue_id: Optional[str] = None

    # Baseline commit for change detection (used in multi-worktree scenarios)
    baseline_commit: Optional[str] = None

    # Loop mode
    is_loop_mode: bool = False
    loop_branch: Optional[str] = None
    loop_worktree_path: Optional[str] = None
    loop_original_branch: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize flow instance to dictionary."""
        return {
            "flow_id": self.flow_id,
            "status": self.status.value,
            "task_description": self.task_description,
            "task_type": self.task_type,
            "state": self.state.to_dict(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "change_name": self.change_name,
            "change_path": str(self.change_path) if self.change_path else None,
            "source_issue_id": self.source_issue_id,
            "baseline_commit": self.baseline_commit,
            "is_loop_mode": self.is_loop_mode,
            "loop_branch": self.loop_branch,
            "loop_worktree_path": self.loop_worktree_path,
            "loop_original_branch": self.loop_original_branch,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FlowInstance:
        """Deserialize flow instance from dictionary."""
        return cls(
            flow_id=data["flow_id"],
            status=FlowStatus(data["status"]),
            task_description=data.get("task_description", ""),
            task_type=data.get("task_type"),
            state=State.from_dict(data.get("state", {})),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
            change_name=data.get("change_name"),
            change_path=Path(data["change_path"]) if data.get("change_path") else None,
            source_issue_id=data.get("source_issue_id"),
            baseline_commit=data.get("baseline_commit"),
            is_loop_mode=data.get("is_loop_mode", False),
            loop_branch=data.get("loop_branch"),
            loop_worktree_path=data.get("loop_worktree_path"),
            loop_original_branch=data.get("loop_original_branch"),
        )

    def get_progress(self) -> tuple[int, int]:
        """Get completion progress as (completed_count, total_count)."""
        if not self.state.selected_steps:
            return (0, 0)
        completed = sum(
            1 for step in self.state.steps.values()
            if step.status == StepStatus.COMPLETED
        )
        return (completed, len(self.state.selected_steps))


# Step pool definition - all available steps
STEP_POOL: Dict[StepType, Dict[str, Any]] = {
    StepType.DISCOVERY: {
        "name": "discovery",
        "description": "Discovery mode: explore requirements with user through multi-turn conversation",
        "uses_llm": True,
        "read_only": True,
        "inputs": ["initial_description", "conversation_history"],
        "outputs": ["refined_description", "discovery_summary", "requirements_clarified"],
    },
    StepType.ANALYZE: {
        "name": "analyze",
        "description": "Analyze input, determine task type and scope; collect project context and select/load specs",
        "uses_llm": True,
        "read_only": True,
        "inputs": ["task_description", "project_context"],
        "outputs": ["task_type", "scope", "complexity", "reasoning", "project_summary", "relevant_specs", "spec_content"],
    },
    # Deprecated: PROJECT_SUMMARY merged into ANALYZE (kept for backward compat with persisted flows)
    StepType.PROJECT_SUMMARY: {
        "name": "project_summary",
        "description": "Generate project context summary (deprecated: merged into analyze)",
        "uses_llm": True,
        "read_only": True,
        "deprecated": True,
        "inputs": ["task_description"],
        "outputs": ["project_summary"],
    },
    StepType.PLAN: {
        "name": "plan",
        "description": "Unified planning: proposal + design + task breakdown in one LLM call",
        "uses_llm": True,
        "read_only": True,
        "inputs": ["task_description", "spec_content", "project_summary", "task_type", "scope"],
        "outputs": ["plan", "task_groups", "spec_changes", "total_complexity", "estimated_effort"],
    },
    # Deprecated step types (kept for backward compatibility with persisted state)
    StepType.PROPOSE: {
        "name": "propose",
        "description": "Generate change proposal (deprecated: use plan)",
        "uses_llm": True,
        "read_only": False,
        "inputs": ["task_description", "spec_content"],
        "outputs": ["proposal"],
    },
    StepType.DESIGN: {
        "name": "design",
        "description": "Design solution and architecture decisions (deprecated: use plan)",
        "uses_llm": True,
        "read_only": False,
        "inputs": ["proposal", "spec_content"],
        "outputs": ["design_doc", "decisions"],
    },
    StepType.PLAN_TASKS: {
        "name": "plan_tasks",
        "description": "Break down into logical task groups (deprecated: use plan)",
        "uses_llm": True,
        "read_only": False,
        "inputs": ["design_doc"],
        "outputs": ["task_groups", "task_list"],
    },
    StepType.CONFIRM: {
        "name": "confirm",
        "description": "Review and confirm previous step output",
        "uses_llm": False,  # Uses LLM only in llm reviewer mode
        "read_only": False,
        "inputs": ["previous_step_output", "previous_step_type"],
        "outputs": ["review_result", "approved"],
    },
    StepType.IMPLEMENT: {
        "name": "implement",
        "description": "Write code implementation (task groups)",
        "uses_llm": True,
        "read_only": False,
        "inputs": ["task_groups", "task_list", "design_doc"],
        "outputs": ["implemented_groups", "files_changed", "total_groups"],
    },
    StepType.TEST: {
        "name": "test",
        "description": "Run tests to verify implementation",
        "uses_llm": False,
        "read_only": False,
        "inputs": ["changes_made"],
        "outputs": ["test_results"],
    },
    StepType.SELF_CHECK: {
        "name": "self_check",
        "description": "Code self-review: check logic completeness, robustness, and test coverage gaps",
        "uses_llm": True,
        "read_only": True,
        "inputs": ["changes_made", "test_results", "spec_content", "task_description"],
        "outputs": ["self_check_result", "issues", "actionable_count"],
    },
    StepType.VERIFY_SPEC: {
        "name": "verify_spec",
        "description": "Check implementation vs spec consistency",
        "uses_llm": True,
        "read_only": True,
        "inputs": ["changes_made", "spec_content", "test_results", "fix_iteration", "spec_changes"],
        "outputs": ["verification_result", "verified", "issues", "fix_needed", "fix_instructions", "fix_context", "in_scope_count", "out_of_scope_count"],
    },
    StepType.UPDATE_SPEC: {
        "name": "update_spec",
        "description": "Update spec to record changes",
        "uses_llm": True,
        "read_only": False,
        "inputs": ["changes_made", "verification_result", "spec_changes", "design_doc"],
        "outputs": ["updated_specs"],
    },
    StepType.VERSION_ANALYZE: {
        "name": "version_analyze",
        "description": "Analyze changes to determine SemVer bump type and generate commit message",
        "uses_llm": True,
        "read_only": True,
        "inputs": ["changes_made", "summary", "verification_result", "task_type"],
        "outputs": ["bump_type", "reasoning", "confidence", "suggested_version", "commit_message"],
    },
    StepType.COMMIT: {
        "name": "commit",
        "description": "Commit changes with version bump",
        "uses_llm": False,
        "read_only": False,
        "inputs": ["changes_made", "updated_specs", "bump_type", "proposal", "commit_message"],
        "outputs": ["commit_hash"],
    },
    StepType.SUMMARIZE: {
        "name": "summarize",
        "description": "Generate summary and handoff",
        "uses_llm": True,
        "read_only": True,
        "inputs": ["changes_made", "commit_hash"],
        "outputs": ["summary"],
    },
}


def get_step_info(step_type: StepType) -> Dict[str, Any]:
    """Get information about a step type from the step pool."""
    return STEP_POOL.get(step_type, {})


def get_default_step_sequence(task_type: str = "feature") -> List[StepType]:
    """Get the default step sequence for a given task type.

    This is the initial selection - the analyze step can modify this.
    """
    sequences: Dict[str, List[StepType]] = {
        "feature": [
            StepType.ANALYZE,
            StepType.PLAN,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
            StepType.VERIFY_SPEC,
            StepType.UPDATE_SPEC,
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
        ],
        "bugfix": [
            StepType.ANALYZE,
            StepType.PLAN,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
            StepType.VERIFY_SPEC,
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
        ],
        "review": [
            StepType.ANALYZE,
            StepType.VERIFY_SPEC,
        ],
        "small": [
            StepType.ANALYZE,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
        ],
        "directive": [
            StepType.ANALYZE,
            StepType.PLAN,
            StepType.IMPLEMENT,
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
        ],
        "discovery": [
            StepType.DISCOVERY,
            StepType.ANALYZE,
            StepType.PLAN,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
            StepType.VERIFY_SPEC,
            StepType.UPDATE_SPEC,
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
        ],
    }
    return sequences.get(task_type, sequences["feature"])
