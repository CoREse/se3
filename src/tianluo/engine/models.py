"""Data models for the flow engine state machine.

Defines the core data structures: Step, State, Transition, and FlowInstance.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .token_usage import UsageTotals
from ..usage import UsageRecord, legacy_session_tally_is_authoritative

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
    INVESTIGATE = "investigate"  # Net-zero-diff root-cause investigation (no fix, no commit)
    PROJECT_SUMMARY = "project_summary"  # Generate project context summary
    PLAN = "plan"  # Unified planning: proposal + design + task breakdown
    PROPOSE = "propose"  # Generate change proposal (deprecated: use PLAN)
    DESIGN = "design"  # Design solution and architecture decisions (deprecated: use PLAN)
    PLAN_TASKS = "plan_tasks"  # Break down into concrete tasks (deprecated: use PLAN)
    CONFIRM = "confirm"  # Review and confirm previous step output
    IMPLEMENT = "implement"  # Write code (most critical step)
    TEST = "test"  # Run tests (program execution, not LLM)
    E2E = "e2e"  # Run declarative e2e scenarios in a real isolated environment (opt-in via e2e.enabled)
    SELF_CHECK = "self_check"  # Code self-review: logic completeness and robustness
    ADJUDICATE = "adjudicate"  # Spec-contradiction adjudication: rule on task/plan contradictions that oscillate the fix loop
    INVARIANT_CHECK = "invariant_check"  # Anchored check: diff vs recorded binding invariants (charter + why-comments + task)
    CHARTER_FRESHNESS = "charter_freshness"  # Advisory: does this diff touch charter's three content classes?
    VERIFY_SPEC = "verify_spec"  # Check implementation vs spec consistency (deprecated by the charter refactor)
    UPDATE_SPEC = "update_spec"  # Update spec to record changes (deprecated by the charter refactor)
    SPEC_GATE = "spec_gate"  # Mechanism A: post-update_spec artifact gate + full re-test (deprecated by the charter refactor)
    VERSION_ANALYZE = "version_analyze"  # Analyze changes to determine version bump type
    COMMIT = "commit"  # Commit changes (program execution)
    SUMMARIZE = "summarize"  # Generate summary and handoff
    # Merge-side steps, appended ONLY to a worktree flow's sequence (never in the
    # default pool): the release point for an isolated --worktree run is the
    # merge, so the flow's "done" means "actually landed on master". Both execute
    # in the MAIN checkout under the merge lock (see Step.cwd + state_machine's
    # step-level cwd override), not in the worktree the flow body ran in.
    MERGE_INTEGRATE = "merge_integrate"  # Merge the flow branch into master (integrate() lib)
    VERSION_RECONCILE = "version_reconcile"  # Derive+apply the final version at merge (reconcile() lib)


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

    # Step-level working-directory override (absolute path). When set, the state
    # machine executes this step against ``cwd`` instead of the flow's
    # ``project_root`` — the merge-side steps (MERGE_INTEGRATE / VERSION_RECONCILE)
    # of a worktree flow must run in the MAIN checkout, inside the merge lock, even
    # though the flow body ran in the isolated worktree. ``None`` means "use the
    # flow project_root" (every ordinary step). A small scalar, so it rides in the
    # engine.json header (to_header_dict) and round-trips through to_dict/from_dict.
    cwd: Optional[str] = None

    # Inputs and outputs
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[Path] = field(default_factory=list)  # Files produced

    # Error tracking
    error_message: Optional[str] = None
    error_details: Optional[str] = None

    # -- Hot/cold split runtime bookkeeping (issue #244 一期; NOT serialized) --
    # A flow can be loaded header-only for resume, leaving each step's heavy
    # inputs/outputs/artifacts on disk until first access. ``cold_ref`` remembers
    # the header's recorded ``{"file", "hash"}`` so an untouched step can be
    # re-persisted (its recorded hash re-emitted, its body never read) without
    # data loss, and ``cold_loaded`` tracks whether the body has been
    # materialized (True for any normally-constructed / fully-inlined step).
    # Excluded from equality/repr: they are load provenance, not part of the
    # step's logical value, so a header-loaded-then-hydrated step still compares
    # equal to the same step built in memory.
    cold_ref: Optional[Dict[str, Any]] = field(default=None, compare=False, repr=False)
    cold_loaded: bool = field(default=True, compare=False, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize step to dictionary (full, inline inputs/outputs)."""
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
            "cwd": self.cwd,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "artifacts": [str(p) for p in self.artifacts],
            "error_message": self.error_message,
            "error_details": self.error_details,
        }

    def to_header_dict(self) -> Dict[str, Any]:
        """Serialize just the KB-scale status-table entry (issue #244 一期 B1).

        The hot/cold split keeps every small per-step field (id / type / status /
        timestamps / retry counts / model) in the engine.json header and
        externalizes only the heavy body. This is :meth:`to_dict` minus the
        ``inputs`` / ``outputs`` / ``artifacts`` bodies; the persistence layer
        adds the ``cold_ref`` pointing at the externalized :meth:`cold_payload`.
        """
        entry = self.to_dict()
        for heavy in ("inputs", "outputs", "artifacts"):
            entry.pop(heavy, None)
        return entry

    def cold_payload(self) -> Dict[str, Any]:
        """The heavy per-step body externalized to a cold file (issue #244 一期)."""
        return {
            "inputs": self.inputs,
            "outputs": self.outputs,
            "artifacts": [str(p) for p in self.artifacts],
        }

    def apply_cold(self, cold: Optional[Dict[str, Any]]) -> None:
        """Materialize this step's externalized body from its cold payload.

        The lazy counterpart to a header-only load: fills inputs/outputs/artifacts
        and flips ``cold_loaded`` so a subsequent persist writes real data rather
        than re-emitting the stale ``cold_ref``.

        A failed read (``None`` — the cold file was missing, unreadable, or the
        parse blew up on a transient EACCES/EIO/NFS blip) degrades the in-memory
        body to empty IO for this access (issue #244 B3) but deliberately leaves
        ``cold_loaded`` False. Marking it loaded would let the very next
        ``_split_flow`` hash the now-empty payload, see a "change", and atomically
        overwrite an *intact* on-disk cold file with ``{}`` — permanently
        destroying real inputs/outputs because of a momentary read glitch. Leaving
        it False re-emits the recorded ``cold_ref`` verbatim (the file is never
        rewritten) and re-reads on the next access, so a recovered file rehydrates
        correctly. Only a genuinely-parsed payload (a dict) marks the step loaded.
        """
        if not isinstance(cold, dict):
            self.inputs = {}
            self.outputs = {}
            self.artifacts = []
            return
        self.inputs = cold.get("inputs", {}) or {}
        self.outputs = cold.get("outputs", {}) or {}
        self.artifacts = [Path(p) for p in (cold.get("artifacts") or [])]
        self.cold_loaded = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Step:
        """Deserialize step from dictionary (full inline form)."""
        step = cls(
            step_type=StepType(data["step_type"]),
            status=StepStatus(data["status"]),
            step_id=data["step_id"],
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            model=data.get("model"),
            fallback_model=data.get("fallback_model"),
            cwd=data.get("cwd"),
            inputs=data.get("inputs", {}),
            outputs=data.get("outputs", {}),
            artifacts=[Path(p) for p in data.get("artifacts", [])],
            error_message=data.get("error_message"),
            error_details=data.get("error_details"),
        )
        # Hot/cold provenance markers, present only when a new-format header was
        # inlined by PersistenceManager._reconstruct_full_dict with a FAILED cold
        # read: mark the step not-loaded and keep its cold_ref so a resume-then-
        # save through the eager path re-emits the reference rather than
        # overwriting the intact-but-transiently-unreadable cold file with {}
        # (issue #244 B3-i). The per-step analogue of State.from_dict's
        # ``_cold_context_loaded`` guard. A legacy fully-inline dict lacks these
        # keys and defaults to loaded=True (its inlined body is the real value).
        step.cold_loaded = bool(data.get("_cold_loaded", True))
        ref = data.get("_cold_ref")
        step.cold_ref = ref if isinstance(ref, dict) else None
        return step

    @classmethod
    def from_header_dict(cls, data: Dict[str, Any]) -> Step:
        """Deserialize a header-only step: status table now, body deferred.

        The step's ``inputs`` / ``outputs`` / ``artifacts`` are left empty and
        ``cold_loaded`` is set False; the recorded ``cold_ref`` is retained so the
        persistence layer can hydrate the body on demand (:meth:`apply_cold`) or
        re-emit the reference unchanged on save.
        """
        step = cls.from_dict(data)
        cold_ref = data.get("cold_ref")
        step.cold_ref = cold_ref if isinstance(cold_ref, dict) else None
        step.cold_loaded = False
        return step


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

    # Pre-implement test baseline: the set of test IDs that were already
    # failing *before* this flow's implement step modified anything. Used by
    # the test / verify_spec steps to distinguish inherited (pre-existing)
    # failures from failures this session introduced. Tri-state semantics that
    # MUST round-trip through persistence so ``--resume`` does not re-measure
    # against a different snapshot:
    #   None  -> baseline not yet captured (do not treat any failure as inherited)
    #   []    -> baseline captured, zero failures at flow start
    #   [...] -> baseline captured, these specific test IDs were already failing
    baseline_failures: Optional[List[str]] = None

    # Session-level cumulative token / cost usage across every step of this
    # flow. ``session_usage_records`` holds the authoritative per-call
    # UsageRecords folded in by ``state_machine.run_step`` after each step's
    # handler returns; ``session_token_usage`` is the legacy five-field
    # projection derived from those records (kept in memory for display and
    # backward compatibility, serialized without its embedded record list —
    # the records live in ``session_usage_records`` so they are never stored
    # twice). An older engine.json lacking the records key imports whatever
    # records the old projection embedded.
    session_usage_records: List[UsageRecord] = field(
        default_factory=list, repr=False
    )
    session_token_usage: UsageTotals = field(default_factory=UsageTotals)

    # WHY: an EMPTY record ledger is ambiguous on its own — a modern flow that
    # has not yet completed its first LLM call looks exactly like a pre-ledger
    # flow whose only fact is the legacy five-field tally. Usage surfaces may
    # adapt that tally into a legacy record ONLY in the latter case, so
    # deserialization records whether the loaded file's tally is its only usage
    # fact (see usage.legacy_session_tally_is_authoritative: a non-zero tally
    # beside an empty ledger stays authoritative even after the modern
    # serializer re-saves the flow and adds ``session_usage_records: []``).
    # Runtime only (never serialized); a freshly constructed State is modern.
    legacy_usage_ledger: bool = field(default=False, repr=False, compare=False)

    def add_session_usage_record(self, record: UsageRecord) -> None:
        """Append one call/attempt record, dropping exact duplicates.

        A step re-run can fold an unchanged accumulator into the session
        again; an exact (call_id, attempt) duplicate must not double-count.
        Maintains the invariant that ``session_token_usage`` is a projection
        of ``session_usage_records`` (never a second, diverging tally).
        """
        identity = (record.call_id, record.attempt)
        if any(
            (existing.call_id, existing.attempt) == identity
            for existing in self.session_usage_records
        ):
            return
        self.session_usage_records.append(record)
        self.session_token_usage = UsageTotals.from_usage_records(
            self.session_usage_records
        )

    # -- Hot/cold split runtime bookkeeping (issue #244 一期; NOT serialized) --
    # The shared context / fix_history are externalized to a per-flow cold file
    # (_context.json). These mirror Step.cold_ref/cold_loaded for that single
    # shared payload: ``cold_context_ref`` remembers the header's recorded
    # {"file","hash"} so a context that was never materialized can be re-emitted
    # without rewriting the intact file, and ``cold_context_loaded`` tracks
    # whether the body was actually loaded (True for any normally-constructed or
    # fully-inlined state). A failed cold read (transient EACCES/EIO/NFS blip)
    # must leave it False so the very next persist re-emits the reference instead
    # of clobbering the real on-disk context with {} — the context analogue of
    # Step.apply_cold's data-loss guard (issue #244 B3-i).
    cold_context_ref: Optional[Dict[str, Any]] = field(
        default=None, compare=False, repr=False
    )
    cold_context_loaded: bool = field(default=True, compare=False, repr=False)

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
        """Serialize state to dictionary (full, inline steps + context)."""
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
            "baseline_failures": self.baseline_failures,
            # Records may live on either holder (old code folds into
            # session_token_usage directly); serialize whichever has them.
            "session_usage_records": [
                record.to_dict()
                for record in (
                    self.session_usage_records
                    or list(self.session_token_usage.usage_records)
                )
            ],
            "session_token_usage": {
                "input_tokens": self.session_token_usage.input_tokens,
                "output_tokens": self.session_token_usage.output_tokens,
                "cache_creation_input_tokens": (
                    self.session_token_usage.cache_creation_input_tokens
                ),
                "cache_read_input_tokens": (
                    self.session_token_usage.cache_read_input_tokens
                ),
                "total_cost_usd": self.session_token_usage.total_cost_usd,
            },
        }

    def to_header_dict(self) -> Dict[str, Any]:
        """Serialize the KB-scale state header (issue #244 一期 B1).

        Keeps the per-step *status table* and every small scalar, but replaces
        each step's heavy body with its :meth:`Step.to_header_dict` entry and
        drops the two unbounded growers — the shared ``context`` and
        ``fix_history`` (the latter can embed a copy of a large fix_context).
        Both are externalized together into the per-flow context cold payload
        (:meth:`cold_context`); the persistence layer wires up the ``cold_ref`` /
        ``context_ref`` pointers. ``fix_iterations`` (the small counter) stays in
        the header.
        """
        header = self.to_dict()
        header["steps"] = {
            sid: step.to_header_dict() for sid, step in self.steps.items()
        }
        header.pop("context", None)
        header.pop("fix_history", None)
        return header

    def cold_context(self) -> Dict[str, Any]:
        """The shared cold payload externalized out of the header (issue #244)."""
        return {"context": self.context, "fix_history": self.fix_history}

    @staticmethod
    def _restore_session_usage(
        data: Dict[str, Any],
    ) -> Tuple[List[UsageRecord], UsageTotals]:
        """Restore (records, projection) from a serialized state.

        Newer files carry the authoritative ``session_usage_records`` list;
        older ones only have the legacy five-field projection, which may embed
        a ``usage_records`` list of its own — those are imported so an
        upgraded engine.json never loses its per-call accounting.
        """
        raw_records = data.get("session_usage_records")
        records = (
            [UsageRecord.from_dict(item) for item in raw_records if isinstance(item, dict)]
            if isinstance(raw_records, list)
            else []
        )
        legacy = UsageTotals.from_dict(data.get("session_token_usage"))
        if records:
            return records, UsageTotals.from_usage_records(records)
        return list(legacy.usage_records), legacy

    @classmethod
    def from_header_dict(cls, data: Dict[str, Any]) -> State:
        """Deserialize a header-only state: steps carry deferred bodies.

        ``context`` / ``fix_history`` are left empty here — they live in the
        external context cold payload that the persistence layer resolves after
        this returns — and each step is built header-only via
        :meth:`Step.from_header_dict`, so no per-step cold file is read.
        """
        loaded_history = data.get("fix_history", [])
        session_records, session_usage = cls._restore_session_usage(data)
        state = cls(
            current_step_id=data.get("current_step_id"),
            step_history=data.get("step_history", []),
            context={},
            selected_steps=[StepType(s) for s in data.get("selected_steps", [])],
            current_step_index=data.get("current_step_index", 0),
            review_iterations=data.get("review_iterations", {}),
            fix_iterations=data.get("fix_iterations", 0),
            fix_history=[],
            baseline_failures=data.get("baseline_failures"),
            session_usage_records=session_records,
            session_token_usage=session_usage,
        )
        state.steps = {
            sid: Step.from_header_dict(step_data)
            for sid, step_data in data.get("steps", {}).items()
        }
        state.legacy_usage_ledger = legacy_session_tally_is_authoritative(data)
        # Context body lives in the external cold payload resolved by the
        # persistence layer after this returns; until then it is NOT loaded, so a
        # failed cold read never masquerades as a genuine empty context on save.
        state.cold_context_loaded = False
        return state

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
        session_records, session_usage = cls._restore_session_usage(data)
        state = cls(
            current_step_id=data.get("current_step_id"),
            step_history=data.get("step_history", []),
            context=data.get("context", {}),
            selected_steps=[StepType(s) for s in data.get("selected_steps", [])],
            current_step_index=data.get("current_step_index", 0),
            review_iterations=data.get("review_iterations", {}),
            fix_iterations=data.get("fix_iterations", 0),
            fix_history=loaded_history,
            # Tri-state: a missing key (older engine.json) deserializes to None
            # ("not yet captured"), while an explicit [] round-trips as "captured,
            # no failures". data.get() naturally preserves both — [] is falsy but
            # is NOT substituted with the default, so the two states stay distinct.
            baseline_failures=data.get("baseline_failures"),
            # A missing key (older engine.json) yields an empty tally via
            # UsageTotals.from_dict(None); a stored dict round-trips exactly.
            session_usage_records=session_records,
            session_token_usage=session_usage,
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
        # Hot/cold provenance markers, present only when a new-format header was
        # inlined by PersistenceManager._reconstruct_full_dict. A legacy fully
        # inline dict lacks them and defaults to "loaded" (its context is real),
        # while a header whose external context read FAILED arrives with
        # ``_cold_context_loaded`` False so the next save re-emits the reference
        # rather than overwriting the intact cold file with {} (issue #244 B3-i).
        state.cold_context_loaded = bool(data.get("_cold_context_loaded", True))
        ref = data.get("_cold_context_ref")
        state.cold_context_ref = ref if isinstance(ref, dict) else None
        state.legacy_usage_ledger = legacy_session_tally_is_authoritative(data)
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

    # Worktree isolation mode (luo run --worktree)
    is_worktree_mode: bool = False
    worktree_branch: Optional[str] = None
    worktree_path: Optional[str] = None
    worktree_original_branch: Optional[str] = None

    # Lock-acquisition wait state: True while this synchronous run is blocked
    # acquiring the project's main-worktree mutex before its first
    # code-touching (non-discovery) step. Surfaced to the daemon / web console
    # as a running "waiting for lock" sub-state so a flow that has started but
    # is queued behind another lock holder shows as RUNNING·waiting-for-lock
    # rather than silently stalling on the "已发布" pseudo-success state. Never
    # set on a --worktree flow body (which runs lock-free), so it is only ever
    # written to engine.json when actually True.
    waiting_for_lock: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Serialize flow instance to dictionary."""
        data: Dict[str, Any] = {
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
            "is_worktree_mode": self.is_worktree_mode,
            "worktree_branch": self.worktree_branch,
            "worktree_path": self.worktree_path,
            "worktree_original_branch": self.worktree_original_branch,
        }
        # Only emit waiting_for_lock when actually waiting: keeps engine.json
        # backward-compatible (old readers ignore the absent key) and keeps a
        # --worktree body's engine.json free of the field, while still
        # round-tripping a True value for a synchronous run that is queued.
        if self.waiting_for_lock:
            data["waiting_for_lock"] = True
        return data

    def to_header_dict(self) -> Dict[str, Any]:
        """Serialize the KB-scale flow header (issue #244 一期 B1).

        Identical to :meth:`to_dict` except the heavy ``state`` block is replaced
        by :meth:`State.to_header_dict` (per-step status table, no bodies; no
        inline context/fix_history). The persistence layer stamps the
        ``engine_format`` marker and the cold-file pointers onto the result.
        """
        data = self.to_dict()
        data["state"] = self.state.to_header_dict()
        return data

    @classmethod
    def from_header_dict(cls, data: Dict[str, Any]) -> FlowInstance:
        """Deserialize a header-only flow: state steps carry deferred bodies.

        Reuses :meth:`from_dict` for the flow-level identity/metadata fields (an
        empty ``state`` is substituted so from_dict does no per-step work), then
        installs the header-only :class:`State` built by
        :meth:`State.from_header_dict`.
        """
        flow = cls.from_dict({**data, "state": {}})
        flow.state = State.from_header_dict(data.get("state") or {})
        return flow

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
            is_worktree_mode=data.get("is_worktree_mode", False),
            worktree_branch=data.get("worktree_branch"),
            worktree_path=data.get("worktree_path"),
            worktree_original_branch=data.get("worktree_original_branch"),
            # Backward compatible: old engine.json files predate this field, so
            # a missing key reads as False (not waiting).
            waiting_for_lock=data.get("waiting_for_lock", False),
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
        "outputs": [
            "task_type",
            "scope",
            "complexity",
            "reasoning",
            "root_cause_clear",
            "project_summary",
            "relevant_specs",
            "spec_content",
        ],
    },
    StepType.INVESTIGATE: {
        "name": "investigate",
        # WHY: read_only is False *deliberately*. Investigation means
        # hypothesis-and-verify on a problem whose cause is unknown — temporary
        # logging, a throwaway probe patch, a scratch script are legitimate
        # instruments, and a tool-level write ban (read_only=True feeds
        # llm_caller's --disallowedTools) would degrade the step to plain
        # reading. The "changes nothing" contract is therefore enforced
        # SEMANTICALLY, not by the tool layer: the handler snapshots the
        # workspace before and after the step and fails it when the two differ
        # (see steps/investigate.py + workspace_snapshot.py). Net-zero diff, not
        # strictly read-only.
        "description": (
            "Root-cause investigation for a problem whose cause is unknown: "
            "read code, run commands, form and test hypotheses. Experimental "
            "edits (temporary logging, probe patches, scratch scripts) are "
            "allowed DURING the step but must all be reverted before it ends — "
            "the engine verifies net-zero diff via before/after workspace "
            "snapshots and never reverts anything itself. No git commit. The "
            "actual fix is always carried out by a later PLAN -> IMPLEMENT; the "
            "report lives in step outputs, never in project files."
        ),
        "uses_llm": True,
        "read_only": False,
        "inputs": [
            "task_description",
            "scope",
            "investigation_iteration",
            "investigation_max_iterations",
            "previous_investigation_reports",
        ],
        "outputs": [
            "root_cause",
            "evidence",
            "files_involved",
            "suggested_fix_direction",
            "confidence",
            "conclusive",
            "root_cause_report",
        ],
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
        "inputs": [
            "task_description",
            "spec_content",
            "project_summary",
            "task_type",
            "scope",
            # The doctrine/granularity a flow entered, read back from context so
            # a resumed flow re-plans under the same model it started with.
            "plan_decomposition",
            "plan_granularity",
        ],
        "outputs": [
            "plan",
            "task_groups",
            "spec_changes",
            "total_complexity",
            "estimated_effort",
            "plan_decomposition",
            "plan_granularity",
            # Group count is what IMPLEMENT reads to pick holistic vs. grouped
            # execution; there is no separate routing flag to keep in sync.
            "plan_group_count",
        ],
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
        "inputs": [
            "task_groups",
            "task_list",
            "design_doc",
            "plan_decomposition",
            "plan_granularity",
        ],
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
    StepType.E2E: {
        "name": "e2e",
        # WHY uses_llm is False: the scenario executor is *program*-driven end to
        # end — it builds the environment, runs the declared action sequence and
        # evaluates deterministic assertions in Python. The two LLM call sites it
        # does have are internal and narrowly scoped: maintaining the declarative
        # content under tianluo/e2e/ (authoring it on first use, evolving it
        # incrementally afterwards) and the third assertion tier (an LLM looking
        # at a screenshot) for scenarios that explicitly declare a semantic visual
        # assertion. Neither is the step's mode of operation, so declaring
        # uses_llm=True here would mislabel every ordinary e2e run (and mis-drive
        # the agent-dispatch / read-only tooling that keys off this flag).
        # read_only is False because the environment binds the source tree in and
        # scenarios legitimately produce artefacts (screenshots, captured files).
        "description": (
            "Run the project's declarative e2e scenarios in a real isolated "
            "environment (a container topology built by the configured runtime) "
            "and evaluate them along the assertion ladder: deterministic first, "
            "baseline screenshot diff only for visual regression, LLM vision "
            "only where the scenario declares a semantic visual assertion. A "
            "failing scenario is a code defect and enters the ordinary fix loop; "
            "an unusable container runtime is an environment problem reported "
            "with remediation guidance instead."
        ),
        "uses_llm": False,
        "read_only": False,
        "inputs": ["changes_made", "test_results"],
        "outputs": [
            "e2e_results",
            "scenarios_passed",
            "scenarios_failed",
            "environment_error",
        ],
    },
    StepType.SELF_CHECK: {
        "name": "self_check",
        "description": "Code self-review: check logic completeness, robustness, and test coverage gaps",
        "uses_llm": True,
        "read_only": True,
        "inputs": ["changes_made", "test_results", "spec_content", "task_description"],
        "outputs": ["self_check_result", "issues", "actionable_count"],
    },
    StepType.ADJUDICATE: {
        "name": "adjudicate",
        # Why a distinct adjudication layer (not folded into self_check): the
        # review layer stays high-recall and reports *deviations*; adjudicate is
        # the single place that rules on *spec contradictions*. When the same
        # location is flagged in opposite directions across rounds, self_check
        # cannot break the tie without mixing adjudication into review. This step
        # reads the cross-round fingerprint ledger + the currently-effective
        # task description (no full transcript) and emits a corrected description.
        # Legacy adjudicated_plan data remains in the schema so old flow records
        # stay readable, but it is no longer an acceptance or routing authority.
        "description": (
            "Spec-contradiction adjudication (the fix-loop 'police'): given the "
            "cross-round issue-fingerprint ledger and the currently-effective "
            "task_description, rule on internal spec contradictions, "
            "spec-vs-hard-constraint conflicts, and review divergence. Emits an "
            "adjudicated_description override with "
            "rationale + timestamp so downstream steps take the latest ruling "
            "while original discovery and derived plan outputs stay untouched."
        ),
        "uses_llm": True,
        # Writes only its own outputs (no file edits), but those products drive
        # the flow via the adjudicated > refined > original effective-text layer.
        "read_only": False,
        "inputs": ["adjudication_ledger", "task_description", "plan"],
        "outputs": [
            "adjudicated_description",
            "adjudicated_plan",
            "adjudication_rationale",
            "adjudicated_at",
            "superseded_fix_instructions",
            "rejected_candidates",
        ],
    },
    StepType.INVARIANT_CHECK: {
        "name": "invariant_check",
        # Why an anchored check (not a free self-check): the source pool is the
        # closed set {task_description, charter full text, why-comments of the
        # touched code}. An issue survives only when its verbatim_quote is a
        # substring of that pool, so coverage is limited to invariants that were
        # *explicitly recorded* — unwritten expectations are not machine-guarded.
        "description": (
            "Anchored invariant check (replaces spec_gate/spec_check): judge "
            "whether the diff violates any explicitly recorded binding "
            "invariant. Anchor set = {task_description, charter, touched-code "
            "why-comments}, frozen at flow start; reuses self_check's "
            "verbatim_quote anchoring. No diff / no anchors => cheap pass."
        ),
        "uses_llm": True,
        "read_only": True,
        "inputs": ["task_description", "charter", "changes_made", "why_comments"],
        "outputs": ["issues", "actionable_count", "fix_needed", "fix_instructions", "fix_context"],
    },
    StepType.CHARTER_FRESHNESS: {
        "name": "charter_freshness",
        # WHY: read_only is False even though the LLM sub-call stays read-only.
        # The handler itself may write tianluo/charter.md: sitting after a COMPLETED
        # invariant_check, a *descriptive* charter update (making the constitution
        # reflect the already-approved new reality) is closed inside the handler
        # via propose -> gate -> apply, with no issue and no state-machine route.
        # The registry must declare that write truthfully (write-guard / audit
        # consumers key off read_only), so it is flipped to False; the LLM
        # sub-process is kept read-only out-of-band via LLMCaller(force_read_only)
        # rather than by lying in the registry. Never blocks the flow.
        "description": (
            "Charter freshness: does this diff touch any of charter's three "
            "content classes (project identity / top-level architecture / "
            "project-wide cross-cutting conventions)? On a hit the handler may "
            "write tianluo/charter.md itself — a descriptive, anchored, gated update "
            "closed in-handler (propose -> gate -> apply); write execution is the "
            "handler's Python, the LLM sub-call only proposes text and stays "
            "read-only. The overwhelming majority of flows pass cheaply. Never "
            "blocks the flow."
        ),
        "uses_llm": True,
        "read_only": False,
        "inputs": ["task_description", "charter", "changes_made"],
        "outputs": [
            "charter_update_needed",
            "touched_classes",
            "reason",
            "suggested_update",
            "charter_auto_updated",
            "charter_diff",
            "gate_verdicts",
            "degraded_reason",
        ],
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
    StepType.SPEC_GATE: {
        "name": "spec_gate",
        "description": (
            "Mechanism A: post-update_spec gate. Validates each edited/new spec "
            "(validate_spec_structure + requirement non-decrease for edited specs) "
            "and, on a clean artifact, re-runs the full test suite. Routes back to "
            "update_spec on an invalid artifact or to implement on an introduced "
            "test failure."
        ),
        "uses_llm": False,
        "read_only": False,
        "inputs": ["changes_made", "baseline_failures", "spec_requirement_baseline"],
        "outputs": ["gate_passed", "gate_route", "fix_needed", "fix_instructions", "fix_context"],
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
    StepType.MERGE_INTEGRATE: {
        "name": "merge_integrate",
        # The "integrate" half of the merge-library split: branch merge + LLM
        # conflict resolution + runtime sync + issue renumber + post-condition
        # checks, executed in the MAIN checkout under the merge lock. Its failure
        # mode is "cannot be merged"; when it fails the flow stops before
        # version_reconcile (no version is decided for work that did not land).
        "description": (
            "Integrate the flow branch into master via the integrate() library "
            "entry: sequential git merge, LLM conflict resolution, runtime sync, "
            "issue renumber, post-condition checks. Runs in the main checkout "
            "inside the merge lock. Worktree flows only."
        ),
        "uses_llm": True,  # LLM conflict resolution may fire inside integrate()
        "read_only": False,
        "inputs": ["worktree_branch", "worktree_original_branch"],
        "outputs": ["merge_result", "merged_branches", "pending_human"],
    },
    StepType.VERSION_RECONCILE: {
        "name": "version_reconcile",
        # The "reconcile" half: re-derive the FINAL version at merge time against
        # master's CURRENT version (not the version the session guessed), file the
        # merged-in changelog bullets under it, and commit — unconditionally (it
        # runs on the already-ancestor / no-op-merge path too). Cheap; its failure
        # mode is "version computed wrong", so a resume re-runs only the version
        # decision, never the merge. Idempotent via consumed-intent marking.
        "description": (
            "Reconcile the final project version at merge time via the "
            "reconcile() library entry: collect merged-in session intents, "
            "re-base on master's current version, pick the deterministic SemVer "
            "or custom-rules channel, enforce no-regression, write the version "
            "file + merge the changelog, and commit. Unconditional and "
            "idempotent. Runs in the main checkout inside the merge lock. "
            "Worktree flows only."
        ),
        "uses_llm": True,  # only in the custom version-rules channel
        "read_only": False,
        "inputs": ["worktree_branch"],
        "outputs": ["reconcile_result", "final_version", "base_version", "channel"],
    },
}


def get_step_info(step_type: StepType) -> Dict[str, Any]:
    """Get information about a step type from the step pool."""
    return STEP_POOL.get(step_type, {})


def get_default_step_sequence(task_type: str = "feature") -> List[StepType]:
    """Get the default step sequence for a given task type.

    This is the initial selection - the analyze step can modify this.

    WHY: an unknown ``task_type`` falls back to the feature sequence instead of
    raising. Retired task types stay persisted in old ``engine.json`` files, and
    this lookup is also used as a pure table read on already-stored types
    (``version_reconcile`` reverse-derives whether a type was ever supposed to
    produce a version intent). Raising here would break ``--resume`` and merge
    reconciliation for those historical flows. Rejecting an unsupported type is
    the job of the CLI ``--type`` entry check, which guards the single point
    where a *user* can introduce one.
    """
    # The spec governance steps (VERIFY_SPEC / UPDATE_SPEC / SPEC_GATE) were
    # retired by the charter refactor. Their role is replaced by:
    #   - INVARIANT_CHECK — an anchored diff check (charter + why-comments +
    #     task) inserted right after SELF_CHECK; it can return REVISION_NEEDED
    #     and drives the shared fix loop, so it sits inside the fix-loop window.
    #   - CHARTER_FRESHNESS — a non-blocking advisory inserted just before
    #     VERSION_ANALYZE; it only surfaces an update prompt and never blocks.
    # The lightweight commit-only flow (small) gets CHARTER_FRESHNESS but not
    # INVARIANT_CHECK (it has no self_check/spec phase to extend).
    #
    # WHY StepType.E2E appears in NO sequence below: e2e is opt-in per project
    # (``e2e.enabled`` in tianluo.yaml) and the guarantee it must keep is that a
    # project which has NOT enabled it sees a byte-identical step sequence to the
    # one it saw before the subsystem existed. Putting E2E in these tables and
    # having the handler no-op when disabled would break that: the step would
    # show up in every project's step list, shift the progress percentage, and
    # make a ``--resume`` of an old flow disagree with the freshly derived
    # sequence. So the step is *conditionally inserted* instead, by
    # ``config.insert_e2e_step`` (right after the first TEST, hence before
    # SELF_CHECK), from the two places that rebuild a sequence:
    # ``StateMachine.create_flow`` and ``analyze._update_flow_steps``.
    sequences: Dict[str, List[StepType]] = {
        "feature": [
            StepType.ANALYZE,
            StepType.PLAN,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
            StepType.INVARIANT_CHECK,
            StepType.CHARTER_FRESHNESS,
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
            StepType.SUMMARIZE,
        ],
        "bugfix": [
            StepType.ANALYZE,
            StepType.PLAN,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
            StepType.INVARIANT_CHECK,
            StepType.CHARTER_FRESHNESS,
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
            StepType.SUMMARIZE,
        ],
        "review": [
            StepType.ANALYZE,
            StepType.INVARIANT_CHECK,
            StepType.SUMMARIZE,
        ],
        "small": [
            StepType.ANALYZE,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.CHARTER_FRESHNESS,
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
            StepType.SUMMARIZE,
        ],
        # WHY survey carries no IMPLEMENT/TEST/COMMIT and no VERSION_ANALYZE:
        # its deliverable is a conclusion, not a code change — there is nothing
        # to test, commit or version. Omitting VERSION_ANALYZE needs no new
        # branch anywhere: version_reconcile decides positively from the task
        # type's default sequence, so a sequence without VERSION_ANALYZE falls
        # into its existing noop short-circuit. Ending on SUMMARIZE (a tail with
        # no commit) is the shape the review sequence already proved viable.
        "survey": [
            StepType.ANALYZE,
            StepType.INVESTIGATE,
            StepType.SUMMARIZE,
        ],
        "discovery": [
            StepType.DISCOVERY,
            StepType.ANALYZE,
            StepType.PLAN,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.SELF_CHECK,
            StepType.INVARIANT_CHECK,
            StepType.CHARTER_FRESHNESS,
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
            StepType.SUMMARIZE,
        ],
    }
    return sequences.get(task_type, sequences["feature"])
